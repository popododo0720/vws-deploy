# CI time across the VWS repos (vws-deploy#322)

The nightly audit of 2026-10-17 found main CI medians up 33% to 119%. This record names the cause
per repo, from the step timings of successful push runs on main, read on 2026-11-06 with
`gh api repos/popododo0720/<repo>/actions/runs/<id>/jobs` and the job logs. The first and last
columns are one run each, named by head SHA.

| Repo | Job | Early run | Late run | Where the time went |
| --- | --- | --- | --- | --- |
| vws-deploy | check | 7s (178d757) | 291s (96e2fb3) | `scripts/guard.sh vws`, the CLI harness run one suite at a time |
| vws-network | test | 26s (71278f4) | 289s (26656bf) | envtest step 178s (test time 168.7s in `internal/network`), `go test ./...` 100s of compile |
| vws-network | lint | new | 21s to 117s | golangci-lint with no analysis cache |
| vws-iam | test | 146s (8ff88ab) | 460s (7ecb010) | `go test -race`: `internal/grpcapi` alone 309.5s |
| vws-iam | lint | 120s (8ff88ab) | 95s (7ecb010) | golangci-lint with no analysis cache, 76s to 98s per run |
| vws-console | quality | 59s (4dbf6dc) | 116s (6b167de) | new steps: unit tests 32s, Playwright install 23s, two e2e runs |

## vws-deploy

Cause: test growth run serially. In run 42212058830 (8b77f45, 2026-11-06) the Check
step took 300s and `scripts/guard.sh all` 288s of it. Timed locally per subcommand,
`guard.sh vws` took 180s, `agents` 27s and `placeholders` 10s; everything else was under 6s.
`guard.sh vws` runs `tests/vws/run.sh`: 25 suites, 1487 cases at 8b77f45. The slow suites wait on
fake-cluster polls rather than compute: `verify-reboot.test.sh` takes 37s of wall time and 6s
of CPU.

Fix (this change): `tests/vws/run.sh` runs the suites in parallel, twice `nproc` at a time, and
prints their output in name order. Measured on this box pinned to 4 CPUs (`taskset -c 0-3`, the
size of a public `ubuntu-latest` runner): the harness went from 178s with one job to 71s with 4
and 56s with 8 (the new default). On the same tree, the serial and parallel runs report the same
cases and the same output apart from one printed duration. On all 8 CPUs, `guard.sh vws` went from 180s to 52s.

What stays above the 7s baseline is justified: 178d757 checked a chart with helm lint and
kubeconform only. Since then the check has gained the CRD envtest (6s), the platform bundle
render (4s), the AGENTS.md guards (27s) and the harness. The installer job (23s to 66s) is new
since 2026-10-13 and is `go test -race` over a module that grew; it runs beside check, so it
does not set the wall time.

## vws-network

Two causes. First, test growth: the envtest step, added after 71278f4, spends 168.7s in the
`internal/network` envtest tests themselves, not in setup. Second, `go test ./...` grew from 6s
(9a5f3cc) to 100s (26656bf), while every package's test time is under 1s, so the time is compile.
Why the build cache did not help there is unverified. The lint job (vws-deploy#300) uses
`golangci/golangci-lint-action` with `install-only: true`, which skips the action's cache, so a
run analyses from cold.

Left for vws-network: find the waits in the envtest tests, run envtest as its own job beside
`test`, and cache `~/.cache/golangci-lint`.

## vws-iam

Cause: test growth in one package. Of the 420s `go test -race ./...` step, `internal/grpcapi`
took 309.5s, `internal/store/kubestore` 28s and `cmd/vws-iam` 19s; every other package about 1s.
The new `govulncheck` step adds 27s. Lint has the same cold cache as vws-network.

Left for vws-iam: profile `internal/grpcapi` under `-race` (sleeps, real timeouts, missing
`t.Parallel`), and cache `~/.cache/golangci-lint`.

## vws-console

Cause: new coverage. The quality job gained unit tests (32s), a Playwright browser install
(23s) and two e2e runs. That roughly doubles the 59s job and is justified. Outliers are the
Playwright install: `npx playwright install --with-deps chromium` took 570s in b750441's run
(704s job), from the apt step of `--with-deps`.

Left for vws-console: cache `~/.cache/ms-playwright` keyed by the Playwright version, so the
install only fetches the system packages.
