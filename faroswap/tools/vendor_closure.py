#!/usr/bin/env python3
"""Regenerate faroswap/lib + faroswap/test-lib from the frozen root vendor closure.

Copies the byte-exact import closure of the FaroSwap module sources from the
repository's tracked lib/ vendor closure into the module, and records every
copied file's SHA-256 in faroswap/DEPENDENCIES.json. Run from the repository
root (no network, no Foundry):

    python3 faroswap/tools/vendor_closure.py

Any difference between lib/ and the module copies is a reviewed change; this
script intentionally overwrites the module copies back to the root bytes.
"""
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "faroswap"

PREFIX = {
    "@openzeppelin/contracts/": "lib/openzeppelin-contracts/",
    "@openzeppelin/contracts-upgradeable/": "lib/openzeppelin-contracts-upgradeable/",
    "forge-std/": "lib/forge-std/src/",
}
# both `import "x";` and `import {...} from "x";`
IMPORT = re.compile(r'import\s+(?:[^;"\']*?from\s*)?["\']([^"\']+)["\']')

FORK_TEST = "faroswap/fork-tests/FaroSwapMainnetFork.t.sol"
FORK_TEST_V2 = "faroswap/fork-tests/FaroSwapUpgradeRehearsal.t.sol"

ENTRIES = [
    "faroswap/contracts/FaroSwapPriceManager.sol",
    "faroswap/contracts/FaroSwapPoolBootstrap.sol",
    "faroswap/contracts/interfaces/IFaroSwapGSP.sol",
    "faroswap/contracts/interfaces/IFaroSwapGSPFactory.sol",
    "faroswap/tests/FaroSwapPriceManager.t.sol",
    "faroswap/tests/FaroSwapPoolBootstrap.t.sol",
    "faroswap/tests/mocks/MockGSP.sol",
    "faroswap/tests/mocks/MockGSPFactory.sol",
    "faroswap/tests/mocks/Mocks.sol",
    "faroswap/tests/mocks/MockERC20.sol",
    "faroswap/tests/mocks/FaroSwapPriceManagerV2.sol",
    FORK_TEST,
    FORK_TEST_V2,
]


def repo_rel(importer, spec):
    for prefix, root in PREFIX.items():
        if spec.startswith(prefix):
            return os.path.normpath(root + spec[len(prefix):])
    return os.path.normpath(str((Path(importer).parent / spec).as_posix()))


def main():
    entries = [e for e in ENTRIES if (ROOT / e).exists()]
    parsed = set()
    lib_files = set()
    stack = list(entries)
    while stack:
        path = stack.pop()
        if path in parsed or not path.endswith(".sol"):
            continue
        parsed.add(path)
        for spec in IMPORT.findall((ROOT / path).read_text()):
            p = repo_rel(path, spec)
            if p not in parsed:
                stack.append(p)
        if path.startswith("lib/"):
            lib_files.add(path)

    record = {}
    for p in sorted(lib_files):
        src = ROOT / p
        if p.startswith("lib/forge-std/src/"):
            rel = p.replace("lib/forge-std/src/", "test-lib/forge-std/src/", 1)
        else:
            rel = p  # faroswap/lib/...
        dst = MOD / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        record[rel] = hashlib.sha256(src.read_bytes()).hexdigest()

    out = {
        "note": (
            "Byte-exact copies of the frozen root vendor closure (git-tracked lib/), "
            "auto-derived from the module import closure by faroswap/tools/vendor_closure.py. "
            "Any drift from lib/ is a reviewed change."
        ),
        "files": record,
    }
    (MOD / "DEPENDENCIES.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"copied {len(record)} files into faroswap/lib + faroswap/test-lib")


if __name__ == "__main__":
    main()
