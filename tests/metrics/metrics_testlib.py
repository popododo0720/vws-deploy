"""Shared helpers for the daily metrics tests (vws-deploy#345). Not a test module."""
import importlib.util
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "metrics", "metrics.py")
BOX = os.path.join(ROOT, ".github", "orchestrate", "box")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

spec = importlib.util.spec_from_file_location("metrics", SCRIPT)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
orch = metrics.orch

DAY = "2026-10-20"
CFG = dict(orch.DEFAULTS, **metrics.DEFAULTS)


def raw():
    with open(os.path.join(FIXTURES, f"raw-{DAY}.json"), encoding="utf-8") as f:
        return json.load(f)


def log_records(day=DAY):
    start, end = metrics.kst_day_window(day)
    return metrics.read_orchestrator_log(FIXTURES, start, end)
