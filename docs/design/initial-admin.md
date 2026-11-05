# Initial admin account from Secret vws-initial-admin (#208)

## Problem

#146 put the initial admin password in Secret `vws-initial-admin` (`username`, `password`) in the release namespace. The installer hands it out once through `POST /result/credential`. Before this change nothing read that Secret.

- The installer created it after the install. `cluster.Install` returned an empty `AdminUser`, so the Secret's `username` was empty.
- Console sign-in is served by vws-console-api. It takes the account root's console password only from `-root-password-file` and reads that file once at start (`cmd/vws-console-api/main.go` `setRootPassword`). The user name is the constant `root` (`internal/iam/session.go` `RootUserName`). It serves sign-in only with `-iam fake` or `-iam grpc` (`signInEnabled`). The pinned image `sha-c09cba3` has the flag.
- The chart passed no `-root-password-file`, and its default is `-iam static`, where sign-in is off. So a default install handed out a password that logged in nowhere.

## Decision

`vws apply` creates the Secret, the chart mounts it into console-api, and the installer only reads it.

- **Data.** Secret `vws-initial-admin` holds `username: root` and a 24-character `password` (letters and digits, Python `secrets`). There is one creator. `scripts/vws.d/lib/apisecret.py` kind `admin` creates it in `vws_apply_secrets`, before helm, together with the console-api and KEK Secrets. An existing Secret is kept (AlreadyExists), so a rerun never rotates it.
- **When.** Only when console-api serves sign-in. `services.console-api.rootPasswordSecret` names the Secret. The chart and `apisecret.py names` act on it only when `flags.iam` is `fake` or `grpc`. With `-iam static` nothing renders and no Secret is made, and the default render is byte-identical to before.
- **Chart.** `-root-password-file=/etc/vws/root-password/password` is derived (setting it in `flags` fails the render, R14). The Secret is mounted read-only with only key `password` and mode `0440`. `scripts/check_render.py` checks the mount, the key, the mode, that no `*PASSWORD*` env is present, and that nothing is mounted while sign-in is off (R16).
- **Installer.** It no longer generates a password. After a successful install it reads the Secret. If the Secret is there, `GET /result` reports its `username` as `adminUser` and `credentialAvailable: true`. NotFound means sign-in is off: `credentialAvailable` is false, and `POST /result/credential` answers 404 `not_found`. Any other read error fails the install, so a retry reads it again.
- **After the first login.** The Secret stays. It is console-api's source of the root password, so deleting it would stop console-api from starting again (the mount is not optional). console-api has no root password change, so a forced change on first login is not possible here and belongs in vws-console-api. To rotate, write a new `password` into the Secret with operator credentials and restart console-api (`kubectl -n vws rollout restart deployment/console-api`).
- **`vws installer finish`.** finish already proves that the installer can no longer `get secrets`, and the TTL reaper revokes the binding even without finish. So after finish the installer API cannot hand the password out. The Secret itself stays, readable with a cluster-admin kubeconfig. finish does not block on it, because blocking would keep cluster-admin alive longer, and the reaper would revoke it at the TTL anyway. Before the revoke, finish reads only the Secret's consumed label and prints `WARN credential` when the password was never handed out.

## Rejected

- **Installer creates the Secret before `vws apply`.** This keeps Go generation, but the installer would have to work out from the CR whether sign-in is on, duplicating chart logic in Go. A direct `vws apply` with sign-in on would also need a hand-made Secret. `vws apply` already creates every other Secret the chart reads by name.
- **Installer creates it after the install, and the chart mounts it `optional: true`.** console-api reads the file only at start, so the pod would start without root sign-in and keep it off until a restart.
- **A helm hook Job that creates the Secret.** The chart renders no Secret (R16), and the Job would need `create secrets`.
- **Turn sign-in on by default** (`services.iam.enabled: true`, `-iam grpc`). Out of scope here. The default render still leaves vws-iam out (`scripts/guard.sh config`), and changing the default is its own decision.
- **Pass the password by env (`secretKeyRef`).** console-api has no env form of the root password, only the file flag.
