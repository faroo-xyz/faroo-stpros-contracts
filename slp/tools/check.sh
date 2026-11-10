#!/usr/bin/env bash
# CI-ready entry point. Invoke through tools/verify/run.sh in a fresh export.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd -P)
cd "$root"
# Validate isolation before any compiler or Foundry process can start.
python3 -c 'from slp.tools.build import approved_export; approved_export()'
action=${1:-verify}
case "$action" in build|test|verify) ;; *) echo 'usage: check.sh build|test|verify' >&2; exit 2 ;; esac
if [ "$action" != test ]; then
  solc=$(python3 -c 'import sys; sys.path.insert(0,"tools/snapshot"); import build; print(build.find_solc())')
  python3 slp/tools/build.py --solc "$solc"
fi
if [ "$action" != build ]; then
  python3 -m unittest discover -s slp/tests -p 'test_migration.py' -v
  forge test --root slp --offline -vv \
    -R 'forge-std/=test-lib/forge-std/src/' \
    -R '@openzeppelin/contracts/proxy/=test-lib/openzeppelin-contracts/proxy/' \
    -R '@openzeppelin/contracts/=lib/openzeppelin-contracts/' \
    -R '@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/' 
fi
if [ "$action" = verify ]; then
  python3 -m unittest discover -s slp/tests -p 'test_build.py' -v
  suites=()
  for test_file in tools/reconcile/test_*.py; do
    module=${test_file%.py}
    suites+=("${module//\//.}")
  done
  python3 -m unittest "${suites[@]}" -v
  python3 tools/ci/canonical.py
  python3 -m unittest discover -s tests/toolchain -p 'test_*.py' -v
fi
