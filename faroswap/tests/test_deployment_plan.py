#!/usr/bin/env python3
"""Deterministic, network-free regression tests for the FaroSwap deployment plan logic.

Covers the ordered deployer transaction sequence, nonce-sensitive CREATE-address
prediction (the HIGH remediation), the (address[]) registry decoding, and the
static calldata encoder. Run inside the approved export:

    python3 -m unittest discover -s faroswap/tests -p 'test_deployment_plan.py' -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "prepare_deployment", ROOT / "faroswap" / "scripts" / "prepare_deployment.py")
plan = importlib.util.module_from_spec(_spec)
sys.modules["prepare_deployment"] = plan
_spec.loader.exec_module(plan)


class SequenceDerivation(unittest.TestCase):
    DEPLOYER = "0x8ce84e9fa0101d317d8956d73610ad3e0e219d41"

    def test_sequence_order_and_nonce_consumption(self):
        seq = plan.derive_sequence(self.DEPLOYER, 45)
        self.assertEqual([s["key"] for s in seq],
                         ["implementation", "proxy", "pool", "bootstrap",
                          "approve-base", "approve-quote", "execute"])
        # consecutive nonce consumption in execution order
        self.assertEqual([s["nonce"] for s in seq], [45, 46, 47, 48, 49, 50, 51])
        # exactly three CREATEs; the factory pool-creation CALL sits between them
        creates = [s["key"] for s in seq if s["creates"]]
        self.assertEqual(creates, ["implementation", "proxy", "bootstrap"])
        self.assertFalse(seq[2]["creates"], "factory pool creation is a CALL, not a CREATE")
        self.assertTrue(all("predictedAddress" in s for s in seq if s["creates"]))
        self.assertTrue(all("predictedAddress" not in s for s in seq if not s["creates"]))

    def test_bootstrap_prediction_accounts_for_factory_call_nonce(self):
        # The HIGH finding: bootstrap must NOT be predicted at base+2; the pool-creation
        # CALL consumes base+2, so bootstrap lands at base+3.
        seq = plan.derive_sequence(self.DEPLOYER, 100)
        self.assertEqual(seq[3]["nonce"], 103)
        self.assertEqual(seq[3]["predictedAddress"], plan.create_address(self.DEPLOYER, 103))
        self.assertNotEqual(seq[3]["predictedAddress"], plan.create_address(self.DEPLOYER, 102),
                            "bootstrap must not be predicted at the pool-creation nonce")

    def test_nonce_shift_moves_every_prediction_deterministically(self):
        a = plan.derive_sequence(self.DEPLOYER, 7)
        b = plan.derive_sequence(self.DEPLOYER, 8)
        for sa, sb in zip(a, b):
            self.assertEqual(sb["nonce"], sa["nonce"] + 1)
            if sa["creates"]:
                self.assertNotEqual(sa["predictedAddress"], sb["predictedAddress"])
        # predictions depend on the sender
        other = plan.derive_sequence("0x" + "11" * 20, 7)
        self.assertNotEqual(a[0]["predictedAddress"], other[0]["predictedAddress"])

    def test_predictions_are_canonical_create_addresses(self):
        # Independently recompute one CREATE address from raw RLP.
        sender = bytes.fromhex(self.DEPLOYER[2:])
        # RLP([address, 45]) hand-built: 0xd6 0x94 <20b> <nonce>
        manual = bytes([0xD6, 0x94]) + sender + bytes([45])
        from tools.lib.keccak import keccak256_bytes
        expected = "0x" + keccak256_bytes(manual)[-20:].hex()
        seq = plan.derive_sequence(self.DEPLOYER, 45)
        self.assertEqual(seq[0]["predictedAddress"], expected)

    def test_funding_steps_shift_later_nonces_only(self):
        # Without funding: bootstrap at +3, approvals +4/+5, execute +6.
        lean = plan.derive_sequence(self.DEPLOYER, 45)
        self.assertEqual([s["nonce"] for s in lean][-3:], [49, 50, 51])
        # With the two funding steps everything after the bootstrap shifts by exactly 2;
        # the three CREATE predictions themselves are unchanged.
        funded = plan.derive_sequence(self.DEPLOYER, 45, include_funding=True)
        self.assertEqual([s["key"] for s in funded],
                         ["implementation", "proxy", "pool", "bootstrap",
                          "fund-wpros", "fund-stpros", "approve-base", "approve-quote", "execute"])
        self.assertEqual([s["nonce"] for s in funded][-3:], [51, 52, 53])
        for lean_step, funded_step in zip(lean, funded[:4]):
            self.assertEqual(lean_step["nonce"], funded_step["nonce"])
            if lean_step["creates"]:
                self.assertEqual(lean_step["predictedAddress"], funded_step["predictedAddress"])

    def test_sequence_table_matches_runbook_order(self):
        # The bootstrap constructor needs the pool address, so its deployment must come
        # after the factory CALL step.
        seq = plan.derive_sequence(self.DEPLOYER, 0)
        keys = [s["key"] for s in seq]
        self.assertLess(keys.index("pool"), keys.index("bootstrap"))


class RegistryDecoding(unittest.TestCase):
    def test_decode_address_array(self):
        items = ["0x" + hex(i)[2:].rjust(40, "0") for i in (1, 2, 3)]
        body = f'{hex(32)[2:].rjust(64, "0")}{hex(len(items))[2:].rjust(64, "0")}' + "".join(
            "0" * 24 + i[2:] for i in items)
        self.assertEqual(plan.decode_address_array("0x" + body), items)

    def test_decode_empty_array(self):
        self.assertEqual(
            plan.decode_address_array("0x" + hex(32)[2:].rjust(64, "0") + "0" * 64), [])

    def test_decode_rejects_bad_offset_and_truncation(self):
        with self.assertRaises(ValueError):
            plan.decode_address_array("0x" + hex(64)[2:].rjust(64, "0") + "0" * 64)
        with self.assertRaises(ValueError):
            plan.decode_address_array("0x" + "0" * 126)  # too short
        # length word announces 2 items but only 1 present
        with self.assertRaises(ValueError):
            plan.decode_address_array(
                "0x" + hex(32)[2:].rjust(64, "0") + hex(2)[2:].rjust(64, "0") + "0" * 64)

    def test_previous_off_by_two_bug_is_fixed(self):
        # The old decoder counted length as len(hex)//64 (offset+length words included),
        # reporting n+2 pools. The fix must return exactly n.
        one = "0x" + hex(32)[2:].rjust(64, "0") + hex(1)[2:].rjust(64, "0") + "0" * 64 + "ab" * 20
        self.assertEqual(len(plan.decode_address_array(one)), 1)


class CalldataEncoding(unittest.TestCase):
    def test_static_encoding_shape(self):
        cd = plan.enc("0x11223344", "0x" + "ab" * 20, 5, True)
        self.assertEqual(len(cd), 10 + 3 * 64)
        self.assertTrue(cd.startswith("0x11223344"))
        self.assertTrue(cd.endswith("0" * 63 + "1"))

    def test_rejects_dynamic_and_bad_types(self):
        with self.assertRaises(TypeError):
            plan.enc("0x11223344", "not-a-value")


if __name__ == "__main__":
    unittest.main()
