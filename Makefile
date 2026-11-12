SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
export PATH := /tmp/bin:$(PATH)

include scripts/versions.env

CHART := charts/vws
RELEASE := vws
NAMESPACE := vws
export VWS_RELEASE := $(RELEASE)

.PHONY: check tools hooks lint render crd guard vws-test

check:
	$(MAKE) hooks
	$(MAKE) guard

tools:
	@command -v helm >/dev/null || { echo "helm missing; run scripts/install-tools.sh" >&2; exit 1; }

hooks:
	git config core.hooksPath .githooks

lint:
	helm lint $(CHART) --strict -f $(CHART)/ci/site.yaml

render:
	mkdir -p build
	helm template $(RELEASE) $(CHART) -f $(CHART)/ci/site.yaml --namespace $(NAMESPACE) > build/vws.yaml


# CRDs against a real kube-apiserver (envtest, ENVTEST_VERSION): apiserver-only rules like uniqueItems (#210).
crd:
	scripts/crd-check.sh


guard:
	scripts/guard.sh all

# make vws-<command> is an alias for scripts/vws <command>. Flags go through ARGS, for example
# make vws-help ARGS="-o json". vws-test runs the harness with fake kubectl and helm.
vws-test:
	tests/vws/run.sh

vws-%:
	scripts/vws $(ARGS) $*
