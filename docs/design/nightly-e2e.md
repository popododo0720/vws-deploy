# Nightly full-stack e2e on vws-test (vws-deploy#253)

One command, `vws nightly-e2e`, runs the whole nightly on vws-test: a PVE scope check, then
`vws nightly` (every feature-map scenario), `vws nightly-conformance` (read-only conformance),
`vws nightly-pr` (the feature-map PR), and
`vws nightly-publish` (the results branch). It writes a JSON summary and
opens an `incident` issue when a step fails. The command reference is `docs/vws-cli.md`.

## Runner

Chosen: the box starts it. The box already reaches vws-test and already runs `vws nightly` from
a timer at 22:00 KST (vws-deploy#290). The timer and the env file with the vws-test credentials
stay on the box, outside this repo, because R10 forbids units here. A manual run is the same
command typed on the box; it replaces the issue's `workflow_dispatch`.

After the nested box exists, the timer's `vws nightly-e2e` invocation also runs the required
conformance step. Set `VWS_CONF_DIR` to the reviewed vws-conformance checkout on the box.
For an operator-triggered conformance-only run, type `vws nightly-conformance` on that same box.
The runner supplies `VWS_CONF_ALLOWED_HOSTS` (the exact nested box host), EC2/IAM endpoints,
region and throwaway account in its environment. Throwaway VWS keys come from that environment
or the systemd `conf-access-key-id` and `conf-secret-access-key` credentials; see
`docs/vws-cli.md`. No workflow or development bot receives these credentials.
Conformance reports and their archive stay under `DIR/conformance` on the box, including on
failure or SIGTERM. Failed scenarios still allow conformance, and a failed conformance step
still allows the feature-map PR and publish steps, so other nightly results are recorded.

Rejected:

- **A self-hosted GitHub Actions runner with vws-test reach.** vws-deploy is public. A pull
  request can add a workflow that runs on any runner label, so a stranger's code would run on a
  host that holds vws-test credentials. GitHub's own guidance is to not attach self-hosted
  runners to public repositories.
- **A GitHub-hosted runner.** It cannot reach vws-test.

Cost of the choice: there is no Actions run page per night. The record of a run is the summary
JSON on the box, the nightly PR (it links the run log) and the incident issue on a failure.

## Scope

The nightly makes no OpenStack API call. vws-test is the nested PVE box pve-test-1, and
the nightly uses the console/PVE APIs and VWS endpoints:

- the command removes every `OS_*` variable from its environment before any step, so no step
  inherits OpenStack credentials;
- before any step that writes, `live.py identity --region "$VWS_VERIFY_REGION"` runs the
  shared live gate and reads `serverid` from `GET /nodes/{node}/subscription`, within 60 seconds.
  It must equal `vwsTest.pveServerId` in the feature map. A missing identity, failed gate,
  retired map or mismatch skips all later steps;
- `tests/vws/nightly-e2e.test.sh` puts a fake `openstack` first on PATH and fails if any step
  calls it.

Conformance forces the `pve-test` target through the reviewed `scripts/pve-test.sh`, which
selects only non-creating cases. Before launching it, the runner refuses AWS-owned endpoint
hosts and hosts outside `VWS_CONF_ALLOWED_HOSTS`; the conformance harness also guards endpoint
dials and the account. Reports have no creating-case flag, so the runner checks their case IDs
against the reviewed 29-case read-only selection, including Describe cases that need no fixtures.
Unknown IDs fail closed. A real nightly, zero creating cases on the box and no traffic to real
AWS remain live checks deferred until after phase 1.

The scope gate requires the console origin and token file (`VWS_API_URL`,
`VWS_API_TOKEN_FILE`), runner-pinned `VWS_LIVE_PVE_URL`, the systemd `pve-token` credential
under `CREDENTIALS_DIRECTORY`, and `VWS_VERIFY_REGION`. Set `VWS_VERIFY_PVE_CA_FILE` for a
private PVE CA. The gate checks the Proxmox capability, allowlisted cluster name, live-tests
flag, single health node, matching PVE origin and readable `vws-live` pool. HTTPS is required
except on loopback. The map's identity stays null until the nested box exists; no real
server ID is assumed. Each scenario repeats the gate before its first AWS call.

## Results branch (vws-deploy#290)

Chosen: the raw scenario results go to their own orphan branch, `nightly-results`, as
`metrics/nightly/<run>/NAME.json`, one fast-forward commit per run, written only by
`vws nightly-publish` (the `publish` step). The feature map still goes to main by the reviewed
nightly PR; the results branch is history, not a source of truth. Like `factory-metrics`, the
branch refuses any commit the command did not write, because all bots share one login. The
`metrics/` prefix and the `metrics:` subject keep each commit inside one scope of
`scripts/scopes` (R2). No workflow runs on it: every `push` trigger filters on `main` or on `v*` tags.

Rejected:

- **The `factory-metrics` branch.** `.github/metrics/metrics.py` refuses a branch with a commit
  it did not write (`metrics_foreign_commit`). Sharing the branch means loosening that check for a
  second writer, which weakens the one control that keeps the metrics data to one script.
- **Results committed to main by the nightly PR.** A file per night on main grows main with data
  nobody reviews, and the AC asks for the results off main.
- **Logs on the branch.** Scenario logs and step output can hold endpoint replies; only the result
  JSON (scenario, result, time, pveServerId, signature) is published.

## Failure wake (vws-deploy#290)

The issue asks that a FAIL posts one comment (scenario, step, log path) on the orchestrator's thread
and wakes an agent, and that a PASS posts nothing.

Chosen: after the incident, the run posts one comment on the verify-wake thread
(`ORCHESTRATE_WAKE_THREAD`, PR #339) whose first line is `<!-- vws-orchestrate:lead-wake -->`. dr
eggbot's `vws-lead-wake-on-339` routine already consumes that marker for "a failure no rule moves on
its own", which is what a failed nightly is, and it trusts only the box login, which is the login
the nightly runs under. The comment carries an `Idempotency key:` line in the orchestrator's form,
sha256 of the repo and the run, so a rerun of the same run finds its own comment and posts nothing;
the orchestrator reads the key as one more of its own keys, and it cannot collide with a lead-wake
key, which hashes `repo#N@head`. The thread and STOP come from the orchestrator's own config
(`~/.config/vws-orchestrate/env` and its `STOP` file, the environment winning as in `load_config`),
read when the run fails, so a STOP set during a six-hour scenarios step still silences the wake. The
comment always goes to vws-deploy, where the thread lives, even when `--repo` points the incident
elsewhere. The incident stays the record; the wake only points at it.

Rejected:

- **A new marker such as `<!-- vws-nightly:fail -->`.** No routine consumes it, and
  docs/factory-orchestrator.md "Wake consumers" forbids a wake without one. Adding a consumer is a
  routine change outside this repo.
- **A comment on the incident or the status issue.** An incident comment wakes nobody, and the
  status issue never carries wakes.
- **A grok-wake.** It means "take this issue" for a Grok owner bot and holds a Grok slot; a nightly
  failure needs triage first.

## Left for later parts

- Building the CR that pins main of every service and the pinned proto tag. The command takes the
  CR file as given.
- The console-api e2e suite as a step.
- Three recorded nightly runs and a forced failure on vws-test (needs the box and the cluster).
