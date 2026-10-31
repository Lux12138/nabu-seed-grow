#!/usr/bin/env python3
"""Grow the Data seed's APFS container on a macOS host without changing its volume UUID.

Reads the packed DBRDISK1 form (the layout drivers/DarwinBoot/Test/packed_ramdisk.py writes), expands
it to a sparse raw image, asks diskutil what the container actually holds, optionally resizes the
container, and re-packs the result. Input and output stay in the tens of kilobytes because the
container holds only metadata (18 non-zero pages in the shipped seed).

usage: grow_seed.py IN.dbr OUT.dbr --gib 1 [--readonly]
"""
import argparse
import json
import pathlib
import plistlib
import struct
import subprocess
import sys
import tempfile

PAGE = 4096


def sh(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        sys.exit(f"FAILED: {' '.join(map(str, cmd))}\n{r.stdout}\n{r.stderr}")
    return r.stdout + r.stderr


def unpack(blob):
    magic, ver, page, logical, count = struct.unpack_from("<8sIIQQ", blob, 0)
    if magic != b"DBRDISK1" or ver != 1 or page != PAGE:
        sys.exit(f"not a DBRDISK1 v1/4096 seed: {magic!r} ver={ver} page={page}")
    spans, cursor = [], 32 + 16 * count
    for i in range(count):
        off, ln = struct.unpack_from("<QQ", blob, 32 + 16 * i)
        spans.append((off, blob[cursor:cursor + ln]))
        cursor += ln
    if cursor != len(blob):
        sys.exit("trailing packed bytes")
    return logical, spans


def pack(image):
    """Zero-page packing; the inverse of unpack above and the same output form as the official packer."""
    spans, start = [], None
    for i in range(0, len(image), PAGE):
        nonzero = any(image[i:i + PAGE])
        if nonzero and start is None:
            start = i
        elif not nonzero and start is not None:
            spans.append((start, image[start:i]))
            start = None
    if start is not None:
        spans.append((start, image[start:]))
    head = struct.pack("<8sIIQQ", b"DBRDISK1", 1, PAGE, len(image), len(spans))
    head += b"".join(struct.pack("<QQ", o, len(d)) for o, d in spans)
    return head + b"".join(d for _o, d in spans)


def apfs_view(dev):
    """diskutil's APFS tree as a plain dict of the numbers we care about, only for our own disk."""
    out = sh("diskutil", "apfs", "list", "-plist")
    tree = plistlib.loads(out.encode())
    containers = []
    for c in tree.get("AllAPFSContainers", tree.get("Containers", [])):
        if dev and c.get("PhysicalStore") != dev:
            continue
        info = {"container": c.get("Identifier"), "store": c.get("PhysicalStore"),
                "size": c.get("SizeTotal"), "free": c.get("SizeFree"),
                "reserved": c.get("SizeReserved"), "used": c.get("SizeUsed")}
        info["volumes"] = [{"name": v.get("VolumeName"), "uuid": v.get("UUID"),
                            "used": v.get("VolumeUsedBytes"),
                            "quota": v.get("VolumeQuota"),
                            "reserve": v.get("VolumeReservedBytes")}
                           for v in c.get("Volumes", [])]
        containers.append(info)
    return containers


def attach(image):
    """/dev/diskN for the whole disk image. hdiutil prints one line per slice, and the trailing
    column is a content hint ('41504653-0000-11AA-…' is literally the ASCII for APFS), so the
    device node has to come off the front of the line, not the end of the output."""
    out = sh("hdiutil", "attach", "-nomount", str(image))
    whole = None
    for line in out.splitlines():
        cols = line.split()
        if not cols or not cols[0].startswith("/dev/disk"):
            continue
        print("   attach:", line.strip())
        if whole is None:
            whole = cols[0]
    if not whole:
        sys.exit(f"hdiutil attach printed no device node:\n{out}")
    return whole


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--gib", type=float, default=1.0)
    ap.add_argument("--readonly", action="store_true")
    a = ap.parse_args()

    blob = pathlib.Path(a.src).read_bytes()
    logical, spans = unpack(blob)
    target = int(a.gib * 1024 ** 3)
    print(f"seed packed {len(blob)} B, logical {logical} ({logical / 1024**3:.3g} GiB), "
          f"{len(spans)} extents, {sum(len(d) for _o, d in spans)} non-zero bytes")
    if not a.readonly and target <= logical:
        sys.exit(f"--gib {a.gib} must exceed the current {logical} bytes, or use --readonly")

    work = pathlib.Path(tempfile.mkdtemp(prefix="seedgrow-"))
    raw = work / "seed.img"
    with raw.open("wb") as f:
        f.truncate(max(logical, target))
        for off, data in spans:
            f.seek(off)
            f.write(data)
    print(f"expanded -> {raw} ({raw.stat().st_size} bytes, sparse)")

    dev = attach(raw)
    print(f"attached as {dev}")
    try:
        print("\n=== BEFORE ===")
        before = apfs_view(dev)
        if not before:
            # Still give the human something to read rather than dying on a parsing detail.
            print("plist view found no container; raw diskutil output follows")
            print(sh("diskutil", "apfs", "list", dev, check=False))
            sys.exit("no APFS container reported for " + dev)
        print(json.dumps(before, indent=2))
        if not a.readonly:
            store = before[0]["container"]
            if not store:
                sys.exit("diskutil did not report a container identifier")
            print(f"\nresizeContainer {store} -> {target} bytes")
            print(sh("diskutil", "apfs", "resizeContainer", store, f"{target}B"))
            print("\n=== AFTER ===")
            print(json.dumps(apfs_view(dev), indent=2))
        sh("hdiutil", "detach", dev)
    except SystemExit:
        sh("hdiutil", "detach", dev, check=False)
        raise

    grown = raw.read_bytes()
    out = pack(grown)
    pathlib.Path(a.dst).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.dst).write_bytes(out)
    pages = sum(1 for i in range(0, len(grown), PAGE) if any(grown[i:i + PAGE]))
    print(f"\nre-packed {a.dst}: {len(out)} B from a {len(grown)} B image, {pages} non-zero pages")


main()
