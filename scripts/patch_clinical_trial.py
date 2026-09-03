#!/usr/bin/env python
"""Add the dataset DOI to already-converted segmentations, without reconverting them.

rcc_aid.convert now stamps every object it writes (see rcc_aid.clinical_trial). The 258 objects in
data/seg_{labelmap,binary}/ predate that, and reconverting them purely to add seven attributes would
be wasteful and actively harmful: dcmqi mints fresh SOPInstanceUIDs and SeriesInstanceUIDs on every
run, so the objects already loaded into the review DICOM store, cited in qc/viewer_urls.csv and
reviewed in the viewer would all be superseded by different-UID copies. Patching in place keeps every
UID, so the store can be refreshed by an exact replacement.

What it changes
---------------
Only the seven attributes rcc_aid.clinical_trial owns. Everything else -- SOPInstanceUID, geometry,
PixelData, transfer syntax, File Meta -- is preserved, and --audit proves it by diffing the patched
object against a pristine copy element by element rather than trusting this run's own log.

    uv run python scripts/patch_clinical_trial.py --check    # report what would change
    uv run python scripts/patch_clinical_trial.py            # patch, keeping .orig backups
    uv run python scripts/patch_clinical_trial.py --audit    # diff patched vs .orig, write the report
    uv run python scripts/patch_clinical_trial.py --drop-backups   # once the audit is clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pandas as pd
import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rcc_aid.clinical_trial import (  # noqa: E402
    DATASET_DOI,
    OWNED_ATTRS,
    attributes,
    current,
    stamp,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIRS = {"labelmap": ROOT / "data" / "seg_labelmap", "binary": ROOT / "data" / "seg_binary"}
OUT_CSV = ROOT / "qc" / "clinical_trial_patch.csv"
AUDIT_CSV = ROOT / "qc" / "clinical_trial_patch_audit.csv"

BACKUP_SUFFIX = ".orig"


def targets(flavours: list[str], cases: list[str] | None) -> list[tuple[str, str, Path]]:
    out = []
    for flavour in flavours:
        for path in sorted(OUT_DIRS[flavour].glob("*.dcm")):
            if cases is None or path.stem in set(cases):
                out.append((path.stem, flavour, path))
    return out


def patch_one(case_id: str, flavour: str, path: Path, doi: str,
              check: bool, keep_backup: bool) -> dict:
    """Patch one object. Returns a report row; never raises for per-file problems."""
    row = {"case_id": case_id, "flavour": flavour,
           "file": str(path.relative_to(ROOT)), "bytes_before": path.stat().st_size}
    try:
        ds = pydicom.dcmread(str(path))
        before = current(ds)
        want = attributes(str(ds.PatientID), doi)
        row["PatientID"] = str(ds.PatientID)
        row["SOPInstanceUID"] = str(ds.SOPInstanceUID)
        row["doi_before"] = before["OtherClinicalTrialProtocolIDsSequence"] or ""
        row["changed_attributes"] = ",".join(k for k in OWNED_ATTRS if before[k] != want[k])

        if before == want:
            row["status"] = "already_current"
            row["bytes_after"] = row["bytes_before"]
            return row
        if check:
            row["status"] = "would_patch"
            row["bytes_after"] = ""
            return row

        # Back up before writing: the audit needs a pristine copy to diff against, and it is the
        # only way back if the stamp turns out to be wrong.
        backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if not backup.exists():
            shutil.copy2(path, backup)

        stamp(ds, doi)
        tmp = path.with_suffix(path.suffix + ".patching")
        try:
            ds.save_as(str(tmp))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

        if not keep_backup:
            backup.unlink(missing_ok=True)

        row["status"] = "patched"
        row["bytes_after"] = path.stat().st_size
        return row
    except Exception as e:  # noqa: BLE001 -- one bad file must not abort the run
        row["status"] = f"FAILED: {type(e).__name__}: {e}"[:300]
        return row


def audit() -> pd.DataFrame:
    """Diff every patched object against its backup, from what is on disk.

    Mirrors rcc_aid.sanitize_dicom --audit: the authoritative record of what changed is the files
    themselves, not the run log. A patch is correct only if the ONLY differences are the attributes
    rcc_aid.clinical_trial owns, and PixelData, the UIDs and the transfer syntax are untouched.
    """
    rows = []
    for flavour, d in OUT_DIRS.items():
        for backup in sorted(d.glob(f"*.dcm{BACKUP_SUFFIX}")):
            path = backup.with_suffix("")
            a = pydicom.dcmread(str(backup))
            b = pydicom.dcmread(str(path))

            keys = {e.tag for e in a} | {e.tag for e in b}
            changed = []
            for t in sorted(keys):
                va = a[t].value if t in a else "<absent>"
                vb = b[t].value if t in b else "<absent>"
                if str(va) != str(vb):
                    changed.append(pydicom.datadict.keyword_for_tag(t) or str(t))
            unexpected = [k for k in changed if k not in OWNED_ATTRS]

            fa = {e.tag: str(e.value) for e in a.file_meta}
            fb = {e.tag: str(e.value) for e in b.file_meta}
            meta_changed = sorted(pydicom.datadict.keyword_for_tag(t) or str(t)
                                  for t in set(fa) | set(fb) if fa.get(t) != fb.get(t))

            problems = []
            if unexpected:
                problems.append(f"unexpected attribute change: {','.join(unexpected)}")
            if meta_changed:
                problems.append(f"File Meta changed: {','.join(meta_changed)}")
            if str(a.SOPInstanceUID) != str(b.SOPInstanceUID):
                problems.append("SOPInstanceUID CHANGED")
            if str(a.SeriesInstanceUID) != str(b.SeriesInstanceUID):
                problems.append("SeriesInstanceUID CHANGED")
            if a.PixelData != b.PixelData:
                problems.append("PixelData CHANGED")
            if a.file_meta.TransferSyntaxUID != b.file_meta.TransferSyntaxUID:
                problems.append("TransferSyntaxUID CHANGED")

            got = current(b)
            want = attributes(str(b.PatientID), DATASET_DOI)
            for k in OWNED_ATTRS:
                if got[k] != want[k]:
                    problems.append(f"{k}={got[k]!r}, expected {want[k]!r}")

            rows.append({
                "case_id": path.stem,
                "flavour": flavour,
                "status": "ok" if not problems else "PROBLEM",
                "attributes_changed": ",".join(changed),
                "doi": got["OtherClinicalTrialProtocolIDsSequence"] or "",
                "sop_instance_uid_preserved": str(a.SOPInstanceUID) == str(b.SOPInstanceUID),
                "pixeldata_preserved": a.PixelData == b.PixelData,
                "transfer_syntax": str(b.file_meta.TransferSyntaxUID.name),
                "bytes_before": backup.stat().st_size,
                "bytes_after": path.stat().st_size,
                "problems": " | ".join(problems)[:400],
            })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids")
    ap.add_argument("--flavours", nargs="*", default=["labelmap", "binary"],
                    choices=["labelmap", "binary"])
    ap.add_argument("--doi", default=DATASET_DOI,
                    help=f"DOI to stamp (default {DATASET_DOI})")
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    ap.add_argument("--audit", action="store_true",
                    help=f"diff patched objects against their {BACKUP_SUFFIX} backups")
    ap.add_argument("--no-backup", action="store_true",
                    help="do not keep .orig backups (--audit will then have nothing to compare)")
    ap.add_argument("--drop-backups", action="store_true",
                    help="delete the .orig backups and exit; run only after a clean --audit")
    args = ap.parse_args()

    if args.drop_backups:
        n = 0
        for d in OUT_DIRS.values():
            for backup in d.glob(f"*.dcm{BACKUP_SUFFIX}"):
                backup.unlink()
                n += 1
        print(f"deleted {n} backup(s)")
        return 0

    if args.audit:
        df = audit()
        if df.empty:
            print(f"no {BACKUP_SUFFIX} backups found; nothing to audit", file=sys.stderr)
            return 1
        AUDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(AUDIT_CSV, index=False)
        print(f"wrote {AUDIT_CSV.relative_to(ROOT)}  ({len(df)} object(s))")
        print(df.groupby(["flavour", "status"]).size().to_string())
        print(f"\nattribute change sets observed:\n"
              f"{df['attributes_changed'].value_counts().to_string()}")
        bad = df[df["status"] != "ok"]
        if len(bad):
            print(f"\n{len(bad)} object(s) with problems:", file=sys.stderr)
            for _, r in bad.iterrows():
                print(f"  {r['flavour']:8s} {r['case_id']}: {r['problems']}", file=sys.stderr)
            return 1
        print("\nevery patched object differs from its backup in the expected attributes only; "
              "UIDs, PixelData and transfer syntax preserved")
        return 0

    work = targets(args.flavours, args.cases)
    if not work:
        print("no objects found to patch", file=sys.stderr)
        return 1
    if args.doi != DATASET_DOI:
        print(f"NOTE: stamping {args.doi!r}, not the configured {DATASET_DOI!r}")
    print(f"{'checking' if args.check else 'patching'} {len(work)} object(s) with DOI {args.doi}")

    rows = [patch_one(c, f, p, args.doi, args.check, not args.no_backup) for c, f, p in work]
    df = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(df.groupby(["flavour", "status"]).size().to_string())
    print(f"\nattribute change sets:\n{df['changed_attributes'].value_counts().to_string()}")

    failed = df[df["status"].str.startswith("FAILED")]
    if len(failed):
        print(f"\n{len(failed)} failure(s):", file=sys.stderr)
        for _, r in failed.iterrows():
            print(f"  {r['flavour']:8s} {r['case_id']}: {r['status']}", file=sys.stderr)
        return 1
    if not args.check:
        n = int((df["status"] == "patched").sum())
        print(f"\n{n} object(s) patched. Verify with --audit, then --drop-backups.")
        print("The DICOM store still holds the unpatched objects -- see docs/conversion-notes.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
