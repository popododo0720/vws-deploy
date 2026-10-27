"""Adversarial fixtures for guard text parsing and commit scans (vws-deploy#439 AC2, class 2 only).

Class 2 of docs/lessons/verify-findings-2026-10.md: guards read PR bodies, comments and commit messages
in different ways, and each difference was a bypass. One normalizer spec now has two implementations,
visible_text in scripts/guard-agents.sh (pr-body, pr-checklist, commit-msg, correct, and
.github/ac/post-merge.sh, which loads the same function) and visible_text in
.github/orchestrate/orchestrate.py (parse_ac and agent_of). tests/fixtures/guard-text/corpus.json is the one
set of vectors both must agree on.

Each past bypass is one failing fixture; each has a passing neighbour. GUARD_AGENTS points the tests at
another copy of guard-agents.sh (for example an old one) to show which bypasses it still has.
Run: python3 -m unittest discover -s tests/workflows
"""
import glob
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUARD = os.environ.get("GUARD_AGENTS") or os.path.join(ROOT, "scripts", "guard-agents.sh")
ORCH = os.path.join(ROOT, ".github", "orchestrate", "orchestrate.py")
with open(os.path.join(ROOT, "tests", "fixtures", "guard-text", "corpus.json"), encoding="utf-8") as f:
    CORPUS = json.load(f)["cases"]

_spec = importlib.util.spec_from_file_location("orchestrate_guard_text", ORCH)
orch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(orch)

ENV = {"PATH": os.environ["PATH"], "LC_ALL": "C"}
# Load visible_text from the guard file the way .github/ac/post-merge.sh does, so this is the code its
# consumers run, not a copy.
LOAD = 'eval "$(sed -n "/^visible_text() {\\$/,/^}\\$/p" "$1")"; declare -F visible_text >/dev/null && visible_text'


def guard_visible(text):
    r = subprocess.run(["bash", "-c", LOAD, "_", GUARD], input=text.encode("utf-8"), capture_output=True,
                       env=ENV, check=False)
    if r.returncode != 0:
        raise AssertionError(f"cannot run visible_text from {GUARD}: {r.stderr.decode()}")
    return r.stdout.decode("utf-8")


def run_guard(args, cwd=None):
    return subprocess.run(["bash", GUARD, *args], cwd=cwd, capture_output=True, text=True, env=ENV, check=False)


class Corpus(unittest.TestCase):
    """Every reader returns the corpus's visible text for every case."""

    def test_corpus_is_adversarial(self):
        # Most cases hide text; a corpus of only plain text would pass any reader.
        hidden = [c for c in CORPUS if c["visible"].rstrip("\n") != c["text"].rstrip("\n")]
        self.assertGreaterEqual(len(hidden), 15)
        self.assertGreaterEqual(len(CORPUS) - len(hidden), 2)

    def test_guard_agents_visible_text(self):
        for c in CORPUS:
            with self.subTest(c["name"]):
                self.assertEqual(guard_visible(c["text"]).rstrip("\n"), c["visible"].rstrip("\n"), c["source"])

    def test_orchestrate_visible_text(self):
        for c in CORPUS:
            with self.subTest(c["name"]):
                self.assertEqual(orch.visible_text(c["text"]).rstrip("\n"), c["visible"].rstrip("\n"), c["source"])

    def test_agent_of_reads_the_visible_text(self):
        # agent_of had its own regex for hidden regions (HIDDEN_RE); it must read what visible_text shows.
        for c in CORPUS:
            with self.subTest(c["name"]):
                m = orch.AGENT_RE.search(c["visible"])
                self.assertEqual(orch.agent_of(c["text"]), m.group(1) if m else "")

    def test_agent_of_has_no_second_parser(self):
        self.assertFalse(hasattr(orch, "HIDDEN_RE"), "agent_of must use visible_text, not its own regex")


CHECKLIST_TITLES = ["Error handling", "Retries and timeouts", "Security", "Unit tests", "Integration tests",
                    "vws-test live evidence (required)", "Observability", "Idempotency",
                    "AWS compatibility (where applicable)"]


def checklist(evidence=None):
    """A Quality checklist body; evidence maps an item number to its raw evidence text."""
    evidence = evidence or {}
    lines = ["Part of #439", "", "## Quality checklist (required)", ""]
    for i, t in enumerate(CHECKLIST_TITLES, 1):
        lines.append(f"- [x] **{t}**: done. Evidence: {evidence.get(i, f'tests/case-{i}.sh')}")
    return "\n".join(lines) + "\n"


class CallSites(unittest.TestCase):
    """The #430 bypass was a call site with its own comment stripper (sed 's/<!--.*-->//' on one line),
    not a bug in visible_text. Each guard reads the text through the shared normalizer."""

    def checklist_rc(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body)
        try:
            return run_guard(["pr-checklist", f.name]).returncode
        finally:
            os.unlink(f.name)

    def test_complete_checklist_passes(self):
        self.assertEqual(self.checklist_rc(checklist()), 0)

    def test_evidence_in_a_comment_closed_on_the_next_line_fails(self):
        # vws-deploy#430 review 5629555715, blocking 2.
        body = checklist({5: "<!--"}).replace("tests/case-6.sh", "-->tests/case-6.sh")
        self.assertNotEqual(self.checklist_rc(body), 0)

    def test_checklist_inside_a_multi_line_comment_fails(self):
        body = checklist().replace("## Quality checklist (required)", "<!--\n## Quality checklist (required)") + "-->\n"
        self.assertNotEqual(self.checklist_rc(body), 0)

    def correct_rc(self, section, rest=""):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".github", "ISSUE_TEMPLATE"))
            with open(os.path.join(d, "AGENTS.md"), "w") as f:
                f.write(f"# t\n## /correct loop\n{section}\n## next\ncontent\n{rest}")
            fields = "".join(f"      id: {x}\n" for x in
                             "what-happened impact detection root-cause catching-check follow-ups".split())
            with open(os.path.join(d, ".github", "ISSUE_TEMPLATE", "incident.yml"), "w") as f:
                f.write("labels: [incident]\nbody:\n" + fields)
            with open(os.path.join(d, ".github", "labels.yml"), "w") as f:
                f.write("- name: incident\n")
            return run_guard(["correct", d]).returncode

    def test_correct_section_with_text_passes(self):
        self.assertEqual(self.correct_rc("Run /correct after an incident."), 0)

    def test_correct_section_with_a_fenced_heading_line_passes(self):
        self.assertEqual(self.correct_rc("```\n# step\n```"), 0)

    def test_correct_section_with_only_an_html_comment_fails(self):
        # cmd_correct had its own fence toggle and no comment handling: a hidden comment counted as content.
        self.assertNotEqual(self.correct_rc("<!-- fill this in -->"), 0)

    def test_correct_section_with_only_an_empty_long_fence_fails(self):
        # A fence that holds only blank lines is no content, whatever its length.
        self.assertNotEqual(self.correct_rc("````\n\n````"), 0)

    def test_correct_section_followed_by_a_long_file_passes(self):
        # PR #515 CI at 8afa51d: the reader stopped at the next heading, so the visible_text stage before it
        # wrote into a closed pipe and pipefail read a filled section as empty. A long file makes that certain.
        self.assertEqual(self.correct_rc("Run /correct after an incident.", "more text after the section\n" * 20000), 0)

    def test_correct_passes_on_this_repo(self):
        self.assertEqual(run_guard(["correct", ROOT]).returncode, 0)

    def test_guard_agents_has_one_text_parser(self):
        # Outside visible_text and the selftest, no code in guard-agents.sh matches comments or fences itself.
        with open(GUARD, encoding="utf-8") as f:
            src = f.read().split("\n")
        skip, bad = False, []
        for n, line in enumerate(src, 1):
            if line in ("visible_text() {", "cmd_selftest() {"):
                skip = True
            if not skip and not line.lstrip().startswith("#") and any(t in line for t in ("<!--", "```", "~~~")):
                bad.append(f"{n}: {line.strip()}")
            if skip and line == "}":
                skip = False
        self.assertEqual(bad, [])

    def test_orchestrate_has_one_text_parser(self):
        # Only visible_text strips comments and fences. Marker strings and the ci-fail json block are not
        # readers of hidden text.
        src = ""
        for path in [ORCH] + sorted(glob.glob(os.path.join(os.path.dirname(ORCH), "rules", "*.py"))):
            with open(path, encoding="utf-8") as f:
                src += f.read()
        head, tail = src.split("def visible_text(", 1)
        rest = head + tail.split("\ndef ", 1)[1]
        self.assertNotIn("<!--.*", rest)
        self.assertNotIn("`{3,}", rest)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


class Commits(unittest.TestCase):
    """pr-commits reads every stored commit message in full, merge commits included, and fails closed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "ci: base", "-m", "Part of #4")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, *paras):
        # --cleanup=verbatim stores '#' lines as they are, as GitHub's web editor and `git commit -F` can.
        args = ["commit", "-q", "--allow-empty", "--cleanup=verbatim"]
        for p in paras:
            args += ["-m", p]
        git(self.repo, *args)

    def merge_side(self, *paras):
        git(self.repo, "switch", "-q", "-c", "side")
        self.commit("ci: side", "Part of #4")
        git(self.repo, "switch", "-q", "main")
        args = ["merge", "-q", "--no-ff"]
        for p in paras:
            args += ["-m", p]
        git(self.repo, *args, "side")

    def assertRejected(self, base, why):
        r = run_guard(["pr-commits", base, "HEAD"], cwd=self.repo)
        self.assertNotEqual(r.returncode, 0, f"accepted: {why}")
        return r.stderr

    def assertAccepted(self, base):
        r = run_guard(["pr-commits", base, "HEAD"], cwd=self.repo)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_clean_commit_passes(self):
        self.commit("ci: x", "# Notes\nA heading line in a stored message is fine.", "Part of #4")
        self.assertAccepted(self.base)

    def test_hash_prefixed_closing_line_fails(self):
        # compute-v2-586d673.md and 9 more copies: stored messages lost their '#' lines before the check.
        self.commit("ci: x", "# Fixes #4", "Part of #4")
        self.assertRejected(self.base, "a stored '# Fixes #4' line")

    def test_merge_commit_with_closing_keyword_fails(self):
        # lib-v2-c6e11b2.md: rev-list --no-merges skipped merge commits. The subject has a valid type
        # prefix, so only the closing keyword can fail it.
        self.merge_side("ci: merge side", "Part of #4", "Closes #4")
        self.assertRejected(self.base, "a merge commit that closes #4")

    def test_merge_commit_without_closing_keyword_passes(self):
        self.merge_side("ci: merge side", "Part of #4")
        self.assertAccepted(self.base)

    def test_range_that_cannot_be_listed_fails(self):
        # network-v2-b6259b8.md, vws-lib-42-6d0b8e9.md: a failed rev-list checked nothing and passed.
        self.commit("ci: x", "Part of #4")
        self.assertIn("cannot list", self.assertRejected("no-such-ref", "an unlistable range"))

    def test_empty_range_fails(self):
        # The lesson's last class 2 gap: a range with no commits checked nothing and passed.
        self.assertIn("no commits", self.assertRejected(self.base, "an empty range"))


if __name__ == "__main__":
    unittest.main()
