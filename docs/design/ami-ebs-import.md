# AMI and EBS snapshot import (#135)

> **Historical Kubernetes design.** The Helm chart and its CRDs were retired in #774. The supported deployment path is `docs/operator/regions.md`.

## Problem

Users moving from AWS (#132, target user (d)) need their AMIs and EBS snapshots in VWS: an imported AMI is an image that RunInstances boots, and an imported EBS snapshot is a snapshot that CreateVolume restores. Today nothing brings a disk from outside the cluster into VWS:

- vws-compute `CreateImage` takes only a VWS volume or snapshot as its source (vws-proto `vws/compute/v1/image.proto`, `ImageSpec.source`). Its `-seed-images` flag registers disk files from the agent's host, which is an install-time path, not a user API.
- vws-storage `CreateVolume` takes only a VWS image or snapshot as its source (vws-proto `vws/storage/v1/volume.proto`, `VolumeSpec.source`), and `CreateSnapshot` takes only a VWS volume (`snapshot.proto`, `SnapshotSpec.volume`).
- vws-console-api serves no `ImportImage`, `ImportSnapshot`, `DescribeImportSnapshotTasks` or `RegisterImage`.
- `vws apply` seeds one image (`ami-0123456789abcdef0`) from a digest-pinned container disk (`scripts/vws.d/apply.sh`, `vws_apply_seed_image`, #232). That is the lab default image, not an import path.

The bytes have to leave AWS first. AWS offers two ways out:

- **VM Import/Export**: `aws ec2 export-image --image-id ami-... --disk-image-format RAW --s3-export-location S3Bucket=...,S3Prefix=...` writes the AMI as one disk image object in an S3 bucket. AWS refuses some AMIs for export (see the AWS VM Import/Export user guide for the current list).
- **EBS direct APIs**: `ListSnapshotBlocks` and `GetSnapshotBlock` read a snapshot block by block. The AWS tool `coldsnap download snap-... disk.raw` writes a snapshot as a raw disk file. An AMI's root device is an EBS snapshot (`aws ec2 describe-images` names it in `BlockDeviceMappings`), so this path covers both AMIs and data volumes.

## Decision

One in-cluster mechanism: **a VWS volume can be created from a disk image at a URL.** The rest is composed from APIs that exist.

```
AMI  ── export-image RAW ──> S3 object ─┐
EBS snapshot ── coldsnap ──> raw file ──> upload to an S3/HTTPS endpoint the cluster reaches
                                        │
          ImportSnapshot(DiskContainer.Url, Format=RAW)
                                        │  console-api
                                        ▼
  vws-storage CreateVolume(source = url)  →  CDI DataVolume import (s3 or http source) into a PVC
                                        ▼
  vws-storage CreateSnapshot(volume)    →  VolumeSnapshot; the temporary volume is deleted
                                        ▼
  RegisterImage(BlockDeviceMapping snapshot)  →  vws-compute CreateImage(source = snapshot) (exists)
                                        ▼
  RunInstances(image)                   →  vws-storage clones the root volume (exists, vws-storage#88)
```

- **vws-proto**: `VolumeSpec.source` gains an import source: a URL (`https://` or `s3://`), a disk format (`RAW`, later `QCOW2`), and the name of a Secret in the caller's project that holds the S3 credentials, empty for a public or presigned HTTPS URL. A volume created from it is the same Volume resource afterwards.
- **vws-storage**: implements that source as a CDI DataVolume with an `s3` or `http` source in the VM namespace, size taken from the disk image (rounded up to whole GiB, or the requested `size_gib` when larger), status progress from the DataVolume. CDI comes from the platform bundle (`CDI_VERSION` in `scripts/versions.env`).
- **vws-console-api**: EC2 `ImportSnapshot` and `DescribeImportSnapshotTasks` (an import task is CreateVolume from the URL, then CreateSnapshot, then DeleteVolume of the temporary volume), and `RegisterImage` with a root `BlockDeviceMappings` snapshot. `ImportImage` is `ImportSnapshot` followed by `RegisterImage` and can come later. Error codes follow EC2 (`InvalidParameter`, `InvalidSnapshot.NotFound`, `InvalidImportTaskId.NotFound`).
- **vws-compute**: `CreateImage` from a snapshot already exists. Its work is the boot check of an imported image (below) and any device-model setting that check shows is needed.
- **vws-deploy**: RBAC for the new vws-storage calls goes through `services.storage.rbac` in the former chart values (R11). CDI importer pods need egress to the source URL; the chart renders no NetworkPolicy today, so nothing blocks it, but a later egress policy must allow it. A migration runbook (export, coldsnap, upload, import, boot) goes into docs once the APIs exist.

Getting bytes out of AWS stays on the operator's workstation with their own AWS credentials (`aws ec2 export-image` or `coldsnap`). No AWS credential is stored in VWS for the export step. The only credential VWS sees is the one for the bucket it reads, and only by Secret name.

## Options considered

1. **Volume from a URL, composed into ImportSnapshot and RegisterImage (chosen).** One new source field and one new CDI source kind in vws-storage. Snapshot, image and boot reuse existing code. It maps onto the EC2 import API that unmodified `aws ec2 import-snapshot` and the Terraform `aws_ebs_snapshot_import` resource call, which is the #132 promise (same tools, endpoint changed).
2. **An in-cluster EBS direct API reader.** A VWS component reads snapshot blocks straight from AWS. Rejected: it stores long-lived AWS credentials in the cluster, adds a new control service whose only job is a one-time migration, and has no EC2 API a user would call on VWS for it.
3. **CDI upload (`virtctl image-upload`) from the workstation.** Rejected as the main path: it needs `cdi-uploadproxy` exposed outside the cluster and a Kubernetes token for the uploader, which a VWS user (an IAM principal, not a cluster user) does not have. It also has no EC2 API shape.
4. **New image source in vws-compute (`ImageSpec.source = url`).** Rejected: it covers AMIs but not EBS snapshots, and compute would import bytes, which is storage's job (`ImageSpec` sources are storage resources today).

## Boot of an imported image

The issue's end state is an instance that boots from an imported AMI. These are the risks found while designing; each is a guess until the live check (below) runs on a cluster:

- **Virtualization type.** Only HVM AMIs can boot on KubeVirt. A paravirtual (PV) AMI has no bootloader for a full VM. `RegisterImage` and `ImportSnapshot` should refuse nothing by type (AWS accepts both), but the runbook says HVM only.
- **Drivers.** Nitro AMIs boot on NVMe and ENA. KubeVirt gives virtio disks and NICs by default. Current Linux kernels ship virtio drivers, but an initramfs built only for the AWS host may not include them. The live check shows whether a stock AWS-published Linux 2023 AMI and an Ubuntu 24.04 AMI boot unmodified.
- **Metadata.** VWS passes SSH keys and user data through cloud-init NoCloud (vws-compute `internal/hypervisor/kubevirt/cloudinit.go`). VWS has no EC2 instance metadata service at `169.254.169.254` (no vws-* service serves that address; a search of the repos finds it only in the vws-conformance endpoint deny tests). An AMI whose cloud-init is limited to the `Ec2` datasource boots but gets no SSH key and no user data. Whether stock AMIs are limited that way is a guess to confirm live. If they are, the fix is an EC2-compatible metadata service, a separate design in vws-compute and vws-network, and not a rewrite of the guest disk at import (that would change the user's image).

## Implementation issues

- popododo0720/vws-proto#145: volume import source (URL, format, Secret name).
- popododo0720/vws-storage#188: CreateVolume from a URL through CDI.
- popododo0720/vws-console-api#566: ImportSnapshot, DescribeImportSnapshotTasks, RegisterImage from a snapshot.
- popododo0720/vws-compute#179: boot check of an imported HVM AMI, and the metadata gap above.
- vws-deploy#677: the live check of the whole path, held until a test cluster exists.
