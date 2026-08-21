#!/usr/bin/env python
"""Quality control for the converted DICOM SEG objects.

Per case and flavour:

  round-trip    segimage2itkimage --mergeSegments, then a voxel-wise comparison against the label map
                dcmqi actually consumed. Because --useLabelIDAsSegmentNumber makes SegmentNumber ==
                the source label value, the decode must be *exactly* equal. Any difference is a hard
                failure.

                The comparison is offset-aware. BINARY is written with --skip 1, so it covers only the
                slice range that carries segmentation and decodes to a cropped volume with its own
                origin. That is correct and intended, so QC locates the crop from the origin, requires
                exact equality inside it, and requires the source to be pure background outside it --
                which is what proves the skipping lost nothing.

  cross-flavour both flavours are embedded back into full source geometry and must equal each other
                and the source. This is the point of ADR-001: IDC holds no LABELMAP series to check
                against, so BINARY is the oracle.

  headers       SegmentationType, SegmentNumber -> code mapping, algorithm type/name,
                PixelPaddingValue on labelmap, patient/study identity, and that referenced instances
                are a subset of the resolved reference list.

Output: qc/qc_report.csv

    uv run python -m rcc_aid.qc
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import pydicom
import SimpleITK as sitk

from .terminology import SEGMENTS

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
CONVERSION_CSV = ROOT / "qc" / "conversion_report.csv"
# The normalized labels are what dcmqi consumed, so they are the round-trip reference.
# rcc_aid.normalize_labels separately proves they preserve the source voxel content exactly.
LABELS_DIR = ROOT / "data" / "labels_norm"
REFS_DIR = ROOT / "data" / "refs"
OUT_DIRS = {"labelmap": ROOT / "data" / "seg_labelmap", "binary": ROOT / "data" / "seg_binary"}
OUT_CSV = ROOT / "qc" / "qc_report.csv"

TOL_ORIGIN_MM = 1e-2
TOL_SPACING_MM = 1e-3
TOL_DIRECTION = 1e-4
TOL_OFFSET_FRAC = 0.05  # how close the origin shift must be to a whole number of slices

# segimage2itkimage is a subprocess; physical cores, matching rcc_aid.convert.
DEFAULT_JOBS = max(1, (os.cpu_count() or 4) // 2)

ALGORITHM_TYPE = "SEMIAUTOMATIC"
ALGORITHM_NAME = "Robust Kidney Abnormality Segmentation"

# dcmqi >=1.5.5 inserts this segment itself for labelmap output; it is not one of ours.
# (125040, DCM, "Background") is a standard code from CID 7165 Abstract Segmentation Type.
BACKGROUND_SEGMENT = (0, "125040")


def decode(seg_path: Path) -> tuple[np.ndarray, sitk.Image]:
    """segimage2itkimage --mergeSegments -> (array, image). Raises on failure or overlap."""
    from .convert import dcmqi_bin

    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [dcmqi_bin("segimage2itkimage"), "--inputDICOM", str(seg_path),
             "--outputDirectory", td, "--outputType", "nrrd", "--mergeSegments", "--prefix", "rt"],
            capture_output=True, text=True,
        )
        images = sorted(Path(td).glob("*.nrrd"))
        if proc.returncode != 0 or not images:
            raise RuntimeError(
                (proc.stderr.strip().splitlines() or ["segimage2itkimage produced no output"])[-1]
            )
        if len(images) > 1:
            raise RuntimeError(f"overlapping segments: {len(images)} files from --mergeSegments")
        img = sitk.ReadImage(str(images[0]))
        return sitk.GetArrayFromImage(img), img


def align(dec_arr: np.ndarray, dec: sitk.Image, src_arr: np.ndarray,
          src: sitk.Image) -> tuple[int, list[str], dict]:
    """Locate the decoded volume inside the source volume. Returns (z_offset, problems, facts)."""
    problems: list[str] = []
    facts: dict = {}

    d_dir = float(np.max(np.abs(np.array(dec.GetDirection()) - np.array(src.GetDirection()))))
    d_sp = float(np.max(np.abs(np.array(dec.GetSpacing()) - np.array(src.GetSpacing()))))
    facts["d_direction"] = d_dir
    facts["d_spacing_mm"] = d_sp
    if d_dir > TOL_DIRECTION:
        problems.append(f"direction off by {d_dir:.3g}")
    if d_sp > TOL_SPACING_MM:
        problems.append(f"spacing off by {d_sp:.3g} mm")

    if dec.GetSize()[:2] != src.GetSize()[:2]:
        problems.append(f"in-plane size {dec.GetSize()[:2]} != {src.GetSize()[:2]}")
        return 0, problems, facts

    # Origin shift must lie purely along the slice normal, by a whole number of slices.
    direction = np.array(src.GetDirection()).reshape(3, 3)
    normal = direction[:, 2]
    delta = np.array(dec.GetOrigin()) - np.array(src.GetOrigin())
    along = float(np.dot(delta, normal))
    lateral = float(np.linalg.norm(delta - along * normal))
    spacing_z = float(src.GetSpacing()[2])
    off_f = along / spacing_z
    off = int(round(off_f))

    facts["origin_lateral_mm"] = lateral
    facts["z_offset_slices"] = off
    facts["z_offset_fractional"] = off_f
    if lateral > TOL_ORIGIN_MM:
        problems.append(f"origin shifted laterally by {lateral:.3g} mm")
    if abs(off_f - off) > TOL_OFFSET_FRAC:
        problems.append(f"origin shift {off_f:.3f} slices is not a whole number")
    if off < 0 or off + dec_arr.shape[0] > src_arr.shape[0]:
        problems.append(f"decoded volume [{off},{off + dec_arr.shape[0]}) "
                        f"outside source extent [0,{src_arr.shape[0]})")
    return off, problems, facts


def embed(dec_arr: np.ndarray, off: int, src_arr: np.ndarray) -> np.ndarray:
    """Place the decoded volume back into full source geometry, background elsewhere."""
    full = np.zeros_like(src_arr)
    full[off:off + dec_arr.shape[0]] = dec_arr
    return full


def check_headers(seg_path: Path, flavour: str, row: pd.Series,
                  ref_sops: set[str]) -> tuple[list[str], dict]:
    ds = pydicom.dcmread(str(seg_path), stop_before_pixels=True)
    problems: list[str] = []
    facts: dict = {}

    actual = str(getattr(ds, "SegmentationType", ""))
    facts["hdr_segmentation_type"] = actual
    if actual != flavour.upper():
        problems.append(f"SegmentationType={actual!r}, expected {flavour.upper()!r}")

    ppv = getattr(ds, "PixelPaddingValue", None)
    facts["hdr_pixel_padding_value"] = "" if ppv is None else str(ppv)
    if flavour == "labelmap" and ppv is None:
        problems.append("labelmap has no PixelPaddingValue (0028,0120)")

    expected = {i: SEGMENTS[i].seg_type["CodeValue"]
                for i in (int(v) for v in str(row["labels_present"]).split(",") if v)}
    seen: dict[int, str] = {}
    for seg in getattr(ds, "SegmentSequence", []):
        num = int(seg.SegmentNumber)
        code = str(seg.SegmentedPropertyTypeCodeSequence[0].CodeValue)
        if (num, code) == BACKGROUND_SEGMENT:
            # dcmqi's own background segment. Verify it is what we think, then set it aside.
            facts["hdr_has_background_segment"] = True
            if flavour != "labelmap":
                problems.append("background segment present in a BINARY object")
            continue
        seen[num] = code
        cat = str(seg.SegmentedPropertyCategoryCodeSequence[0].CodeValue)
        if num in SEGMENTS and cat != SEGMENTS[num].category["CodeValue"]:
            problems.append(f"segment {num} category {cat} != "
                            f"{SEGMENTS[num].category['CodeValue']}")
        if str(getattr(seg, "SegmentAlgorithmType", "")) != ALGORITHM_TYPE:
            problems.append(f"segment {num} SegmentAlgorithmType="
                            f"{getattr(seg, 'SegmentAlgorithmType', None)!r}")
        if str(getattr(seg, "SegmentAlgorithmName", "")) != ALGORITHM_NAME:
            problems.append(f"segment {num} SegmentAlgorithmName="
                            f"{getattr(seg, 'SegmentAlgorithmName', None)!r}")
    facts.setdefault("hdr_has_background_segment", False)
    facts["hdr_segment_numbers"] = ",".join(str(k) for k in sorted(seen))
    facts["hdr_n_segments"] = len(seen)
    if seen != expected:
        problems.append(f"segment map {seen} != expected {expected}")

    facts["hdr_PatientID"] = str(getattr(ds, "PatientID", ""))
    facts["hdr_StudyInstanceUID"] = str(getattr(ds, "StudyInstanceUID", ""))
    if facts["hdr_PatientID"] != str(row["idc_PatientID"]):
        problems.append(f"PatientID {facts['hdr_PatientID']!r} != {row['idc_PatientID']!r}")
    if facts["hdr_StudyInstanceUID"] != str(row["idc_StudyInstanceUID"]):
        problems.append("StudyInstanceUID does not match the source study")

    refd: set[str] = set()
    ref_series: set[str] = set()
    for rs in getattr(ds, "ReferencedSeriesSequence", []):
        ref_series.add(str(rs.SeriesInstanceUID))
        for ri in getattr(rs, "ReferencedInstanceSequence", []):
            refd.add(str(ri.ReferencedSOPInstanceUID))
    facts["hdr_n_referenced_instances"] = len(refd)
    if ref_series != {str(row["SeriesInstanceUID"])}:
        problems.append(f"ReferencedSeriesSequence={sorted(ref_series)} != source series")
    stray = refd - ref_sops
    if stray:
        problems.append(f"{len(stray)} referenced instance(s) outside the resolved list")

    facts["hdr_n_frames"] = int(getattr(ds, "NumberOfFrames", 0) or 0)
    facts["hdr_bits_allocated"] = int(getattr(ds, "BitsAllocated", 0) or 0)
    facts["hdr_transfer_syntax"] = str(ds.file_meta.TransferSyntaxUID.name)
    return problems, facts


def check_case(case_id: str, row: pd.Series) -> dict:
    """All checks for one case, across both flavours. Returns one report row."""
    rec: dict = {"case_id": case_id}
    problems: list[str] = []
    src_img = sitk.ReadImage(str(LABELS_DIR / f"{case_id}_seg.nrrd"))
    src_arr = sitk.GetArrayFromImage(src_img)
    rec["src_size"] = "x".join(str(v) for v in src_img.GetSize())
    for lid, seg in sorted(SEGMENTS.items()):
        rec[f"src_voxels_{seg.name.lower()}"] = int((src_arr == lid).sum())
    sops = {ln.strip() for ln in (REFS_DIR / f"{case_id}.sop.txt").read_text().splitlines()
            if ln.strip()}
    embedded: dict[str, np.ndarray] = {}
    for flavour in ("labelmap", "binary"):
        seg_path = OUT_DIRS[flavour] / f"{case_id}.dcm"
        if not seg_path.exists():
            rec[f"{flavour}_roundtrip"] = "absent"
            continue
        try:
            dec_arr, dec_img = decode(seg_path)
        except Exception as e:  # noqa: BLE001
            rec[f"{flavour}_roundtrip"] = "decode_failed"
            problems.append(f"{flavour}: decode failed: {e}")
            continue
        rec[f"{flavour}_decoded_size"] = "x".join(str(v) for v in dec_img.GetSize())
        off, probs, facts = align(dec_arr, dec_img, src_arr, src_img)
        problems += [f"{flavour}: {p}" for p in probs]
        rec.update({f"{flavour}_{k}": v for k, v in facts.items()})
        if probs:
            rec[f"{flavour}_roundtrip"] = "misaligned"
        else:
            full = embed(dec_arr, off, src_arr)
            embedded[flavour] = full
            n_diff = int(np.count_nonzero(full != src_arr))
            rec[f"{flavour}_roundtrip"] = "exact" if n_diff == 0 else "DIFFERS"
            rec[f"{flavour}_n_voxels_differing"] = n_diff
            # what --skip dropped must have been background, or information was lost
            outside = np.concatenate([src_arr[:off].ravel(),
                                      src_arr[off + dec_arr.shape[0]:].ravel()])
            n_outside = int(np.count_nonzero(outside))
            rec[f"{flavour}_n_nonzero_outside_extent"] = n_outside
            rec[f"{flavour}_slices_dropped"] = int(src_arr.shape[0] - dec_arr.shape[0])
            if n_outside:
                problems.append(f"{flavour}: {n_outside} labelled voxel(s) outside the "
                                f"encoded extent -- skipped frames lost data")
            if n_diff:
                problems.append(f"{flavour}: round-trip differs in {n_diff} voxel(s)")
            c = {int(v): int(k) for v, k in zip(*np.unique(full, return_counts=True))}
            for lid, seg in sorted(SEGMENTS.items()):
                rec[f"{flavour}_voxels_{seg.name.lower()}"] = c.get(lid, 0)
        try:
            probs, facts = check_headers(seg_path, flavour, row, sops)
            problems += [f"{flavour}: {p}" for p in probs]
            rec.update({f"{flavour}_{k}": v for k, v in facts.items()})
        except Exception as e:  # noqa: BLE001
            problems.append(f"{flavour}: header check failed: {e}")
    if len(embedded) == 2:
        same = np.array_equal(embedded["labelmap"], embedded["binary"])
        rec["labelmap_eq_binary"] = "yes" if same else "NO"
        if not same:
            problems.append("labelmap and binary decode differently")
    rec["n_problems"] = len(problems)
    rec["problems"] = " | ".join(problems)
    rec["pass"] = not problems
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"cases checked in parallel (default {DEFAULT_JOBS})")
    args = ap.parse_args()

    idx = pd.read_csv(CASE_INDEX).set_index("case_id")
    conv = pd.read_csv(CONVERSION_CSV)
    cases = sorted(set(conv[conv["status"] == "ok"]["case_id"]))
    if args.cases:
        cases = [c for c in cases if c in set(args.cases)]
    if not cases:
        print("no successfully converted cases to check", file=sys.stderr)
        return 1

    print(f"QC over {len(cases)} case(s), {args.jobs} job(s) in parallel")
    rows, done = [], 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(check_case, c, idx.loc[c]): c for c in cases}
        for fut in as_completed(futures):
            case_id = futures[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = {"case_id": case_id, "n_problems": 1, "pass": False,
                       "problems": f"QC raised {type(e).__name__}: {e}"}
            rows.append(rec)
            done += 1
            print(f"  [{done}/{len(cases)}] "
                  f"{'ok ' if rec.get('pass') else 'FAIL'} {case_id}"
                  + ("" if rec.get("pass") else f"  <- {rec['problems'].split(' | ')[0]}"),
                  flush=True)

    df = pd.DataFrame(rows).sort_values("case_id").reset_index(drop=True)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(f"passed: {int(df['pass'].sum())}/{len(df)}")
    for col in ("labelmap_roundtrip", "binary_roundtrip", "labelmap_eq_binary"):
        if col in df:
            print(f"\n{col}: {df[col].value_counts(dropna=False).to_dict()}")
    if "binary_slices_dropped" in df:
        print(f"\nbinary empty slices skipped: median "
              f"{df['binary_slices_dropped'].median():.0f}, max "
              f"{df['binary_slices_dropped'].max():.0f} "
              f"(all verified to contain no labels)")
    if "labelmap_hdr_transfer_syntax" in df:
        print(f"labelmap transfer syntax: {df['labelmap_hdr_transfer_syntax'].unique().tolist()}")
        print(f"binary transfer syntax:   {df['binary_hdr_transfer_syntax'].unique().tolist()}")

    failed = df[~df["pass"]]
    if len(failed):
        print(f"\n{len(failed)} case(s) with QC problems:", file=sys.stderr)
        for _, r in failed.iterrows():
            print(f"  {r['case_id']}: {r['problems']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
