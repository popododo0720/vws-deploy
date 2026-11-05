# Nightly full-stack e2e on vws-test (vws-deploy#253)

One command, `vws nightly-e2e`, runs the whole nightly on vws-test: a scope check, then
`vws apply -f CR` (install or upgrade the stack the CR pins), then `vws nightly` (every
feature-map scenario), then `vws nightly-pr` (the feature-map PR). It writes a JSON summary and
opens an `incident` issue when a step fails. The command reference is `docs/vws-cli.md`.

## Runner

Chosen: the box starts it. The box already reaches vws-test and already runs `vws nightly` from
a timer at 22:00 KST (vws-deploy#290). The timer and the env file with the vws-test credentials
stay on the box, outside this repo, because R10 forbids units here. A manual run is the same
command typed on the box; it replaces the issue's `workflow_dispatch`.

Rejected:

- **A self-hosted GitHub Actions runner with vws-test reach.** vws-deploy is public. A pull
  request can add a workflow that runs on any runner label, so a stranger's code would run on a
  host that holds vws-test credentials. GitHub's own guidance is to not attach self-hosted
  runners to public repositories.
- **A GitHub-hosted runner.** It cannot reach vws-test.

Cost of the choice: there is no Actions run page per night. The record of a run is the summary
JSON on the box, the nightly PR (it links the run log) and the incident issue on a failure.

## Scope

The nightly makes no OpenStack API call. vws-test is a Kubernetes cluster on OpenStack VMs, and
everything the nightly does goes through the cluster API and the VWS endpoints. To keep it that
way:

- the command removes every `OS_*` variable from its environment before any step, so no step
  inherits OpenStack credentials;
- before any step that writes, it compares the cluster's kube-system UID with
  `vwsTest.kubeSystemUID` in the feature map and stops on any other cluster;
- `tests/vws/nightly-e2e.test.sh` puts a fake `openstack` first on PATH and fails if any step
  calls it.

## Left for later parts

- Building the CR that pins main of every service and the pinned proto tag. The command takes the
  CR file as given.
- The console-api e2e suite as a step.
- Three recorded nightly runs and a forced failure on vws-test (needs the box and the cluster).
