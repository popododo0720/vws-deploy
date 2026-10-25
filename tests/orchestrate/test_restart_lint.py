"""The restart-test lint (vws-deploy#517 AC2, class 3). Every function in .github/orchestrate/*.py that makes
two or more GitHub writes, counted from its own body, must be named in the covers= of a
fault_gh.restart_check call in tests/orchestrate/test_*.py, and every covers= name must be such a function.

A GitHub write is `<x>.write(...)` with two or more positional arguments (Gh.write takes a method and a
path; a file's write takes one) or `<x>.graphql(..., write=<anything but False>)`. A function makes two or
more when it has two such calls or one inside a loop or a comprehension. Callers of such a function (tick,
apply's callers) are not counted: their restart test is the callee's, which runs whole ticks."""
import ast
import glob
import os
import unittest

import fault_gh
from fault_gh import FaultGitHub, restart_check
from orch_testlib import FIXTURES, ROOT, orch

HERE = os.path.dirname(os.path.abspath(__file__))
LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def is_write(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr == "write":
        return len(node.args) >= 2
    if node.func.attr == "graphql":
        kw = next((k.value for k in node.keywords if k.arg == "write"), None)
        return kw is not None and not (isinstance(kw, ast.Constant) and kw.value is False)
    return False


def _writes(node, in_loop=False):
    """(write calls, any inside a loop) in node's body, not descending into nested functions."""
    count, looped = 0, False
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        loop = in_loop or isinstance(child, LOOPS)
        if is_write(child):
            count += 1
            looped = looped or loop
        c, lp = _writes(child, loop)
        count, looped = count + c, looped or lp
    return count, looped


def multi_write_functions(paths):
    """{function name: "path:line"} for each function (or method) making two or more GitHub writes."""
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                count, looped = _writes(node)
                if count >= 2 or (count and looped):
                    out[node.name] = f"{os.path.relpath(path, ROOT)}:{node.lineno}"
    return out


def covered(paths):
    """{name: [test file]} from every restart_check(..., covers=(<string literals>)) call."""
    out = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "restart_check"):
                continue
            kw = next((k.value for k in node.keywords if k.arg == "covers"), None)
            if not isinstance(kw, (ast.Tuple, ast.List)) or not kw.elts or not all(
                    isinstance(e, ast.Constant) and isinstance(e.value, str) for e in kw.elts):
                raise ValueError(f"{path}:{node.lineno}: restart_check needs covers=(\"name\", ...) as string literals")
            for e in kw.elts:
                out.setdefault(e.value, []).append(os.path.basename(path))
    return out


def lint(src_paths, test_paths):
    """Problems: multi-write functions without a restart test, and covers= names that are none."""
    funcs, cov = multi_write_functions(src_paths), covered(test_paths)
    problems = [f"{where}: {name} makes two or more GitHub writes but no restart_check(covers=...) names it"
                for name, where in sorted(funcs.items()) if name not in cov]
    problems += [f"{', '.join(sorted(set(files)))}: covers={name!r} is not an orchestrate function with two or more GitHub writes"
                 for name, files in sorted(cov.items()) if name not in funcs]
    return problems


ORCH_SRC = sorted(glob.glob(os.path.join(ROOT, ".github", "orchestrate", "*.py")))
TESTS = sorted(p for p in glob.glob(os.path.join(HERE, "test_*.py")) if os.path.basename(p) != "test_restart_lint.py")  # its harness self-test covers a toy
LINT_FX = os.path.join(FIXTURES, "class3", "lint")


class Lint(unittest.TestCase):
    def test_every_multi_write_function_has_a_restart_test(self):
        self.assertEqual(lint(ORCH_SRC, TESTS), [])

    def test_it_finds_the_known_multi_write_functions(self):
        """Not vacuous: today's orchestrate has these three (apply, the AC tick, the board batches)."""
        self.assertLessEqual({"apply", "apply_ac_tick", "board_write"}, set(multi_write_functions(ORCH_SRC)))
        self.assertNotIn("publish_status", multi_write_functions(ORCH_SRC), "one write is not a restart risk")


class LintFixtures(unittest.TestCase):
    SRC = [os.path.join(LINT_FX, "orch_two_writes.py")]

    def test_detector(self):
        self.assertEqual(sorted(multi_write_functions(self.SRC)), ["batched_graphql", "in_a_loop", "two_sites"])

    def test_failing_fixture_uncovered_functions(self):
        problems = lint(self.SRC, [os.path.join(LINT_FX, "test_covers_two_sites.py")])
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("tests/orchestrate/fixtures/class3/lint/orch_two_writes.py:"))
        self.assertIn("batched_graphql makes two or more GitHub writes", problems[0])
        self.assertIn("in_a_loop makes two or more GitHub writes", problems[1])

    def test_stale_covers_name(self):
        problems = lint([os.path.join(LINT_FX, "orch_two_writes.py")], [os.path.join(LINT_FX, "test_covers_two_sites.py")]
                        + TESTS)
        self.assertIn("covers='apply' is not an orchestrate function", "\n".join(problems))

    def test_passing_fixture(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("restart_check(self, s, covers=('two_sites', 'in_a_loop'))\n"
                    "restart_check(self, s, covers=['batched_graphql'])\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(lint(self.SRC, [f.name]), [])

    def test_covers_must_be_literal(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write("restart_check(self, s, covers=NAMES)\n")
        self.addCleanup(os.unlink, f.name)
        with self.assertRaises(ValueError):
            lint(self.SRC, [f.name])


class Toy:
    """A two-write job against FaultGitHub: a comment on issue 1, then a label."""

    def __init__(self, dedupe):
        self.dedupe = dedupe
        issue = {"number": 1, "state": "open", "body": "", "labels": []}
        self.gh = FaultGitHub({f"{fault_gh.O}/vws-deploy/issues/1": {"body": issue}}, 1_800_000_000)

    def run(self):
        gh = orch.Gh(None, runner=self.gh, sleep=lambda s: None)
        base = f"{fault_gh.O}/vws-deploy/issues/1"
        seen = [c["body"] for c in gh.get_all(f"{base}/comments")]
        try:
            if not (self.dedupe and "done" in seen):
                gh.write("POST", f"{base}/comments", {"body": "done"})
            gh.write("POST", f"{base}/labels", {"labels": ["ready"]})
        except orch.GhError:
            pass


class HarnessSelfTest(unittest.TestCase):
    """restart_check fails a job that duplicates a comment on rerun and passes one that does not."""

    def outcome(self, dedupe):
        class Inner(unittest.TestCase):
            def runTest(inner):
                restart_check(inner, lambda: Toy(dedupe), covers=("toy",))
        result = unittest.TestResult()
        Inner().run(result)
        return result

    def test_non_idempotent_job_fails(self):
        r = self.outcome(dedupe=False)
        self.assertTrue(r.failures, "a rerun that posts the comment twice must fail the restart check")
        self.assertIn("'done', 'done'", "\n".join(t for _, t in r.failures))

    def test_idempotent_job_passes(self):
        r = self.outcome(dedupe=True)
        self.assertEqual((r.failures, r.errors), ([], []))


if __name__ == "__main__":
    unittest.main()
