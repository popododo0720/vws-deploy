"""ORCHESTRATE_DISPATCH=off pauses new DeepSeek dispatches and (vws-deploy#521) every wake; merges and in-flight
tracking continue. The wake silence itself is tested in test_wake_silence.py."""
import unittest

import test_merge as tm
from orch_testlib import CFG, orch, ready_task, snapshot

OFF = dict(CFG, ORCHESTRATE_DISPATCH="off")


def types(r):
    return [a["type"] for a in r["actions"]]


class DispatchSwitch(unittest.TestCase):
    def test_default_is_on(self):
        self.assertEqual(CFG["ORCHESTRATE_DISPATCH"], "on")

    def test_off_blocks_new_dispatch(self):
        s = snapshot(ready_task("vws-iam", 1, 9), ready_task("vws-deploy", 2, 8))
        self.assertIn("dispatch", types(orch.decide(s, {}, tm.NOW, CFG, set())))
        self.assertNotIn("dispatch", types(orch.decide(s, {}, tm.NOW, OFF, set())))

    def test_off_still_plans_merges(self):
        r = tm.run(tm.snap(tm.candidate()), cfg=OFF)
        self.assertEqual(len(tm.merges(r)), 1)

    def test_invalid_value_rejected(self):
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("", "OFF", "no", "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_DISPATCH=bad))
        self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_DISPATCH="off"))["ORCHESTRATE_DISPATCH"], "off")


if __name__ == "__main__":
    unittest.main()
