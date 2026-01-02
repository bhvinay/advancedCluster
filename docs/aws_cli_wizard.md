# AWS CLI Wizard for Informatica Advanced Integration Cluster

Goal: a distributable CLI wizard (Python) that guides users through AWS setup for an Informatica advanced integration cluster. Focus on AWS only, reuse existing VPC by default, create least-privilege IAM, and avoid storing secrets.

## Approach
- Delivery: Python CLI packaged as a zip/pip package. Dependencies: `boto3`, `click` (or `typer`), `rich` for output. No network calls beyond AWS SDK.
- Auth: require users to log in with `aws sso login` or set profile/keys; the CLI reads standard AWS config/credentials. Do not prompt for raw keys.
- Idempotent: every step should be read -> diff -> apply. Print planned actions and ask for confirmation.
- Outputs: emit a small manifest (YAML/JSON) with IDs of created/reused resources and cluster inputs; never persist credentials.

## Wizard Flow (AWS)
1) Pre-flight: detect account ID, caller identity, home region, current profile; check required services (IAM, EC2, EC2 VPC, STS).
2) Region selection: default to current/SSO region, allow override.
3) VPC:
   - Default path: reuse an existing VPC (the “agent’s” VPC or default VPC if present). List candidate VPCs filtered by tags such as `Environment=Informatica`.
   - If creating: make VPC (/16), 2–3 private subnets across AZs (/20 or /21), 1–2 public subnets if needed for NAT/ingress, IGW, route tables, NAT gateway(s), VPC endpoints if required (S3, STS, ECR). Tag everything consistently.
4) Subnets: ask user to select subnets for control/data plane (or capture them for product cluster config). Validate AZ diversity.
5) Security groups: create or reuse SGs with least privileges (egress 0.0.0.0/0 if acceptable; ingress only required ports). Allow user to supply additional ingress CIDRs as needed.
6) IAM:
   - Create or reuse roles with inline/managed policies. Keep names predictable and tag them.
   - Trust policy: allow STS assume from the Informatica agent role/instance profile or service principal the product uses.
   - Permissions: start from least privilege (EC2, S3, CloudWatch Logs, Secrets Manager/KMS if used, ECR). Split duties if you have separate control-plane/data-plane roles.
7) Artifacts: write `cluster-setup.json` with VPC, subnet IDs, SG IDs, IAM role ARNs, region.

## Policy Starter (example, tighten as needed)
- Trust (example): principal is the Informatica agent role/instance profile.
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::<ACCOUNT_ID>:role/InformaticaAgentRole" },
      "Action": "sts:AssumeRole"
    }
  ]
}
```
- Inline permissions (trim/expand per product needs):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["ec2:Describe*","ec2:CreateTags","ec2:CreateSecurityGroup","ec2:AuthorizeSecurityGroupIngress","ec2:AuthorizeSecurityGroupEgress","ec2:CreateNetworkInterface","ec2:ModifyNetworkInterfaceAttribute"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["iam:PassRole"], "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/Informatica*"},
    { "Effect": "Allow", "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"], "Resource": "*" },
    { "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": "arn:aws:s3:::<BUCKET_NAME>" },
    { "Effect": "Allow", "Action": ["s3:GetObject","s3:PutObject"], "Resource": "arn:aws:s3:::<BUCKET_NAME>/*" }
  ]
}
```

## UX Best Practices
- Default to reuse: preselect existing VPC and subnets; only create when asked.
- Show dry-run plans before mutating AWS.
- Validate inputs early (CIDRs, overlapping ranges, AZ count, IAM name collisions).
- Tag everything (`Project=Informatica`, `Owner`, `Environment`, `ManagedBy=InformaticaCLI`).
- Log to stdout and optional file; include AWS request IDs on errors.

## Skeleton CLI (Python)
This sketch shows structure; fill in product-specific IAM actions and VPC shape.
```python
import json
import boto3
import botocore
import click

SESSION = boto3.session.Session()

def ensure_role(iam, name, trust_doc, policy_doc):
    try:
        role = iam.get_role(RoleName=name)["Role"]
        click.echo(f"Reusing role {name} ({role['Arn']})")
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust_doc), Tags=[{"Key": "ManagedBy", "Value": "InformaticaCLI"}])["Role"]
        click.echo(f"Created role {name}")
    iam.put_role_policy(RoleName=name, PolicyName=f"{name}Inline", PolicyDocument=json.dumps(policy_doc))
    return role["Arn"]

def choose_vpc(ec2):
    vpcs = ec2.describe_vpcs()["Vpcs"]
    if not vpcs:
        raise click.ClickException("No VPCs found; create one.")
    default = next((v for v in vpcs if v.get("IsDefault")), None)
    click.echo("Select VPC (default first):")
    ordered = [v for v in [default] + vpcs if v]
    for idx, v in enumerate(ordered, start=1):
        click.echo(f"[{idx}] {v['VpcId']} {v.get('CidrBlock')}")
    choice = click.prompt("Enter choice", type=int, default=1)
    return ordered[choice - 1]["VpcId"]

@click.command()
@click.option("--profile", help="AWS profile", default=None)
@click.option("--region", help="AWS region override", default=None)
@click.option("--output-manifest", default="cluster-setup.json")
@click.option("--create-vpc/--reuse-vpc", default=False, help="Create new VPC if needed")
def main(profile, region, output_manifest, create_vpc):
    session = boto3.session.Session(profile_name=profile, region_name=region)
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    click.echo(f"Authenticated as {identity['Arn']} in account {identity['Account']}")

    ec2 = session.client("ec2")
    iam = session.client("iam")

    vpc_id = choose_vpc(ec2)
    # TODO: optionally create VPC/subnets if create_vpc is True

    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::<ACCOUNT_ID>:role/InformaticaAgentRole"}, "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["ec2:Describe*"], "Resource": "*"}]}  # tighten/expand
    role_arn = ensure_role(iam, "InformaticaClusterRole", trust, policy)

    manifest = {"account": identity["Account"], "region": session.region_name, "vpc_id": vpc_id, "role_arn": role_arn}
    with open(output_manifest, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    click.echo(f"Wrote {output_manifest}")

if __name__ == "__main__":
    try:
        main()
    except botocore.exceptions.BotoCoreError as exc:
        raise SystemExit(f"AWS error: {exc}")
```

## Next Steps
- Fill in exact IAM actions and trust relationships required by the Informatica cluster runtime.
- Decide the minimal VPC shape for “create” path (subnet count, NAT strategy, endpoints).
- Add validation for overlapping CIDRs and AZ diversity.
- Package with `pip install .` or a zipapp; add tests that mock boto3 (e.g., `moto`) to keep the wizard safe to run locally.

## How to Run the Skeleton Locally
1) Create and activate a venv; install deps:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r wizard/requirements.txt
```
2) Ensure you are logged in: `aws sso login --profile <your-profile>` or have `AWS_PROFILE`/`AWS_ACCESS_KEY_ID` set.
3) The CLI lives in `wizard/wizard.py`.
4) Run it (reuse-only; VPC/subnets must already exist):
```bash
python wizard/wizard.py --profile <your-profile> --region us-east-1 --trusted-principal <arn> --role-specs-file roles.json
```
Use `--create-vpc` to allow creation when you wire that path up.

## Current Behavior
- Prompts for VPC (optional; enter 0 to skip and let the product create it) and subnets (optional; can skip).
- Prompts to reuse a Security Group or skip (if skipped, product can create/manage SG).
- Prompts for S3 bucket and prefixes (staging, logging, init) and uses them in the cluster operator policy.
- Prompts for cluster operator role name; if it exists, you can reuse or enter a new name. Creates/updates inline `cluster_operator_policy` and later patches trust to allow EC2 plus the agent role.
- Prompts for agent role name; if it exists, you can reuse or enter a new name. Attaches inline `assume_role_agent_policy` to allow `sts:AssumeRole` on the cluster operator role, and sets trust to EC2 plus an optional principal you provide. Creates/reuses an instance profile for the agent role so it can be attached to the Secure Agent EC2; prints a reminder at the end.
- Shows a plan summary and asks for confirmation before IAM changes.
- If you supply `--role-specs-file`, it will use that list of roles instead of the built-in cluster operator spec. Templating keys: `{account_id}`, `{region}`, `{caller_arn}`.

## Example `roles.json` (if overriding)
```json
[
  {
    "name": "cluster_operator_role",
    "trust": {
      "Version": "2012-10-17",
      "Statement": [
        { "Effect": "Allow", "Principal": { "AWS": "{caller_arn}" }, "Action": "sts:AssumeRole" }
      ]
    },
    "policy": { "Version": "2012-10-17", "Statement": [ /* your statements here */ ] }
  }
]
```
