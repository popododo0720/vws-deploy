"""Unit tests for .github/drift/proto_drift.py (vws-deploy#404 AC3, AC4). Fixtures only, no network.

Run: python3 -m unittest discover -s tests/drift
"""
import base64
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(ROOT, "tests", "drift", "fixtures", "proto")
spec = importlib.util.spec_from_file_location("proto_drift", os.path.join(ROOT, ".github", "drift", "proto_drift.py"))
pd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pd)

TAGS = ["v0.5.0", "v0.4.0", "v0.3.0", "v0.2.0", "v0.1.0"]


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class ParseGoMod(unittest.TestCase):
    def test_require_block(self):
        self.assertEqual(pd.parse_gomod(fixture("consumer-ok.go.mod")), ("v0.5.0", False))

    def test_replace_block_on_vws_proto(self):
        self.assertEqual(pd.parse_gomod(fixture("consumer-replace.go.mod")), ("v0.4.0", True))

    def test_single_line_replace(self):
        text = b"module m\n\nrequire github.com/popododo0720/vws-proto v0.5.0\n" \
               b"replace github.com/popododo0720/vws-proto => ../vws-proto\n"
        self.assertEqual(pd.parse_gomod(text), ("v0.5.0", True))

    def test_indirect_pseudo_version(self):
        self.assertEqual(pd.parse_gomod(fixture("consumer-pseudo.go.mod")),
                         ("v0.5.1-0.20261023022740-3fbc83c36744", False))

    def test_comment_and_prefix_module_are_not_a_require(self):
        self.assertEqual(pd.parse_gomod(fixture("not-consumer.go.mod")), (None, False))

    def test_replace_of_other_module_only(self):
        self.assertFalse(pd.parse_gomod(fixture("consumer-ok.go.mod"))[1])


class LatestTag(unittest.TestCase):
    def test_semver_order_not_string_order(self):
        self.assertEqual(pd.latest_tag(["v0.9.0", "v0.10.0", "v0.2.1"]), "v0.10.0")

    def test_prerelease_and_junk_are_ignored(self):
        self.assertEqual(pd.latest_tag(["v0.6.0-rc1", "latest", "v0.5.0"]), "v0.5.0")

    def test_no_tag(self):
        self.assertIsNone(pd.latest_tag([]))
        self.assertIsNone(pd.latest_tag(["main"]))


class VersionIssues(unittest.TestCase):
    def test_latest_is_ok(self):
        self.assertEqual(pd.version_issues("v0.5.0", False, "v0.5.0", TAGS), [])

    def test_one_minor_behind_is_ok(self):
        self.assertEqual(pd.version_issues("v0.4.0", False, "v0.5.0", TAGS), [])

    def test_two_minors_behind_fails(self):
        self.assertEqual(pd.version_issues("v0.3.0", False, "v0.5.0", TAGS),
                         ["v0.3.0 is 2 minor versions behind v0.5.0 (max 1)"])

    def test_major_behind_fails(self):
        self.assertEqual(pd.version_issues("v0.5.0", False, "v1.0.0", TAGS + ["v1.0.0"]),
                         ["v0.5.0 is a major version behind v1.0.0"])

    def test_replace_fails(self):
        self.assertIn("replace for vws-proto in go.mod", pd.version_issues("v0.5.0", True, "v0.5.0", TAGS))

    def test_pseudo_version_fails(self):
        self.assertEqual(pd.version_issues("v0.5.1-0.20261023022740-3fbc83c36744", False, "v0.5.0", TAGS),
                         ["v0.5.1-0.20261023022740-3fbc83c36744 is a pseudo-version, not a release tag"])

    def test_untagged_version_fails(self):
        self.assertEqual(pd.version_issues("v0.4.9", False, "v0.5.0", TAGS), ["v0.4.9 is not a vws-proto tag"])

    def test_not_a_consumer(self):
        self.assertEqual(pd.version_issues(None, False, "v0.5.0", TAGS), [])

    def test_replace_without_require_fails(self):
        self.assertEqual(pd.version_issues(None, True, "v0.5.0", TAGS), ["replace for vws-proto in go.mod"])

    def test_unknown_latest_fails_closed(self):
        self.assertEqual(pd.version_issues("v0.5.0", False, None, []), ["latest vws-proto tag unreadable"])


class CopyIssues(unittest.TestCase):
    """The `rg -l '^// source: vws/' -g '*.pb.go'` check, over a repo tree listing."""

    def reader(self, files):
        return lambda path: files.get(path)

    def test_generated_copy_by_source_header(self):
        files = {"internal/api/image.pb.go": fixture("copied.pb.go.txt")}
        self.assertEqual(pd.copy_issues(list(files) + ["main.go"], self.reader(files)),
                         ["1 vws-proto pb.go copy: internal/api/image.pb.go"])

    def test_third_party_dir_and_gen_path_are_copies_without_reading(self):
        paths = ["third_party/vws-proto/go.mod", "x/gen/go/vws/common/v1/error.pb.go"]
        got = pd.copy_issues(paths, self.reader({}))
        self.assertEqual(got, ["2 vws-proto pb.go copies: third_party/vws-proto/go.mod, "
                               "x/gen/go/vws/common/v1/error.pb.go"])

    def test_own_proto_is_not_a_copy(self):
        files = {"api/health.pb.go": fixture("own.pb.go.txt")}
        self.assertEqual(pd.copy_issues(list(files), self.reader(files)), [])

    def test_unreadable_pb_go_fails_closed(self):
        self.assertEqual(pd.copy_issues(["api/x.pb.go"], self.reader({})), ["api/x.pb.go unreadable"])

    def test_vendor_proto_script_is_not_a_copy(self):
        self.assertEqual(pd.copy_issues(["scripts/vendor-proto.sh", "third_party/vws-lib/go.mod"],
                                        self.reader({})), [])

    def test_many_copies_are_capped(self):
        paths = [f"third_party/vws-proto/f{i}.pb.go" for i in range(5)]
        self.assertEqual(pd.copy_issues(paths, self.reader({})),
                         ["5 vws-proto pb.go copies: third_party/vws-proto/f0.pb.go, "
                          "third_party/vws-proto/f1.pb.go, third_party/vws-proto/f2.pb.go (+2 more)"])


def _b64(b):
    return {"content": base64.b64encode(b).decode()}


class ProtoCell(unittest.TestCase):
    """proto_cell reads one repo's tree and go.mod files through a fake gh api."""

    def api(self, files, truncated=False, tree=True):
        def gh(path):
            if path == "repos/o/r/git/trees/main?recursive=1":
                if not tree:
                    return None
                return {"truncated": truncated,
                        "tree": [{"type": "blob", "path": p} for p in files] + [{"type": "tree", "path": "x"}]}
            prefix = "repos/o/r/contents/"
            if path.startswith(prefix) and path[len(prefix):] in files:
                return _b64(files[path[len(prefix):]])
            return None
        return gh

    def test_consumer_on_latest(self):
        gh = self.api({"go.mod": fixture("consumer-ok.go.mod"), "main.go": b"package main"})
        self.assertEqual(pd.proto_cell("o", "r", "v0.5.0", TAGS, gh), ([], "v0.5.0 (latest v0.5.0)"))

    def test_nested_go_mod_is_checked_and_vendored_go_mod_is_not(self):
        gh = self.api({"go.mod": fixture("not-consumer.go.mod"), "installer/go.mod": fixture("consumer-ok.go.mod"),
                       "third_party/vws-lib/go.mod": fixture("consumer-replace.go.mod")})
        self.assertEqual(pd.proto_cell("o", "r", "v0.5.0", TAGS, gh),
                         ([], "installer/go.mod v0.5.0 (latest v0.5.0)"))

    def test_not_used(self):
        gh = self.api({"go.mod": fixture("not-consumer.go.mod")})
        self.assertEqual(pd.proto_cell("o", "r", "v0.5.0", TAGS, gh), ([], "not used (latest v0.5.0)"))

    def test_behind_replace_and_copy_all_reported(self):
        gh = self.api({"go.mod": fixture("consumer-replace.go.mod"),
                       "third_party/vws-proto/gen/go/vws/compute/v1/image.pb.go": fixture("copied.pb.go.txt")})
        issues, _ = pd.proto_cell("o", "r", "v0.6.0", TAGS + ["v0.6.0"], gh)
        self.assertEqual(issues, ["go.mod: v0.4.0 is 2 minor versions behind v0.6.0 (max 1)",
                                  "go.mod: replace for vws-proto in go.mod",
                                  "1 vws-proto pb.go copy: third_party/vws-proto/gen/go/vws/compute/v1/image.pb.go"])

    def test_truncated_tree_fails_closed(self):
        issues, _ = pd.proto_cell("o", "r", "v0.5.0", TAGS, self.api({"go.mod": b"module m\n"}, truncated=True))
        self.assertEqual(issues, ["repo tree truncated; pb.go copy check incomplete"])

    def test_unreadable_tree_fails_closed(self):
        issues, _ = pd.proto_cell("o", "r", "v0.5.0", TAGS, self.api({}, tree=False))
        self.assertEqual(issues, ["repo tree unreadable"])

    def test_unreadable_go_mod_fails_closed(self):
        gh = self.api({"go.mod": b""})
        gh2 = lambda p: None if p.endswith("contents/go.mod") else gh(p)  # noqa: E731
        issues, _ = pd.proto_cell("o", "r", "v0.5.0", TAGS, gh2)
        self.assertEqual(issues, ["go.mod unreadable"])


class Tags(unittest.TestCase):
    def test_tag_names(self):
        def gh(p):
            return [{"name": "v0.5.0"}, {"name": "v0.4.0"}] if p.startswith("repos/o/vws-proto/tags") else None
        self.assertEqual(pd.proto_tags("o", gh), ["v0.5.0", "v0.4.0"])

    def test_unreadable_tags(self):
        self.assertIsNone(pd.proto_tags("o", lambda p: None))


class DocRule(unittest.TestCase):
    def test_design_note_states_the_rule(self):
        with open(os.path.join(ROOT, "docs", "design", "proto-module.md")) as f:
            text = f.read()
        self.assertIn(f"more than {pd.MAX_MINOR_LAG} minor version behind", text)
        self.assertIn(".github/drift/proto_drift.py", text)


if __name__ == "__main__":
    unittest.main()
