SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
export PATH := /tmp/bin:$(PATH)

include scripts/versions.env

CHART := charts/vws
RELEASE := vws
NAMESPACE := vws
export VWS_RELEASE := $(RELEASE)

.PHONY: check tools hooks lint render conform platform guard

check:
	$(MAKE) tools
	$(MAKE) hooks
	$(MAKE) lint
	$(MAKE) conform
	$(MAKE) platform
	$(MAKE) guard

tools:
	@command -v helm >/dev/null || { echo "helm missing; run scripts/install-tools.sh" >&2; exit 1; }
	@command -v kubeconform >/dev/null || { echo "kubeconform missing; run scripts/install-tools.sh" >&2; exit 1; }

hooks:
	git config core.hooksPath .githooks

lint:
	helm lint $(CHART) --strict

render:
	mkdir -p build
	helm template $(RELEASE) $(CHART) --namespace $(NAMESPACE) > build/vws.yaml

conform: render
	mkdir -p .cache/kubeconform
	kubeconform -strict -summary -kubernetes-version $(KUBERNETES_VERSION) -cache .cache/kubeconform build/vws.yaml

platform:
	scripts/platform.sh

guard:
	scripts/guard.sh all
