#!/usr/bin/env python
"""Build the case index: one row per RCC-AID label file, with everything needed downstream.

Parses the label filenames (which embed the SeriesInstanceUID), pulls all imaging metadata from
idc-index -- including the geometry characterisation in `volume_geometry_index`, which tells us up
front which series are not regularly-spaced 3D volumes -- and reads each NIfTI for its grid and
per-label voxel counts.

Output: qc/case_index.csv -- the provenance backbone for the whole pipeline.

    uv run python -m rcc_aid.build_case_index
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import SimpleITK as sitk
from idc_index import IDCClient

from .terminology import COHORTS, SEGMENTS

ROOT = Path(__file__).resolve().parents[2]
LABELS_DIR = ROOT / "data" / "labels"
METADATA_CSV = ROOT / "data" / "zenodo" / "metadata.csv"
MANIFEST = ROOT / "data" / "zenodo" / "manifest.tcia"
OUT_CSV = ROOT / "qc" / "case_index.csv"

# Optional idc-index tables this pipeline relies on.
IDC_INDICES = ["volume_geometry_index", "ct_index", "contrast_index"]

# KIRC_1_3_6_1_..._acq_12_seg.nii.gz  /  ..._acq_unknown_seg.nii.gz
FILENAME_RE = re.compile(
    r"^(?P<cohort>KIRC|KIRP|KICH)_(?P<uid_us>[0-9_]+?)_acq_(?P<acq>\d+|unknown)_seg\.nii\.gz$"
)


def parse_label_filename(name: str) -> dict:
    m = FILENAME_RE.match(name)
    if not m:
        raise ValueError(f"unparseable label filename: {name}")
    return {
        "case_id": name[: -len("_seg.nii.gz")],
        "label_file": name,
        "cohort": m.group("cohort"),
        "SeriesInstanceUID": m.group("uid_us").replace("_", "."),
        "acquisition_number": m.group("acq"),
    }


def read_label_geometry(path: Path) -> dict:
    """Grid parameters and per-label voxel counts for one NIfTI label map."""
    img = sitk.ReadImage(str(path))
    arr = sitk.GetArrayViewFromImage(img)  # (z, y, x)
    # Round, do not truncate: 13 files are float32 and 10 of those store label 1 as
    # 1.0000152587890625 (see rcc_aid.normalize_labels). int() happens to work for values slightly
    # above the integer but would silently misattribute anything slightly below it.
    values, counts_arr = np.unique(np.round(arr), return_counts=True)
    counts = {int(v): int(c) for v, c in zip(values, counts_arr)}

    out = {
        "label_size": "x".join(str(s) for s in img.GetSize()),
        "label_spacing": ",".join(f"{s:.6g}" for s in img.GetSpacing()),
        "label_origin": ",".join(f"{s:.6g}" for s in img.GetOrigin()),
        "label_direction": ",".join(f"{s:.6g}" for s in img.GetDirection()),
        "label_pixel_type": img.GetPixelIDTypeAsString(),
        "label_n_slices": img.GetSize()[2],
        "unexpected_label_values": ",".join(
            str(v) for v in sorted(counts) if v != 0 and v not in SEGMENTS
        ),
    }
    for label_id, seg in sorted(SEGMENTS.items()):
        out[f"voxels_{seg.name.lower()}"] = counts.get(label_id, 0)
    out["labels_present"] = ",".join(str(i) for i in sorted(SEGMENTS) if counts.get(i, 0) > 0)
    return out


def query_idc(uids: list[str]) -> pd.DataFrame:
    """All imaging metadata for the given series, joined inside idc-index's own SQL layer."""
    client = IDCClient()
    for name in IDC_INDICES:
        client.fetch_index(name)
    print(f"idc-index {client.get_idc_version()}; loaded {', '.join(IDC_INDICES)}")

    uid_list = ",".join(f"'{u}'" for u in uids)
    return client.sql_query(f"""
        SELECT
            i.SeriesInstanceUID,
            i.collection_id            AS idc_collection_id,
            i.PatientID                AS idc_PatientID,
            i.StudyInstanceUID         AS idc_StudyInstanceUID,
            i.Modality                 AS idc_Modality,
            i.SeriesDescription        AS idc_SeriesDescription,
            i.SeriesNumber             AS idc_SeriesNumber,
            i.BodyPartExamined         AS idc_BodyPartExamined,
            i.Manufacturer             AS idc_Manufacturer,
            i.ManufacturerModelName    AS idc_ManufacturerModelName,
            i.PatientSex               AS idc_PatientSex,
            i.PatientAge               AS idc_PatientAge,
            i.instanceCount            AS idc_instanceCount,
            i.series_size_MB           AS idc_series_size_MB,
            i.license_short_name       AS idc_license,
            i.source_DOI               AS idc_source_DOI,
            i.series_aws_url           AS idc_series_aws_url,
            ct.Rows                    AS ct_rows,
            ct.Columns                 AS ct_columns,
            ct.SliceThickness          AS ct_slice_thickness,
            ct.PixelSpacing_row_mm     AS ct_pixel_spacing_row,
            ct.ImageType               AS ct_image_type,
            cb.ContrastBolusAgent      AS contrast_agent,
            v.regularly_spaced_3d_volume AS geom_regular,
            v.single_orientation         AS geom_single_orientation,
            v.unique_slice_positions     AS geom_unique_slice_positions,
            v.uniform_slice_spacing      AS geom_uniform_slice_spacing,
            v.consistent_image_dimensions AS geom_consistent_dims,
            v.consistent_pixel_spacing   AS geom_consistent_pixel_spacing,
            v.obliquity_degrees          AS geom_obliquity_degrees
        FROM index i
        LEFT JOIN ct_index ct              USING (SeriesInstanceUID)
        LEFT JOIN contrast_index cb        USING (SeriesInstanceUID)
        LEFT JOIN volume_geometry_index v  USING (SeriesInstanceUID)
        WHERE i.SeriesInstanceUID IN ({uid_list})
    """)


def main() -> int:
    label_files = sorted(p.name for p in LABELS_DIR.glob("*.nii.gz"))
    if not label_files:
        print(f"no label files in {LABELS_DIR}", file=sys.stderr)
        return 1
    df = pd.DataFrame([parse_label_filename(n) for n in label_files])
    print(f"parsed {len(df)} label filenames")

    # --- cross-check against the TCIA manifest shipped with the Zenodo record ---
    manifest_uids = {
        ln.strip() for ln in MANIFEST.read_text().splitlines() if ln.strip().startswith("1.3.6")
    }
    label_uids = set(df["SeriesInstanceUID"])
    if label_uids == manifest_uids:
        print(f"manifest cross-check OK: {len(manifest_uids)} UIDs match exactly")
    else:
        print(f"WARNING manifest mismatch: {len(label_uids - manifest_uids)} label-only, "
              f"{len(manifest_uids - label_uids)} manifest-only", file=sys.stderr)
        for u in sorted(label_uids - manifest_uids):
            print(f"  label-only    {u}", file=sys.stderr)
        for u in sorted(manifest_uids - label_uids):
            print(f"  manifest-only {u}", file=sys.stderr)
    df["in_manifest"] = df["SeriesInstanceUID"].isin(manifest_uids)

    # --- Zenodo metadata.csv: only what IDC does not already carry (the contrast-phase call) ---
    meta = pd.read_csv(METADATA_CSV, dtype=str)
    keep = ["SeriesInstanceUID", "phase_name", "Site", "StudyDate", "LicenseName",
            "DataDescriptionURI"]
    keep = [c for c in keep if c in meta.columns]
    meta = meta[keep].drop_duplicates("SeriesInstanceUID").rename(
        columns={c: f"zen_{c}" for c in keep if c != "SeriesInstanceUID"}
    )
    df = df.merge(meta, on="SeriesInstanceUID", how="left")
    n_phase = df["zen_phase_name"].notna().sum() if "zen_phase_name" in df else 0
    print(f"metadata.csv join: {n_phase}/{len(df)} cases have a contrast phase")

    # --- idc-index ---
    idc = query_idc(sorted(label_uids))
    df = df.merge(idc, on="SeriesInstanceUID", how="left")
    n_in_idc = df["idc_collection_id"].notna().sum()
    print(f"IDC join: {n_in_idc}/{len(df)} series found")
    for u in df.loc[df["idc_collection_id"].isna(), "SeriesInstanceUID"]:
        print(f"  NOT IN IDC  {u}", file=sys.stderr)

    # cohort prefix must agree with the IDC collection it resolved to
    df["expected_collection_id"] = df["cohort"].map(lambda c: COHORTS[c][0])
    df["rcc_subtype"] = df["cohort"].map(lambda c: COHORTS[c][1])
    bad = df[df["idc_collection_id"].notna()
             & (df["idc_collection_id"] != df["expected_collection_id"])]
    if len(bad):
        print(f"WARNING {len(bad)} cases: cohort prefix disagrees with IDC collection_id",
              file=sys.stderr)
        for _, r in bad.iterrows():
            print(f"  {r['case_id']}: prefix->{r['expected_collection_id']} "
                  f"idc->{r['idc_collection_id']}", file=sys.stderr)
    else:
        print("cohort prefix vs IDC collection_id: all consistent")

    # --- read every label map ---
    geom = pd.DataFrame([read_label_geometry(LABELS_DIR / n) for n in df["label_file"]],
                        index=df.index)
    df = pd.concat([df, geom], axis=1)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    # ------------------------------------------------------------------ summary
    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}  ({len(df)} rows, {len(df.columns)} cols)")
    print("\ncohort / subtype:")
    print(df.groupby(["cohort", "rcc_subtype"]).size().to_string())
    print(f"\npatients: {df['idc_PatientID'].nunique()}")
    print("\nlabels present (1=Kidney 2=Tumor 3=Cyst):")
    print(df["labels_present"].value_counts().to_string())
    if "zen_phase_name" in df:
        print("\ncontrast phase:")
        print(df["zen_phase_name"].value_counts(dropna=False).to_string())
    print(f"\nacq_unknown cases: {(df['acquisition_number'] == 'unknown').sum()}")
    print("\nIDC volume geometry (series level):")
    print(f"  regularly_spaced_3d_volume=True : {(df['geom_regular'] == True).sum()}")   # noqa: E712
    print(f"  regularly_spaced_3d_volume=False: {(df['geom_regular'] == False).sum()}")  # noqa: E712
    irregular = df[df["geom_regular"] == False]  # noqa: E712
    if len(irregular):
        print("  failure modes among the irregular series:")
        print(irregular.groupby(
            ["geom_single_orientation", "geom_unique_slice_positions",
             "geom_uniform_slice_spacing", "geom_consistent_dims"]
        ).size().to_string())
        print(f"  oblique (>0.01 deg): {(irregular['geom_obliquity_degrees'] > 0.01).sum()}")
    print(f"\nDICOM to download: {df['idc_series_size_MB'].sum() / 1024:.2f} GB, "
          f"{int(df['idc_instanceCount'].sum())} instances, "
          f"{df['SeriesInstanceUID'].nunique()} series")

    unexpected = df[df["unexpected_label_values"] != ""]
    if len(unexpected):
        print(f"\nWARNING {len(unexpected)} cases with label values outside 0-3:", file=sys.stderr)
        for _, r in unexpected.iterrows():
            print(f"  {r['case_id']}: {r['unexpected_label_values']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
