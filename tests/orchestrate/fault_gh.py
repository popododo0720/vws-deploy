"""A fault-injecting GitHub fake for the orchestrator restart tests (vws-deploy#517 AC2, class 3: partial
failure and rerun recovery). Not a test module.

FaultGitHub is an in-memory GitHub behind orch.Gh's `runner` hook, so every request still goes through the
real Gh class (allowlist, issue-edit guard, one try per write). Reads answer from routes shaped like
stub_gh.py's ({"<path>": {"status": 200, "body": ...}}, a path with its query string wins over one
without, unknown GET paths answer []). Writes change that state the way GitHub does: a comment is added to
the issue and repo comment lists, a label is added or removed (removing an absent one answers 404 "Label
does not exist"), an issue body or state is edited, and every change bumps the issue's updated_at. The
Projects v2 board (board_read and board_write) is modelled when a `project` is given. A write this fake
does not model fails the test.

A fault (n, kind) acts on write number n (1-based, GraphQL mutations included):
- "crash_after": the write lands, then the process stops (SimulatedCrash), so nothing after it runs and
  nothing the tick keeps in memory is saved;
- "error_landed": the write lands, but the answer is a 502 (a timed-out request that went through);
- "error_lost": the write does not land and the answer is a 500.

restart_check() runs a scenario once cleanly, then for every n up to the clean run's write count and every
kind it runs the tick with that fault, runs it once more without one, and requires the GitHub end state to
equal the clean run's. tests/orchestrate/test_restart_lint.py requires a restart_check(covers=...) for every
orchestrate function that makes two or more GitHub writes.
"""
import copy
import hashlib
import json
import re
import subprocess

from orch_testlib import orch

O = f"repos/{orch.OWNER}"
FAULT_KINDS = ("crash_after", "error_landed", "error_lost")
_ISSUE_RE = re.compile(rf"^{O}/(vws-[a-z0-9-]+)/issues/([0-9]+)$")
_LIST_RE = re.compile(rf"^{O}/(vws-[a-z0-9-]+)/issues$")
_COMMENTS_RE = re.compile(rf"^{O}/(vws-[a-z0-9-]+)/issues/([0-9]+)/comments$")
_MUTATION_RE = re.compile(r"m([0-9]+): (addProjectV2ItemById|updateProjectV2ItemFieldValue|clearProjectV2ItemFieldValue)")


class SimulatedCrash(BaseException):
    """The process stopped here (SIGKILL, OOM, power loss). A BaseException, so no `except Exception`
    in the orchestrator can swallow it, the way nothing can catch a real kill."""


def _answer(status, body, headers=""):
    text = f"HTTP/2.0 {status} X\n{headers}\n{json.dumps(body)}" if body is not None else f"HTTP/2.0 {status} X\n{headers}\n"
    return subprocess.CompletedProcess([], 0 if status < 400 else 1, text, "")


class FaultGitHub:
    def __init__(self, routes, now, login=orch.OWNER, project=None):
        self.routes = copy.deepcopy(routes)
        self.now, self.login = now, login
        self.project = copy.deepcopy(project) if project else None
        self.fault = None  # (n, kind) or None
        self.fault_hit = False
        self.writes = 0  # write requests seen, faulted ones included
        self.clock = 0  # bumped by every landed change, so updated_at always moves
        self.next_id = 9_000_000

    # ---------------------------------------------------------------- transport (orch.Gh runner)
    def __call__(self, args, input=None, **_kw):
        if args[:4] == ["gh", "api", "-i", "graphql"]:
            req = json.loads(input or "{}")
            if req.get("query", "").lstrip().startswith("mutation"):
                return self._write(lambda: self._gql_write(req["query"], req.get("variables") or {}))
            return self._gql_read(req.get("query", ""), req.get("variables") or {})
        method = args[args.index("--method") + 1]
        path = args[args.index("--method") + 2]
        if method == "GET":
            inm = args[args.index("-H") + 1].split(":", 1)[1].strip() if "-H" in args else ""
            return self._get(path, inm)
        payload = json.loads(input) if input else None
        return self._write(lambda: self._rest_write(method, path, payload))

    def _write(self, land):
        self.writes += 1
        n, kind = self.fault or (0, "")
        if n != self.writes:
            return land()
        self.fault_hit = True
        if kind == "error_lost":
            return _answer(500, {"message": "injected: not landed"})
        out = land()
        if kind == "crash_after":
            raise SimulatedCrash(f"injected crash after write {n}")
        return _answer(502, {"message": f"injected: landed, answered {out.stdout.split(' ', 2)[1]} as 502"})

    def _get(self, path, inm):
        key = path.split("?", 1)[0]
        r = self.routes.get(path) or self.routes.get(key, {"status": 200, "body": []})
        status = r.get("status", 200)
        body = r["body"]
        if isinstance(body, list) and "state=open" in path and _LIST_RE.match(key):
            body = [i for i in body if not isinstance(i, dict) or i.get("state", "open") == "open"]
        etag = 'W/"' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16] + '"'
        if inm and inm == etag and status == 200:
            return _answer(304, None, f"Etag: {etag}\n")
        return _answer(status, body, f"Etag: {etag}\n")

    # ---------------------------------------------------------------- REST writes
    def _tick(self):
        self.clock += 1
        return orch.iso(self.now - 600 + self.clock)

    def _issue(self, repo, number):
        """The issue's route body, created from its list entry when only the list has it."""
        key = f"{O}/{repo}/issues/{number}"
        if key not in self.routes:
            entry = next((dict(i) for i in self._listed(repo) if i.get("number") == int(number)), None)
            if entry is None:
                raise AssertionError(f"fake GitHub: no issue {repo}#{number}")
            self.routes[key] = {"body": entry}
        return self.routes[key]["body"]

    def _listed(self, repo):
        for k, r in self.routes.items():
            if _LIST_RE.match(k.split("?", 1)[0]) and _LIST_RE.match(k.split("?", 1)[0]).group(1) == repo:
                yield from (i for i in r.get("body") or [] if isinstance(i, dict))

    def _edit(self, repo, number, fn):
        """Apply fn to the issue route and to every list entry of it; bump updated_at."""
        at = self._tick()
        targets = [self._issue(repo, number)] + [i for i in self._listed(repo) if i.get("number") == int(number)]
        for t in targets:
            fn(t)
            t["updated_at"] = at
        return at

    def _event(self, repo, number, kind, label, at):
        r = self.routes.setdefault(f"{O}/{repo}/issues/{number}/events", {"body": []})
        r["body"].append({"event": kind, "label": {"name": label}, "created_at": at})

    def _rest_write(self, method, path, payload):
        m = _COMMENTS_RE.match(path)
        if method == "POST" and m:
            repo, number = m.groups()
            at = self._edit(repo, number, lambda t: None)
            self.next_id += 1
            c = {"id": self.next_id, "html_url": f"https://github.com/{orch.OWNER}/{repo}/issues/{number}#issuecomment-{self.next_id}",
                 "issue_url": f"https://api.github.com/{O}/{repo}/issues/{number}", "created_at": at, "updated_at": at,
                 "body": payload["body"], "user": {"login": self.login, "id": 1}, "author_association": "OWNER"}
            self.routes.setdefault(path, {"body": []})["body"].append(c)
            self.routes.setdefault(f"{O}/{repo}/issues/comments", {"body": []})["body"].append(c)
            return _answer(201, c)
        m = re.match(rf"^{O}/(vws-[a-z0-9-]+)/issues/([0-9]+)/labels$", path)
        if method == "POST" and m:
            repo, number = m.groups()
            have = {lb["name"] for lb in self._issue(repo, number).get("labels") or []}
            new = [x for x in payload["labels"] if x not in have]
            if new:
                at = self._edit(repo, number, lambda t: t.__setitem__(
                    "labels", (t.get("labels") or []) + [{"name": x} for x in new]))
                for x in new:
                    self._event(repo, number, "labeled", x, at)
            return _answer(200, self._issue(repo, number).get("labels") or [])
        m = re.match(rf"^{O}/(vws-[a-z0-9-]+)/issues/([0-9]+)/labels/([^/]+)$", path)
        if method == "DELETE" and m:
            repo, number, label = m.groups()
            if label not in {lb["name"] for lb in self._issue(repo, number).get("labels") or []}:
                return _answer(404, {"message": "Label does not exist"})
            at = self._edit(repo, number, lambda t: t.__setitem__(
                "labels", [lb for lb in t.get("labels") or [] if lb["name"] != label]))
            self._event(repo, number, "unlabeled", label, at)
            return _answer(200, self._issue(repo, number).get("labels") or [])
        m = _ISSUE_RE.match(path)
        if method == "PATCH" and m:
            repo, number = m.groups()
            self._edit(repo, number, lambda t: t.update(payload))
            return _answer(200, self._issue(repo, number))
        raise AssertionError(f"fake GitHub does not model {method} {path}")

    # ---------------------------------------------------------------- GraphQL
    def _gql_read(self, query, variables):
        if query == orch.AC_EDIT_QUERY:
            r = self.routes.get("graphql", {"body": {"errors": [{"message": "no graphql route"}]}})
            return _answer(r.get("status", 200), r["body"])
        if query == orch.BOARD_QUERY and self.project is not None:
            p = self.project
            fields = [{"id": fid, "name": f["name"], "options": [{"id": oid, "name": on} for oid, on in f["options"].items()]}
                      for fid, f in sorted(p["fields"].items())]
            items = []
            for iid, it in sorted(p["items"].items()):
                vals = []
                for fid, v in sorted(it["values"].items()):
                    f = p["fields"][fid]
                    vals.append({"name": f["options"][v], "field": {"name": f["name"]}} if f["options"]
                                else {"text": v, "field": {"name": f["name"]}})
                content = p["content"][it["content"]]
                items.append({"id": iid, "content": {"url": content["url"], "issueState": content["state"]},
                              "fieldValues": {"nodes": vals}})
            node = {"fields": {"nodes": fields}, "items": {"nodes": items, "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            return _answer(200, {"data": {"rateLimit": {"remaining": 4000}, "node": node}})
        raise AssertionError(f"fake GitHub does not model GraphQL query {query[:40]!r}")

    def _gql_write(self, query, v):
        if self.project is None:
            raise AssertionError("fake GitHub: a board mutation without a project")
        p, data = self.project, {}
        for i, op in _MUTATION_RE.findall(query):
            if op == "addProjectV2ItemById":
                # GitHub answers an add of content already on the board with the existing item.
                content = v[f"c{i}"]
                iid = next((k for k, it in p["items"].items() if it["content"] == content), None)
                if iid is None:
                    iid = f"PVTI_{len(p['items']) + 1}"
                    p["items"][iid] = {"content": content, "values": {}}
                data[f"m{i}"] = {"item": {"id": iid}}
                continue
            item, fid = p["items"][v[f"i{i}"]], v[f"f{i}"]
            if op == "clearProjectV2ItemFieldValue":
                item["values"].pop(fid, None)
            else:
                item["values"][fid] = v[f"v{i}"]
            data[f"m{i}"] = {"clientMutationId": None}
        self.clock += 1
        return _answer(200, {"data": data})

    # ---------------------------------------------------------------- end state
    def end_state(self, ignore=()):
        """What a person sees on GitHub: each issue's state, state_reason, body and labels, each issue's
        comment bodies (a duplicate counts), and the board's items and values. Ids, times and events are
        left out; so are the issues in ignore (the status issue body is a report rewritten every tick)."""
        issues = {}
        for k, r in self.routes.items():
            key = k.split("?", 1)[0]
            m, lm = _ISSUE_RE.match(key), _LIST_RE.match(key)
            entries = [(m.group(1), r["body"])] if m and isinstance(r.get("body"), dict) else \
                [(lm.group(1), i) for i in r.get("body") or [] if isinstance(i, dict)] if lm else []
            for repo, i in entries:
                n = i.get("number") or int(m.group(2))
                if f"{repo}#{n}" in ignore:
                    continue
                cur = {"state": i.get("state"), "state_reason": i.get("state_reason"), "body": i.get("body"),
                       "labels": sorted(lb["name"] for lb in i.get("labels") or [])}
                if m or f"{repo}#{n}" not in issues:  # the issue route wins over a list entry
                    issues[f"{repo}#{n}"] = cur
        comments = {f"{m.group(1)}#{m.group(2)}": sorted(c.get("body") or "" for c in r["body"])
                    for k, r in self.routes.items() for m in [_COMMENTS_RE.match(k.split("?", 1)[0])]
                    if m and r.get("body") and f"{m.group(1)}#{m.group(2)}" not in ignore}
        board = None
        if self.project is not None:
            board = sorted((self.project["content"][it["content"]]["url"], sorted(it["values"].items()))
                           for it in self.project["items"].values())
        return {"issues": issues, "comments": comments, "board": board}


def restart_check(tc, scenario, covers, ignore=()):
    """Run scenario cleanly, then under every fault (n, kind) for n up to the clean run's write count, each
    followed by one fault-free rerun, and require the end state to match the clean run. covers names the
    orchestrate functions this exercises (test_restart_lint.py reads it).

    scenario() returns a fresh world: an object with .gh (a FaultGitHub) and .run() (one tick against it;
    state the tick saves to disk survives a crash, state it keeps in memory does not)."""
    if not covers or not all(isinstance(c, str) and c for c in covers):
        raise ValueError("covers must name the orchestrate functions under test")
    clean = scenario()
    clean.run()
    want = clean.gh.end_state(ignore)
    total = clean.gh.writes
    tc.assertGreaterEqual(total, 2, f"the scenario for {covers} must make at least two GitHub writes")
    before = clean.gh.writes
    clean.run()
    tc.assertEqual(clean.gh.end_state(ignore), want, "a second clean tick changes nothing")
    extra = clean.gh.writes - before
    for n in range(1, total + 1):
        for kind in FAULT_KINDS:
            with tc.subTest(covers=covers, write=n, of=total, fault=kind):
                w = scenario()
                w.gh.fault = (n, kind)
                try:
                    w.run()
                except SimulatedCrash:
                    pass
                tc.assertTrue(w.gh.fault_hit, f"write {n} never happened under the fault")
                w.gh.fault = None
                w.run()
                tc.assertEqual(w.gh.end_state(ignore), want)
    return {"writes": total, "rerun_writes": extra}
