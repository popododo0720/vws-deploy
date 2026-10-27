# Consume vws-proto by tag as a Go module (#404)

## Problem

Service repos used to copy vws-proto's generated `*.pb.go` into `third_party/vws-proto` and point a `replace` at it. The copies drifted (vws-network kept the removed `node.proto`, vws-compute sat near v0.1.0), each copy brought a nested `go.mod` that broke `govulncheck` (#321), and nothing failed when a copy went stale.

## Chosen

- **One module, path unchanged.** `github.com/popododo0720/vws-proto` at the repo root; packages are `github.com/popododo0720/vws-proto/gen/go/vws/<service>/v1`. Import lines in consumers never change, only `go.mod` does.
- **Generated Go is committed in vws-proto.** `gen/go` stays in the repo and vws-proto CI fails when it differs from `buf generate` (`scripts/check-generated-go.sh`, plus buf lint, build and breaking). Consumers never run buf or protoc, so their builds stay hermetic.
- **Consumers `require` a release tag.** Each service repo's `go.mod` requires `vX.Y.Z` with no `replace`, no pseudo-version and no `third_party/vws-proto` directory. `go.sum` pins the hashes.
- **No auth.** All vws-* repos are public, so `go get` resolves through the default `GOPROXY=https://proxy.golang.org,direct` and checks `sum.golang.org`. No token, `GOPRIVATE`, `GONOSUMDB` or `-mod=vendor`.

## Versioning

- Semver tags `vX.Y.Z`, cut from vws-proto `main` only.
- While on v0: a new field, message, RPC or enum value is a minor bump; a change to comments or options only is a patch.
- A wire-breaking change needs the existing `buf breaking` exception, a minor bump, and a release note that names every consumer that must change.
- Tags are immutable. proxy.golang.org and sum.golang.org cache a version forever, so a tag is never moved or deleted. A bad release is fixed by a new patch tag.
- v1.0.0 is a separate, later decision.

## How a proto change reaches consumers

1. A vws-proto PR merges to `main` after CI passes.
2. The lead tags the merge commit and publishes a release:

   ```sh
   git tag -a vX.Y.Z -m "vX.Y.Z" <sha>
   git push origin vX.Y.Z
   gh release create vX.Y.Z --repo popododo0720/vws-proto --generate-notes
   GOPROXY=https://proxy.golang.org go list -m github.com/popododo0720/vws-proto@vX.Y.Z
   ```

   The last command warms the proxy and proves `go get` resolves the tag. A tag-triggered workflow can automate this step later.
3. Each consumer bumps in its own PR:

   ```sh
   go get github.com/popododo0720/vws-proto@vX.Y.Z
   go mod tidy
   go build ./... && go test -race ./...
   ```

   The repo's existing `go mod tidy && git diff --exit-code go.mod go.sum` CI step keeps `go.sum` honest.

Merged commits on vws-proto `main` after the latest tag reach no consumer until the next tag. That is intended: a consumer only ever sees a reviewed, tagged release.

## Checks that fail on drift

Cross-repo, in vws-deploy drift-check (present: `.github/drift/proto_drift.py`, called by `.github/drift/drift_check.py` as the `vws-proto` column; daily schedule and every drift-check PR run):

- reports each Go repo's required vws-proto version from every `go.mod` (outside `third_party/`, `vendor/` and `testdata/`) next to the latest release tag (the highest `vX.Y.Z` tag on vws-proto; pre-releases are ignored);
- fails when a consumer is more than 1 minor version behind the latest tag, or on any older major version. With latest v0.5.0, v0.4.x passes and v0.3.x fails. One minor of lag gives each consumer one release cycle to bump in its own PR;
- fails on a `replace` for vws-proto, a pseudo-version, or a version that is not a vws-proto tag;
- fails on vws-proto generated code in the tree: anything under `third_party/vws-proto/`, a `*.pb.go` under a `gen/go/vws/` path, or any other `*.pb.go` whose header is `// source: vws/...` (the `rg -l '^// source: vws/' -g '*.pb.go'` check, read through the contents API). A file over about 1MB comes back as `encoding: none` with an empty or missing body; that file is read through the Git Blobs API;
- fails closed: an unreadable tree, `go.mod` or tag list, and a truncated tree listing, are drift. A Contents or Blobs response with `encoding: none` and an empty or missing body is unreadable, so a large `*.pb.go` whose bytes the API did not return cannot pass the source-header check.

Per consumer, in the repo's own tests: not present, and not planned. No service repo has a test for its `go.mod` vws-proto entry (checked on 2026-10-27 in the five consumers). drift-check is the one place for the rule, so it is not copied into five repos. The cost: a consumer PR that adds a copy is caught by the next daily run, not at the PR.

`govulncheck ./...` exits 0 per consumer: run by hand for #404 (see the PR evidence), not yet in drift-check.

## State on 2026-10-27

Read from each repo's `main` with `gh api`:

| repo | `go.mod` requires | `replace` for vws-proto | `vws-proto` paths in tree |
| --- | --- | --- | --- |
| vws-console-api | v0.5.0 | none | 0 |
| vws-storage | v0.5.0 | none | 0 |
| vws-iam | v0.5.0 | none | 0 |
| vws-network | v0.5.0 | none | 0 |
| vws-compute | v0.5.0 | none | 0 |

vws-console, vws-conformance, vws-hypervisor and vws-deploy do not use vws-proto.

vws-proto has tags v0.1.0 to v0.5.0; v0.5.0 is commit 3fbc83c and `https://proxy.golang.org/github.com/popododo0720/vws-proto/@v/v0.5.0.info` returns 200. `main` is 5 commits past v0.5.0 (CI workflows, guard scripts and AGENTS.md only; no `.proto` or `gen/go` change). The GitHub Release for v0.5.0 was published on 2026-10-27: https://github.com/popododo0720/vws-proto/releases/tag/v0.5.0. `GOPROXY=https://proxy.golang.org go list -m github.com/popododo0720/vws-proto@v0.5.0` resolves it (origin hash 3fbc83c).

`scripts/vendor-proto.sh` is still in vws-lib and vws-lib#27 (proto pin check) is still open; both are superseded by the per-repo checks above and are retired in a later PR.

## vws-lib

vws-lib is still vendored as `third_party/vws-lib` with a `replace` (vws-storage, vws-iam, vws-network) through `scripts/vendor-lib.sh`, which is the current team rule. The same design fits vws-lib, but changing that rule is a lead decision outside #404.

## Rollback

- A consumer migration or bump PR is undone with `git revert`, which restores the previous `go.mod`, `go.sum` and, for a migration, the copy and `replace`. No data or deployment state changes.
- A bad vws-proto release: consumers pin the previous tag (`go get github.com/popododo0720/vws-proto@vPREV`) or wait for a fixing patch tag. Never delete or move a tag.
- If proxy.golang.org is unreachable, `GOPROXY=direct go mod download` fetches from GitHub without a token, because the repo is public.

## Rejected

- **Keep vendoring with a pin check (vws-lib#27).** Still one copy per repo and one nested `go.mod` each for `govulncheck`; the check only detects drift that a module `require` cannot have.
- **`go work` or a monorepo.** Couples every service's release to every proto change and needs a checkout of all repos to build one.
- **Consumers run `buf generate`.** Adds buf to every consumer's CI and image build, and two consumers could generate different code from the same tag.
