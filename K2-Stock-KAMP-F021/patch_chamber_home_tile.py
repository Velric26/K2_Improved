"""Keep the live chamber readout visible on the F021 1.1.8.3 Home screen."""

import argparse
import hashlib
from pathlib import Path


STOCK_SHA256 = "0e23e687276a989c286d25d9cf137a1c08ca1643f294a77afbda4e6d8ba25190"

# The stock ARM text segment maps at file offset = virtual address - 0x10000.
# Before the tile visibility check, keep model 1017's original bypass and
# select the read-only tile for model 1015, even with a nonzero heater target.
EDITS = (
    (0x16ED4C, bytes.fromhex(
        "0030a0e1 05304be5 303003e3 2e3140e3 003093e5 f92300e3 020053e1 3a00000a 05205be5"
    ), bytes.fromhex(
        "303003e3 2e3140e3 003093e5 f92300e3 020053e1 3c00000a f72300e3 020053e1 0000a003"
    )),
    # The sole caller passes a boolean. Keep that value in r0 until stored.
    (0x16ED7C, bytes.fromhex("030052e1"), bytes.fromhex("030050e1")),
    (0x16ED84, bytes.fromhex("05205be5"), bytes.fromhex("05004be5")),
    (0x16ED90, bytes.fromhex("0020c3e5"), bytes.fromhex("0000c3e5")),
)


def patch_display(data):
    if hashlib.sha256(data).hexdigest() != STOCK_SHA256:
        raise ValueError("The display-server binary does not match the stock 1.1.8.3 build.")
    result = bytearray(data)
    for offset, original, replacement in EDITS:
        end = offset + len(original)
        if data[offset:end] != original or len(original) != len(replacement):
            raise ValueError(f"Unexpected display instructions at file offset {offset:#x}.")
        result[offset:end] = replacement
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = patch_display(args.stock.read_bytes())
    with args.output.open("xb") as output:
        output.write(data)
    print(hashlib.sha256(data).hexdigest(), args.output)


if __name__ == "__main__":
    main()
