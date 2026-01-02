import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import botocore
import click


TAG_DEFAULTS = [
    {"Key": "Project", "Value": "Informatica"},
    {"Key": "ManagedBy", "Value": "InformaticaCLI"},
]

def format_with_context(obj: Any, ctx: Dict[str, str]) -> Any:
    """Recursively format strings in a mapping/list with context keys."""
    if isinstance(obj, str):
        return obj.format(**ctx)
    if isinstance(obj, list):
        return [format_with_context(item, ctx) for item in obj]
    if isinstance(obj, dict):
        return {key: format_with_context(value, ctx) for key, value in obj.items()}
    return obj


def load_role_specs(path: str | None, ctx: Dict[str, str]) -> List[Dict[str, Any]]:
    if path:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            specs = json.load(handle)
        return [format_with_context(spec, ctx) for spec in specs]
    return []


def ensure_role(iam_client, role_name, trust_doc, policy_doc, policy_name: Optional[str] = None):
    """Create or reuse a role, then attach an inline policy."""
    try:
        role = iam_client.get_role(RoleName=role_name)["Role"]
        click.echo(f"Reusing role {role_name} ({role['Arn']})")
    except iam_client.exceptions.NoSuchEntityException:
        role = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_doc),
            Tags=TAG_DEFAULTS,
        )["Role"]
        click.echo(f"Created role {role_name} ({role['Arn']})")

    inline_policy_name = policy_name or f"{role_name}Inline"
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=inline_policy_name,
        PolicyDocument=json.dumps(policy_doc),
    )
    return role["Arn"]


def choose_vpc(ec2_client, allow_skip: bool = True):
    """Pick an existing VPC (prefers default) or skip if allowed."""
    vpcs = ec2_client.describe_vpcs()["Vpcs"]
    if not vpcs:
        if allow_skip:
            click.echo("No VPCs found. Skipping VPC selection (product will create VPC).")
            return None
        raise click.ClickException("No VPCs found; create one first or implement --create-vpc.")

    # Deduplicate by VPC ID to avoid duplicates in some accounts.
    seen = set()
    unique_vpcs = []
    for vpc in vpcs:
        if vpc["VpcId"] not in seen:
            unique_vpcs.append(vpc)
            seen.add(vpc["VpcId"])

    default_vpc = next((v for v in unique_vpcs if v.get("IsDefault")), None)
    ordered = []
    if default_vpc:
        ordered.append(default_vpc)
    for vpc in unique_vpcs:
        if default_vpc and vpc["VpcId"] == default_vpc["VpcId"]:
            continue
        ordered.append(vpc)

    click.echo("Select VPC (default first). Enter 0 to skip (product will create VPC).")
    for idx, vpc in enumerate(ordered, start=1):
        cidr = vpc.get("CidrBlock", "")
        click.echo(f"[{idx}] {vpc['VpcId']} {cidr}")

    choice_str = click.prompt("Enter choice number", default="1", show_default=True)
    if allow_skip and choice_str.strip() == "0":
        click.echo("Skipping VPC selection (product will create VPC).")
        return None
    try:
        choice = int(choice_str)
    except ValueError as exc:
        raise click.ClickException("Invalid selection") from exc
    try:
        selected = ordered[choice - 1]
    except IndexError as exc:
        raise click.ClickException("Invalid selection") from exc
    return selected["VpcId"]


def choose_subnets(ec2_client, vpc_id: str | None, minimum: int = 1):
    """Let the user choose subnets within a VPC; allow skip if no VPC or none selected."""
    if not vpc_id:
        click.echo("No VPC selected; skipping subnet selection (product will create/manage).")
        return []
    subnets = ec2_client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if not subnets:
        click.echo("No subnets found in the selected VPC. Skipping subnet selection (product will create/manage).")
        return []

    click.echo(f"Select subnets in {vpc_id} (comma-separated indexes). Leave blank to skip.")
    for idx, subnet in enumerate(subnets, start=1):
        az = subnet.get("AvailabilityZone", "?")
        cidr = subnet.get("CidrBlock", "")
        click.echo(f"[{idx}] {subnet['SubnetId']} {cidr} {az}")

    default_selection = ",".join(str(i) for i in range(1, min(len(subnets), max(minimum, 2)) + 1))
    choice = click.prompt("Enter choices (blank to skip)", default="", show_default=False)
    if choice.strip() == "":
        click.echo("Skipping subnet selection (product will create/manage).")
        return []
    try:
        indexes = [int(x.strip()) for x in choice.split(",") if x.strip()]
    except ValueError as exc:
        raise click.ClickException("Invalid subnet selection") from exc
    selected = []
    for idx in indexes:
        try:
            selected.append(subnets[idx - 1]["SubnetId"])
        except IndexError as exc:
            raise click.ClickException(f"Invalid subnet index: {idx}") from exc
    if len(selected) < minimum:
        raise click.ClickException(f"Select at least {minimum} subnet(s).")
    return selected


def pick_or_create_sg(ec2_client, vpc_id: str):
    """Reuse an existing SG or skip (let product create later)."""
    existing = click.prompt(
        "Enter an existing Security Group ID to reuse (leave blank to skip)",
        default="",
        show_default=False,
    )
    if existing:
        try:
            ec2_client.describe_security_groups(GroupIds=[existing])
            click.echo(f"Reusing security group {existing}")
            return existing
        except botocore.exceptions.ClientError as exc:
            raise click.ClickException(f"Failed to describe security group {existing}: {exc}") from exc

    click.echo("No Security Group provided. Skipping (product will create/manage SG).")
    return None


def choose_role_name(iam_client, default_name: str, prompt_text: str) -> str:
    """Prompt for a role name; if it exists, ask to reuse or choose another."""
    name = default_name
    while True:
        try:
            iam_client.get_role(RoleName=name)
            # Role exists; ask if we should reuse.
            if click.confirm(f"Role {name} exists. Reuse it?", default=True):
                return name
            name = click.prompt(prompt_text, default=name + "_v2")
        except iam_client.exceptions.NoSuchEntityException:
            return name
        except botocore.exceptions.ClientError as exc:
            raise click.ClickException(f"Failed to check role {name}: {exc}") from exc


def prompt_s3_paths():
    """Collect S3 bucket and prefixes for staging/logging/init script."""
    bucket = click.prompt("S3 bucket for staging/logging/init", default="dev")

    def norm(prefix: str) -> str:
        return prefix.strip().strip("/")

    staging = norm(click.prompt("Staging prefix", default="Staging"))
    logging = norm(click.prompt("Logging prefix", default="Logging"))
    init = norm(click.prompt("Init script prefix", default="InitScript"))

    def to_arns(prefix: str) -> List[str]:
        base = f"arn:aws:s3:::{bucket}"
        path = f"{base}/{prefix}"
        return [base, f"{path}", f"{path}/*"]

    resources = list({arn for pref in [staging, logging, init] for arn in to_arns(pref)})
    return {"bucket": bucket, "staging": staging, "logging": logging, "init": init, "resources": sorted(resources)}


def build_cluster_operator_policy(s3_resources: List[str]):
    """Construct the cluster operator inline policy using provided S3 resources."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ClusterS3Access",
                "Effect": "Allow",
                "Action": [
                    "s3:GetLifecycleConfiguration",
                    "s3:GetBucketTagging",
                    "s3:GetBucketWebsite",
                    "s3:GetBucketLogging",
                    "s3:ListBucket",
                    "s3:GetAccelerateConfiguration",
                    "s3:GetBucketVersioning",
                    "s3:GetReplicationConfiguration",
                    "s3:PutObject",
                    "s3:GetObjectAcl",
                    "s3:GetObject",
                    "s3:GetEncryptionConfiguration",
                    "s3:PutBucketTagging",
                    "s3:GetBucketRequestPayment",
                    "s3:GetBucketCORS",
                    "s3:GetObjectTagging",
                    "s3:PutObjectTagging",
                    "s3:GetBucketLocation",
                    "s3:GetObjectVersion",
                    "s3:DeleteObjectTagging",
                    "s3:DeleteObjectVersion",
                    "s3:DeleteObject",
                ],
                "Resource": s3_resources,
            },
            {
                "Sid": "ClusterInfrastructureAccess",
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeAccountAttributes",
                    "ec2:DescribeInternetGateways",
                    "ec2:AttachInternetGateway",
                    "ec2:CreateInternetGateway",
                    "ec2:DetachInternetGateway",
                    "ec2:DeleteInternetGateway",
                    "ec2:CreateKeyPair",
                    "ec2:ImportKeyPair",
                    "ec2:DescribeKeyPairs",
                    "ec2:DeleteKeyPair",
                    "ec2:CreateRoute",
                    "ec2:DeleteRoute",
                    "ec2:DescribeRouteTables",
                    "ec2:CreateRouteTable",
                    "ec2:ReplaceRouteTableAssociation",
                    "ec2:AssociateRouteTable",
                    "ec2:DisassociateRouteTable",
                    "ec2:DeleteRouteTable",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeVpcs",
                    "ec2:CreateVpc",
                    "ec2:DeleteVpc",
                    "ec2:ModifyVpcAttribute",
                    "ec2:DescribeSubnets",
                    "ec2:CreateSubnet",
                    "ec2:DeleteSubnet",
                    "ec2:DescribeSecurityGroups",
                    "ec2:CreateSecurityGroup",
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:AuthorizeSecurityGroupEgress",
                    "ec2:RevokeSecurityGroupEgress",
                    "ec2:DeleteSecurityGroup",
                    "ec2:CreateTags",
                    "ec2:DescribeTags",
                    "ec2:DeleteTags",
                    "ec2:CreateVolume",
                    "ec2:DescribeVolumes",
                    "ec2:DeleteVolume",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstanceAttribute",
                    "ec2:ModifyInstanceAttribute",
                    "ec2:RunInstances",
                    "ec2:DescribeInstances",
                    "ec2:StartInstances",
                    "ec2:StopInstances",
                    "ec2:DescribeInstanceTypes",
                    "ec2:TerminateInstances",
                    "ec2:DescribeRegions",
                    "ec2:DescribeAvailabilityZones",
                    "ec2:CreateLaunchTemplate",
                    "ec2:DescribeLaunchTemplateVersions",
                    "ec2:DescribeLaunchTemplates",
                    "ec2:DeleteLaunchTemplate",
                    "ec2:CreateLaunchTemplateVersion",
                    "ec2:DeleteLaunchTemplateVersions",
                    "autoscaling:AttachLoadBalancers",
                    "autoscaling:DescribeTags",
                    "autoscaling:CreateAutoScalingGroup",
                    "autoscaling:DescribeAutoScalingGroups",
                    "autoscaling:DescribeScalingActivities",
                    "autoscaling:UpdateAutoScalingGroup",
                    "autoscaling:DeleteAutoScalingGroup",
                    "autoscaling:TerminateInstanceInAutoScalingGroup",
                    "elasticloadbalancing:AddTags",
                    "elasticloadbalancing:DescribeTags",
                    "elasticloadbalancing:ApplySecurityGroupsToLoadBalancer",
                    "elasticloadbalancing:AttachLoadBalancerToSubnets",
                    "elasticloadbalancing:ConfigureHealthCheck",
                    "elasticloadbalancing:CreateLoadBalancer",
                    "elasticloadbalancing:DescribeLoadBalancers",
                    "elasticloadbalancing:DeleteLoadBalancer",
                    "elasticloadbalancing:CreateLoadBalancerListeners",
                    "elasticloadbalancing:DescribeInstanceHealth",
                    "elasticloadbalancing:DescribeLoadBalancerAttributes",
                    "elasticloadbalancing:ModifyLoadBalancerAttributes",
                    "elasticloadbalancing:RegisterInstancesWithLoadBalancer",
                    "pricing:GetProducts",
                    "iam:GetInstanceProfile",
                    "iam:GetContextKeysForPrincipalPolicy",
                    "iam:ListInstanceProfiles",
                    "iam:SimulatePrincipalPolicy",
                    "iam:CreateInstanceProfile",
                    "iam:DeleteInstanceProfile",
                    "iam:CreateRole",
                    "iam:GetRole",
                    "iam:ListRoles",
                    "iam:PassRole",
                    "iam:ListRolePolicies",
                    "iam:CreateServiceLinkedRole",
                    "iam:DeleteRole",
                    "iam:TagRole",
                    "iam:GetRolePolicy",
                    "iam:AddRoleToInstanceProfile",
                    "iam:ListAttachedRolePolicies",
                    "iam:ListInstanceProfilesForRole",
                    "iam:RemoveRoleFromInstanceProfile",
                    "iam:PutRolePolicy",
                    "iam:AttachRolePolicy",
                    "iam:DetachRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:GetUser",
                    "kms:DescribeKey",
                    "kms:Get*",
                    "sts:AssumeRole",
                    "sts:DecodeAuthorizationMessage",
                ],
                "Resource": "*",
            },
        ],
    }


def prompt_agent_trust():
    """Prompt for an additional principal that will assume the agent role (optional)."""
    return click.prompt(
        "Additional principal ARN allowed to assume agent_role (leave blank for none)",
        default="",
        show_default=False,
    ) or None


def update_trust_with_retry(iam_client, role_name: str, policy_doc: Dict[str, Any], retries: int = 3):
    """Update assume role policy with retries for eventual consistency on new principals."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            iam_client.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(policy_doc))
            return
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            msg = exc.response.get("Error", {}).get("Message", "")
            if code == "MalformedPolicyDocument" and "Invalid principal" in msg and attempt < retries:
                sleep_for = 2 * attempt
                click.echo(f"Trust update failed due to principal propagation; retrying in {sleep_for}s...")
                time.sleep(sleep_for)
                last_exc = exc
                continue
            last_exc = exc
            break
    if last_exc:
        raise last_exc


def ensure_instance_profile(iam_client, profile_name: str, role_name: str):
    """Create or reuse an instance profile and add the role."""
    try:
        profile = iam_client.get_instance_profile(InstanceProfileName=profile_name)["InstanceProfile"]
        click.echo(f"Reusing instance profile {profile_name}")
    except iam_client.exceptions.NoSuchEntityException:
        profile = iam_client.create_instance_profile(
            InstanceProfileName=profile_name,
            Tags=TAG_DEFAULTS,
        )["InstanceProfile"]
        click.echo(f"Created instance profile {profile_name}")

    role_names = [r["RoleName"] for r in profile.get("Roles", [])]
    if role_name not in role_names:
        try:
            iam_client.add_role_to_instance_profile(
                InstanceProfileName=profile_name,
                RoleName=role_name,
            )
            click.echo(f"Attached role {role_name} to instance profile {profile_name}")
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "LimitExceeded":
                raise
            raise click.ClickException(
                f"Instance profile {profile_name} already has a role attached; remove it or choose another profile."
            ) from exc
    return profile["Arn"]


def confirm_plan(
    vpc_id: str | None,
    subnets: List[str],
    sg_id: str | None,
    s3_info: Dict[str, str],
    role_specs: List[Dict[str, Any]],
    using_role_specs_file: bool,
    cluster_role_name: str,
    agent_role_name: str,
    instance_profile_name: str,
    agent_trust_principal: str | None,
):
    """Show a summary of planned changes and confirm."""
    click.echo("")
    click.echo("=== Planned Changes ===")
    click.echo(f"- VPC: {vpc_id or 'skip (product will create)'}")
    click.echo(f"- Subnets: {', '.join(subnets) if subnets else 'skip (product will create)'}")
    click.echo(f"- Security Group: {sg_id or 'skip (product will create/manage)'}")
    click.echo(
        f"- S3: bucket={s3_info['bucket']} staging={s3_info['staging']} logging={s3_info['logging']} init={s3_info['init']}"
    )
    if using_role_specs_file:
        click.echo(f"- Roles from specs file: {', '.join(spec['name'] for spec in role_specs)}")
    click.echo(f"- Cluster operator role: {cluster_role_name} (policy: cluster_operator_policy)")
    click.echo(f"- Agent role: {agent_role_name} (instance profile: {instance_profile_name})")
    click.echo(f"- Agent role extra principal: {agent_trust_principal or 'none'}")
    click.echo("- Cluster operator trust will be set to EC2 + agent role")
    click.echo("")
    if not click.confirm("Proceed with IAM changes?", default=True):
        raise click.Abort()


def write_manifest(path, manifest):
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    click.echo(f"Wrote {path}")


@click.command()
@click.option("--profile", help="AWS profile to use", default=None)
@click.option("--region", help="AWS region override", default=None)
@click.option("--trusted-principal", help="ARN allowed to assume the cluster role (defaults to current caller).", default=None)
@click.option("--role-specs-file", help="Optional JSON file with role specs (name, trust, policy). Overrides built-in cluster operator spec if provided.", default=None)
@click.option("--output-manifest", default="cluster-setup.json", show_default=True)
@click.option(
    "--create-vpc/--reuse-vpc",
    default=False,
    show_default=True,
    help="Allow wizard to create a new VPC (create path not yet implemented).",
)
def main(profile, region, trusted_principal, role_specs_file, output_manifest, create_vpc):
    """Wizard to prepare AWS resources for an Informatica advanced integration cluster."""
    session = boto3.session.Session(profile_name=profile, region_name=region)
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account_id = identity["Account"]
    click.echo(f"Authenticated as {identity['Arn']} in account {account_id}")

    ec2 = session.client("ec2")
    iam = session.client("iam")

    if create_vpc:
        raise click.ClickException("VPC creation path is not wired yet; reuse an existing VPC for now.")
    vpc_id = choose_vpc(ec2, allow_skip=True)

    subnets = choose_subnets(ec2, vpc_id, minimum=1)
    sg_id = pick_or_create_sg(ec2, vpc_id) if vpc_id else None

    context = {
        "account_id": account_id,
        "region": session.region_name,
        "caller_arn": trusted_principal or identity["Arn"],
    }
    role_specs = load_role_specs(role_specs_file, context)
    s3_info = prompt_s3_paths()

    using_role_specs_file = bool(role_specs)
    if using_role_specs_file:
        click.echo("Using role specs from file.")
        cluster_role_name = click.prompt("Enter cluster operator role name", default="cluster_operator_role")
        spec_names = {spec["name"] for spec in role_specs}
        if cluster_role_name not in spec_names:
            click.echo("Note: cluster operator role is not in the role specs file; it must already exist in IAM.")
    else:
        click.echo("Using built-in cluster operator role spec.")
        cluster_role_name = choose_role_name(
            iam,
            default_name="cluster_operator_role",
            prompt_text="Enter cluster operator role name",
        )
        # cluster_operator_role trust will be patched after agent_role creation.
        role_specs = [
            {
                "name": cluster_role_name,
                "policy_name": "cluster_operator_policy",
                "trust": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Allow", "Principal": {"AWS": context["caller_arn"]}, "Action": "sts:AssumeRole"}
                    ],
                },
                "policy": build_cluster_operator_policy(s3_info["resources"]),
            }
        ]

    agent_trust_principal = prompt_agent_trust()
    agent_role_name = choose_role_name(
        iam,
        default_name="agent_role",
        prompt_text="Enter agent role name",
    )
    instance_profile_name = f"{agent_role_name}_instance_profile"

    confirm_plan(
        vpc_id=vpc_id,
        subnets=subnets,
        sg_id=sg_id,
        s3_info=s3_info,
        role_specs=role_specs,
        using_role_specs_file=using_role_specs_file,
        cluster_role_name=cluster_role_name,
        agent_role_name=agent_role_name,
        instance_profile_name=instance_profile_name,
        agent_trust_principal=agent_trust_principal,
    )

    role_arns: Dict[str, str] = {}
    for spec in role_specs:
        role_arns[spec["name"]] = ensure_role(
            iam,
            spec["name"],
            spec["trust"],
            spec["policy"],
            policy_name=spec.get("policy_name"),
        )

    # Resolve cluster operator role ARN (from created roles or existing IAM).
    cluster_role_arn = role_arns.get(cluster_role_name)
    if not cluster_role_arn:
        try:
            cluster_role_arn = iam.get_role(RoleName=cluster_role_name)["Role"]["Arn"]
            role_arns[cluster_role_name] = cluster_role_arn
            click.echo(f"Using existing cluster operator role {cluster_role_name} ({cluster_role_arn})")
        except iam.exceptions.NoSuchEntityException as exc:
            raise click.ClickException(
                f"{cluster_role_name} ARN not found; include it in role specs or create it first."
            ) from exc

    # Create or reuse agent_role with assume_role_agent_policy targeting cluster_operator_role.
    agent_trust_statements = [
        {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ]
    if agent_trust_principal:
        agent_trust_statements.append(
            {
                "Effect": "Allow",
                "Principal": {"AWS": agent_trust_principal},
                "Action": "sts:AssumeRole",
            }
        )
    agent_trust = {"Version": "2012-10-17", "Statement": agent_trust_statements}
    assume_role_agent_policy = {
        "Version": "2012-10-17",
        "Statement": {
            "Effect": "Allow",
            "Action": "sts:AssumeRole",
            "Resource": cluster_role_arn,
        },
    }
    agent_role_arn = ensure_role(
        iam,
        agent_role_name,
        agent_trust,
        assume_role_agent_policy,
        policy_name="assume_role_agent_policy",
    )
    role_arns[agent_role_name] = agent_role_arn

    # Ensure instance profile for agent_role so it can be attached to EC2.
    instance_profile_arn = ensure_instance_profile(iam, instance_profile_name, agent_role_name)

    # Patch cluster_operator_role trust to allow both EC2 and agent_role.
    operator_trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"},
            {"Effect": "Allow", "Principal": {"AWS": agent_role_arn}, "Action": "sts:AssumeRole"},
        ],
    }
    update_trust_with_retry(iam, cluster_role_name, operator_trust)

    manifest = {
        "account": account_id,
        "region": session.region_name,
        "vpc_id": vpc_id,
        "subnet_ids": subnets,
        "security_group_id": sg_id,
        "roles": role_arns,
        "instance_profiles": {
            agent_role_name: instance_profile_name,
        },
        "s3": {
            "bucket": s3_info["bucket"],
            "staging_prefix": s3_info["staging"],
            "logging_prefix": s3_info["logging"],
            "init_prefix": s3_info["init"],
        },
        "trusted_principals": {
            "cluster_operator_role": operator_trust["Statement"],
            agent_role_name: agent_trust_principal,
        },
    }
    write_manifest(output_manifest, manifest)

    click.echo("")
    click.echo("=== Next steps for Secure Agent EC2 ===")
    click.echo(f"- Use instance profile: {instance_profile_name}")
    click.echo(f"- Agent role: {agent_role_name}")
    click.echo("Attach the instance profile when launching the Secure Agent EC2 (or associate to a running instance).")


if __name__ == "__main__":
    try:
        main()
    except botocore.exceptions.BotoCoreError as exc:
        raise SystemExit(f"AWS error: {exc}") from exc
