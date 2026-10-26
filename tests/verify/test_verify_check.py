"""Behavior tests for .github/verify/verify-check.py, the verify checks of vws-deploy#517 AC4.

The fixtures are real: tests/verify/fixtures/real-411.json is the GitHub data of vws-deploy#484, #494 and
#500 and issue #411 (the cited "blocker carried into a rebuild" and "unmet AC" findings). The git history
is built in a temp dir so the FAIL review's file:line (orchestrate.py:2046-2047) points at the same text.
gh is tests/orchestrate/stub_gh.py; no network.

Run: python3 -m unittest discover -s tests/verify
"""
import copy
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github" / "verify" / "verify-check.py"
STUB = ROOT / "tests" / "orchestrate" / "stub_gh.py"
REAL = json.loads((pathlib.Path(__file__).parent / "fixtures" / "real-411.json").read_text())
R = "repos/popododo0720/vws-deploy"
# orchestrate.py:2046-2047 at the #494 FAIL head 31640e4 (the claim is any non-marker comment).
BLOCKER = ['        if kind == "commented" and at and not (e.get("body") or "").lstrip().startswith("<!--"):',
           '            act["comments"].append(at)']
FIXED = ['        if kind == "commented" and at and author in claimers and not (e.get("body") or "").lstrip().startswith("<!--"):',
         '            act["comments"].append(at)']
ISSUE_517_SHAPE = ("Follow-up of #439.\n\n## Still open (from #439 + verify notes)\n- [ ] Remaining AC2 classes\n"
                   "- [ ] AC3: shared workflow\n- [ ] AC4: project verification script\n")


def source(region):
    lines = [f"x_{i} = {i}" for i in range(1, 2101)]
    lines[2045:2047] = region
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = pathlib.Path(self.tmp.name)
        (self.t / "bin").mkdir()
        os.symlink(STUB, self.t / "bin" / "gh")
        self.routes = copy.deepcopy(REAL)
        self.log = self.t / "gh.log"

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, *args):
        rp = self.t / "routes.json"
        rp.write_text(json.dumps(self.routes))
        env = dict(os.environ, PATH=f"{self.t / 'bin'}:{os.environ['PATH']}", STUB_ROUTES=str(rp), STUB_LOG=str(self.log))
        r = subprocess.run(["python3", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)
        writes = [x for x in self.log.read_text().splitlines() if json.loads(x)["method"] != "GET"] if self.log.exists() else []
        self.assertEqual(writes, [], "verify-check must only read")
        return r

    def pr_body(self, n, body):
        self.routes[f"{R}/pulls/{n}"]["body"]["body"] = body


class TestAC(Base):
    def test_real_500_lists_each_claimed_box(self):
        r = self.run_check("ac", "--repo", "vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        checks = [x for x in r.stdout.splitlines() if x.startswith("CHECK ac: AC: ")]
        self.assertEqual(len(checks), 4)
        self.assertIn("no assignee/comment from the woken bot", checks[0])
        self.assertIn("PASS ac: AC: 1, 2, 3, 4 name existing boxes of vws-deploy#411", r.stdout)

    def test_box_beyond_the_issue_fails(self):
        self.pr_body(500, "Part of #411\n\nAC: 1\nAC: 5\n")
        r = self.run_check("ac", "--repo", "vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL ac: AC: 5 but vws-deploy#411 has 4 acceptance-criteria boxes", r.stdout)

    def test_box_number_on_an_issue_without_the_heading_fails(self):
        self.routes[f"{R}/issues/411"]["body"]["body"] = ISSUE_517_SHAPE
        self.pr_body(500, "Part of #411\n\nAC: 3\n")
        r = self.run_check("ac", "--repo", "vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("has no boxes under '## Acceptance criteria'", r.stdout)

    def test_ac_none_passes_and_lists_open_boxes(self):
        self.pr_body(500, "Part of #411\n\nAC: none - tooling only\n")
        r = self.run_check("ac", "--repo", "vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS ac: 'AC: none - tooling only' claims no box", r.stdout)
        self.assertEqual(r.stdout.count("INFO ac: box "), 4)

    def test_already_ticked_box_warns(self):
        b = self.routes[f"{R}/issues/411"]["body"]
        b["body"] = b["body"].replace("- [ ] Logs", "- [x] Logs")
        r = self.run_check("ac", "--repo", "vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("WARN ac: AC: 2 is already ticked", r.stdout)

    def test_invalid_ac_line_and_missing_part_of_fail(self):
        self.pr_body(500, "Part of #411\n\nAC: x\n")
        self.assertIn("FAIL ac: #500: 'AC: x' is not", self.run_check("ac", "--repo", "vws-deploy", "--pr", "500").stdout)
        self.pr_body(500, "AC: 1\n")
        self.assertIn("has no 'Part of #N' line", self.run_check("ac", "--repo", "vws-deploy", "--pr", "500").stdout)

    def test_foreign_owner_is_refused(self):
        r = self.run_check("ac", "--repo", "someone/vws-deploy", "--pr", "500")
        self.assertEqual(r.returncode, 2)


class TestRebuild(Base):
    def git(self, d, *a):
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, d, files, msg):
        for name, text in files.items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.git(d, "add", "-A")
        self.git(d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
        return self.git(d, "rev-parse", "HEAD")

    def history(self, rebuild_files):
        """main, the #494 FAIL head (adds the blocker), main moved on, the #500 head (cherry-pick + rebuild_files)."""
        origin, w = self.t / "origin.git", self.t / "w"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(w)], check=True)
        f = ".github/orchestrate/orchestrate.py"
        self.commit(w, {f: source(["pass  # a", "pass  # b"]), "docs/x.md": "doc\n"}, "base")
        self.git(w, "checkout", "-q", "-b", "v2")
        fail = self.commit(w, {f: source(BLOCKER), "tests/t.py": "claim test\n"}, "ci: claim")
        self.git(w, "checkout", "-q", "main")
        self.commit(w, {"other.txt": "main moved\n"}, "main moves")
        self.git(w, "checkout", "-q", "-b", "v3")
        self.git(w, "-c", "user.name=t", "-c", "user.email=t@t", "cherry-pick", fail)
        head = self.commit(w, rebuild_files, "rebuild") if rebuild_files else self.git(w, "rev-parse", "HEAD")
        self.git(w, "remote", "add", "origin", str(origin))
        self.git(w, "push", "-q", "origin", "main", f"{fail}:refs/pull/494/head", f"{head}:refs/pull/500/head")
        d = self.t / "checkout"
        subprocess.run(["git", "clone", "-q", str(origin), str(d)], check=True)
        self.routes[f"{R}/pulls/494"]["body"]["head"]["sha"] = fail
        for r in self.routes[f"{R}/pulls/494/reviews"]["body"]:
            r["commit_id"] = fail
        self.routes[f"{R}/pulls/500"]["body"]["head"]["sha"] = head
        for r in self.routes[f"{R}/pulls/500/reviews"]["body"]:
            r["commit_id"] = head  # the FAIL on the current head is not an earlier FAIL
        return d

    def rebuild(self, d):
        return self.run_check("rebuild", "--repo", "vws-deploy", "--pr", "500", "--dir", str(d))

    def test_real_500_carries_the_494_blocker(self):
        # #500: cherry-pick of FAIL head 31640e4 plus an unrelated code fix (the AC_BOX_RE rename).
        d = self.history({".github/orchestrate/orchestrate.py": source(BLOCKER).replace("x_10 = 10", "x_10 = 'renamed'")})
        r = self.rebuild(d)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("against the newest FAIL: #494", r.stdout)
        self.assertIn("PASS rebuild: non-docs changes differ", r.stdout)
        self.assertIn("FAIL rebuild: blocking item 1 of the FAIL review is carried unchanged: every cited span "
                      "(.github/orchestrate/orchestrate.py:2046-2047", r.stdout)

    def test_fixed_blocker_passes(self):
        d = self.history({".github/orchestrate/orchestrate.py": source(FIXED)})
        r = self.rebuild(d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS rebuild: blocking item 1: cited lines changed at the head "
                      "(.github/orchestrate/orchestrate.py:2046-2047)", r.stdout)

    def test_docs_only_rebuild_fails(self):
        d = self.history({"docs/x.md": "doc, now with a rebuild note\n", "README.md": "readme\n"})
        r = self.rebuild(d)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL rebuild: the PR's non-docs changes are the same as at FAIL head", r.stdout)

    def test_moved_blocker_still_counts_as_carried(self):
        moved = source(["pass  # a", "pass  # b"]).replace("x_5 = 5\n", "x_5 = 5\n" + "\n".join(BLOCKER) + "\n")
        r = self.rebuild(self.history({".github/orchestrate/orchestrate.py": moved}))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL rebuild: blocking item 1", r.stdout)

    def test_no_earlier_fail_passes(self):
        d = self.history(None)
        self.routes[f"{R}/pulls/494/reviews"]["body"] = []
        r = self.rebuild(d)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS rebuild: no vws-verify FAIL on an earlier head of #484, #494, #500", r.stdout)
        # #494's body says "-v2 of #475"; #475 has no route here (the stub answers []), so it is skipped once.
        self.assertEqual(r.stdout.count("WARN rebuild: skipped rebuild reference #475: #475 is not a pull request"), 1)

    def test_fail_from_another_login_is_ignored(self):
        d = self.history(None)
        for r in self.routes[f"{R}/pulls/494/reviews"]["body"]:
            r["user"]["login"] = "someone"
        self.assertIn("PASS rebuild: no vws-verify FAIL", self.rebuild(d).stdout)


class TestParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_check", SCRIPT)
        cls.vc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.vc)

    def test_real_500_review_items_and_spans(self):
        body = next(r["body"] for r in REAL[f"{R}/pulls/500/reviews"]["body"] if r["state"] == "CHANGES_REQUESTED")
        items = self.vc.blocking_items(body)
        self.assertEqual(len(items), 2)
        self.assertNotIn("recheck_grok", "".join(items))  # the Non-blocking section is left out
        p = ".github/orchestrate/orchestrate.py"
        self.assertEqual(self.vc.item_spans(items[0]), [(p, 2367, 2368), (p, 1702, 1704)])
        self.assertEqual(self.vc.item_spans(items[1]), [(p, 2372, 2376), (p, 1696, 1698), (p, 1702, 1702)])

    def test_times_and_urls_are_not_spans(self):
        self.assertEqual(self.vc.item_spans("run 16:59 at https://github.com/x/y:443 a.py:3"), [("a.py", 3, 3)])

    def test_rebuild_refs(self):
        text = "ci: x (-v3 of #494)\nRebuild (-v3 of #494, -v2 of #484). Supersedes #12, replaces #13. #9 is closed."
        self.assertEqual([m.group(1) for m in self.vc.REBUILD_RE.finditer(text)], ["494", "494", "484", "12", "13"])


if __name__ == "__main__":
    unittest.main()
