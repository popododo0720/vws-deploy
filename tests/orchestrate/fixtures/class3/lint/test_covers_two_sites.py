# Lint fixture (not run): covers two_sites only, so in_a_loop and batched_graphql stay uncovered.
from fault_gh import restart_check


def test_two_sites(self):
    restart_check(self, lambda: None, covers=("two_sites",))
