#!/usr/bin/env python3
"""Check a packed seed on any OS: unpack it, report the container's own geometry and volume identity.

usage: verify_seed.py FILE.dbr [FILE.dbr ...]
"""
import pathlib
import struct
import sys

PAGE = 4096
VOLUME_UUID = bytes.fromhex("5a1b2c3d4e5f60718293a4b5c6d7e8f9")

ok = True
for name in sys.argv[1:]:
    blob = pathlib.Path(name).read_bytes()
    magic, ver, page, logical, count = struct.unpack_from("<8sIIQQ", blob, 0)
    cursor = 32 + 16 * count
    img = bytearray(logical)
    for i in range(count):
        off, ln = struct.unpack_from("<QQ", blob, 32 + 16 * i)
        img[off:off + ln] = blob[cursor:cursor + ln]
        cursor += ln
    body = bytes(img)
    blocks = int.from_bytes(body[0x28:0x30], "little")
    pages = sum(1 for i in range(0, logical, PAGE) if any(body[i:i + PAGE]))
    good = (magic == b"DBRDISK1" and ver == 1 and page == PAGE and cursor == len(blob)
            and body[0x20:0x24] == b"NXSB" and blocks * PAGE == logical and body.count(VOLUME_UUID) >= 1)
    ok &= good
    print(f"{name}: packed {len(blob)} B logical {logical} ({logical / 1024**3:.3g} GiB) extents {count} "
          f"trailer_consumed={cursor == len(blob)}")
    print(f"   container magic={body[0x20:0x24]!r} block_size={int.from_bytes(body[0x24:0x28],'little')} "
          f"blocks={blocks} non_zero_pages={pages} volume-uuid-hits={body.count(VOLUME_UUID)}")
    print(f"   {'OK' if good else 'BAD'}")
sys.exit(0 if ok else 1)
