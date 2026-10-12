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
    "Namespace",
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


# Resources a ClusterRole rule may name. Everything else is namespaced and needs a rule with a
# namespace. The kubeovn.io entries are the CRDs of the pinned bundle that VWS writes; their
# scope is Cluster in the rendered Kube-OVN CRDs.
CLUSTER_SCOPED = {
    ("", "namespaces"),
    ("", "nodes"),
    ("", "persistentvolumes"),
    ("storage.k8s.io", "storageclasses"),
    ("snapshot.storage.k8s.io", "volumesnapshotclasses"),
    ("kubeovn.io", "vpcs"),
    ("kubeovn.io", "subnets"),
    ("kubeovn.io", "security-groups"),
    ("kubeovn.io", "ovn-eips"),
    ("kubeovn.io", "ovn-fips"),
    ("kubeovn.io", "ovn-snat-rules"),
    ("kubeovn.io", "vpc-nat-gateways"),
}


def load_docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def check_rbac(name: str, spec: dict, by_kind: dict, pod: dict, release: str, ns: str) -> list[str]:
    errors: list[str] = []
    want = f"{release}-{name}"
    cluster_role = by_kind.get("ClusterRole", {}).get(want)
    binding = by_kind.get("ClusterRoleBinding", {}).get(want)
    lease_role = by_kind.get("Role", {}).get((ns, f"{name}-leader"))
    lease_binding = by_kind.get("RoleBinding", {}).get((ns, f"{name}-leader"))
    mounted = pod.get("automountServiceAccountToken")
    own_roles = {
        key[0]: obj for key, obj in by_kind.get("Role", {}).items() if key[1] == want
    }
    own_bindings = {
        key[0]: obj for key, obj in by_kind.get("RoleBinding", {}).items() if key[1] == want
    }
    if "rbac" not in spec:
        if mounted is not False:
            errors.append(f"R11: {name} has no rbac and must not mount an API token")
        if any(
            obj is not None for obj in (cluster_role, binding, lease_role, lease_binding)
        ) or own_roles or own_bindings:
            errors.append(f"R11: {name} has no rbac but renders RBAC objects")
        return errors
    if mounted is not True:
        errors.append(f"R11: {name} has rbac and needs an API token")

    cluster_rules = [rule for rule in spec["rbac"] if "namespace" not in rule]
    ns_rules: dict[str, list[dict]] = {}
    for rule in spec["rbac"]:
        if "namespace" in rule:
            ns_rules.setdefault(rule["namespace"], []).append(
                {key: value for key, value in rule.items() if key != "namespace"}
            )
    for rule in cluster_rules:
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                if (group, resource) not in CLUSTER_SCOPED:
                    errors.append(
                        f"R11: {name} gives {group or 'core'}/{resource} to every namespace;"
                        " add a namespace to the rule or list it in CLUSTER_SCOPED"
                    )

    expected = [("Role", lease_role), ("RoleBinding", lease_binding)]
    if cluster_rules:
        expected += [("ClusterRole", cluster_role), ("ClusterRoleBinding", binding)]
    elif cluster_role is not None or binding is not None:
        errors.append(f"R11: {name} has no cluster rules but renders a ClusterRole")
    for kind, obj in expected:
        if obj is None:
            errors.append(f"R11: missing {kind} for {name}")
    for namespace in ns_rules:
        if own_roles.get(namespace) is None:
            errors.append(f"R11: missing Role {want} in {namespace}")
        if own_bindings.get(namespace) is None:
            errors.append(f"R11: missing RoleBinding {want} in {namespace}")
        if namespace not in by_kind.get("Namespace", {}):
            errors.append(f"R11: missing Namespace {namespace}")
    for extra in (set(own_roles) | set(own_bindings)) - set(ns_rules):
        errors.append(f"R11: {name} has a Role or RoleBinding in {extra} that values do not ask for")

    for obj in [cluster_role, lease_role, *own_roles.values()]:
        for rule in (obj or {}).get("rules") or []:
            for field in ("apiGroups", "resources", "verbs", "resourceNames"):
                if any("*" in item for item in rule.get(field) or []):
                    errors.append(f"R11: {name} has a wildcard in {field}")
    if cluster_role is not None and cluster_role.get("rules") != cluster_rules:
        errors.append(f"R11: ClusterRole {want} does not match values")
    for namespace, rules in ns_rules.items():
        role = own_roles.get(namespace)
        if role is not None and role.get("rules") != rules:
            errors.append(f"R11: Role {want} in {namespace} does not match values")
    if lease_role is not None:
        leases = [r for r in lease_role["rules"] if r.get("resourceNames") == [name]]
        if not leases or not set(leases[0]["verbs"]) >= {"get", "update"}:
            errors.append(f"R11: {name} lacks get and update on its own Lease")
    for obj in (binding, lease_binding, *own_bindings.values()):
        if obj is None:
            continue
        subjects = obj.get("subjects") or []
        if [(s.get("name"), s.get("namespace")) for s in subjects] != [(name, ns)]:
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
        key = (doc["metadata"].get("namespace"), name) if kind in ("Role", "RoleBinding") else name
        if key in by_kind.get(kind, {}):
            errors.append(f"R5: duplicate {kind} {name}")
            continue
        by_kind.setdefault(kind, {})[key] = doc
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
        errors.extend(check_rbac(name, spec, by_kind, pod, release, workload["metadata"].get("namespace")))
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

    def service_with(pred):
        return next(
            (
                name
                for name, spec in values["services"].items()
                if any(pred(rule) for rule in (spec or {}).get("rbac") or [])
            ),
            None,
        )

    with_cluster = service_with(lambda rule: "namespace" not in rule)
    with_ns = service_with(lambda rule: "namespace" in rule)
    with_rbac = with_cluster or with_ns

    def without(kind, name, namespace=None):
        return [
            doc
            for doc in copy.deepcopy(docs)
            if not (
                doc.get("kind") == kind
                and doc["metadata"]["name"] == name
                and (namespace is None or doc["metadata"].get("namespace") == namespace)
            )
        ]

    if with_cluster is not None:
        broken = copy.deepcopy(docs)
        for doc in broken:
            if doc.get("kind") == "ClusterRole" and doc["metadata"]["name"] == f"{release}-{with_cluster}":
                doc["rules"][0]["resources"] = ["*"]
        if not check(broken, values, release):
            return ["R11: a wildcard resource did not fail"]
        if not check(without("ClusterRoleBinding", f"{release}-{with_cluster}"), values, release):
            return ["R11: removing the ClusterRoleBinding did not fail"]
    if with_rbac is not None:
        if not check(without("RoleBinding", f"{with_rbac}-leader"), values, release):
            return ["R11: removing the Lease binding did not fail"]
    if with_ns is not None:
        role = next(
            doc
            for doc in docs
            if doc.get("kind") == "Role"
            and doc["metadata"]["name"] == f"{release}-{with_ns}"
        )
        namespace = role["metadata"]["namespace"]
        if not check(without("Role", f"{release}-{with_ns}", namespace), values, release):
            return ["R11: removing a namespaced Role did not fail"]
        if not check(without("RoleBinding", f"{release}-{with_ns}", namespace), values, release):
            return ["R11: removing a namespaced RoleBinding did not fail"]
        broken = copy.deepcopy(docs)
        for doc in broken:
            if doc is not None and doc.get("kind") == "Role" and doc["metadata"] == role["metadata"]:
                doc["rules"][0]["verbs"] = doc["rules"][0]["verbs"] + ["patch"]
        if not check(broken, values, release):
            return ["R11: a Role that differs from values did not fail"]
        broken = copy.deepcopy(docs)
        extra = copy.deepcopy(role)
        extra["metadata"]["namespace"] = "elsewhere"
        broken.append(extra)
        if not check(broken, values, release):
            return ["R11: a Role in a namespace that values do not ask for did not fail"]
        broken = without("Namespace", namespace)
        if not check(broken, values, release):
            return ["R11: a missing Namespace did not fail"]
        # A namespaced resource in a ClusterRole is the cluster-wide grant R11 forbids.
        broken_values = copy.deepcopy(values)
        for rule in broken_values["services"][with_ns]["rbac"]:
            rule.pop("namespace", None)
        wide = [err for err in check(docs, broken_values, release) if "every namespace" in err]
        if not wide:
            return ["R11: a namespaced resource without a namespace did not fail"]

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
