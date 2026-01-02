# Advanced Cluster Setup Tools

This repo contains the AWS CLI wizard used to prepare IAM roles and configuration for Informatica advanced integration clusters.

## What You Can Run
- `wizard/` — the AWS CLI wizard (primary tool)
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
