# 0.1.0 upgrade fixture

Tag: v0.1.0 (chart `0.1.0`, `charts/vws/Chart.yaml`; `.github/workflows/installer-publish.yml`
publishes on `v*` tags. No tag is pushed yet, so the record was written by the 0.1.0 tree at the
commit that adds this file.)
Command: `scripts/vws init --from examples/vwscluster/single-node.yaml`, then
`scripts/vws apply --unattended`, then `kubectl get vwsclusters vws -o json`.
Recorded object: the VWSCluster `vws` as the cluster returned it after that install, with
`status.appliedValuesHash` from that release.

Regenerating a record is allowed only at a release bump (AGENTS.md R23). A change that cannot read
this record fails `tests/vws/upgrade-compat.test.sh` and `scripts/guard.sh upgrade-compat`.

`apiVersion` is `FILLED_BY_TEST` for the same reason `tests/fixtures/installer` uses it: R18 keeps
the VWSCluster API group string only in `charts/vws/crds/vwsclusters.yaml`. The test and the guard
fill it from that CRD.
