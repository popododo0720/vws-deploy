# Cross-service image pin rules

Each service in `charts/vws/values.yaml` pins its image with `services.<name>.imageTag`
(vws-deploy#280). Most pins move on their own. The rules below list the pins that must move
together, because a newer build of one service reads a field that only a newer build of another
service writes. A PR that moves the first pin past its threshold moves the second pin in the same PR
to a published build at or after the floor.

A published build is a `sha-<7>` tag for which an anonymous manifest request to `ghcr.io` returns
HTTP 200 with a `docker-content-digest` header. The values comment above the pin cites that digest
and the date of the request.

## Rules

| When this pin includes | Then this pin must include | Why | Issue |
| --- | --- | --- | --- |
| `console-api` at or after vws-console-api `ac413c5` (vws-console-api#457) | `compute` at or after vws-compute `f609a2e` (vws-compute#141) | From #457, DescribeVolumes reports a volume `attached` only when compute reports `ATTACHED` in `InstanceStatus.volumes` (vws-proto v0.8.0). Compute writes that field from #141. With an older compute, every attached volume reads `attaching` forever. | vws-deploy#618 |

"At or after" means the pinned commit has the named commit as an ancestor on that repo's `main`.
Check it with `git merge-base --is-ancestor <floor> <pinned commit>` in a clone of the service repo.

## Published floor builds

On 2026-11-02 an anonymous manifest request returned HTTP 200 for these floor builds:

| Image | Tag | Digest |
| --- | --- | --- |
| `ghcr.io/popododo0720/vws/console-api` | `sha-ac413c5` | `sha256:779d710a4b7d2226bee1adbd6c55f0067d03a96958578d90e3321758f5310e84` |
| `ghcr.io/popododo0720/vws/compute` | `sha-f609a2e` | `sha256:d8ebeacba67126c06b24b45ee851244fb822ca3fd1c579033f41d3ee21195653` |

The chart pins on that date (`console-api` `sha-c09cba3`, `compute` `sha-1b62a87`) were both before
their thresholds. On 2026-11-06 console-api moved to `sha-d3a4057` (vws-deploy#310), still before
`ac413c5`, so the rule does not apply yet.
