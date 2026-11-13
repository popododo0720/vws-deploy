# The `vws` command

The admin CLI for configuration, regions and AWS accounts. The legacy installer entry points and Kubernetes-gated scenario runner remain during the staged cleanup in #775. Chart deployment commands have been retired; use `docs/operator/regions.md` for the supported deployment path.

```
scripts/vws [flags] <command> [args]
make vws-<command> ARGS="<flags>"     # alias of the same command
make vws-test                          # harness (fake kubectl, helm)
```

## Structure

- `scripts/vws`: the dispatcher. It reads flags, loads `scripts/vws.d/<command>.sh` and runs `vws_cmd_main`. Names must match `[a-z][a-z0-9-]*`.
- `scripts/vws.d/<command>.sh`: one command. Near the first line, `# summary: <one line>`; required tools in `VWS_NEED=(kubectl helm)`; the body in `vws_cmd_main`. `help` builds its list from `# summary:`.
- `scripts/vws.d/lib/common.sh`: output, exit codes, tool checks, confirmation (`--yes`), and the kubectl and helm wrappers.
- Commands never call `kubectl` or `helm` directly; they use `vws_kubectl` and `vws_helm`. The wrappers add `--context` and `--namespace`, and under `--dry-run` they do not run mutating calls but print `DRYRUN ...` to stderr. Any verb not in the read verb list counts as mutating.
- Secret values never go into `vws_emit` output. Only names and counts are reported.

## Common flags

| Flag | Meaning |
| --- | --- |
| `--context NAME` | kubeconfig context (kubectl `--context`, helm `--kube-context`) |
| `-n`, `--namespace N` | namespace |
| `-o text\|json` | output format. json prints `{"command","status","checks":[{"level","name","message"}]}` once at the end |
| `--dry-run` | do not run mutating kubectl or helm calls |
| `-y`, `--yes` | confirmation for destructive commands |
| `--strict` | WARN also exits 1 |
| `-h`, `--help` | same as `help` |

Flags may go before or after the command. An unknown flag before the command name is exit code 2; one after the name goes to the command. After `--`, everything is a command argument.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | all OK (WARN is 0 unless `--strict`) |
| 1 | there is a FAIL (or a WARN under `--strict`) |
| 2 | usage error: unknown command or flag, a flag without a value, a destructive command without `--yes` |
| 3 | environment problem: a required tool is missing, the cluster is unreachable |

## Harness (`tests/vws/run.sh`)

It never reaches a cluster or the external network. Admin commands use a loopback HTTP stub started by the test with a readiness signal. Other commands put the fake `kubectl` and `helm` from `tests/fixtures/bin/` first in PATH and test the dispatcher and common library with the test commands in `tests/fixtures/commands/`.

- On every call the fake tools write `tool<TAB>verb<TAB>args` to `$VWS_FAKE_LOG` and return `tests/fixtures/<tool>/<verb>_<resource>.json` as is. A missing file fails loudly (an unplanned call never passes).
- `assert_no_changes` checks the log for mutating verbs. Its verb list is deliberately kept apart from `common.sh` (it does not trust the code under test). `assert_changes` is the control.
- What it tests: exit codes 0, 1, 2 and 3, parsing json output with `jq`, zero mutating calls in read scenarios, passing on `--dry-run`, `--context` and `-n`, destructive commands without `--yes`, `--strict`, and whether the log catches mutating calls.
- It does not test the output format of the real `kubectl` and `helm` or a real cluster. Fixtures for each command are added when its body arrives.

- Totals: each test file prints its own `N cases, M failed` line, and `run.sh` adds up every file and prints `total: C cases, F failed in S suites` as the last line. A file's failure count is the larger of its summary line and its number of `FAIL ` lines. A file that exits without a summary counts as 1; one that exits 0 is named as missing its summary. A run that finds no files fails, so a wrong `VWS_TEST_DIR` cannot pass. It names the failed files and exits 1 when F is greater than 0. Before this, only the last file's summary stayed at the bottom of the screen, and an earlier file's FAIL was hidden behind `0 failed`. `VWS_TEST_DIR` runs another directory (`tests/vws/run.test.sh` uses it).
- Parallel: the files run `VWS_TEST_JOBS` at a time (default twice `nproc`), each in its own process group, and their output is printed in file name order once all have finished. `VWS_TEST_JOBS=1` runs them one after another; unset or empty means the default. Any other value outside 1 to 9999 exits 2 before any file runs. A killed runner (TERM or INT) stops every file it started and any child a file left behind (#322, docs/ci-timing.md).

`scripts/guard.sh vws` (R17) runs the config, region, aws-account and project suites and shellcheck, and checks that each command file has a test file and an entry in this document, and that no command calls kubectl or helm directly. It also tests with broken copies that this check actually fails.

## Commands

### vws install

`vws install --profile lab --inventory hosts.yaml --packages DIR --credentials DIR`
installs one lab host over SSH. Requires python3 with PyYAML, ssh and dpkg-deb
locally, and root access (or passwordless sudo), apt-get, systemctl and keepalived
on the Debian target. Supply exactly one `vws-etcd_*.deb`, `vws-global_*.deb` and
`vws-region_*.deb`; this command does not build packages or download releases.
`small` and `ha` remain vws-deploy#776.

```yaml
profile: lab
hosts:
  - name: lab-1
    address: 192.0.2.1
    ssh_user: root
    roles: [etcd, global, region]
vip:
  address: 192.0.2.10/24
memory_mib:
  global: 1024
  region: 512
  etcd: 512
```

The optional `memory_mib` mapping defaults to the values above. All three
values must be positive whole MiB, totaling at most 2048 MiB; an over-budget
inventory exits 2 before SSH. Installation requires the target's cgroup v2
memory controller. It places the four global services together in
`vws-lab-global.slice`, the region service in `vws-lab-region.slice`, and etcd
with its snapshot/defrag services in `vws-lab-etcd.slice`. Each slice gets
`MemoryAccounting=yes`, its budget as `MemoryMax`, and `MemorySwapMax=0`.
These are aggregate group limits, including service children, so the global
processes cannot each consume the full global budget. Drop-ins live under
`/etc/systemd/system/` as `50-vws-memory.conf`; the installer reloads systemd
before reconciling services, including recovery after an interrupted write.

On the 8 GiB lab box this caps VWS at 2 GiB, leaves 2 GiB for the two 1 GiB
test VMs, and 4 GiB for PVE, the OS and other overhead. These limits are a
budget, not measured working sets or a guarantee that an arbitrary workload
fits. Reaching a limit can cause a cgroup OOM and service restart. Check
`systemctl show vws-lab-global.slice vws-lab-region.slice vws-lab-etcd.slice
-p MemoryCurrent -p MemoryMax -p MemorySwapMax` and journald for OOMs on the
live box after phase 1. Keep VM admission's `host_reserve_mib: 2048` setting
consistent with the host's measured overhead.

The inventory accepts only the documented keys, one IPv4 host with all three roles, and
an IPv4 VIP with an optional prefix (default /32). The VIP uses the packaged
keepalived template's `vmbr0` interface and router id 83. Use a dedicated lab
host and reserve that router id. Unknown, duplicate and secret-looking keys
are refused. `--global-config FILE` defaults to `global.yaml` beside the
inventory and passes the same validation as `vws config validate`.

The credential directory contains the file names in
`packaging/vws-global/README.md`, `packaging/vws-etcd/README.md`, plus
`region-kek` and `region.key` from `packaging/vws-region/vws-region.service`.
`iam-kek/` requires `active` and key-id files. Symlinks and parent traversal in
credential sources are refused. All required inputs are checked before SSH.
Private files go to root-owned 0600 files in `/etc/vws/credentials` (0700),
except `/etc/vws/region.key` (0600). Public certificates go to `/etc/vws` (0644).
Package, credential and config contents travel only on SSH stdin.

Installation starts etcd, then global units, then region, and runs each shipped
readiness helper. Only after every check passes does it enable keepalived.
Changing a file or package disables keepalived first; a failed check disables
it and exits 1 naming the host and check. A rerun compares file bytes, ownership,
permissions and package versions, restarts changed services, and preserves
unchanged files. It also reruns readiness, so retrying after an interruption is
safe. Text prints one `changed` or `unchanged` line per step; `-o json` prints
the same list. Invalid local inputs exit 2 before SSH. Each external call has
a 180-second deadline; SSH connection failures retry twice with 1/2-second
backoff. Remote output is withheld to keep credentials out of diagnostics.
Live PVE verification and working-set measurement remain deferred until after
phase 1. For AWS access during a global-layer outage, use
[the break-glass procedure](operator/break-glass.md).

### vws region

`vws region add NAME`, `edit NAME`, `remove NAME --yes`, `list`, `show NAME`,
`test NAME`, and `health NAME` use the console admin API. Set `VWS_API_URL`
to its HTTPS origin and supply a console bearer token through
`VWS_API_TOKEN_FILE` (preferred) or `VWS_API_TOKEN` in the environment.
HTTP is accepted only on loopback for local testing. Needs curl and python3.

Add requires `--cluster-name`, `--pve-api-url`, `--token-id`,
`--region-endpoint`, and `--token-secret-file PATH|-`. Optional `--ca-pem-file`
reads PEM certificates, and `--zones` takes a JSON array of
`{"letter":"a","nodes":["node1"]}` objects (defaults to `[]` on add).
Edit accepts the same options, all optional; omitted fields stay unchanged.
Test takes only an optional `--token-secret-file PATH|-`; without it the API
skips PVE checks because only the region daemon retains the saved secret.

```bash
export VWS_API_URL=https://console.example.org
export VWS_API_TOKEN_FILE="$HOME/.config/vws/console-token"
scripts/vws region add pve-test-1 --cluster-name lab \
  --pve-api-url https://pve.example.org:8006 --token-id 'root@pam!vws' \
  --region-endpoint https://region.example.org --token-secret-file -
scripts/vws region test pve-test-1
scripts/vws region edit pve-test-1 --zones '[{"letter":"a","nodes":["node1"]}]'
```

Each operation makes one API call, except edit: GET reads the current ETag,
then PATCH sends only supplied fields with `If-Match`. A concurrent change
returns `Conflict` (exit 1); inspect the record before editing again.
Remove sends DELETE without `If-Match`. Writes and tests are never retried.
GET retries transient failures at most twice with bounded backoff; each curl
attempt has a 20-second deadline and the whole call a 65-second deadline.
`--dry-run` prints the method and path without authentication or a request.

Text lists name, cluster name, PVE API URL, region endpoint, token ID,
secret metadata (`set · fingerprint`), and zones. Health prints status, quorum,
leader, online nodes and last error. Test prints STEP/RESULT/MESSAGE and exits
1 on any FAIL, including with `-o json`. JSON output passes the API JSON through
(DELETE 204 has no body). A 403 prints the command's missing `vws:Admin*`
permission and requestId, and exits 3. Other API errors print code, message and
requestId: invalid arguments exit 2, authentication/unavailability exit 3,
and other failures exit 1.

Secrets never go on argv: `--token-secret` and `--external-id`, including
`=value`, are usage errors. File secrets and stdin strip trailing newlines;
token and request body reach curl through a private header file and stdin.
The dispatcher disables xtrace before parsing arguments so rejected secrets
cannot enter trace output. Registration changes go only through the API.

### vws aws-account

`vws aws-account add ID`, `edit ID`, `remove ID --yes`, `list`, and `test ID` use
`/api/v1/admin/aws-accounts` with the same credentials, output modes, timeouts,
edit ETag sequence and exit codes as `vws region`.
ID is the 12-digit AWS account ID. Add requires `--role-arn`,
`--allowed-regions us-east-1,eu-west-1`, and `--external-id-file PATH|-`.
Edit accepts those options and sends only supplied fields.
Text prints the AWS account ID, role ARN, external ID metadata and allowed
regions. Test sends one POST to `/api/v1/admin/aws-accounts/ID/test` with exactly
`{}` and Content-Type `application/json`, using the saved registration.
It accepts no registration or secret fields, prints STEP/RESULT/MESSAGE, and
exits 1 if any step is not PASS, including with `-o json`. A 403 reports
`vws:AdminListAwsAccounts` and requestId with exit 3.

### vws project

`vws project show PROJECT`, `map PROJECT`, and `unmap PROJECT --yes` manage
`/api/v1/admin/projects/PROJECT` using the credentials, output modes and
timeouts of `vws region`. PROJECT and AWS account IDs must be 12 digits.
Map and unmap take either `--region REGION --pool POOL` or
`--aws-account ID`. A region has one pool per project: mapping a new pool in
that region replaces its previous pool. Other regions and AWS accounts stay
unchanged. Unmap removes only the supplied region/pool pair or AWS account;
an absent entry exits 1 without sending PUT.

```bash
scripts/vws project show 123456789012
scripts/vws project map 123456789012 --region pve-test-1 --pool vws-live
scripts/vws project map 123456789012 --aws-account 234567890123
scripts/vws project unmap 123456789012 --aws-account 234567890123 --yes
```

Each command is one admin operation: show sends one GET; map/unmap use the
API's conditional update (GET for the ETag plus one PUT), the same pattern
as region/aws-account edit, because PUT replaces both arrays.
PUT sends both full merged arrays with `If-Match`; conflicts (409 or 412)
exit 1 without retrying or overwriting the concurrent change.
Repeated map calls preserve the same mapping. A missing GET ETag stops the
operation before PUT. A 403 at either step prints the missing
`vws:AdminMapProject` action and requestId and exits 3.

Text output lists the project, pools and AWS accounts. `-o json` passes the
API JSON through. `--dry-run` prints the GET and, for map/unmap, the PUT path
without making requests or requiring credentials. Help discovers `project`
from its command module automatically.

### vws config

`vws config validate FILE|-` checks a global layer bootstrap file
(`/etc/vws/global.yaml`, shipped by `packaging/vws-global`) with the rules
vws-console-api applies at startup (`internal/globalconfig`). It accepts only
the bootstrap keys: `iam.etcd_endpoints` (https://host:port) and `iam.tls`,
`listeners`, `credentials.kek_file` and `credentials.mtls`, and
`default_project`. A region or AWS account entry exits 2 and points at the
admin API (`vws region`, `vws aws-account`). A secret written inline (a
`secret`, `password`, `token`, `kek` or `key` key, a file field that is not a
clean absolute path, or user info in an etcd URL) exits 2, as do unknown or
duplicate keys, more than one document, a missing etcd endpoint, CA or KEK
file, a bad listener, an invalid project name and a file over 64 KiB. An
unreadable file exits 3. Messages name keys and line numbers, never a value
from the file. `-o json` prints `{"valid":true}` or `valid`, `kind`
(`registration`, `inline-secret` or `invalid`) and `error`. It reads only the
file and needs python3 with PyYAML. `tests/vws/config_test.py` uses the case
table of console-api's `TestParseRefusesWhatIsNotBootstrap`.

### vws help

Shows the command list, common flags and exit codes. With `-o json`, one entry per command. Needs no cluster tools.

### vws init

`vws init` starts the legacy installer API TUI inside the installer pod. `vws init --print-url` prints its connection details through `vws-installer print-url`. These entry points remain until #775. CR creation with `--from` or interactive chart questions is retired.

### vws installer

`vws installer finish`: deletes the temporary installation cluster-admin binding (`vws-installer-temp`) and confirms it is really gone. Design and reasons are in `docs/installer.md`. The confirmation uses `kubectl auth can-i` (a SelfSubjectAccessReview; outside the pod, `--as=system:serviceaccount:vws:vws-installer`, i.e. a SubjectAccessReview) and checks that `* *`, `create clusterrolebindings`, `create clusterroles`, `delete namespaces`, `create customresourcedefinitions` and `get secrets` are all `no`, and that the operator rights `get vwsclusters` and `patch vwsclusters/status` are `yes`. Any mismatch exits 1; an unreadable answer exits 3. If the binding is already gone, `unchanged`, and the confirmation runs again (idempotent). Before the delete it reads only the consumed label of Secret `vws-initial-admin` (#208), never its data: `WARN credential` when the initial admin password was never handed out (afterwards only a cluster-admin kubeconfig reads it), `OK` when it was or when there is no Secret (sign-in off). It never blocks the revoke.

### vws backup

`vws backup --etcd [--out FILE]` writes one snapshot of the vws-etcd cluster (IAM and the admin registry; no VM disks or Proxmox data) to `vws-etcd-<UTC>.db` or `FILE` (0600; an existing file is refused, exit 2). etcdctl reads the member and the client certificate from `ETCDCTL_ENDPOINTS` (one https URL), `ETCDCTL_CACERT`, `ETCDCTL_CERT` and `ETCDCTL_KEY` (from `packaging/vws-etcd/vws-etcd-tls`); anything else exits 2. `ETCDCTL_COMMAND_TIMEOUT` bounds the save (default 60s). A failed save or a snapshot that does not verify with `etcdutl snapshot status` is a FAIL and leaves no file. `--etcd` is required; omitting it exits 2 with the supported usage. `tests/vws/etcd.test.sh` runs it against real etcd.

### vws restore

`vws restore --etcd SNAPSHOT --data-dir DIR --name NAME --peer-url URL [--initial-cluster LIST]` writes the data directory of a fresh vws-etcd member from a `vws backup --etcd` snapshot with `etcdutl snapshot restore` (token `vws-etcd`). `LIST` defaults to `NAME=URL`, a cluster of one; for three members run it on each member with the same snapshot and the full list. An existing `DIR`, a non-https peer URL or a missing flag exits 2; a snapshot that does not verify exits 1 and writes nothing. Then start the member on `DIR` with the config from `packaging/vws-etcd/vws-etcd-config`. `tests/vws/etcd.test.sh` restores into a fresh member and reads the data back.

### vws verify

`vws verify --update-feature-map [--map FILE] RESULT.json...` records vws-test scenario results in `docs/feature-map.json` and regenerates `docs/feature-map.md` (feature, verify command, last pass in KST, evidence). A result is `{"scenario", "result": "pass"|"fail", "cluster": "vws-test", "finishedAt": RFC3339, "evidence", "pveServerId", "signature"}`. `pveServerId` is the PVE node serverid read by `live.py identity` and must equal `vwsTest.pveServerId` in the map; a missing or different server ID, or an unset fingerprint, is refused with FAIL (exit 1). The fingerprint stays null until the nested PVE box pve-test-1 exists. Retired Kubernetes result identities are refused (exit 1), and retired map identities are invalid input (exit 2). A matching server ID only catches results from another target or a mislabelled file. A vws-test result must also be signed (vws-deploy#288, `docs/design/result-signing.md`): `signature` is an SSHSIG (`ssh-keygen -Y sign`, namespace `vws-feature-map@vws-deploy`) over the result without `signature`, as JSON with sorted keys and no whitespace, by a key that `vwsTest.signers` in the map lists for principal `vws-test`. An unsigned result, a tampered field, a bad signature, a key not listed or an empty `signers` list is refused with FAIL. Exit codes: 0 recorded or ignored (WARN lines); 1 a result refused (server ID mismatch, unset fingerprint, no trusted signer, missing or bad signature) or not readable as JSON; 2 usage error or invalid input, i.e. `scenario`, `cluster`, `finishedAt` or `result` missing or not a non-empty string, `pveServerId`/`evidence`/`signature` not a string, `vwsTest.signers` not a list of one-line strings, `finishedAt` not RFC3339 with a zone or more than 5 minutes in the future, or `vwsTest.pveServerId` neither a string nor null; exit 2 prints one `FAIL: <reason>` line, no traceback, and leaves the map and md unchanged; 3 the map itself is unreadable. Only a `pass` on `vws-test` moves a last pass forward; a fail, another cluster or an older time changes nothing. Features without a scenario print `WARN unverified`; each such row carries an `na` reason. `vws verify --check-feature-map` exits 1 when the md was edited by hand or is stale, when a name in the `phase1` list of `phase1-features.json` in the map's directory (`docs/phase1-features.json`, the owner-approved Phase 1 feature list, vws-deploy#289) has no row in the map, when a map row is in neither `phase1` nor `later`, when the map names a scenario that has no `scripts/vws.d/lib/scenario-<name>.sh` (`nightly` would fail on it every night), or when a row has neither a `scenario` nor an `na` reason, has both, or has an `na` that is not a one-line string without `|` (vws-deploy#290); it prints one FAIL line per problem. The md shows a row's `na` as `N/A: <na>` in the Verify command column. A missing or malformed list, a `later` entry without a `reason`, or a name in both lists, exits 3. `tests/vws/verify.test.sh` runs the check on the repo map. Local files only; no cluster call (vws-deploy#252). `vws verify scenario NAME` writes the result files this mode reads.

`vws verify list` prints each scenario and its summary. `vws verify scenario NAME [--json] [--fail-at STEP] [--result FILE] [--map FILE]` runs one scenario (vws-deploy#251). The consumer is vws-verify. It runs a scenario on vws-test and cites the step lines, or the `--result` steps file, as live evidence in its `Verified-by` comment. `vws nightly` runs every scenario wired into the feature map the same way.

- A scenario is one file, `scripts/vws.d/lib/scenario-<name>.sh`, with a `# scenario:` summary line, the variables it needs in `VWS_SCENARIO_ENV` and a `vws_scenario_run` function. The runner is `scripts/vws.d/lib/verify_scenario.sh`. The scenarios are `s3-basic` and `ec2-volume-reboot` (vws-deploy#290); eip and iam are follow-ups (vws-deploy#251).
- Before any AWS call, `live.py identity --region "$VWS_VERIFY_REGION"` runs the same checks as `live.py gate`: Proxmox capability, allowlisted cluster name, `features.liveTests: true`, one health node, a matching pinned PVE origin and readable `vws-live` pool. It reads `serverid` from `GET /nodes/{node}/subscription` and requires 1 to 64 ASCII letters or digits, equal to `vwsTest.pveServerId` in the map (`docs/feature-map.json` by default). An unreadable or mismatched identity, failed gate, retired map or unset fingerprint exits 3 with no AWS call. Identity writes no `.gate.json` and needs no `--results`.
- Every step prints one JSON line with `--json` (or `-o json`): `{"scenario", "step", "cmd", "started_at", "duration_ms", "result", "evidence"}`. `result` is `pass`, `fail` or `skip`. `evidence` is the first 300 characters of the step's output, or of its error on a fail, with `VWS_VERIFY_SECRET_ACCESS_KEY` replaced by `<redacted>`. Without `--json` each step is an `OK`, `FAIL` or `WARN` (skip) line. After the first failed step the later steps are `skip`.
- Cleanup. A scenario registers a cleanup step before the step that creates a resource, so a create whose reply was lost is still deleted. The cleanup steps (`cleanup:<name>`) run newest first after the last step, also after a failure and on SIGTERM or SIGINT (exit 143 or 130). On a signal the step that was running, and every process under it, is stopped and recorded as `fail` (`interrupted by SIGTERM`); an interrupted cleanup step runs again. The cleanup after a signal gets `VWS_VERIFY_SIGNAL_GRACE` seconds (default 25, inside the 30 seconds `vws nightly` allows before SIGKILL): each aws call is cut to the time left, and a cleanup step with no time left is a `fail` that names the run ID. The `--result` file then says `fail`. Each accepts an already deleted resource (`already gone`), so a rerun converges. Every name a run creates holds its run ID `vws-verify-<8 hex>`, so a run killed with SIGKILL leaves resources that can be found by that ID.
- `--fail-at STEP` makes that step fail without running it, then tears down as for a real failure. It is the failure-path demo. A `--fail-at` step the scenario never reaches is a FAIL.
- `--result FILE` first deletes a `FILE` left by an earlier run, then writes `{"scenario", "result", "cluster": "vws-test", "finishedAt", "pveServerId", "evidence"}` for `--update-feature-map`. `evidence` is the path of `FILE` with `.json` replaced by `.steps.jsonl`, which holds the step lines. When `VWS_RESULT_SIGNING_KEY` names an SSH private key file, the result is signed with it (`signature`); a key that cannot sign exits 3 and leaves no result. Without the variable the result is unsigned, a line on stderr says so, and `--update-feature-map` refuses it.
- Result signing key (trust root). The key belongs to the vws-test runner only. Create it there with `ssh-keygen -t ed25519 -N '' -C vws-test-results-<YYYYMM> -f <path>` (mode 0600, never copied off the runner or committed), set `VWS_RESULT_SIGNING_KEY=<path>` for `vws nightly`, and add `vws-test <the .pub file's type and key>` to `vwsTest.signers` in `docs/feature-map.json` through a reviewed PR. Rotation: create the new key, add its line next to the old one, switch `VWS_RESULT_SIGNING_KEY`, and after the next recorded nightly remove the old line in a second PR. A leaked key is removed from `signers` at once; from then on its results are refused. A line may carry the allowed_signers options `valid-after` and `valid-before`. `signers` is empty until the runner's key is listed, so every result is refused until then.
- Exit codes: 0 every step and cleanup passed, 1 a step or a cleanup failed, 2 usage (unknown scenario, bad flag, an endpoint that is not http(s) or is an AWS-owned host, the list of `AWS_OWNED` in `tests/compat/compat.py`), 3 environment (`aws`, `jq` or `timeout` missing, a variable in `VWS_SCENARIO_ENV` unset, the cluster unreadable or not vws-test).
- aws calls go through `vws_aws`. It runs aws-cli with only the environment it builds: a throwaway `HOME` and config with S3 path-style addressing, no shared credentials file, IMDS off, standard retries (5 attempts), a 10 s connect and 60 s read timeout and an overall deadline of `VWS_VERIFY_CALL_TIMEOUT` seconds (default 300). The secret key goes in the environment, never in argv, so `cmd` never shows it.
- Variables: `VWS_VERIFY_ENDPOINT` (the VWS S3 endpoint for `s3-basic`), `VWS_VERIFY_EC2_ENDPOINT` (the VWS EC2 endpoint for `ec2-volume-reboot`, passed to aws-cli as `AWS_ENDPOINT_URL_EC2`), `VWS_VERIFY_ACCESS_KEY_ID` and `VWS_VERIFY_SECRET_ACCESS_KEY` (credentials of the vws-test project), `VWS_VERIFY_REGION` (required, for example `pve-test-1`). Each endpoint that is set must be http(s) and not AWS-owned. The identity gate also requires `VWS_API_URL`, `VWS_API_TOKEN_FILE`, `VWS_LIVE_PVE_URL` and the systemd `pve-token` credential under `CREDENTIALS_DIRECTORY`, as documented under `vws nightly-live`. Both origins require HTTPS except on loopback. Set `VWS_VERIFY_PVE_CA_FILE` to pass a trusted PVE CA file to identity as `--pve-ca-file`.
- `s3-basic` creates bucket `<run ID>`, puts `verify.txt`, gets it back and compares the bytes, lists it, then deletes the object and the bucket. It touches no other bucket. A real vws-test run is not recorded yet (vws-deploy#251).
- `ec2-volume-reboot` runs one instance of `VWS_VERIFY_IMAGE_ID` and `VWS_VERIFY_INSTANCE_TYPE` (in `VWS_VERIFY_SUBNET_ID` when set) with the run ID as client token and `Name` tag, waits for `running`, creates a `VWS_VERIFY_VOLUME_SIZE` GiB volume (default 1) in the instance's zone, attaches it as `VWS_VERIFY_VOLUME_DEVICE` (default `/dev/sdf`), reboots the instance, records every state it sees for `VWS_VERIFY_REBOOT_SETTLE` seconds (default 60, step `reboot-settle`), then checks that the instance is `running` and the volume still `attached` to it. Then it detaches and deletes the volume and terminates the instance. Each wait polls describe every `VWS_VERIFY_POLL` seconds (default 5) for at most `VWS_VERIFY_WAIT` seconds (default 600), never past the signal grace. The reboot check is API-level; nothing logs into the guest, so a reboot whose `reboot-settle` evidence shows only `running` is not proven to have happened. The cleanups find the instance and the volume by the `Name` tag, not by a saved ID, so a create whose reply was lost is still removed. The instance cleanup runs first and, after a signal, sends the terminate without waiting, so a slow detach cannot spend the grace before it; the volume cleanup then force-detaches an attached volume and deletes it. It is wired to `EC2 RunInstances / TerminateInstances` and `EBS volume detach / delete` in the feature map. A real vws-test run is not recorded yet.

### vws nightly

`vws [--context CTX] nightly [--map FILE] [--results DIR] [--timeout SECONDS]` runs every scenario named in `docs/feature-map.json`, one at a time in name order, with `scripts/vws verify scenario NAME --result FILE --map MAP` (vws-deploy#287), so the scenario checks the same map's `vwsTest.pveServerId` through the shared PVE gate and requires the verify identity environment above, and records the results with the same update as `vws verify --update-feature-map`. Each scenario runs under `--timeout` (default 1800 seconds; then SIGTERM, and SIGKILL 30 seconds later) and writes `NAME.json` and `NAME.log` under `DIR` (default `$XDG_STATE_HOME/vws-nightly/<UTC time>`). A lock file next to `DIR` (`nightly.lock`) refuses a second run while one is active (exit 3). Exit codes: 0 every scenario passed, or no feature has a scenario (WARN); 1 a scenario failed, timed out, wrote no valid result, or had an invalid name (it is not run); 2 usage error; 3 a missing tool, a held lock, or an unreadable map. The command makes no cluster call itself. The scenarios do. A box timer starts it at 22:00 KST against main HEAD (vws-deploy#290). The timer is not in this repo, because R10 forbids systemd units here.

### vws nightly-live

`vws nightly-live --region NAME [--results DIR] [--snippets-storage ID]
[--pve-ca-file FILE] [--run-id ID] [--fail-at STEP]` runs Proxmox L0 cleanup,
guarded L2 launches and L7 termination. It is a sibling of `vws nightly`, which remains the
feature-map scenario runner called by `vws nightly-e2e`.

Set `VWS_API_URL` to the console HTTPS origin, `VWS_API_TOKEN_FILE` to a
console token file, and the required `VWS_LIVE_PVE_URL` to the PVE origin
pinned independently on the runner. Both origins require HTTPS; HTTP is
allowed only on loopback. The runner reads its
PVE token only from `${CREDENTIALS_DIRECTORY}/pve-token`, supplied by systemd
`LoadCredential=pve-token:/path/on/verification-runner`. Its single-line
content is `vws@pve!live=<secret>`; never put it in argv, environment values,
workflows or the repository. Create that token with privilege separation:
grant its role on `/pool/vws-live` VM.Audit, VM.PowerMgmt, VM.Allocate,
VM.Snapshot and Pool.Audit; on `/storage/<snippets>` grant Datastore.Audit
and Datastore.Allocate; on `/` grant Sys.Audit for cluster resources.
Grant both the user and the separated token the required permissions.

The read-only gate requires capability `kind: proxmox`, `cluster.name:
pve-test`, `features.liveTests: true`, exactly one health node, a valid
`pveApiUrl` matching `VWS_LIVE_PVE_URL`, and an existing `vws-live` pool.
An unset or invalid pin, or a console origin mismatch, exits 3 before any
PVE request. Every PVE request uses the runner's pinned origin.
`live_tests` comes from
`features.liveTests` in popododo0720/vws-console-api#943: until it ships, the
missing field refuses the run. The gate makes read calls; no sweep, step or
PVE write starts before it passes.

Both start and end sweeps remove non-template QEMU VMs in pool `vws-live`
with a semicolon-separated tag matching `vws.live.*`. For each live VM,
read its config UUID, delete only its `vws-<uuid>-user.yaml` snippet, then
stop and delete the VM. A missing snippet counts as already removed.
If interrupted before VM deletion, the next sweep finds that VM and
retries its snippet cleanup. Every other snippet stays, including apparent
orphans and snippets belonging to VMs hidden from the token. The sweeps
also delete every non-current snapshot of remaining pool VMs. Snippets
storage defaults to `local`; `--pve-ca-file` supplies a trusted PVE CA file.
Every HTTP call has a 20-second timeout; GETs retry transient errors with
1- and 2-second backoff. Each asynchronous task has a 120-second deadline
and must finish with exitstatus OK.

Set `VWS_VERIFY_EC2_ENDPOINT`, `VWS_VERIFY_ACCESS_KEY_ID`,
`VWS_VERIFY_SECRET_ACCESS_KEY`, `VWS_VERIFY_IMAGE_ID` and
`VWS_VERIFY_KEY_NAME` (an existing key pair). Install `aws` on the runner.
`VWS_VERIFY_SUBNET_ID` optionally selects the launch subnet.
`VWS_VERIFY_INSTANCE_TYPE` defaults to `vws.small`; every other value fails
before a call. EC2 credentials go only to the aws subprocess environment;
neither API replies nor subprocess stderr are copied to results.

Every RunInstances call, including the same-token retry, passes the shared
`slots --need 1` guard: fewer than two non-template, non-holder pool VMs,
each at most 1 GiB. L2 sends ClientToken, cloud-init user-data, the key pair
and a 128-character tag value, checks DescribeInstances private IP and tags,
and verifies the retry returns the same ID without another PVE VM.
Every launch sends `vws-live-run=<runid>` and verifies `vws.live.<runid>`
on that VM through the pinned PVE API (console-api#997/#1004). The default
run ID is generated once per invocation; an explicit ID must match
`[a-z0-9][a-z0-9-]{0,39}`. Use a fresh ID for each run.

L2 deletes its VM before L7 creates a fresh pair, then L7 terminates both
through EC2 and confirms their absence. Each step cleans up in a finally
block, including errors, timeouts, SIGINT and SIGTERM; the end sweep follows.
`--fail-at slots|l2|l7` injects a failure (live steps fail after creation).
After the end sweep, a separate final assertion requires zero non-template,
non-holder pool VMs, no holder disks, no snapshots on remaining pool VMs,
and no snippets for the VM UUIDs recorded by this invocation. Foreign and
unidentified orphan snippets remain untouched. L3-L6 and L8 are deferred.

Results default to `$XDG_STATE_HOME/vws-nightly-live/<UTC>` (or
`$HOME/.local/state/...`): `l0-start.json`, `slots.json`, `l2.json`, `l7.json`,
`l0-end.json`, `final.json`, and aggregated `results.json`. A failing final
assertion fails the job and aggregate result. Old step results are removed
under the run lock. A sibling `nightly-live.lock` refuses concurrent runs; stale
lock files alone do not hold the lock. Exit codes: 0 all steps and both
sweeps passed; 1 step or sweep failure; 2 usage; 3 environment or refused
gate; 130/143 cancellation after the end sweep.

### vws nightly-pr

`vws nightly-pr --results DIR --log-url URL [--root DIR] [--repo OWNER/NAME] [--remote NAME]` opens the nightly PR after `vws nightly` (vws-deploy#290). It commits only `docs/feature-map.json` and `docs/feature-map.md` from the checkout at `--root` (default this repo), on top of its HEAD, to the branch `nightly/<run>`, where `<run>` is the basename of `DIR` (for example `20261031T130000Z`). It pushes only that branch, never main, and opens one PR into main (`--repo`, default `popododo0720/vws-deploy`) whose body says `Part of #252`, links `URL` (an https link to the run log) and lists every result in `DIR`. The checkout's branch, index and files are not changed. A rerun of the same run reuses the pushed branch when it already holds the same files, and reports an open PR instead of opening a second one. Exit codes: 0 the PR was opened or is already open, or the map is unchanged against HEAD (WARN, no PR); 1 the push or the PR failed; 2 usage error; 3 a missing tool or a `--root` that is not the top of a git checkout.

### vws nightly-publish

`vws nightly-publish --results DIR [--root DIR] [--remote NAME] [--init]` keeps the results JSON of a nightly run off main (vws-deploy#290, design [docs/design/nightly-e2e.md](design/nightly-e2e.md)). It copies every scenario result `DIR/NAME.json` to `metrics/nightly/<run>/` on the orphan branch `nightly-results`, where `<run>` is the basename of `DIR` (for example `20261031T130000Z`), and pushes one fast-forward commit `metrics: nightly results <run>` with `Part of #252` and the trailer `Results-Generator: vws-nightly-publish`. Logs are not published. Each file must be an ASCII JSON object of at most 1 MiB, or nothing is published; a file whose name is not a scenario name is skipped (WARN). The branch is never merged and never opened as a PR, and the checkout at `--root` (default this repo) keeps its branch, index and files. Before it adds a commit it refuses a branch with any commit it did not write: a merge, a commit without the trailer, another subject, or a path outside `metrics/nightly/`. A missing branch is created only with `--init`, once. A rerun of the same run replaces that run's directory and pushes nothing when the files are the same. A push that loses a race, or a remote it cannot reach, is retried on the new head three times in all, each git call under 120 seconds. `--dry-run` is refused. Exit codes: 0 pushed, already there, or no result in `DIR` (WARN); 1 the branch is refused, missing without `--init`, a file is bad, or the push failed; 2 usage error; 3 a missing tool or a `--root` that is not the top of a git checkout.

### vws nightly-e2e

`vws [--context CTX] nightly-e2e --log-url URL [--results DIR] [--timeout SECONDS] [--repo OWNER/NAME] [--wake-thread N]` runs the full-stack nightly on vws-test (vws-deploy#253, design [docs/design/nightly-e2e.md](design/nightly-e2e.md)). It first removes every `OS_*` variable, so no step gets OpenStack credentials. Then it runs four steps in order:

- `scope`: within 60 seconds, `live.py identity` must pass the shared PVE gate and read a server ID equal to `vwsTest.pveServerId` in `docs/feature-map.json`. It requires `VWS_VERIFY_REGION` and the console/PVE credentials described under `vws verify`; `VWS_VERIFY_PVE_CA_FILE` supplies `--pve-ca-file`. A missing identity, failed gate, retired map or mismatch skips every later step.
- `scenarios`: `vws nightly --results DIR --timeout SECONDS` (limit 21600 seconds).
- `feature-map-pr`: `vws nightly-pr --results DIR --log-url URL --repo REPO` (limit 600 seconds). It also runs after failed scenarios, so the passes are still recorded.
- `publish`: `vws nightly-publish --results DIR` (limit 600 seconds) pushes the results JSON to the `nightly-results` branch. It also runs after failed scenarios, so the failures are recorded too.

A failed scope skips the steps after it (WARN). Each step logs to `DIR/e2e/<step>.log`. `DIR/e2e/summary.json` is `{run, cluster, startedAt, finishedAt, logUrl, result, incident, steps: [{name, result, exit, startedAt, finishedAt, log, message}]}`. `DIR` defaults to `$XDG_STATE_HOME/vws-nightly/<UTC time>` and its name must look like `20261031T130000Z`. On a failure it opens one issue labelled `incident` in `REPO` (default `popododo0720/vws-deploy`), titled `incident: nightly e2e <run> failed`, one per run whichever step failed. The body follows the incident form and names the first failing step, then has the step table, the last 60 lines of the failing step's log and, for `scenarios`, the last 30 lines of every scenario log that did not pass. Exported variables whose names look secret (`SECRET`, `TOKEN`, `PASSWORD`, `ACCESS_KEY` and the like) have their values replaced with `[redacted]`, each line of a multi-line value on its own, any piece of 3 or more characters, in the body and the step messages. An open incident with the same title is reused, so a rerun of the same run opens no second one. gh is tried three times with backoff, each call under 60 seconds; if it still fails, the body stays at `DIR/e2e/incident.md` and the run exits 1. Then it wakes an agent: one comment on issue or PR `N` of `popododo0720/vws-deploy`, whatever `REPO` is (`--wake-thread`, else `ORCHESTRATE_WAKE_THREAD` read the way the orchestrator reads it: `$ORCHESTRATE_CONFIG_DIR/env`, default `~/.config/vws-orchestrate/env`, with the environment winning), whose first line is `<!-- vws-orchestrate:lead-wake -->`. It names the run, the failing step and its exit, main's HEAD, the step log path, the run log URL, the incident and, for `scenarios`, each scenario that did not pass with its log path, and ends with `Idempotency key: `sha256:...``, one key per repo and run. Before each post it reads the thread and skips the post when a comment by its own gh login already has that key, so a rerun of the same run posts no second wake; a copy of the key by another login does not count. The comment goes through the same redaction as the incident. No wake thread, or STOP (the `STOP` file in that directory, or `ORCHESTRATE_WAKES=off` or `ORCHESTRATE_DISPATCH=off` in the env file or the environment), posts nothing (WARN). The thread and STOP are read when the run fails, not when it starts, so a STOP set during the scenarios holds. A thread that is not a number is a FAIL. The post is tried three times with backoff; if it still fails, the body stays at `DIR/e2e/wake.md` and the run exits 1. A passing run posts nothing. A lock next to `DIR` (`nightly-e2e.lock`) refuses an overlapping run (exit 3). `--dry-run` is refused, because the steps push a branch and open issues. Exit codes: 0 every step passed, 1 a step failed or the incident or the wake could not be posted, 2 usage, 3 a missing tool or a held lock. The box starts it; the timer and the credential env file stay on the box (R10).
