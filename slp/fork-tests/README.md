# Current mainnet integration

These opt-in tests execute existing mainnet proxies, never etched replacement SLP code.
They are separate from the original 76 mock tests in `../tests/`.

Authenticate a new provider-finalized block, numeric/hash rereads, full runtimes, proxy
slots, authorities and wiring before selecting a pin. Batch 3's authenticated pin is
22640026 / `0x5701dc0edd4ce908f0e2c239e66062c1728b2a690511df5e04e583ba88a743bd`.
New identities require investigation, not an automatic baseline update.

From the production root, export once, then run only inside the approved export:

```sh
dest=$(tools/verify/export.sh | tail -1)
tools/verify/run.sh "$dest" FOUNDRY_TEST=fork-tests SLP_FORK_BLOCK=22640026 \
  forge test --root slp --offline --evm-version cancun \
  -R 'forge-std/=test-lib/forge-std/src/' \
  --fork-url https://rpc.pharos.xyz --fork-block-number 22640026 \
  --match-contract '^CurrentMainnet$' -vvv
tools/verify/run.sh "$dest" FOUNDRY_TEST=fork-tests SLP_FORK_BLOCK=22640026 \
  forge test --root slp --offline --evm-version cancun \
  -R 'forge-std/=test-lib/forge-std/src/' \
  --fork-url https://rpc.pharos.xyz --fork-block-number 22640026 \
  --match-contract '^NativeBoundary$' --match-test '^test_NATIVE_' -vvv
```

The execution EVM must support the deployed core's Cancun opcodes. This override
changes neither canonical compiler settings nor release identities. The native suite
inherits the fixture; the explicit test filter prevents double-counting inherited tests.

- `REAL_FORK`: actual deployed runtime and existing cash, with local caller impersonation.
- `CONTROLLED`: actual runtime, but seeded test cash, clock advancement, or local privileged
  receiver configuration. These are attack/boundary simulations, never observed live behavior.
- `NATIVE`: EVM diagnostics of real native code. Claim counter growth without cash is a
  missing node credit boundary. The immediate-claim diagnostic records the revert; its
  success is not proof of mature withdrawal settlement. No `vm.etch`, `vm.mockCall`, native
  reward balance credit or synthetic Staking is used.

A full native exit/maturity/credit lifecycle remains INCOMPLETE. Historical replay,
ordinary EVM execution and native-node evidence are separate qualifications.
