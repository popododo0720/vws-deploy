"""The rule registry and the dispatcher merge (vws-deploy#501 AC3, AC4, AC5).

- every module in rules/ is registered once, keeps the protocol, and every name has one definition;
- a stub rule file plus one REGISTRY line is all the dispatcher needs to run it (AC4);
- the merge gives the same result whatever order the rules are called in, and collapses duplicate actions
  (AC5); a state key outside a rule's STATE_KEYS or set by two rules is an error.
Run: python3 -m unittest discover -s tests/orchestrate"""
import ast
import collections
import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
import types
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot
from rules.rules_testlib import ORCH_DIR, RULES_DIR, golden_inputs, read, rules, source_files, top_level_names

PROTOCOL_NAMES = {"NAME", "STATE_KEYS", "ACTIONS_AFTER", "decide"}


def fake_rule(name, actions=(), notes=(), state=None, state_keys=(), after="", **extra):
    def decide(snapshot, state_, cfg, now):
        return orch.decision(actions, notes, state, **extra)
    return types.SimpleNamespace(NAME=name, STATE_KEYS=tuple(state_keys), ACTIONS_AFTER=after, decide=decide)


def decisions(registry):
    return {r.NAME: r.decide({}, {}, {}, NOW) for r in registry}


class Registry(unittest.TestCase):
    def test_every_rule_module_is_registered_once(self):
        files = sorted(f[:-3] for f in os.listdir(RULES_DIR) if f.endswith(".py") and f not in ("__init__.py", "common.py"))
        self.assertEqual(sorted(rules.REGISTRY), files)
        self.assertEqual(len(set(rules.REGISTRY)), len(rules.REGISTRY))
        self.assertEqual([r.NAME for r in rules.RULES], list(rules.REGISTRY))

    def test_state_keys_are_disjoint_and_actions_after_is_registered(self):
        keys = [k for r in rules.RULES for k in r.STATE_KEYS]
        self.assertEqual(len(keys), len(set(keys)), keys)
        for r in rules.RULES:
            after = getattr(r, "ACTIONS_AFTER", "")
            if after:
                self.assertIn(after, rules.REGISTRY, r.NAME)

    def test_each_name_has_one_definition(self):
        # AC (scope 4): a helper or constant is defined in exactly one of orchestrate.py and rules/*.py.
        where = collections.defaultdict(list)
        for rel in source_files():
            for name in set(top_level_names(read(rel))) - PROTOCOL_NAMES:
                where[name].append(rel)
        dup = {n: w for n, w in where.items() if len(w) > 1}
        self.assertEqual(dup, {})

    def test_rule_modules_do_no_io(self):
        # Scope 1: rule modules have no gh I/O, no file or network access and read no clock.
        banned_imports = {"subprocess", "socket", "urllib", "http", "fcntl", "time"}
        for rel in source_files()[1:]:
            tree = ast.parse(read(rel))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in node.names} & banned_imports, rel)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split(".")[0], banned_imports, rel)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "print", "input"}, f"{rel}:{node.lineno}")
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, {"now", "utcnow", "today"}, f"{rel}:{node.lineno}")

    def test_rule_modules_keep_the_static_rules_of_orchestrate_py(self):
        # The same text checks test_status_issue and test_runtime run on orchestrate.py.
        for rel in source_files()[1:]:
            text = read(rel)
            for bad in ("339", "urllib", "webhook", "X-Automation-Key", "GH_TOKEN"):
                self.assertNotIn(bad, text, rel)

    def test_orch_still_names_every_moved_helper(self):
        # Tests, .github/metrics and .github/verify read the moved names as orch.X.
        for rel in [f for f in source_files()[1:] if not f.endswith("__init__.py")]:
            for name in set(top_level_names(read(rel))) - PROTOCOL_NAMES:
                self.assertTrue(hasattr(orch, name), f"orch.{name} ({rel})")


class StubRule(unittest.TestCase):
    """AC4: a new rule is one file plus one REGISTRY line; orchestrate.py is not edited."""

    STUB = '''"""A stub rule for the vws-deploy#501 AC4 test."""
from .common import decision

NAME = "stub_rule"
STATE_KEYS = ("stub_seen",)


def decide(snapshot, state, cfg, now):
    """Pure."""
    first = sorted(snapshot["items"])[0]
    return decision([{"type": "add_label", "target": first, "label": "stub", "reason": "stub rule"}],
                    [{"kind": "stub_note", "target": first}], {"stub_seen": state.get("stub_seen", 0) + 1})
'''

    def setUp(self):
        self.saved = {k: v for k, v in sys.modules.items() if k == "orchestrate_rules" or k.startswith("orchestrate_rules.")}
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "orchestrate")
        shutil.copytree(ORCH_DIR, self.dir, ignore=shutil.ignore_patterns("__pycache__", "box"))

    def tearDown(self):
        for k in [k for k in sys.modules if k == "orchestrate_rules" or k.startswith("orchestrate_rules.")]:
            del sys.modules[k]
        sys.modules.update(self.saved)
        shutil.rmtree(self.tmp)

    def load(self):
        spec = importlib.util.spec_from_file_location("orchestrate_stub_test", os.path.join(self.dir, "orchestrate.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_rule_file_and_one_registry_line_run(self):
        with open(os.path.join(self.dir, "rules", "stub_rule.py"), "w", encoding="utf-8") as f:
            f.write(self.STUB)
        init = os.path.join(self.dir, "rules", "__init__.py")
        with open(init, encoding="utf-8") as f:
            text = f.read()
        line = '    "verify_labels",  # V (#487): last, label repairs take the writes left under the cap\n'
        self.assertIn(line, text)
        with open(init, "w", encoding="utf-8") as f:
            f.write(text.replace(line, line + '    "stub_rule",\n'))
        stub = self.load()
        with open(os.path.join(self.dir, "orchestrate.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), read(source_files()[0]), "the recipe never edits orchestrate.py")
        self.assertEqual(stub.rules.REGISTRY[-1], "stub_rule")
        snap = snapshot(item("vws-iam", 7))
        base = orch.decide(snap, {}, NOW, CFG, set())
        got = stub.decide(snap, {"stub_seen": 4}, NOW, CFG, set())
        self.assertIn({"type": "add_label", "target": "vws-iam#7", "label": "stub", "reason": "stub rule"}, got["actions"])
        self.assertEqual(got["notes"][-1], {"kind": "stub_note", "target": "vws-iam#7"})
        self.assertEqual(got["state"]["stub_seen"], 5)
        # Nothing else changed: drop the stub's output and the rest is what the real registry decides.
        got["actions"] = [a for a in got["actions"] if a.get("label") != "stub"]
        got["notes"] = got["notes"][:-1]
        del got["state"]["stub_seen"]
        self.assertEqual(got, base)


class Merge(unittest.TestCase):
    """AC5: the merge is deterministic and dedupes."""

    def test_shuffled_call_order_gives_the_same_result(self):
        rng = random.Random(501)
        for snapshot_, state, now, cfg, exclusions in golden_inputs():
            want = json.dumps(orch.decide(snapshot_, state, now, cfg, exclusions), default=sorted)
            for _ in range(3):
                order = list(rules.RULES)
                rng.shuffle(order)
                got = orch.decide(snapshot_, state, now, cfg, exclusions, call_order=order)
                self.assertEqual(json.dumps(got, default=sorted), want, [r.NAME for r in order])

    def test_duplicate_actions_collapse_to_the_first(self):
        a = {"type": "add_label", "target": "vws-iam#1", "label": "blocked", "reason": "first"}
        b = dict(a, reason="second")
        c = dict(a, label="in-review")
        w1 = {"type": "grok_wake", "target": "t", "key": "k1"}
        w2 = dict(w1, body="again")
        reg = [fake_rule("one", [a, w1]), fake_rule("two", [b, c, w2])]
        for order in (reg, reg[::-1]):
            out = orch.merge_decisions(reg, decisions(order), {})
            self.assertEqual(out["actions"], [a, w1, c])

    def test_actions_after_moves_only_the_actions(self):
        reg = [fake_rule("x", [{"type": "t", "target": "x"}], [{"kind": "x"}], after="y"),
               fake_rule("y", [{"type": "t", "target": "y"}], [{"kind": "y"}])]
        out = orch.merge_decisions(reg, decisions(reg), {})
        self.assertEqual([a["target"] for a in out["actions"]], ["y", "x"])
        self.assertEqual([n["kind"] for n in out["notes"]], ["x", "y"])
        with self.assertRaises(ValueError):
            orch.action_order([fake_rule("x", after="nope")])

    def test_state_namespaces(self):
        ok = fake_rule("a", state={"k": 1}, state_keys=("k",))
        out = orch.merge_decisions([ok], decisions([ok]), {"old": 0})
        self.assertEqual(out["state"], {"old": 0, "k": 1})
        undeclared = fake_rule("b", state={"other": 1}, state_keys=("k2",))
        with self.assertRaisesRegex(ValueError, "not in its STATE_KEYS"):
            orch.merge_decisions([undeclared], decisions([undeclared]), {})
        clash = fake_rule("c", state={"k": 2}, state_keys=("k",))
        with self.assertRaisesRegex(ValueError, "set by rules a and c"):
            orch.merge_decisions([ok, clash], decisions([ok, clash]), {})

    def test_extras_and_missing_decisions(self):
        e1, e2 = fake_rule("e1", pending_count=1), fake_rule("e2", pending_count=2)
        with self.assertRaisesRegex(ValueError, "pending_count"):
            orch.merge_decisions([e1, e2], decisions([e1, e2]), {})
        bad = fake_rule("e3")
        with self.assertRaisesRegex(ValueError, "do not match the registry"):
            orch.merge_decisions([e1, bad], decisions([e1]), {})


if __name__ == "__main__":
    unittest.main()
