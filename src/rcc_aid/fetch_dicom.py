#!/usr/bin/env python
"""Download the source DICOM CT series for every RCC-AID case from IDC.

Downloads via idc-index (direct S3 transfer from IDC's public buckets), into a flat
series-keyed layout so `resolve_references.py` can find a series by UID:

    data/dicom/<SeriesInstanceUID>/*.dcm

    uv run python -m rcc_aid.fetch_dicom            # download
    uv run python -m rcc_aid.fetch_dicom --dry-run  # size only, no transfer
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from idc_index import IDCClient

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
DICOM_DIR = ROOT / "data" / "dicom"

# Flat, series-keyed. The collection/patient/study hierarchy is already in qc/case_index.csv.
DIR_TEMPLATE = "%SeriesInstanceUID"


def already_complete(df: pd.DataFrame) -> pd.DataFrame:
    """Rows whose series directory already holds the expected number of instances."""
    counts = []
    for uid, expected in zip(df["SeriesInstanceUID"], df["idc_instanceCount"]):
        d = DICOM_DIR / uid
        n = len(list(d.glob("*.dcm"))) if d.is_dir() else 0
        counts.append((n, int(n) == int(expected)))
    df = df.copy()
    df["files_on_disk"] = [c[0] for c in counts]
    df["complete"] = [c[1] for c in counts]
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report size, do not transfer")
    ap.add_argument("--force", action="store_true", help="re-download series already complete")
    args = ap.parse_args()

    if not CASE_INDEX.exists():
        print(f"missing {CASE_INDEX}; run rcc_aid.build_case_index first", file=sys.stderr)
        return 1
    df = pd.read_csv(CASE_INDEX)
    uids = sorted(set(df["SeriesInstanceUID"]))

    # One row per series (a series maps 1:1 to a case in this dataset, but do not rely on it).
    series = df.drop_duplicates("SeriesInstanceUID")[
        ["SeriesInstanceUID", "idc_instanceCount", "idc_series_size_MB"]
    ]
    series = already_complete(series)

    total_gb = series["idc_series_size_MB"].sum() / 1024
    todo = series if args.force else series[~series["complete"]]
    todo_gb = todo["idc_series_size_MB"].sum() / 1024

    print(f"{len(uids)} series, {total_gb:.2f} GB total")
    print(f"already complete on disk: {int(series['complete'].sum())}")
    print(f"to download: {len(todo)} series, {todo_gb:.2f} GB")

    partial = series[(~series["complete"]) & (series["files_on_disk"] > 0)]
    if len(partial):
        print(f"  ({len(partial)} partially downloaded; s5cmd sync will fill the gaps)")

    if args.dry_run:
        return 0
    if todo.empty:
        print("nothing to do")
        return 0

    DICOM_DIR.mkdir(parents=True, exist_ok=True)
    client = IDCClient()
    client.download_from_selection(
        downloadDir=str(DICOM_DIR),
        seriesInstanceUID=list(todo["SeriesInstanceUID"]),
        dirTemplate=DIR_TEMPLATE,
        use_s5cmd_sync=True,
        show_progress_bar=True,
        quiet=False,
    )

    after = already_complete(series)
    n_ok = int(after["complete"].sum())
    print(f"\ncomplete: {n_ok}/{len(after)} series")
    incomplete = after[~after["complete"]]
    if len(incomplete):
        print(f"INCOMPLETE ({len(incomplete)}):", file=sys.stderr)
        for _, r in incomplete.iterrows():
            print(f"  {r['SeriesInstanceUID']}  {r['files_on_disk']}/"
                  f"{int(r['idc_instanceCount'])} instances", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
