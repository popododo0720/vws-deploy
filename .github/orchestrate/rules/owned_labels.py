"""Owned labels: forget label ownership the item no longer shows.

Ownership is journaled in state["owned_labels"] before the label write (apply). An entry whose label is not
on the item means the write never happened or a human removed the label, so it is dropped here. Rules B and
C read the same pruned map through common.owned_labels.
"""
from .common import (
    decision, owned_labels,
)

NAME = "owned_labels"
STATE_KEYS = ("owned_labels",)


def decide(snapshot, state, cfg, now):
    """Pure. Only state: the pruned owned_labels map."""
    return decision(state={"owned_labels": owned_labels(state, snapshot["items"])})
