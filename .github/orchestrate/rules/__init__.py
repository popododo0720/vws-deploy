"""The orchestrator rule registry (vws-deploy#501).

Every rule is one module here with:
  NAME           the module name
  STATE_KEYS     the state keys the rule may set (a tuple; no two rules share one)
  ACTIONS_AFTER  optional: the NAME of the rule whose actions go right before this rule's
  decide(snapshot, state, cfg, now) -> Decision   pure; see common.decision

orchestrate.py decide calls every rule and merges the Decisions (merge_decisions). REGISTRY order is the
contract: notes, state updates and extras merge in this order, and so do actions (but ACTIONS_AFTER), and the
write cap in apply takes actions in that order. Adding a rule is three files: rules/<rule>.py, one line in
REGISTRY, and tests/orchestrate/rules/test_<rule>.py (docs/factory-orchestrator.md).
"""
import importlib
import os
import sys
import types

# trusted_read.py sits next to orchestrate.py, one directory up (vws-deploy#439 AC2 class 1: markers, comment
# authors, fresh PR reads and trusted PR bodies come from there; .github/drift/trusted_read_lint.py fails on a
# marker match in orchestrate.py or any rules/*.py).
_ORCHESTRATE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ORCHESTRATE_DIR not in sys.path:
    sys.path.insert(0, _ORCHESTRATE_DIR)

REGISTRY = (
    "owned_labels",   # first: the pruned label ownership map
    "verify_wake",    # A
    "ci_fail",        # CI-failure markers (#412)
    "deepseek",       # B and C per item: failures, infra retries, dependencies, the queue
    "grok_rework",    # B4 (#398(a)); actions go after rebuild
    "dispatch",       # B: hand out the DeepSeek slots
    "grok",           # G
    "rebuild",        # R
    "superseded",     # Z (#377): close a PR a newer rework or rebuild PR names as its source
    "lead",           # lead wake (#491)
    "hold_release",   # H (#501): a Held for: hold whose PR is no longer open loses hold
    "merge",          # D
    "ac_tick",        # T (#486)
    "guard_startup",  # S (#446): one repo-guard startup_failure re-trigger, then the #412 marker
    "verify_labels",  # V (#487): last, label repairs take the writes left under the cap
)
PROTOCOL = ("NAME", "STATE_KEYS", "ACTIONS_AFTER", "decide")

RULES = [importlib.import_module(f"{__name__}.{name}") for name in REGISTRY]
for _rule, _name in zip(RULES, REGISTRY):
    if getattr(_rule, "NAME", None) != _name or not callable(getattr(_rule, "decide", None)):
        raise ImportError(f"rules/{_name}.py must set NAME = {_name!r} and define decide")


def exports():
    """{name: object} of every public name the rule modules define or import (but the protocol names and
    modules), for orchestrate.py's orch.X compat. Raises when two modules bind one name to different objects."""
    out, where = {}, {}
    for mod in [importlib.import_module(f"{__name__}.common")] + RULES:
        for name, obj in vars(mod).items():
            if name.startswith("__") or name in PROTOCOL or isinstance(obj, types.ModuleType):
                continue
            if name in out and out[name] is not obj:
                raise ImportError(f"{name} is defined in both {where[name]} and {mod.__name__}")
            out.setdefault(name, obj)
            where.setdefault(name, mod.__name__)
    return out
