#!/usr/bin/env python
"""Generate one dcmqi metadata JSON per case.

Only segments whose label value is actually present in that case's label map are emitted -- 91 of the
129 cases contain no cyst, and declaring an empty segment would be wrong.

Output: data/meta/<case_id>.json

    uv run python -m rcc_aid.make_metadata
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from .terminology import BODY_PART_EXAMINED, SEGMENTS

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
META_DIR = ROOT / "data" / "meta"

# See docs/decisions.md ADR-008: model-initialised, human-corrected.
ALGORITHM_TYPE = "SEMIAUTOMATIC"
ALGORITHM_NAME = "Robust Kidney Abnormality Segmentation"

# Group attribution: two annotators (Ziegelmayer, Russo) worked across the dataset and per-case
# attribution is not published, so a dataset-level creator is the only honest value.
CONTENT_CREATOR_NAME = "RCC-AID"
COORDINATING_CENTER = "Radboud University Medical Center"

# SeriesNumber is derived from the source CT series: N -> N*100+1 for LABELMAP, N*100+2 for BINARY.
# This keeps a segmentation adjacent to the images it describes when a study is sorted by series
# number, and is unique within a study by construction -- N1*100+1 = N2*100+2 has no integer
# solution, and the source series numbers are themselves unique within a study. Verified
# collision-free over all 106 studies; largest value generated is 618602, well inside IS.
SERIES_NUMBER_OFFSET = {"labelmap": 1, "binary": 2}
# Only reachable if the source CT carries no SeriesNumber. Does not occur in this dataset.
SERIES_NUMBER_FALLBACK = {"labelmap": "300", "binary": "301"}


def series_number(row: pd.Series, flavour: str) -> str:
    """IS. Derived from the source CT SeriesNumber -- see the note on SERIES_NUMBER_OFFSET."""
    src = row.get("idc_SeriesNumber")
    if src is None or pd.isna(src) or str(src).strip() == "":
        return SERIES_NUMBER_FALLBACK[flavour]
    return str(int(src) * 100 + SERIES_NUMBER_OFFSET[flavour])


def series_description(row: pd.Series, flavour: str) -> str:
    """LO, 64 characters max.

    Names the source CT series explicitly. Contrast phase alone is not enough to tell two
    segmentations apart: 7 (study, phase) groups here cover more than one annotated series --
    typically a thick and a thin reconstruction of the same acquisition -- so 15 cases would
    otherwise produce descriptions identical to another segmentation in the same study,
    distinguishable only by ReferencedSeriesSequence, which no viewer series list shows.
    """
    phase = row.get("zen_phase_name")
    phase_txt = f", {phase}" if isinstance(phase, str) and phase else ""
    src = row.get("idc_SeriesNumber")
    src_txt = "" if src is None or pd.isna(src) else f", CT series {int(src)}"
    desc = f"RCC-AID kidney/mass/cyst seg{phase_txt}{src_txt}"
    if flavour == "binary":
        desc += " [BINARY]"
    # Raise rather than truncate: silently clipping to 64 could reproduce the very collision this
    # is meant to remove. Longest across the 129 cases is 62.
    if len(desc) > 64:
        raise ValueError(f"SeriesDescription exceeds the LO limit at {len(desc)} chars: {desc!r}")
    return desc


def build_metadata(row: pd.Series, flavour: str) -> dict:
    present = [int(v) for v in str(row["labels_present"]).split(",") if v != ""]
    missing = [i for i in sorted(SEGMENTS) if i not in present]

    segments = [
        SEGMENTS[i].to_dcmqi(ALGORITHM_TYPE, ALGORITHM_NAME) for i in sorted(present)
    ]
    if not segments:
        raise ValueError(f"{row['case_id']}: no labels present")

    return {
        "ContentCreatorName": CONTENT_CREATOR_NAME,
        "ClinicalTrialSeriesID": "1",
        "ClinicalTrialTimePointID": "1",
        "ClinicalTrialCoordinatingCenterName": COORDINATING_CENTER,
        "ContentLabel": "SEGMENTATION",
        "ContentDescription": "RCC-AID kidney, mass and cyst segmentation",
        "SeriesDescription": series_description(row, flavour),
        "SeriesNumber": series_number(row, flavour),
        "InstanceNumber": "1",
        "BodyPartExamined": BODY_PART_EXAMINED,
        # Outer list = one entry per --inputImageList file; inner = segments within that file.
        "segmentAttributes": [segments],
        "_rcc_aid": {  # provenance, ignored by dcmqi
            "case_id": row["case_id"],
            "labels_present": present,
            "labels_absent": missing,
            "source_SeriesInstanceUID": row["SeriesInstanceUID"],
            "rcc_subtype": row["rcc_subtype"],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids")
    args = ap.parse_args()

    df = pd.read_csv(CASE_INDEX)
    if args.cases:
        df = df[df["case_id"].isin(args.cases)]
        if df.empty:
            print("no matching cases", file=sys.stderr)
            return 1

    META_DIR.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for _, row in df.iterrows():
        for flavour in ("labelmap", "binary"):
            meta = build_metadata(row, flavour)
            suffix = "" if flavour == "labelmap" else ".binary"
            (META_DIR / f"{row['case_id']}{suffix}.json").write_text(
                json.dumps(meta, indent=2) + "\n"
            )
        key = row["labels_present"]
        counts[key] = counts.get(key, 0) + 1

    print(f"wrote {2 * len(df)} metadata files to {META_DIR.relative_to(ROOT)}")
    print(f"  ({len(df)} labelmap + {len(df)} binary)")
    print("\nsegment sets emitted:")
    for key in sorted(counts):
        names = ", ".join(SEGMENTS[int(i)].dicom_label for i in key.split(","))
        print(f"  labels {key} -> [{names}]  : {counts[key]} cases")
    print(f"\nSegmentAlgorithmType: {ALGORITHM_TYPE}")
    print(f"SegmentAlgorithmName: {ALGORITHM_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
