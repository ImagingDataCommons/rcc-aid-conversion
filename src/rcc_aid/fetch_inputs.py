#!/usr/bin/env python
"""Fetch the RCC-AID annotation files from Zenodo and verify them against the record's checksums.

Downloads labels.zip, metadata.csv and manifest.tcia, and deliberately NOT images.zip -- see
docs/decisions.md ADR-002. Source imaging comes from IDC as DICOM via rcc_aid.fetch_dicom.

    uv run python -m rcc_aid.fetch_inputs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ZENODO_DIR = ROOT / "data" / "zenodo"
LABELS_DIR = ROOT / "data" / "labels"

RECORD_ID = "20719257"
RECORD_DOI = "10.5281/zenodo.20719257"
API_URL = f"https://zenodo.org/api/records/{RECORD_ID}"

# md5 as published in the record. Verified 2026-08-21.
WANTED = {
    "labels.zip": "6d453071037640a79042033723442c1a",
    "metadata.csv": "7297e5ea8201eddb0c3aae55c1068952",
    "manifest.tcia": "adfa32c16ef9287711cab0702083f3e7",
}
# 4.1 GB of NIfTI copies of imaging IDC already serves as DICOM.
SKIPPED = {"images.zip"}

EXPECTED_LABEL_FILES = 129


def md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as r, dest.open("wb") as out:
        while block := r.read(1 << 20):
            out.write(block)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if checksums match")
    args = ap.parse_args()

    print(f"Zenodo record {RECORD_ID} (doi:{RECORD_DOI})")
    with urllib.request.urlopen(API_URL) as r:
        record = json.load(r)

    published = {
        f["key"]: (f["checksum"].split(":", 1)[-1], f["size"], f["links"]["self"])
        for f in record.get("files", [])
    }
    print(f"record version {record['metadata'].get('version')}, "
          f"{len(published)} file(s) published")

    for name in sorted(SKIPPED & published.keys()):
        print(f"  skipping {name} ({published[name][1] / 1e9:.1f} GB) -- ADR-002")

    failures = []
    for name, want_md5 in WANTED.items():
        if name not in published:
            failures.append(f"{name}: not present in the record any more")
            continue
        record_md5, size, url = published[name]
        if record_md5 != want_md5:
            failures.append(
                f"{name}: record checksum {record_md5} != pinned {want_md5} "
                f"-- the record changed; review before trusting the data"
            )
            continue

        dest = ZENODO_DIR / name
        if dest.exists() and not args.force and md5(dest) == want_md5:
            print(f"  ok (cached) {name}  {size:,} bytes")
            continue
        print(f"  downloading {name}  {size:,} bytes")
        fetch(url, dest)
        got = md5(dest)
        if got != want_md5:
            failures.append(f"{name}: downloaded md5 {got} != {want_md5}")
        else:
            print(f"  ok          {name}  md5 {got}")

    if failures:
        print("\nfailures:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1

    # unpack labels.zip -> data/labels/*.nii.gz (the archive has a single top-level labels/ dir)
    LABELS_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZENODO_DIR / "labels.zip") as z:
        members = [m for m in z.namelist() if m.endswith(".nii.gz")]
        for m in members:
            target = LABELS_DIR / Path(m).name
            if not target.exists():
                with z.open(m) as src, target.open("wb") as out:
                    out.write(src.read())
    n = len(list(LABELS_DIR.glob("*.nii.gz")))
    print(f"\nunpacked {n} label maps to {LABELS_DIR.relative_to(ROOT)}")

    n_manifest = sum(
        1 for ln in (ZENODO_DIR / "manifest.tcia").read_text().splitlines()
        if ln.strip().startswith("1.3.6")
    )
    print(f"manifest lists {n_manifest} SeriesInstanceUID(s)")

    if n != EXPECTED_LABEL_FILES or n_manifest != EXPECTED_LABEL_FILES:
        print(f"WARNING expected {EXPECTED_LABEL_FILES} label maps and manifest UIDs; "
              f"got {n} and {n_manifest}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
