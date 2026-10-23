"""Unit tests for .github/drift/drift_check.py (vws-deploy#300). Run: python3 -m unittest discover -s tests/drift"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("drift_check", os.path.join(ROOT, ".github", "drift", "drift_check.py"))
dc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dc)

COMMON = {"linters": {"enable": ["govet", "lll"]}}
GOOD = """linters:
  enable: [govet, lll]
  settings:
    lll:
      line-length: 120
formatters:
  enable: [gofumpt, goimports]
"""
WANT = ["what-happened", "impact", "detection", "root-cause", "catching-check", "follow-ups"]
GOOD_INCIDENT = "name: incident\nlabels: [incident]\nbody:\n" + "".join(
    f"  - type: textarea\n    id: {i}\n" for i in WANT
)


class LintDrift(unittest.TestCase):
    def test_good_config(self):
        self.assertEqual(dc.lint_drift(GOOD, COMMON), [])

    def test_empty_config(self):
        for cfg in ("", "   \n", "# only a comment\n"):
            self.assertEqual(dc.lint_drift(cfg, COMMON), ["config is empty or not a YAML mapping"])

    def test_not_a_mapping(self):
        for cfg in ("- a\n- b\n", "just a string\n", "42\n"):
            self.assertEqual(dc.lint_drift(cfg, COMMON), ["config is empty or not a YAML mapping"])

    def test_malformed_yaml(self):
        out = dc.lint_drift("linters: [unclosed\n", COMMON)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("config does not parse:"))

    def test_wrong_shapes(self):
        self.assertEqual(dc.lint_drift("linters: [govet]\n", COMMON), ["config 'linters' is not a mapping"])
        out = dc.lint_drift("linters:\n  enable: govet\n  settings: x\nformatters: 3\n", COMMON)
        self.assertIn("missing linters: govet,lll", out)
        self.assertIn("lll line-length is None, not 120", out)
        self.assertIn("formatter gofumpt is off", out)


class LoadCommon(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_common(f.name)

    def test_valid(self):
        self.assertEqual(self.load("linters:\n  enable: [govet]\n")["linters"]["enable"], ["govet"])

    def test_bad(self):
        for text in ("", "linters: [x\n", "- a\n", "linters:\n  enable: []\n", "linters: {}\n"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("drift-check:", err.getvalue())

    def test_missing_file(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            dc.load_common("/nonexistent/golangci-common.yml")
        self.assertEqual(cm.exception.code, 2)

    def test_repo_baseline(self):
        dc.load_common(os.path.join(ROOT, ".github", "golangci-common.yml"))


class IncidentDrift(unittest.TestCase):
    def drift(self, tpl=GOOD_INCIDENT, legacy=None):
        tpl = tpl.encode() if isinstance(tpl, str) else tpl
        return dc.incident_drift(tpl, legacy, WANT)

    def test_good_form(self):
        self.assertEqual(self.drift(), [])

    def test_no_form(self):
        self.assertEqual(self.drift(None), ["no .github/ISSUE_TEMPLATE/incident.yml"])

    def test_no_form_still_reports_legacy(self):
        self.assertEqual(self.drift(None, b"name: incident\n"),
                         ["no .github/ISSUE_TEMPLATE/incident.yml", "incident.md still exists"])

    def test_legacy_form(self):
        self.assertEqual(self.drift(legacy=b"name: incident\n"), ["incident.md still exists"])

    def test_labels_as_a_string(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("labels: [incident]", 'labels: "bug, incident"')), [])

    def test_missing_label(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("labels: [incident]", "labels: [bug]")),
                         ["does not apply the incident label"])

    def test_missing_field(self):
        self.assertEqual(self.drift(GOOD_INCIDENT.replace("    id: impact\n", "")),
                         ["missing fields: impact"])

    def test_malformed_yaml(self):
        out = self.drift("body: [unclosed\n")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("incident.yml does not parse:"))

    def test_not_a_mapping(self):
        for tpl in ("", "- a\n- b\n"):
            self.assertEqual(self.drift(tpl), ["incident.yml is not a YAML mapping"])

    def test_extra_field(self):
        self.assertEqual(self.drift(GOOD_INCIDENT + "  - type: textarea\n    id: extra\n"), [])

    def test_non_dict_body_item(self):
        out = self.drift("name: incident\nlabels: [incident]\nbody:\n  - just-a-string\n")
        self.assertEqual(out, ["missing fields: " + ",".join(WANT)])

    def test_bad_yaml_still_reports_legacy(self):
        out = self.drift("body: [unclosed\n", b"name: incident\n")
        self.assertTrue(out[0].startswith("incident.yml does not parse:"))
        self.assertEqual(out[1], "incident.md still exists")

    def test_non_mapping_still_reports_legacy(self):
        self.assertEqual(self.drift("- a\n", b"name: incident\n"),
                         ["incident.yml is not a YAML mapping", "incident.md still exists"])


class LoadIncident(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_incident(f.name)

    def test_valid(self):
        self.assertEqual(self.load(GOOD_INCIDENT), WANT)

    def test_bad(self):
        for text in ("", "- a\n", "just a string\n", "body:\n  - type: textarea\n", "body: []\n"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("drift-check:", err.getvalue())

    def test_missing_file(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            dc.load_incident("/nonexistent/incident.yml")
        self.assertEqual(cm.exception.code, 2)

    def test_repo_baseline(self):
        self.assertEqual(dc.load_incident(os.path.join(ROOT, ".github", "ISSUE_TEMPLATE", "incident.yml")), WANT)


class LabelExists(unittest.TestCase):
    def test_present(self):
        with mock.patch.object(dc, "gh", return_value={"name": "incident"}):
            self.assertTrue(dc.label_exists("popododo0720", "vws-lib", "incident"))

    def test_missing(self):
        with mock.patch.object(dc, "gh", return_value=None):
            self.assertFalse(dc.label_exists("popododo0720", "vws-lib", "incident"))

    def test_asks_for_the_label_path(self):
        seen = []

        def fake(path):
            seen.append(path)
            return {}

        with mock.patch.object(dc, "gh", fake):
            dc.label_exists("o", "r", "incident")
        self.assertEqual(seen, ["repos/o/r/labels/incident"])


class GuardVariants(unittest.TestCase):
    WANT = "a" * 64
    VARIANT = "7070d89befc2ef534ea190006cad968d425a5793c416c33bc4daa275b5de2383"

    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        return dc.load_variants(f.name)

    def test_canonical_sha_is_ok(self):
        self.assertEqual(dc.guard_status(self.WANT, self.WANT, self.VARIANT), (None, "ok"))

    def test_accepted_variant(self):
        self.assertEqual(dc.guard_status(self.VARIANT, self.WANT, self.VARIANT), (None, "accepted variant (7070d89befc2)"))

    def test_unexpected_sha_with_a_variant_is_drift(self):
        issue, _ = dc.guard_status("b" * 64, self.WANT, self.VARIANT)
        self.assertEqual(issue, "guard-agents.sh differs from vws-deploy")

    def test_repo_without_an_exception_is_drift(self):
        variants = self.load("vws-conformance\t%s\treason\n" % self.VARIANT)
        issue, _ = dc.guard_status(self.VARIANT, self.WANT, variants.get("vws-lib"))
        self.assertEqual(issue, "guard-agents.sh differs from vws-deploy")

    def test_load(self):
        text = "# comment\n\nvws-conformance\t%s\tcheck-run variant\n" % self.VARIANT
        self.assertEqual(self.load(text), {"vws-conformance": self.VARIANT})

    def test_missing_file_means_no_variants(self):
        self.assertEqual(dc.load_variants("/nonexistent/guard-variants.tsv"), {})

    def test_bad_rows(self):
        bad = (
            "vws-conformance\t%s\n" % self.VARIANT,  # no reason
            "vws-conformance\t%s\t \n" % self.VARIANT,  # blank reason
            "vws-conformance\t7070d89b\treason\n",  # short sha
            "vws-conformance\t%s\treason\n" % self.VARIANT.upper(),  # not lowercase
            "\t%s\treason\n" % self.VARIANT,  # no repo
            "vws-conformance\t%s\tr\nvws-conformance\t%s\tr\n" % (self.VARIANT, "c" * 64),  # repeated repo
        )
        for text in bad:
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                self.load(text)
            self.assertEqual(cm.exception.code, 2, text)
            self.assertIn("drift-check:", err.getvalue())

    def test_repo_file_pins_the_conformance_variant(self):
        variants = dc.load_variants(os.path.join(ROOT, ".github", "drift", "guard-variants.tsv"))
        self.assertEqual(variants, {"vws-conformance": self.VARIANT})


class RequiredChecks(unittest.TestCase):
    def test_ruleset_contexts(self):
        rules = [
            {"type": "deletion"},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "pr-hygiene"}, {"context": "verify-gate"}]}},
        ]
        self.assertEqual(dc.rule_checks(rules), ["pr-hygiene", "verify-gate"])

    def test_bad_ruleset_shapes(self):
        self.assertEqual(dc.rule_checks(None), [])
        self.assertEqual(dc.rule_checks(["x", {"type": "required_status_checks", "parameters": None}]), [])
        self.assertEqual(dc.rule_checks([
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                None, {"context": ""}, {"context": "lint"}]}},
        ]), ["lint"])

    def test_classic_and_ruleset_union(self):
        rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "vws-verify"}, {"context": "lint"}]}}]
        self.assertEqual(dc.effective_checks(["build", "lint"], rules), ["build", "lint", "vws-verify"])

    def test_each_common_check_is_required(self):
        self.assertEqual(dc.missing_checks(dc.COMMON_CHECKS, ""), [])
        self.assertEqual(dc.missing_checks(["pr-hygiene", "verify-gate"], ""), ["vws-verify"])
        self.assertEqual(dc.missing_checks(["pr-hygiene", "vws-verify", "verify-gate"], "lint test"), ["lint", "test"])


class RulesetDrift(unittest.TestCase):
    ACTIVE = {"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}
    # GET /rulesets/{id} as a non-admin caller (github.token): no bypass_actors key at all.
    NON_ADMIN = {"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active",
                 "current_user_can_bypass": "never", "rules": []}

    def test_active_ruleset_without_bypass(self):
        self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": []}}), ([], []))

    def test_no_active_branch_ruleset(self):
        for rulesets in ([], [{**self.ACTIVE, "enforcement": "disabled"}],
                         [{**self.ACTIVE, "target": "tag"}]):
            self.assertEqual(dc.ruleset_drift(rulesets, {}), (["no active branch ruleset"], []))

    def test_other_ruleset_name(self):
        self.assertEqual(dc.ruleset_drift([{**self.ACTIVE, "name": "other"}], {1: {"bypass_actors": []}}),
                         (["no active vws-main ruleset"], []))

    def test_bypass_actor_is_drift(self):
        self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": [{"actor_id": 5}]}}),
                         (["ruleset vws-main has 1 bypass actor(s)"], []))

    def test_unreadable_detail_is_drift(self):
        for detail in (None, [], "x"):
            self.assertEqual(dc.ruleset_drift([self.ACTIVE], {1: detail}), (["ruleset vws-main unreadable"], []))

    def test_non_admin_shape_is_unverified_not_ok(self):
        drift, unverified = dc.ruleset_drift([self.ACTIVE], {1: self.NON_ADMIN})
        self.assertEqual(drift, [])
        self.assertEqual(unverified, ["ruleset vws-main bypass actors unverified (needs admin token)"])

    def test_malformed_bypass_field_is_unverified(self):
        for value in (None, {}, "none", 0):
            _, unverified = dc.ruleset_drift([self.ACTIVE], {1: {"bypass_actors": value}})
            self.assertEqual(len(unverified), 1, value)


class MainRulesetCell(unittest.TestCase):
    """main() end to end with a stubbed gh: the non-admin ruleset detail must never print ruleset ok."""

    def run_main(self, detail, rules=None):
        files = {".github/workflows/repo-guard.yml": b"x", ".github/workflows/direct-push-watch.yml": b"x",
                 "AGENTS.md": b"main is protected by the active vws-main ruleset", "QUALITY.md": b"q"}

        def gh(path):
            if path.endswith("/rulesets/1"):
                return detail
            if path.endswith("/rulesets"):
                return [{"id": 1, "name": "vws-main", "target": "branch", "enforcement": "active"}]
            if "/rules/branches/main" in path:
                return rules if rules is not None else MainRules.GOOD + [
                    {"type": "required_status_checks", "ruleset_id": 1, "parameters": {
                        "required_status_checks": [{"context": c} for c in dc.COMMON_CHECKS]}}]
            if "/labels/" in path:  # the incident label check (#454)
                return {"name": path.rsplit("/", 1)[1]}
            if path == "repos/o/r":
                return {"archived": False}
            return None

        def content(owner, repo, path):
            if path == "scripts/guard-agents.sh":
                with open(os.path.join(ROOT, "scripts", "guard-agents.sh"), "rb") as g:
                    return g.read()
            if path == ".github/ISSUE_TEMPLATE/incident.yml":
                with open(os.path.join(ROOT, path), "rb") as g:
                    return g.read()
            return files.get(path)

        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as f:
            f.write("r\tproto\t-\t\n")
        self.addCleanup(os.unlink, f.name)
        saved = (dc.gh, dc.content, dc.protection, dc.sys.argv)
        dc.gh, dc.content = gh, content
        dc.protection = lambda o, r: {"protected": True, "admins": True, "force": False, "delete": False,
                                      "checks": []}
        dc.sys.argv = ["drift_check.py", "--owner", "o", "--repos", f.name]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = dc.main()
        finally:
            dc.gh, dc.content, dc.protection, dc.sys.argv = saved
        row = [line for line in out.getvalue().splitlines() if line.startswith("| r |")][0]
        return rc, row.split(" | ")[2], out.getvalue()

    def test_non_admin_detail_is_reported_unverified(self):
        rc, cell, text = self.run_main(RulesetDrift.NON_ADMIN)
        self.assertEqual(rc, 0)
        self.assertEqual(cell, "ruleset vws-main bypass actors unverified (needs admin token)")
        self.assertIn("unverified: 1 repo(s)", text)

    def test_admin_detail_with_actor_is_drift(self):
        rc, cell, _ = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": [{"actor_id": 5}]})
        self.assertEqual(cell, "ruleset vws-main has 1 bypass actor(s)")
        self.assertEqual(rc, dc.DRIFT_EXIT)

    def test_admin_detail_without_actor_is_ok(self):
        _, cell, text = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []})
        self.assertEqual(cell, "ok")
        self.assertNotIn("unverified:", text)


    def test_missing_main_rule_is_drift(self):
        rules = [r for r in MainRules.GOOD if r["type"] != "non_fast_forward"]
        rc, cell, _ = self.run_main({**RulesetDrift.NON_ADMIN, "bypass_actors": []}, rules)
        self.assertEqual(cell, "vws-main has no non_fast_forward rule")
        self.assertEqual(rc, dc.DRIFT_EXIT)


class MainRules(unittest.TestCase):
    """vws-main must apply to main with the rules AGENTS.md states (#455 verify FAIL)."""

    RULESETS = [RulesetDrift.ACTIVE]
    GOOD = [{"type": "pull_request", "ruleset_id": 1, "parameters": {"allowed_merge_methods": ["squash"]}},
            {"type": "non_fast_forward", "ruleset_id": 1},
            {"type": "deletion", "ruleset_id": 1}]

    def test_good_rules(self):
        self.assertEqual(dc.main_rules_drift(self.RULESETS, self.GOOD), [])

    def test_each_missing_rule_is_drift(self):
        for name in ("pull_request", "non_fast_forward", "deletion"):
            rules = [r for r in self.GOOD if r["type"] != name]
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules), [f"vws-main has no {name} rule"])

    def test_only_status_checks_left_is_drift(self):
        rules = [{"type": "required_status_checks", "ruleset_id": 1, "parameters": {}}]
        self.assertEqual(len(dc.main_rules_drift(self.RULESETS, rules)), 3)

    def test_not_squash_only_is_drift(self):
        for methods in (["squash", "merge"], ["merge"], [], None):
            rules = [{**self.GOOD[0], "parameters": {"allowed_merge_methods": methods}}] + self.GOOD[1:]
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules),
                             [f"vws-main allows merge methods {methods}, not squash only"])

    def test_vws_main_not_applying_to_main_is_drift(self):
        other = [{**r, "ruleset_id": 2} for r in self.GOOD]
        for rules in ([], None, other):
            self.assertEqual(dc.main_rules_drift(self.RULESETS, rules), ["vws-main does not apply to main"])

    def test_no_vws_main_is_left_to_ruleset_drift(self):
        self.assertEqual(dc.main_rules_drift([{**RulesetDrift.ACTIVE, "name": "other"}], []), [])


class StaleFacts(unittest.TestCase):
    def test_stale_phrases_are_pinned(self):
        self.assertEqual(dc.STALE_PHRASES, ("private, free plan", "no server-side protection",
                                            "not a required check yet", "detection, not blocking"))

    def test_each_stale_phrase_is_reported(self):
        for phrase in dc.STALE_PHRASES:
            self.assertEqual(dc.stale_phrases("before " + phrase + " after"),
                             [f"stale phrase: {phrase}"])

    def test_clean_text_passes(self):
        self.assertEqual(dc.stale_phrases("main is protected by the active vws-main ruleset"), [])

    def test_repo_agents_md_is_clean(self):
        with open(os.path.join(ROOT, "AGENTS.md"), "rb") as f:
            self.assertEqual(dc.stale_phrases(f.read().decode("utf-8")), [])


class ExitStatus(unittest.TestCase):
    def test_drift_is_not_a_crash_status(self):
        # 1 is what Python returns on a traceback; drift must not share it with a crash.
        self.assertEqual(dc.DRIFT_EXIT, 3)


class CallerPending(unittest.TestCase):
    """caller-pending.tsv lists repos that still carry the full repo-guard.yml (vws-deploy#402)."""

    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_load(self):
        p = self.write("# c\nvws-lib\tfull copy until its caller PR lands\n\n")
        self.assertEqual(dc.load_pending(p), {"vws-lib": "full copy until its caller PR lands"})

    def test_missing_file_means_none(self):
        self.assertEqual(dc.load_pending("/nonexistent/caller-pending.tsv"), {})

    def test_bad_rows(self):
        for text in ("vws-lib\n", "vws-lib\t\n", "\treason\n", "a\tb\tc\n", "a\tr\na\tr\n"):
            with self.subTest(text=text), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    dc.load_pending(self.write(text))
                self.assertEqual(cm.exception.code, 2)

    def test_repo_file_never_lists_vws_deploy(self):
        pending = dc.load_pending(os.path.join(ROOT, ".github", "drift", "caller-pending.tsv"))
        self.assertNotIn("vws-deploy", pending)


SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
HOME_CALLER = open(os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")).read()


def pinned(sha=SHA):
    """This repo's caller as another repo carries it: each local call pinned to sha."""
    out = []
    for line in HOME_CALLER.splitlines():
        if line.startswith("    uses: ./.github/workflows/"):
            line = line.replace("./", "popododo0720/vws-deploy/", 1) + "@" + sha
        out.append(line)
    return "\n".join(out) + "\n"


class HeadArgs(unittest.TestCase):
    """--head REPO=SHA names a PR head to check, from the workflow event (vws-deploy#402)."""

    def test_parse(self):
        self.assertEqual(dc.parse_heads(["vws-lib=" + SHA, "vws-deploy=" + OTHER], {"vws-lib", "vws-deploy"}),
                         {"vws-lib": [SHA], "vws-deploy": [OTHER]})
        self.assertEqual(dc.parse_heads([], {"vws-lib"}), {})

    def test_bad(self):
        for arg in ("vws-lib", "vws-lib=", "vws-lib=main", "vws-lib=" + SHA[:12], "vws-lib=" + SHA.upper(),
                    "=" + SHA, "vws-unknown=" + SHA):
            with self.subTest(arg=arg), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    dc.parse_heads([arg], {"vws-lib"})
                self.assertEqual(cm.exception.code, 2)


class OpenPrHeads(unittest.TestCase):
    def test_lists_number_and_head(self):
        api = {"repos/o/r/pulls?state=open&per_page=100": [{"number": 7, "head": {"sha": SHA}}]}.get
        self.assertEqual(dc.open_pr_heads("o", "r", api=api), [("#7", SHA)])

    def test_api_error_is_none(self):
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: None))
        self.assertIsNone(dc.open_pr_heads("o", "r", api=lambda path: {"message": "x"}))


class HeadCallers(unittest.TestCase):
    """The caller at a PR head is checked like the default branch one, against the fleet pin (AC3)."""

    def issues(self, heads, repo="vws-lib", pending=(), common=SHA, main=(SHA,)):
        return dc.head_issues(repo, heads, home=(repo == "vws-deploy"), pending=set(pending), common=common,
                              on_main=lambda sha: sha in main)

    def test_same_pin_is_clean(self):
        self.assertEqual(self.issues([("#7", SHA, pinned())]), [])

    def test_pr_that_changes_the_pin_is_flagged(self):
        out = self.issues([("#7", OTHER, pinned(OTHER))], main=(SHA, OTHER))
        self.assertEqual(len(out), 1)
        self.assertIn("#7", out[0])
        self.assertIn("not the common pin", out[0])

    def test_pin_not_on_main_is_flagged(self):
        out = self.issues([("#7", OTHER, pinned(OTHER))], common=None)
        self.assertEqual(len(out), 1)
        self.assertIn("not on vws-deploy main", out[0])

    def test_missing_or_broken_caller_is_flagged(self):
        self.assertIn("no .github/workflows/repo-guard.yml", self.issues([("#7", OTHER, None)])[0])
        broken = pinned().replace("permissions: {}\n", "permissions: {}\ndefaults: {run: {shell: sh}}\n", 1)
        out = self.issues([("#7", OTHER, broken)])
        self.assertEqual(len(out), 1)
        self.assertIn("top-level keys", out[0])

    def test_pending_repo(self):
        # A repo still on the full copy is not drift at a PR head either, but its migration PR is checked.
        self.assertEqual(self.issues([("#7", OTHER, "name: repo-guard\njobs: {}\n")], pending=["vws-lib"]), [])
        self.assertEqual(self.issues([("#7", OTHER, pinned())], pending=["vws-lib"]), [])
        out = self.issues([("#7", OTHER, pinned(OTHER))], pending=["vws-lib"], main=(SHA, OTHER))
        self.assertIn("not the common pin", out[0])

    def test_home_repo_head(self):
        self.assertEqual(self.issues([("#9", OTHER, HOME_CALLER)], repo="vws-deploy"), [])
        self.assertTrue(self.issues([("#9", OTHER, pinned())], repo="vws-deploy"))

    def test_label_names_the_head(self):
        out = self.issues([("--head", OTHER, None)])
        self.assertTrue(out[0].startswith("--head %s:" % OTHER[:12]), out)


class HeadWiring(unittest.TestCase):
    def test_workflow_passes_the_event_head(self):
        import yaml
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "drift-check.yml")))
        step = next(s for s in wf["jobs"]["drift"]["steps"] if s.get("name") == "Compare every repo against the baseline")
        self.assertEqual(step["env"]["PR_HEAD"], "${{ github.event.pull_request.head.sha }}")
        self.assertIn('--head "vws-deploy=${PR_HEAD}"', step["run"])
        self.assertIn('--head "${IN_REPO}=${IN_SHA}"', step["run"])
        self.assertNotIn("${{ inputs", step["run"])  # inputs reach the script through env only
        on = wf.get("on", wf.get(True))
        self.assertEqual(sorted(on["workflow_dispatch"]["inputs"]), ["repo", "sha"])


if __name__ == "__main__":
    unittest.main()
