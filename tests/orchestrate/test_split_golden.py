"""vws-deploy#501 AC2: splitting orchestrate.py into rules/ modules is a pure move.

Both fixtures were recorded from the pre-split orchestrate.py (main 1c21811); the outputs were re-recorded
from the same inputs on pre-split main f3ba31e (#549 adds `allow_assigned` to grok_wake). vws-deploy#641 re-recorded the `out` of
cases 10, 13, 14 and 33 and dry-run-plan.json from the same inputs: only the grok_wake body changed (the
soft-CAP park/idle sentence); actions, notes and state are otherwise identical. They must stay
byte-identical:

- decide-cases.jsonl: decide() inputs and outputs recorded while the tests/orchestrate suite ran on the
  pre-split code (867 calls, 692 distinct inputs), reduced to the 34 calls that together produce every
  action type, note kind and state key seen in the whole run. Each output is compared as JSON text, so
  key order counts too.
- dry-run-*.json: one `orchestrate.py --fixture <snapshot> --print-plan` tick (ORCHESTRATE_MODE is
  forced to dry-run by --fixture) on a recorded snapshot; the printed plan (actions and notes) must match.

A behavior change belongs in its own PR, which then re-records these files and says why.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from orch_testlib import orch

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "split-golden")
RUN_ID = "20261021T120000Z-501501"


def encode(o):
    """The JSON form the fixtures were recorded with: sets and tuples are tagged, so a type change shows."""
    if isinstance(o, (set, frozenset)):
        return {"__set__": sorted(o)}
    if isinstance(o, tuple):
        return {"__tuple__": list(o)}
    raise TypeError(type(o).__name__)


def load(name):
    with open(os.path.join(GOLDEN, name), encoding="utf-8") as f:
        return json.load(f)


def dry_run_plan():
    """One in-process `orchestrate.py --once --fixture ... --print-plan`; returns the printed JSON text."""
    tmp = tempfile.mkdtemp()
    try:
        env = {"ORCHESTRATE_STATE_DIR": os.path.join(tmp, "state"), "ORCHESTRATE_CONFIG_DIR": os.path.join(tmp, "config")}
        paths = orch.Paths(env)
        os.makedirs(paths.state)
        orch.write_json(paths.state_file, load("dry-run-state.json"))
        cfg = load("dry-run-cfg.json")
        args = argparse.Namespace(fixture=os.path.join(GOLDEN, "dry-run-snapshot.json"), verbose=False, repos="",
                                  print_plan=True, dump_snapshot=None)
        out = io.StringIO()
        with mock.patch.object(orch, "run_id_now", lambda now: RUN_ID), contextlib.redirect_stdout(out):
            rc = orch.tick(paths, cfg, args, now=load("dry-run-now.json"))
        return rc, out.getvalue()
    finally:
        shutil.rmtree(tmp)


class SplitGolden(unittest.TestCase):
    def test_recorded_decide_calls_are_byte_identical(self):
        with open(os.path.join(GOLDEN, "decide-cases.jsonl"), encoding="utf-8") as f:
            cases = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(cases), 34)
        for i, case in enumerate(cases):
            inp = case["in"]
            out = orch.decide(inp["snapshot"], inp["state"], inp["now"], inp["cfg"], set(inp["exclusions"]))
            with self.subTest(case=i):
                self.assertEqual(json.dumps(out, default=encode), case["out"])

    def test_dry_run_plan_is_byte_identical(self):
        rc, text = dry_run_plan()
        self.assertEqual(rc, 0)
        with open(os.path.join(GOLDEN, "dry-run-plan.json"), encoding="utf-8") as f:
            self.assertEqual(text, f.read())
        plan = json.loads(text)
        self.assertEqual(plan["mode"], "dry-run")
        self.assertTrue(plan["actions"] and plan["notes"], "the fixture exercises actions and notes")


if __name__ == "__main__":
    unittest.main()
