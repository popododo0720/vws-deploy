"""vws-deploy#348 part 1: spec_state() and the collect() spec reads. ORCHESTRATE_SPEC_FIRST defaults to off,
and decide() does not read the state yet (part 2), so nothing here changes a tick's plan."""
import hashlib
import json
import unittest

import test_runtime
from orch_testlib import CFG, NOW, item, orch

O = test_runtime.O
AC = "## Acceptance criteria\n- [ ] `make check` exits 0\n\n"
PR = "https://github.com/popododo0720/vws-deploy/pull/400"
SHA = "a" * 40
COMMIT = f"https://github.com/popododo0720/vws-deploy/commit/{SHA}"


def task(body):
    return item("vws-iam", 1, labels=("ready", "deepseek"), body=body)


class SpecState(unittest.TestCase):
    def state(self, body, info=None):
        return orch.spec_state(task(body), info)

    def test_no_ac_section(self):
        self.assertEqual(self.state(f"Spec: {PR}\n", {"ci": "failing"}), "missing_ac")

    def test_ac_heading_without_checkbox(self):
        self.assertEqual(self.state(f"## Acceptance criteria\nmake check passes\n## Notes\n- [ ] x\nSpec: {PR}\n",
                                    {"ci": "failing"}), "missing_ac")

    def test_ac_level_three_heading(self):
        self.assertEqual(self.state(f"### Acceptance criteria\n- [ ] x\n\nSpec: {PR}\n", {"ci": "failing"}), "ok")

    def test_no_spec_line(self):
        self.assertEqual(self.state(AC), "missing_spec")

    def test_spec_by_trailer_is_not_a_spec_line(self):
        self.assertEqual(self.state(AC + "Spec-by: grok\n"), "missing_spec")

    def test_spec_pr_red_is_ok(self):
        self.assertEqual(self.state(AC + f"Spec: {PR}\n", {"ci": "failing"}), "ok")

    def test_spec_pr_green_is_not_red(self):
        self.assertEqual(self.state(AC + f"Spec: {PR}\n", {"ci": "ok"}), "spec_not_red")

    def test_spec_commit_red_is_ok(self):
        self.assertEqual(self.state(AC + f"Spec: {COMMIT}\n", {"ci": "failing"}), "ok")

    def test_spec_unreadable(self):
        self.assertEqual(self.state(AC + f"Spec: {PR}\n", {"unreadable": True}), "spec_unreadable")

    def test_spec_not_read_outside_the_repos(self):
        # collect never reads a spec outside its repos, so spec_info is None.
        body = AC + "Spec: https://github.com/popododo0720/vws-gone/pull/3\n"
        self.assertEqual(self.state(body, None), "spec_unreadable")

    def test_spec_outside_the_owner(self):
        self.assertEqual(self.state(AC + "Spec: https://github.com/other/vws-deploy/pull/3\n", {"ci": "failing"}),
                         "spec_unreadable")

    def test_malformed_and_duplicate_spec_lines(self):
        for body in (AC + f"Spec: {PR}/files\n", AC + f"Spec: {COMMIT[:-1]}\n", AC + "Spec: see the PR\n",
                     AC + f"Spec: {PR}\nSpec: {COMMIT}\n", AC + "Spec:\n"):
            with self.subTest(body=body):
                self.assertEqual(self.state(body, {"ci": "failing"}), "spec_unreadable")

    def test_each_listed_exempt_reason(self):
        for reason in ("docs", "ci", "config", "revert", "live-only", "test-in-ac"):
            with self.subTest(reason=reason):
                self.assertEqual(self.state(AC + f"Spec: exempt ({reason}): no unit seam\n"), "exempt")

    def test_unknown_or_incomplete_exempt(self):
        for line in ("exempt (lazy): no time", "exempt (docs):", "exempt (docs)", "exempt: docs", "exempt (DOCS): x"):
            with self.subTest(line=line):
                self.assertEqual(self.state(AC + f"Spec: {line}\n"), "bad_exempt")

    def test_parse_spec_crlf_and_case(self):
        self.assertEqual(orch.parse_spec(f"x\r\n  spec: {PR}  \r\ny"),
                         {"kind": "pr", "url": PR, "repo": "vws-deploy", "ref": "400", "reason": ""})

    def test_states_are_listed(self):
        self.assertEqual(set(orch.SPEC_STATES), {"ok", "exempt", "missing_ac", "missing_spec", "bad_exempt",
                                                 "spec_unreadable", "spec_not_red"})


class Config(test_runtime.Env):
    def test_default_is_off(self):
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_SPEC_FIRST"], "off")

    def test_bad_mode_fails_closed(self):
        for v in ("on", "Warn", ""):
            with self.subTest(v=v), self.assertRaises(ValueError):
                orch.load_config(self.paths, {"ORCHESTRATE_SPEC_FIRST": v})
        for v in ("off", "warn", "enforce"):
            self.assertEqual(orch.load_config(self.paths, {"ORCHESTRATE_SPEC_FIRST": v})["ORCHESTRATE_SPEC_FIRST"], v)


class CollectSpecs(test_runtime.Env):
    REPOS = ["vws-deploy", "vws-iam"]

    def setup(self, body, red=True, extra=None):
        r = test_runtime.routes()
        labels = [{"name": "ready"}, {"name": "deepseek"}]
        r[f"{O}/vws-iam/issues"] = {"body": [{"number": 1, "html_url": "https://github.com/popododo0720/vws-iam/issues/1",
                                              "state": "open", "created_at": "2026-10-01T00:00:00Z", "body": body,
                                              "labels": labels}]}
        conclusion = "failure" if red else "success"
        for sha in (f"{400:040x}", SHA, "b" * 40):
            r[f"{O}/vws-deploy/commits/{sha}/check-runs"] = {"body": {"total_count": 1, "check_runs": [
                {"id": 1, "name": "test", "status": "completed", "conclusion": conclusion}]}}
            r[f"{O}/vws-deploy/commits/{sha}/status"] = {"body": {"state": "success", "statuses": []}}
        r.update(extra or {})
        self.set_routes(r)

    def collect(self, mode):
        open(self.calls_file, "w").close()
        gh = orch.Gh()
        snap = orch.collect(gh, self.REPOS, NOW, dict(CFG, ORCHESTRATE_SPEC_FIRST=mode), "run")
        return snap, [(c["method"], c["path"]) for c in self.calls()]

    def test_off_reads_nothing_more(self):
        # A spec on a commit nothing else reads: off must not read it, so off's request list is today's.
        self.setup(AC + f"Spec: {COMMIT}\n")
        snap, calls = self.collect("off")
        self.assertNotIn("specs", snap)
        self.assertFalse([c for c in calls if SHA in c[1]])

    def test_open_spec_pr_costs_two_ci_reads(self):
        self.setup(AC + f"Spec: {PR}\n")
        _, off = self.collect("off")
        snap, warn = self.collect("warn")
        self.assertEqual(snap["specs"], {PR: {"ci": "failing"}})
        extra = list(warn)
        for c in off:
            extra.remove(c)
        # PR 400's head is already read for its own verify labels; the spec adds the same two CI reads again,
        # which the ETag cache answers 304 on a real tick.
        self.assertEqual(sorted(extra), [("GET", f"{O}/vws-deploy/commits/{400:040x}/check-runs?per_page=100"),
                                         ("GET", f"{O}/vws-deploy/commits/{400:040x}/status")])
        self.assertTrue(all(m == "GET" for m, _ in warn))
        self.assertEqual(orch.spec_state(snap["items"]["vws-iam#1"], snap["specs"][PR]), "ok")

    def test_green_spec_commit(self):
        self.setup(AC + f"Spec: {COMMIT}\n", red=False)
        snap, _ = self.collect("enforce")
        self.assertEqual(snap["specs"], {COMMIT: {"ci": "ok"}})
        self.assertEqual(orch.spec_state(snap["items"]["vws-iam#1"], snap["specs"][COMMIT]), "spec_not_red")

    def test_closed_spec_pr_reads_its_head(self):
        url = "https://github.com/popododo0720/vws-deploy/pull/77"
        self.setup(AC + f"Spec: {url}\n", extra={f"{O}/vws-deploy/pulls/77": {"body": {"head": {"sha": "b" * 40}}}})
        snap, calls = self.collect("warn")
        self.assertEqual(snap["specs"], {url: {"ci": "failing"}})
        self.assertIn(("GET", f"{O}/vws-deploy/pulls/77"), calls)

    def test_unreadable_spec_never_fails_the_tick(self):
        url = "https://github.com/popododo0720/vws-deploy/pull/78"
        for route in ({"status": 404, "body": {"message": "Not Found"}}, {"body": {"no": "head"}}):
            with self.subTest(route=route):
                self.setup(AC + f"Spec: {url}\n", extra={f"{O}/vws-deploy/pulls/78": route})
                snap, _ = self.collect("warn")
                self.assertEqual(snap["specs"], {url: {"unreadable": True}})

    def test_spec_outside_the_repos_is_not_read(self):
        url = "https://github.com/popododo0720/vws-gone/pull/3"
        self.setup(AC + f"Spec: {url}\n")
        snap, calls = self.collect("warn")
        self.assertEqual(snap["specs"], {})
        self.assertFalse([c for c in calls if "vws-gone" in c[1]])
        self.assertEqual(orch.spec_state(snap["items"]["vws-iam#1"], snap["specs"].get(url)), "spec_unreadable")

    def test_every_spec_read_is_allowlisted(self):
        self.setup(AC + "Spec: https://github.com/popododo0720/vws-deploy/pull/77\n",
                   extra={f"{O}/vws-deploy/pulls/77": {"body": {"head": {"sha": "b" * 40}}}})
        _, calls = self.collect("warn")
        self.assertTrue(all(orch.allowed(m, p) for m, p in calls))


class AllowlistUnchanged(unittest.TestCase):
    """AC 3: the spec reads need no new ALLOWED entry. The digest is ALLOWED on main c56fb64; a PR that
    changes the allowlist changes this pin and says why."""

    def test_allowed_equals_main(self):
        table = json.dumps({k: [p.pattern for p in v] for k, v in sorted(orch.ALLOWED.items())}, sort_keys=True)
        self.assertEqual(hashlib.sha256(table.encode()).hexdigest(),
                         "9a5dec567b0ea404758df8fc16fe3ba0e4805868ae1cc507aba7a7aa26793df4")


if __name__ == "__main__":
    unittest.main()
