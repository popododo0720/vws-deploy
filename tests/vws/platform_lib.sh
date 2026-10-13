# shellcheck shell=bash
# platform.py on a CR built from examples/vwscluster/ha.yaml and tests/fixtures/render/node-network.yaml.
# Sourced by render.test.sh.
# shellcheck disable=SC2034 # read by render.test.sh
PLATFORM_BUNDLE="${VWS_TEST_ROOT}/tests/fixtures/platform/bundle"
platform_cr() { # PYTHON_EDIT -> ${WORK}/pcr.yaml
  python3 - "${VWS_TEST_ROOT}/examples/vwscluster/ha.yaml" "${VWS_TEST_ROOT}/tests/fixtures/render/node-network.yaml" "$1" >"${WORK}/pcr.yaml" <<'PY'
import sys, yaml
cr = yaml.safe_load(open(sys.argv[1]))
cr["spec"]["network"] = yaml.safe_load(open(sys.argv[2]))
exec(sys.argv[3], {"n": cr["spec"]["network"]})
yaml.safe_dump(cr, sys.stdout)
PY
}
run_platform() { # BUNDLE -> rc in PRC, output dir ${WORK}/pout
  rm -rf "${WORK}/pout"
  PRC=0
  python3 "${VWS_TEST_ROOT}/scripts/vws.d/lib/platform.py" "${WORK}/pcr.yaml" "$1" "${WORK}/pout" 2>"${WORK}/perr" || PRC=$?
}
