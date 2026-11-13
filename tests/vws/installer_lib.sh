#!/usr/bin/env bash
# Scenario setup for installer and backup tests. Source after lib.sh.
# installer_scenario NAME: a fresh copy of tests/fixtures/installer in $WORK/NAME.
installer_scenario() {
  rm -rf "${WORK:?}/$1"
  cp -r "${FIXTURES}/installer" "${WORK}/$1"
  export VWS_FAKE_DIR="${WORK}/$1"
}

calls() { cat "$VWS_FAKE_LOG"; }
