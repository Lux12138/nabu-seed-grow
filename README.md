# nabu-seed-grow

A GitHub-hosted **macOS** job that grows the APFS container of an iPad Data seed. Nothing else in
this repo needs macOS; the board build itself runs on Linux.

## Why this exists

The writable `/private/var` on our boot comes from a *Data seed*: a small APFS container delivered
inside the payload as a zero-page-packed image (`DBRDISK1`, see
`drivers/DarwinBoot/Test/packed_ramdisk.py` upstream, BSD-2-Clause-Patent). The shipped 256 MiB seed
holds ~10 MiB of free space, and one iOS boot task (`tzinit`) needs ~137 MiB for a single
transaction, so it fails with ENOSPC and the kernel then panics because a critical boot task exited.

Growing an APFS container can only be done by Apple's own tooling (`diskutil apfs resizeContainer`),
which exists on macOS - so we run that one step on a CI macOS runner instead of owning a Mac.
Creating the image from scratch with `apfsprogs`/`mkapfs` does **not** work: those containers are
rejected by Apple's APFS driver (verified on hardware - `unexpected checkpoint map entry address
0x44, expected 0x42` at the same size as a container that does mount).

Sizes stay tiny in both directions: the seed's 256 MiB is only 18 non-zero pages, so the packed form
is ~74 KB, and the grown 1 GiB form packs to about the same.

## Usage

Run **Run workflow** on `grow-data-seed`:

| input | meaning |
|---|---|
| `mode = inspect` | attach the shipped seed read-only and print what `diskutil` reports for it (capacity, free, reserved, per-volume quota). No writes, no resize. |
| `mode = grow` | resize the container to `size_gib` and re-pack. |
| `size_gib` | keep **below 2**. Above 2 GiB the RAM-backed block device switches to a store-backed path that needs a persistent write window on real storage. |

The `verify` job then re-opens the artifact with `verify_seed.py` (pure Python, the same reader the
board-side gate uses) and with `libfsapfs`, so a bad grow is caught in CI rather than by a device
round. Download the artifact with:

```
gh run download <run-id> -n seed-grow-1gib
```

## Files

- `grow_seed.py` - unpack `DBRDISK1`, expand to a sparse image, `hdiutil attach -nomount`, read
  `diskutil apfs list -plist`, optionally `resizeContainer`, re-pack. Run only on macOS.
- `verify_seed.py` - cross-platform check: geometry, the container's own block count, volume UUID.
- `seed/seed-data-256m.dbrdisk` - the packed 256 MiB seed (73,840 B) that currently boots; it holds
  APFS metadata only, no user or copyrighted content.

## Known limits

- `resizeContainer` can only grow, never shrink.
- The resize keeps the volume UUID (`5a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9`), which is what the
  kernel matches on, so no kernel rebuild is implied by a grow.
