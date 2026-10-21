#!/usr/bin/env python3
"""A fake gh for the metrics tests (vws-deploy#345). Not a test module.

Answers `gh api -i --method M PATH [-H If-None-Match: E] [--input -]` from $STUB_ROUTES (JSON). A GET
looks up the full path first, then the path without its query; unknown GET paths answer []. A write
looks up "METHOD path"; unknown writes answer 201 with a sha derived from the call. Every call, with
its stdin and whether GH_TOKEN was set, is appended to $STUB_LOG. A GET whose If-None-Match equals the
route's etag answers 304.
"""
import hashlib
import json
import os
import sys

args = sys.argv[1:]
method = args[args.index("--method") + 1]
path = args[args.index("--method") + 2]
inm = ""
if "-H" in args:
    inm = args[args.index("-H") + 1].split(":", 1)[1].strip()
stdin = sys.stdin.read() if "--input" in args else ""
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"method": method, "path": path, "inm": inm, "stdin": stdin,
                        "gh_token_set": "GH_TOKEN" in os.environ}) + "\n")
with open(os.environ["STUB_ROUTES"], encoding="utf-8") as f:
    routes = json.load(f)
if method != "GET":
    r = routes.get(f"{method} {path}")
    if r is None:
        sha = hashlib.sha1(f"{method} {path} {stdin}".encode()).hexdigest()
        r = {"status": 201, "body": {"sha": sha, "object": {"sha": sha}}}
    print(f"HTTP/2.0 {r.get('status', 201)} X\n\n{json.dumps(r['body'])}")
    sys.exit(0 if r.get("status", 201) < 400 else 1)
r = routes.get(path) or routes.get(path.split("?", 1)[0]) or {"status": 200, "body": []}
etag = 'W/"' + hashlib.sha256(json.dumps(r["body"], sort_keys=True).encode()).hexdigest()[:16] + '"'
if inm and inm == etag:
    print("HTTP/2.0 304 Not Modified\nEtag: " + etag + "\n")
    sys.exit(1)
status = r.get("status", 200)
print(f"HTTP/2.0 {status} X\nEtag: {etag}\n\n{json.dumps(r['body'])}")
sys.exit(0 if status < 400 else 1)
