# 0.1.0 upgrade fixture

Tag: none yet. This repo has cut no release tag (`git tag` is empty), so the record is pinned to
the commit below; the chart version there is `0.1.0` (`charts/vws/Chart.yaml`).
Commit: 6d46293b42b82981d01e62d8380f887978c8212c
Command: `scripts/vws init --from examples/vwscluster/single-node.yaml`, then
`scripts/vws apply --unattended`, then `kubectl get vwsclusters vws -o json`.
Recorded object: the VWSCluster `vws` as the cluster returned it after that install, with
`status.appliedValuesHash` from that release.

Regenerating a record is allowed only at a release bump (AGENTS.md R23). A change that cannot read
this record fails `tests/vws/upgrade-compat.test.sh` and `scripts/guard.sh upgrade-compat`; the
guard also checks that the commit above is in this history and carries chart version `0.1.0`.

`apiVersion` is `FILLED_BY_TEST` for the same reason `tests/fixtures/installer` uses it: R18 keeps
the VWSCluster API group string only in `charts/vws/crds/vwsclusters.yaml`. The test and the guard
fill it from that CRD, so an API group or version change is not covered by this fixture.
