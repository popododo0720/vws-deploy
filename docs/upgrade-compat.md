# Upgrade-compat fixtures

R23 requires every VWS repo that persists records to keep a golden record written by the previous
released build under `testdata/upgrade/<version>/`, with a test that decodes it using the current
code in the normal CI `test` job. A change that cannot read the record fails CI. Regenerating a
fixture is allowed only at a release bump.

This page is the cross-repo inventory for vws-deploy#396. Each repo owns its fixtures and its test;
the row links the issue or PR that carries the work.

## Inventory

| Repo | Persisted records | Fixture and test | Status |
| --- | --- | --- | --- |
| vws-deploy | `VWSCluster` record, including `status.appliedValuesHash`, `specHistory` and the `Applied` condition | `testdata/upgrade/0.1.0/`; `tests/vws/upgrade-compat.test.sh` and `scripts/guard.sh upgrade-compat` | Done, #445 |
| vws-storage | SQLite rows for volumes, snapshots, create-operation tokens and operations | `testdata/upgrade/0.1.0/storage.db`; `TestUpgradeFrom010RecordsRoundTrip` and `TestUpgradeFrom010DecodesStoredRecords` | Done, vws-storage#129 |
| vws-console-api | Detach records, operation-id labels, the held map and the idempotency record | `testdata/upgrade/ceaf591/`; `TestPreviousReleaseRecords` and `TestPreviousReleaseIdempotencyRecord` | Done, vws-console-api#341 |
| vws-compute | One ConfigMap per resource (`instance`, `image`, `keypair`) plus the request-id indexes and the key-name and volume claims | `testdata/upgrade/89f2ca3/configmaps.json`; `TestPreviousReleaseRecordsDecode` | PR, vws-compute#131 |
| vws-iam | Eleven namespaced CRs: users, groups, roles, policies, access keys, MFA devices, the access-key index, console sessions, operations, STS replay entries and attempt counters | `testdata/upgrade/sha-74a29dc/`; `TestPreviousReleaseRecordsDecode` | Task filed, vws-iam#152 |
| vws-network | SQLite `projects` state documents (VPCs, subnets, groups, rules, elastic IPs), `operations` and `addresses`, plus the Kube-OVN objects it owns through the id annotation (`internal/network/annotation_test.go`) | `testdata/upgrade/<version>/`; a decode test over `openSQLite` and the Kube-OVN `ensure` path | Task filed, vws-network#135 |

## Adding a fixture

1. Pick the commit or image tag of the last released build. When the repo has no tag, name the build
   by its commit, as `publish.yml` names an image `sha-<7>`. Record it as `Tag:` and as the full
   `Commit:` in `testdata/upgrade/<version>/README.md`, with the exact `Command:` that wrote the
   files.
2. Capture the record the released build actually wrote. Do not hand-write it. Use a throwaway
   generator or a dump of the store, and keep the generator under `testdata/upgrade/<version>/gen/`
   with a `.txt` suffix so `go test ./...` never compiles it.
3. Strip cluster metadata the next API server assigns (`uid`, `resourceVersion`, `creationTimestamp`,
   `managedFields`) unless the store reads it. Keep `metadata.name` and the ownership annotation or
   labels the store needs to find the record.
4. Add the decode test beside the store. Copy the fixture to `t.TempDir()` when the store opens it
   for writing, so the committed file never changes. Prove the test fails by renaming one stored
   field or changing one encoding, show that failure in the PR, then revert it.
5. Name the test in the repo's `AGENTS.md` rule row. Do not regenerate the fixture outside a release
   bump.
