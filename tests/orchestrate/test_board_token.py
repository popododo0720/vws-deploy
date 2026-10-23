"""board_sync token tests for .github/orchestrate/orchestrate.py: board GraphQL calls use the
project-scope token from ORCHESTRATE_BOARD_TOKEN_FILE, REST calls keep the gh login.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import tempfile
import unittest
from unittest import mock

from orch_testlib import CFG, orch

TOKEN = "ghp_BoardOnlyToken0123456789abcdefABCDEF"
REST_TOKEN = "ghp_AmbientEnvToken0123456789abcdefABCD"

BOARD_PAGE = {"data": {"rateLimit": {"remaining": 4000}, "node": {
    "fields": {"nodes": []},
    "items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}}


class Result:
    def __init__(self, body):
        self.stdout = "HTTP/2.0 200 OK\r\nEtag: \"e\"\r\n\r\n" + json.dumps(body)
        self.stderr = ""


class Runner:
    """Records every gh call with the GH_TOKEN it was given."""
    def __init__(self, graphql_body=BOARD_PAGE):
        self.calls, self.graphql_body = [], graphql_body

    def __call__(self, args, input=None, env=None, **_):
        kind = "graphql" if "graphql" in args else "rest"
        self.calls.append((kind, (env or {}).get("GH_TOKEN"), (env or {}).get("GITHUB_TOKEN")))
        if kind == "graphql":
            return Result(self.graphql_body)
        return Result({"resources": {"core": {"remaining": 5000}}})


class BoardToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.paths = orch.Paths({"ORCHESTRATE_CONFIG_DIR": os.path.join(d, "cfg"),
                                 "ORCHESTRATE_STATE_DIR": os.path.join(d, "state")})
        os.makedirs(self.paths.state)
        self.token_file = os.path.join(d, "board-sync-token")
        self.cfg = dict(CFG, ORCHESTRATE_BOARD_TOKEN_FILE=self.token_file)
        self.log = orch.Log(self.paths, self.cfg, "run", "dry-run")
        self.env = mock.patch.dict(os.environ, {"GH_TOKEN": REST_TOKEN, "GITHUB_TOKEN": REST_TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def sync(self, runner, label_change=False):
        gh = orch.Gh(runner=runner, sleep=lambda s: None)
        gh.get("rate_limit")  # a REST call before the sync
        orch.board_sync(gh, {"items": {}}, self.cfg, self.paths, self.log, "dry-run", label_change)
        gh.get("rate_limit")  # and one after it
        return gh

    def events(self):
        with open(self.paths.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def log_text(self):
        with open(self.paths.log, encoding="utf-8") as f:
            return f.read()

    def write_token(self, text):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_or_empty_token_skips_with_one_log_line_and_no_graphql(self):
        for content in (None, "", "  \n"):
            with self.subTest(content=content):
                if content is not None:
                    self.write_token(content)
                runner = Runner()
                self.sync(runner, label_change=True)
                self.assertEqual([c[0] for c in runner.calls], ["rest", "rest"])
                skipped = [e for e in self.events() if e["event"].startswith("board")]
                self.assertEqual([(e["event"], e.get("reason")) for e in skipped][-1:],
                                 [("board_sync_skipped", "token_missing")])
                self.assertNotIn("board_sync_error", [e["event"] for e in self.events()])
                # The label change is kept, so the first tick with a token syncs.
                self.assertTrue(orch.read_json(self.paths.board_file, {}).get("dirty"))

    def test_unreadable_token_file_skips(self):
        os.makedirs(self.token_file)  # a directory cannot be read as a file
        runner = Runner()
        self.sync(runner)
        self.assertEqual([c[0] for c in runner.calls], ["rest", "rest"])
        self.assertEqual(self.events()[-1]["event"], "board_sync_skipped")

    def test_graphql_gets_the_file_token_and_rest_gets_none(self):
        self.write_token(TOKEN + "\n")
        runner = Runner()
        gh = self.sync(runner)
        graphql = [c for c in runner.calls if c[0] == "graphql"]
        rest = [c for c in runner.calls if c[0] == "rest"]
        self.assertEqual(graphql, [("graphql", TOKEN, None)])
        self.assertEqual(rest, [("rest", None, None), ("rest", None, None)])
        self.assertIn("board_synced", [e["event"] for e in self.events()])
        self.assertIsNone(gh.board_token)

    def test_token_never_appears_in_the_log(self):
        # The error path echoes the GitHub error text; a token in it must still be redacted.
        self.write_token(TOKEN)
        runner = Runner({"errors": [{"message": f"bad credentials for {TOKEN}"}]})
        self.sync(runner)
        self.assertIn("board_sync_error", [e["event"] for e in self.events()])
        self.assertNotIn(TOKEN, self.log_text())
        # Also when the token does not look like a GitHub token to the generic scrubber.
        odd = "plain-secret-value-without-prefix"
        self.write_token(odd)
        self.sync(Runner({"errors": [{"message": f"bad credentials for {odd}"}]}))
        self.assertNotIn(odd, self.log_text())

    def test_graphql_without_a_board_token_is_refused(self):
        runner = Runner()
        gh = orch.Gh(runner=runner, sleep=lambda s: None)
        with self.assertRaises(PermissionError):
            gh.graphql(orch.BOARD_QUERY, {"p": "PVT_x", "after": None})
        self.assertEqual(runner.calls, [])

    def test_token_file_must_be_absolute(self):
        for v in ("relative/token", "/root/x\nGH_TOKEN=y"):
            with self.subTest(v=v):
                with self.assertRaises(ValueError):
                    orch.load_config(orch.Paths({"ORCHESTRATE_CONFIG_DIR": "/nonexistent"}),
                                     {"ORCHESTRATE_BOARD_TOKEN_FILE": v})
        cfg = orch.load_config(orch.Paths({"ORCHESTRATE_CONFIG_DIR": "/nonexistent"}), {})
        self.assertEqual(cfg["ORCHESTRATE_BOARD_TOKEN_FILE"], "/root/.config/vws/board-sync-token")


if __name__ == "__main__":
    unittest.main()
