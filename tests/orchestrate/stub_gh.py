#!/usr/bin/env python3
"""A fake gh for the orchestrator tests (vws-deploy#333). Not a test module.

Answers `gh api -i --method M PATH [-H If-None-Match: E] [--input -]` from $STUB_ROUTES (JSON:
{"<path without query>": {"status": 200, "body": ...}}) and appends every call, with its stdin and
whether GH_TOKEN was set, to $STUB_LOG. Unknown GET paths answer []. Writes answer 201 unless
$STUB_DENY_WRITES is set; a "<METHOD> <path>" route overrides a write answer. A GraphQL read answers
from the "graphql" route. A GET whose
If-None-Match equals the route's etag answers 304. A route key with the query string wins over one without.
"""
import hashlib
import json
import os
import sys

args = sys.argv[1:]
if args[:3] == ["api", "-i", "graphql"]:
    # A GraphQL read (`gh api -i graphql --input -`): logged as method GRAPHQL, answered from the "graphql" route.
    stdin = sys.stdin.read()
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"method": "GRAPHQL", "path": "graphql", "inm": "", "stdin": stdin,
                            "gh_token_set": "GH_TOKEN" in os.environ}) + "\n")
    r = json.load(open(os.environ["STUB_ROUTES"], encoding="utf-8")).get(
        "graphql", {"body": {"errors": [{"message": "no stub route"}]}})
    print(f"HTTP/2.0 {r.get('status', 200)} X\n\n{json.dumps(r['body'])}")
    sys.exit(0 if r.get("status", 200) < 400 else 1)
method = args[args.index("--method") + 1]
path = args[args.index("--method") + 2]
inm = ""
if "-H" in args:
    inm = args[args.index("-H") + 1].split(":", 1)[1].strip()
stdin = sys.stdin.read() if "--input" in args else ""
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"method": method, "path": path, "inm": inm, "stdin": stdin,
                        "gh_token_set": "GH_TOKEN" in os.environ}) + "\n")
routes = json.load(open(os.environ["STUB_ROUTES"], encoding="utf-8"))
key = path.split("?", 1)[0]
if method != "GET":
    if os.environ.get("STUB_DENY_WRITES"):
        print("HTTP/2.0 500 Internal Server Error\n\n{}")
        sys.exit(1)
    w = routes.get(f"{method} {key}")
    if w:
        print(f"HTTP/2.0 {w.get('status', 200)} X\n\n{json.dumps(w['body'])}")
        sys.exit(0 if w.get("status", 200) < 400 else 1)
    print('HTTP/2.0 201 Created\n\n{"id": 1}')
    sys.exit(0)
r = routes.get(path) or routes.get(key, {"status": 200, "body": []})  # an exact path with query wins
etag = 'W/"' + hashlib.sha256(json.dumps(r["body"], sort_keys=True).encode()).hexdigest()[:16] + '"'
if inm and inm == etag:
    print("HTTP/2.0 304 Not Modified\nEtag: " + etag + "\n")
    sys.exit(1)
status = r.get("status", 200)
print(f"HTTP/2.0 {status} X\nEtag: {etag}\n\n{json.dumps(r['body'])}")
sys.exit(0 if status < 400 else 1)
