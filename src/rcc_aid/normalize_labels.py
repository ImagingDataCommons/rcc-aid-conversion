#!/usr/bin/env python
"""Produce corrected, integer-typed label maps for conversion.

Two defects in the published RCC-AID NIfTI label maps have to be repaired before they can be expressed
as DICOM. Both affect the same 13 files (all KIRC, all from irregular multi-acquisition series), which
evidently went through a different upstream code path:

  1. dtype. 13 of 129 are float32; 10 of those carry values like 1.0000152587890625 (= 1 + 1/65536, a
     NIfTI scale-factor artifact) rather than exact 1/2/3. Segment label values must be exact integers.
     Every file rounds to {0,1,2} or {0,1,2,3} with a worst-case deviation of 1.5e-5, so rounding is
     unambiguous.

  2. Slice order. The same 13 store their slices in *descending* DICOM z order while still declaring a
     +z direction cosine. Their declared geometry therefore places the volume outside the imaged range
     entirely -- for one case, z from -155 to +290 against a scan covering -600 to -155. Confirmed
     independently by intensity: with the array flipped, kidney-labelled voxels fall in
     contrast-enhanced soft tissue; unflipped they land on air and bone.

Output is NRRD, not NIfTI, deliberately: NIfTI's inability to carry an arbitrary direction matrix is
what lost the slice-order information upstream. NRRD represents it faithfully and dcmqi reads it
natively.

Output: data/labels_norm/<case_id>_seg.nrrd, qc/normalization_report.csv

    uv run python -m rcc_aid.normalize_labels                  # includes the intensity check
    uv run python -m rcc_aid.normalize_labels --no-intensity   # skip it (avoids reading 8 GB of CT)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import SimpleITK as sitk

from .terminology import SEGMENTS

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
RESOLUTION_CSV = ROOT / "qc" / "reference_resolution.csv"
LABELS_DIR = ROOT / "data" / "labels"
REFS_DIR = ROOT / "data" / "refs"
OUT_DIR = ROOT / "data" / "labels_norm"
OUT_CSV = ROOT / "qc" / "normalization_report.csv"

MAX_ROUNDING_DEVIATION = 0.1  # anything near 0.5 would make rounding ambiguous
TOL_ORIGIN_MM = 1e-2
TOL_SPACING_MM = 1e-3
TOL_DIRECTION = 1e-4

# Kidney parenchyma on contrast-enhanced CT. Wide on purpose: this is a gross orientation check, not a
# tissue classifier.
SOFT_TISSUE_HU = (-50, 400)


def parse_triple(s: str) -> tuple[float, ...]:
    return tuple(float(x) for x in str(s).split(","))


def intensity_plausibility(ref_list: Path, arr: np.ndarray) -> dict:
    """Fraction of kidney-labelled voxels landing in soft tissue, for this array and its z-flip."""
    files = [ln.strip() for ln in ref_list.read_text().splitlines() if ln.strip()]
    reader = sitk.ImageSeriesReader()
    reader.SetFileNames(files)
    ct = sitk.GetArrayFromImage(reader.Execute())
    if ct.shape != arr.shape:
        return {"intensity_check": "shape_mismatch"}

    lo, hi = SOFT_TISSUE_HU
    out = {}
    for tag, a in (("chosen", arr), ("alternative", arr[::-1])):
        m = a == 1
        if not m.any():
            out[f"frac_soft_tissue_{tag}"] = float("nan")
            out[f"kidney_mean_hu_{tag}"] = float("nan")
            continue
        hu = ct[m]
        out[f"frac_soft_tissue_{tag}"] = float(np.mean((hu >= lo) & (hu <= hi)))
        out[f"kidney_mean_hu_{tag}"] = float(hu.mean())
    out["intensity_check"] = "done"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--no-intensity", action="store_true",
                    help="skip the CT intensity plausibility check")
    args = ap.parse_args()

    res = pd.read_csv(RESOLUTION_CSV)
    res = res[res["status"] == "resolved"]
    idx = pd.read_csv(CASE_INDEX).set_index("case_id")
    if args.cases:
        res = res[res["case_id"].isin(args.cases)]
    if res.empty:
        print("no resolved cases", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows, problems_total = [], 0

    for n, (_, r) in enumerate(res.iterrows(), 1):
        case_id = r["case_id"]
        src_path = LABELS_DIR / idx.loc[case_id, "label_file"]
        img = sitk.ReadImage(str(src_path))
        raw = sitk.GetArrayFromImage(img)

        rec: dict = {"case_id": case_id, "src_dtype": str(raw.dtype),
                     "needs_z_flip": bool(r["needs_z_flip"])}
        problems: list[str] = []

        # 1. exact integers
        dev = float(np.max(np.abs(raw - np.round(raw))))
        rec["max_rounding_deviation"] = dev
        if dev > MAX_ROUNDING_DEVIATION:
            problems.append(f"rounding deviation {dev:.4g} too large to be unambiguous")
        arr = np.round(raw).astype(np.uint8)

        values = sorted(int(v) for v in np.unique(arr))
        rec["values"] = ",".join(str(v) for v in values)
        unexpected = [v for v in values if v != 0 and v not in SEGMENTS]
        if unexpected:
            problems.append(f"unexpected label values {unexpected}")

        counts_before = {int(v): int(c) for v, c in zip(*np.unique(arr, return_counts=True))}

        # 2. slice order
        if r["needs_z_flip"]:
            arr = arr[::-1].copy()
        counts_after = {int(v): int(c) for v, c in zip(*np.unique(arr, return_counts=True))}
        if counts_before != counts_after:
            problems.append("voxel counts changed across the flip")
        for lid, seg in sorted(SEGMENTS.items()):
            rec[f"voxels_{seg.name.lower()}"] = counts_after.get(lid, 0)
            # must equal what build_case_index recorded from the untouched source
            expected = int(idx.loc[case_id, f"voxels_{seg.name.lower()}"])
            if counts_after.get(lid, 0) != expected:
                problems.append(f"{seg.name} voxel count {counts_after.get(lid,0)} != {expected}")

        # 3. stamp the geometry taken from the resolved DICOM instances
        out = sitk.GetImageFromArray(arr)
        out.SetOrigin(parse_triple(r["dicom_origin_first"]))
        out.SetSpacing(parse_triple(r["dicom_spacing"]))
        out.SetDirection(parse_triple(r["dicom_direction"]))

        d_o = float(np.max(np.abs(np.array(out.GetOrigin())
                                  - np.array(parse_triple(r["dicom_origin_first"])))))
        if d_o > TOL_ORIGIN_MM:
            problems.append(f"origin not stamped correctly ({d_o:.3g} mm)")

        out_path = OUT_DIR / f"{case_id}_seg.nrrd"
        sitk.WriteImage(out, str(out_path), useCompression=True)

        # 4. read back and confirm nothing changed on the way to disk
        back = sitk.ReadImage(str(out_path))
        rt = sitk.GetArrayFromImage(back)
        if not np.array_equal(rt, arr):
            problems.append("NRRD write/read round-trip differs")
        for name, a, b, tol in (
            ("origin", back.GetOrigin(), out.GetOrigin(), TOL_ORIGIN_MM),
            ("spacing", back.GetSpacing(), out.GetSpacing(), TOL_SPACING_MM),
            ("direction", back.GetDirection(), out.GetDirection(), TOL_DIRECTION),
        ):
            d = float(np.max(np.abs(np.array(a) - np.array(b))))
            if d > tol:
                problems.append(f"{name} changed on write/read by {d:.3g}")

        # 5. independent evidence that the orientation is right
        if not args.no_intensity:
            rec.update(intensity_plausibility(REFS_DIR / f"{case_id}.txt", arr))
            fc = rec.get("frac_soft_tissue_chosen")
            fa = rec.get("frac_soft_tissue_alternative")
            if fc is not None and fa is not None and not (np.isnan(fc) or np.isnan(fa)):
                if fc < fa:
                    problems.append(
                        f"orientation suspect: soft-tissue fraction {fc:.3f} < "
                        f"alternative {fa:.3f}"
                    )

        rec["output"] = f"data/labels_norm/{case_id}_seg.nrrd"
        rec["output_bytes"] = out_path.stat().st_size
        rec["n_problems"] = len(problems)
        rec["problems"] = " | ".join(problems)
        rec["pass"] = not problems
        problems_total += len(problems)
        rows.append(rec)

        mark = "ok " if not problems else "FAIL"
        flip = "  [z-flipped]" if r["needs_z_flip"] else ""
        extra = ""
        if "frac_soft_tissue_chosen" in rec and not np.isnan(rec["frac_soft_tissue_chosen"]):
            extra = (f"  soft-tissue {rec['frac_soft_tissue_chosen']:.3f}"
                     f" vs alt {rec['frac_soft_tissue_alternative']:.3f}")
        print(f"  [{n}/{len(res)}] {mark} {case_id}{flip}{extra}"
              + ("" if not problems else f"  <- {problems[0]}"))

    df = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(f"passed: {int(df['pass'].sum())}/{len(df)}")
    print(f"\nsource dtype: {df['src_dtype'].value_counts().to_dict()}")
    print(f"z-flipped: {int(df['needs_z_flip'].sum())}")
    print(f"max rounding deviation: {df['max_rounding_deviation'].max():.3e}")
    print(f"label value sets: {df['values'].value_counts().to_dict()}")
    if "frac_soft_tissue_chosen" in df:
        ok = df.dropna(subset=["frac_soft_tissue_chosen"])
        print(f"\nintensity check over {len(ok)} cases:")
        print(f"  soft-tissue fraction, chosen orientation:      "
              f"min={ok['frac_soft_tissue_chosen'].min():.3f} "
              f"median={ok['frac_soft_tissue_chosen'].median():.3f}")
        print(f"  soft-tissue fraction, alternative orientation: "
              f"min={ok['frac_soft_tissue_alternative'].min():.3f} "
              f"median={ok['frac_soft_tissue_alternative'].median():.3f}")
        print(f"  chosen better than alternative: "
              f"{int((ok['frac_soft_tissue_chosen'] > ok['frac_soft_tissue_alternative']).sum())}"
              f"/{len(ok)}")
        flips = ok[ok["needs_z_flip"]]
        if len(flips):
            print(f"  among the {len(flips)} z-flipped cases: chosen median "
                  f"{flips['frac_soft_tissue_chosen'].median():.3f} vs alternative median "
                  f"{flips['frac_soft_tissue_alternative'].median():.3f}")

    failed = df[~df["pass"]]
    if len(failed):
        print(f"\n{len(failed)} case(s) with problems:", file=sys.stderr)
        for _, r in failed.iterrows():
            print(f"  {r['case_id']}: {r['problems']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
