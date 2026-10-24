"""Rule E's rebuild job (vws-deploy#398): rebuild a conflicting PR onto main as a new -vN PR, never force-push.

Each test builds a local bare origin, a clone and a fake gh, then runs .github/orchestrate/rebuild_job.py
(and the vendored box/rebuild-pr.sh) as the box would. No network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
JOB = os.path.join(ROOT, ".github", "orchestrate", "rebuild_job.py")
REBUILD = os.path.join(ROOT, ".github", "orchestrate", "box", "rebuild-pr.sh")
REPO = "popododo0720/vws-iam"
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

# A fake gh: answers `gh api --method M PATH [--input -]` from $STUB_DIR/routes.json ("M PATH" keys),
# logs every call to $STUB_DIR/calls.jsonl. Unknown calls answer {} (a write) or 404 (a GET).
STUB = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
d = os.environ["STUB_DIR"]
m, p = a[a.index("--method") + 1], a[a.index("--method") + 2]
body = sys.stdin.read() if "--input" in a else ""
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": a, "method": m, "path": p, "body": json.loads(body) if body else None,
                        "gh_token_set": "GH_TOKEN" in os.environ}) + "\n")
r = json.load(open(os.path.join(d, "routes.json"))).get(f"{m} {p}")
if r is None:
    r = {"status": 404, "body": {"message": "Not Found"}} if m == "GET" else {"status": 200, "body": {}}
if r.get("status", 200) >= 400:
    sys.stderr.write(f"gh: HTTP {r['status']}\n"); sys.exit(1)
print(json.dumps(r["body"]))
'''


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd, *args], check=check, capture_output=True, text=True,
                          env=dict(os.environ, **IDENT)).stdout.strip()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rebuild-job-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.clone = os.path.join(self.tmp, "clone")
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        self.commit(seed, "a.txt", "one\ntwo\n", "base")
        git(seed, "remote", "add", "origin", self.origin)
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", "ci/feat")
        self.commit(seed, "b.txt", "feature\n", "ci: add b")
        self.commit(seed, "c.txt", "more\n", "ci: add c")
        git(seed, "push", "-q", "origin", "ci/feat")
        git(seed, "checkout", "-q", "main")
        self.seed = seed
        self.old_head = git(self.origin, "rev-parse", "ci/feat")
        subprocess.run(["git", "clone", "-q", self.origin, self.clone], check=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        with open(os.path.join(self.bin, "gh"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(self.bin, "gh"), 0o755)
        self.routes = {f"GET repos/{REPO}/pulls/7": {"body": self.pr()},
                       f"POST repos/{REPO}/pulls": {"body": {"number": 99, "html_url": f"https://github.com/{REPO}/pull/99"}}}

    def commit(self, repo, name, text, msg):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", msg)

    def advance_main(self, name="z.txt", text="main moved\n"):
        self.commit(self.seed, name, text, "ci: main moves")
        git(self.seed, "push", "-q", "origin", "main")

    def pr(self, **kw):
        pr = {"number": 7, "state": "open", "draft": False, "title": "ci: add b and c",
              "body": "Part of #12\n\nAgent: vws-platform\n\nDetails.", "labels": [],
              "base": {"ref": "main"}, "head": {"ref": "ci/feat", "sha": self.old_head if hasattr(self, "old_head") else "",
                                                "repo": {"full_name": REPO}}}
        pr.update(kw)
        return pr

    def run_job(self, *extra, job="job-1"):
        with open(os.path.join(self.tmp, "routes.json"), "w") as f:
            json.dump(self.routes, f)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], STUB_DIR=self.tmp, GH_TOKEN="x",
                   **IDENT)
        return subprocess.run([sys.executable, JOB, "--repo", REPO, "--pr", "7", "--clone", self.clone,
                               "--job-id", job, *extra], capture_output=True, text=True, env=env)

    def calls(self):
        p = os.path.join(self.tmp, "calls.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(x) for x in f]

    def writes(self):
        return [(c["method"], c["path"]) for c in self.calls() if c["method"] != "GET"]

    def origin_refs(self):
        return sorted(git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())

    def assert_clean_clone(self):
        self.assertFalse(os.path.exists(os.path.join(self.clone, ".worktrees", "job-1")), "the job worktree is removed")
        self.assertEqual(git(self.clone, "worktree", "list", "--porcelain").count("worktree "), 1)
        self.assertEqual([b for b in git(self.clone, "branch", "--format=%(refname:short)").split() if "-v" in b], [])


class Clean(Fixture):
    def test_clean_pick_opens_a_new_pr_and_closes_the_old_one(self):
        self.advance_main()
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]),
                         {"result": "rebuilt", "repo": REPO, "pr": 7, "new_pr": 99, "branch": "ci/feat-v2",
                          "base": git(self.origin, "rev-parse", "main")})
        self.assertEqual(self.origin_refs(), ["ci/feat", "ci/feat-v2", "main"])
        self.assertEqual(git(self.origin, "rev-parse", "ci/feat"), self.old_head, "the old branch is untouched")
        self.assertEqual(git(self.origin, "log", "--format=%s", "main..ci/feat-v2").splitlines(), ["ci: add c", "ci: add b"])
        self.assertEqual(git(self.origin, "rev-parse", "ci/feat-v2~2"), git(self.origin, "rev-parse", "main"))
        self.assertEqual(self.writes(), [("POST", f"repos/{REPO}/pulls"), ("POST", f"repos/{REPO}/issues/7/comments"),
                                         ("PATCH", f"repos/{REPO}/pulls/7")])
        create, comment, close = [c["body"] for c in self.calls() if c["method"] != "GET"]
        self.assertEqual({k: create[k] for k in ("title", "head", "base")},
                         {"title": "ci: add b and c", "head": "ci/feat-v2", "base": "main"})
        self.assertTrue(create["body"].startswith("Part of #12\n\nAgent: vws-platform\n\nDetails."), create["body"])
        self.assertIn("Supersedes #7", create["body"])
        self.assertTrue(comment["body"].startswith("Superseded by #99"), comment)
        self.assertEqual(close, {"state": "closed"})
        self.assertFalse(any(c["gh_token_set"] for c in self.calls()), "gh uses the box's stored login, not GH_TOKEN")
        self.assert_clean_clone()

    def test_next_free_version(self):
        git(self.seed, "push", "-q", "origin", "ci/feat:refs/heads/ci/feat-v2", "ci/feat:refs/heads/ci/feat-v4")
        self.advance_main()
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["branch"], "ci/feat-v5")

    def test_a_vn_branch_gets_the_next_version_of_its_stem(self):
        git(self.seed, "push", "-q", "origin", "ci/feat:refs/heads/ci/feat-v3")
        self.routes[f"GET repos/{REPO}/pulls/7"]["body"]["head"]["ref"] = "ci/feat-v3"
        self.advance_main()
        r = self.run_job()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["branch"], "ci/feat-v4")

    def test_dry_run_writes_nothing(self):
        self.advance_main()
        r = self.run_job("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["result"], "would_rebuild")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.origin_refs(), ["ci/feat", "main"])
        self.assert_clean_clone()

    def test_pr_create_failure_stops_before_closing(self):
        self.advance_main()
        self.routes[f"POST repos/{REPO}/pulls"] = {"status": 422}
        r = self.run_job()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.writes(), [("POST", f"repos/{REPO}/pulls")], "the old PR stays open")
        self.assertIn("ci/feat-v2", r.stderr, "the pushed branch is reported for a person to clean up")
        self.assert_clean_clone()


class Conflict(Fixture):
    def test_conflict_labels_needs_rebuild_and_pushes_nothing(self):
        git(self.seed, "checkout", "-q", "main")
        self.commit(self.seed, "b.txt", "main's own b\n", "ci: main adds b")
        git(self.seed, "push", "-q", "origin", "main")
        r = self.run_job()
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["result"], "conflict")
        self.assertEqual(self.writes(), [("POST", f"repos/{REPO}/issues/7/labels")])
        self.assertEqual([c["body"] for c in self.calls() if c["method"] == "POST"], [{"labels": ["needs-rebuild"]}])
        self.assertEqual(self.origin_refs(), ["ci/feat", "main"], "no push on a conflict")
        self.assert_clean_clone()


class Skip(Fixture):
    def test_prs_the_job_never_touches(self):
        cases = {
            "blocked": {"labels": [{"name": "blocked"}]},
            "needs-rebuild already": {"labels": [{"name": "needs-rebuild"}]},
            "closed": {"state": "closed"},
            "draft": {"draft": True},
            "base not main": {"base": {"ref": "release"}},
            "fork": {"head": {"ref": "ci/feat", "sha": self.old_head, "repo": {"full_name": "someone/vws-iam"}}},
            "head moved": {"head": {"ref": "ci/feat", "sha": "f" * 40, "repo": {"full_name": REPO}}},
        }
        self.advance_main()
        for why, change in cases.items():
            with self.subTest(why):
                self.routes[f"GET repos/{REPO}/pulls/7"] = {"body": self.pr(**change)}
                open(os.path.join(self.tmp, "calls.jsonl"), "w").close()
                r = self.run_job()
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertEqual(json.loads(r.stdout.splitlines()[-1])["result"], "skipped")
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.origin_refs(), ["ci/feat", "main"])
                self.assert_clean_clone()

    def test_bad_arguments(self):
        for job in ("../x", "a/b", "", "-x"):
            with self.subTest(job=job):
                self.assertEqual(self.run_job(job=job).returncode, 2)
        self.assertEqual(self.calls(), [])


class Auth(unittest.TestCase):
    def test_git_and_gh_use_the_stored_gh_login(self):
        sys.path.insert(0, os.path.dirname(JOB))
        import rebuild_job
        old = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(old)))
        os.environ.update(GH_TOKEN="x", GITHUB_TOKEN="y", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.pager")
        e = rebuild_job.env()
        self.assertNotIn("GH_TOKEN", e)
        self.assertNotIn("GITHUB_TOKEN", e)
        self.assertEqual([(e[f"GIT_CONFIG_KEY_{i}"], e[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(e["GIT_CONFIG_COUNT"]))],
                         [("credential.helper", ""), ("credential.helper", "!gh auth git-credential")])
        self.assertEqual(e["GIT_TERMINAL_PROMPT"], "0")


class NoForce(unittest.TestCase):
    def test_the_job_and_script_never_force(self):
        for path in (JOB, REBUILD):
            src = open(path).read()
            for bad in ("--force", "+refs/", "push -f", '"-f"', "update-ref -d", "--delete", "reset --hard"):
                # The only --force allowed is removing the job's own local worktree.
                local = src.replace('"worktree", "remove", "--force"', "").replace("worktree remove --force", "")
                self.assertNotIn(bad, local, (path, bad))


class RebuildScript(Fixture):
    def sh(self, *args):
        return subprocess.run(["bash", REBUILD, "--repo-dir", self.clone, *args], capture_output=True, text=True,
                              env=dict(os.environ, **IDENT))

    def test_new_branch_must_be_versioned(self):
        r = self.sh("--old", "ci/feat", "--new", "ci/feat2", "--worktree", os.path.join(self.tmp, "w"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("-vN", r.stderr)

    def test_existing_worktree_path_is_refused(self):
        r = self.sh("--old", "ci/feat", "--new", "ci/feat-v2", "--worktree", self.tmp)
        self.assertEqual(r.returncode, 2)

    def test_dry_run_lists_the_commits(self):
        self.advance_main()
        git(self.clone, "fetch", "-q", "origin")
        r = self.sh("--old", "ci/feat", "--new", "ci/feat-v2", "--worktree", os.path.join(self.tmp, "w"), "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(git(self.origin, "rev-parse", "ci/feat~1"), r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "w")))


if __name__ == "__main__":
    unittest.main()
