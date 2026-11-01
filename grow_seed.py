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
import re
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


def apfs_view(devs):
    """diskutil's APFS summary, parsed from the text listing.

    The -plist form was tried first and is not worth the guessing: its container key/PhysicalStore
    spelling did not match on a runner, while the human listing is stable and already carries every
    number we need. hdiutil also synthesizes a second node for the container inside an attached image
    (disk7 = whole image, disk8 = container), and diskutil names stores without the /dev/ prefix, so
    match on the stripped set of everything the attach produced."""
    wanted = {d.replace("/dev/", "") for d in devs}
    text = sh("diskutil", "apfs", "list")
    return parse_apfs_list(text, wanted)


def num(pat, where):
    m = re.search(pat, where)
    return int(m.group(1)) if m else None


def parse_apfs_list(text, wanted):
    """Split diskutil's listing into per-container facts, keeping only containers on our own disks."""
    blocks = []
    for chunk in text.split("Container disk")[1:]:
        name = chunk.split()[0]
        head = chunk.split("+-> Volume")[0]
        store = re.search(r"Physical Store\s+(disk\S+)", head)
        if not store or store.group(1) not in wanted:
            continue
        volumes = []
        for v in re.split(r"\+-> Volume ", chunk)[1:]:
            uuid = re.search(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", v)
            nm = re.search(r"Name:\s+(.+?)\s+\(Case", v)
            volumes.append({"device": v.split()[0], "uuid": uuid.group(0) if uuid else None,
                            "name": nm.group(1) if nm else None,
                            "consumed": num(r"Capacity Consumed:\s+(\d+) B", v)})
        blocks.append({"container": "disk" + name, "store": store.group(1),
                       "ceiling": num(r"Size \(Capacity Ceiling\):\s+(\d+) B", head),
                       "in_use": num(r"Capacity In Use By Volumes:\s+(\d+) B", head),
                       "not_allocated": num(r"Capacity Not Allocated:\s+(\d+) B", head),
                       "store_size": num(r"Physical Store Disk:\s+disk\S+\s+.?\s*Size:\s+(\d+) B", head)
                       or num(r"Size:\s+(\d+) B", head),
                       "volumes": volumes})
    return blocks


def attach(image):
    """/dev/diskN for the whole disk image. hdiutil prints one line per slice, and the trailing
    column is a content hint ('41504653-0000-11AA-…' is literally the ASCII for APFS), so the
    device node has to come off the front of the line, not the end of the output."""
    out = sh("hdiutil", "attach", "-nomount", str(image))
    devs = []
    for line in out.splitlines():
        cols = line.split()
        if not cols or not cols[0].startswith("/dev/disk"):
            continue
        print("   attach:", line.strip())
        devs.append(cols[0])
    if not devs:
        sys.exit(f"hdiutil attach printed no device node:\n{out}")
    return devs


def inject_disable_watchdogd(mount):
    """Write launchd's own opt-out for one daemon, byte-for-byte the shape the official
    kits/sheng/BootWatchdogPolicy.c writes when the hardware watchdog backend is missing.

    Done here instead of in the kernel because the Data volume is an image we build: this also proves
    we can place *files* on it, which mkdir-only skeleton steps cannot do (prng.seed and friends need it).
    """
    import plistlib

    target = mount / "db" / "com.apple.xpc.launchd"
    target.mkdir(parents=True, exist_ok=True)
    plist = target / "disabled.plist"
    plist.write_bytes(plistlib.dumps({"com.apple.watchdogd": True}))
    (target / "disabled.migrated").write_bytes(b"")
    print(f"injected {plist} ({plist.stat().st_size} B) + disabled.migrated (0 B)")
    return plistlib.dumps({"com.apple.watchdogd": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--gib", type=float, default=1.0)
    ap.add_argument("--readonly", action="store_true")
    ap.add_argument("--disable-watchdogd", action="store_true",
                    help="mount the Data volume and write launchd's disabled.plist for com.apple.watchdogd")
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

    def sb_slots(image):
        """Every u64 slot of the container superblock page that is non-zero, keyed by file offset.

        Used only as a before/after diff so a resize is judged by which fields actually moved, instead
        of by offsets guessed from a header file."""
        base = 0x20
        return {base + o: int.from_bytes(image[base + o:base + o + 8], "little")
                for o in range(0, 512, 8)
                if any(image[base + o:base + o + 8])}

    devs = attach(raw)
    print(f"attached as {' '.join(devs)}")
    before_slots = sb_slots(raw.read_bytes()[:0x300])
    try:
        print("\n=== BEFORE ===")
        before = apfs_view(devs)
        if not before:
            # Still give the human something to read rather than dying on a parsing detail.
            print("plist view found no container; raw diskutil output follows")
            for d in devs:
                print(sh("diskutil", "apfs", "list", d, check=False))
            sys.exit("no APFS container reported for " + " ".join(devs))
        print(json.dumps(before, indent=2))
        if not a.readonly:
            store = before[0]["container"]
            if not store:
                sys.exit("diskutil did not report a container identifier")
            print(f"\nresizeContainer {store} -> {target} bytes")
            print(sh("diskutil", "apfs", "resizeContainer", store, f"{target}B"))
            print("\n=== AFTER ===")
            after = apfs_view(devs)
            print(json.dumps(after, indent=2))
            if a.disable_watchdogd:
                vols = [v for block in after for v in block["volumes"]]
                if not vols:
                    sys.exit("the container reports no volumes; cannot inject")
                data = next((v for v in vols if v["name"] == "Data"), vols[0])
                print(f"\nmounting volume {data['device']} (name={data['name']} "
                      f"uuid={data['uuid']} consumed={data['consumed']} B)")
                print(sh("diskutil", "mount", data["device"]))
                inject_disable_watchdogd(pathlib.Path("/Volumes") / (data["name"] or "Data"))
                print(sh("diskutil", "unmount", data["device"]))
        sh("hdiutil", "detach", devs[0])
    except SystemExit:
        sh("hdiutil", "detach", devs[0], check=False)
        raise

    grown = raw.read_bytes()
    if not a.readonly:
        after_slots = sb_slots(grown[:0x300])
        moved = {hex(k): (before_slots.get(k), after_slots.get(k))
                 for k in set(before_slots) | set(after_slots)
                 if before_slots.get(k) != after_slots.get(k)}
        print(f"\nsuperblock fields changed by the resize: {json.dumps(moved, indent=2)}")
    if a.disable_watchdogd:
        # The catalog records the names as plain ASCII and the bplist stores its keys the same way, so
        # this is a real read-back of what landed on the volume, not a trust-the-tool claim.
        for probe in [b"disabled.plist", b"disabled.migrated", b"com.apple.watchdogd", b"bplist00"]:
            print(f"  in-image probe {probe!r}: {grown.count(probe)} hit(s)")
        if not grown.count(b"com.apple.watchdogd"):
            sys.exit("INJECT FAILED: the watchdogd key is not in the image; the volume write did not land")
    out = pack(grown)
    pathlib.Path(a.dst).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(a.dst).write_bytes(out)
    pages = sum(1 for i in range(0, len(grown), PAGE) if any(grown[i:i + PAGE]))
    print(f"\nre-packed {a.dst}: {len(out)} B from a {len(grown)} B image, {pages} non-zero pages")


main()
