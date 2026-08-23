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

# An item is unusable if ANY of the Type 1 attributes is missing.
#
# This was initially narrowed to CodeMeaning alone, on the grounds that dcmqi 1.5.7 only enforces that
# one and three cases with a valid CodeValue+CodeMeaning but no CodingSchemeDesignator carried real
# content (e.g. CodeValue='6500572', CodeMeaning='CT, ABDOMEN W&W/O CONTRAST') that dropping would
# discard. That was wrong. dcmqi copies those items through happily, but dciodvfy then reports
# `Missing attribute Type 1C Conditional Element=<CodingSchemeDesignator>` against *our* output: the
# objects we emit are non-conformant. Tolerating a source defect is one thing; propagating it into a
# dataset headed for IDC is another. The alternative -- inventing a "99..." private scheme designator
# -- would fabricate a coding-scheme identity the source never asserted, which is worse than omitting
# an optional (Type 3) sequence.
REQUIRED_IN_CODE_ITEM_BLOCKING = REQUIRED_IN_CODE_ITEM

# VR CS (Code String) permits only uppercase A-Z, 0-9, space and underscore. dcmqi takes
# BodyPartExamined from the reference image in preference to the value in our metadata JSON (we supply
# "KIDNEY"), so a source carrying "Kidney" propagates its own VR violation straight into the output --
# dciodvfy reports one error per lowercase character. Uppercasing is a pure normalisation to the
# standard defined term, inventing nothing, so we fix it rather than ship invalid Code Strings.
CS_LEGAL = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _")
CS_ATTRS = ["BodyPartExamined"]


def cs_violations(ds: pydicom.Dataset) -> list[tuple[str, str, str]]:
    """(attribute, current value, uppercased value) where uppercasing makes the value CS-legal."""
    out = []
    for name in CS_ATTRS:
        val = ds.get(name)
        if val is None:
            continue
        val = str(val)
        if set(val) <= CS_LEGAL:
            continue
        fixed = val.upper()
        # Only touch it when uppercasing actually resolves the violation; anything else needs a human.
        if set(fixed) <= CS_LEGAL:
            out.append((name, val, fixed))
    return out


def malformed(ds: pydicom.Dataset) -> list[str]:
    """Code sequences with an item that blocks dcmqi: no usable CodeMeaning."""
    bad = []
    for name in CODE_SEQUENCES:
        seq = ds.get(name)
        if seq is None:
            continue
        for item in seq:
            if any(not str(item.get(t, "") or "").strip()
                   for t in REQUIRED_IN_CODE_ITEM_BLOCKING):
                bad.append(name)
                break
    return bad


def non_conformant_but_tolerated(ds: pydicom.Dataset) -> list[str]:
    """Type 1 violations left in place. Empty now that all of them are treated as
    blocking -- retained so the report keeps the column and a future relaxation is visible."""
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


def audit() -> "pd.DataFrame":
    """Diff every sanitized reference copy against its original, from what is on disk.

    This is the authoritative record of what was changed: it does not trust the run log, it compares
    the files. Each case whose reference list was repointed keeps its pristine list as
    <case_id>.txt.orig, which gives the original path for every sanitized instance.
    """
    rows = []
    for backup in sorted(REFS_DIR.glob("*.txt.orig")):
        case_id = backup.name[: -len(".txt.orig")]
        originals = [ln.strip() for ln in backup.read_text().splitlines() if ln.strip()]
        current = [ln.strip() for ln in (REFS_DIR / f"{case_id}.txt").read_text().splitlines()
                   if ln.strip()]
        if len(originals) != len(current):
            rows.append({"case_id": case_id, "status": "MISMATCHED_LENGTHS",
                         "n_instances": len(current)})
            continue

        changes: dict[str, set[str]] = {}
        uid_ok = True
        n_changed = 0
        for o, c in zip(originals, current):
            a = pydicom.dcmread(o, stop_before_pixels=True, force=True)
            b = pydicom.dcmread(c, stop_before_pixels=True, force=True)
            if str(a.SOPInstanceUID) != str(b.SOPInstanceUID):
                uid_ok = False
            keys = {e.keyword for e in a if e.keyword} | {e.keyword for e in b if e.keyword}
            inst_changed = False
            for k in keys:
                va, vb = a.get(k, "<absent>"), b.get(k, "<absent>")
                if str(va) != str(vb):
                    changes.setdefault(k, set()).add(f"{va!r} -> {vb!r}")
                    inst_changed = True
            if inst_changed:
                n_changed += 1

        rows.append({
            "case_id": case_id,
            "status": "ok" if uid_ok else "SOPInstanceUID_CHANGED",
            "n_instances": len(current),
            "n_instances_changed": n_changed,
            "attributes_changed": ",".join(sorted(changes)),
            "changes": " | ".join(f"{k}: {'; '.join(sorted(v)[:2])}"
                                 for k, v in sorted(changes.items()))[:600],
            "sanitized_dir": f"data/dicom_sanitized/{case_id}",
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--check", action="store_true", help="report only; do not write or repoint")
    ap.add_argument("--audit", action="store_true",
                    help="diff sanitized copies against originals; write qc/sanitization_audit.csv")
    args = ap.parse_args()

    if args.audit:
        df = audit()
        out = ROOT / "qc" / "sanitization_audit.csv"
        df.to_csv(out, index=False)
        print(f"audited {len(df)} sanitized case(s) -> {out.relative_to(ROOT)}\n")
        if df.empty:
            return 0
        print(df["status"].value_counts().to_string())
        print("\nattributes changed, by frequency:")
        from collections import Counter
        cnt = Counter(a for v in df["attributes_changed"].fillna("") if v for a in v.split(","))
        for a, n in cnt.most_common():
            print(f"  {a:28s} {n} case(s)")
        print(f"\ninstances rewritten: {int(df['n_instances_changed'].fillna(0).sum())}"
              f" of {int(df['n_instances'].fillna(0).sum())}")
        bad = df[df["status"] != "ok"]
        return 1 if len(bad) else 0

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
        cs_bad = cs_violations(first)
        rec = {
            "case_id": case_id,
            "n_instances": len(files),
            "malformed_sequences": ",".join(bad),
            "detail": " | ".join(f"{n}: {describe(first, n)}" for n in bad),
            "non_conformant_but_tolerated": ",".join(tolerated),
            "cs_violations": ",".join(f"{n}={v!r}->{f!r}" for n, v, f in cs_bad),
            "tolerated_detail": " | ".join(
                f"{n.split('(')[0]}: {describe(first, n.split('(')[0])}" for n in tolerated),
            "action": "none",
        }
        if not bad and not cs_bad:
            rows.append(rec)
            continue

        if args.check:
            rec["action"] = "would_sanitize"
            rows.append(rec)
            n_fixed += 1
            print(f"  needs fix  {case_id}  "
                  f"{rec['detail'] or rec['cs_violations']}")
            continue

        out_dir = SANITIZED_DIR / case_id
        out_dir.mkdir(parents=True, exist_ok=True)
        new_files, n_touched = [], 0
        for src in files:
            ds = pydicom.dcmread(src)
            removed = list(malformed(ds))
            for name in removed:
                delattr(ds, name)
            fixed_cs = cs_violations(ds)
            for name, _old, new_val in fixed_cs:
                setattr(ds, name, new_val)
            dest = out_dir / Path(src).name
            ds.save_as(str(dest), enforce_file_format=True)
            new_files.append(str(dest))
            if removed or fixed_cs:
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

        # Keep the first backup: a case can be sanitized more than once (e.g. a code-sequence
        # pass followed by a CS pass), and overwriting would lose the pristine list.
        backup = ref_list.with_suffix(".txt.orig")
        if not backup.exists():
            shutil.copy2(ref_list, backup)
        ref_list.write_text("\n".join(new_files) + "\n")
        rec.update(action="sanitized", n_instances_modified=n_touched,
                   sanitized_dir=str(out_dir.relative_to(ROOT)))
        rows.append(rec)
        n_fixed += 1
        what = []
        if bad:
            what.append(f"removed {','.join(bad)}")
        if cs_bad:
            what.append("uppercased " + ",".join(n for n, _, _ in cs_bad))
        print(f"  sanitized  {case_id}  {'; '.join(what)}"
              f" in {n_touched}/{len(files)} instance(s)")

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
