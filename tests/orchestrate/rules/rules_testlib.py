"""Shared helpers for tests/orchestrate/rules (vws-deploy#501)."""
import ast
import copy
import glob
import inspect
import json
import os

from orch_testlib import ROOT, orch

ORCH_DIR = os.path.join(ROOT, ".github", "orchestrate")
RULES_DIR = os.path.join(ORCH_DIR, "rules")
GOLDEN = os.path.join(ROOT, "tests", "orchestrate", "testdata", "split-golden")
rules = orch.rules
BY_NAME = {r.NAME: r for r in rules.RULES}


def source_files():
    """orchestrate.py and every rules/*.py, relative to the repo root."""
    out = [os.path.join(ORCH_DIR, "orchestrate.py")] + sorted(glob.glob(os.path.join(RULES_DIR, "*.py")))
    return [os.path.relpath(p, ROOT) for p in out]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def top_level_names(src):
    """Names a module defines at top level: def, class and assignment targets (imports excluded)."""
    out = []
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out += [n.id for n in ast.walk(t) if isinstance(n, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append(node.target.id)
    return out


def golden_inputs():
    """The 34 recorded decide() inputs (decide-cases.jsonl) as (snapshot, state, now, cfg, exclusions)."""
    with open(os.path.join(GOLDEN, "decide-cases.jsonl"), encoding="utf-8") as f:
        cases = [json.loads(line)["in"] for line in f if line.strip()]
    return [(c["snapshot"], c["state"], c["now"], c["cfg"], set(c["exclusions"])) for c in cases]


def rule_cfg(cfg, exclusions):
    """cfg as the dispatcher passes it to a rule."""
    out = dict(cfg)
    out[orch.EXCLUSIONS_KEY] = exclusions
    return out


def run_rule(name, snapshot, state, now, cfg, exclusions):
    return BY_NAME[name].decide(snapshot, copy.deepcopy(state), rule_cfg(cfg, exclusions), now)


def corpus_profile(name):
    """Over the golden inputs: (#cases with output, sorted action types, sorted note kinds, sorted state keys,
    #actions, #notes) of one rule."""
    hit, types, kinds, keys, n_actions, n_notes = 0, set(), set(), set(), 0, 0
    for args in golden_inputs():
        d = run_rule(name, *args)
        if d["actions"] or d["notes"] or d["state"]:
            hit += 1
        types |= {a["type"] for a in d["actions"]}
        kinds |= {n["kind"] for n in d["notes"]}
        keys |= set(d["state"])
        n_actions += len(d["actions"])
        n_notes += len(d["notes"])
    return hit, sorted(types), sorted(kinds), sorted(keys), n_actions, n_notes


class RuleContract:
    """Mixin for test_<rule>.py: the protocol every rule module keeps, plus its pinned golden profile.
    Set RULE (the NAME) and PROFILE (corpus_profile(RULE) as recorded when the rule moved)."""
    RULE = ""
    PROFILE = None

    def test_protocol(self):
        mod = BY_NAME[self.RULE]
        self.assertEqual(mod.NAME, self.RULE)
        self.assertEqual(os.path.basename(mod.__file__), f"{self.RULE}.py")
        self.assertIsInstance(mod.STATE_KEYS, tuple)
        self.assertEqual(list(inspect.signature(mod.decide).parameters), ["snapshot", "state", "cfg", "now"])

    def test_pure(self):
        # decide never changes its inputs and gives the same Decision twice.
        for snapshot, state, now, cfg, exclusions in golden_inputs():
            before = json.dumps([snapshot, state, cfg], sort_keys=True)
            c = rule_cfg(cfg, exclusions)
            a = BY_NAME[self.RULE].decide(snapshot, state, c, now)
            b = BY_NAME[self.RULE].decide(snapshot, state, c, now)
            self.assertEqual(json.dumps([snapshot, state, {k: v for k, v in c.items() if k != orch.EXCLUSIONS_KEY}],
                                        sort_keys=True), before)
            self.assertEqual(json.dumps(a, sort_keys=True, default=sorted), json.dumps(b, sort_keys=True, default=sorted))
            self.assertEqual(set(a), {"actions", "notes", "state", "extra"})
            self.assertLessEqual(set(a["state"]), set(BY_NAME[self.RULE].STATE_KEYS))

    def test_golden_profile(self):
        # The rule's share of the recorded pre-split decide() output (vws-deploy#501 AC2 fixtures).
        self.assertEqual(corpus_profile(self.RULE), self.PROFILE)
