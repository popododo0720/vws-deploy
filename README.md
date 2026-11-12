# vws-deploy

The deployment repository of the VWS private cloud. It contains the systemd packages and operator CLI for the Proxmox control plane. The design is https://github.com/popododo0720/vws-deploy/issues/2. This repository applies nothing to a cluster.

## Who it is for

The same Terraform, aws-cli and SDKs run unchanged with only the endpoint swapped.

- Home labs
- Test environments before deploying to real AWS
- Teams running hybrid with AWS
- Teams moving from AWS to on-premises

The first priority is EC2/S3 API compatibility. The roadmap is https://github.com/popododo0720/vws-deploy/issues/132.

## Control plane

VWS runs on Proxmox with systemd packages. Follow [the operator guide](docs/operator/regions.md) to register regions and AWS accounts.

## Checks

`make check` runs the static guards and admin CLI tests locally and in GitHub Actions. The working rules are in `AGENTS.md`.
