# Direct AWS access during a VWS outage

Use this procedure when VWS IAM, the global API, its VIP or etcd is unavailable
and an approved incident requires access to AWS resources. Direct AWS access
uses AWS identities, authorization and endpoints. VWS-issued keys, VWS sessions
and the proxy's source identity are not emergency credentials. This procedure
does not restore Proxmox access or VWS availability.

## Prepare before an outage

- Create an emergency role in each target AWS account, trusted by a separately
  managed AWS emergency principal. Give it only the recovery permissions the
  incident plan needs. Require MFA and a short session; keep the source identity,
  MFA device and recovery instructions outside VWS and its hosts.
- Keep an approved operator workstation and a network path to AWS DNS, STS and
  the needed service endpoints that do not depend on the VWS VIP or proxy.
  Keep the AWS CLI installed there. The examples below use the commercial AWS
  partition and `us-east-1`; use the correct partition, region and endpoints
  from the account's incident plan.
- Prepare dedicated AWS files, owned by the operator and mode 0600, at
  `~/.aws/emergency-config` and `~/.aws/emergency-credentials`. The source
  profile is provisioned through the organization's secure credential process.
  Do not copy `/etc/vws/credentials/aws-source` or put credentials in this repo,
  a ticket, shell history, command arguments or captured output.
- Enable CloudTrail management event retention in the target account, with
  alerts on emergency role assumption and use. Enable data events separately
  if the recovery operations require them. Test the procedure periodically
  with the VWS path unavailable and record the AWS account and assumed-role
  identity, without credentials.

The dedicated config contains role metadata only. Replace the example account,
principal MFA ARN and incident session name with approved values:

```ini
[profile vws-break-glass]
role_arn = arn:aws:iam::123456789012:role/VwsBreakGlass
source_profile = emergency-source
mfa_serial = arn:aws:iam::123456789012:mfa/emergency-operator
role_session_name = incident-1234
duration_seconds = 900
region = us-east-1
```

The `emergency-source` profile in the separate credentials file is an AWS
principal permitted to assume this role. Its role trust and identity policy
must both permit the assumption. Do not make emergency access depend on a VWS
identity provider, a VWS credential helper or a VWS-managed credential vault.
If that source is unavailable, follow the organization's independent AWS
account recovery plan.

## Activate and verify

1. Open an incident record outside VWS. Record approval, operator, target
   account, region, reason and start time. Set `role_session_name` to that
   incident's identifier so CloudTrail can correlate the session.
2. Start a clean shell without tracing or session recording of secret input.
   Define this helper to discard inherited VWS endpoint/credential variables,
   proxy settings and custom CA settings. It uses only the dedicated profiles,
   keeps TLS verification enabled, and bounds retries and request timeouts:

   ```sh
   emergency_aws() {
     env -i HOME="$HOME" PATH="$PATH" \
       AWS_CONFIG_FILE="$HOME/.aws/emergency-config" \
       AWS_SHARED_CREDENTIALS_FILE="$HOME/.aws/emergency-credentials" \
       AWS_PROFILE=vws-break-glass AWS_DEFAULT_REGION=us-east-1 \
       AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true AWS_EC2_METADATA_DISABLED=true \
       AWS_RETRY_MODE=standard AWS_MAX_ATTEMPTS=3 AWS_PAGER="" \
       aws --cli-connect-timeout 5 --cli-read-timeout 30 "$@"
   }
   emergency_aws sts get-caller-identity
   ```

   Enter MFA only at the CLI prompt. `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS`
   bypasses configured global and service endpoint overrides. Do not pass
   `--endpoint-url` or use an alias/wrapper that adds it: a command-line endpoint
   takes precedence. Do not use `--debug`, `--no-verify-ssl` or print credentials.
3. Check that `Account` is the approved target and `Arn` is an
   `arn:aws:sts::...:assumed-role/VwsBreakGlass/incident-...` identity. Stop if
   either differs. Never retry with broader credentials after `AccessDenied`;
   ask the incident approver to review the AWS role and account restrictions.
4. Make a read-only check against native AWS, for example:

   ```sh
   emergency_aws ec2 describe-instances --region us-east-1 \
     --query 'Reservations[].Instances[].InstanceId'
   ```

   Continue only with the incident's approved recovery operations. This path
   bypasses VWS policy checks and VWS audit logs; AWS authorization, SCPs and
   CloudTrail still apply. Record actions and AWS request IDs in the incident,
   excluding secrets and session tokens.

## Close the session

When VWS is healthy again, verify its ordinary endpoint with an ordinary
profile separately. Reconcile any direct AWS changes with the resource owner
before resuming automation; do not replay mutations through VWS blindly.
Record the end time and review CloudTrail's `AssumeRole` and subsequent events
with the security owner. Revoke active emergency role sessions when the
incident requires immediate termination; otherwise let the short session
expire. Clear the CLI's temporary role cache (`~/.aws/cli/cache`) under the
organization's credential cleanup procedure, close the shell, and return or
rotate the source credential as required. Removing a local cache does not
revoke a session already issued by AWS.

AWS references: AWS CLI User Guide, "Using endpoints in the AWS CLI"
(endpoint precedence) and "Using an IAM role in the AWS CLI" (roles and MFA);
IAM User Guide, "Revoke IAM role temporary security credentials" (session
revocation).
