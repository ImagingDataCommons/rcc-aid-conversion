#!/usr/bin/env python
"""Export converted segmentations into a human-navigable tree for review.

Layout is PatientID / Modality / SeriesName, with the source annotations alongside so a reviewer can
compare what was published against what was converted:

    <out>/<PatientID>/SEG/<SeriesNumber>-<SeriesDescription>/<case_id>.dcm
    <out>/<PatientID>/CT/<SeriesNumber>-<SeriesDescription>/<instance>.dcm     (--ct)
    <out>/<PatientID>/NIFTI/original-published/<case_id>_seg.nii.gz
    <out>/<PatientID>/NIFTI/normalized-for-conversion/<case_id>_seg.nrrd

Series names come from the DICOM headers themselves, not from our metadata, so the tree reflects what
a consumer of these objects would actually see.

With --ct the segmented CT is copied alongside, making a patient folder self-contained enough to load
in 3D Slicer and check that the segmentation actually overlays the images. It is off by default
because the CT dwarfs everything else: ~284 MB for the three pilot cases, ~8 GB for all 129.

    uv run python scripts/export_review_tree.py --out review/pilot --cases <case_id> ...
    uv run python scripts/export_review_tree.py --out review/pilot --ct series --cases <case_id> ...
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
DICOM_DIR = ROOT / "data" / "dicom"
REFS_DIR = ROOT / "data" / "refs"
OUT_DIRS = {"labelmap": ROOT / "data" / "seg_labelmap", "binary": ROOT / "data" / "seg_binary"}


def slug(text: str) -> str:
    """Filesystem-safe, readable directory component."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text)).strip("-")
    return s or "unnamed"


def repaired_instances(case_id: str) -> dict[str, Path]:
    """Sanitized replacements for this case's reference instances, keyed by filename.

    For the cases whose source DICOM needed repair, data/refs/<case>.txt points into
    data/dicom_sanitized/<case_id>/ -- which holds only the *referenced* instances, a subset of the
    series for multi-acquisition cases. Exporting a full series from data/dicom/ would therefore hand
    the reviewer instances that differ from the ones dcmqi actually consumed, so substitute them back
    in. sanitize_dicom.py preserves the filename, which is what makes matching by name safe.
    """
    ref_list = REFS_DIR / f"{case_id}.txt"
    if not ref_list.exists():
        return {}
    return {
        Path(line).name: Path(line)
        for line in ref_list.read_text().split()
        if "dicom_sanitized" in Path(line).parts
    }


def export_ct(case_id: str, series_uid: str, mode: str, out_root: Path) -> tuple[int, int]:
    """Copy the segmented CT into <out>/<PatientID>/CT/<SeriesNumber>-<SeriesDescription>/.

    mode 'series' takes the whole DICOM series as IDC holds it; 'referenced' takes only the instances
    the segmentation references. Returns (instances copied, sanitized substitutions).
    """
    if mode == "referenced":
        sources = [Path(p) for p in (REFS_DIR / f"{case_id}.txt").read_text().split()]
        repaired: dict[str, Path] = {}
    else:
        series_dir = DICOM_DIR / series_uid
        if not series_dir.is_dir():
            print(f"  {case_id}: no source DICOM at {series_dir}", file=sys.stderr)
            return 0, 0
        sources = sorted(series_dir.glob("*.dcm"))
        repaired = repaired_instances(case_id)

    if not sources:
        print(f"  {case_id}: no CT instances found", file=sys.stderr)
        return 0, 0

    # Name the leaf from the CT's own headers, as the SEG leaves are named from theirs.
    ds = pydicom.dcmread(str(sources[0]), stop_before_pixels=True)
    leaf = (
        out_root / slug(ds.PatientID) / slug(ds.Modality)
        / f"{int(getattr(ds, 'SeriesNumber', 0) or 0)}-"
          f"{slug(getattr(ds, 'SeriesDescription', series_uid))}"
    )
    leaf.mkdir(parents=True, exist_ok=True)

    n_sub = 0
    for src in sources:
        use = repaired.get(src.name, src)
        if use is not src:
            n_sub += 1
        shutil.copy2(use, leaf / src.name)
    return len(sources), n_sub


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output directory (created)")
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids")
    ap.add_argument("--no-source", action="store_true",
                    help="omit the original/normalized annotation files")
    ap.add_argument("--ct", choices=("none", "series", "referenced"), default="none",
                    help="also copy the segmented CT: 'series' = the whole DICOM series as IDC holds "
                         "it, 'referenced' = only the instances the segmentation references "
                         "(default: none -- the CT is ~284 MB for 3 cases, ~8 GB for all 129)")
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

    n_seg = n_src = n_ct = n_sub = 0
    for case_id in cases:
        patient = str(idx.loc[case_id, "idc_PatientID"])
        pdir = out_root / slug(patient)

        if args.ct != "none":
            copied, substituted = export_ct(
                case_id, str(idx.loc[case_id, "SeriesInstanceUID"]), args.ct, out_root
            )
            n_ct += copied
            n_sub += substituted

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
    if args.ct != "none":
        print(f"exported {n_ct} CT instance(s) [--ct {args.ct}]"
              + (f", {n_sub} of them the sanitized copy" if n_sub else ""))
    print(f"-> {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
