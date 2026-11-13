# VWS compatibility matrix

Target `fake`, run `vws-compat-3b33615d`, 2026-11-13T11:48:03Z.
Profile `pve`.
Tools: aws-cli 2.39.4, terraform 1.17.1.
Terraform AWS provider: pinned in tests/compat/terraform/main.tf.

pass 1, fail 3, skip 17.

| Tool | Resource | Action | Result | Owner | Detail |
|---|---|---|---|---|---|
| aws-cli | image | describe-images (pve) | fail | vws-console-api | describe-images returned no images |
| aws-cli | instance | run-instances (pve) | fail | vws-console-api | aws: [ERROR]: An error occurred (InvalidParameterValue) when calling the RunInstances operation: malformed identifier |
| aws-cli | instance | describe-instances (pve) | skip | vws-console-api | needs instance |
| aws-cli | volume | create-volume (pve) | skip | vws-console-api | needs zone |
| aws-cli | volume | describe-volumes (pve) | skip | vws-console-api | needs volume |
| aws-cli | volume | attach-volume (pve) | skip | vws-console-api | needs volume, instance |
| aws-cli | volume | detach-volume (pve) | skip | vws-console-api | needs attach-volume |
| aws-cli | instance | stop-instances (pve) | skip | vws-console-api | needs instance |
| aws-cli | instance | start-instances (pve) | skip | vws-console-api | needs stop-instances |
| aws-cli | instance | terminate-instances (pve) | skip | vws-console-api | needs instance |
| aws-cli | volume | delete-volume (pve) | skip | vws-console-api | needs volume |
| terraform | provider | init (pve) | pass | vws-deploy |  |
| terraform | aws_instance | apply (pve) | fail | vws-console-api | creating EC2 Instance: operation error EC2: RunInstances, https response error StatusCode: 400, RequestID: 9603f1054fdb622d, api error InvalidParameterValue: unsupported: |
| terraform | aws_ebs_volume | apply (pve) | skip | vws-console-api | not applied: a dependency failed |
| terraform | aws_volume_attachment | apply (pve) | skip | vws-console-api | not applied: a dependency failed |
| terraform | aws_instance | plan (pve) | skip | vws-console-api | not in state after apply |
| terraform | aws_ebs_volume | plan (pve) | skip | vws-console-api | not in state after apply |
| terraform | aws_volume_attachment | plan (pve) | skip | vws-console-api | not in state after apply |
| terraform | aws_instance | destroy (pve) | skip | vws-console-api | not in state after apply |
| terraform | aws_ebs_volume | destroy (pve) | skip | vws-console-api | not in state after apply |
| terraform | aws_volume_attachment | destroy (pve) | skip | vws-console-api | not in state after apply |
