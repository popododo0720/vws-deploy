# IAM listeners

## Problem

console-api reaches AWS-shaped EC2, S3 and IAM on the addresses it is given. The vws-iam binary listens on `127.0.0.1:9095` unless `-listen` says otherwise, and its health port stays on `127.0.0.1:9096`. A Service on another port, or a loopback address, leaves those listeners unreachable from outside the pod. The chart is the only renderer. This repo does not apply it to a cluster.

## Usage

A VWSCluster turns iam on and publishes the three AWS listeners. The chart writes `-iam-endpoint`. The CR does not set it.

```yaml
spec:
  services:
    iam:
      enabled: true
    console-api:
      flags:
        iam: grpc
        ec2-addr: 0.0.0.0:8081
        s3-addr: 0.0.0.0:8082
        iam-addr: 0.0.0.0:8084
```

`services.iam.port` stays 9095 and `services.iam.flags.listen` stays `0.0.0.0:9095`. With `enabled: true` the image is `ghcr.io/popododo0720/vws/iam:sha-b567471` (`services.iam.imageTag`, digest `sha256:ea481d637e103b7426e7c9b12b8ea1e9291ab1de65ecfe6ce746f8c6660b231d`). `spec.version` does not move it. `enabled` stays false in the catalog.

## Shape

Two facts, one owner each.

The iam listen port lives only in `charts/vws/values.yaml` `services.iam.port` and `services.iam.flags.listen`. Both are 9095. `-health-listen` is not passed.

An AWS listen address lives only in a flag whose key ends in `-addr`. A string `0.0.0.0:<n>`, with `n` in 1024..65535 and `n` different from the service's primary port, becomes one extra port on the client Service. The port name is the key with `-addr` removed, and it matches `^[a-z]([a-z0-9-]{0,13}[a-z0-9])?$`. Ports are sorted by number. A repeated name or port fails the render. `127.0.0.1:<n>` fails the render. A `0.0.0.0` port outside 1024..65535 fails the render. One `-addr` flag may use the primary port, which is how `-console-addr` stays the probe port. A second `-addr` flag on that same port fails the render. Any other value, including `compute:50051`, stays an argument. `flags.listen` is not an `-addr` flag.

When `flags.iam` is the string `grpc`, the render fails if `services.iam.enabled` is false, fails if the caller set `iam-endpoint`, and sets `iam-endpoint` to `iam:<services.iam.port>`. When `flags.iam` is absent or any other value, the render fails if `iam-endpoint` is present and adds nothing. The rule does not branch on the service name.

`vws.extraPorts` and the iam rule in `vws.args` do not read `.Values`. `control-plane.yaml` passes `iamEnabled` and `iamPort`. `iamEnabled` is true unless `services.iam.enabled` is false. `iamPort` is `services.iam.port`. The same rule is in `scripts/check_render.py` `want_args`. `render.py` does not add the endpoint. There is no `publishedPorts` field, no CRD field, no schema field and no profile key.

Extra ports are appended only on the client Service. The headless `-peers` Service stays one `http` port. Container ports stay the primary port. The extra targetPort is the number.

The NetworkPolicy `ports` entry is the integer `services.iam.port`. kube-ovn with `--np-enforcement=standard` does not resolve a named port, so `port: http` blocks console-api from `iam:9095`. `check_render.py` rejects a NetworkPolicy port that is not that integer, a port range (`endPort`), and a protocol other than TCP.

`services.iam.replicas` is 1. Users are in memory, so a second replica would split them (vws-iam#110). One replica does not survive a restart. A shared store is still required.

## Tradeoffs

The Service publishes 8081, 8082 and 8084 with numeric targetPorts. The container port list stays the primary port, which is what the probes use. The process binds the extra addresses itself.

`iam-endpoint` is derived, so a CR uses the same iam port as `services.iam.port`.

`127.0.0.1` on an `-addr` flag fails the render, so a loopback address cannot look configured while staying unreachable.

`enabled` stays false, so the default render still leaves iam out. With `enabled` true the image is `ghcr.io/popododo0720/vws/iam:sha-b567471`. The digest stays in the comment next to `imageTag`. The template builds `registry/name:tag` and does not take a digest.

## Alternatives rejected

A `publishedPorts` list would be a second catalog that can drift from the flags the process binds.

A `vws-test` profile would change every install. Profiles share one key set. One cluster sets the fields on its VWSCluster.

containerPorts for 8081, 8082 and 8084 would add ports the probes do not use. The workload keeps one container port. The Service targetPort is the number.

A per-service template would copy the Service. One emitter owns every Service.

Teaching the CR a port field would give the listen port a second owner. The port already lives in values. The CR sets `enabled` and flags.

## Gaps

| Gap | Standing |
| --- | --- |
| live AWS CLI `iam create-user` and `list-users` | console-api#198. Those operations return `InvalidAction` over gRPC. This chart does not implement them |
| `sts get-caller-identity` with a non-root key | this repo does not apply to a cluster |
| AWS error shapes | handlers are in vws-console-api, not this chart |
| one iam replica | mitigation only. Users stay in memory. A restart drops them. The shared store is vws-iam#110 |
| the default install does not start iam | vws-iam#112 published `sha-b567471` (digest `sha256:ea481d637e103b7426e7c9b12b8ea1e9291ab1de65ecfe6ce746f8c6660b231d`). An anonymous manifest request returned HTTP 200 on 2026-10-15. `services.iam.imageTag` is that tag. `enabled` stays false, so a default install does not pull it |
| the live Deployment described as `vws-iam-test` is not in this repo | the chart creates Deployment and Service `iam` |
| the live VWSCluster is not in this repo | nothing here changes vws-test until an operator sets the fields and runs `vws apply` |
