#!/usr/bin/env python3
"""Reproduce the verified DP dummy EDID using only Python's standard library."""

import argparse
import hashlib
from pathlib import Path
import struct


# Width, height, vertical front porch, pixel clock in EDID's 10 kHz units.
# CVT RBv2 timings from edid-decode's CVT calculator, rounded to 10 kHz.
TIMINGS = [(1920, 1080, 25, 16785), (1920, 1080, 33, 20286),
           (2560, 1440, 27, 23459), (2560, 1440, 38, 29542),
           (2560, 1440, 49, 35711)]
SHA256 = "a370a75525fb634c05b3f75a2a29777da7a40ceae35d8443e830bbf65a78d448"


def rb_timing(width, height, vfront, clock):
    descriptor = bytearray(18)
    descriptor[:2] = struct.pack("<H", clock)
    descriptor[2:5] = bytes([width & 255, 80, (width >> 8) << 4])
    descriptor[5:8] = bytes([height & 255, vfront + 14, (height >> 8) << 4])
    descriptor[8:12] = bytes([8, 32, ((vfront & 15) << 4) | 8, (vfront >> 4) << 2])
    descriptor[12:15] = bytes([600 & 255, 340 & 255, (2 << 4) | 1])
    descriptor[17] = 0x1A  # Separate sync, +H/-V.
    return descriptor


def generate():
    base = bytearray(128)
    base[:8] = bytes.fromhex("00ffffffffffff00")
    base[8:10] = struct.pack(">H", (12 << 10) | (14 << 5) | 24)  # Synthetic LNX.
    base[10:12] = struct.pack("<H", 0xD002)
    # Fixed identity/date for reproducible bytes; digital DisplayPort, 8 bpc.
    base[16:25] = bytes([0, 36, 1, 4, 0xA5, 60, 34, 120, 6])
    chroma = [round(v * 1024) for v in [.64, .33, .30, .60, .15, .06, .3127, .3290]]
    for offset in (0, 4):
        base[25 + offset // 4] = sum((chroma[offset + i] & 3) << (6 - 2 * i)
                                    for i in range(4))
    base[27:35] = bytes(v >> 2 for v in chroma)
    base[35] = 0x20  # VGA fallback.
    base[38:54] = b"\x01\x01" * 8
    # Preferred/native CTA 1080p60: 148.5 MHz, totals 2200 x 1125, +H/+V.
    base[54:72] = bytes.fromhex("023a801871382d40582c450058542100001e")
    base[72:90] = rb_timing(*TIMINGS[2])
    base[90:108] = bytes([0, 0, 0, 0xFD, 0, 59, 90, 31, 136, 36, 1, 10]) + b" " * 6
    base[108:126] = b"\0\0\0\xFC\0" + b"DP Dummy1080\n"
    base[126] = 1
    base[127] = -sum(base) & 255
    extension = bytearray(128)
    # CTA v3, underscan, one native DTD (base DTD 1), VIC 16, RGB quantization.
    extension[:9] = bytes([2, 3, 9, 0x81, 0x41, 0x10, 0xE2, 0, 0x4A])
    for index, timing in enumerate([TIMINGS[i] for i in (0, 1, 3, 4)]):
        start = 9 + index * 18
        extension[start:start + 18] = rb_timing(*timing)
    extension[127] = -sum(extension) & 255
    result = bytes(base + extension)
    assert hashlib.sha256(result).hexdigest() == SHA256
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", type=Path, help="Compare an existing EDID with the reproducible profile")
    action.add_argument("--output", type=Path, help="Create an EDID file; existing files are never overwritten")
    args = parser.parse_args()
    data = generate()
    if args.check:
        if args.check.read_bytes() != data:
            parser.error("EDID differs from the verified profile")
        print("EDID matches the verified profile: " + SHA256)
    else:
        with args.output.open("xb") as stream:
            stream.write(data)
        print("Created " + str(args.output) + " (256 bytes, SHA-256 " + SHA256 + ")")


if __name__ == "__main__":
    main()
