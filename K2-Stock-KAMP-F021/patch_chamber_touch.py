"""Make the F021 1.1.8.3 chamber tiles open the temperature keypad and show the target, like the heated K2 models.

Builds on patch_chamber_home_tile.py: the Home tile keeps its live reading, and both the
Home tile and the Axis/Temp tile now accept a tap. The keypad sends M141 S<temp>; the
overlay's M141 macro sets the chamber heater target.
"""

import argparse
import hashlib
import struct
from pathlib import Path

from patch_chamber_home_tile import EDITS as HOME_TILE_EDITS, STOCK_SHA256

# File offset = virtual address - 0x10000 for the ARM text segment.
BASE = 0x10000
MODEL = 0x12E3030          # machine model number read from system_config.json
F021 = 1015
HEATED = (1013, 1016, 1014, 1018)   # models the stock Home page makes tappable

# Home page builder: the stock gate for the chamber tap handler.
HOME_GATE = 0x17C63C
HOME_GATE_END = 0x17C708
HOME_READOUT = 0x1ABAAD4   # live chamber tile (kept visible for F021)
HOME_HEATING = 0x1ABAAE4   # heating tile, never created on F021
HOME_TAP = 0x1759A8        # opens the chamber keypad on LV_EVENT_CLICKED
ADD_FLAG = 0x1E1064
ADD_EVENT_CB = 0x1D8BF4

# Axis/Temp page builder: F021 skips the tappable chamber setup at these branches.
# Axis/Temp updater: F021 forces "not heating", which keeps the current/target view hidden.
AXIS_SKIPS = (0x13338C, 0x1335C8, 0x144058)
NOP = 0xE1A00000

# Home read-only chamber tile: its updater shows only the current value. Pass the target
# too. The big value label keeps the current reading; the small grey unit label after it
# shows "/target°C" while a target is set and "°C" otherwise, like the bed tile.
HOME_LABEL_CALL = 0x17EF78     # caller holds current at fp-0x26 and target at fp-0x28
HOME_LABEL = 0x17F624          # rewritten up to HOME_LABEL_END
HOME_LABEL_END = 0x17F720
HOME_VALUE = 0x1ABAADC         # big value label
HOME_UNIT = 0x1ABAAE0          # small unit label
HOME_ICON = 0x1ABAAD8          # chamber icon
# The tile is 148 px wide. The builder places the icon at x=27 and the value at x=60.
# With "/target" showing, both move left so icon, value and "/55°C" sit centered.
HOME_ICON_X, HOME_VALUE_X = 27, 60
HOME_TARGET_SHIFT = 16
SET_X = 0x1E5B74
HOME_PAGE_ACTIVE = 0x181A10
HOME_ALIGN = 0x181CA0          # places the unit label after the value
SET_TEXT = 0x27A88C
SPRINTF = 0x13530
FORMAT_INT = 0x123F4AC         # "%d"
FORMAT_SLASH_INT = 0x123F4B0   # "/%d"
FORMAT_STRING = 0x122724C      # "%s"
UNIT = 0x123F354               # "°C"


def movw(rd, imm):
    return 0xE3000000 | (imm >> 12) << 16 | rd << 12 | imm & 0xFFF


def movt(rd, imm):
    return 0xE3400000 | (imm >> 12) << 16 | rd << 12 | imm & 0xFFF


def ldr(rd, rn):
    return 0xE5900000 | rn << 16 | rd << 12


def mov(rd, imm):
    assert 0 <= imm < 256
    return 0xE3A00000 | rd << 12 | imm


def cmp(rn, rm):
    return 0xE1500000 | rn << 16 | rm


def ldrsh_fp(rd, offset):
    return 0xE15B00F0 | rd << 12 | (offset >> 4) << 8 | offset & 0xF   # ldrsh rd, [fp, #-offset]


def strh_fp(rd, offset):
    return 0xE14B00B0 | rd << 12 | (offset >> 4) << 8 | offset & 0xF   # strh rd, [fp, #-offset]


def strb_fp(rd, offset):
    return 0xE54B0000 | rd << 12 | offset                                 # strb rd, [fp, #-offset]


def sub_fp(rd, imm):
    return 0xE24B0000 | rd << 12 | imm                                    # sub rd, fp, #imm


def branch(cond, pc, target, link=False):
    offset = (target - pc - 8) >> 2
    return cond << 28 | (0x0B000000 if link else 0x0A000000) | offset & 0xFFFFFF


EQ, NE, GE, LE, AL = 0, 1, 10, 13, 14


def home_gate():
    """Heated models: unchanged handlers. F021: tap handler on the live tile only."""
    code = []

    def emit(*words):
        code.extend(words)

    def pc():
        return HOME_GATE + 4 * len(code)

    def call(function, *setup):
        emit(*setup)
        emit(branch(AL, pc(), function, link=True))

    def load_global(reg, address):
        emit(movw(reg, address & 0xFFFF), movt(reg, address >> 16), ldr(reg, reg))

    load_global(3, MODEL)
    heated_branches = []
    for model in HEATED:
        emit(movw(2, model), cmp(3, 2))
        heated_branches.append(len(code))
        emit(0)
    emit(movw(2, F021), cmp(3, 2))
    skip = len(code)
    emit(0)
    readout_branch = len(code)
    emit(0)
    heated = pc()
    load_global(0, HOME_HEATING)
    call(ADD_FLAG, mov(1, 3))
    load_global(0, HOME_HEATING)
    call(ADD_EVENT_CB, mov(3, 0), mov(2, 0), movw(1, HOME_TAP & 0xFFFF), movt(1, HOME_TAP >> 16))
    readout = pc()
    load_global(0, HOME_READOUT)
    call(ADD_FLAG, mov(1, 2))
    load_global(0, HOME_READOUT)
    call(ADD_EVENT_CB, mov(3, 0), mov(2, 0), movw(1, HOME_TAP & 0xFFFF), movt(1, HOME_TAP >> 16))
    for index in heated_branches:
        code[index] = branch(EQ, HOME_GATE + 4 * index, heated)
    code[skip] = branch(NE, HOME_GATE + 4 * skip, HOME_GATE_END)
    code[readout_branch] = branch(AL, HOME_GATE + 4 * readout_branch, readout)
    room = (HOME_GATE_END - HOME_GATE) // 4
    assert len(code) <= room, len(code)
    code += [NOP] * (room - len(code))
    return struct.pack("<%dI" % room, *code)


def home_label_edits():
    """(address, stock words, new words) for the Home tile's current/target text."""
    call = branch(AL, HOME_LABEL_CALL + 8, HOME_LABEL, link=True)
    code, fixups, labels = [], [], {}

    def emit(*words):
        code.extend(words)

    def bl(target):
        emit(branch(AL, HOME_LABEL + 4 * len(code), target, link=True))

    def jump(cond, label):
        fixups.append((len(code), cond, label))
        emit(0)

    def pointer(reg, value):
        emit(movw(reg, value & 0xFFFF), movt(reg, value >> 16))

    def load_global(reg, address):
        pointer(reg, address)
        emit(ldr(reg, reg))

    emit(0xE92D4810, 0xE28DB008, 0xE24DD024)          # push {r4, fp, lr}; add fp, sp, #8; sub sp, #0x24
    emit(strh_fp(0, 0x26), strh_fp(1, 0x28))          # current, target
    bl(HOME_PAGE_ACTIVE)
    emit(0xE3500000)                                   # cmp r0, #0
    jump(EQ, "exit")
    # The builder always creates the icon and unit label with the value label.
    load_global(0, HOME_VALUE)
    emit(0xE3500000)
    jump(EQ, "exit")
    emit(sub_fp(0, 0x20))                              # value label: "%d" current
    pointer(1, FORMAT_INT)
    emit(ldrsh_fp(2, 0x26))
    bl(SPRINTF)
    load_global(0, HOME_VALUE)
    emit(sub_fp(1, 0x20))
    bl(SET_TEXT)
    emit(mov(4, 0))                                    # r4 = left shift of icon and value
    emit(mov(0, 0), ldrsh_fp(2, 0x28), 0xE3520000)    # r0 = 0; cmp target, #0
    jump(LE, "unit")                                   # no target: "°C"
    emit(ldrsh_fp(3, 0x26), 0xE2833001, cmp(3, 2))    # current + 1 >= target: reached
    jump(GE, "unit")
    emit(mov(4, HOME_TARGET_SHIFT))
    emit(sub_fp(0, 0x20))                              # "/%d" target
    pointer(1, FORMAT_SLASH_INT)
    bl(SPRINTF)
    labels["unit"] = len(code)
    emit(sub_fp(3, 0x20), 0xE0830000)                  # r0 = buffer + length so far
    pointer(1, FORMAT_STRING)
    pointer(2, UNIT)
    bl(SPRINTF)                                        # append "°C"
    load_global(0, HOME_UNIT)
    emit(sub_fp(1, 0x20))
    bl(SET_TEXT)
    for obj, x in ((HOME_ICON, HOME_ICON_X), (HOME_VALUE, HOME_VALUE_X)):
        load_global(0, obj)
        emit(0xE2641000 | x)                           # rsb r1, r4, #x
        bl(SET_X)
    bl(HOME_ALIGN)
    labels["exit"] = len(code)
    emit(0xE24BD008, 0xE8BD8810)                       # sub sp, fp, #8; pop {r4, fp, pc}
    for index, cond, label in fixups:
        code[index] = branch(cond, HOME_LABEL + 4 * index, HOME_LABEL + 4 * labels[label])
    room = (HOME_LABEL_END - HOME_LABEL) // 4
    assert len(code) <= room, len(code)
    code += [NOP] * (room - len(code))
    return [
        (HOME_LABEL_CALL, (ldrsh_fp(3, 0x26), 0xE1A00003, call),
         (ldrsh_fp(0, 0x26), ldrsh_fp(1, 0x28), call)),
        (HOME_LABEL, (0xE92D4810, 0xE28DB008, 0xE24DD024, 0xE1A03000, strh_fp(3, 0x26)), tuple(code)),
    ]


def patch_display(data):
    if hashlib.sha256(data).hexdigest() != STOCK_SHA256:
        raise ValueError("The display-server binary does not match the stock 1.1.8.3 build.")
    result = bytearray(data)
    for offset, original, replacement in HOME_TILE_EDITS:
        assert data[offset:offset + len(original)] == original
        result[offset:offset + len(original)] = replacement
    for address in AXIS_SKIPS:
        offset = address - BASE
        word, = struct.unpack_from("<I", data, offset)
        if word >> 24 != 0x0A:
            raise ValueError(f"Unexpected Axis/Temp branch at {address:#x}.")
        struct.pack_into("<I", result, offset, NOP)
    start, end = HOME_GATE - BASE, HOME_GATE_END - BASE
    expected = struct.pack("<3I", movw(3, MODEL & 0xFFFF), movt(3, MODEL >> 16), ldr(3, 3))
    if data[start:start + 12] != expected:
        raise ValueError("Unexpected Home chamber gate.")
    result[start:end] = home_gate()
    for address, stock, words in home_label_edits():
        offset = address - BASE
        if stock is not None and data[offset:offset + 4 * len(stock)] != struct.pack("<%dI" % len(stock), *stock):
            raise ValueError(f"Unexpected Home chamber label code at {address:#x}.")
        result[offset:offset + 4 * len(words)] = struct.pack("<%dI" % len(words), *words)
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
