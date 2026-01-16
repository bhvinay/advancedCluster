# Advanced Cluster Setup Tools

This repo contains AWS CLI tools used to prepare IAM roles and provision EC2 resources for Informatica advanced integration clusters.

## What You Can Run
- `wizard/` — IAM and cluster setup wizard (current)
- `codec_ec2/` — EC2 provisioning + bootstrap tool (v1)
- `docs/` — design notes and behavior documentation
- `archive/` — older experiments and prior projects (kept for reference)

## Quick Start (Wizard)
```bash
cd wizard
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python wizard.py --profile <profile> --region us-east-1
```

The wizard writes a `cluster-setup.json` manifest to the current directory and prints next steps for Secure Agent EC2.

## Quick Start (EC2 Provisioning)
```bash
cd codec_ec2
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python codec_ec2.py up --profile <profile> --region us-east-1
```

The EC2 tool writes a `codec-ec2-run.json` manifest to the current directory and prints SSH commands.
