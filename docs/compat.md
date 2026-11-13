# Compatibility matrix: Terraform AWS provider and aws-cli

vws-deploy#133. The matrix shows which unmodified aws-cli calls and Terraform AWS provider resources work against
VWS. Each row is one tool, one resource and one action, with the result `pass`, `fail` or `skip` and the repo that
owns a failure. vws-conformance checks single API responses against the AWS documentation. This matrix checks the
whole workflow a user runs: create, describe, change and delete with aws-cli, and apply, plan with no changes and
destroy with Terraform.

## What runs

`tests/compat/compat.py run OUT_DIR` writes `OUT_DIR/results.json` and `OUT_DIR/matrix.md`.

- aws-cli v2 (`AWSCLI_VERSION` in `scripts/versions.env`):
  - ec2: describe-images, create/describe/delete-vpc, create/describe/delete-subnet,
    create/authorize-ingress/describe/delete-security-group, run/describe/stop/start/terminate-instances,
    create/describe/attach/detach/delete-volume, and allocate/associate/describe/disassociate/release-address.
  - s3 and s3api: mb, head-bucket, ls, cp up and down, head-object, multipart upload (two parts), rm and rb.
- Terraform (`TERRAFORM_VERSION`) with the AWS provider pinned in `tests/compat/terraform/main.tf` and its
  `.terraform.lock.hcl`. The config is in `tests/compat/terraform/main.tf`. It holds a VPC, a subnet, a security
  group, an instance, an EBS volume and its attachment, an EIP and its association, and an S3 bucket and object.
  Every resource gets an `apply`, a `plan` (pass means no change after apply) and a `destroy` row.

`VWS_COMPAT_PROFILE=aws` is the default and keeps the full scenario above.
`VWS_COMPAT_PROFILE=pve` selects `tests/compat/terraform-pve/main.tf`: only `aws_instance`,
`aws_ebs_volume` and `aws_volume_attachment`, with provider `default_tags` and no `subnet_id`.
The scenario relies on the region's default bridge subnet. Its CLI scenario runs image discovery, the instance lifecycle
and EBS create, describe, attach, detach and delete calls. It creates no VPC, subnet, security group,
Elastic IP or S3 resources. Pve actions have a ` (pve)` suffix, for example
`terraform | aws_instance | plan (pve)`. A pve plan passes only when
`terraform plan -detailed-exitcode` exits 0; exit 2 is a failure even without a resource diagnostic.
Both profiles share the provider version and lock file in `tests/compat/terraform/`.
The pve provider omits S3 configuration because this profile uses only EC2/EBS.

The [first generated pve matrix](compat-pve.md) was measured on 2026-11-13 against console-api
`0b2a95fc5280d0581c9539df400215d560bae82d`: pass 1, fail 3, skip 17, with no leftovers.
This is not an all-pass compatibility claim. The backend's `DescribeImages` still returns empty inventory.
Its EC2 Query `RunInstances` parser requires `SubnetId` even for a pve region, and the stock Terraform
provider's launch is rejected with `InvalidParameterValue: unsupported`. Supplying the seeded template ID
lets the run expose both launch failures instead of skipping every resource. Consequently no real
post-apply no-change plan has been observed at this pin; the exit-code assertion is covered by unit tests.

A step whose input a failed step should have made is `skip` with `needs <input>`, so one failure does not hide or
fail the rows after it. Every name the run creates has the run ID `vws-compat-<8 hex>` (bucket and security group
names, the `vws-compat` tag). At the end the run deletes, newest first, what a failed step left, and lists what it
could not delete under "Resources the run could not delete". A killed run leaves its resources. Find them by the
run ID.

`tests/compat/install.sh` installs the pinned terraform and aws-cli into `VWS_TOOL_DIR` (default `/tmp/bin`) and
checks their sha256.

## Environment

| Variable | Meaning |
| --- | --- |
| `VWS_COMPAT_TARGET` | `fake` or `vws`. Only a `vws` run can open issues |
| `VWS_COMPAT_PROFILE` | `aws` (default) or `pve`; other values are refused |
| `VWS_COMPAT_EC2_ENDPOINT`, `VWS_COMPAT_S3_ENDPOINT` | VWS endpoint URLs, http or https |
| `VWS_COMPAT_ALLOWED_HOSTS` | comma list; each endpoint host must be in it |
| `VWS_COMPAT_ACCESS_KEY_ID`, `VWS_COMPAT_SECRET_ACCESS_KEY` | VWS test credentials |
| `VWS_COMPAT_REGION` | default `vws-1` for aws, `pve-test-1` for pve |
| `VWS_COMPAT_IMAGE_ID` | image for instances; default the first image describe-images returns. Without an image, Terraform still runs the resources that need none |
| `VWS_COMPAT_INSTANCE_TYPE` | default `t3.micro`, mapped by console-api to `vws.small` |

The run refuses an AWS-owned endpoint host (`AWS_OWNED` in `tests/compat/compat.py`) and any host outside the
allowlist. aws and terraform get only the environment the script builds. That is the credentials above, a
throwaway `HOME`, an `AWS_CONFIG_FILE` that sets S3 path-style addressing, no shared credentials file, IMDS off,
and `AWS_ENDPOINT_URL` plus the per-service `AWS_ENDPOINT_URL_*` pointing at VWS. So `~/.aws`, `AWS_PROFILE` and
runner credentials never reach them, and a service without its own endpoint still goes to VWS. The secret key is
redacted from every detail. aws-cli uses standard retries (5 attempts) with a 10 s connect and 60 s read timeout,
and every command has a deadline. The provider uses `max_retries = 5`, and `terraform init` is retried 3 times.

## CI

`.github/workflows/compat.yml` runs the unit tests (`python3 -m unittest discover -s tests/compat -v`) and the
matrix, writes the matrix to the job summary and uploads `results.json` and `matrix.md` as the
`compat-matrix-<target>` artifact.

- Pull requests that change `tests/compat/`, `scripts/versions.env` or the workflow run against
  `vws-console-api -backend fake` at `CONSOLE_API_FAKE_REF`. A fake run checks the harness and measures the
  console-api API surface. It is not VWS.
- The same PR workflow runs the pve profile against the real Proxmox backend and EC2 Query surface
  with the upstream `pvefake` HTTPS transport. `tests/compat/pvefake/main.go` builds inside console-api
  at `CONSOLE_API_PROXMOX_FAKE_REF`; it seeds one public template, a bridge and local storage, and prints
  the template's deterministic image ID. This pin is separate from the existing aws fake pin.
  Its generated matrix and JSON are uploaded as `compat-matrix-fake-pve`.
  A successful harness invocation writes the matrix even when API compatibility rows fail; read the
  row results, not just the job status. This is also how the existing aws matrix reports gaps.
- A dispatch with `target: vws` runs against VWS. It reads the endpoints, allowlist, region, image and instance
  type and profile from the `VWS_COMPAT_*` repo variables and the credentials from the `VWS_COMPAT_ACCESS_KEY_ID` and
  `VWS_COMPAT_SECRET_ACCESS_KEY` secrets. `runner` picks a runner that reaches the endpoints.
  With `open_issues: true` it runs `compat.py issues --apply` with the `VWS_COMPAT_ISSUE_TOKEN` secret, a token with
  issue write on the owner repos only.

## Issues for failures

`compat.py issues RESULTS` prints one issue per failing row. `--apply` opens it in `popododo0720/<owner>` with the
title `compat: <tool> <resource> <action> fails against VWS`, unless an open issue with that exact title exists in
any repo of the org, so a rerun opens nothing twice, also after an owner moved the issue. `gh issue create` is
tried once: a create whose reply was lost is found by title on the next run. The body names the tool version, the run and the error, and says
`Part of popododo0720/vws-deploy#133`. Results from a `fake` run are refused.

| Resource | Owner |
| --- | --- |
| image, instance, `aws_instance` | vws-console-api |
| volume, `aws_ebs_volume`, `aws_volume_attachment` | vws-console-api |
| vpc, subnet, security-group, address, `aws_vpc`, `aws_subnet`, `aws_security_group`, `aws_eip`, `aws_eip_association` | vws-network |
| bucket, object, `aws_s3_bucket`, `aws_s3_object` | vws-storage |
| provider (`terraform init`) | vws-deploy |

The owner is where an issue starts. When the cause is in the API layer, the owner moves it to vws-console-api.
`OWNERS` in `tests/compat/compat.py` is the only copy of this table that the code reads. A row whose resource is
not in it is an error.

## First fake run

Measured on 2026-11-05 against vws-console-api `CONSOLE_API_FAKE_REF` with aws-cli 2.39.4, terraform 1.17.1 and
provider 6.72.0. The result was pass 23, fail 7, skip 37. These are the fake's results, not VWS:

- `aws-cli instance run-instances`: `InvalidParameterValue: unsupported`. The fake rejects one of the form keys the
  stock CLI sends, so every instance, volume attach and address association row after it is `skip`.
- `aws-cli object cp-upload`: `BadDigest`. aws-cli v2 sends a CRC checksum on PutObject by default.
- `aws-cli object multipart-upload`: `NotImplemented: uploads is not served`.
- `terraform aws_vpc apply` and `aws_eip apply`: `AuthFailure: credential scope rejected`. When an S3 endpoint is
  set and the region is not an AWS region, the provider uses `us-east-1` in place of `vws-1` (its debug log says
  "Substituting non-standard region"), and the EC2 calls then fail the scope check.
- `terraform aws_s3_bucket plan` and `aws_s3_object plan`: the plan after apply wants an update.
