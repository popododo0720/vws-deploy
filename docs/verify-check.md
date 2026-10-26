# verify-check: repeated verify checks as one script

`.github/verify/verify-check.py` runs two checks that vws-verify used to do by
hand on PR heads (vws-deploy#517 AC4). Both come from the "Considered, not
chosen as a top class" list in `docs/lessons/verify-findings-2026-10.md`.
The script only reads. It sends GitHub GETs through the orchestrator's `Gh`
client and read allowlist. In git, it only fetches `refs/pull/N/head` from
`origin` into `FETCH_HEAD`.

## Run it

From a vws-deploy checkout (any vws-* repo works with `--repo`; `--dir` must
be a checkout of that repo with an `origin` remote):

```bash
git -C <checkout> fetch origin main
.github/verify/verify-check.py all --repo vws-deploy --pr <N> --dir <checkout>
```

`ac` needs no checkout. `rebuild` and `all` need `--dir`. `--base` sets the
base ref for the merge bases (default `origin/main`).

Each finding is one line, `STATUS check: message`, and the last line is
`RESULT PASS` or `RESULT FAIL`. Exit codes:

| Exit | Meaning |
|---|---|
| 0 | No FAIL. Still read every CHECK and WARN line. |
| 1 | At least one FAIL. |
| 2 | The check could not run: bad input, or a GitHub or git read error. |

## `ac`: unmet AC while claiming done

The script reads the `AC:` lines and the `Part of` issue with the
orchestrator's own parsers (`parse_ac`, `PART_OF_RE`, `ac_boxes`), so the
result matches what the AC tick will do after merge.

- FAIL: the `AC:` lines are invalid, there is no `Part of #N` line, the
  issue is a pull request, `AC: <n>` names a box the issue does not have, or
  the issue has no boxes under `## Acceptance criteria` (the AC tick would
  skip the PR; use `AC: none - <reason>`).
- WARN: a claimed box is already ticked, or the issue is closed.
- CHECK: one line per claimed box, with the box text. The verdict confirms
  each one against the diff and the evidence. The script cannot judge
  whether code meets a sentence; it makes sure no claimed box is skipped.
- INFO: open boxes the PR does not claim.

## `rebuild`: a blocker carried unchanged into a rebuild

The script finds the newest vws-verify FAIL review (`CHANGES_REQUESTED` from
the verify login, as `verify_reviews` in the orchestrator reads it) on an
earlier head of the PR, or of a PR it rebuilds. A rebuild reference is
`-vN of #M`, `rebuild of #M`, `rebuilt of #M`, `supersedes #M` or
`replaces #M` in the title or visible body. References are followed up to
5 levels. A number that is not a readable pull request is a WARN and is
skipped. With no earlier FAIL, the check passes.

Against that FAIL head:

1. INFO: the `git range-diff` summary of the two PR ranges (each from its
   merge base with `--base`).
2. The PR's own non-docs changes must differ. For each file outside `docs/`
   and `*.md`, the script compares the patch-id of `git diff -U0` from each
   side's merge base. Main moving on does not count as a change. FAIL when
   no non-docs file differs.
3. Each numbered blocking item of the FAIL review that cites `file:line` or
   `file:L1-L2` spans (a bare `:L` after a space or `(` reuses the item's
   last path) must have at least one cited span whose lines are gone from the
   head. Lines are compared without indentation and with blank lines removed,
   so a moved but unchanged block still counts as carried. A span shorter
   than 16 characters is not judged. The review's `Non-blocking` section is
   left out. FAIL when every cited span of an item is still in the head.
   WARN when the review cites no span the script can judge.

A FAIL from step 3 means the cited code did not change. If the blocker was
fixed somewhere else, say where in the PR body. The verdict decides; the
script only shows that the cited lines are the same.

## Example: vws-deploy#500

#500 is the `-v3 of #494` rebuild that verify failed because the #494
blocker was still there. The script finds the same blocker:

```text
INFO rebuild: head 63b2ad2 of #500 against the newest FAIL: #494 head 31640e4 (https://github.com/popododo0720/vws-deploy/pull/494#pullrequestreview-5638458208)
PASS rebuild: non-docs changes differ from the FAIL head in .github/orchestrate/orchestrate.py
FAIL rebuild: blocking item 1 of the FAIL review is carried unchanged: every cited span (.github/orchestrate/orchestrate.py:2046-2047 at 31640e4) is still in the head 63b2ad2
RESULT FAIL
```

## Tests

`tests/verify/test_verify_check.py` uses the real GitHub data of #484, #494,
#500 and issue #411 (`tests/verify/fixtures/real-411.json`), the stub gh of
the orchestrator tests, and git histories built in a temp dir. CI runs it in
the drift-check workflow:

```bash
python3 -m unittest discover -s tests/verify -v
```
