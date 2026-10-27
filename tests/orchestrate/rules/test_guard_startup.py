"""rules/guard_startup.py: one repo-guard startup_failure re-trigger (vws-deploy#446).

The recorded vws-storage#133 cases live in tests/orchestrate/test_guard_startup.py.
Run: python3 -m unittest discover -s tests/orchestrate
"""
import unittest

from rules.rules_testlib import RuleContract


class GuardStartup(RuleContract, unittest.TestCase):
    RULE = "guard_startup"
    # The golden decide() inputs have no guard_runs, so this rule adds nothing there.
    PROFILE = (0, [], [], [], 0, 0)


if __name__ == "__main__":
    unittest.main()
