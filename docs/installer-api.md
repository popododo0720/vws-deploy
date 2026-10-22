# Installer API server (vws-deploy#141, design #139 (b))

`installer/` is the Go module `github.com/popododo0720/vws-deploy/installer`. The `vws-installer` binary serves #139's installer API over TLS (self-signed). Logs, metrics and traces use vws-lib `obs` (`obs.Setup`, `httpobs.Middleware`), and errors use vws-lib `errs`. There is no `go.mod` at the root, so R10 (the chart is the only manifest path) still holds.

## Running

```
vws-installer -api-version GROUP/VERSION [-listen 0.0.0.0:8443] [-state-dir /tmp/vws-installer] [-kubectl kubectl] [-vws vws]
```

- `-api-version` is required and has no default. The group is written only in `charts/vws/crds/vwsclusters.yaml` (R18). The bootstrap Deployment passes the group/version that `scripts/bootstrap.sh` reads from the CRD as `-api-version=` (#191). R20 checks the whole command line and the probes.
- In `-state-dir` (0700) it writes the one-time access code `access-code` and the certificate's SHA-256 fingerprint `fingerprint` (both 0600). Neither is logged. `vws init --print-url` (#142) reads these files.
- Installing is `vws apply --unattended -f <state-dir>/vwscluster.json`. That command stores the CR (`vws init --from`) and applies it (#144).

## Endpoints (base `/api/v1`, JSON)

| Method | Path | This PR |
| --- | --- | --- |
| POST | /session | implemented. No authentication |
| GET | /state | implemented |
| GET, PUT | /steps/{step} | implemented |
| POST | /steps/{step}/preflight | implemented (re-checks the stored value) |
| GET | /summary | implemented. `editing`/`failed` → `confirming` |
| POST | /install | implemented. 202, the install runs in the background |
| GET | /result | implemented. `credentialAvailable` is true when succeeded and a credential store exists |
| GET | /install/events | implemented (#145). Install progress as SSE, resumable with `Last-Event-ID` |
| POST | /result/credential | implemented (#146). Returns the initial admin password once; after that 410 `credential_consumed` |
| GET | /cr | implemented (#144). The CR file (`vwscluster.json`) when every step is valid |
| POST | /cr | implemented (#144). Fills and checks every step from a CR file |

Every path except `/session` needs a session (`Authorization: Bearer <token>` or the cookie `vws_installer_session`, HttpOnly, Secure, SameSite=Strict). Bodies are limited to 1 MiB, and unknown fields or data after the JSON are 400 `bad_request`. A known path with an unsupported method is 405 `method_not_allowed`, and the `Allow` header names the methods it takes.

## Authentication (one-time access code)

- The code is 16 characters drawn with `crypto/rand`. Confusable characters (0 O 1 l I) are left out, and rejection sampling keeps the draw uniform.
- When `POST /session {"code"}` matches, it returns a session token (32 bytes hex), discards the used code and writes the next code to the file. A new login ends the previous session, so there is always one session.
- A wrong code is 401 `unauthenticated`. The 5th in a row is 403 `forbidden` and issues a new code (the old code stops working).
- After 30 minutes without requests, the next request is 401 `session_expired` and the token is discarded. Every request restarts the idle timer.

## State machine

There is one session (a singleton). `editing → confirming → installing → succeeded | failed`. Saving a step in `confirming` or `failed` returns to `editing`. In `succeeded`, once `POST /result/credential` has returned the password, the state is `finished` (#146).

| Request | Refusal |
| --- | --- |
| PUT a step while an earlier step is not valid | 409 `step_out_of_order` |
| GET /summary while a step is not valid | 409 `step_out_of_order` |
| POST /install without `confirm` true | 400 `bad_request` |
| POST /install without reading the summary, or after editing since | 409 `not_confirmed` |
| POST /install with a `specHash` that differs from the current draft | 409 `spec_changed` |
| Editing, summary or install during an install | 409 `install_in_progress` |
| Editing or install after `succeeded` | 409 `already_installed` |

`specHash` is the SHA-256 of the canonical form: the CR JSON read back as a map with sorted keys. The installed CR is byte-identical to the confirmed one. Saving a step re-checks the steps saved after it; for example, changing network's external CIDR makes eip `invalid`.

## Steps and the CR

| Step | Value | CR |
| --- | --- | --- |
| roles | `{"nodes": [...]}`, all nodes in the cluster | not included. Roles are read from node labels applied while preparing the cluster (the installer only reads them, with nodes get/list from `vws-operator-read`). The CRD has no `spec.nodes` (#192, option B) |
| network | `spec.network` as is (#125). driver is kube-ovn only. CIDRs, VLAN 0-4094, MTU 1280-9216, external and direct gateways inside their CIDR | `spec.network` |
| storage | `spec.storage`. rook-ceph, existing (storageClass required), nfs (server and share required). minio and local are disabled choices labelled "Coming soon" (vws-storage#70, #71) | `spec.storage` |
| eip | a CIDR string. No default, required. Must be inside network's external | `spec.network.nodes.external.eipPool` |
| profile | single-node, small, ha. Default is single-node with one node, else small | `spec.profile` |
| observability | `{"otlpEndpoint": ""}`. vws render does not take an endpoint yet, so it must be empty | `spec.observability` |

When validation fails, the value is stored as `invalid` and the response is 400 `validation_failed` with `fields`. `fail`+`error` makes the step `invalid`. Other non-`pass` checks (`warning`, `unknown`) do not block and go to the summary's `warnings`.

## Preflight engine (#143)

`internal/preflight` builds checks from the step values. It reads only the Kubernetes API and an optional links file (R10). It ports #128's node network checks (`scripts/vws.d/lib/nodenet.py`) with the same rules. nodenet.py in `vws preflight` stays for the CLI outside the pod.

| Step | id | Severity | Content |
| --- | --- | --- | --- |
| roles | `node.ready` | error | each chosen node is Ready |
| network | `node.ip` | error | each node has one InternalIP inside the management CIDR |
| network | `nic.exists`, `nic.mtu` | error | each node has the interface and its MTU is at least the CR value (1500 when unset). Host networks are `<nic>` or `<nic>.<vlan>`; external and direct (underlay) use the NIC as is. When several networks share an interface, the larger MTU |
| network | `nic.links` | warning, unknown | no links for that node. Does not block |
| storage | `storage.class` | error (warning, unknown when the list cannot be read) | the existing StorageClass exists |
| storage | `storage.nfs` | info, unknown | the NFS server is not contacted |
| profile | `profile.nodes` | error | Ready node count ≥ 1 for single-node, 3 for small, 6 for ha (`VWS_PROFILE_MIN_NODES`) |

NICs and MTUs are read from `vws-installer -links FILE` (`talosctl -n <all nodes> get links -o json`, the same input as nodenet.py). A links entry's `node` is a node name or InternalIP. When links does not know a node, `nic.links` is unknown and the network option `options.nics` is empty for that node. The bootstrap Deployment does not pass `-links` yet (how to provide the file needs checking).

## Storing the CR and unattended installs (#144)

- `GET /cr` returns the same file as the CR the summary would install (every step valid, else 409 `step_out_of_order`). `vws apply --unattended -f vwscluster.json` does the same install without the wizard.
- `POST /cr` splits a CR body into steps. `spec.network.nodes.external.eipPool` goes to the eip step, and the other spec fields go to the steps of the same name. roles are not in the CR, so the stored value is kept if valid, else all nodes. A missing observability is cleared. Every step is re-checked. A body with an invalid field is refused before the node lookup and changes no step; the response is 400 `validation_failed` with every field. It works only while editing is allowed (409 `install_in_progress` during an install, 409 `already_installed` after).
- `spec.version`, `spec.services` and `spec.secrets`, which the draft does not have, are refused as `unsupported`. Such a CR goes through `vws apply --unattended -f` without the installer. `apiVersion`, `kind: VWSCluster` and `metadata.name: vws` are checked too.
- Uploading the same CR twice gives the same steps and the same `specHash`. Running `vws apply --unattended -f` twice makes the second run `unchanged` for both init and apply.

## Install event stream (#145)

- `GET /install/events` is `text/event-stream`. Events are `state` (`installing`, `succeeded`, `failed`) and `log` (one line of install log). Ids start at 1, increase, and are never reused, even across reinstalls. It starts with `retry: 5000` and sends `: keepalive` every 15 seconds while idle.
- It sends from after the `Last-Event-ID` header (`?lastEventId=` for clients that cannot set headers). The server keeps only the last 2000 (`EventBuffer`). If the events after that id were already dropped, it sends `event: gap` (the number dropped) and then starts from the oldest one left. An invalid id is 400 `bad_request`.
- If no install is running and nothing is left to send, the stream ends. EventSource reconnects after the retry and continues.
- Log lines are masked before they enter the ring and are never sent to the pod log. Masked: the current access code and session token (literal values), the values of `key=value`, `key: value` and `"key": "value"` whose names match `password|secret|token|api_key|access_code|credential|private_key…`, the value after `Bearer`, the password in a URL's `user:password@`, and whole PEM private key blocks. All become `***`. Secrets that do not fit these shapes are not filtered (for example, a log that prints a value without a name). So the first rule is that `vws apply` never logs secrets.

## Initial admin password (#146)

- When the install succeeds, the installer draws 24 characters with `crypto/rand` (upper- and lowercase letters and digits, unbiased) and writes them only to the Secret `vws-initial-admin` (`username`, `password`) in the `vws` namespace (`-namespace`). It passes them to kubectl through stdin and never puts them in arguments, errors, logs, the CR, status or state-dir files. It does not keep them in installer memory either.
- An existing Secret is not changed (a reinstall keeps the same password). If the Secret cannot be created, the state is `failed` and the install is repeated.
- `POST /result/credential` reads the Secret and returns `{"adminUser","password"}` with `Cache-Control: no-store` only after labelling the Secret `vws-initial-admin-consumed=true`. If the label cannot be applied it returns 500 and no password (calling again is fine). After that, and when the label already exists (including after an installer restart), it is 410 `credential_consumed` and the state is `finished`. Before succeeded it is 409.
- Do this step before `vws installer finish`. Once finish removes the temporary rights, the installer cannot read the Secret (needs checking). Whether the console creates the admin account from this Secret is outside this repository (needs checking). The TUI (#142) does not call this endpoint yet.

## Error model

```json
{"error":{"code":"validation_failed","message":"...","fields":[{"field":"spec.network.nodes.external.eipPool","code":"required","message":"..."}],"retryable":false}}
```

Each code has one HTTP status and one vws-lib `errs.Code`. `retryable` is `errs.Code.Retryable()`. Added to the #139 table: 404 `not_found` (unknown path or step), 405 `method_not_allowed` (known path, unsupported method), 409 `not_confirmed`, 501 `not_implemented`, 500 `internal`. The cause of an internal error goes only to the log; the response says only "internal error". The cause of an install failure is not put in the response either.

## TUI `vws init` (#142)

Inside the installer pod, `vws init` (no arguments) runs `vws-installer tui`. It logs in with the access code from the state directory and pins the certificate to the SHA-256 in `fingerprint` (no CA; any other certificate is refused). It asks in step order.

- roles: the node list (all by default). network: shows the NIC list (per node) and asks for the NIC, CIDR, VLAN and MTU of management (required) and tunnel, storage public and external (optional, `skip`), plus the gateway for external. storage: shows disabled drivers as "Coming soon" and asks only about the selectable ones. It asks for eip and profile and leaves observability empty.
- On `validation_failed` it shows the field errors and asks that step again. It shows non-`pass` preflight checks and asks again when blocked.
- It shows the summary's CR and warnings and installs only on `yes`. Until the progress stream (#145) it reads `/result` every 2 seconds. When done it shows the console URL and the admin name. It says the initial password is not available until #146.
- On `no` or end of input it installs nothing and exits (the draft stays in the installer).

`vws init --print-url` is `vws-installer print-url`. It prints the port-forward address, the access code and the certificate fingerprint only to the exec terminal. The code is one-time, so a fresh code must be read for every login.

## Tests (fake cluster, T0)

`cd installer && go vet ./... && go test -race ./...`. CI runs them in the `installer` job. Coverage:

- Authentication: code shape, no repeats, one-time use, a new login ends the old session, 403 and a new code after 5 failures, 30-minute idle expiry and renewal, cookie attributes, 401 on every path without a session, no code in response bodies.
- State machine: start to finish, order violations, `spec_changed`, `not_confirmed`, 409 during an install, 409 after an install, retry after a failure.
- 16 step validation cases, disabled drivers, eip re-check after a network change, preflight blocking and warnings, bad bodies, 404, 405 with `Allow`, 501, the CR's apiVersion equals the chart CRD's.
- TUI (`internal/tui`): with the real API over TLS, from the start to install, asking again after field errors, preflight display, refusal and end of input, install failure, refusing another certificate, the client error model. `print-url` output. `tests/vws/init.test.sh`: `vws init` in the pod runs the TUI, `--print-url` runs print-url, and `--from` stores as before.
- CR (`cr_test.go`): export needs every step; uploading an exported CR to a new installer gives the same summary hash; uploading twice gives the same; refusal of apiVersion/kind/name/services, a missing eipPool, a missing profile and disabled drivers; a bad CR is 400 even when the node lookup fails; 409 during and after an install. `tests/vws/apply.test.sh`: `apply -f` applies after init, the second run is unchanged with 0 changes, a bad file is 2 with no helm calls.
- Events (`events_test.go`, `mask_test.go`): a client attached during the install and one that reads after it finished see the same thing; fake secrets, access codes and tokens appear nowhere in the raw stream; `Last-Event-ID` resume; the 2000 ring and gap; 400 on a bad id; an empty stream before the install. A table per masking shape.
- Credentials (`credential_test.go`): 410 and `finished` after one return; shape (24 characters, alphabet); the value is in no response other than the return, nor in the CR or code files; a Secret creation failure is failed; a label failure is 500 without the value; an already used Secret is 410 and unchanged. `cluster.KubeSecret`: create with a fake kubectl, AlreadyExists, read, label, the value is not in kubectl arguments. `credential_leak_test.go`: after a label failure (500, ERROR log) and a normal return, the generated password appears nowhere in the raw `GET /install/events` stream or the slog logs (down to debug level).
- `cluster.VWS`: with fake `kubectl` and `vws` scripts, the node list, a single `apply --unattended -f`, passing on an apply failure, CR file mode 0600.

## Remaining work (outside this PR)

- Image: the build stage of `bootstrap/Containerfile` (`INSTALLER_GO_IMAGE`, which must match the Go version in `installer/go.mod`, R20) builds with `CGO_ENABLED=0` into `/opt/vws/bin/vws-installer` (#191). Publishing and pinning the digest are not done yet (the `VWS_INSTALLER_IMAGE` digest is zeros).
- TLS, port-forward and the install path on a real cluster (not run). NIC checks with a real links file (not run).
