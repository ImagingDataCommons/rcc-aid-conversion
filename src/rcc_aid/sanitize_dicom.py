#!/usr/bin/env python
"""Repair non-conformant code sequences in the reference DICOM so dcmqi can propagate them.

Why this is needed
------------------
dcmqi copies study-level attributes from the reference image into the derived SEG, including
ProcedureCodeSequence (0008,1032). CodeMeaning (0008,0104) is Type 1 in the Code Sequence Macro --
mandatory and non-empty -- so when the source carries a malformed item, dcmqi refuses to write the
object and the conversion aborts with:

    ERROR - CodeMeaning (0008,0104) absent in CodeSequenceMacro (type 1)
    ERROR - Could not write item #0 in ProcedureCodeSequence: Missing Attribute(s)
    FATAL ERROR: Writing of the SEG dataset failed!

11 of the 129 RCC-AID source series are affected: 8 carry a completely empty ProcedureCodeSequence
item, and 3 (TCGA-G7-6797) carry CodeValue='74160' with the private scheme '99DRSYS134' and no
CodeMeaning at all.

dcmqi's `--noDicomValueChecks` does NOT help here, despite being documented for precisely this
situation ("when taking over attributes from non-compliant DICOM image files"). Tested on
KIRC_...114780349668855871735537947410_acq_5 with dcmqi 1.5.7: the conversion fails identically. The
flag relaxes *value* checking, whereas this is a missing Type 1 *attribute* check inside DCMTK's
CodeSequenceMacro write path. Worth reporting upstream to dcmqi -- until then, this module is the
workaround.

What this does
--------------
Writes copies of only the affected reference instances with the offending sequence *removed*, and
repoints the case's reference list at them. ProcedureCodeSequence is Type 3 (optional) in the General
Study module, so dropping it is conformant -- and it is the honest repair, since inventing a CodeMeaning
would fabricate clinical metadata that the source does not contain.

Nothing else is touched: SOPInstanceUID, geometry, and PixelData are copied verbatim, so the SEG still
references exactly the same instances.

    uv run python -m rcc_aid.sanitize_dicom            # scan all cases, fix what needs fixing
    uv run python -m rcc_aid.sanitize_dicom --check    # report only, change nothing
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd
import pydicom

ROOT = Path(__file__).resolve().parents[2]
RESOLUTION_CSV = ROOT / "qc" / "reference_resolution.csv"
REFS_DIR = ROOT / "data" / "refs"
SANITIZED_DIR = ROOT / "data" / "dicom_sanitized"
OUT_CSV = ROOT / "qc" / "sanitization_report.csv"

# Sequences dcmqi copies from the reference image that must satisfy the Code Sequence Macro.
# All are Type 3 where they appear, so removal is conformant.
CODE_SEQUENCES = ["ProcedureCodeSequence", "AdmittingDiagnosesCodeSequence",
                  "ReasonForPerformedProcedureCodeSequence"]
REQUIRED_IN_CODE_ITEM = ["CodeValue", "CodingSchemeDesignator", "CodeMeaning"]

# Intervene only where conversion is actually blocked. All three attributes above are Type 1, but
# dcmqi 1.5.7 enforces CodeMeaning alone -- it copies items with an absent CodingSchemeDesignator
# without complaint. Three of the 129 source series are in exactly that position and carry real
# content (e.g. CodeValue='6500572', CodeMeaning='CT, ABDOMEN W&W/O CONTRAST'); dropping their
# sequence would destroy clinical metadata to fix a non-conformance nothing is tripping over. So the
# blocking criterion is a missing CodeMeaning, and the rest is reported but left alone.
BLOCKING_ATTR = "CodeMeaning"


def malformed(ds: pydicom.Dataset) -> list[str]:
    """Code sequences with an item that blocks dcmqi: no usable CodeMeaning."""
    bad = []
    for name in CODE_SEQUENCES:
        seq = ds.get(name)
        if seq is None:
            continue
        for item in seq:
            if not str(item.get(BLOCKING_ATTR, "") or "").strip():
                bad.append(name)
                break
    return bad


def non_conformant_but_tolerated(ds: pydicom.Dataset) -> list[str]:
    """Type 1 violations dcmqi accepts. Reported for the record; not modified."""
    out = []
    for name in CODE_SEQUENCES:
        seq = ds.get(name)
        if seq is None or name in malformed(ds):
            continue
        for item in seq:
            missing = [t for t in REQUIRED_IN_CODE_ITEM
                       if not str(item.get(t, "") or "").strip()]
            if missing:
                out.append(f"{name}(missing {'+'.join(missing)})")
                break
    return out


def describe(ds: pydicom.Dataset, name: str) -> str:
    seq = ds.get(name) or []
    return "; ".join(
        "{%s}" % ", ".join(f"{t}={item.get(t)!r}" for t in REQUIRED_IN_CODE_ITEM) for item in seq
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--check", action="store_true", help="report only; do not write or repoint")
    args = ap.parse_args()

    res = pd.read_csv(RESOLUTION_CSV)
    res = res[res["status"] == "resolved"]
    if args.cases:
        res = res[res["case_id"].isin(args.cases)]

    rows = []
    n_fixed = 0
    for _, r in res.iterrows():
        case_id = r["case_id"]
        ref_list = ROOT / str(r["ref_list"])
        files = [ln.strip() for ln in ref_list.read_text().splitlines() if ln.strip()]
        if not files:
            continue

        # The offending attributes are study/series level, so the first instance is representative;
        # verify across all instances anyway, since these datasets have already surprised us once.
        first = pydicom.dcmread(files[0], stop_before_pixels=True, force=True)
        bad = malformed(first)
        tolerated = non_conformant_but_tolerated(first)
        rec = {
            "case_id": case_id,
            "n_instances": len(files),
            "malformed_sequences": ",".join(bad),
            "detail": " | ".join(f"{n}: {describe(first, n)}" for n in bad),
            "non_conformant_but_tolerated": ",".join(tolerated),
            "tolerated_detail": " | ".join(
                f"{n.split('(')[0]}: {describe(first, n.split('(')[0])}" for n in tolerated),
            "action": "none",
        }
        if not bad:
            rows.append(rec)
            continue

        if args.check:
            rec["action"] = "would_sanitize"
            rows.append(rec)
            n_fixed += 1
            print(f"  needs fix  {case_id}  {rec['detail']}")
            continue

        out_dir = SANITIZED_DIR / case_id
        out_dir.mkdir(parents=True, exist_ok=True)
        new_files, n_touched = [], 0
        for src in files:
            ds = pydicom.dcmread(src)
            removed = [n for n in malformed(ds)]
            for name in removed:
                delattr(ds, name)
            dest = out_dir / Path(src).name
            ds.save_as(str(dest), enforce_file_format=True)
            new_files.append(str(dest))
            if removed:
                n_touched += 1

        # sanity: the instance identity and count must be unchanged
        old_uids = [pydicom.dcmread(f, stop_before_pixels=True, force=True).SOPInstanceUID
                    for f in files]
        new_uids = [pydicom.dcmread(f, stop_before_pixels=True, force=True).SOPInstanceUID
                    for f in new_files]
        if old_uids != new_uids:
            rec["action"] = "FAILED: SOPInstanceUID order changed"
            rows.append(rec)
            print(f"  FAILED     {case_id}: instance identity changed", file=sys.stderr)
            continue

        shutil.copy2(ref_list, ref_list.with_suffix(".txt.orig"))
        ref_list.write_text("\n".join(new_files) + "\n")
        rec.update(action="sanitized", n_instances_modified=n_touched,
                   sanitized_dir=str(out_dir.relative_to(ROOT)))
        rows.append(rec)
        n_fixed += 1
        print(f"  sanitized  {case_id}  removed {','.join(bad)} from {n_touched}/{len(files)}"
              f" instance(s)")

    df = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(f"cases scanned: {len(df)}")
    print(f"cases needing repair: {n_fixed}")
    if n_fixed:
        print(f"sequences involved: "
              f"{sorted(set(s for v in df['malformed_sequences'] if isinstance(v, str) and v for s in v.split(',')))}")
    if args.check and n_fixed:
        print("\n--check only; re-run without it to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
