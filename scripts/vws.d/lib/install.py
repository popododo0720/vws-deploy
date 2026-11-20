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
import subprocess
import sys
import tempfile
import time

import globalconfig
import yaml

# A host is the global layer on a VM or bare metal (etcd and global), or the lab host that also has the
# region role, as before. A region-only host is not installed here (vws-deploy#850).
ROLES = ({"etcd", "global"}, {"etcd", "global", "region"})
PRIVATE = (
    "ca.crt member.crt member.key client.crt client.key admin-kek console-token "
    "vws-access-key vws-secret-key api-etcd-key.pem api-mtls-key.pem aws-source "
    "iam-etcd-key.pem iam-key.pem"
).split()
PUBLIC = "etcd-ca.pem api-etcd.pem internal-ca.pem api-mtls.pem iam-etcd.pem iam.pem".split()
SCHEMA = {"profile": str, "hosts": [{"name": str, "address": str, "ssh_user": str,
                                   "roles": [str]}], "vip": {"address": str},
          "memory_mib": {"global": str, "region": str, "etcd": str}}
LAB_MEMORY_MIB = {"global": 1024, "region": 512, "etcd": 512}
LAB_SERVICES = {
    "global": ("vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console"),
    "region": ("vws-region",),
    "etcd": ("vws-etcd", "vws-etcd-snapshot", "vws-etcd-defrag"),
}
PACKAGES = {
    "vws-etcd": ("etcd", ["vws-etcd", "vws-etcd-snapshot.timer", "vws-etcd-defrag.timer"],
                 [("etcd", "CREDENTIALS_DIRECTORY=/run/credentials/vws-etcd.service /usr/lib/vws/etcd/check-ready")]),
    "vws-global": ("global", ["vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console"],
                   [(p, "/usr/lib/vws/check-global-ready " + p) for p in ("iam", "proxy", "api", "console")]),
    "vws-region": ("region", ["vws-region"], [("region", "/usr/lib/vws/check-region-ready")]),
}
# The etcd probe asks once; a unit that was just started gets 30 seconds to answer.
RETRY = "n=0; until {}; do n=$((n + 1)); test $n -lt 15 || exit 1; sleep 2; done"
PACKAGING = Path(__file__).resolve().parents[3] / "packaging"
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
        if cfg.get("profile") != "lab" or len(cfg.get("hosts", [])) != 1:
            raise Invalid("lab inventory requires one host")
        memory = cfg.get("memory_mib", LAB_MEMORY_MIB)
        if (set(memory) != set(LAB_MEMORY_MIB)
                or any(not re.fullmatch(r"[1-9][0-9]{0,3}", str(v)) for v in memory.values())
                or sum(int(v) for v in memory.values()) > 2048):
            raise Invalid("lab memory_mib requires positive global, region and etcd budgets totaling <= 2048 MiB")
        cfg["memory_mib"] = {k: int(v) for k, v in memory.items()}
        host = cfg["hosts"][0]
        roles = host.get("roles", [])
        if (set(roles) not in ROLES or len(roles) != len(set(roles))
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", host.get("name", ""))
                or host.get("name") == "client"
                or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", host.get("ssh_user", "root"))):
            raise Invalid("invalid lab host")
        ipaddress.IPv4Address(host["address"])
        if ("vip" in cfg) != ("region" in roles):
            raise Invalid("vip is required with the region role and refused without it")
        if "vip" in cfg:
            ipaddress.IPv4Interface(cfg["vip"]["address"])
        return cfg
    except (yaml.YAMLError, UnicodeError, globalconfig.Refused, KeyError, ValueError, TypeError, RecursionError):
        raise Invalid("invalid inventory") from None


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


def inputs(args):
    cfg = inventory(args.inventory)
    host = cfg["hosts"][0]
    roles = set(host["roles"])
    source = args.credentials
    if source.is_symlink() or ".." in source.parts or any(p.is_symlink() for p in source.parents):
        raise Invalid("unsafe credentials directory")
    generate(source, host, roles)
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
    if "region" in roles:
        files.append(("/etc/vws/credentials/region-kek", "600", read_file(source / "region-kek")))
        files.append(("/etc/vws/region.key", "600", read_file(source / "region.key")))
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
    return cfg, files, packages


class Target:
    def __init__(self, host, output):
        self.host = host
        self.output = output
        self.steps = []
        self.region = "region" in host["roles"]
        # Only a host with the region role has keepalived; anything that changes it gives up the VIP first.
        self.vip_off = "systemctl disable --now keepalived >/dev/null 2>&1" if self.region else ":"

    def run(self, step, command, data=None):
        user = self.host.get("ssh_user", "root")
        remote = "sh -ec " + shlex.quote(command)
        if user != "root":
            remote = "sudo -n " + remote
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=2",
                "--", user + "@" + self.host["address"], remote]
        for attempt in range(3):
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
    {self.vip_off}
    {DIRTY}
    install -m {mode} -o root -g root "$t" {path}
    echo changed
fi
""", data)
        changed = result == b"changed"
        self.emit(path, changed)
        return changed

    def service(self, name, changed=False):
        result = self.run(name, f"""
if systemctl is-enabled --quiet {name} && systemctl is-active --quiet {name}; then
    {'systemctl restart ' + name + ' >/dev/null; echo changed' if changed else 'echo unchanged'}
else
    systemctl enable --now {name} >/dev/null
    echo changed
fi
""")
        self.emit(name, result == b"changed")

    def install(self, cfg, files, packages):
        try:
            self.run("memory controller", "test -f /sys/fs/cgroup/cgroup.controllers && "
                     "grep -qw memory /sys/fs/cgroup/cgroup.controllers")
            changed = False
            for group, mib in cfg["memory_mib"].items():
                if group not in self.host["roles"]:
                    continue
                slice_name = "vws-lab-" + group + ".slice"
                changed = self.write(
                    "/etc/systemd/system/" + slice_name + ".d/50-vws-memory.conf", "644",
                    f"[Slice]\nMemoryAccounting=yes\nMemoryMax={mib}M\nMemorySwapMax=0\n".encode()) or changed
                for service in LAB_SERVICES[group]:
                    # The region package also sets a per-service 512 MiB limit.
                    limit = f"MemoryMax={mib}M\n" if group == "region" else ""
                    changed = self.write(
                        "/etc/systemd/system/" + service + ".service.d/50-vws-memory.conf", "644",
                        f"[Service]\nSlice={slice_name}\n{limit}".encode()) or changed
            # Reload even after an interrupted write whose reply never reached us.
            self.run("load memory budget", "systemctl daemon-reload")
            self.emit("memory budget")
            for path, mode, data in files:
                changed = self.write(path, mode, data) or changed
            if self.region:
                template = Path(__file__).with_name("keepalived.conf")
                vip = template.read_text().replace("192.0.2.10/24", cfg["vip"]["address"])
                changed = self.write("/etc/keepalived/keepalived.conf", "644", vip.encode()) or changed
            changed = self.run("interrupted install", "test ! -f " + PENDING + " || echo pending") == b"pending" or changed
            for name, version, payload in packages:
                current = self.run(name, f"dpkg-query -W -f='${{db:Status-Abbrev}}\\t${{Version}}' {name} 2>/dev/null || true")
                upgrade = current != b"ii \t" + version.encode()
                if upgrade:
                    self.run(name, self.vip_off + "\n" + DIRTY + """
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
            if self.region:
                self.service("keepalived")
            self.run("finish", "rm -f " + PENDING)
        except Failed:
            if self.region:
                self.run("disable keepalived", self.vip_off)
            raise


def main(argv):
    target = None
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
        cfg, files, packages = inputs(args)
        target = Target(cfg["hosts"][0], args.o)
        target.install(cfg, files, packages)
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
            print(json.dumps(target.steps))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
