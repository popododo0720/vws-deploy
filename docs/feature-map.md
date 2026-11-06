# Feature map

Generated from docs/feature-map.json by `scripts/vws verify --update-feature-map`. Do not edit by hand.
Last pass is a vws-test scenario run (KST). `unverified` means no scenario exists yet; `never passed` means a scenario exists but no passing vws-test result was recorded.

| Feature | Repo | Issue | Verify command | Last pass (KST) | Evidence |
|---|---|---|---|---|---|
| EC2 RunInstances / TerminateInstances | vws-console-api | - | - | unverified | - |
| EC2 DescribeInstances / StopInstances / StartInstances | vws-console-api | - | - | unverified | - |
| EC2 key pair import | vws-console-api | vws-console-api#128 | - | unverified | - |
| EC2 user-data | vws-console-api | vws-console-api#128 | - | unverified | - |
| AMI / EBS snapshot import | vws-compute | vws-deploy#135 | - | unverified | - |
| EBS volume create / attach / snapshot | vws-storage | - | - | unverified | - |
| EBS volume detach / delete | vws-storage | - | - | unverified | - |
| VPC and subnet create / delete | vws-network | vws-network#5 | - | unverified | - |
| Security group create / ingress rules | vws-network | vws-network#117 | - | unverified | - |
| Elastic IP allocate / associate | vws-network | - | - | unverified | - |
| Direct subnets as shared EC2 subnets and network interfaces | vws-console-api | vws-console-api#60 | - | unverified | - |
| S3 object put / get (RGW) | vws-storage | vws-deploy#51 | `scripts/vws verify scenario s3-basic` | never passed | - |
| IAM users and access keys | vws-iam | vws-iam#3 | - | unverified | - |
| IAM groups and roles | vws-iam | vws-iam#1 | - | unverified | - |
| IAM policy evaluation (Authorize) | vws-iam | vws-iam#4 | - | unverified | - |
| STS AssumeRole / GetSessionToken | vws-iam | vws-iam#5 | - | unverified | - |
| IAM virtual MFA | vws-iam | vws-iam#6 | - | unverified | - |
| Console login | vws-console | - | - | unverified | - |
| Console EC2 Instances list and Launch Instance wizard | vws-console | vws-console#1 | - | unverified | - |
| Console IAM pages | vws-console | vws-console#47 | - | unverified | - |
| Installer web wizard and TUI | vws-deploy | vws-deploy#139 | - | unverified | - |
| Node reboot recovery | vws-deploy | vws-deploy#51 | - | unverified | - |
