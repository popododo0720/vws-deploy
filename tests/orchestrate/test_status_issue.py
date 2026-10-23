"""vws-deploy#416: status lives in the BODY of the status issue (default vws-deploy#415), edited in place
only when its content changes; no status comments anywhere; verify-wake never posts on the status issue."""
import glob
import json
import os
import re
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot
from test_runtime import O, STATUS_SEED, Env, routes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
STATUS_PATH = f"{O}/vws-deploy/issues/415"
EXCL = {"repos": set(), "items": set()}
START, END = "<!-- vws-orchestrate:status -->", "<!-- /vws-orchestrate:status -->"
# Human text around the managed block; it must survive every write byte for byte.
HEAD = "Read me first: notes for people.\r\n\n- keep `this`\n\n"
TAIL = "\n## Human notes\n\n_Updated by hand_\n## Failed ticks\nnot the orchestrator's\n<!-- note -->"


def prs(n):
    return [item("vws-deploy", 400 + i, is_pr=True) for i in range(n)]


def decide(snap, cfg=CFG, state=None):
    return orch.decide(snap, state or {}, NOW, cfg, EXCL)


class Config(unittest.TestCase):
    def test_no_hardcoded_old_status_thread(self):
        files = [os.path.join(ROOT, ".github", "orchestrate", "orchestrate.py"),
                 os.path.join(ROOT, ".github", "metrics", "metrics.py")]
        files += glob.glob(os.path.join(ROOT, ".github", "orchestrate", "box", "*"))
        for path in files:
            with open(path, encoding="utf-8") as f:
                hits = [n for n, line in enumerate(f, 1) if re.search(r"\b339\b", line)]
            self.assertEqual(hits, [], f"{os.path.relpath(path, ROOT)} names #339; the issue number comes from config")

    def test_defaults(self):
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_STATUS_ISSUE"], "415")
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_WAKE_THREAD"], "", "no wake thread is built in")
        self.assertNotIn("ORCHESTRATE_STATUS_PR", orch.DEFAULTS)
        self.assertEqual(orch.status_target(orch.DEFAULTS), "vws-deploy#415")
        self.assertEqual(orch.wake_target(orch.DEFAULTS), "")

    def test_wake_thread_may_not_be_the_status_issue(self):
        paths = orch.Paths({"ORCHESTRATE_STATE_DIR": "/nonexistent/s", "ORCHESTRATE_CONFIG_DIR": "/nonexistent/c"})
        with self.assertRaisesRegex(ValueError, "verify-wake never posts there"):
            orch.load_config(paths, {"ORCHESTRATE_WAKE_THREAD": "415"})
        with self.assertRaisesRegex(ValueError, "verify-wake never posts there"):
            orch.load_config(paths, {"ORCHESTRATE_WAKE_THREAD": "20", "ORCHESTRATE_STATUS_ISSUE": "20"})
        cfg = orch.load_config(paths, {"ORCHESTRATE_WAKE_THREAD": "339"})
        self.assertEqual((orch.wake_target(cfg), orch.status_target(cfg)), ("vws-deploy#339", "vws-deploy#415"))


class Decide(unittest.TestCase):
    def test_wake_goes_to_the_wake_thread_never_the_status_issue(self):
        r = decide(snapshot(*prs(5)))
        wakes = [a["target"] for a in r["actions"] if a["type"] == "wake_verify"]
        self.assertEqual(wakes, ["vws-deploy#339"])
        self.assertNotIn(orch.status_target(CFG), [a["target"] for a in r["actions"] if a["type"] == "wake_verify"])

    def test_no_wake_thread_no_wake(self):
        r = decide(snapshot(*prs(5)), dict(CFG, ORCHESTRATE_WAKE_THREAD=""))
        self.assertEqual([a for a in r["actions"] if a["type"] == "wake_verify"], [])
        self.assertTrue(any(n["kind"] == "wake_off" for n in r["notes"]))

    def test_body_entry_key_is_not_reported_again(self):
        snap = snapshot(*prs(1))
        state = {"status_events": [{"key": "sha256:" + "a" * 64, "at": NOW, "kind": "infra", "text": "x"}]}
        self.assertIn("sha256:" + "a" * 64, orch.status_keys(snap, state))
        snap["status_issue"] = {"body": "- infra: y (key `sha256:" + "b" * 64 + "`)\n"}
        self.assertIn("sha256:" + "b" * 64, orch.status_keys(snap, {}))


class Body(unittest.TestCase):
    def render(self, now=NOW, state=None, failure="", cfg=CFG):
        snap = snapshot(*prs(5))
        return orch.status_body(snap, decide(snap, cfg), cfg, state or {}, "apply", now, failure)

    def test_render_is_deterministic_and_only_the_updated_line_moves(self):
        a, b = self.render(), self.render(now=NOW + 3600)
        self.assertEqual(a, self.render())
        self.assertNotEqual(a, b)
        self.assertTrue(orch.status_same(a, b), "a quiet tick changes only the _Updated_ line")
        self.assertTrue(orch.status_same(a.replace("\n", "\r\n"), b), "line endings from the web editor do not count")
        self.assertTrue(a.startswith(orch.STATUS_MARKER + "\n"))
        self.assertIn("verify-wake does not post here", a)
        self.assertIn("PRs waiting for verify: 5", a)
        self.assertIn("verify wakes go to vws-deploy#339", a)
        self.assertIn("verify wake is off", self.render(cfg=dict(CFG, ORCHESTRATE_WAKE_THREAD="")))

    def test_entries_and_failure_section_change_the_body(self):
        a = self.render()
        ev = {"status_events": [{"key": "sha256:" + "c" * 64, "at": NOW, "kind": "merge", "text": "main CI red in vws-deploy at 1234567"}]}
        b = self.render(state=ev)
        self.assertFalse(orch.status_same(a, b))
        self.assertIn("merge: main CI red in vws-deploy at 1234567 (key `sha256:" + "c" * 64 + "`)", b)
        c = self.render(state=ev, failure="## Failed ticks\n\nboom")
        self.assertFalse(orch.status_same(b, c))
        self.assertEqual(orch.failure_section(c), "## Failed ticks\n\nboom")
        self.assertEqual(orch.failure_section(orch.set_failure_section(c, "## Failed ticks\n\nnew")), "## Failed ticks\n\nnew")

    def test_events_are_bounded_and_one_per_key(self):
        state = {}
        for i in range(orch.STATUS_EVENTS_KEEP + 5):
            orch.add_status_event(state, {"type": "infra_status", "key": f"k{i}", "reason": "r"}, NOW + i)
        orch.add_status_event(state, {"type": "merge_status", "key": "k30", "reason": "again"}, NOW + 99)
        self.assertEqual(len(state["status_events"]), orch.STATUS_EVENTS_KEEP)
        self.assertEqual(sum(e["key"] == "k30" for e in state["status_events"]), 1)


class Block(unittest.TestCase):
    """vws-deploy#431 verdict: the orchestrator owns only the text between the two markers."""

    def test_rendered_block_is_delimited(self):
        snap = snapshot(*prs(1))
        body = orch.status_body(snap, decide(snap), CFG, {}, "apply", NOW)
        self.assertEqual((orch.STATUS_MARKER, orch.STATUS_END_MARKER), (START, END))
        self.assertTrue(body.startswith(START + "\n"))
        self.assertTrue(body.endswith("\n" + END + "\n"))
        self.assertEqual((body.count(START), body.count(END)), (1, 1))

    def test_splice_keeps_outside_text_byte_for_byte(self):
        current = HEAD + f"{START}\nold\n{END}" + TAIL
        new = orch.splice_status(current, f"{START}\nnew\n{END}\n")
        self.assertEqual(new, HEAD + f"{START}\nnew\n{END}" + TAIL)

    def test_splice_refuses_a_body_it_does_not_own(self):
        for bad in ("", "human text only", f"{START}\nno end", f"no start\n{END}", f"{END}\nx\n{START}\n",
                    f"{START}\na\n{END}\n{START}\nb\n{END}\n", f"{START}\n{START}\n{END}\n"):
            with self.subTest(body=bad):
                self.assertIsNone(orch.splice_status(bad, f"{START}\nnew\n{END}\n"))
                self.assertTrue(orch.status_unowned(bad))
        self.assertEqual(orch.status_unowned(HEAD + f"{START}\nx\n{END}" + TAIL), "")

    def test_failure_section_stays_inside_the_block(self):
        current = HEAD + f"{START}\nFactory status.\n\n_Updated x_\n{END}" + TAIL
        new = orch.set_failure_section(current, "## Failed ticks\n\nboom")
        self.assertTrue(new.startswith(HEAD) and new.endswith(TAIL), new)
        inner = new[len(HEAD):len(new) - len(TAIL)]
        self.assertIn("## Failed ticks\n\nboom", inner)
        self.assertEqual(orch.failure_section(new), "## Failed ticks\n\nboom")
        again = orch.set_failure_section(new, "## Failed ticks\n\nnew")
        self.assertTrue(again.startswith(HEAD) and again.endswith(TAIL))
        self.assertEqual(again.count("## Failed ticks\n\n"), 1 + TAIL.count("## Failed ticks\n\n"))
        self.assertIn("## Failed ticks\nnot the orchestrator's", again, "a human heading outside is untouched")
        self.assertIsNone(orch.set_failure_section("human text only", "## Failed ticks\n\nboom"))
        self.assertIsNone(orch.set_failure_section(START + "\nno end\n", "## Failed ticks\n\nboom"))


class GhGuard(Env):
    def test_only_the_status_issue_body_may_be_edited(self):
        gh = orch.Gh(status_issue="415")
        ok = f"{START}\nx\n{END}\n"
        refused = [("PATCH", f"{O}/vws-deploy/issues/416", {"body": ok}),
                   ("PATCH", STATUS_PATH, {"body": ok, "title": "t"}),
                   ("PATCH", STATUS_PATH, {"body": ok, "state": "closed"}),
                   ("PATCH", STATUS_PATH, {"body": "no marker"}),
                   ("PATCH", STATUS_PATH, {"body": START + "\nno end marker"}),
                   ("PATCH", STATUS_PATH, {"body": "x\n" + END + "\n" + START + "\n"}),
                   ("PATCH", STATUS_PATH, {"body": f"{START}\na\n{END}\n{START}\nb\n{END}\n"}),
                   ("PATCH", f"{O}/vws-iam/issues/415", {"body": ok})]
        for method, path, payload in refused:
            with self.subTest(path=path, payload=payload), self.assertRaises(PermissionError):
                gh.write(method, path, payload)
        with self.assertRaises(PermissionError):
            orch.Gh().write("PATCH", STATUS_PATH, {"body": f"{START}\nx\n{END}\n"})
        self.assertEqual(self.calls(), [], "a refused write never reaches gh")
        gh.write("PATCH", STATUS_PATH, {"body": f"human text\n{START}\nok\n{END}\nmore"})
        self.assertEqual([(c["method"], c["path"]) for c in self.calls()], [("PATCH", STATUS_PATH)])


class Ticks(Env):
    def test_apply_edits_the_status_body_and_never_comments_on_it(self):
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        edits = self.status_edits()
        self.assertEqual(len(edits), 1)
        self.assertTrue(edits[0].startswith(orch.STATUS_MARKER + "\n"))
        self.assertIn("PRs waiting for verify: 5", edits[0])
        posts = [c["path"] for c in self.calls() if c["method"] == "POST"]
        self.assertNotIn(f"{STATUS_PATH}/comments", posts, "no comments on the status issue")
        self.assertIn(f"{O}/vws-deploy/issues/339/comments", posts, "the wake goes to the wake thread")
        self.assertTrue(any(e["event"] == "status_updated" for e in self.log()))

    def test_unchanged_body_is_not_edited_and_the_read_is_etag_cached(self):
        self.tick(ORCHESTRATE_MODE="apply")
        first = self.status_edits()[0]
        r = routes()
        r[STATUS_PATH] = {"body": {"number": 415, "state": "open", "body": first}}
        self.set_routes(r)
        os.remove(self.calls_file)
        self.tick(ORCHESTRATE_MODE="apply")
        self.assertEqual(self.status_edits(), [], "same content (only the _Updated_ line differs): no PATCH")
        self.assertTrue(any(e["event"] == "status_unchanged" for e in self.log()))
        os.remove(self.calls_file)
        self.tick(ORCHESTRATE_MODE="apply")
        reads = [c for c in self.calls() if c["method"] == "GET" and c["path"] == STATUS_PATH]
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0]["inm"], "the status issue read sends If-None-Match")
        self.assertEqual(self.status_edits(), [], "a 304 is compared from the cache: still no PATCH")

    def test_dry_run_renders_locally_and_never_edits(self):
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.status_edits(), [])
        with open(os.path.join(self.paths.state, "status-body.md"), encoding="utf-8") as f:
            self.assertTrue(f.read().startswith(orch.STATUS_MARKER + "\n"))

    def test_scope_elsewhere_skips_the_edit(self):
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(self.status_edits(), [])
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-deploy#415")
        self.assertEqual(len(self.status_edits()), 1)

    def test_no_wake_thread_means_no_wake_comment(self):
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_WAKE_THREAD="")
        self.assertFalse([c for c in self.calls() if "/issues/339" in c["path"]], "nothing read or posted on #339")
        self.assertIn("verify wake is off", self.status_edits()[0])
        self.assertFalse([c for c in self.writes() if "verify-wake" in c["stdin"]])

    def test_failed_status_edit_fails_the_tick(self):
        r = routes()
        r[f"PATCH {STATUS_PATH}"] = {"status": 500, "body": {"message": "boom"}}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_WAKE_THREAD=""), orch.FAIL_EXIT)
        self.assertTrue(any(e["event"] == "apply_failed" and e.get("action") == "status_body" for e in self.log()))

    def test_a_good_tick_drops_the_failure_section(self):
        orch.main(["--notify-failure", "--failures", "3", "--exit-code", "4", "--streak-start", "1000",
                   "--last-error", "boom"], dict(self.env, ORCHESTRATE_MODE="apply"))
        failed = self.status_edits()[-1]
        self.assertIn("## Failed ticks", failed)
        r = routes()
        r[STATUS_PATH] = {"body": {"number": 415, "state": "open", "body": failed}}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertNotIn("## Failed ticks", self.status_edits()[-1])
        self.assertFalse([c for c in self.calls() if c["path"].endswith("/comments") and orch.FAILURE_MARKER in c["stdin"]])


    def human_body(self, body):
        r = routes()
        r[STATUS_PATH] = {"body": {"number": 415, "state": "open", "body": body}}
        self.set_routes(r)

    def test_human_text_outside_the_block_survives_ticks_and_failure_sections(self):
        self.human_body(HEAD + f"{START}\nFactory status.\n{END}" + TAIL)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        first = self.status_edits()[-1]
        self.assertTrue(first.startswith(HEAD), "text before the block is kept")
        self.assertTrue(first.endswith(TAIL), "text after the block is kept")
        self.assertIn("PRs waiting for verify: 5", first)
        self.assertEqual((first.count(START), first.count(END)), (1, 1))
        # A failure section, then a good tick that drops it: the human text is still there.
        self.human_body(first)
        orch.main(["--notify-failure", "--failures", "3", "--exit-code", "4", "--streak-start", "1000",
                   "--last-error", "boom"], dict(self.env, ORCHESTRATE_MODE="apply"))
        failed = self.status_edits()[-1]
        self.assertTrue(failed.startswith(HEAD) and failed.endswith(TAIL))
        self.assertIn("3 failed ticks in a row", failed[len(HEAD):len(failed) - len(TAIL)])
        self.human_body(failed)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_WAKE_THREAD=""), 0)
        last = self.status_edits()[-1]
        self.assertTrue(last.startswith(HEAD) and last.endswith(TAIL))
        self.assertNotIn("failed ticks in a row", last)

    def test_body_without_the_block_is_never_overwritten(self):
        for bad in ("Human notes only.\n", START + "\nFactory status.\n",
                    f"{START}\na\n{END}\n{START}\nb\n{END}\n", f"{END}\nx\n{START}\n"):
            with self.subTest(body=bad):
                self.human_body(bad)
                if os.path.exists(self.calls_file):
                    os.remove(self.calls_file)
                seen = len(self.log()) if os.path.exists(self.paths.log) else 0
                self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_WAKE_THREAD=""), orch.FAIL_EXIT,
                                 "an unowned body is a tick error")
                self.assertEqual(self.status_edits(), [], "no PATCH")
                unowned = [e for e in self.log()[seen:] if e["event"] == "status_body_unowned"]
                self.assertEqual(len(unowned), 1)
                self.assertIn("marker", unowned[0]["reason"])
                seen = len(self.log())
                orch.main(["--notify-failure", "--failures", "3", "--exit-code", "4", "--streak-start",
                           str(1000 + len(bad)), "--last-error", "boom"], dict(self.env, ORCHESTRATE_MODE="apply"))
                self.assertEqual(self.status_edits(), [], "the failure section is not written either")
                self.assertTrue(any(e["event"] == "failure_alert_failed" and "status_body_unowned" in e.get("error", "")
                                    for e in self.log()[seen:]))
                self.assertFalse(os.path.exists(self.paths.failure_file), "the next failed tick tries again")

if __name__ == "__main__":
    unittest.main()
