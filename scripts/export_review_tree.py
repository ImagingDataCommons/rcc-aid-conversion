#!/usr/bin/env python
"""Export converted segmentations into a human-navigable tree for review.

Layout is PatientID / Modality / SeriesName, with the source annotations alongside so a reviewer can
compare what was published against what was converted:

    <out>/<PatientID>/SEG/<SeriesNumber>-<SeriesDescription>/<case_id>.dcm
    <out>/<PatientID>/NIFTI/original-published/<case_id>_seg.nii.gz
    <out>/<PatientID>/NIFTI/normalized-for-conversion/<case_id>_seg.nrrd

Series names come from the DICOM headers themselves, not from our metadata, so the tree reflects what
a consumer of these objects would actually see.

    uv run python scripts/export_review_tree.py --out review/pilot --cases <case_id> ...
    uv run python scripts/export_review_tree.py --out review/all
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import pandas as pd
import pydicom

ROOT = Path(__file__).resolve().parents[1]
CONVERSION_CSV = ROOT / "qc" / "conversion_report.csv"
CASE_INDEX = ROOT / "qc" / "case_index.csv"
LABELS_RAW = ROOT / "data" / "labels"
LABELS_NORM = ROOT / "data" / "labels_norm"
OUT_DIRS = {"labelmap": ROOT / "data" / "seg_labelmap", "binary": ROOT / "data" / "seg_binary"}


def slug(text: str) -> str:
    """Filesystem-safe, readable directory component."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-")
    return s or "unnamed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output directory (created)")
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids")
    ap.add_argument("--no-source", action="store_true",
                    help="omit the original/normalized annotation files")
    args = ap.parse_args()

    conv = pd.read_csv(CONVERSION_CSV)
    conv = conv[conv["status"] == "ok"]
    idx = pd.read_csv(CASE_INDEX).set_index("case_id")
    cases = sorted(set(conv["case_id"]))
    if args.cases:
        cases = [c for c in cases if c in set(args.cases)]
        missing = set(args.cases) - set(cases)
        for m in sorted(missing):
            print(f"  skipping {m}: not successfully converted", file=sys.stderr)
    if not cases:
        print("nothing to export", file=sys.stderr)
        return 1

    out_root = Path(args.out)
    if not out_root.is_absolute():
        out_root = ROOT / out_root
    out_root.mkdir(parents=True, exist_ok=True)

    n_seg = n_src = 0
    for case_id in cases:
        patient = str(idx.loc[case_id, "idc_PatientID"])
        pdir = out_root / slug(patient)

        for flavour in ("labelmap", "binary"):
            src = OUT_DIRS[flavour] / f"{case_id}.dcm"
            if not src.exists():
                continue
            ds = pydicom.dcmread(str(src), stop_before_pixels=True)
            # Prefer the DICOM's own PatientID; fall back to the index if absent.
            pid = str(getattr(ds, "PatientID", "") or patient)
            series_dir = (
                out_root / slug(pid) / slug(ds.Modality)
                / f"{int(getattr(ds, 'SeriesNumber', 0) or 0)}-"
                  f"{slug(getattr(ds, 'SeriesDescription', flavour))}"
            )
            series_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, series_dir / f"{case_id}.dcm")
            n_seg += 1
            pdir = out_root / slug(pid)

        if not args.no_source:
            raw = LABELS_RAW / str(idx.loc[case_id, "label_file"])
            norm = LABELS_NORM / f"{case_id}_seg.nrrd"
            if raw.exists():
                d = pdir / "NIFTI" / "original-published"
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(raw, d / raw.name)
                n_src += 1
            if norm.exists():
                d = pdir / "NIFTI" / "normalized-for-conversion"
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(norm, d / norm.name)
                n_src += 1

    print(f"exported {n_seg} segmentation object(s) and {n_src} source annotation file(s) "
          f"for {len(cases)} case(s)")
    print(f"-> {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
