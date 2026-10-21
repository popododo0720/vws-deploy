"""board_sync plan tests for .github/orchestrate/orchestrate.py (vws-deploy#347).
Run: python3 -m unittest discover -s tests/orchestrate"""
import re
import unittest

from orch_testlib import U, orch


def item(repo, number, labels=(), is_pr=False, draft=False, body=""):
    return {"repo": repo, "number": number, "url": f"{U}/{repo}/{'pull' if is_pr else 'issues'}/{number}",
            "is_pr": is_pr, "draft": draft, "state": "open", "labels": sorted(labels), "body": body,
            "node_id": f"N_{repo}_{number}"}


def items(*its):
    return {f"{i['repo']}#{i['number']}": i for i in its}


def synced(open_items):
    """The board after a full sync of open_items."""
    return {i["url"]: {"id": f"PVTI_{k}", "state": "OPEN", "values": {f: v for f, v in orch.board_values(i).items() if v}}
            for k, i in open_items.items()}


class Mapping(unittest.TestCase):
    def test_table(self):
        cases = [  # labels, is_pr, draft -> Status, Owner, Priority
            ((), False, False, "Backlog", "", "Normal"),
            (("ready",), False, False, "Ready", "", "Normal"),
            (("ready", "deepseek"), False, False, "DeepSeek", "deepseek", "Normal"),
            (("grok", "deepseek"), False, False, "Grok", "grok", "Normal"),
            (("in-review",), False, False, "In review", "verify", "Normal"),
            ((), True, False, "In review", "verify", "Normal"),
            ((), True, True, "In review", "", "Normal"),
            (("blocked", "deepseek"), True, False, "Blocked", "deepseek", "Normal"),
            (("blocked",), True, False, "Blocked", "verify", "Normal"),
            (("priority/blocker", "ready"), False, False, "Ready", "", "Blocker"),
        ]
        for labels, is_pr, draft, status, owner, prio in cases:
            with self.subTest(labels=labels, is_pr=is_pr, draft=draft):
                v = orch.board_values(item("vws-iam", 1, labels, is_pr, draft))
                self.assertEqual((v["Status"], v["Owner"], v["Priority"]), (status, owner, prio))

    def test_depends_on_uses_the_orchestrator_parser(self):
        body = "Text\nDepends on vws-proto#51, #7\nThis depends on #4 mid-sentence\nDepends on the proto PR"
        self.assertEqual(orch.board_values(item("vws-iam", 1, body=body))["Depends on"], "vws-proto#51, vws-iam#7")


class Plan(unittest.TestCase):
    def test_new_item_is_added_with_fields(self):
        muts, deferred = orch.board_plan(items(item("vws-iam", 1, ("ready", "deepseek"), body="Depends on #2")), {}, 50)
        self.assertEqual([(m["op"], m.get("field"), m.get("value")) for m in muts],
                         [("add", None, None), ("set", "Status", "DeepSeek"), ("set", "Owner", "deepseek"),
                          ("set", "Priority", "Normal"), ("set", "Depends on", "vws-iam#2")])
        self.assertEqual((muts[0]["content"], deferred), ("N_vws-iam_1", 0))

    def test_second_plan_is_empty(self):
        its = items(item("vws-iam", 1, ("ready",)), item("vws-deploy", 339, is_pr=True, draft=True),
                    item("vws-proto", 51, ("deepseek", "priority/blocker"), body="Depends on #50"))
        self.assertEqual(orch.board_plan(its, synced(its), 50), ([], 0))

    def test_owner_is_cleared_and_hand_edits_are_overwritten(self):
        its = items(item("vws-iam", 1, ("ready",)))
        board = synced(its)
        board[its["vws-iam#1"]["url"]]["values"].update(Owner="grok", Status="Grok")
        muts, _ = orch.board_plan(its, board, 50)
        self.assertEqual([(m["op"], m["field"], m["value"]) for m in muts],
                         [("set", "Status", "Ready"), ("clear", "Owner", "")])

    def test_closed_items_get_done_and_nothing_else(self):
        board = {f"{U}/vws-deploy/pull/350": {"id": "A", "state": "MERGED", "values": {"Status": "In review", "Owner": "verify"}},
                 f"{U}/vws-deploy/issues/341": {"id": "B", "state": "CLOSED", "values": {"Status": "Ready"}},
                 f"{U}/vws-deploy/issues/1": {"id": "C", "state": "CLOSED", "values": {"Status": "Done"}},
                 "https://github.com/other/vws-x/issues/1": {"id": "D", "state": "CLOSED", "values": {}}}
        muts, _ = orch.board_plan({}, board, 50)
        self.assertEqual([(m["target"], m["field"], m["value"]) for m in muts],
                         [("vws-deploy#341", "Status", "Done"), ("vws-deploy#350", "Status", "Done")])

    def test_scope_limits_to_one_item(self):
        its = items(item("vws-iam", 1), item("vws-iam", 2))
        muts, _ = orch.board_plan(its, {}, 50, scope="vws-iam#2")
        self.assertEqual({m["target"] for m in muts}, {"vws-iam#2"})


class Budget(unittest.TestCase):
    def setUp(self):
        self.old = items(item("vws-iam", 1, ("ready",)))
        self.board = synced(self.old)
        self.board[self.old["vws-iam#1"]["url"]]["values"]["Status"] = "Backlog"
        self.its = dict(self.old, **items(item("vws-iam", 2, ("deepseek",), body="Depends on #1")))

    def test_order_status_then_adds_then_owner_priority_then_depends(self):
        muts, deferred = orch.board_plan(self.its, self.board, 50)
        self.assertEqual([(m["target"], m["op"], m.get("field")) for m in muts],
                         [("vws-iam#1", "set", "Status"), ("vws-iam#2", "add", None), ("vws-iam#2", "set", "Status"),
                          ("vws-iam#2", "set", "Owner"), ("vws-iam#2", "set", "Priority"),
                          ("vws-iam#2", "set", "Depends on")])
        self.assertEqual(deferred, 0)

    def test_cut_off_defers_the_rest(self):
        for budget, n in ((1, 1), (3, 3), (5, 5)):
            with self.subTest(budget=budget):
                muts, deferred = orch.board_plan(self.its, self.board, budget)
                self.assertEqual((len(muts), deferred), (n, 6 - n))

    def test_sets_of_an_add_over_budget_are_deferred(self):
        muts, deferred = orch.board_plan(items(item("vws-iam", 2), item("vws-iam", 3)), {}, 2)
        self.assertEqual([(m["target"], m["op"]) for m in muts], [("vws-iam#2", "add"), ("vws-iam#2", "set")])
        self.assertEqual(deferred, 4)  # #3 add + Status, then Priority of #2 and #3 (Owner empty: no write)


class Cadence(unittest.TestCase):
    def test_due(self):
        cases = [({}, True), ({"last_sync": "x", "ticks": 0}, False), ({"last_sync": "x", "ticks": 4}, False),
                 ({"last_sync": "x", "ticks": 5}, True), ({"last_sync": "x", "ticks": 1, "dirty": True}, True)]
        for bstate, due in cases:
            with self.subTest(bstate=bstate):
                self.assertEqual(orch.board_due(bstate, 6), due)


class Read(unittest.TestCase):
    def fragment_names(self, kind):
        body = re.search(r"\.\.\. on " + kind + r"\{([^}]*)\}", orch.BOARD_QUERY).group(1)
        return {t.split(":")[0] for t in re.findall(r"\w+:\s*\w+|\w+", body)}

    def test_issue_and_pr_state_use_distinct_response_names(self):
        # IssueState and PullRequestState differ, so a shared unaliased `state` is an invalid merge.
        shared = self.fragment_names("Issue") & self.fragment_names("PullRequest")
        self.assertEqual(shared, {"url"})

    def test_board_read_parses_the_aliased_states(self):
        class FakeGh:
            def graphql(self, query, variables, write=False):
                assert query == orch.BOARD_QUERY and not write
                node = lambda i, c: {"id": i, "content": c, "fieldValues": {"nodes": [
                    {"name": "Done", "field": {"name": "Status"}}]}}
                return {"rateLimit": {"remaining": 4000}, "node": {
                    "fields": {"nodes": [{"id": "F", "name": "Status", "options": [{"id": "o", "name": "Done"}]}]},
                    "items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                        node("A", {"url": f"{U}/vws-deploy/issues/1", "issueState": "CLOSED"}),
                        node("B", {"url": f"{U}/vws-deploy/pull/2", "prState": "MERGED"}),
                        node("C", {})]}}}
        fields, items, remaining = orch.board_read(FakeGh(), "PVT_x")
        self.assertEqual({u: i["state"] for u, i in items.items()},
                         {f"{U}/vws-deploy/issues/1": "CLOSED", f"{U}/vws-deploy/pull/2": "MERGED"})
        self.assertEqual((items[f"{U}/vws-deploy/pull/2"]["values"], remaining), ({"Status": "Done"}, 4000))


class Guard(unittest.TestCase):
    def test_only_item_mutations_from_the_templates(self):
        gh = orch.Gh(runner=lambda *a, **k: self.fail("no call expected"))
        bad = ["mutation{ m0: deleteProjectV2Item(input:{projectId:\"P\", itemId:\"I\"}){ clientMutationId } }",
               "mutation($p:ID!){ m0: addLabelsToLabelable(input:{labelableId:$p, labelIds:[]}){ clientMutationId } }",
               "mutation($p:ID!){ m0: addProjectV2ItemById(input:{projectId:$p, contentId:\"x\"}){ item{ id } } }"]
        for q in bad:
            with self.subTest(q=q[:40]):
                with self.assertRaises(PermissionError):
                    gh.graphql(q, {}, write=True)
        with self.assertRaises(PermissionError):
            gh.graphql("query{ viewer{ login } }", {})

    def test_templates_pass_the_guard(self):
        self.assertFalse(orch.board_write_ok("mutation($p:ID!){ %s m1: deleteProjectV2Item(input:{}){ x } }"
                                             % orch.BOARD_ADD.format(i=0)))
        q = "mutation($p:ID!, $c0:ID!, $i1:ID!, $f1:ID!, $v1:String!, $i2:ID!, $f2:ID!){ %s %s %s }" % (
            orch.BOARD_ADD.format(i=0), orch.BOARD_SET.format(i=1, kind="text"), orch.BOARD_CLEAR.format(i=2))
        self.assertTrue(orch.board_write_ok(q))

    def test_executor_batches_pass_the_guard(self):
        sent, slept = [], []

        def gql(query, variables, write=False):
            self.assertTrue(write and orch.board_write_ok(query), query)
            # The GitHub schema types ProjectV2FieldValue.singleSelectOptionId and .text as String.
            self.assertNotRegex(query, r"\$v[0-9]+:ID")
            sent.append(variables)
            return {f"m{i}": {"item": {"id": f"NEW{i}"}} for i in range(orch.BOARD_BATCH)}

        gh = type("G", (), {"graphql": staticmethod(gql)})()
        its = items(*[item("vws-iam", n, ("ready",), body="Depends on #1") for n in range(1, 13)])
        muts, _ = orch.board_plan(its, {}, 50)
        fields = {"Status": {"id": "FS", "options": {"Ready": "o1"}}, "Priority": {"id": "FP", "options": {"Normal": "o2"}},
                  "Owner": {"id": "FO", "options": {}}, "Depends on": {"id": "FD", "options": {}}}
        self.assertEqual(orch.board_write(gh, "PVT_x", fields, muts, slept.append), 48)
        self.assertEqual([len(v) - 1 for v in sent], [12, 60, 48])  # 12 adds, then 36 sets in batches of 20 (3 vars each)
        self.assertEqual(slept, [orch.BOARD_PAUSE] * 2)
        self.assertEqual(sent[1]["i0"], "NEW0")

    def test_config_is_validated(self):
        for k, v in (("ORCHESTRATE_BOARD", "yes"), ("ORCHESTRATE_BOARD_WRITES", "0"),
                     ("ORCHESTRATE_BOARD_EVERY", "2.5"), ("ORCHESTRATE_BOARD_PROJECT", "x\" }")):
            with self.subTest(k=k):
                with self.assertRaises(ValueError):
                    orch.load_config(orch.Paths({"ORCHESTRATE_CONFIG_DIR": "/nonexistent"}), {k: v})


if __name__ == "__main__":
    unittest.main()
