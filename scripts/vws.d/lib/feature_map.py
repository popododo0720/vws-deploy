#!/usr/bin/env python3
"""Record signed scenario results and render the feature map.
Only a signed vws-test pass with pveServerId matching the map moves lastPass forward.
Retired map/result identities are refused; signing follows docs/design/result-signing.md.

Update exits 0 for recorded/ignored results, 1 for refusals, 2 for invalid
input without writes, and 3 for unreadable maps. Check validates Phase 1 coverage and markdown.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
CLUSTER = "vws-test"
CLOCK_SKEW = timedelta(minutes=5)
PRINCIPAL = "vws-test"
NAMESPACE = "vws-feature-map@vws-deploy"
SSH_TIMEOUT = 30


class InvalidInput(Exception):
    """A result or the map fingerprint has the wrong shape: exit 2, nothing written."""


def check_result(rpath, r):
    """Validate the fields update() reads; return (scenario, finishedAt as aware datetime)."""
    if not isinstance(r, dict):
        raise InvalidInput(f"{rpath}: result is {type(r).__name__}, not an object")
    for key in ("scenario", "cluster", "finishedAt", "result"):
        if key not in r:
            raise InvalidInput(f"{rpath}: {key} is missing")
        if not isinstance(r[key], str) or not r[key]:
            raise InvalidInput(f"{rpath}: {key} must be a non-empty string, got {type(r[key]).__name__}")
    for key in ("pveServerId", "evidence", "signature"):
        if r.get(key) is not None and not isinstance(r[key], str):
            raise InvalidInput(f"{rpath}: {key} must be a string, got {type(r[key]).__name__}")
    try:
        when = parse_time(r["finishedAt"])
    except (AttributeError, TypeError, ValueError) as e:
        raise InvalidInput(f"{rpath}: finishedAt {r['finishedAt']!r} is not RFC3339 with a zone ({e})") from None
    if when > datetime.now(timezone.utc) + CLOCK_SKEW:
        raise InvalidInput(f"{rpath}: finishedAt {r['finishedAt']} is in the future")
    return r["scenario"], when


def canonical(r):
    """The signed bytes: the result without "signature", keys sorted, no whitespace."""
    body = {k: v for k, v in r.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def ssh_keygen(args, data):
    try:
        p = subprocess.run(["ssh-keygen", "-Y", *args], input=data, capture_output=True, timeout=SSH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"ssh-keygen: {e}"
    msg = (p.stderr or p.stdout).decode(errors="replace").strip().replace("\n", "; ")
    return (p.stdout if p.returncode == 0 else None), msg


def verify_signature(r, signers):
    """None when r["signature"] is a valid SSHSIG by a key in signers, else the reason."""
    if not r.get("signature"):
        return "unsigned"
    with tempfile.TemporaryDirectory() as d:
        allowed, sig = os.path.join(d, "allowed_signers"), os.path.join(d, "result.sig")
        with open(allowed, "w", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in signers))
        with open(sig, "w", encoding="utf-8") as f:
            f.write(r["signature"])
        ok, msg = ssh_keygen(["verify", "-f", allowed, "-I", PRINCIPAL, "-n", NAMESPACE, "-s", sig], canonical(r))
    return None if ok is not None else f"signature does not verify against vwsTest.signers ({msg[:200]})"


def sign(rpath, key):
    with open(rpath, encoding="utf-8") as f:
        r = json.load(f)
    if not isinstance(r, dict):
        raise InvalidInput(f"{rpath}: result is {type(r).__name__}, not an object")
    sig, msg = ssh_keygen(["sign", "-f", key, "-n", NAMESPACE], canonical(r))
    if sig is None:
        return f"cannot sign {rpath}: {msg[:200]}"
    r["signature"] = sig.decode()
    write(rpath, json.dumps(r, indent=2, ensure_ascii=False) + "\n")
    return None


def parse_time(s):
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time without zone: {s}")
    return t


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    names = set()
    for feat in data["features"]:
        for key in ("feature", "repo"):
            if not feat.get(key):
                raise ValueError(f"feature entry without {key}: {feat}")
        if feat["feature"] in names:
            raise ValueError(f"duplicate feature {feat['feature']}")
        names.add(feat["feature"])
    vws_test = data.get("vwsTest") or {}
    if isinstance(vws_test, dict) and "kubeSystemUID" in vws_test:
        raise InvalidInput(f"{path}: vwsTest.kubeSystemUID is retired (vws-deploy#826); set vwsTest.pveServerId")
    return data


def phase1_path(map_path):
    return os.path.join(os.path.dirname(map_path), "phase1-features.json")


def phase1_gaps(map_path, data):
    """FAIL lines for Phase 1 features missing from the map and map rows in neither list."""
    with open(phase1_path(map_path), encoding="utf-8") as f:
        p1 = json.load(f)
    want, later = p1["phase1"], p1["later"]
    if not isinstance(want, list) or not isinstance(later, list) or not all(
            isinstance(x, dict) and isinstance(x.get("reason"), str) and x["reason"] for x in later):
        raise ValueError(f"{phase1_path(map_path)}: phase1 must be a list and later a list of {{feature, reason}}")
    names = want + [x.get("feature") for x in later]
    if not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
        raise ValueError(f"{phase1_path(map_path)}: feature names must be unique non-empty strings")
    have = [f["feature"] for f in data["features"]]
    out = [f"FAIL\tphase1\t{n}: Phase 1 feature has no row in {map_path}" for n in want if n not in have]
    out += [f"FAIL\tphase1\t{n}: map row is neither phase1 nor later in {phase1_path(map_path)}"
            for n in have if n not in names]
    return out


def scenario_gaps(map_path, data):
    """FAIL lines for rows with neither a scenario nor an `na` reason, or with both."""
    out = []
    for f in data["features"]:
        sc, na = f.get("scenario"), f.get("na")
        if na is not None and not (isinstance(na, str) and na.strip() and na.splitlines() == [na] and "|" not in na):
            out.append(f"FAIL\tfeature-map\t{f['feature']}: na must be a one-line reason without |")
        elif sc and na:
            out.append(f"FAIL\tfeature-map\t{f['feature']}: has both a scenario and an na reason in {map_path}")
        elif not sc and not na:
            out.append(f"FAIL\tfeature-map\t{f['feature']}: no scenario and no na reason in {map_path}")
    return out


def md_path(map_path):
    return os.path.splitext(map_path)[0] + ".md"


def render(data):
    rows = [
        "# Feature map",
        "",
        "Generated from docs/feature-map.json by `scripts/vws verify --update-feature-map`. Do not edit by hand.",
        "Last pass is a vws-test scenario run (KST). `unverified` means the feature has no scenario, and the Verify command column gives the reason; `never passed` means a scenario exists but no passing vws-test result was recorded.",
        "",
        "| Feature | Repo | Issue | Verify command | Last pass (KST) | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    for f in data["features"]:
        sc = f.get("scenario")
        cmd = f"`scripts/vws verify scenario {sc}`" if sc else f"N/A: {f['na']}" if f.get("na") else "-"
        if not sc:
            last = "unverified"
        elif f.get("lastPass"):
            last = parse_time(f["lastPass"]).astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        else:
            last = "never passed"
        rows.append("| {} | {} | {} | {} | {} | {} |".format(
            f["feature"], f["repo"], f.get("issue") or "-", cmd, last, f.get("evidence") or "-"))
    return "\n".join(rows) + "\n"


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def update(map_path, results):
    data = load(map_path)
    by_sc = {f["scenario"]: f for f in data["features"] if f.get("scenario")}
    vws_test = data.get("vwsTest") or {}
    if not isinstance(vws_test, dict):
        raise InvalidInput(f"{map_path}: vwsTest must be an object, got {type(vws_test).__name__}")
    want_id = vws_test.get("pveServerId")
    if want_id is not None and not isinstance(want_id, str):
        raise InvalidInput(f"{map_path}: vwsTest.pveServerId must be a string or null, got {type(want_id).__name__}")
    signers = vws_test.get("signers") or []
    if not isinstance(signers, list) or not all(isinstance(x, str) and x.strip() and "\n" not in x for x in signers):
        raise InvalidInput(f"{map_path}: vwsTest.signers must be a list of one-line allowed_signers strings")
    out, failed = [], False
    for rpath in results:
        try:
            with open(rpath, encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError) as e:
            out.append(f"FAIL\tfeature-map\t{rpath}: unreadable scenario result ({e})")
            failed = True
            continue
        sc, when = check_result(rpath, r)
        if "clusterUID" in r:
            out.append(f"FAIL\tfeature-map\t{sc}: clusterUID is a retired Kubernetes identity; refused")
            failed = True
        elif sc not in by_sc:
            out.append(f"WARN\tfeature-map\t{sc}: no feature uses this scenario")
        elif r.get("cluster") != CLUSTER:
            out.append(f"WARN\tfeature-map\t{sc}: result is from {r.get('cluster')!r}, not {CLUSTER}; ignored")
        elif not want_id:
            out.append(f"FAIL\tfeature-map\t{sc}: no vws-test fingerprint (vwsTest.pveServerId) in {map_path}; refused")
            failed = True
        elif r.get("pveServerId") != want_id:
            out.append(f"FAIL\tfeature-map\t{sc}: pveServerId {r.get('pveServerId')!r} does not match the vws-test fingerprint; refused")
            failed = True
        elif not signers:
            out.append(f"FAIL\tfeature-map\t{sc}: no trusted signer (vwsTest.signers) in {map_path}; refused")
            failed = True
        elif (why := verify_signature(r, signers)) is not None:
            out.append(f"FAIL\tfeature-map\t{sc}: {rpath}: {why}; refused")
            failed = True
        elif r.get("result") != "pass":
            out.append(f"WARN\tfeature-map\t{sc}: result {r.get('result')!r}; last pass kept")
        else:
            f = by_sc[sc]
            if f.get("lastPass") and parse_time(f["lastPass"]) >= when:
                out.append(f"OK\tfeature-map\t{sc}: already has a pass at or after {r['finishedAt']}")
            else:
                f["lastPass"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f["evidence"] = r.get("evidence") or None
                out.append(f"OK\tfeature-map\t{sc}: last pass {f['lastPass']}")
    write(map_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    write(md_path(map_path), render(data))
    for f in data["features"]:
        if not f.get("scenario"):
            out.append(f"WARN\tunverified\t{f['feature']}")
    return out, failed


def main(argv):
    if len(argv) < 2 or argv[0] not in ("update", "render", "check", "sign") or (argv[0] == "sign" and len(argv) != 3):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if argv[0] == "sign":
            try:
                err = sign(argv[1], argv[2])
            except (InvalidInput, OSError, ValueError) as e:
                err = f"cannot sign {argv[1]}: {e}"
            if err:
                print(f"FAIL: {err}", file=sys.stderr)
                return 1
            return 0
        if argv[0] == "update":
            try:
                out, failed = update(argv[1], argv[2:])
            except (AttributeError, TypeError) as e:
                print(f"FAIL: malformed input ({type(e).__name__}: {e})")
                return 2
            print("\n".join(out))
            return 1 if failed else 0
        data = load(argv[1])
        text = render(data)
        if argv[0] == "render":
            write(md_path(argv[1]), text)
            return 0
        out = phase1_gaps(argv[1], data) + scenario_gaps(argv[1], data)
        with open(md_path(argv[1]), encoding="utf-8") as f:
            if f.read() != text:
                out.append(f"FAIL\tfeature-map\t{md_path(argv[1])} is stale; run scripts/vws verify --update-feature-map")
        print("\n".join(out))
        return 1 if out else 0
    except InvalidInput as e:
        print(f"FAIL: {e}")
        return 2
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"feature-map: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
