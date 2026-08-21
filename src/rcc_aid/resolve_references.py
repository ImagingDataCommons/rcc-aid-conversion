#!/usr/bin/env python
"""Resolve each RCC-AID label file to the exact ordered DICOM instances that produced its grid.

Why this exists
---------------
The label filenames carry an `_acq_{N|unknown}` token because upstream `dcm2nii.py` split each DICOM
series by AcquisitionNumber before writing NIfTI. These TCGA series bundle several contrast phases into
one series -- 21 of the 129 fail IDC's `regularly_spaced_3d_volume` check. A label map therefore
describes a *subset* of its series' instances, so pointing dcmqi at the whole series directory would
emit per-frame references to instances the annotation does not describe.

This step also detects the slice ordering of each label volume. 13 of the 129 label maps (all KIRC,
all from irregular multi-acquisition series) are stored in *descending* DICOM z order while still
declaring a +z direction cosine -- so their declared geometry places the volume outside the imaged
range. See docs/conversion-notes.md. `normalize_labels.py` corrects them; this step decides which.

The reference list is always written in ascending order along the slice normal, which is the
convention `normalize_labels.py` stamps onto the label maps.

    uv run python -m rcc_aid.resolve_references
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pydicom
import SimpleITK as sitk

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
LABELS_DIR = ROOT / "data" / "labels"
DICOM_DIR = ROOT / "data" / "dicom"
REFS_DIR = ROOT / "data" / "refs"
OUT_CSV = ROOT / "qc" / "reference_resolution.csv"

# DICOM stores positions as decimal strings; NIfTI stores the affine as float32, which for coordinates
# of a few hundred mm gives ~1e-4 mm of representation error. Observed worst case: 3.0e-5 mm.
TOL_ORIGIN_MM = 1e-2
TOL_SPACING_MM = 1e-3
TOL_DIRECTION = 1e-4


def read_series_headers(series_dir: Path) -> pd.DataFrame:
    """One row per instance, headers only (no pixel data)."""
    rows = []
    for f in sorted(series_dir.glob("*.dcm")):
        ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        if not hasattr(ds, "ImagePositionPatient") or not hasattr(ds, "ImageOrientationPatient"):
            continue
        acq = getattr(ds, "AcquisitionNumber", None)
        rows.append({
            "file": str(f),
            "SOPInstanceUID": str(getattr(ds, "SOPInstanceUID", "")),
            "acq": "unknown" if acq is None or acq == "" else str(int(acq)),
            "ipp": np.array([float(x) for x in ds.ImagePositionPatient]),
            "iop": np.array([float(x) for x in ds.ImageOrientationPatient]),
            "rows": int(ds.Rows),
            "cols": int(ds.Columns),
            "pixel_spacing": tuple(float(x) for x in (ds.PixelSpacing or (1.0, 1.0))),
        })
    return pd.DataFrame(rows)


def ascending_grid(inst: pd.DataFrame) -> dict:
    """The grid a SimpleITK ImageSeriesReader builds from these instances in ascending order."""
    iop = inst.iloc[0]["iop"]
    row_cos, col_cos = iop[:3], iop[3:]
    normal = np.cross(row_cos, col_cos)

    proj = np.array([float(np.dot(p, normal)) for p in inst["ipp"]])
    order = np.argsort(proj)
    inst = inst.iloc[order].reset_index(drop=True)
    proj = proj[order]

    gaps = np.diff(proj)
    ps = inst.iloc[0]["pixel_spacing"]
    n = len(inst)
    z_spacing = abs(float(np.median(gaps))) if n > 1 else 1.0

    return {
        "instances": inst,
        "size": (int(inst.iloc[0]["cols"]), int(inst.iloc[0]["rows"]), n),
        # ITK reports spacing as (x=column direction, y=row direction); DICOM PixelSpacing is
        # (row spacing, column spacing), hence the swap.
        "spacing": (float(ps[1]), float(ps[0]), z_spacing),
        "origin_first": tuple(float(v) for v in inst.iloc[0]["ipp"]),
        "origin_last": tuple(float(v) for v in inst.iloc[-1]["ipp"]),
        # ITK direction is column-major: columns are (row_cos, col_cos, normal).
        "direction": tuple(float(v) for v in np.column_stack([row_cos, col_cos, normal]).flatten()),
        "gap_min": float(gaps.min()) if n > 1 else float("nan"),
        "gap_max": float(gaps.max()) if n > 1 else float("nan"),
        "gap_median": float(np.median(gaps)) if n > 1 else float("nan"),
        "n_duplicate_positions": int(n - len(np.unique(np.round(proj, 4)))),
    }


def label_grid(path: Path) -> dict:
    img = sitk.ReadImage(str(path))
    return {
        "size": tuple(int(v) for v in img.GetSize()),
        "origin": tuple(float(v) for v in img.GetOrigin()),
        "spacing": tuple(float(v) for v in img.GetSpacing()),
        "direction": tuple(float(v) for v in img.GetDirection()),
    }


def evaluate(cand: dict, lab: dict) -> dict:
    """Does this instance subset explain the label grid, and in which slice order?"""
    size_ok = tuple(cand["size"]) == tuple(lab["size"])
    d_inplane = float(np.max(np.abs(np.array(cand["spacing"][:2]) - np.array(lab["spacing"][:2]))))
    # In-plane direction cosines must agree; the third column is what the 13 reversed cases get wrong,
    # so it is judged via the origin instead.
    d_dir_inplane = float(np.max(np.abs(
        np.array(cand["direction"][:6]) - np.array(lab["direction"][:6]))))

    d_asc = float(np.max(np.abs(np.array(cand["origin_first"]) - np.array(lab["origin"]))))
    d_desc = float(np.max(np.abs(np.array(cand["origin_last"]) - np.array(lab["origin"]))))
    order = "asc" if d_asc <= d_desc else "desc"
    d_origin = min(d_asc, d_desc)

    return {
        "size_match": size_ok,
        "slice_order": order,
        "d_origin_mm": d_origin,
        "d_origin_asc_mm": d_asc,
        "d_origin_desc_mm": d_desc,
        "d_spacing_inplane_mm": d_inplane,
        "d_direction_inplane": d_dir_inplane,
        "d_spacing_z_mm": float(abs(cand["spacing"][2] - lab["spacing"][2])),
        "match": bool(size_ok
                      and d_origin <= TOL_ORIGIN_MM
                      and d_inplane <= TOL_SPACING_MM
                      and d_dir_inplane <= TOL_DIRECTION),
    }


def candidate_groups(hdr: pd.DataFrame, acq_token: str) -> list[tuple[str, pd.DataFrame]]:
    """Candidate instance subsets, best guess first."""
    out: list[tuple[str, pd.DataFrame]] = []

    def add(sub: pd.DataFrame, tag: str) -> None:
        if sub.empty:
            return
        # Upstream dropped instances whose Rows/Columns differ from the group's modal value.
        dims = Counter(zip(sub["rows"], sub["cols"]))
        modal = dims.most_common(1)[0][0]
        keep = sub[(sub["rows"] == modal[0]) & (sub["cols"] == modal[1])]
        out.append((tag, keep))
        if len(keep) != len(sub):
            out.append((f"{tag}+all_dims", sub))

    add(hdr[hdr["acq"] == acq_token], f"acq={acq_token}")       # the filename's group
    add(hdr, "all_instances")                                    # documented fallbacks
    for a in sorted(hdr["acq"].unique()):
        if a != acq_token:
            add(hdr[hdr["acq"] == a], f"acq={a}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids (pilot)")
    args = ap.parse_args()

    df = pd.read_csv(CASE_INDEX)
    if args.cases:
        df = df[df["case_id"].isin(args.cases)]
        if df.empty:
            print("no matching cases", file=sys.stderr)
            return 1

    REFS_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for n, (_, row) in enumerate(df.iterrows(), 1):
        case_id, uid = row["case_id"], row["SeriesInstanceUID"]
        acq_token = str(row["acquisition_number"])
        series_dir = DICOM_DIR / uid
        rec: dict = {"case_id": case_id, "SeriesInstanceUID": uid, "acq_token": acq_token,
                     "geom_regular": row.get("geom_regular")}

        if not series_dir.is_dir():
            rec.update(status="missing_dicom", group="", n_instances=0)
            results.append(rec)
            print(f"  [{n}/{len(df)}] MISSING DICOM  {case_id}")
            continue

        hdr = read_series_headers(series_dir)
        lab = label_grid(LABELS_DIR / row["label_file"])
        rec.update(n_series_instances=len(hdr), n_acq_groups=hdr["acq"].nunique(),
                   acq_groups=",".join(sorted(hdr["acq"].unique())),
                   label_size="x".join(str(v) for v in lab["size"]))

        chosen, attempts = None, []
        for tag, sub in candidate_groups(hdr, acq_token):
            if sub.empty:
                continue
            cand = ascending_grid(sub)
            ev = evaluate(cand, lab)
            attempts.append(f"{tag}:n={len(sub)}:"
                            + (f"MATCH[{ev['slice_order']}]" if ev["match"] else "no"))
            if ev["match"]:
                chosen = (tag, cand, ev)
                break

        if chosen is None:
            rec.update(status="unresolved", group="", n_instances=0,
                       attempts=";".join(attempts))
            results.append(rec)
            print(f"  [{n}/{len(df)}] UNRESOLVED  {case_id}  [{';'.join(attempts)}]")
            continue

        tag, cand, ev = chosen
        inst = cand["instances"]
        (REFS_DIR / f"{case_id}.txt").write_text("\n".join(inst["file"]) + "\n")
        (REFS_DIR / f"{case_id}.sop.txt").write_text("\n".join(inst["SOPInstanceUID"]) + "\n")

        rec.update(
            status="resolved",
            group=tag,
            used_primary_group=bool(tag == f"acq={acq_token}"),
            n_instances=len(inst),
            ref_list=f"data/refs/{case_id}.txt",
            dicom_size="x".join(str(v) for v in cand["size"]),
            dicom_origin_first=",".join(f"{v:.6g}" for v in cand["origin_first"]),
            dicom_spacing=",".join(f"{v:.6g}" for v in cand["spacing"]),
            dicom_direction=",".join(f"{v:.6g}" for v in cand["direction"]),
            gap_min=cand["gap_min"], gap_max=cand["gap_max"], gap_median=cand["gap_median"],
            n_duplicate_positions=cand["n_duplicate_positions"],
            needs_z_flip=bool(ev["slice_order"] == "desc"),
            attempts=";".join(attempts),
            **{k: v for k, v in ev.items() if k != "match"},
        )
        results.append(rec)
        marks = []
        if ev["slice_order"] == "desc":
            marks.append("Z-FLIP")
        if tag != f"acq={acq_token}":
            marks.append(f"FALLBACK {tag}")
        suffix = ("  [" + ", ".join(marks) + "]") if marks else ""
        print(f"  [{n}/{len(df)}] ok  {case_id}  n={len(inst)}/{len(hdr)}{suffix}")

    out = pd.DataFrame(results)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(out["status"].value_counts().to_string())
    res = out[out["status"] == "resolved"]
    if len(res):
        n_primary = int(res["used_primary_group"].astype(bool).sum())
        print(f"\nresolved via the filename's acquisition group: {n_primary}/{len(res)}")
        fb = res[~res["used_primary_group"].astype(bool)]
        for _, r in fb.iterrows():
            print(f"  fallback: {r['case_id']}  acq_token={r['acq_token']} -> {r['group']}")
        print("\nslice order:")
        print(res["slice_order"].value_counts().to_string())
        flips = res[res["needs_z_flip"].astype(bool)]
        print(f"\nrequiring z-flip: {len(flips)}")
        print(f"worst origin delta: {res['d_origin_mm'].max():.3e} mm")
        nonuniform = res[(res["gap_max"] - res["gap_min"]).abs() > 1e-3]
        print(f"matched groups with non-uniform slice gaps: {len(nonuniform)}")
        for _, r in nonuniform.iterrows():
            print(f"  {r['case_id']}: gaps {r['gap_min']:.4g}..{r['gap_max']:.4g} mm")
        dups = res[res["n_duplicate_positions"] > 0]
        print(f"matched groups with duplicate slice positions: {len(dups)}")
        for _, r in dups.iterrows():
            print(f"  {r['case_id']}: {int(r['n_duplicate_positions'])} duplicate(s)")

    bad = out[out["status"] != "resolved"]
    if len(bad):
        print(f"\n{len(bad)} case(s) NOT resolved -- excluded from conversion:", file=sys.stderr)
        for _, r in bad.iterrows():
            print(f"  {r['case_id']}  ({r['status']})  {r.get('attempts','')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
