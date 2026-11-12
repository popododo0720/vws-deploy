import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

def one(pattern, text):
    matches = re.findall(pattern, text, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"expected one {pattern!r}, found {len(matches)}")
    return matches[0]


def flag(text, name):
    args = shlex.split(one(r"^ExecStart=(.+)$", text))
    return args[args.index(name) + 1]


def address(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError(f"invalid readiness URL: {url}")
    return f"{parsed.hostname}:{parsed.port or 80}"


def check(packaging):
    region = packaging / "vws-region"
    global_ = packaging / "vws-global"
    listeners = {
        "region": flag((region / "vws-region.service").read_text(), "-ops-addr"),
    }
    config = (global_ / "global.yaml").read_text()
    block = one(r"^listeners:\n((?:[ \t]+[^\n]*\n)+)", config)
    for line in block.splitlines():
        name, value = line.strip().split(": ", 1)
        listeners[f"api.{name}"] = value
    iam = (global_ / "vws-iam.service").read_text()
    listeners["iam"] = flag(iam, "-health-listen")
    console = (global_ / "vws-console.service").read_text()
    environment = dict(word.split("=", 1) for line in console.splitlines()
                       if line.startswith("Environment=")
                       for word in shlex.split(line.removeprefix("Environment=")))
    listeners["console"] = f"{environment['HOSTNAME']}:{environment['PORT']}"
    proxy = (global_ / "vws-aws-proxy.service").read_text()
    listeners["proxy"] = "unix:" + flag(proxy, "-socket")

    probes = {
        "region": address(one(r"http://[^\s;]+", (region / "check-ready").read_text())),
    }
    text = (global_ / "check-ready").read_text()
    cases = re.findall(r"^\s+(\w+)\)\s*(.*?);;", text, re.MULTILINE | re.DOTALL)
    for name, body in cases:
        url = one(r"url=(\S+)", body)
        socket = re.findall(r"socket=(\S+)", body)
        probes["api.ops" if name == "api" else name] = (
            "unix:" + socket[0] if socket else address(url))
    if set(probes) != {"region", "api.ops", "iam", "console", "proxy"}:
        raise ValueError(f"unexpected readiness services: {sorted(probes)}")
    for name, probe in probes.items():
        if listeners[name] != probe:
            raise ValueError(f"{name}: listener {listeners[name]} != readiness {probe}")
    owners = {}
    for name, listener in listeners.items():
        if listener in owners:
            raise ValueError(f"{name} and {owners[listener]} share {listener}")
        owners[listener] = name


if __name__ == "__main__":
    try:
        check(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent)
    except (ValueError, KeyError, IndexError, OSError) as error:
        sys.exit(f"ops/readiness addresses: {error}")
    print("ops/readiness addresses are distinct and match their listeners")
