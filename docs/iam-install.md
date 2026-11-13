# Installing vws-iam (#243)

Chart installation and CRD packaging are retired. Use [the operator guide](operator/regions.md) for deployment.

The design is vws-iam#1 and the contract is vws-proto#24 (`vws/iam/v1`, branch `feat/iam-v1`).

## Legacy Kubernetes client behavior

vws-iam#168 (merge `caf768e`) adds the kind `IamFidoDevice`. `DeleteUser` lists it on every call, so an image with #168 fails `DeleteUser` on the kube store when the CRD or the grant is missing.

**Client note: FIDO registration changes the user's etag.** `RegisterFido` commits by updating the user (`FidoRegistrations`, vws-iam `internal/mfa/fido.go` `claimUser`), so the user's store version and etag change. A client that sends the old etag on a later user write gets `ABORTED` / `ETAG_MISMATCH` (AWS `ConcurrentModification`) and must read the user again and retry. A `DeleteUser` that races a registration fails the same way, and its retry is `DeleteConflict` while the user has a key. No RPC reaches `RegisterFido` yet (vws-iam#133), so clients see this only once the FIDO RPCs ship.

vws-iam#97 adds the kind `IamInstanceProfile`. Nothing reads or writes it until vws-iam serves the instance profile RPCs (vws-proto#118), so an image that serves them fails those calls on the kube store when the CRD or the grant is missing.
