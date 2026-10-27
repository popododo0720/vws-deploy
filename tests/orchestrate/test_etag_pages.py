"""get_all and the ETag cache on a growing list (vws-deploy#575): a full page that was once the last page
is cached with next=False; GitHub answers 304 for it with no Link header after more items are added, so
the cached next=False must not hide the newer pages.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import subprocess
import unittest

from orch_testlib import orch

O = "repos/popododo0720"
BASE = f"{O}/vws-deploy/issues/339/comments?per_page=100"
KEY = "sha256:2ac98180d02f4d2cb52f378794d97f919bdbe6bef5fc63ddba5e69224b126a74"


def page_path(n):
    return BASE if n == 1 else f"{BASE}&page={n}"


class FakeGh:
    """Runner for orch.Gh: answers each page with its ETag, 304 on a matching If-None-Match.
    Like GitHub, a 304 carries no Link header."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, args, **kw):
        path = args[5]
        inm = next((a.split(": ", 1)[1] for a in args if a.startswith("If-None-Match: ")), "")
        self.calls.append((path, inm))
        n = next(i for i in range(1, len(self.pages) + 1) if page_path(i) == path)
        etag = f'"p{n}-{len(self.pages[n - 1])}"'
        if inm == etag:
            out = f"HTTP/2.0 304 Not Modified\nEtag: {etag}\n\n"
        else:
            link = f'Link: <https://api.github.com/{page_path(n + 1)}>; rel="next"\n' if n < len(self.pages) else ""
            out = f"HTTP/2.0 200 OK\nEtag: {etag}\n{link}\n{json.dumps(self.pages[n - 1])}"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


def comments(lo, hi, key_at=None):
    return [{"id": i, "body": f"<!-- orchestrate:lead-wake {KEY} -->" if i == key_at else f"c{i}"}
            for i in range(lo, hi)]


class StaleLastPage(unittest.TestCase):
    def gh(self, runner, cache):
        gh = orch.Gh(runner=runner, sleep=lambda s: None)
        gh.cache = cache
        return gh

    def test_full_cached_last_page_is_revalidated_after_304(self):
        p1, p2 = comments(0, 100), comments(100, 200)
        # The cache from a tick when the thread had exactly 200 comments: page 2 was the last page.
        cache = {page_path(1): {"etag": '"p1-100"', "data": p1, "next": True},
                 page_path(2): {"etag": '"p2-100"', "data": p2, "next": False}}
        fake = FakeGh([p1, p2, comments(200, 288, key_at=250)])
        items = self.gh(fake, cache).get_all(BASE)
        self.assertEqual(len(items), 288, "page 3 must be read")
        self.assertTrue(any(KEY in c["body"] for c in items), "the lead-wake key on page 3 must be seen")
        self.assertEqual([p for p, _ in fake.calls].count(page_path(2)), 2, "page 2 re-read once")
        self.assertEqual(fake.calls[2], (page_path(2), ""), "the re-read drops If-None-Match")

    def test_revalidated_page_is_cached_with_its_new_next(self):
        p1, p2, p3 = comments(0, 100), comments(100, 200), comments(200, 288)
        cache = {page_path(1): {"etag": '"p1-100"', "data": p1, "next": True},
                 page_path(2): {"etag": '"p2-100"', "data": p2, "next": False}}
        gh = self.gh(FakeGh([p1, p2, p3]), cache)
        gh.get_all(BASE)
        self.assertTrue(gh.used[page_path(2)]["next"])
        fake = FakeGh([p1, p2, p3])
        self.assertEqual(len(self.gh(fake, gh.used).get_all(BASE)), 288)
        self.assertTrue(all(inm for _, inm in fake.calls), "the next tick is all 304s")

    def test_partial_cached_last_page_keeps_the_etag_saving(self):
        p1, p2 = comments(0, 100), comments(100, 150)
        cache = {page_path(1): {"etag": '"p1-100"', "data": p1, "next": True},
                 page_path(2): {"etag": '"p2-50"', "data": p2, "next": False}}
        fake = FakeGh([p1, p2])
        gh = self.gh(fake, cache)
        self.assertEqual(len(gh.get_all(BASE)), 150)
        self.assertEqual(len(fake.calls), 2, "an unchanged partial page is provably still the last page")
        self.assertEqual(gh.stats["not_modified"], 2)

    def test_true_last_full_page_costs_one_extra_read(self):
        p1 = comments(0, 100)
        cache = {page_path(1): {"etag": '"p1-100"', "data": p1, "next": False}}
        fake = FakeGh([p1])
        self.assertEqual(len(self.gh(fake, cache).get_all(BASE)), 100)
        self.assertEqual(fake.calls, [(page_path(1), '"p1-100"'), (page_path(1), "")])


if __name__ == "__main__":
    unittest.main()
