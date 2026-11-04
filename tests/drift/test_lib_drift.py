"""Unit tests for .github/drift/lib_drift.py (vws-deploy#321 AC4). Fake gh api, no network.

Run: python3 -m unittest discover -s tests/drift
"""
import base64
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, ".github", "drift"))
spec = importlib.util.spec_from_file_location("lib_drift", os.path.join(ROOT, ".github", "drift", "lib_drift.py"))
ld = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ld)

GOOD_MOD = (b"module github.com/popododo0720/vws-lib\n\ngo 1.26.0\n\ntoolchain go1.26.9\n\n"
            b"require (\n\tgolang.org/x/net v0.60.0 // indirect\n)\n")
TAG = {"go.mod": "m1", "obs/obs.go": "o1", "examples/consumer/go.mod": "m2"}


def tree(blobs, truncated=False):
    return {"truncated": truncated, "tree": [{"type": "blob", "path": p, "sha": s} for p, s in blobs.items()]
            + [{"type": "tree", "path": "third_party", "sha": "t"}]}


def copy(blobs, root="third_party/vws-lib"):
    return {f"{root}/{p}": s for p, s in blobs.items()}


class GoModIssues(unittest.TestCase):
    def test_floors_met(self):
        self.assertEqual(ld.gomod_issues(GOOD_MOD), [])

    def test_no_toolchain_falls_back_to_go_line(self):
        # The 2026-10-17 vws-iam copy: go 1.26.0, no toolchain, x/net v0.59.0.
        text = b"module m\n\ngo 1.26.0\n\nrequire golang.org/x/net v0.59.0 // indirect\n"
        self.assertEqual(ld.gomod_issues(text),
                         ["toolchain go1.26.0 < go1.26.9", "golang.org/x/net v0.59.0 < v0.60.0"])

    def test_newer_go_line_without_toolchain_is_ok(self):
        self.assertEqual(ld.gomod_issues(b"module m\n\ngo 1.27\n"), [])

    def test_version_order_is_numeric(self):
        text = b"module m\ngo 1.26.0\ntoolchain go1.26.10\nrequire golang.org/x/net v0.100.0\n"
        self.assertEqual(ld.gomod_issues(text), [])

    def test_commented_toolchain_does_not_count(self):
        text = b"module m\n// toolchain go1.30.0\n"
        self.assertEqual(ld.gomod_issues(text), ["no toolchain or go line (want >= go1.26.9)"])

    def test_bad_values(self):
        text = b"module m\ntoolchain local\nrequire golang.org/x/net v0.61.0-0.20261001000000-abcdefabcdef\n"
        self.assertEqual(ld.gomod_issues(text), [
            "toolchain local unreadable",
            "golang.org/x/net v0.61.0-0.20261001000000-abcdefabcdef is not a release (want >= v0.60.0)"])

    def test_quoted_module_path_counts(self):
        text = b'module m\ngo 1.26.9\nrequire "golang.org/x/net" v0.59.0\n'
        self.assertEqual(ld.gomod_issues(text), ["golang.org/x/net v0.59.0 < v0.60.0"])

    def test_xnet_not_required_is_ok(self):
        self.assertEqual(ld.gomod_issues(b"module m\ntoolchain go1.26.9\n"), [])


class LibCell(unittest.TestCase):
    def api(self, mods=None, tags=("v0.2.0", "v0.1.1", "v0.10.0-rc1"), tag_tree=None):
        mods = {} if mods is None else mods
        calls = self.calls = []

        def api(path):
            calls.append(path)
            if path == "repos/o/vws-lib/tags?per_page=100":
                return None if tags is None else [{"name": t} for t in tags]
            if path == "repos/o/vws-lib/git/trees/v0.2.0?recursive=1":
                return tree(TAG) if tag_tree is None else tag_tree
            if path.startswith("repos/o/r/contents/"):
                body = mods.get(path[len("repos/o/r/contents/"):], GOOD_MOD)
                return None if body is None else {"content": base64.b64encode(body).decode(), "size": len(body)}
            return None
        return api

    def cell(self, blobs, api, truncated=False):
        rel = lambda: ld.release("o", api)  # noqa: E731
        return ld.lib_cell("o", "r", tree(blobs, truncated), rel, api)

    def test_copy_at_latest_tag_is_ok(self):
        api = self.api()
        self.assertEqual(self.cell({"go.mod": "x", **copy(TAG)}, api), ([], "third_party/vws-lib = v0.2.0"))

    def test_vendor_record_is_ignored(self):
        self.assertEqual(self.cell(copy({**TAG, "VENDOR": "v"}), self.api())[0], [])

    def test_stale_copy_names_the_files(self):
        stale = {**TAG, "obs/obs.go": "old", "extra.go": "e"}
        del stale["examples/consumer/go.mod"]
        issues, _ = self.cell(copy(stale), self.api())
        self.assertEqual(issues, ["third_party/vws-lib is stale: 3 file(s) differ from vws-lib v0.2.0: "
                                  "examples/consumer/go.mod, extra.go, obs/obs.go"])

    def test_nested_copy_and_old_gomod(self):
        old = b"module m\ngo 1.26.0\n"
        api = self.api({"svc/third_party/vws-lib/go.mod": old})
        issues, ok = self.cell(copy(TAG, "svc/third_party/vws-lib"), api)
        self.assertEqual(issues, ["svc/third_party/vws-lib/go.mod: toolchain go1.26.0 < go1.26.9"])
        self.assertEqual(ok, "svc/third_party/vws-lib = v0.2.0")

    def test_lookalike_dir_is_not_a_copy(self):
        api = self.api()
        self.assertEqual(self.cell({"third_party/vws-lib-old/go.mod": "x", "my_third_party/vws-lib/a": "y"}, api),
                         ([], "not vendored"))
        self.assertEqual(self.calls, [], "no vws-lib read without a copy")

    def test_unreadable_gomod_is_drift(self):
        api = self.api({"third_party/vws-lib/go.mod": None})
        self.assertEqual(self.cell(copy(TAG), api)[0], ["third_party/vws-lib/go.mod unreadable"])

    def test_unreadable_release_is_drift(self):
        self.assertEqual(self.cell(copy(TAG), self.api(tags=None))[0], ["vws-lib release tag unreadable"])
        self.assertEqual(self.cell(copy(TAG), self.api(tag_tree=tree(TAG, truncated=True)))[0],
                         ["vws-lib release v0.2.0 unreadable"])

    def test_blob_without_sha_is_drift(self):
        t = tree(copy(TAG))
        t["tree"].append({"type": "blob", "path": "third_party/vws-lib/extra.go"})
        self.assertEqual(ld.lib_cell("o", "r", t, lambda: ld.release("o", self.api()), self.api())[0],
                         ["repo tree unreadable or truncated; vws-lib copy check incomplete"])

    def test_truncated_or_unreadable_repo_tree_is_drift(self):
        want = ["repo tree unreadable or truncated; vws-lib copy check incomplete"]
        self.assertEqual(self.cell(copy(TAG), self.api(), truncated=True)[0], want)
        self.assertEqual(ld.lib_cell("o", "r", None, lambda: (None, None), self.api())[0], want)


if __name__ == "__main__":
    unittest.main()
