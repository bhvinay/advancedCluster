import json
import os
import shlex
import socket
import time
import uuid
import ipaddress
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import urlopen

import boto3
import botocore
import click
import paramiko


DEFAULT_INSTANCE_TYPE = "c6i.4xlarge"
DEFAULT_DISK_GB = 30
DEFAULT_VPC_CIDR = "10.0.0.0/16"
DEFAULT_SUBNET_CIDR = "10.0.1.0/24"


def detect_public_ip() -> Optional[str]:
    try:
        with urlopen("https://checkip.amazonaws.com", timeout=5) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return None


def validate_cidr(cidr: str) -> bool:
    try:
        ipaddress.ip_network(cidr, strict=False)
        return True
    except ValueError:
        return False


def parse_tags(tag_str: str) -> List[Dict[str, str]]:
    tags = []
    for part in [p.strip() for p in tag_str.split(",") if p.strip()]:
        if "=" not in part:
            raise click.ClickException(f"Invalid tag '{part}'. Use key=value format.")
        key, value = [x.strip() for x in part.split("=", 1)]
        if not key or not value:
            raise click.ClickException(f"Invalid tag '{part}'. Use key=value format.")
        tags.append({"Key": key, "Value": value})
    return tags


def load_ec2_config() -> Dict:
    """Load EC2 configuration from ec2_config.json"""
    config_file = Path(__file__).parent / "ec2_config.json"
    if not config_file.exists():
        return {}
    try:
        with open(config_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        click.echo(f"Warning: Failed to load ec2_config.json: {e}")
        return {}


def load_mandatory_tags() -> List[Dict[str, str]]:
    """Load mandatory AWS tags from aws_mandatory_tags.json"""
    tags_file = Path(__file__).parent / "aws_mandatory_tags.json"
    if not tags_file.exists():
        click.echo(f"Warning: Mandatory tags file not found: {tags_file}")
        return []
    try:
        with open(tags_file, 'r') as f:
            tags_dict = json.load(f)
        return [{"Key": k, "Value": v} for k, v in tags_dict.items()]
    except Exception as e:
        click.echo(f"Warning: Failed to load mandatory tags: {e}")
        return []


def add_internal_tags(tags: List[Dict[str, str]], run_id: str) -> List[Dict[str, str]]:
    internal = [
        {"Key": "ProvisionedBy", "Value": "codec-ec2"},
        {"Key": "RunId", "Value": run_id},
        {"Key": "CreatedAt", "Value": datetime.now(timezone.utc).isoformat()},
    ]
    return tags + internal


def tags_to_dict(tags: List[Dict[str, str]]) -> Dict[str, str]:
    return {t["Key"]: t["Value"] for t in tags}


def pick_from_list(items: List[Dict[str, str]], label_fn, prompt: str) -> Dict[str, str]:
    for idx, item in enumerate(items, start=1):
        click.echo(f"[{idx}] {label_fn(item)}")
    choice = click.prompt(prompt, type=int, default=1)
    try:
        return items[choice - 1]
    except IndexError as exc:
        raise click.ClickException("Invalid selection") from exc


def ensure_keypair(ec2, tags: List[Dict[str, str]], run_id: str, config: Dict = None):
    # Check if there's a default key pair in config
    if config and "default_key_pair" in config:
        default_kp = config["default_key_pair"]
        default_name = default_kp.get("name")
        default_path = default_kp.get("path")

        if default_name and default_path:
            # Verify the key pair exists in AWS and the file exists
            try:
                ec2.describe_key_pairs(KeyNames=[default_name])
                if Path(default_path).exists():
                    if click.confirm(f"Use default key pair '{default_name}'?", default=True):
                        click.echo(f"Using key pair: {default_name}")
                        click.echo(f"Key path: {default_path}")
                        return default_name, default_path
                else:
                    click.echo(f"Warning: Default key file not found at {default_path}")
            except botocore.exceptions.ClientError:
                click.echo(f"Warning: Default key pair '{default_name}' not found in AWS")

    if click.confirm("Use existing EC2 key pair?", default=False):
        key_name = click.prompt("Key pair name")
        try:
            ec2.describe_key_pairs(KeyNames=[key_name])
        except botocore.exceptions.ClientError as exc:
            raise click.ClickException(f"Key pair {key_name} not found: {exc}") from exc
        while True:
            key_path = click.prompt(
                "Path to private key (.pem)",
                default=str(Path.home() / ".codec" / "keys" / f"{key_name}.pem"),
            )
            if Path(key_path).exists():
                return key_name, key_path
            click.echo(f"Key file not found: {key_path}. Please provide a valid path.")

    key_name = click.prompt("New key pair name", default=f"codec-ec2-{run_id[:8]}")
    try:
        ec2.describe_key_pairs(KeyNames=[key_name])
        if click.confirm(f"Key pair {key_name} already exists. Reuse it?", default=True):
            while True:
                key_path = click.prompt(
                    "Path to private key (.pem)",
                    default=str(Path.home() / ".codec" / "keys" / f"{key_name}.pem"),
                )
                if Path(key_path).exists():
                    return key_name, key_path
                click.echo(f"Key file not found: {key_path}. Please provide a valid path.")
        key_name = click.prompt("New key pair name")
    except botocore.exceptions.ClientError:
        pass

    try:
        response = ec2.create_key_pair(
            KeyName=key_name,
            TagSpecifications=[{"ResourceType": "key-pair", "Tags": tags}],
        )
    except botocore.exceptions.ClientError as exc:
        raise click.ClickException(f"Failed to create key pair: {exc}") from exc

    key_material = response["KeyMaterial"]
    key_dir = Path.home() / ".codec" / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"{key_name}.pem"
    if key_path.exists() and not click.confirm(f"{key_path} exists. Overwrite?", default=False):
        raise click.ClickException("Key file already exists; choose a different key name.")
    key_path.write_text(key_material, encoding="utf-8")
    os.chmod(key_path, 0o400)
    click.echo(f"Saved private key to {key_path}")
    return key_name, str(key_path)


def list_vpcs(ec2):
    return ec2.describe_vpcs()["Vpcs"]


def list_subnets(ec2, vpc_id: str):
    return ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]


def list_sgs(ec2, vpc_id: str):
    return ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]


def is_subnet_public(ec2, subnet_id: str) -> bool:
    subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
    vpc_id = subnet["VpcId"]

    route_tables = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
    )["RouteTables"]
    if not route_tables:
        route_tables = ec2.describe_route_tables(
            Filters=[
                {"Name": "association.main", "Values": ["true"]},
                {"Name": "vpc-id", "Values": [vpc_id]},
            ]
        )["RouteTables"]
    for rt in route_tables:
        for route in rt.get("Routes", []):
            if route.get("DestinationCidrBlock") == "0.0.0.0/0" and route.get("GatewayId", "").startswith("igw-"):
                return True
    return False


def create_network(ec2, tags: List[Dict[str, str]]):
    vpc = ec2.create_vpc(CidrBlock=DEFAULT_VPC_CIDR)["Vpc"]
    vpc_id = vpc["VpcId"]
    ec2.create_tags(Resources=[vpc_id], Tags=tags)
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    azs = ec2.describe_availability_zones()["AvailabilityZones"]
    az = azs[0]["ZoneName"]
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=DEFAULT_SUBNET_CIDR, AvailabilityZone=az)["Subnet"]
    subnet_id = subnet["SubnetId"]
    ec2.create_tags(Resources=[subnet_id], Tags=tags)
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})

    igw = ec2.create_internet_gateway()["InternetGateway"]
    igw_id = igw["InternetGatewayId"]
    ec2.create_tags(Resources=[igw_id], Tags=tags)
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)

    rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]
    rt_id = rt["RouteTableId"]
    ec2.create_tags(Resources=[rt_id], Tags=tags)
    ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)

    return {
        "vpc_id": vpc_id,
        "subnet_id": subnet_id,
        "igw_id": igw_id,
        "route_table_id": rt_id,
        "subnet_created": True,
        "igw_created": True,
    }


def pick_subnet_cidr(vpc_cidr: str, used_cidrs: List[str]) -> str:
    vpc_net = ipaddress.ip_network(vpc_cidr)
    if vpc_net.prefixlen <= 24:
        target_prefix = 24
    elif vpc_net.prefixlen < 28:
        target_prefix = vpc_net.prefixlen + 1
    else:
        raise click.ClickException("VPC CIDR is too small to create a subnet.")

    used = [ipaddress.ip_network(c) for c in used_cidrs]
    for subnet in vpc_net.subnets(new_prefix=target_prefix):
        if any(subnet.overlaps(u) for u in used):
            continue
        return str(subnet)
    raise click.ClickException("No available CIDR block found for a new subnet.")


def ensure_igw_and_route(ec2, vpc_id: str, subnet_id: str, tags: List[Dict[str, str]]):
    igws = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    )["InternetGateways"]
    if igws:
        igw_id = igws[0]["InternetGatewayId"]
        created_igw = False
    else:
        igw = ec2.create_internet_gateway()["InternetGateway"]
        igw_id = igw["InternetGatewayId"]
        ec2.create_tags(Resources=[igw_id], Tags=tags)
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        created_igw = True

    rt = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]
    rt_id = rt["RouteTableId"]
    ec2.create_tags(Resources=[rt_id], Tags=tags)
    try:
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "RouteAlreadyExists":
            raise
    ec2.associate_route_table(RouteTableId=rt_id, SubnetId=subnet_id)
    return igw_id, rt_id, created_igw


def create_subnet_in_vpc(ec2, vpc_id: str, tags: List[Dict[str, str]]):
    vpc = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    used_subnets = [s["CidrBlock"] for s in list_subnets(ec2, vpc_id)]
    cidr = pick_subnet_cidr(vpc["CidrBlock"], used_subnets)
    azs = ec2.describe_availability_zones()["AvailabilityZones"]
    az = azs[0]["ZoneName"]
    subnet = ec2.create_subnet(VpcId=vpc_id, CidrBlock=cidr, AvailabilityZone=az)["Subnet"]
    subnet_id = subnet["SubnetId"]
    ec2.create_tags(Resources=[subnet_id], Tags=tags)
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    igw_id, rt_id, igw_created = ensure_igw_and_route(ec2, vpc_id, subnet_id, tags)
    return {
        "subnet_id": subnet_id,
        "igw_id": igw_id,
        "route_table_id": rt_id,
        "igw_created": igw_created,
    }


def ensure_security_group(ec2, vpc_id: str, ssh_cidr: str, tags: List[Dict[str, str]]):
    if click.confirm("Use existing Security Group?", default=False):
        sgs = list_sgs(ec2, vpc_id)
        if not sgs:
            raise click.ClickException("No security groups found in the selected VPC.")
        selected = pick_from_list(
            sgs,
            lambda sg: f"{sg['GroupId']} {sg.get('GroupName', '')}",
            "Select Security Group",
        )
        return selected["GroupId"], False

    name = f"codec-ec2-sg-{uuid.uuid4().hex[:8]}"
    sg = ec2.create_security_group(
        GroupName=name,
        Description="codec-ec2 SSH-only security group",
        VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tags}],
    )
    sg_id = sg["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": ssh_cidr}],
            }
        ],
    )
    try:
        ec2.authorize_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "InvalidPermission.Duplicate":
            raise
    return sg_id, True


def resolve_ami(ssm, ec2, linux_version: str) -> str:
    param_name = (
        "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
        if linux_version == "2023"
        else "/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"
    )
    try:
        return ssm.get_parameter(Name=param_name)["Parameter"]["Value"]
    except botocore.exceptions.ClientError:
        pass

    name_filter = "al2023-ami-*-x86_64" if linux_version == "2023" else "amzn2-ami-hvm-*-x86_64-gp2"
    images = ec2.describe_images(
        Owners=["amazon"],
        Filters=[{"Name": "name", "Values": [name_filter]}],
    )["Images"]
    if not images:
        raise click.ClickException("Unable to resolve Amazon Linux AMI.")
    images.sort(key=lambda img: img["CreationDate"], reverse=True)
    return images[0]["ImageId"]


def wait_for_ssh(host: str, port: int = 22, timeout: int = 300):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=5):
                return
        except OSError:
            time.sleep(5)
    raise click.ClickException("SSH did not become reachable in time.")


def run_ssh_commands(host: str, key_path: str, commands: List[str]):
    key = None
    key_loaders = [
        paramiko.RSAKey.from_private_key_file,
        paramiko.ECDSAKey.from_private_key_file,
        paramiko.Ed25519Key.from_private_key_file,
    ]
    for loader in key_loaders:
        try:
            key = loader(key_path)
            break
        except paramiko.SSHException:
            continue
    if not key:
        raise click.ClickException(f"Unsupported or unreadable private key: {key_path}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username="ec2-user", pkey=key, timeout=10)
    try:
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                raise click.ClickException(f"Command failed: {cmd}\n{stderr.read().decode('utf-8')}")
    finally:
        client.close()


def bootstrap_infa(host: str, key_path: str, passwordless_sudo: bool):
    commands = [
        "id -u infa >/dev/null 2>&1 || sudo useradd -m -s /bin/bash infa",
        "sudo usermod -aG wheel infa",
        "sudo mkdir -p /home/infa/.ssh",
        "sudo cp /home/ec2-user/.ssh/authorized_keys /home/infa/.ssh/authorized_keys",
        "sudo chown -R infa:infa /home/infa/.ssh",
        "sudo chmod 700 /home/infa/.ssh",
        "sudo chmod 600 /home/infa/.ssh/authorized_keys",
    ]
    if passwordless_sudo:
        commands.append("echo 'infa ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/infa >/dev/null")
        commands.append("sudo chmod 440 /etc/sudoers.d/infa")
    commands.extend(["id infa", "sudo -l -U infa"])
    run_ssh_commands(host, key_path, commands)


def load_manifest(manifest_path: str) -> Dict:
    """Load and validate the EC2 provisioning manifest file."""
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        raise click.ClickException(
            f"Manifest file not found: {manifest_path}\n"
            "Run 'codec_ec2.py up' first to provision an EC2 instance."
        )

    try:
        with open(manifest_file, 'r') as f:
            manifest = json.load(f)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON in manifest file: {exc}") from exc

    # Validate required fields
    required_fields = [
        ("instance", "public_ip"),
        ("keypair", "path"),
    ]
    for section, field in required_fields:
        if section not in manifest:
            raise click.ClickException(f"Manifest missing '{section}' section")
        if field not in manifest[section]:
            raise click.ClickException(f"Manifest missing '{section}.{field}' field")

    return manifest


def get_pod_url(pod_choice: str, custom_url: Optional[str] = None) -> str:
    """Map pod choice to full IDMC URL."""
    pod_mapping = {
        "dm-us": "https://dm-us.informaticacloud.com",
        "dm-ap": "https://dm-ap.informaticacloud.com",
        "dm-em": "https://dm-em.informaticacloud.com",
    }

    if pod_choice == "custom":
        if not custom_url:
            raise click.ClickException("Custom pod URL must be provided")
        return custom_url

    return pod_mapping.get(pod_choice, pod_mapping["dm-us"])


def prompt_idmc_config() -> Dict[str, str]:
    """Prompt user for IDMC configuration interactively."""
    click.echo("\n=== IDMC Agent Configuration ===")

    # Pod URL selection
    pod_choice = click.prompt(
        "Select IDMC Pod",
        type=click.Choice(["dm-us", "dm-ap", "dm-em", "custom"]),
        default="dm-us"
    )

    custom_url = None
    if pod_choice == "custom":
        custom_url = click.prompt("Enter custom pod URL (e.g., https://dm-custom.informaticacloud.com)")

    pod_url = get_pod_url(pod_choice, custom_url)

    # Email (required for registration)
    email = click.prompt("IDMC user email")

    # Install token (hidden input for security)
    token = click.prompt("IDMC install token", hide_input=True)

    # Optional agent group
    agent_group = click.prompt(
        "Agent group name (optional, press Enter to skip)",
        default="",
        show_default=False
    )

    return {
        "pod_url": pod_url,
        "email": email,
        "token": token,
        "agent_group": agent_group if agent_group else None,
    }


def scp_file_to_host(host: str, key_path: str, local_path: str, remote_path: str, username: str = "infa"):
    """Transfer file to remote host via SCP using paramiko."""
    # Load SSH key (reuse logic from run_ssh_commands)
    key = None
    key_loaders = [
        paramiko.RSAKey.from_private_key_file,
        paramiko.ECDSAKey.from_private_key_file,
        paramiko.Ed25519Key.from_private_key_file,
    ]
    for loader in key_loaders:
        try:
            key = loader(key_path)
            break
        except paramiko.SSHException:
            continue
    if not key:
        raise click.ClickException(f"Unsupported or unreadable private key: {key_path}")

    # Create SSH client
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(hostname=host, username=username, pkey=key, timeout=10)

        # Use SFTP to transfer file
        sftp = client.open_sftp()
        click.echo(f"Transferring {local_path} to {host}:{remote_path}...")
        sftp.put(local_path, remote_path)
        sftp.close()

        # Set executable permissions
        stdin, stdout, stderr = client.exec_command(f"chmod +x {remote_path}")
        stdout.channel.recv_exit_status()

        click.echo("File transfer completed")
    finally:
        client.close()


def install_idmc_agent_on_host(host: str, key_path: str, installer_source: Dict, idmc_config: Dict):
    """Install and configure IDMC agent on EC2 instance."""
    click.echo("\n=== Installing IDMC Agent ===")

    # Build remote installer path
    remote_installer = "/tmp/agent_installer.bin"

    # Step 1: Transfer or download installer
    if installer_source["type"] == "local":
        scp_file_to_host(host, key_path, installer_source["path"], remote_installer, "infa")
    elif installer_source["type"] == "url":
        click.echo(f"Downloading installer from {installer_source['url']}...")
        download_cmd = [
            f"curl -L -o {remote_installer} '{installer_source['url']}'",
            f"sudo chown infa:infa {remote_installer}",
            f"sudo chmod +x {remote_installer}",
        ]
        run_ssh_commands(host, key_path, download_cmd)

    # Step 2: Build installation command sequence
    pod_url = idmc_config["pod_url"]
    email = idmc_config["email"]
    token = idmc_config["token"]
    agent_group = idmc_config.get("agent_group")

    # Escape pod URL for ini file (replace : with \\:)
    escaped_pod_url = pod_url.replace("https://", "https\\\\://")

    commands = [
        # Install dependencies
        "sudo yum install -y libnsl",

        # Create installation directory
        "sudo mkdir -p /opt/infaagent",
        "sudo chown infa:infa /opt/infaagent",

        # Run installer as infa user
        f"sudo -u infa {remote_installer} -i silent -DUSER_INSTALL_DIR=/opt/infaagent",

        # Verify installation
        "test -f /opt/infaagent/apps/agentcore/infaagent && echo 'INSTALL_SUCCESS' || echo 'INSTALL_FAILED'",
    ]

    # Create configuration file
    config_content = f"""InfaAgent.UseToken=true
#
InfaAgent.MasterUrl={escaped_pod_url}
InfaAgent.cloudProvider=AWS"""

    if agent_group:
        config_content += f"\nInfaAgent.GroupName={agent_group}"

    commands.extend([
        # Write configuration file
        (
            "cat << 'EOFCONFIG' | sudo tee /opt/infaagent/apps/agentcore/conf/infaagent.ini > /dev/null\n"
            f"{config_content}\n"
            "EOFCONFIG"
        ),

        # Set ownership
        "sudo chown -R infa:infa /opt/infaagent",

        # Configure auto-start
        "sudo bash -c \"echo 'sudo su - infa -c \\\"cd /opt/infaagent/apps/agentcore;./infaagent startup\\\"' >> /etc/rc.d/rc.local\"",
        "sudo chmod +x /etc/rc.d/rc.local",

        # Start agent
        "sudo -u infa bash -lc 'cd /opt/infaagent/apps/agentcore && ./infaagent startup'",
    ])

    click.echo("Running installation commands...")
    run_ssh_commands(host, key_path, commands)

    # Wait for agent to start
    click.echo("Waiting 2 minutes for agent to initialize...")
    time.sleep(120)

    # Verify agent is running and register
    safe_email = shlex.quote(email)
    safe_token = shlex.quote(token)
    register_commands = [
        "ps aux | grep infaagent | grep -v grep && echo 'AGENT_RUNNING' || echo 'AGENT_NOT_RUNNING'",

        # Register agent with IDMC (using email parameter)
        (
            "sudo -u infa bash -lc "
            f"'cd /opt/infaagent/apps/agentcore && ./consoleAgentManager.sh configureToken {safe_email} {safe_token}'"
        ),

        # Cleanup installer
        f"sudo rm -f {remote_installer}",
    ]

    click.echo("Registering agent with IDMC...")
    run_ssh_commands(host, key_path, register_commands)


def update_manifest_with_agent(manifest_path: str, agent_info: Dict):
    """Update manifest file with IDMC agent installation details."""
    manifest = load_manifest(manifest_path)

    manifest["idmc_agent"] = {
        "installed": True,
        "installation_timestamp": datetime.now(timezone.utc).isoformat(),
        "pod_url": agent_info["pod_url"],
        "agent_group": agent_info.get("agent_group"),
        "installation_path": "/opt/infaagent",
        "agent_email": agent_info["email"],
        "registration_status": "registered",
        "installer_source": agent_info["installer_source"],
    }

    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)

    click.echo(f"Manifest updated: {manifest_path}")


@click.group()
def cli():
    pass


@cli.command()
@click.option("--profile", help="AWS profile", default=None)
@click.option("--region", help="AWS region override", default=None)
def up(profile, region):
    # Load configuration
    ec2_config = load_ec2_config()

    session = boto3.session.Session(profile_name=profile, region_name=region)
    if not session.region_name:
        region = click.prompt("AWS region")
        session = boto3.session.Session(profile_name=profile, region_name=region)

    sts = session.client("sts")
    ec2 = session.client("ec2")
    ssm = session.client("ssm")

    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    click.echo(f"Authenticated as {identity['Arn']} in account {account_id}")

    run_id = uuid.uuid4().hex

    caller_ip = detect_public_ip()
    ssh_cidr = None
    if caller_ip and click.confirm(f"Detected caller IP {caller_ip}. Use for SSH allowlist?", default=True):
        ssh_cidr = f"{caller_ip}/32"
    if not ssh_cidr:
        while True:
            ssh_cidr = click.prompt("Enter CIDR for SSH allowlist (e.g., 1.2.3.4/32)")
            if validate_cidr(ssh_cidr):
                break
            click.echo(f"Invalid CIDR: {ssh_cidr}. Please use valid CIDR notation.")

    # Load mandatory tags first
    mandatory_tags = load_mandatory_tags()
    if mandatory_tags:
        click.echo(f"Loaded {len(mandatory_tags)} mandatory tags from aws_mandatory_tags.json")
        for tag in mandatory_tags:
            click.echo(f"  - {tag['Key']}: {tag['Value']}")

    # Get additional tags from user (optional)
    tag_input = click.prompt("Enter additional tags (comma-separated key=value, or press Enter to skip)", default="")
    user_tags = parse_tags(tag_input) if tag_input.strip() else []

    # Merge all tags: mandatory + user + internal
    all_tags = mandatory_tags + user_tags
    tags = add_internal_tags(all_tags, run_id)

    key_name, key_path = ensure_keypair(ec2, tags, run_id, ec2_config)

    if click.confirm("Use existing VPC?", default=True):
        vpcs = list_vpcs(ec2)
        if not vpcs:
            raise click.ClickException("No VPCs found in this region.")
        vpc = pick_from_list(
            vpcs,
            lambda v: f"{v['VpcId']} {v.get('CidrBlock')} {v.get('IsDefault', False)}",
            "Select VPC",
        )
        vpc_id = vpc["VpcId"]
        subnets = list_subnets(ec2, vpc_id)
        created_subnet = False
        created_igw = False
        route_table_id = None
        if not subnets:
            if not click.confirm("No subnets found. Create a public subnet in this VPC?", default=True):
                raise click.ClickException("No subnets found in the selected VPC.")
            subnet_info = create_subnet_in_vpc(ec2, vpc_id, tags)
            subnet_id = subnet_info["subnet_id"]
            created_subnet = True
            created_igw = subnet_info["igw_created"]
            route_table_id = subnet_info["route_table_id"]
        else:
            subnet = pick_from_list(
                subnets,
                lambda s: f"{s['SubnetId']} {s.get('CidrBlock')} {s.get('AvailabilityZone')}",
                "Select subnet",
            )
            subnet_id = subnet["SubnetId"]
        if not is_subnet_public(ec2, subnet_id):
            click.echo("Warning: selected subnet may not be public; SSH could fail.")
        network_created = created_subnet or created_igw
        network_ids = {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "route_table_id": route_table_id,
            "igw_created": created_igw,
            "subnet_created": created_subnet,
        }
    else:
        click.echo("Creating new VPC + public subnet...")
        network_ids = create_network(ec2, tags)
        vpc_id = network_ids["vpc_id"]
        subnet_id = network_ids["subnet_id"]
        network_created = True

    sg_id, sg_created = ensure_security_group(ec2, vpc_id, ssh_cidr, tags)

    # Use config defaults if available
    default_linux = ec2_config.get("default_linux_version", "2023")
    default_instance = ec2_config.get("default_instance_type", DEFAULT_INSTANCE_TYPE)
    default_disk = ec2_config.get("default_disk_gb", DEFAULT_DISK_GB)

    linux_version = click.prompt("Amazon Linux version (2023 or 2)", default=default_linux)
    instance_type = click.prompt("Instance type", default=default_instance)
    disk_gb = click.prompt("Disk size (GiB)", type=int, default=default_disk)
    passwordless = click.confirm("Enable passwordless sudo for infa?", default=False)

    ami_id = resolve_ami(ssm, ec2, "2023" if linux_version == "2023" else "2")

    click.echo("Launching EC2 instance...")
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        KeyName=key_name,
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": disk_gb,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
    )
    instance_id = response["Instances"][0]["InstanceId"]

    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    ec2.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id])

    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    public_ip = instance.get("PublicIpAddress")
    public_dns = instance.get("PublicDnsName")
    if not public_ip:
        raise click.ClickException("Instance does not have a public IP. Ensure subnet is public.")

    click.echo("Waiting for SSH...")
    wait_for_ssh(public_ip)
    bootstrap_infa(public_ip, key_path, passwordless)

    manifest = {
        "run_id": run_id,
        "account_id": account_id,
        "region": session.region_name,
        "instance": {
            "instance_id": instance_id,
            "public_ip": public_ip,
            "public_dns": public_dns,
            "instance_type": instance_type,
            "ami_id": ami_id,
        },
        "network": {
            "vpc_id": vpc_id,
            "subnet_id": subnet_id,
            "security_group_id": sg_id,
            "network_created": network_created,
            "security_group_created": sg_created,
            "subnet_created": network_ids.get("subnet_created"),
            "igw_created": network_ids.get("igw_created"),
            "route_table_id": network_ids.get("route_table_id"),
        },
        "keypair": {"name": key_name, "path": key_path},
        "tags": tags_to_dict(tags),
    }
    manifest_path = Path.cwd() / "codec-ec2-run.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    click.echo("")
    click.echo("=== Instance Ready ===")
    click.echo(f"- InstanceId: {instance_id}")
    click.echo(f"- Public IP: {public_ip}")
    click.echo(f"- Public DNS: {public_dns}")
    click.echo(f"- Key path: {key_path}")
    click.echo(f"- SSH (ec2-user): ssh -i {key_path} ec2-user@{public_ip}")
    click.echo(f"- SSH (infa): ssh -i {key_path} infa@{public_ip}")
    click.echo(f"- Manifest: {manifest_path}")


@cli.command()
@click.option("--manifest", default="codec-ec2-run.json", help="Path to manifest file")
@click.option("--installer-path", help="Local path to agent installer .bin file")
@click.option("--installer-url", help="URL to download agent installer")
def install_agent(manifest, installer_path, installer_url):
    """Install and register Informatica IDMC Secure Agent on provisioned EC2 instance."""

    # Load manifest to get instance details
    click.echo("Loading manifest file...")
    manifest_data = load_manifest(manifest)

    instance_ip = manifest_data["instance"]["public_ip"]
    key_path = manifest_data["keypair"]["path"]

    click.echo(f"Target instance: {instance_ip}")

    # Determine installer source
    installer_source = None

    if installer_path and installer_url:
        raise click.ClickException(
            "Cannot specify both --installer-path and --installer-url. Choose one."
        )

    if installer_path:
        # Validate local file exists
        installer_file = Path(installer_path)
        if not installer_file.exists():
            raise click.ClickException(f"Installer file not found: {installer_path}")
        installer_source = {"type": "local", "path": str(installer_file.absolute())}
        click.echo(f"Using local installer: {installer_path}")

    elif installer_url:
        installer_source = {"type": "url", "url": installer_url}
        click.echo(f"Using installer URL: {installer_url}")

    else:
        # Prompt user for choice
        click.echo("\n=== Agent Installer Source ===")
        source_type = click.prompt(
            "How would you like to provide the installer?",
            type=click.Choice(["local", "url"]),
            default="local"
        )

        if source_type == "local":
            installer_path = click.prompt("Path to local installer .bin file")
            installer_file = Path(installer_path)
            if not installer_file.exists():
                raise click.ClickException(f"Installer file not found: {installer_path}")
            installer_source = {"type": "local", "path": str(installer_file.absolute())}

        else:  # url
            installer_url = click.prompt("Installer download URL")
            installer_source = {"type": "url", "url": installer_url}

    # Prompt for IDMC configuration
    idmc_config = prompt_idmc_config()

    # Confirm before proceeding
    click.echo("\n=== Installation Summary ===")
    click.echo(f"Target Instance: {instance_ip}")
    click.echo(f"IDMC Pod: {idmc_config['pod_url']}")
    click.echo(f"Email: {idmc_config['email']}")
    if idmc_config.get("agent_group"):
        click.echo(f"Agent Group: {idmc_config['agent_group']}")
    click.echo(f"Installer Source: {installer_source['type']}")

    if not click.confirm("\nProceed with installation?", default=True):
        click.echo("Installation cancelled")
        return

    # Install agent
    try:
        install_idmc_agent_on_host(instance_ip, key_path, installer_source, idmc_config)

        # Update manifest with installation details
        agent_info = {
            "pod_url": idmc_config["pod_url"],
            "email": idmc_config["email"],
            "agent_group": idmc_config.get("agent_group"),
            "installer_source": installer_source["type"],
        }
        update_manifest_with_agent(manifest, agent_info)

        click.echo("\n=== Installation Complete ===")
        click.echo(f"IDMC Agent installed successfully on {instance_ip}")
        click.echo(f"Pod URL: {idmc_config['pod_url']}")
        click.echo(f"Installation Path: /opt/infaagent")
        click.echo("\nVerify the agent in IDMC Administrator Console:")
        click.echo("  Runtime Environments > Secure Agents")
        click.echo(f"\nSSH to instance: ssh -i {key_path} infa@{instance_ip}")

    except Exception as exc:
        click.echo(f"\nInstallation failed: {exc}", err=True)
        raise


if __name__ == "__main__":
    cli()
