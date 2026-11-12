#!/usr/bin/env python3
"""Install operator-supplied lab packages; all payloads travel on SSH stdin."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import globalconfig
import yaml

PRIVATE = (
    "ca.crt member.crt member.key client.crt client.key admin-kek console-token "
    "vws-access-key vws-secret-key api-etcd-key.pem api-mtls-key.pem aws-source "
    "iam-etcd-key.pem iam-key.pem region-kek"
).split()
PUBLIC = "etcd-ca.pem api-etcd.pem internal-ca.pem api-mtls.pem iam-etcd.pem iam.pem".split()
SCHEMA = {"profile": str, "hosts": [{"name": str, "address": str, "ssh_user": str,
                                   "roles": [str]}], "vip": {"address": str}}
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
        host = cfg["hosts"][0]
        if (set(host.get("roles", [])) != {"etcd", "global", "region"}
                or len(host["roles"]) != 3
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", host.get("name", ""))
                or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", host.get("ssh_user", "root"))):
            raise Invalid("invalid lab host")
        ipaddress.IPv4Address(host["address"])
        ipaddress.IPv4Interface(cfg["vip"]["address"])
        return cfg
    except (yaml.YAMLError, UnicodeError, globalconfig.Refused, KeyError, ValueError, TypeError, RecursionError):
        raise Invalid("invalid inventory") from None


def inputs(args):
    cfg = inventory(args.inventory)
    config = read_file(args.global_config or args.inventory.parent / "global.yaml")
    try:
        globalconfig.parse(config)
    except (globalconfig.Refused, RecursionError, AttributeError, TypeError):
        raise Invalid("invalid global.yaml; run vws config validate") from None
    source = args.credentials
    if source.is_symlink() or ".." in source.parts or any(p.is_symlink() for p in source.parents):
        raise Invalid("unsafe credentials directory")
    files = []
    for names, dest, mode in ((PRIVATE, "/etc/vws/credentials/", "600"),
                              (PUBLIC, "/etc/vws/", "644"), (["region.key"], "/etc/vws/", "600")):
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
    packages = []
    for name in ("vws-etcd", "vws-global", "vws-region"):
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
    return cfg, config, files, packages


class Target:
    def __init__(self, host, output):
        self.host = host
        self.output = output
        self.steps = []

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

    def emit(self, step, changed=False):
        row = {"host": self.host["name"], "step": step,
               "status": "changed" if changed else "unchanged"}
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
    systemctl disable --now keepalived >/dev/null 2>&1
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

    def install(self, cfg, config, files, packages):
        try:
            changed = False
            for path, mode, data in files:
                changed = self.write(path, mode, data) or changed
            changed = self.write("/etc/vws/global.yaml", "644", config) or changed
            host = self.host
            changed = self.write("/etc/vws/etcd.conf", "644",
                                 f'NAME={host["name"]}\nLIST={host["name"]}={host["address"]}\n'.encode()) or changed
            template = Path(__file__).resolve().parents[3] / "packaging/vws-region/keepalived.conf"
            vip = template.read_text().replace("192.0.2.10/24", cfg["vip"]["address"])
            changed = self.write("/etc/keepalived/keepalived.conf", "644", vip.encode()) or changed
            changed = self.run("interrupted install", "test ! -f " + PENDING + " || echo pending") == b"pending" or changed
            for name, version, payload in packages:
                current = self.run(name, f"dpkg-query -W -f='${{Version}}' {name} 2>/dev/null || true")
                upgrade = current != version.encode()
                if upgrade:
                    self.run(name, """
systemctl disable --now keepalived >/dev/null 2>&1
""" + DIRTY + """
t=$(mktemp -d /var/tmp/vws-install.XXXXXX)
trap 'rm -rf "$t"' EXIT HUP INT TERM
cat > "$t/package.deb"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends -o Dpkg::Options::=--force-confold "$t/package.deb" >/dev/null 2>&1
""", payload)
                self.emit(name + " package", upgrade)
                services, probes = {
                    "vws-etcd": (["vws-etcd"], [("etcd", "CREDENTIALS_DIRECTORY=/run/credentials/vws-etcd.service /usr/lib/vws/etcd/check-ready")]),
                    "vws-global": (["vws-iam", "vws-aws-proxy", "vws-console-api", "vws-console"],
                                   [(p, "/usr/lib/vws/check-global-ready " + p) for p in ("iam", "proxy", "api", "console")]),
                    "vws-region": (["vws-region"], [("region", "/usr/lib/vws/check-region-ready")]),
                }[name]
                for service in services:
                    self.service(service, changed or upgrade)
                for probe, command in probes:
                    self.run(probe + " readiness", command)
                    self.emit(probe + " readiness")
            self.service("keepalived")
            self.run("finish", "rm -f " + PENDING)
        except Failed:
            self.run("disable keepalived", "systemctl disable --now keepalived >/dev/null 2>&1")
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
        cfg, config, files, packages = inputs(args)
        target = Target(cfg["hosts"][0], args.o)
        target.install(cfg, config, files, packages)
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
