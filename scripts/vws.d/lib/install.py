#!/usr/bin/env python3
"""Install operator-supplied lab packages on one host by role; all payloads travel on SSH stdin."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import globalconfig
import yaml

# The global host runs etcd and global, and may also run region instances. Any other host runs only region
# instances. A region instance serves one Proxmox cluster; there is no VIP and no keepalived.
GLOBAL_ROLES = ({"etcd", "global"}, {"etcd", "global", "region"})
REGION_ROLES = ({"region"},)
PRIVATE = (
    "ca.crt member.crt member.key client.crt client.key admin-kek console-token "
    "vws-access-key vws-secret-key api-etcd-key.pem api-mtls-key.pem aws-source "
    "iam-etcd-key.pem iam-key.pem"
).split()
PUBLIC = "etcd-ca.pem api-etcd.pem internal-ca.pem api-mtls.pem iam-etcd.pem iam.pem".split()
SCHEMA = {"profile": str, "hosts": [{"name": str, "address": str, "ssh_user": str, "roles": [str],
                                   "regions": [{"name": str, "port": str, "ops_port": str}]}],
          "memory_mib": {"global": str, "region": str, "etcd": str}}
# The fields of the region daemon's region.yaml (vws-console-api internal/backend/proxmox/region.go
# RegionConfig), which refuses any other key, a repeated key and a second document.
REGION_SCHEMA = {
    "read_only": str, "sdn_zone": str, "storages": {str: str}, "templates": {"pool": str, "tag": str},
    "instance_types": {str: {"cores": str, "memory_mib": str}},
    "subnets": {str: {"cidr": str, "gateway": str, "ip_range": str, "mode": str}},
    "default_bridge": str, "default_project": str, "pools": {str: str},
    "scheduler": {"policy": str, "host_reserve_mib": str, "mem_overcommit": str, "cpu_overcommit": str},
    "linked_clones": str, "live_tests": str, "shutdown_timeout": str,
}
LAB_MEMORY_MIB = {"global": 1024, "region": 512, "etcd": 512}
LAB_SERVICES = {
    "global": ("vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console"),
    "etcd": ("vws-etcd", "vws-etcd-snapshot", "vws-etcd-defrag"),
}
# The unit validates the instance name the same way (packaging/vws-region/run-region).
REGION_NAME = r"pve-[a-z0-9]+(-[a-z0-9]+)*-[0-9]+"
# Ports of the global host's own listeners (etcd, console, console-api, vws-iam and its health port).
RESERVED_PORTS = {2379, 2380, 3000, 8080, 8081, 8082, 8083, 8084, 9095, 9096}
PACKAGES = {
    "vws-etcd": ("etcd", ["vws-etcd", "vws-etcd-snapshot.timer", "vws-etcd-defrag.timer"],
                 [("etcd", "CREDENTIALS_DIRECTORY=/run/credentials/vws-etcd.service /usr/lib/vws/etcd/check-ready")]),
    "vws-global": ("global", ["vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console"],
                   [(p, "/usr/lib/vws/check-global-ready " + p) for p in ("iam", "proxy", "api", "console")]),
    # The instances are template units, so the installer starts and probes them itself.
    "vws-region": ("region", [], []),
}
# The etcd probe asks once; a unit that was just started gets 30 seconds to answer.
RETRY = "n=0; until {}; do n=$((n + 1)); test $n -lt 15 || exit 1; sleep 2; done"
PACKAGING = Path(__file__).resolve().parents[3] / "packaging"


def owner(unit, port):
    """A shell test that the main process of unit holds every listener that 127.0.0.1:port reaches.

    It reads /proc/net/tcp{,6} and the process's /proc/<pid>/fd links, with no package beyond the base
    system: the IPv4 loopback and wildcard listeners and the IPv6 wildcard and mapped-loopback ones.
    """
    x = "%04X" % port
    addresses = "|".join("0A:" + a + ":" + x for a in (
        "0100007F", "00000000", "0" * 32, "0000000000000000FFFF00000100007F"))
    return (f'p=$(systemctl show -p MainPID --value {unit}) && test "${{p:-0}}" -gt 0 && '
            "l=$(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | while read -r _ a _ st _ _ _ _ _ i _; do "
            f'case $st:$a in {addresses}) echo "socket:[$i]" ;; esac; done) && test -n "$l" && '
            'test "$(for f in /proc/$p/fd/*; do readlink "$f"; done 2>/dev/null | grep -xF "$l" | sort -u | wc -l)" '
            '-eq "$(printf \'%s\\n\' "$l" | wc -l)"')
# Generated TLS sets: state directory, vws-etcd-tls member, and credential name -> file below the state
# directory ({} is the member). The internal set names its one member iam, on the loopback IAM listener.
TLS = (
    ("etcd-tls", None, {
        "ca.crt": "ca.crt", "etcd-ca.pem": "ca.crt", "member.crt": "{}/member.crt",
        "member.key": "{}/member.key", "client.crt": "client/client.crt",
        "client.key": "client/client.key", "api-etcd.pem": "client/client.crt",
        "api-etcd-key.pem": "client/client.key", "iam-etcd.pem": "client/client.crt",
        "iam-etcd-key.pem": "client/client.key"}),
    ("internal-tls", "iam=127.0.0.1", {
        "internal-ca.pem": "ca.crt", "iam.pem": "{}/member.crt", "iam-key.pem": "{}/member.key",
        "api-mtls.pem": "client/client.crt", "api-mtls-key.pem": "client/client.key"}),
)
# Certificate and key that must be one pair.
PAIRS = (("member.crt", "member.key"), ("client.crt", "client.key"), ("api-etcd.pem", "api-etcd-key.pem"),
         ("iam-etcd.pem", "iam-etcd-key.pem"), ("iam.pem", "iam-key.pem"),
         ("api-mtls.pem", "api-mtls-key.pem"))
PENDING = "/var/lib/vws-install/pending"
DIRTY = ("test ! -L /var/lib/vws-install; install -d -m 700 -o root -g root /var/lib/vws-install; "
         "test ! -L " + PENDING + "; touch " + PENDING)


class Invalid(Exception):
    pass


class Failed(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Invalid("invalid arguments; see docs/vws-cli.md")


def local(args, data=None):
    return subprocess.run(args, input=data if data is not None else b"",
                          capture_output=True, timeout=180)


def read_file(path):
    if path.is_symlink() or not path.is_file():
        raise Invalid("required regular file: " + path.name)
    return path.read_bytes()


def inventory(path):
    data = read_file(path)
    if len(data) > globalconfig.MAX_BYTES:
        raise Invalid("inventory exceeds 64 KiB")
    try:
        nodes = list(yaml.compose_all(data.decode(), Loader=yaml.SafeLoader))
        if len(nodes) != 1 or not isinstance(nodes[0], yaml.MappingNode):
            raise Invalid("inventory requires one document")
        # Inspect sequences too: the global config schema has no nested host lists.
        def secrets(node):
            if isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    if not isinstance(key, yaml.ScalarNode) or globalconfig.secret_key(key.value):
                        raise Invalid("inventory contains a forbidden key")
                    secrets(value)
            elif isinstance(node, yaml.SequenceNode):
                for item in node.value:
                    secrets(item)
        secrets(nodes[0])
        cfg = globalconfig.decode(nodes[0], SCHEMA)
        if cfg.get("profile") != "lab" or not cfg.get("hosts"):
            raise Invalid("lab inventory requires one global host")
        memory = cfg.get("memory_mib", LAB_MEMORY_MIB)
        if (set(memory) != set(LAB_MEMORY_MIB)
                or any(not re.fullmatch(r"[1-9][0-9]{0,3}", str(v)) for v in memory.values())
                or sum(int(v) for v in memory.values()) > 2048):
            raise Invalid("lab memory_mib requires positive global, region and etcd budgets totaling <= 2048 MiB")
        cfg["memory_mib"] = {k: int(v) for k, v in memory.items()}
        hosts(cfg)
        return cfg
    except (yaml.YAMLError, UnicodeError, globalconfig.Refused, KeyError, ValueError, TypeError, RecursionError):
        raise Invalid("invalid inventory") from None


def hosts(cfg):
    """Check the hosts and their region instances; every violation exits before any SSH call."""
    seen = {"names": set(), "addresses": set(), "regions": set()}
    owners = 0
    for host in cfg["hosts"]:
        roles = host.get("roles", [])
        if (len(roles) != len(set(roles)) or (set(roles) not in GLOBAL_ROLES + REGION_ROLES)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host.get("name", ""))
                or host.get("name") == "client"
                or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", host.get("ssh_user", "root"))):
            raise Invalid("invalid lab host")
        address = str(ipaddress.IPv4Address(host["address"]))
        if host["name"] in seen["names"] or address in seen["addresses"]:
            raise Invalid("host name and address must be unique")
        seen["names"].add(host["name"])
        seen["addresses"].add(address)
        owners += "global" in roles
        regions = host.get("regions", [])
        if bool(regions) != ("region" in roles):
            raise Invalid("host " + host["name"] + ": the region role and regions go together")
        ports = set(RESERVED_PORTS) if "global" in roles else set()
        for region in regions:
            name = region.get("name", "")
            if not re.fullmatch(REGION_NAME, name) or len(name) > 63:
                raise Invalid("invalid region name; use pve-<name>-<number>")
            if name in seen["regions"]:
                raise Invalid("region name " + name + " appears twice")
            seen["regions"].add(name)
            if not re.fullmatch(r"[1-9][0-9]{3,4}", region.get("port", "")):
                raise Invalid("region " + name + ": ports must be 1024 to 65535")
            port = int(region["port"])
            ops = region.get("ops_port") or str(port + 1000)
            if not re.fullmatch(r"[1-9][0-9]{3,4}", ops):
                raise Invalid("region " + name + ": ports must be 1024 to 65535")
            for value in (port, int(ops)):
                if not 1024 <= value <= 65535:
                    raise Invalid("region " + name + ": ports must be 1024 to 65535")
            if port == int(ops) or {port, int(ops)} & ports:
                raise Invalid("region " + name + ": port is used by another instance or service on host "
                              + host["name"])
            ports |= {port, int(ops)}
            region["port"], region["ops_port"] = port, int(ops)
    if owners != 1:
        raise Invalid("lab inventory requires one global host")


def generate(source, host, roles):
    """Create the secrets of these roles that are absent; an existing path is never rewritten."""
    def absent(name):
        return not (source / name).exists() and not (source / name).is_symlink()

    def put(name, data):
        # Link a complete 0600 file into place: a crash leaves no partial secret, a race no overwrite.
        if not absent(name):
            return
        fd, temporary = tempfile.mkstemp(dir=source, prefix=".vws-install.")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.link(temporary, source / name)
        except FileExistsError:
            pass
        finally:
            os.unlink(temporary)

    def key():
        return base64.b64encode(os.urandom(32)) + b"\n"

    os.umask(0o077)
    source.mkdir(exist_ok=True)
    put("admin-kek", key())
    if absent("iam-kek"):
        staged = Path(tempfile.mkdtemp(dir=source, prefix=".vws-install."))
        (staged / "active").write_text("k1\n")
        (staged / "k1").write_bytes(key())
        staged.rename(source / "iam-kek")
    for state, members, names in TLS:
        members = members or host["name"] + "=" + host["address"]
        member = members.split("=")[0]
        kept = source / state / "ca.key"
        present = [name for name in names if not absent(name)]
        if not present or (kept.is_file() and len(present) < len(names)):
            # Resume from the state directory only when every file already there is that state's own.
            stray = foreign(source, state, names, member, present)
            if stray:
                raise Invalid(stray + " differs from " + state + "/; replace the whole " + state
                              + " set or delete its files and " + state + "/")
            if local(["bash", str(PACKAGING / "vws-etcd/vws-etcd-tls"), str(source / state), members]).returncode:
                raise Invalid("cannot generate " + state + "; openssl and flock are required")
            for name, path in names.items():
                put(name, (source / state / path.format(member)).read_bytes())
    for cert, private in PAIRS:
        if not absent(cert) and not absent(private):
            if public(["x509", "-noout", "-pubkey", "-in"], source / cert) != public(["pkey", "-pubout", "-in"], source / private):
                raise Invalid(cert + " and " + private + " are not one key pair")
    certificate = source / "member.crt"
    if certificate.is_file():
        for address in (host["address"], "127.0.0.1"):
            shown = local(["openssl", "x509", "-noout", "-checkip", address, "-in", str(certificate)])
            if shown.returncode or b"does match" not in shown.stdout or b"NOT" in shown.stdout:
                kept = source / "etcd-tls"
                etcd = TLS[0][2]
                left = [name for name in etcd if name not in ("member.crt", "member.key")
                        and (source / name).exists()]
                same_ca = ((kept / "ca.key").is_file() and (source / "ca.crt").is_file()
                           and (kept / "ca.crt").is_file()
                           and (source / "ca.crt").read_bytes() == (kept / "ca.crt").read_bytes())
                if same_ca and not foreign(source, "etcd-tls", etcd, host["name"], left):
                    fix = ("To reissue it from the same CA, delete member.crt, member.key and etcd-tls/"
                           + host["name"] + "/, then run again")
                elif same_ca:
                    fix = ("Deleting files will not work because the credentials you supplied differ from etcd-tls/. "
                           "Reissue the member pair under the same CA with " + host["address"] + " and 127.0.0.1 "
                           "in its subject alternative names, then replace member.crt and member.key, "
                           "keeping your separate client credentials")
                else:
                    fix = ("Reissue it under your own CA with " + host["address"] + " and 127.0.0.1 "
                           "in its subject alternative names, then replace member.crt and member.key together")
                raise Invalid("member.crt does not name " + address + ". " + fix)


def resolve_merges(node):
    """A copy of node with YAML merge keys (<<) resolved as the daemon's YAML parser does: a key the mapping
    names itself wins over a merged one, and an earlier merged mapping over a later one. A key repeated in one
    mapping stays repeated, so the schema check refuses it; an unknown merged key reaches the schema check."""
    if isinstance(node, yaml.SequenceNode):
        return yaml.SequenceNode(node.tag, [resolve_merges(item) for item in node.value],
                                 node.start_mark, node.end_mark)
    if not isinstance(node, yaml.MappingNode):
        return node
    pairs, merged = [], []
    # The daemon refuses a second << in one mapping as a repeated key; one << may list several mappings.
    if sum(isinstance(k, yaml.ScalarNode) and k.tag == "tag:yaml.org,2002:merge" for k, _ in node.value) > 1:
        raise ValueError
    for key, value in node.value:
        if isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:merge":
            for source in value.value if isinstance(value, yaml.SequenceNode) else [value]:
                source = resolve_merges(source)
                if not isinstance(source, yaml.MappingNode) or not all(
                        isinstance(k, yaml.ScalarNode) for k, _ in source.value):
                    raise ValueError
                keys = [k.value for k, _ in source.value]
                if len(keys) != len(set(keys)):
                    raise ValueError
                merged.extend(source.value)
        else:
            pairs.append((key, resolve_merges(value)))
    names = {key.value for key, _ in pairs if isinstance(key, yaml.ScalarNode)}
    for key, value in merged:
        if key.value not in names:
            names.add(key.value)
            pairs.append((key, value))
    return yaml.MappingNode(node.tag, pairs, node.start_mark, node.end_mark)


def credential_field(node, schema):
    """Whether a field name of the schema's mappings, not a free name such as a pool, looks like a secret."""
    if not isinstance(node, yaml.MappingNode) or not isinstance(schema, dict):
        return False
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            continue
        if key.value not in schema and str not in schema:
            if globalconfig.secret_key(key.value):
                return True
        elif credential_field(value, schema.get(key.value, schema.get(str))):
            return True
    return False


def foreign(source, state, names, member, present):
    """The first present credential that is not byte for byte the retained state's own file, if any."""
    for name in present:
        theirs = source / state / names[name].format(member)
        if not theirs.is_file() or read_file(source / name) != theirs.read_bytes():
            return name
    return None


def public(command, path):
    result = local(["openssl", *command, str(path)])
    if result.returncode or not result.stdout:
        raise Invalid("cannot read " + path.name + " with openssl")
    return result.stdout


def names_address(certificate, address, name):
    """Whether the certificate names the host address, 127.0.0.1 and the instance name."""
    for check, value in (("-checkip", address), ("-checkip", "127.0.0.1"), ("-checkhost", name)):
        shown = local(["openssl", "x509", "-noout", check, value, "-in", str(certificate)])
        if shown.returncode or b"does match" not in shown.stdout or b"NOT" in shown.stdout:
            return False
    # OpenSSL -checkhost falls back to the Common Name when no DNS name is listed; Go TLS never does.
    shown = local(["openssl", "x509", "-noout", "-ext", "subjectAltName", "-in", str(certificate)])
    names = shown.stdout.decode(errors="replace").splitlines()[1:]
    # DNS names compare without case, as Go's hostname check does.
    listed = (n.strip().lower() for line in names for n in line.split(","))
    return not shown.returncode and "dns:" + name.lower() in listed


def server_only(certificate):
    """Whether the leaf is a non-CA certificate whose only extended key usage is serverAuth."""
    for extension, title, only in (("extendedKeyUsage", "X509v3 Extended Key Usage", "TLS Web Server Authentication"),
                                   ("basicConstraints", "X509v3 Basic Constraints", "CA:FALSE")):
        shown = local(["openssl", "x509", "-noout", "-ext", extension, "-in", str(certificate)])
        lines = [line.strip() for line in shown.stdout.decode(errors="replace").splitlines()]
        if shown.returncode or len(lines) != 2 or not lines[0].startswith(title) or lines[1] != only:
            return False
    return True


def issue_region_certificate(source, host, name, folder):
    """Issue a server certificate for one instance from the retained internal CA, all or nothing."""
    state = source / "internal-tls"
    if not (state / "ca.key").is_file() or read_file(state / "ca.crt") != read_file(source / "internal-ca.pem"):
        raise Invalid("regions/" + name + "/ is missing and internal-tls/ holds no key of internal-ca.pem to issue "
                      "it; supply region.crt and region.key there, issued under internal-ca.pem")
    os.umask(0o077)
    (source / "regions").mkdir(exist_ok=True)
    staged = Path(tempfile.mkdtemp(dir=source / "regions", prefix=".vws-install."))
    try:
        (staged / "region.ext").write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
            f"subjectAltName=IP:{host['address']},IP:127.0.0.1,DNS:{name}\n")
        for command in (["req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                         "-subj", "/CN=" + name, "-keyout", str(staged / "region.key"),
                         "-out", str(staged / "region.csr")],
                        ["x509", "-req", "-in", str(staged / "region.csr"), "-CA", str(state / "ca.crt"),
                         "-CAkey", str(state / "ca.key"), "-days", "825", "-set_serial", "0x" + os.urandom(16).hex(),
                         "-extfile", str(staged / "region.ext"), "-out", str(staged / "region.crt")]):
            if local(["openssl", *command]).returncode:
                raise Invalid("cannot issue the certificate of " + name + "; openssl is required")
        (staged / "region.csr").unlink()
        (staged / "region.ext").unlink()
        staged.rename(folder)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def region_files(args, source, host, region):
    """The files of one region instance: its config, certificate, key and the CA of the global client."""
    name = region["name"]
    if (source / "regions").is_symlink():
        raise Invalid("unsafe regions directory")
    folder = source / "regions" / name
    if not folder.exists() and not folder.is_symlink():
        issue_region_certificate(source, host, name, folder)
    if folder.is_symlink() or not folder.is_dir():
        raise Invalid("required regular directory: regions/" + name)
    certificate, key = folder / "region.crt", folder / "region.key"
    pair = (read_file(certificate), read_file(key))
    if public(["x509", "-noout", "-pubkey", "-in"], certificate) != public(["pkey", "-pubout", "-in"], key):
        raise Invalid("regions/" + name + "/region.crt and region.key are not one key pair")
    if local(["openssl", "verify", "-CAfile", str(source / "internal-ca.pem"), str(certificate)]).returncode:
        raise Invalid("regions/" + name + "/region.crt is not issued under internal-ca.pem, the CA the global "
                      "layer trusts for regions")
    if not server_only(certificate):
        raise Invalid("regions/" + name + "/region.crt must be a server-only leaf: extendedKeyUsage exactly serverAuth "
                      "(no clientAuth or anyExtendedKeyUsage) and basicConstraints CA:FALSE. Reissue it under "
                      "internal-ca.pem as a server certificate, or delete regions/" + name + "/ to issue it from "
                      "the CA kept in internal-tls/")
    if not names_address(certificate, host["address"], name):
        retained = (source / "internal-tls/ca.key").is_file()
        fix = ("To reissue it from the CA kept in internal-tls/, delete regions/" + name + "/ and run again"
               if retained else "Reissue it under internal-ca.pem with " + host["address"] + ", 127.0.0.1 and "
               + name + " in its subject alternative names, then replace region.crt and region.key together")
        raise Invalid("regions/" + name + "/region.crt does not name " + host["address"] + ", 127.0.0.1 and "
                      + name + ". " + fix)
    # The operator may supply the configuration; else the packaged example is the default. Never a symlink.
    config = args.inventory.parent / "regions" / (name + ".yaml")
    config = read_file(config) if config.exists() or config.is_symlink() else read_file(PACKAGING / "vws-region/region.yaml")
    if len(config) > globalconfig.MAX_BYTES:
        raise Invalid("region config exceeds 64 KiB")
    try:
        nodes = list(yaml.compose_all(config.decode(), Loader=yaml.SafeLoader))
        if len(nodes) != 1 or not isinstance(nodes[0], yaml.MappingNode):
            raise ValueError
        config_node = resolve_merges(nodes[0])
        if credential_field(config_node, REGION_SCHEMA):
            raise Invalid("region config for " + name + " holds a credential field; region.yaml takes no secret. "
                          "Give the PVE token to scripts/vws region add or region edit")
        globalconfig.decode(config_node, REGION_SCHEMA)
    except (yaml.YAMLError, UnicodeError, ValueError, globalconfig.Refused, RecursionError):
        raise Invalid("invalid region config for " + name + ": only the fields of the packaged region.yaml, "
                      "each once, in one document") from None
    base = "/etc/vws-region/" + name
    env = (f"# Written by vws install; one instance per Proxmox cluster, no secret here.\n"
           f"VWS_REGION_ADDR=:{region['port']}\nVWS_REGION_OPS_ADDR=127.0.0.1:{region['ops_port']}\n")
    return [(base + ".env", "644", env.encode()), (base + "/region.yaml", "644", config),
            (base + "/region.crt", "644", pair[0]), (base + "/region.key", "600", pair[1]),
            (base + "/global-ca.pem", "644", read_file(source / "internal-ca.pem"))]


def inputs(args):
    """The inventory and, for each host, what to install: its files, packages and region instances."""
    cfg = inventory(args.inventory)
    owner = next(h for h in cfg["hosts"] if "global" in h["roles"])
    source = args.credentials
    if source.is_symlink() or ".." in source.parts or any(p.is_symlink() for p in source.parents):
        raise Invalid("unsafe credentials directory")
    generate(source, owner, set(owner["roles"]))
    plans = []
    for host in cfg["hosts"]:
        roles = set(host["roles"])
        files = []
        if "global" in roles:
            config = read_file(args.global_config or args.inventory.parent / "global.yaml")
            try:
                globalconfig.parse(config)
            except (globalconfig.Refused, RecursionError, AttributeError, TypeError):
                raise Invalid("invalid global.yaml; run vws config validate") from None
            for names, dest, mode in ((PRIVATE, "/etc/vws/credentials/", "600"), (PUBLIC, "/etc/vws/", "644")):
                for name in names:
                    files.append((dest + name, mode, read_file(source / name)))
            kek = source / "iam-kek"
            if kek.is_symlink() or not kek.is_dir():
                raise Invalid("required regular directory: iam-kek")
            keys = sorted(kek.iterdir())
            if not any(p.name == "active" for p in keys) or len(keys) < 2:
                raise Invalid("iam-kek requires active and a key file")
            for key in keys:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", key.name):
                    raise Invalid("invalid iam-kek filename")
                files.append(("/etc/vws/credentials/iam-kek/" + key.name, "600", read_file(key)))
            files.append(("/etc/vws/global.yaml", "644", config))
            files.append(("/etc/vws/etcd.conf", "644",
                          f'NAME={host["name"]}\nLIST={host["name"]}={host["address"]}\n'.encode()))
        regions = [dict(region, files=region_files(args, source, host, region)) for region in host.get("regions", [])]
        packages = []
        for name in (name for name, package in PACKAGES.items() if package[0] in roles):
            matches = list(args.packages.glob(name + "_*.deb"))
            if len(matches) != 1:
                raise Invalid("require exactly one package: " + name)
            payload = read_file(matches[0])
            metadata = local(["dpkg-deb", "-f", str(matches[0]), "Package", "Version"])
            fields = metadata.stdout.decode().splitlines()
            if metadata.returncode or len(fields) != 2 or fields[0] != "Package: " + name:
                raise Invalid("invalid package: " + name)
            version = fields[1].removeprefix("Version: ")
            if not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version):
                raise Invalid("invalid package version: " + name)
            packages.append((name, version, payload))
        plans.append((host, files, packages, regions))
    return cfg, plans


class Target:
    def __init__(self, host, output, steps):
        self.host = host
        self.output = output
        self.steps = steps
        self.retried = False

    def run(self, step, command, data=None):
        user = self.host.get("ssh_user", "root")
        remote = "sh -ec " + shlex.quote(command)
        if user != "root":
            remote = "sudo -n " + remote
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "--", user + "@" + self.host["address"], remote]
        for attempt in range(3):
            self.retried = attempt > 0
            try:
                result = local(argv, data)
            except subprocess.TimeoutExpired:
                raise Failed(self.host["name"] + ": " + step + " timed out") from None
            if result.returncode != 255 or attempt == 2:
                break
            time.sleep(attempt + 1)
        if result.returncode:
            raise Failed(self.host["name"] + ": " + step + " failed")
        return result.stdout.strip()

    def emit(self, step, changed=False, status=None):
        row = {"host": self.host["name"], "step": step,
               "status": status or ("changed" if changed else "unchanged")}
        self.steps.append(row)
        if self.output == "text":
            print("{host} {step} {status}".format(**row), flush=True)

    def write(self, path, mode, data):
        parent = str(Path(path).parent)
        parents = list(reversed(Path(parent).parents)) + [Path(parent)]
        checks = "; ".join("test ! -L " + shlex.quote(str(p)) for p in parents)
        result = self.run(path, f"""
{checks}
install -d -m {'700' if '/credentials' in parent else '755'} -o root -g root {parent}
test ! -L {path}
t=$(mktemp {parent}/.vws-install.XXXXXX)
trap 'rm -f "$t"' EXIT HUP INT TERM
cat > "$t"
if test -f {path} && cmp -s "$t" {path} && test "$(stat -c %u:%g:%a {path})" = 0:0:{mode}; then
    echo unchanged
else
    {DIRTY}
    install -m {mode} -o root -g root "$t" {path}
    echo changed
fi
""", data)
        # A reply lost to a dropped connection may hide a write that happened: a retried write counts as one.
        changed = result == b"changed" or self.retried
        self.emit(path, changed)
        return changed

    def service(self, name, changed=False):
        # Enabling and starting are separate: a running unit that is only disabled still restarts for a change.
        restart = f"else\n    systemctl restart {name} >/dev/null\n    s=changed" if changed else ""
        result = self.run(name, f"""
s=unchanged
if ! systemctl is-enabled --quiet {name}; then
    systemctl enable {name} >/dev/null
    s=changed
fi
if ! systemctl is-active --quiet {name}; then
    systemctl start {name} >/dev/null
    s=changed
{restart}
fi
echo "$s"
""")
        self.emit(name, result == b"changed")

    def register_state(self, region):
        """Report `unregistered` for a started instance without state, or require readiness for one with.

        Either answer counts only from the instance's own daemon: the unit's main process must hold every
        listener that can take a connection to 127.0.0.1 on the ops port. A stale instance left on a
        reused port answers /readyz the same way, and its answer fails the run with a fixed diagnostic.
        """
        name, unit = region["name"], "vws-region@" + region["name"]
        state = "/var/lib/vws-region/" + name + "/registration.json"
        ops = "127.0.0.1:" + str(region["ops_port"])
        # An instance with no registration answers /readyz with 503 and the word unregistered; a broken
        # one answers nothing, so a crash loop is never reported as unregistered.
        answer = (f"if test -f {state}; then /usr/lib/vws/check-region-ready {name} && s=registered; "
                  f"else curl --silent --noproxy '*' --max-time 2 http://{ops}/readyz | grep -q unregistered "
                  "&& s=unregistered; fi")
        probe = f"s=; {{ {answer}; }} && {{ {owner(unit + '.service', region['ops_port'])} || {{ s=foreign; false; }}; }}"
        result = self.run(unit + " readiness", "n=0; until " + probe + "; do n=$((n + 1)); "
                          'test $n -lt 15 || break; sleep 2; done; echo "$s"').decode()
        if result == "foreign":
            raise Failed(f"{self.host['name']}: {unit} readiness failed: {ops} is answered by a process "
                         f"other than the main process of {unit}.service")
        if not result:
            raise Failed(self.host["name"] + ": " + unit + " readiness failed")
        self.emit(unit + " registration", status=result)

    def install(self, cfg, files, packages, regions=()):
        self.run("memory controller", "test -f /sys/fs/cgroup/cgroup.controllers && "
                 "grep -qw memory /sys/fs/cgroup/cgroup.controllers")
        # A marker left by an interrupted run, read before this run writes: it restarts every instance, while
        # a change of this run restarts only the instance it belongs to.
        leftover = self.run("interrupted install", "test ! -f " + PENDING + " || echo pending") == b"pending"
        changed = False
        for group, mib in cfg["memory_mib"].items():
            if group not in self.host["roles"]:
                continue
            slice_name = "vws-lab-" + group + ".slice"
            changed = self.write(
                "/etc/systemd/system/" + slice_name + ".d/50-vws-memory.conf", "644",
                f"[Slice]\nMemoryAccounting=yes\nMemoryMax={mib}M\nMemorySwapMax=0\n".encode()) or changed
            if group == "region":
                for region in regions:
                    region["changed"] = self.write(
                        "/etc/systemd/system/vws-region@" + region["name"] + ".service.d/50-vws-memory.conf", "644",
                        f"[Service]\nSlice={slice_name}\n".encode())
                continue
            for service in LAB_SERVICES[group]:
                changed = self.write(
                    "/etc/systemd/system/" + service + ".service.d/50-vws-memory.conf", "644",
                    f"[Service]\nSlice={slice_name}\n".encode()) or changed
        # Reload even after an interrupted write whose reply never reached us.
        self.run("load memory budget", "systemctl daemon-reload")
        self.emit("memory budget")
        for path, mode, data in files:
            changed = self.write(path, mode, data) or changed
        for region in regions:
            for path, mode, data in region["files"]:
                region["changed"] = self.write(path, mode, data) or region.get("changed", False)
        changed = self.run("interrupted install", "test ! -f " + PENDING + " || echo pending") == b"pending" or changed
        for name, version, payload in packages:
            current = self.run(name, f"dpkg-query -W -f='${{db:Status-Abbrev}}\\t${{Version}}' {name} 2>/dev/null || true")
            upgrade = current != b"ii \t" + version.encode()
            if upgrade:
                self.run(name, DIRTY + """
t=$(mktemp -d /var/tmp/vws-install.XXXXXX)
trap 'rm -rf "$t"' EXIT HUP INT TERM
cat > "$t/package.deb"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold "$t/package.deb" >/dev/null 2>&1
""", payload)
            self.emit(name + " package", upgrade)
            _, services, probes = PACKAGES[name]
            for service in services:
                self.service(service, changed or upgrade)
            for probe, command in probes:
                self.run(probe + " readiness", RETRY.format(command) if probe == "etcd" else command)
                self.emit(probe + " readiness")
            if name == "vws-region":
                # Each instance restarts only for its own change; a file the host shares does not touch it.
                for region in regions:
                    self.service("vws-region@" + region["name"], region.get("changed", False) or upgrade or leftover)
                    self.register_state(region)
        self.run("finish", "rm -f " + PENDING)


def main(argv):
    target = None
    steps = []
    try:
        parser = Parser(add_help=False)
        parser.add_argument("--profile", required=True)
        for name in ("inventory", "packages", "credentials"):
            parser.add_argument("--" + name, required=True, type=Path)
        parser.add_argument("--global-config", type=Path)
        parser.add_argument("-o", choices=("text", "json"), default=os.environ.get("VWS_OUTPUT", "text"))
        args = parser.parse_args(argv)
        if args.profile != "lab":
            raise Invalid("only lab is supported; small/ha: vws-deploy#776")
        cfg, plans = inputs(args)
        for host, files, packages, regions in plans:
            target = Target(host, args.o, steps)
            target.install(cfg, files, packages, regions)
        return 0
    except Invalid as error:
        print("vws: " + str(error), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, subprocess.SubprocessError):
        print("vws: cannot read inputs or run required tool", file=sys.stderr)
        return 2 if target is None else 1
    except Failed as error:
        print("vws: " + str(error), file=sys.stderr)
        return 1
    finally:
        if target and target.output == "json":
            print(json.dumps(steps))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
