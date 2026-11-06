# Signed feature-map results

vws-deploy#288, part of #262. Before this, the only provenance of a scenario result was that its
`clusterUID` equals `vwsTest.kubeSystemUID` in `docs/feature-map.json`. That UID is committed, so
anyone could write a passing result by hand and `vws verify --update-feature-map` recorded it.

## Chosen: an SSH signature from a key only the vws-test runner holds

- `vws verify scenario --result FILE` signs the result when `VWS_RESULT_SIGNING_KEY` names an SSH
  private key file. The signature is an SSHSIG made by `ssh-keygen -Y sign` with namespace
  `vws-feature-map@vws-deploy`, stored in the result as `"signature"` (the armored text).
- The signed bytes are the result without `"signature"`, as JSON with sorted keys and no
  whitespace (`canonical()` in `scripts/vws.d/lib/feature_map.py`). Signing and verifying use the
  same function, so the bytes cannot drift between bash and Python.
- The trust root is `vwsTest.signers` in `docs/feature-map.json`: a list of
  [allowed_signers](https://man.openbsd.org/ssh-keygen#ALLOWED_SIGNERS) lines for principal
  `vws-test`. `--update-feature-map` runs `ssh-keygen -Y verify -I vws-test -n
  vws-feature-map@vws-deploy` against those lines for every vws-test result. An unsigned result, a
  bad or tampered signature, a key not listed, or an empty list is refused with FAIL (exit 1) and
  the map is not changed.
- Only the public key is in the repo. Anyone can verify; only the holder of the private key can
  sign.

`ssh-keygen` is in openssh-client, which the box and the GitHub runners already have, and Ed25519
needs no extra Python package.

## Rejected

- **HMAC.** The verifier needs the same key as the signer. Every checkout that runs
  `--update-feature-map` (a developer, CI, the nightly PR) would hold a key that can also forge.
- **Sigstore/cosign keyless.** It binds a result to an OIDC identity such as a GitHub Actions
  workflow, which is the better fit once results come from CI. The nightly runs on the vws-test box
  today (vws-deploy#290), with no OIDC identity, and verifying needs cosign, network access to the
  transparency log and a pinned identity regex. It can replace the SSH key later by changing
  `verify_signature()` only.
- **openssl / the Python `cryptography` package with a raw Ed25519 key.** It works, but has no
  principal, namespace or validity window, so key rotation and "wrong signer" would be our own code.

## Trust root and key rotation

The operator procedure is in `docs/vws-cli.md` under `vws verify`. In short: the private key is
created on the vws-test runner and never leaves it, its public line goes into `vwsTest.signers`
through a reviewed PR, and rotation lists the new key next to the old one until the runner signs
with the new key, then removes the old line. A removed key's results are refused from then on.

## Limits

- The signature covers the result fields only. The steps file that `evidence` names is not signed.
- A PR can still change `vwsTest.signers`. Review of `docs/feature-map.json` is the control;
  `--update-feature-map` writes only `lastPass` and `evidence`, so a nightly PR never changes
  `vwsTest`.
- Until a real key is listed (vws-deploy#288 follow-up), `vwsTest.signers` is empty and every
  result is refused. That is the intended fail-closed state.
