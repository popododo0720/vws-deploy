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
}


def load_docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def check(docs: list[dict], values: dict) -> list[str]:
    errors: list[str] = []
    by_kind: dict[str, dict[str, dict]] = {}
    for doc in docs:
        kind = doc.get("kind")
        name = doc.get("metadata", {}).get("name")
        if kind not in ALLOWED_KINDS:
            errors.append(f"R3: kind {kind} is not a control-plane object")
            continue
        if name in by_kind.get(kind, {}):
            errors.append(f"R5: duplicate {kind} {name}")
            continue
        by_kind.setdefault(kind, {})[name] = doc
        pod = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})
        if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
            errors.append(f"R3: {kind}/{name} joins the host namespace")
        for volume in pod.get("volumes") or []:
            if "hostPath" in volume:
                errors.append(f"R3: {kind}/{name} mounts a host path")

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


def self_test(docs: list[dict], values: dict) -> list[str]:
    base = check(docs, values)
    if base:
        return ["R4: rendered chart failed before mutations"] + base

    deployment = _first(docs, "Deployment")
    if deployment is not None:
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["containers"][0].pop(
            "livenessProbe", None
        )
        if not check(broken, values):
            return ["R4: removing livenessProbe did not fail"]
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["restartPolicy"] = "Never"
        if not check(broken, values):
            return ["R4: restartPolicy Never did not fail"]
        broken = copy.deepcopy(docs)
        _first(broken, "Deployment")["spec"]["template"]["spec"]["hostNetwork"] = True
        if not check(broken, values):
            return ["R3: hostNetwork did not fail"]

    stateful_doc = _first(docs, "StatefulSet")
    if stateful_doc is not None:
        broken = copy.deepcopy(docs)
        _first(broken, "StatefulSet")["spec"].pop("volumeClaimTemplates", None)
        if not check(broken, values):
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
        if not check(broken, values):
            return ["R4: removing a PodDisruptionBudget did not fail"]

    stateful = next(
        (name for name, spec in values["services"].items() if "state" in (spec or {})),
        None,
    )
    if stateful is not None:
        broken_values = copy.deepcopy(values)
        broken_values["services"][stateful]["state"] = None
        if not check(docs, broken_values):
            return ["R4: null state did not fail"]

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
    )
    if not any("restartPolicy" in item or "livenessProbe" in item for item in synthetic):
        return ["R4: synthetic deployment did not fail the recovery check"]
    return []


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check_render.py RENDERED.yaml VALUES.yaml", file=sys.stderr)
        return 2
    docs = load_docs(Path(argv[1]).read_text())
    values = yaml.safe_load(Path(argv[2]).read_text())
    errors = self_test(docs, values)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
