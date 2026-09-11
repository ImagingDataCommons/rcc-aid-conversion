#!/usr/bin/env python
"""Build the IDC submitter instance manifest for the LABELMAP segmentations.

The manifest is the authoritative statement of what this IDC release adds: one row per DICOM
instance, naming the file, its hash, and the source file it was converted from. Its spec, columns
and 22 validation rules live in idc-comet's `docs/submitter-manifest.md`.

Two buckets, both organized under a per-release folder so a later release adds a sibling:

    gs://rcc-aid/v25/rcc_aid-v25.csv                      this manifest
    gs://rcc-aid/v25/<collection_id>/<PatientID>/<StudyInstanceUID>/
                     <SeriesInstanceUID>/<SOPInstanceUID>.dcm
    gs://rcc-aid-src/v25/labels/<case_id>_seg.nii.gz      the published RCC-AID annotations
    gs://rcc-aid-src/v25/labels_norm/<case_id>_seg.nrrd   what dcmqi actually read (ADR-011)
    gs://rcc-aid-src/v25/{labels.zip,metadata.csv,manifest.tcia}

`collection_id` leads the segmentation hierarchy because an analysis result spans three collections
here, and that column of the manifest names the collection *analyzed* -- so the layout and the
manifest agree on which collection an instance belongs to. The manifest must sit in the top-level
folder of what it describes (rule 16), which is what makes every `relative_gcs_url` a `./` path
below it.

`source_doi` is taken from `rcc_aid.clinical_trial.DATASET_DOI` rather than restated, and every
object is checked to carry that same DOI in OtherClinicalTrialProtocolIDsSequence before it is
written into a row: the manifest row and the instance it points at must identify the dataset the
same way (ADR-013).

    uv run python scripts/make_submitter_manifest.py                    # write the manifest
    uv run python scripts/make_submitter_manifest.py --stage <dir>      # + the tree to upload
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from pathlib import Path

import pandas as pd
import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rcc_aid.clinical_trial import DATASET_DOI, stamped_doi  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
SEG_DIR = ROOT / "data" / "seg_labelmap"
LABELS_RAW = ROOT / "data" / "labels"

IDC_VERSION = "v25"
DATA_BUCKET = "gs://rcc-aid"
SOURCE_BUCKET = "gs://rcc-aid-src"
ANALYSIS_RESULT_ID = "rcc_aid"

# The columns idc-comet's validate_submitter_manifest.py expects, in the order it documents them.
COLUMNS = [
    "collection_id",
    "source_doi",
    "SOPInstanceUID",
    "relative_gcs_url",
    "crdc_instance_uuid",
    "operation",
    "instance_hash",
    "source_file_url",
    "source_file_hash",
]


def md5(path: Path) -> str:
    """Lowercase hex MD5 -- the only form the manifest accepts (never the API's base64)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rows(seg_dir: Path) -> list[dict]:
    cases = pd.read_csv(CASE_INDEX).set_index("case_id")
    out = []

    for dcm in sorted(seg_dir.glob("*.dcm")):
        case_id = dcm.stem
        if case_id not in cases.index:
            raise SystemExit(f"{dcm.name}: no row in {CASE_INDEX.relative_to(ROOT)}")
        case = cases.loc[case_id]

        doi = stamped_doi(dcm)
        if doi != DATASET_DOI:
            raise SystemExit(
                f"{dcm.name}: carries dataset DOI {doi!r}, manifest declares {DATASET_DOI!r}. "
                "Re-run scripts/patch_clinical_trial.py before building the manifest."
            )

        ds = pydicom.dcmread(str(dcm), stop_before_pixels=True)
        label = LABELS_RAW / case["label_file"]
        if not label.exists():
            raise SystemExit(f"{dcm.name}: source annotation {label} is missing")

        relative = "/".join([
            ".",
            case["idc_collection_id"],
            ds.PatientID,
            ds.StudyInstanceUID,
            ds.SeriesInstanceUID,
            f"{ds.SOPInstanceUID}.dcm",
        ])
        out.append({
            "collection_id": case["idc_collection_id"],
            "source_doi": DATASET_DOI,
            "SOPInstanceUID": ds.SOPInstanceUID,
            "relative_gcs_url": relative,
            "crdc_instance_uuid": "",          # additions have no UUID yet
            "operation": "addition",
            "instance_hash": md5(dcm),
            "source_file_url": f"{SOURCE_BUCKET}/{IDC_VERSION}/labels/{case['label_file']}",
            "source_file_hash": md5(label),
        })

    if not out:
        raise SystemExit(f"no .dcm found in {seg_dir}")
    if len({r["SOPInstanceUID"] for r in out}) != len(out):
        raise SystemExit("duplicate SOPInstanceUID across the objects (rule 20)")
    return out


def stage(manifest_rows: list[dict], seg_dir: Path, dest: Path) -> None:
    """Lay the objects out under <dest>/<IDC_VERSION>/ exactly as the manifest addresses them."""
    root = dest / IDC_VERSION
    for row, dcm in zip(manifest_rows, sorted(seg_dir.glob("*.dcm"))):
        out = root / row["relative_gcs_url"][2:]
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dcm, out)
    print(f"staged {len(manifest_rows)} objects under {root}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path,
                    default=ROOT / "submission" / f"{ANALYSIS_RESULT_ID}-{IDC_VERSION}.csv",
                    help="where to write the manifest (default submission/rcc_aid-v25.csv)")
    ap.add_argument("--seg-dir", type=Path, default=SEG_DIR,
                    help=f"directory of LABELMAP objects (default {SEG_DIR.relative_to(ROOT)})")
    ap.add_argument("--stage", type=Path,
                    help="also write the upload tree here, ready for `gcloud storage rsync`")
    args = ap.parse_args()

    manifest_rows = rows(args.seg_dir)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest_rows)

    by_collection = pd.Series([r["collection_id"] for r in manifest_rows]).value_counts()
    print(f"{args.out.relative_to(ROOT)}: {len(manifest_rows)} additions, source_doi {DATASET_DOI}")
    for collection, n in by_collection.sort_index().items():
        print(f"  {collection:<12} {n}")
    print(f"  manifest md5 {md5(args.out)}")

    if args.stage:
        stage(manifest_rows, args.seg_dir, args.stage)

    print(f"\nupload:\n  gcloud storage cp {args.out} "
          f"{DATA_BUCKET}/{IDC_VERSION}/{args.out.name}")


if __name__ == "__main__":
    main()
