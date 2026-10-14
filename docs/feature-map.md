# Feature map

Generated from docs/feature-map.json by `scripts/vws verify --update-feature-map`. Do not edit by hand.
Last pass is a vws-test scenario run (KST). `unverified` means no scenario exists yet; `never passed` means a scenario exists but no passing vws-test result was recorded.

| Feature | Repo | Issue | Verify command | Last pass (KST) | Evidence |
|---|---|---|---|---|---|
| EC2 RunInstances / TerminateInstances | vws-console-api | - | - | unverified | - |
| EC2 key pair import | vws-console-api | vws-console-api#128 | - | unverified | - |
| EC2 user-data | vws-console-api | vws-console-api#128 | - | unverified | - |
| EBS volume create / attach / snapshot | vws-storage | - | - | unverified | - |
| Elastic IP allocate / associate | vws-network | - | - | unverified | - |
| S3 object put / get (RGW) | vws-storage | vws-deploy#51 | - | unverified | - |
| IAM users and access keys | vws-iam | vws-iam#3 | - | unverified | - |
| IAM policy evaluation (Authorize) | vws-iam | vws-iam#4 | - | unverified | - |
| STS AssumeRole / GetSessionToken | vws-iam | vws-iam#5 | - | unverified | - |
| IAM virtual MFA | vws-iam | vws-iam#6 | - | unverified | - |
| Console login | vws-console | - | - | unverified | - |
| Node reboot recovery | vws-deploy | vws-deploy#51 | - | unverified | - |
