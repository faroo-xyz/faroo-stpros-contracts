# All execution stays in the allowlisted export and empty-environment runner.
# Native build requires only Python, reviewed native solc and vendored sources.
.NOTPARALLEL:
.PHONY: build toolchain-test upgrade-test upgrade-rehearsal
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := build
build:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 tools/ci/canonical.py; echo "Artifacts: $$dest/cache/native-r4"
toolchain-test:
	@dest=$$(tools/verify/export.sh | tail -1); tools/verify/run.sh "$$dest" python3 -m unittest discover -s tests/toolchain -p 'test_*.py' -v
upgrade-test:
	@dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" python3 tests/release/native/gate.py; \
	tools/verify/run.sh "$$dest" python3 -m unittest discover -s tests/release/native -p 'test_*.py' -v; \
	tools/verify/run.sh "$$dest" FOUNDRY_PROFILE=final forge test --offline --match-contract '^(FoundationWitnessTest|NativeReleaseWitnessTest|LossContingencyTest)$$' -vv
upgrade-rehearsal:
	@dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" python3 tools/ci/canonical.py; \
	tools/verify/run.sh "$$dest" FOUNDRY_PROFILE=final forge test --offline --match-contract 'Anvil.*Fixture' --match-test '^test_exportAnvil' -vv; \
	tools/verify/run.sh "$$dest" python3 tests/release/native/rehearsal.py

# Independent SLP module gates; existing core and owner targets stay separate.
.PHONY: slp-build slp-test slp-verify
slp-build slp-test slp-verify:
	@set -euo pipefail; dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" bash slp/tools/check.sh $(patsubst slp-%,%,$@); \
	echo "SLP evidence: $$dest"

# Independent FaroSwap module gates (unit tests offline; fork tests need the public
# read-only RPC passed explicitly through the runner).
.PHONY: faroswap-test faroswap-fork
faroswap-test:
	@set -euo pipefail; dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" forge test --root faroswap --offline; \
	tools/verify/run.sh "$$dest" python3 -m unittest discover -s faroswap/tests -p 'test_deployment_plan.py' -v; \
	echo "FaroSwap evidence: $$dest"
faroswap-fork:
	@set -euo pipefail; dest=$$(tools/verify/export.sh | tail -1); \
	tools/verify/run.sh "$$dest" FOUNDRY_TEST=fork-tests FAROO_FORK_RPC_MAINNET=https://rpc.pharos.xyz \
	  forge test --root faroswap --offline --fork-url https://rpc.pharos.xyz -vv; \
	echo "FaroSwap fork evidence: $$dest"
