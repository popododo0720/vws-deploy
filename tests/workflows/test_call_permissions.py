"""A caller job must grant every permission that any job in the file it calls declares (vws-deploy#402).

GitHub validates the permissions of every job in a called workflow when the run starts, before any if:
is evaluated, and a called job can only keep or lower what the caller job grants. A called file that also
holds a job with wider permissions than the caller grants fails the whole run with startup_failure and 0
jobs, so no required check is ever reported. This test reads the real files; caller_pin.CALLS (the shape
every other repo's caller must match) is checked against the same called files.

Run: python3 -m unittest discover -s tests/workflows
"""
import importlib.util
import os
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WFDIR = os.path.join(ROOT, ".github", "workflows")
LEVEL = {"none": 0, "read": 1, "write": 2}
SCOPES = ["actions", "attestations", "checks", "contents", "deployments", "discussions", "id-token", "issues",
          "models", "packages", "pages", "pull-requests", "repository-projects", "security-events", "statuses"]


def expand(perms):
    """A permissions value as {scope: level}; None means the job inherits (asks for nothing extra)."""
    if perms is None:
        return {}
    if perms in ("read-all", "write-all"):
        return {s: perms.split("-")[0] for s in SCOPES}
    if perms == {}:
        return {}
    return dict(perms)


def needed(path, seen=()):
    """{(job, scope): level} that the jobs of the called file at path declare, following nested calls."""
    wf = yaml.safe_load(open(path))
    top = wf.get("permissions")
    out = {}
    for name, job in (wf.get("jobs") or {}).items():
        for scope, level in expand(job.get("permissions", top)).items():
            out[(name, scope)] = level
        uses = job.get("uses", "")
        if uses.startswith("./") and uses not in seen:
            for (sub, scope), level in needed(os.path.join(ROOT, uses[2:]), seen + (uses,)).items():
                out[(f"{name} / {sub}", scope)] = level
    return out


def gaps(granted, path):
    granted = expand(granted)
    return sorted(f"{job} needs {scope}: {level}, the caller grants {granted.get(scope, 'none')}"
                  for (job, scope), level in needed(path).items()
                  if LEVEL[level] > LEVEL[granted.get(scope, "none")])


def local_calls(wf):
    for cid, job in (wf.get("jobs") or {}).items():
        uses = job.get("uses", "")
        if uses.startswith("./"):
            yield cid, job.get("permissions", wf.get("permissions")), os.path.join(ROOT, uses[2:])


class CallPermissions(unittest.TestCase):
    def test_every_local_caller_covers_its_called_file(self):
        found = 0
        for name in sorted(os.listdir(WFDIR)):
            if not name.endswith((".yml", ".yaml")):
                continue
            wf = yaml.safe_load(open(os.path.join(WFDIR, name)))
            for cid, granted, path in local_calls(wf):
                found += 1
                with self.subTest(workflow=name, job=cid):
                    self.assertEqual(gaps(granted, path), [])
        self.assertGreater(found, 0, "repo-guard.yml has no local calls")

    def test_pinned_caller_shape_covers_the_called_files(self):
        # Other repos must match caller_pin.CALLS exactly, so its permissions must cover each called file.
        spec = importlib.util.spec_from_file_location("caller_pin", os.path.join(ROOT, ".github", "drift", "caller_pin.py"))
        cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cp)
        for cid, call in cp.CALLS.items():
            with self.subTest(job=cid):
                self.assertEqual(gaps(call.permissions, os.path.join(ROOT, call.path)), [])

    def test_gaps_sees_a_wider_sibling_job(self):
        # The startup_failure of run 39904989710: one file, a read-only call, and a write job beside it.
        d = self.enterContext(__import__("tempfile").TemporaryDirectory())
        p = os.path.join(d, "x.yml")
        with open(p, "w") as f:
            f.write("on: workflow_call\npermissions: {}\njobs:\n"
                    "  a: {runs-on: x, permissions: {contents: read}, steps: [{run: 'true'}]}\n"
                    "  b: {runs-on: x, permissions: {issues: write}, steps: [{run: 'true'}]}\n")
        self.assertEqual(gaps({"contents": "read"}, p), ["b needs issues: write, the caller grants none"])
        self.assertEqual(gaps({"contents": "read", "issues": "write"}, p), [])
        self.assertEqual(gaps("write-all", p), [])


if __name__ == "__main__":
    unittest.main()
