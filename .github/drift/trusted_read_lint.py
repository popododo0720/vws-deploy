#!/usr/bin/env python3
"""Class 1 lint (vws-deploy#439 AC2): untrusted or stale GitHub state. Python 3 stdlib only, no network.

It fails (exit 1, one `path:line: rule: why` line per finding) when:

- event-labels: a workflow reads `github.event.pull_request.labels`. That is the event snapshot, and a
  re-run replays it; read the live labels with `gh api repos/.../issues/N/labels`.
- committer: a workflow reads `head_commit.committer`. Anyone can set a commit's committer; use the
  pusher from the repository activity API (repo-guard-verify-gate.yml).
- marker-match: .github/orchestrate/orchestrate.py matches a marker outside the trusted-read module
  .github/orchestrate/trusted_read.py (an AST check). A marker is a `<!-- vws-orchestrate:`,
  `<!-- orchestrate:` or `<!-- vws-ci-fail` literal or an `Idempotency key: ` literal, a name ending in
  `_MARKER`, a call of a function ending in `_marker`, or a marker regex that trusted_read.py compiles.
  A match is `in`/`not in`/`==`/`!=` with a marker operand, a str method that searches (`startswith`,
  `split`, `count`, `index`...) with a marker receiver or argument, a `re.*` call with a marker argument,
  or a search method of a marker regex. Building a comment that contains a marker is not a match.

Usage: trusted_read_lint.py [--root DIR]
"""
import argparse
import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ORCHESTRATE = os.path.join(".github", "orchestrate", "orchestrate.py")
TRUSTED = os.path.join(".github", "orchestrate", "trusted_read.py")
WORKFLOWS = os.path.join(".github", "workflows")

MARKER_LIT = re.compile(r"<!--\s*/?(?:vws-orchestrate:|orchestrate:|vws-ci-fail\b)|Idempotency key: ")
WORKFLOW_RULES = (
    ("event-labels", re.compile(r"\bpull_request(?:['\"]\s*\])?\s*(?:\.|\[\s*['\"])labels\b"),
     "reads the event snapshot of the PR labels; read the live labels (gh api repos/<repo>/issues/<n>/labels)"),
    ("committer", re.compile(r"\bhead_commit(?:['\"]\s*\])?\s*(?:\.|\[\s*['\"])committer\b"),
     "trusts the commit committer, which anyone can set; use the pusher from the repository activity API"),
)
STR_SEARCH = {"startswith", "endswith", "find", "rfind", "index", "rindex", "count", "split", "rsplit",
              "partition", "rpartition", "removeprefix", "removesuffix"}
RE_FUNCS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split", "escape"}
REGEX_METHODS = {"search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split", "scanner"}
MATCH_OPS = (ast.In, ast.NotIn, ast.Eq, ast.NotEq)


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def is_marker(node, marker_res):
    """True when node is, or is built from, a marker (see the module docstring)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(MARKER_LIT.search(node.value))
    if isinstance(node, (ast.Name, ast.Attribute)):
        n = _name(node)
        return n.endswith("_MARKER") or n in marker_res
    if isinstance(node, ast.Call):
        return _name(node.func).endswith("_marker")
    if isinstance(node, ast.JoinedStr):
        return any(is_marker(v, marker_res) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return is_marker(node.value, marker_res)
    if isinstance(node, ast.BinOp):
        return is_marker(node.left, marker_res) or is_marker(node.right, marker_res)
    return False


def _contains_marker(node, marker_res):
    return any(is_marker(n, marker_res) for n in ast.walk(node))


def marker_regexes(trusted_src):
    """Names that trusted_read.py binds to re.compile(<marker>)."""
    out = set()
    for node in ast.parse(trusted_src).body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and _name(node.value.func) == "compile" and _contains_marker(node.value, set())):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return out


def marker_matches(path, src, marker_res):
    """A finding for every marker match in src."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare) and any(isinstance(op, MATCH_OPS) for op in node.ops):
            if any(is_marker(x, marker_res) for x in [node.left] + node.comparators):
                out.append((node.lineno, "compares text with a marker"))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, recv = node.func.attr, node.func.value
            args = list(node.args) + [k.value for k in node.keywords]
            if isinstance(recv, ast.Name) and recv.id == "re" and attr in RE_FUNCS:
                if any(_contains_marker(a, marker_res) for a in args):
                    out.append((node.lineno, f"re.{attr} with a marker"))
            elif attr in REGEX_METHODS and _name(recv) in marker_res:
                out.append((node.lineno, f"{_name(recv)}.{attr} matches a marker regex"))
            elif attr in STR_SEARCH and (is_marker(recv, marker_res) or any(is_marker(a, marker_res) for a in args)):
                out.append((node.lineno, f".{attr}() with a marker"))
    return [f"{path}:{line}: marker-match: {what} outside {TRUSTED}; add a function there"
            for line, what in sorted(set(out))]


def lint_workflow(path, text):
    out = []
    for i, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        for rule, rx, why in WORKFLOW_RULES:
            if rx.search(line):
                out.append(f"{path}:{i}: {rule}: {why}")
    return out


def lint(root):
    findings = []
    for path in sorted(glob.glob(os.path.join(root, WORKFLOWS, "*.yml")) + glob.glob(os.path.join(root, WORKFLOWS, "*.yaml"))):
        with open(path, encoding="utf-8") as f:
            findings += lint_workflow(os.path.relpath(path, root), f.read())
    try:
        with open(os.path.join(root, TRUSTED), encoding="utf-8") as f:
            res = marker_regexes(f.read())
    except FileNotFoundError:
        res = set()
        findings.append(f"{TRUSTED}:0: trusted-read-missing: the one module that may match markers does not exist")
    with open(os.path.join(root, ORCHESTRATE), encoding="utf-8") as f:
        src = f.read()
    findings += marker_matches(ORCHESTRATE, src, res)
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args(argv)
    findings = lint(args.root)
    for f in findings:
        print(f)
    if findings:
        print(f"trusted_read_lint: {len(findings)} finding(s) (vws-deploy#439 class 1)", file=sys.stderr)
        return 1
    print("trusted_read_lint: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
