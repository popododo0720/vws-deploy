# Feature map

Generated from docs/feature-map.json by `scripts/vws verify --update-feature-map`. Do not edit by hand.
Last pass is a vws-test scenario run (KST). `unverified` means the feature has no scenario, and the Verify command column gives the reason; `never passed` means a scenario exists but no passing vws-test result was recorded.

| Feature | Repo | Issue | Verify command | Last pass (KST) | Evidence |
|---|---|---|---|---|---|
| EC2 RunInstances / TerminateInstances | vws-console-api | - | `scripts/vws verify scenario ec2-volume-reboot` | never passed | - |
| EC2 DescribeInstances / StopInstances / StartInstances | vws-console-api | - | N/A: no scenario yet; ec2-volume-reboot runs, reboots and terminates an instance but never stops or starts one | unverified | - |
| EC2 key pair import | vws-console-api | vws-console-api#128 | N/A: no scenario yet; ImportKeyPair is open in vws-console-api#128 | unverified | - |
| EC2 user-data | vws-console-api | vws-console-api#128 | N/A: no scenario yet; user-data to cloud-init is open in vws-console-api#128 | unverified | - |
| AMI / EBS snapshot import | vws-compute | vws-deploy#135 | N/A: no scenario yet | unverified | - |
| EBS volume create / attach / snapshot | vws-storage | - | N/A: no scenario yet; ec2-volume-reboot creates and attaches a volume but takes no snapshot | unverified | - |
| EBS volume detach / delete | vws-storage | - | `scripts/vws verify scenario ec2-volume-reboot` | never passed | - |
| VPC and subnet create / delete | vws-network | vws-network#5 | N/A: no scenario yet | unverified | - |
| Security group create / ingress rules | vws-network | vws-network#117 | N/A: no scenario yet | unverified | - |
| Elastic IP allocate / associate | vws-network | - | N/A: no scenario yet | unverified | - |
| Direct subnets as shared EC2 subnets and network interfaces | vws-console-api | vws-console-api#60 | N/A: no scenario yet; the API is open in vws-console-api#60 | unverified | - |
| S3 object put / get (RGW) | vws-storage | vws-deploy#51 | `scripts/vws verify scenario s3-basic` | never passed | - |
| IAM users and access keys | vws-iam | vws-iam#3 | N/A: no scenario yet | unverified | - |
| IAM groups and roles | vws-iam | vws-iam#1 | N/A: no scenario yet; the IAM Phase 1 design is open in vws-iam#1 | unverified | - |
| IAM policy evaluation (Authorize) | vws-iam | vws-iam#4 | N/A: no scenario yet | unverified | - |
| STS AssumeRole / GetSessionToken | vws-iam | vws-iam#5 | N/A: no scenario yet; the API is open in vws-iam#5 | unverified | - |
| IAM virtual MFA | vws-iam | vws-iam#6 | N/A: no scenario yet | unverified | - |
| Console login | vws-console | - | N/A: browser UI; vws verify scenarios call the APIs only, and there is no browser e2e suite yet | unverified | - |
| Console EC2 Instances list and Launch Instance wizard | vws-console | vws-console#1 | N/A: browser UI; vws verify scenarios call the APIs only, and there is no browser e2e suite yet | unverified | - |
| Console IAM pages | vws-console | vws-console#47 | N/A: browser UI; vws verify scenarios call the APIs only, and there is no browser e2e suite yet | unverified | - |
| Installer web wizard and TUI | vws-deploy | vws-deploy#139 | N/A: no scenario yet; the installer runs before the stack and no nightly step bootstraps a cluster, so only the installer Go tests under installer/ (API and TUI) run, in CI | unverified | - |
| Node reboot recovery | vws-deploy | vws-deploy#51 | N/A: no scenario yet; vws verify-reboot checks recovery only around a node reboot an operator makes | unverified | - |
