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
    ("kubeovn.io", "provider-networks"),
    ("kubeovn.io", "vlans"),
    ("kubeovn.io", "ovn-eips"),
    ("kubeovn.io", "ovn-fips"),
    ("kubeovn.io", "ovn-snat-rules"),
    ("kubeovn.io", "vpc-nat-gateways"),
}


def load_docs(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def check_rbac(name: str, spec: dict, by_kind: dict, pod: dict, release: str, ns: str, nsmap: dict) -> list[str]:
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
            if rule["namespace"] not in nsmap:
                errors.append(f"R14: {name} rule names namespace key {rule['namespace']}, which values do not define")
                continue
            ns_rules.setdefault(nsmap[rule["namespace"]], []).append(
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
        if namespace != ns and namespace not in by_kind.get("Namespace", {}):
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


# apiCredentials: flag -> env kubelet expands it from, and env -> key of the Secret (vws-console-api
# cmd/vws-console-api/main.go -console-token, -access-key, -secret; -backend=grpc refuses the defaults).
API_CREDENTIAL_FLAGS = {"console-token": "VWS_CONSOLE_TOKEN", "access-key": "VWS_ACCESS_KEY", "secret": "VWS_SECRET_KEY"}
API_CREDENTIAL_KEYS = {"VWS_CONSOLE_TOKEN": "console-token", "VWS_ACCESS_KEY": "access-key", "VWS_SECRET_KEY": "secret-key"}
API_CLIENT_ENV = ("VWS_CONSOLE_API_URL", "VWS_CONSOLE_API_TOKEN")


def want_args(spec: dict, values: dict, nsmap: dict) -> tuple[list[str], list[str]]:
    """The args a service must get from values, and the errors found while resolving them."""
    flags = dict(spec.get("flags") or {})
    if (values.get("global") or {}).get("pendingFlags"):
        flags.update(spec.get("pendingFlags") or {})
    args: list[str] = []
    errors: list[str] = []
    rgw = (spec.get("s3") or {}).get("rgw") or {}
    if rgw.get("enabled"):
        flags.update({"object-backend": "s3", "s3-profile": "rgw", "s3-endpoint": rgw.get("endpoint")})
        if rgw.get("bucketPrefix"):
            flags["s3-bucket-prefix"] = rgw["bucketPrefix"]
    if spec.get("apiCredentials"):
        flags.update({f: f"$({env})" for f, env in API_CREDENTIAL_FLAGS.items()})
    for key in sorted(flags):
        value = flags[key]
        if isinstance(value, dict):
            if value.get("namespace") not in nsmap:
                errors.append(f"R14: flag -{key} names namespace key {value.get('namespace')}, which values do not define")
                continue
            value = nsmap[value["namespace"]]
        elif isinstance(value, bool):
            value = str(value).lower()
        args.append(f"-{key}={value}")
    return args, errors


# Flags a service refuses to start without, from its usage text on origin/main.
# network: cmd/vws-network/main.go (-db PATH -pool CIDR).
# storage: starts without -db but keeps state in memory only, so a restart loses it (#85).
REQUIRED_FLAGS = {"network": ["db", "pool"], "storage": ["db"]}


# Resources a service creates with an existing-object check (Create, then Get on AlreadyExists).
# The rule that grants create on them must grant get too, and delete: remove reads the object with
# Get and deletes it under a UID precondition (vws-network #33, dataplane_kubeovn.go removeObject).
# network: internal/network/dataplane_kubeovn.go ensureObject, vws-network #27.
ENSURE_NEEDS_GET = {
    "network": [
        ("kubeovn.io", "vpcs"),
        ("kubeovn.io", "subnets"),
        ("kubeovn.io", "ovn-eips"),
        ("kubeovn.io", "ovn-fips"),
        ("kubeovn.io", "ovn-snat-rules"),
        ("kubeovn.io", "security-groups"),
        ("kubeovn.io", "vpc-nat-gateways"),
        ("k8s.cni.cncf.io", "network-attachment-definitions"),
    ]
}


def check_ensure_get(name: str, spec: dict) -> list[str]:
    errors: list[str] = []
    for group, resource in ENSURE_NEEDS_GET.get(name, []):
        verbs: set[str] = set()
        for rule in spec.get("rbac") or []:
            if group in rule["apiGroups"] and resource in rule["resources"]:
                verbs.update(rule["verbs"])
        if "create" in verbs and "get" not in verbs:
            errors.append(f"R11: {name} creates {group}/{resource} but cannot get it, so ensure fails on an existing object")
        if "create" not in verbs:
            errors.append(f"R11: {name} has no create on {group}/{resource}, which ensure needs")
        if "delete" not in verbs or "get" not in verbs:
            errors.append(f"R11: {name} cannot get and delete {group}/{resource}, which remove needs")
    return errors


def check_listen(name: str, spec: dict, pod: dict, container: dict) -> list[str]:
    """The listen flag, the service port, the container port and every probe name one port."""
    errors: list[str] = []
    port = int(spec.get("port", 8080))
    listen = (spec.get("flags") or {}).get("listen")
    if listen is not None:
        host, _, number = str(listen).rpartition(":")
        if host != "0.0.0.0" or not number.isdigit() or int(number) != port:
            errors.append(f"R15: {name} -listen={listen} must be 0.0.0.0:{port}, the service port")
    ports = [p for p in container.get("ports") or []]
    if [p.get("containerPort") for p in ports] != [port]:
        errors.append(f"R15: {name} containerPort {[p.get('containerPort') for p in ports]} must be only {port}")
    named = {p.get("name"): p.get("containerPort") for p in ports}
    for kind in ("startupProbe", "livenessProbe", "readinessProbe"):
        probe = container.get(kind) or {}
        if "httpGet" in probe or "exec" in probe:
            errors.append(f"R15: {name} {kind} must be tcpSocket or grpc, not HTTP or exec")
            continue
        target = probe.get("tcpSocket") or probe.get("grpc")
        if target is None:
            errors.append(f"R15: {name} has no {kind}")
            continue
        number = named.get(target.get("port"), target.get("port"))
        if number != port:
            errors.append(f"R15: {name} {kind} port {target.get('port')} is not the service port {port}")
    for flag in REQUIRED_FLAGS.get(name, []):
        if flag not in (spec.get("flags") or {}):
            errors.append(f"R15: {name} needs the -{flag} flag to start")
    # grpc.health.v1: liveness and startup ask the empty name (process up), readiness asks the
    # service's aggregate name, so a cluster outage marks it unready instead of restarting it.
    ready = spec.get("grpcReadinessService")
    if ready and "grpc" not in (container.get("readinessProbe") or {}):
        errors.append(f"R15: {name} has grpcReadinessService but no gRPC readiness probe")
    for kind in ("startupProbe", "livenessProbe", "readinessProbe"):
        grpc = (container.get(kind) or {}).get("grpc")
        if grpc is None or not ready:
            continue
        want = ready if kind == "readinessProbe" else None
        if grpc.get("service") != want:
            errors.append(f"R15: {name} {kind} grpc service is {grpc.get('service')!r}, want {want!r}")
    for group, resource in READY_NEEDS_GET.get(name, []) if ready else []:
        rules = [r for r in spec.get("rbac") or [] if "namespace" not in r and group in r["apiGroups"] and resource in r["resources"]]
        if not any("get" in r["verbs"] for r in rules):
            errors.append(f"R11: {name} readiness gets cluster-scoped {group}/{resource} but has no ClusterRole get on it")
        if any(set(r["verbs"]) - {"get"} for r in rules):
            errors.append(f"R11: {name} needs only get on {group}/{resource}")
    return errors


def check_console_api(name: str, spec: dict, values: dict) -> list[str]:
    """console-api talks gRPC to the real services and serves the console on the Service port (#84).

    The image defaults are -backend=fake and 127.0.0.1 listeners (vws-console-api
    cmd/vws-console-api/main.go:41-51), so a missing flag silently serves a fake cloud.
    """
    if name != "console-api":
        return []
    errors: list[str] = []
    flags = spec.get("flags") or {}
    if flags.get("backend") != "grpc":
        errors.append(f"R15: console-api -backend={flags.get('backend')} must be grpc")
    port = int(spec.get("port", 8080))
    if flags.get("console-addr") != f"0.0.0.0:{port}":
        errors.append(f"R15: console-api -console-addr={flags.get('console-addr')} must be 0.0.0.0:{port}, the service port")
    for target in ("compute", "storage", "network"):
        want = f"{target}:{int((values['services'].get(target) or {}).get('port', 8080))}"
        if flags.get(f"{target}-addr") != want:
            errors.append(f"R15: console-api -{target}-addr={flags.get(f'{target}-addr')} must be {want}")
    return errors


# Cluster-scoped objects a service's readiness reads (vws-storage#39 internal/backend/kube/ready.go:20,29,33).
READY_NEEDS_GET = {
    "storage": [
        ("", "namespaces"),
        ("storage.k8s.io", "storageclasses"),
        ("snapshot.storage.k8s.io", "volumesnapshotclasses"),
    ]
}


def check_kubeconfig(name: str, spec: dict, pod: dict, container: dict) -> list[str]:
    """kubeconfigSecret mounts one operator-made Secret read-only next to the -kubeconfig path."""
    errors: list[str] = []
    mounts = [m for m in container.get("volumeMounts") or [] if m.get("name") == "kubeconfig"]
    volumes = [v for v in pod.get("volumes") or [] if v.get("name") == "kubeconfig"]
    secret = spec.get("kubeconfigSecret")
    if secret is None:
        if mounts or volumes:
            errors.append(f"R16: {name} mounts a kubeconfig volume that values do not ask for")
        return errors
    path = (spec.get("flags") or {}).get("kubeconfig")
    if not path:
        return [f"R16: {name} has kubeconfigSecret but no -kubeconfig flag"]
    directory, _, key = path.rpartition("/")
    if len(mounts) != 1 or mounts[0].get("mountPath") != directory or mounts[0].get("readOnly") is not True:
        errors.append(f"R16: {name} must mount the kubeconfig Secret read-only at {directory}")
    if directory in ("/tmp", "/var/lib/vws") or directory.startswith(("/tmp/", "/var/lib/vws/")):
        errors.append(f"R16: {name} kubeconfig directory {directory} overlaps another mount")
    if len(volumes) != 1 or (volumes[0].get("secret") or {}).get("secretName") != secret:
        errors.append(f"R16: {name} kubeconfig volume must be Secret {secret}")
    else:
        vol = volumes[0]["secret"]
        if [i.get("key") for i in vol.get("items") or []] != [key]:
            errors.append(f"R16: {name} must project only the key {key} of the Secret")
        if vol.get("defaultMode") != 0o440:
            errors.append(f"R16: {name} kubeconfig file mode must be 0440")
    for env in container.get("env") or []:
        if "KUBECONFIG" in env.get("name", "").upper() or ("TOKEN" in env.get("name", "").upper() and "secretKeyRef" not in (env.get("valueFrom") or {})):
            errors.append(f"R16: {name} passes a credential through env {env.get('name')}")
    return errors


S3_KEYS = {"VWS_S3_ACCESS_KEY": "access-key", "VWS_S3_SECRET_KEY": "secret-key"}


def check_s3(name: str, spec: dict, container: dict) -> list[str]:
    """s3.rgw: an endpoint and a Secret name, and the keys only through secretKeyRef of that Secret (#90)."""
    errors: list[str] = []
    rgw = (spec.get("s3") or {}).get("rgw") or {}
    env = {e.get("name"): e for e in container.get("env") or []}
    for key, value in env.items():
        upper = (key or "").upper()
        if ("ACCESS_KEY" in upper or "SECRET" in upper) and "value" in value:
            errors.append(f"R16: {name} passes a credential through env value {key}")
    if not rgw.get("enabled"):
        if set(S3_KEYS) & set(env):
            errors.append(f"R16: {name} gets S3 keys that values do not ask for")
        return errors
    if not rgw.get("endpoint") or not rgw.get("existingSecret"):
        errors.append(f"R16: {name} enables s3.rgw with an empty endpoint or existingSecret")
    for key, secret_key in S3_KEYS.items():
        ref = ((env.get(key) or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
        if ref != {"name": rgw.get("existingSecret"), "key": secret_key}:
            errors.append(f"R16: {name} must read {key} from Secret {rgw.get('existingSecret')} key {secret_key}")
    return errors


def entry_of(env: list[dict], key: str) -> dict:
    return next(e for e in env if e["name"] == key)


def check_api(name: str, spec: dict, container: dict, values: dict, release_ns: str) -> list[str]:
    """apiCredentials and apiClient: credentials only by secretKeyRef of one Secret, the URL from values."""
    errors: list[str] = []
    env = {e.get("name"): e for e in container.get("env") or []}

    def ref(key: str) -> dict:
        return ((env.get(key) or {}).get("valueFrom") or {}).get("secretKeyRef") or {}

    secret = (spec.get("apiCredentials") or {}).get("existingSecret")
    if spec.get("apiCredentials") is None:
        if set(API_CREDENTIAL_KEYS) & set(env):
            errors.append(f"R16: {name} gets console API credentials that values do not ask for")
    else:
        if not secret:
            errors.append(f"R16: {name} apiCredentials has an empty existingSecret")
        for key, secret_key in API_CREDENTIAL_KEYS.items():
            if "value" in (env.get(key) or {}) or ref(key) != {"name": secret, "key": secret_key}:
                errors.append(f"R16: {name} must read {key} from Secret {secret} key {secret_key}")
    client = spec.get("apiClient")
    if client is None:
        if set(API_CLIENT_ENV) & set(env):
            errors.append(f"R16: {name} gets console API wiring that values do not ask for")
        return errors
    target = values["services"].get(client.get("service")) or {}
    target_secret = (target.get("apiCredentials") or {}).get("existingSecret")
    want_url = f"http://{client.get('service')}.{release_ns}.svc:{int(target.get('port', 8080))}"
    if (env.get("VWS_CONSOLE_API_URL") or {}).get("value") != want_url:
        errors.append(f"R16: {name} VWS_CONSOLE_API_URL must be {want_url}")
    if not target_secret or "value" in (env.get("VWS_CONSOLE_API_TOKEN") or {}) or ref("VWS_CONSOLE_API_TOKEN") != {"name": target_secret, "key": "console-token"}:
        errors.append(f"R16: {name} must read VWS_CONSOLE_API_TOKEN from Secret {target_secret} key console-token")
    return errors


def check_flags(name: str, spec: dict, container: dict, values: dict, nsmap: dict) -> list[str]:
    want, errors = want_args(spec, values, nsmap)
    got = container.get("args") or []
    if got != want:
        errors.append(f"R14: {name} args {got} do not match the flags in values {want}")
    roles = {
        nsmap[rule["namespace"]]
        for rule in spec.get("rbac") or []
        if rule.get("namespace") in nsmap
    }
    for key, value in (spec.get("flags") or {}).items():
        if isinstance(value, dict) and nsmap.get(value.get("namespace")) not in roles:
            errors.append(f"R14: {name} gets a namespace through -{key} but has no Role there")
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

    for doc in by_kind.get("Namespace", {}).values():
        if (doc["metadata"].get("annotations") or {}).get("helm.sh/resource-policy") != "keep":
            errors.append(f"R14: Namespace {doc['metadata']['name']} must be kept on uninstall")
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
        release_ns = workload["metadata"].get("namespace")
        nsmap = {**(values.get("namespaces") or {}), "release": release_ns}
        errors.extend(check_rbac(name, spec, by_kind, pod, release, release_ns, nsmap))
        errors.extend(check_flags(name, spec, container, values, nsmap))
        errors.extend(check_ensure_get(name, spec))
        errors.extend(check_listen(name, spec, pod, container))
        errors.extend(check_console_api(name, spec, values))
        errors.extend(check_kubeconfig(name, spec, pod, container))
        errors.extend(check_s3(name, spec, container))
        errors.extend(check_api(name, spec, container, values, release_ns))
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
        kept = {d["metadata"]["name"] for d in docs if d.get("kind") == "Namespace"}
        role = next(
            doc
            for doc in docs
            if doc.get("kind") == "Role"
            and doc["metadata"]["name"] == f"{release}-{with_ns}"
            and doc["metadata"]["namespace"] in kept
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

    # console-api keeps the gRPC backend, the open console address and the real service ports (#84).
    if (values["services"].get("console-api") or {}).get("flags"):
        for key, value in (("backend", "fake"), ("console-addr", "127.0.0.1:8080"), ("compute-addr", "compute:1"), ("network-addr", None)):
            broken_values = copy.deepcopy(values)
            flags = broken_values["services"]["console-api"]["flags"]
            if value is None:
                flags.pop(key)
            else:
                flags[key] = value
            if not any("R15: console-api" in err for err in check(docs, broken_values, release)):
                return [f"R15: console-api -{key}={value} did not fail"]

    # Flags come from values, and a namespace flag needs a Role in that namespace.
    with_flags = next(
        (n for n, spec in values["services"].items() if (spec or {}).get("flags")), None
    )
    if with_flags is not None:
        for index in range(3):
            broken = copy.deepcopy(docs)
            for doc in broken:
                if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == with_flags:
                    args = doc["spec"]["template"]["spec"]["containers"][0].setdefault("args", [])
                    if index == 0 and args:
                        args.pop()
                    elif index == 1:
                        args.append("-surprise=1")
                    elif index == 2 and args:
                        args[0] = args[0].split("=")[0] + "=changed"
            if not check(broken, values, release):
                return [f"R14: changing the args of {with_flags} did not fail (case {index})"]
        ns_flag = next(
            (
                (n, key)
                for n, spec in values["services"].items()
                for key, value in ((spec or {}).get("flags") or {}).items()
                if isinstance(value, dict)
            ),
            None,
        )
        if ns_flag is not None:
            broken_values = copy.deepcopy(values)
            broken_values["services"][ns_flag[0]]["flags"][ns_flag[1]] = {"namespace": "nowhere"}
            if not check(docs, broken_values, release):
                return ["R14: a flag naming an undefined namespace did not fail"]
            broken_values = copy.deepcopy(values)
            has_role = {r.get("namespace") for r in values["services"][ns_flag[0]].get("rbac") or []}
            others = [k for k in values.get("namespaces") or {} if k not in has_role]
            if others:
                broken_values["services"][ns_flag[0]]["flags"][ns_flag[1]] = {"namespace": others[0]}
                if not any("no Role there" in err for err in check(docs, broken_values, release)):
                    return ["R14: a namespace flag without a Role in that namespace did not fail"]
    # One port: -listen, containerPort and the probes. Probes are tcpSocket or grpc, never HTTP.
    target = next(
        (n for n, spec in values["services"].items() if (spec or {}).get("flags", {}).get("listen")), None
    )
    if target is not None:
        def mutate(change):
            broken = copy.deepcopy(docs)
            for doc in broken:
                if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == target:
                    change(doc["spec"]["template"]["spec"]["containers"][0])
            return broken

        def http(c):
            c["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": "http"}}

        def probe_port(c):
            (c["livenessProbe"].get("tcpSocket") or c["livenessProbe"]["grpc"])["port"] = 9

        def container_port(c):
            c["ports"][0]["containerPort"] = 9999

        for label, change in (("an HTTP probe", http), ("a probe on another port", probe_port),
                              ("a containerPort that differs", container_port)):
            if not check(mutate(change), values, release):
                return [f"R15: {label} did not fail"]
        broken_values = copy.deepcopy(values)
        broken_values["services"][target]["flags"]["listen"] = "127.0.0.1:%d" % int(values["services"][target].get("port", 8080))
        if not check(docs, broken_values, release):
            return ["R15: a -listen on 127.0.0.1 did not fail"]
    # gRPC health names and the readiness ClusterRole (vws-storage#39), when rendered.
    for hname, hspec in values["services"].items():
        ready = (hspec or {}).get("grpcReadinessService")
        if not ready:
            continue
        rendered = [d for d in docs if d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] == hname]
        if not rendered or "grpc" not in rendered[0]["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]:
            continue

        def hmutate(change, hname=hname):
            broken = copy.deepcopy(docs)
            for doc in broken:
                if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == hname:
                    change(doc["spec"]["template"]["spec"]["containers"][0])
            return broken

        def no_ready_name(c):
            c["readinessProbe"]["grpc"].pop("service", None)

        def live_name(c, ready=ready):
            c["livenessProbe"]["grpc"]["service"] = ready

        def tcp_ready(c):
            c["readinessProbe"].pop("grpc")
            c["readinessProbe"]["tcpSocket"] = {"port": "http"}

        for label, change in (("readiness without the Ready service name", no_ready_name),
                              ("liveness asking the Ready service name", live_name),
                              ("a tcpSocket readiness for a gRPC service (#110)", tcp_ready)):
            if not check(hmutate(change), values, release):
                return [f"R15: {label} did not fail"]
        for group, resource in READY_NEEDS_GET.get(hname, []):
            for how in ("drop", "list"):
                broken_values = copy.deepcopy(values)
                for rule in broken_values["services"][hname]["rbac"]:
                    if "namespace" not in rule and group in rule["apiGroups"] and resource in rule["resources"]:
                        rule["verbs"] = ["list"] if how == "drop" else ["get", "list"]
                if not check(docs, broken_values, release):
                    return [f"R11: {hname} readiness rule on {resource} ({how}) did not fail"]
        broken_values = copy.deepcopy(values)
        broken_values["services"][target]["flags"]["listen"] = "0.0.0.0:9"
        if not check(docs, broken_values, release):
            return ["R15: a -listen port that differs from the service port did not fail"]
    if "network" in values["services"]:
        for rule in values["services"]["network"].get("rbac") or []:
            if "get" in rule["verbs"] and "create" in rule["verbs"] and rule["resources"]:
                broken_values = copy.deepcopy(values)
                for broken_rule in broken_values["services"]["network"]["rbac"]:
                    if broken_rule["resources"] == rule["resources"] and broken_rule.get("namespace") == rule.get("namespace"):
                        broken_rule["verbs"] = [v for v in broken_rule["verbs"] if v != "get"]
                if not check(docs, broken_values, release):
                    return [f"R11: dropping get from {rule['resources']} did not fail"]
            if "delete" in rule["verbs"] and rule["resources"]:
                broken_values = copy.deepcopy(values)
                for broken_rule in broken_values["services"]["network"]["rbac"]:
                    if broken_rule["resources"] == rule["resources"] and broken_rule.get("namespace") == rule.get("namespace"):
                        broken_rule["verbs"] = [v for v in broken_rule["verbs"] if v != "delete"]
                if not check(docs, broken_values, release):
                    return [f"R11: dropping delete from {rule['resources']} did not fail"]
        broken_values = copy.deepcopy(values)
        broken_values["services"]["network"]["flags"].pop("pool", None)
        if not check(docs, broken_values, release):
            return ["R15: network without -pool did not fail"]
    if "storage" in values["services"]:
        broken_values = copy.deepcopy(values)
        broken_values["services"]["storage"]["flags"].pop("db", None)
        if not check(docs, broken_values, release):
            return ["R15: storage without -db did not fail"]

    # kubeconfigSecret: the mount, the Secret name, the key and the mode come from values only.
    kube = next((n for n, spec in values["services"].items() if (spec or {}).get("kubeconfigSecret")), None)
    if kube is not None:
        def mutate_pod(change):
            broken = copy.deepcopy(docs)
            for doc in broken:
                if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == kube:
                    change(doc["spec"]["template"]["spec"])
            return broken

        def volume(pod):
            return next(v for v in pod["volumes"] if v["name"] == "kubeconfig")

        def mount(pod):
            return next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "kubeconfig")

        cases = {
            "another Secret": lambda pod: volume(pod)["secret"].update(secretName="other"),
            "a writable mount": lambda pod: mount(pod).pop("readOnly"),
            "a mount path that differs from the flag": lambda pod: mount(pod).update(mountPath="/etc/other"),
            "a world-readable mode": lambda pod: volume(pod)["secret"].update(defaultMode=0o644),
            "the whole Secret projected": lambda pod: volume(pod)["secret"].pop("items"),
            "a credential in env": lambda pod: pod["containers"][0]["env"].append({"name": "KUBECONFIG", "value": "x"}),
            "no mount": lambda pod: pod["containers"][0]["volumeMounts"].remove(mount(pod)),
        }
        for label, change in cases.items():
            if not check(mutate_pod(change), values, release):
                return [f"R16: {label} did not fail"]
        broken_values = copy.deepcopy(values)
        broken_values["services"][kube]["kubeconfigSecret"] = None
        broken_values["services"][kube].pop("kubeconfigSecret")
        if not check(docs, broken_values, release):
            return ["R16: a kubeconfig volume that values do not ask for did not fail"]

    # s3.rgw: the keys come only from secretKeyRef, and enabled needs every value.
    rgw_name = next((n for n, s in values["services"].items() if (((s or {}).get("s3") or {}).get("rgw") or {}).get("enabled")), None)
    if rgw_name is not None:
        def mutate_env(change):
            broken = copy.deepcopy(docs)
            for doc in broken:
                if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == rgw_name:
                    change(doc["spec"]["template"]["spec"]["containers"][0]["env"])
            return broken

        def entry(env, key):
            return next(e for e in env if e["name"] == key)

        cases = {
            "a key as a literal value": lambda env: entry(env, "VWS_S3_SECRET_KEY").update(value="x"),
            "another Secret": lambda env: entry(env, "VWS_S3_ACCESS_KEY")["valueFrom"]["secretKeyRef"].update(name="other"),
            "no secret key env": lambda env: env.remove(entry(env, "VWS_S3_SECRET_KEY")),
        }
        for label, change in cases.items():
            if not check(mutate_env(change), values, release):
                return [f"R16: s3.rgw {label} did not fail"]
        for field in ("endpoint", "existingSecret"):
            broken_values = copy.deepcopy(values)
            broken_values["services"][rgw_name]["s3"]["rgw"][field] = ""
            if not check(docs, broken_values, release):
                return [f"R16: s3.rgw with an empty {field} did not fail"]

    # apiCredentials / apiClient: credentials only by secretKeyRef, the URL from values.
    api_cases = []
    for svc, spec in values["services"].items():
        spec = spec or {}
        if spec.get("apiCredentials"):
            api_cases += [
                (svc, "a token as a literal value", lambda env: entry_of(env, "VWS_CONSOLE_TOKEN").update(value="x")),
                (svc, "another credential Secret", lambda env: entry_of(env, "VWS_SECRET_KEY")["valueFrom"]["secretKeyRef"].update(name="other")),
                (svc, "no access key env", lambda env: env.remove(entry_of(env, "VWS_ACCESS_KEY"))),
            ]
        if spec.get("apiClient"):
            api_cases += [
                (svc, "a hardcoded API URL", lambda env: entry_of(env, "VWS_CONSOLE_API_URL").update(value="http://console-api:9999")),
                (svc, "an API token from another key", lambda env: entry_of(env, "VWS_CONSOLE_API_TOKEN")["valueFrom"]["secretKeyRef"].update(key="secret-key")),
            ]
    for svc, label, change in api_cases:
        broken = copy.deepcopy(docs)
        for doc in broken:
            if doc.get("kind") in ("Deployment", "StatefulSet") and doc["metadata"]["name"] == svc:
                change(doc["spec"]["template"]["spec"]["containers"][0]["env"])
        if not check(broken, values, release):
            return [f"R16: {svc} {label} did not fail"]

    broken = copy.deepcopy(docs)
    for doc in broken:
        if doc.get("kind") == "Namespace":
            doc["metadata"]["annotations"] = {}
            break
    if any(d.get("kind") == "Namespace" for d in docs) and not check(broken, values, release):
        return ["R14: a Namespace that is not kept did not fail"]

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


def deep_merge(base: dict, over: dict) -> dict:
    """Helm's value merge: maps merge key by key, everything else is replaced."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("usage: check_render.py RENDERED.yaml VALUES.yaml RELEASE [OVERRIDE.yaml...]", file=sys.stderr)
        return 2
    docs = load_docs(Path(argv[1]).read_text())
    values = yaml.safe_load(Path(argv[2]).read_text())
    for override in argv[4:]:
        values = deep_merge(values, yaml.safe_load(Path(override).read_text()))
    errors = self_test(docs, values, argv[3])
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
