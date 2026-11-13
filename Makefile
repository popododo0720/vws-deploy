SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
export PATH := /tmp/bin:$(PATH)

include scripts/versions.env

.PHONY: check hooks guard vws-test

check:
	$(MAKE) hooks
	$(MAKE) guard

hooks:
	git config core.hooksPath .githooks

guard:
	scripts/guard.sh all

# make vws-<command> is an alias for scripts/vws <command>. Flags go through ARGS, for example
# make vws-help ARGS="-o json". vws-test runs the harness with fake kubectl and helm.
vws-test:
	tests/vws/run.sh

vws-%:
	scripts/vws $(ARGS) $*
