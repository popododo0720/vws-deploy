#!/usr/bin/env python3
"""redact.py DIR REPORT: mask secret-shaped lines in every file under DIR, in place (vws-deploy#58, #43 section 7).

A matching line becomes "[REDACTED]". REPORT gets one "<pattern> <count>" line per pattern and a
total; it never holds a matched value. Before scanning, a canary of every pattern (obviously fake)
must be caught, so a broken scanner fails closed. Exit 0 scanned, 3 the scanner cannot be trusted.
"""
import json
import re
import sys
from pathlib import Path

PATTERNS = [
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("token-value", re.compile(r"""(?i)\b[\w-]*token["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{8,}""")),
    ("credential-url", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@\"']+:[^/\s@\"']+@")),
    ("long-digits", re.compile(r"(?<!\d)\d{12,}(?!\d)")),
]
PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")
# A data map: YAML "data:" or JSON "data": { on its own line. Values of 40+ base64 characters inside it.
DATA_START = re.compile(r"""^(\s*)["']?(data|binaryData)["']?\s*:\s*\{?\s*$""")
B64_VALUE = re.compile(r"""^\s*["']?[\w.-]+["']?\s*:\s*["']?[A-Za-z0-9+/]{40,}={0,2}["']?,?\s*$""")
# A key whose name looks like it holds a secret: its value is masked whatever it looks like (also a
# nested object or list below it). Case-insensitive, plus camelCase "...Key". secretName and
# secretRef name a Secret and are not secret themselves.
SECRET_KEY = re.compile(r"(?i)(passw|passphrase|token|secret|credential|private|apikey|api[-_]key|dsn|auth|(^|[-_.])key$)")
SECRET_KEY_CAMEL = re.compile(r"[a-z0-9]Key$")
REFERENCE_KEYS = {"secretname", "secretref", "secretkeyref", "tokensecretref"}
KEY_LINE = re.compile(r"""^(\s*)(?:-\s+)?["']?([A-Za-z0-9_.-]+)["']?\s*:\s*(.*?)\s*,?\s*$""")
NAMES = ["pem-private-key", "data-base64", "secret-key"] + [n for n, _ in PATTERNS]


def secret_key(name):
    if name.lower() in REFERENCE_KEYS:
        return False
    return bool(SECRET_KEY.search(name) or SECRET_KEY_CAMEL.search(name))


def mask_json(value, counts):
    """Replace every value under a secret-shaped key, at any depth."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if secret_key(str(k)) and v not in (None, "", {}, []):
                counts["secret-key"] += 1
                out[k] = REDACTED
            else:
                out[k] = mask_json(v, counts)
        return out
    if isinstance(value, list):
        return [mask_json(v, counts) for v in value]
    return value
REDACTED = "[REDACTED]"


def scan(lines, counts):
    out, in_pem, data_indent, secret_indent = [], False, None, None
    for line in lines:
        body = line.rstrip("\n")
        indent = len(body) - len(body.lstrip())
        if data_indent is not None and body.strip() and (indent <= data_indent):
            data_indent = None
        if secret_indent is not None and body.strip() and indent <= secret_indent and not body.lstrip().startswith(("}", "]")):
            secret_indent = None
        if secret_indent is not None:
            out.append(REDACTED + "\n" if body.strip() and body.strip() not in ("}", "},", "]", "],") else line)
            continue
        k = KEY_LINE.match(body)
        if k and secret_key(k.group(2)):
            value = k.group(3)
            if value.strip("\"'") == REDACTED:
                out.append(line)
                continue
            if value in ("", "{", "[", "|", ">", "|-", ">-"):
                counts["secret-key"] += 1
                secret_indent = len(k.group(1))
                out.append(line)
                continue
            if value not in ('""', "''", "{}", "[]", "null", "~"):
                counts["secret-key"] += 1
                out.append(f"{k.group(1)}{k.group(2)}: {REDACTED}\n")
                continue
        if in_pem or PEM_BEGIN.search(body):
            counts["pem-private-key"] += 1 if not in_pem else 0
            in_pem = not PEM_END.search(body)
            out.append(REDACTED + "\n")
            continue
        m = DATA_START.match(body)
        if m:
            data_indent = len(m.group(1))
            out.append(line)
            continue
        if data_indent is not None and B64_VALUE.match(body):
            counts["data-base64"] += 1
            out.append(REDACTED + "\n")
            continue
        hit = next((n for n, p in PATTERNS if p.search(body)), None)
        if hit:
            counts[hit] += 1
            out.append(REDACTED + "\n")
            continue
        out.append(line)
    return out


def canary():
    # Obviously fake values, built at run time so this file holds none.
    fake = "FAKE" * 12
    lines = [
        "-----BEGIN " + "PRIVATE KEY-----\n", fake + "\n", "-----END " + "PRIVATE KEY-----\n",
        "data:\n", "  ca.crt: " + fake + "\n",
        "sent with Bearer " + fake + "\n",
        "args: --token=" + fake + "\n",
        "url: https://" + "fakeuser:fakepass" + "@example.invalid/\n",
        "account " + "0" * 12 + "\n",
        "pass" + "word: " + "x" + "\n",
    ]
    counts = dict.fromkeys(NAMES, 0)
    out = scan(lines, counts)
    return all(counts[n] > 0 for n in NAMES) and not any(fake in l for l in out)


def main():
    if len(sys.argv) != 3:
        print("usage: redact.py DIR REPORT", file=sys.stderr)
        return 2
    if not canary():
        print("redact.py: the canary was not caught; refusing to scan", file=sys.stderr)
        return 3
    counts = dict.fromkeys(NAMES, 0)
    for path in sorted(Path(sys.argv[1]).rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="strict").splitlines(keepends=True)
        except UnicodeDecodeError:
            path.write_text(REDACTED + " (binary content removed)\n")
            counts.setdefault("binary-file", 0)
            counts["binary-file"] += 1
            continue
        try:
            doc = json.loads("".join(text))
        except ValueError:
            doc = None
        if isinstance(doc, (dict, list)):
            text = (json.dumps(mask_json(doc, counts), indent=2, sort_keys=True) + "\n").splitlines(keepends=True)
        path.write_text("".join(scan(text, counts)))
    report = "".join(f"{n} {c}\n" for n, c in counts.items()) + f"total {sum(counts.values())}\n"
    Path(sys.argv[2]).write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
