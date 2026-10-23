# 0.1.0 upgrade fixture

Tag: none yet. This repo has cut no release tag (`git tag` is empty), so the record is pinned to
the commit below; the chart version there is `0.1.0` (`charts/vws/Chart.yaml`).
Commit: 6d46293b42b82981d01e62d8380f887978c8212c
Command: `scripts/vws init --from testdata/upgrade/0.1.0/vwscluster.yaml`, then
`scripts/vws apply --unattended`, both at that commit with the fake kubectl and helm of `tests/vws`
(no cluster).
Recorded object: `vwscluster.yaml` is the input CR, which `init --from` copied as is; the API
server gives that first save `metadata.generation: 1`. `vwscluster.json` is that object plus the
status patch `scripts/vws.d/apply.sh` of the commit above wrote (`observedGeneration`,
`appliedValuesHash`, `specHistory: []`, the `Applied` condition). The file was assembled from the
harness calls, not read back from a live cluster. `status.appliedValuesHash` is the hash
`vws_applied_hash` gives for this spec at that commit.

Regenerating a record is allowed only at a release bump (AGENTS.md R23). A change that cannot read
this record fails `tests/vws/upgrade-compat.test.sh` and `scripts/guard.sh upgrade-compat`; the
guard also checks that the commit above is in this history and carries chart version `0.1.0`.

`apiVersion` is `FILLED_BY_TEST` for the same reason `tests/fixtures/installer` uses it: R18 keeps
the VWSCluster API group string only in `charts/vws/crds/vwsclusters.yaml`. The test and the guard
fill it from that CRD, so an API group or version change is not covered by this fixture.
