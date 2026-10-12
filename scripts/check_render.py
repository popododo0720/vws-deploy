#!/usr/bin/env python3
"""Recovery invariants for rendered control-plane manifests.

The chart is the only renderer. This script reads that output and fails when a
workload lost a probe, a disruption budget, restartPolicy Always, or its volume.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ALLOWED_KINDS = {
    "ServiceAccount",
    "Service",
    "Deployment",
    "StatefulSet",
    "PodDisruptionBudget",
    "ClusterRole",
    "ClusterRoleBinding",
    "Role",
    "RoleBinding",
}


def load_docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def check_rbac(name: str, spec: dict, by_kind: dict, pod: dict, release: str) -> list[str]:
    errors: list[str] = []
    want = f"{release}-{name}"
    cluster_role = by_kind.get("ClusterRole", {}).get(want)
    binding = by_kind.get("ClusterRoleBinding", {}).get(want)
    lease_role = by_kind.get("Role", {}).get(f"{name}-leader")
    lease_binding = by_kind.get("RoleBinding", {}).get(f"{name}-leader")
    mounted = pod.get("automountServiceAccountToken")
    if "rbac" not in spec:
        if mounted is not False:
            errors.append(f"R11: {name} has no rbac and must not mount an API token")
        if any(obj is not None for obj in (cluster_role, binding, lease_role, lease_binding)):
            errors.append(f"R11: {name} has no rbac but renders RBAC objects")
        return errors
    if mounted is not True:
        errors.append(f"R11: {name} has rbac and needs an API token")
    for kind, obj in (
        ("ClusterRole", cluster_role),
        ("ClusterRoleBinding", binding),
        ("Role", lease_role),
        ("RoleBinding", lease_binding),
    ):
        if obj is None:
            errors.append(f"R11: missing {kind} for {name}")
    for obj in (cluster_role, lease_role):
        for rule in (obj or {}).get("rules") or []:
            for field in ("apiGroups", "resources", "verbs", "resourceNames"):
                if any("*" in item for item in rule.get(field) or []):
                    errors.append(f"R11: {name} has a wildcard in {field}")
    if cluster_role is not None and cluster_role.get("rules") != spec["rbac"]:
        errors.append(f"R11: ClusterRole {want} does not match values")
    if lease_role is not None:
        leases = [r for r in lease_role["rules"] if r.get("resourceNames") == [name]]
        if not leases or not set(leases[0]["verbs"]) >= {"get", "update"}:
            errors.append(f"R11: {name} lacks get and update on its own Lease")
    for obj in (binding, lease_binding):
        subjects = (obj or {}).get("subjects") or []
        if [s.get("name") for s in subjects] != [name]:
            errors.append(f"R11: {name} binding must name only its own ServiceAccount")
    return errors


def check(docs: list[dict], values: dict, release: str) -> list[str]:
    errors: list[str] = []
    by_kind: dict[str, dict[str, dict]] = {}
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name")
        if kind not in ALLOWED_KINDS:
            errors.append(f"R10: kind {kind} is not a control-plane object")
            continue
        if name in by_kind.get(kind, {}):
            errors.append(f"R5: duplicate {kind} {name}")
            continue
        by_kind.setdefault(kind, {})[name] = doc
        pod = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})
        if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
            errors.append(f"R10: {kind}/{name} joins the host namespace")
        for volume in pod.get("volumes") or []:
            if "hostPath" in volume:
                errors.append(f"R10: {kind}/{name} mounts a host path")

    for name, spec in values["services"].items():
        spec = spec or {}
        if "state" in spec and spec["state"] is None:
            errors.append(f"R4: {name} state must be a volume size or be omitted, not null")
            continue
        stateful = "state" in spec
        workload_kind = "StatefulSet" if stateful else "Deployment"
        workload = by_kind.get(workload_kind, {}).get(name)
        if workload is None:
            errors.append(f"R4: missing {workload_kind} {name}")
            continue
        wspec = workload["spec"]
        pod = wspec["template"]["spec"]
        if pod.get("restartPolicy") != "Always":
            errors.append(f"R4: {name} restartPolicy is not Always")
        containers = pod.get("containers") or []
        if len(containers) != 1:
            errors.append(f"R4: {name} must have one container")
            continue
        container = containers[0]
        expected = (
            ("startupProbe", "/healthz"),
            ("livenessProbe", "/healthz"),
            ("readinessProbe", "/readyz"),
        )
        for probe, path in expected:
            http = (container.get(probe) or {}).get("httpGet") or {}
            if http.get("path") != path or http.get("port") != "http":
                errors.append(f"R4: {name} {probe} must request {path} on port http")
        startup_seconds = int(spec.get("startupSeconds") or 60)
        want_threshold = (startup_seconds + 4) // 5 + 1
        got_threshold = (container.get("startupProbe") or {}).get("failureThreshold")
        if got_threshold != want_threshold:
            errors.append(
                f"R4: {name} startup budget is {got_threshold}, want {want_threshold}"
            )
        pdb = by_kind.get("PodDisruptionBudget", {}).get(name)
        if pdb is None:
            errors.append(f"R4: missing PodDisruptionBudget {name}")
        else:
            pspec = pdb.get("spec") or {}
            if pspec.get("maxUnavailable") != 1 or "minAvailable" in pspec:
                errors.append(f"R4: PodDisruptionBudget {name} must set maxUnavailable 1")
        service = by_kind.get("Service", {}).get(name)
        if service is None:
            errors.append(f"R4: missing Service {name}")
        else:
            want = "LoadBalancer" if spec.get("external") else "ClusterIP"
            if (service.get("spec") or {}).get("type") != want:
                errors.append(f"R4: Service {name} type is not {want}")
        errors.extend(check_rbac(name, spec, by_kind, pod, release))
        if stateful:
            if wspec.get("replicas") != 1:
                errors.append(f"R4: StatefulSet {name} must have one replica")
            if wspec.get("podManagementPolicy") != "Parallel":
                errors.append(f"R4: StatefulSet {name} must start every pod after a reboot")
            retain = wspec.get("persistentVolumeClaimRetentionPolicy") or {}
            if retain.get("whenDeleted") != "Retain" or retain.get("whenScaled") != "Retain":
                errors.append(f"R4: StatefulSet {name} must retain its volume")
            claims = wspec.get("volumeClaimTemplates") or []
            if len(claims) != 1 or claims[0].get("metadata", {}).get("name") != "state":
                errors.append(f"R4: StatefulSet {name} needs one state volume")
            else:
                request = claims[0]["spec"]["resources"]["requests"]["storage"]
                if request != spec["state"]["size"]:
                    errors.append(f"R4: StatefulSet {name} volume size does not match values")
                if claims[0]["spec"].get("accessModes") != ["ReadWriteOnce"]:
                    errors.append(f"R4: StatefulSet {name} volume must be ReadWriteOnce")
                labels = claims[0].get("metadata", {}).get("labels") or {}
                moving = [key for key in labels if key in ("app.kubernetes.io/version", "helm.sh/chart")]
                if moving:
                    errors.append(
                        f"R4: {name} volume claim labels {moving} change on every release"
                    )
            mounts = [
                mount
                for mount in container.get("volumeMounts") or []
                if mount.get("mountPath") == "/var/lib/vws"
            ]
            if len(mounts) != 1 or mounts[0].get("name") != "state":
                errors.append(f"R4: {name} must mount its volume at /var/lib/vws")
            peers = by_kind.get("Service", {}).get(f"{name}-peers")
            if peers is None or (peers.get("spec") or {}).get("clusterIP") != "None":
                errors.append(f"R4: StatefulSet {name} needs a headless peers Service")
        else:
            if wspec.get("replicas", 0) < 2:
                errors.append(f"R4: Deployment {name} needs at least two replicas")
            rolling = (wspec.get("strategy") or {}).get("rollingUpdate") or {}
            if rolling.get("maxUnavailable") != 0:
                errors.append(f"R4: Deployment {name} must keep a ready pod during rollout")
            if any(mount.get("name") == "state" for mount in container.get("volumeMounts") or []):
                errors.append(f"R4: Deployment {name} must not mount a state volume")
    return errors


def _first(docs: list[dict], kind: str) -> dict | None:
    for doc in docs:
        if doc.get("kind") == kind:
            return doc
    return None


def self_test(docs: list[dict], values: dict, release: str) -> list[str]:
    base = check(docs, values, release)
    if base:
        return ["R4: rendered chart failed before mutations"] + base

    deployment = _first(docs, "Deployment")
    if deployment is not None:
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["containers"][0].pop(
            "livenessProbe", None
        )
        if not check(broken, values, release):
            return ["R4: removing livenessProbe did not fail"]
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["restartPolicy"] = "Never"
        if not check(broken, values, release):
            return ["R4: restartPolicy Never did not fail"]
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["hostNetwork"] = True
        if not check(broken, values, release):
            return ["R10: hostNetwork did not fail"]

    stateful_doc = _first(docs, "StatefulSet")
    if stateful_doc is not None:
        broken = copy.deepcopy(docs)
        _first(broken, "StatefulSet")["spec"].pop("volumeClaimTemplates", None)
        if not check(broken, values, release):
            return ["R4: removing the state volume did not fail"]

    stateless = next(
        (name for name, spec in values["services"].items() if "state" not in (spec or {})),
        None,
    )
    if stateless is not None:
        broken = [
            doc
            for doc in copy.deepcopy(docs)
            if not (
                doc.get("kind") == "PodDisruptionBudget" and doc["metadata"]["name"] == stateless
            )
        ]
        if not check(broken, values, release):
            return ["R4: removing a PodDisruptionBudget did not fail"]

    stateful = next(
        (name for name, spec in values["services"].items() if "state" in (spec or {})),
        None,
    )
    if stateful is not None:
        broken_values = copy.deepcopy(values)
        broken_values["services"][stateful]["state"] = None
        if not check(docs, broken_values, release):
            return ["R4: null state did not fail"]

    with_rbac = next(
        (name for name, spec in values["services"].items() if "rbac" in (spec or {})), None
    )
    if with_rbac is not None:
        broken = copy.deepcopy(docs)
        for doc in broken:
            if doc.get("kind") == "ClusterRole" and doc["metadata"]["name"] == f"{release}-{with_rbac}":
                doc["rules"][0]["resources"] = ["*"]
        if not check(broken, values, release):
            return ["R11: a wildcard resource did not fail"]
        broken = [
            doc
            for doc in copy.deepcopy(docs)
            if not (doc.get("kind") == "RoleBinding" and doc["metadata"]["name"] == f"{with_rbac}-leader")
        ]
        if not check(broken, values, release):
            return ["R11: removing the Lease binding did not fail"]
        broken = [
            doc
            for doc in copy.deepcopy(docs)
            if not (doc.get("kind") == "ClusterRoleBinding" and doc["metadata"]["name"] == f"{release}-{with_rbac}")
        ]
        if not check(broken, values, release):
            return ["R11: removing the ClusterRoleBinding did not fail"]

    # A catalog with only one kind must still prove the other kind's check.
    synthetic = check(
        [
            {
                "kind": "Deployment",
                "metadata": {"name": "probe"},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{"name": "probe"}],
                        }
                    },
                },
            }
        ],
        {"services": {"probe": {}}},
        release,
    )
    if not any("restartPolicy" in item or "livenessProbe" in item for item in synthetic):
        return ["R4: synthetic deployment did not fail the recovery check"]
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: check_render.py RENDERED.yaml VALUES.yaml RELEASE", file=sys.stderr)
        return 2
    docs = load_docs(Path(argv[1]).read_text())
    values = yaml.safe_load(Path(argv[2]).read_text())
    errors = self_test(docs, values, argv[3])
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
