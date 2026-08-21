#!/usr/bin/env python
"""Drive dcmqi's itkimage2segimage to produce DICOM SEG for every resolved case.

Two flavours per case (docs/decisions.md ADR-001):
  labelmap -> data/seg_labelmap/<case_id>.dcm   (the deliverable)
  binary   -> data/seg_binary/<case_id>.dcm     (validation oracle)

Reference instances come from data/refs/<case_id>.txt, produced by rcc_aid.resolve_references, and are
passed via --inputDICOMList. dcmqi's own --referencesGeometryCheck stays enabled.

    uv run python -m rcc_aid.convert
    uv run python -m rcc_aid.convert --cases <case_id> ... --flavours labelmap
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

# dcmqi is a subprocess, so threads are the right pool here. Physical cores, not logical: each dcmqi
# run is CPU- and memory-bandwidth-bound, and hyperthreads buy little.
DEFAULT_JOBS = max(1, (os.cpu_count() or 4) // 2)

ROOT = Path(__file__).resolve().parents[2]
CASE_INDEX = ROOT / "qc" / "case_index.csv"
RESOLUTION_CSV = ROOT / "qc" / "reference_resolution.csv"
# Normalized labels, not the raw Zenodo NIfTI: 13 files need integer casting and a z-flip before they
# describe the DICOM correctly. See rcc_aid.normalize_labels and docs/conversion-notes.md.
LABELS_DIR = ROOT / "data" / "labels_norm"
META_DIR = ROOT / "data" / "meta"
REFS_DIR = ROOT / "data" / "refs"
OUT_DIRS = {"labelmap": ROOT / "data" / "seg_labelmap", "binary": ROOT / "data" / "seg_binary"}
LOG_DIR = ROOT / "data" / "convert_logs"
OUT_CSV = ROOT / "qc" / "conversion_report.csv"

EXPECTED_DCMQI = "v1.5.7"

# Per-flavour PixelData compression. See docs/decisions.md ADR-010.
#   labelmap -> deflate: labelmap PixelData is 8 bits/voxel over every frame (vs 1-bit packed for
#               binary), so uncompressed it can exceed the binary equivalent. Deflate is lossless.
#   binary   -> none:    this is the QC oracle, so keep it in plain Explicit VR Little Endian, which
#               every viewer reads without question.
COMPRESS = {"labelmap": "deflate", "binary": "none"}

# Empty-frame handling, stated explicitly rather than left to dcmqi's default. See ADR-010.
#   labelmap -> 0: keep every frame. The result is a complete, contiguous volume that does not depend
#               on dcmqi refilling absent slice positions at read time, and the empty frames are
#               uniform PixelPaddingValue, so deflate reduces them to almost nothing.
#   binary   -> 1: essential here. A kidney segmentation touches a minority of the slices in an
#               abdominal CT, and BINARY writes one frame per segment per slice; encoding the empty
#               ones would multiply the object size for no information. Uncompressed by design, so
#               there is no deflate to absorb them either.
SKIP_EMPTY = {"labelmap": "0", "binary": "1"}


def dcmqi_bin(name: str) -> str:
    """Resolve from the installed dcmqi package, never from PATH.

    PATH resolution is actively dangerous here: an older dcmqi elsewhere on the system (this machine
    has revision 011237d in ~/local/bin, which has no --segmentationType at all) would shadow the
    pinned dependency and silently change the output. Only fall back to PATH if the package layout
    is unexpected, and let assert_dcmqi_version() catch it either way.
    """
    import dcmqi

    exe = Path(dcmqi.__file__).parent / "bin" / name
    if exe.is_file():
        return str(exe)
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"{name} not found in the dcmqi package or on PATH")
    return found


def assert_dcmqi_version() -> str:
    out = subprocess.run([dcmqi_bin("itkimage2segimage"), "--version"],
                         capture_output=True, text=True).stdout
    first = out.strip().splitlines()[0] if out.strip() else ""
    if EXPECTED_DCMQI not in first:
        raise SystemExit(
            f"dcmqi version mismatch (ADR-009 pins {EXPECTED_DCMQI}); got: {first!r}"
        )
    print(f"dcmqi: {first}")
    return first


def convert_one(case_id: str, flavour: str) -> dict:
    label = LABELS_DIR / f"{case_id}_seg.nrrd"
    meta = META_DIR / (f"{case_id}.json" if flavour == "labelmap" else f"{case_id}.binary.json")
    refs = REFS_DIR / f"{case_id}.txt"
    out = OUT_DIRS[flavour] / f"{case_id}.dcm"

    for p in (label, meta, refs):
        if not p.exists():
            return {"case_id": case_id, "flavour": flavour, "status": "missing_input",
                    "detail": f"missing {p.relative_to(ROOT)}"}

    dicom_list = [ln for ln in refs.read_text().splitlines() if ln.strip()]
    cmd = [
        dcmqi_bin("itkimage2segimage"),
        "--inputImageList", str(label),
        "--inputMetadata", str(meta),
        "--inputDICOMList", ",".join(dicom_list),
        "--outputDICOM", str(out),
        "--segmentationType", flavour,
        "--useLabelIDAsSegmentNumber",
        "--compress", COMPRESS[flavour],
        "--skip", SKIP_EMPTY[flavour],
        "--verbose",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0

    log = LOG_DIR / f"{case_id}.{flavour}.log"
    log.write_text(
        "$ " + " ".join(cmd[:3] + ["..."] + cmd[-8:]) + f"\n\n[stdout]\n{proc.stdout}\n"
        f"[stderr]\n{proc.stderr}\n[returncode] {proc.returncode}\n"
    )

    ok = proc.returncode == 0 and out.exists() and out.stat().st_size > 0
    return {
        "case_id": case_id,
        "flavour": flavour,
        "status": "ok" if ok else "failed",
        "returncode": proc.returncode,
        "n_reference_instances": len(dicom_list),
        "output": str(out.relative_to(ROOT)) if ok else "",
        "output_bytes": out.stat().st_size if ok else 0,
        "compress": COMPRESS[flavour],
        "skip_empty": SKIP_EMPTY[flavour],
        "seconds": round(elapsed, 2),
        "log": str(log.relative_to(ROOT)),
        "detail": "" if ok else (proc.stderr.strip().splitlines() or ["(no stderr)"])[-1][:300],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*", help="restrict to these case_ids (pilot)")
    ap.add_argument("--flavours", nargs="*", default=["labelmap", "binary"],
                    choices=["labelmap", "binary"])
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help=f"concurrent dcmqi processes (default {DEFAULT_JOBS})")
    ap.add_argument("--skip-existing", action="store_true",
                    help="leave already-written outputs alone (resume a partial run)")
    args = ap.parse_args()

    assert_dcmqi_version()

    if not RESOLUTION_CSV.exists():
        print(f"missing {RESOLUTION_CSV}; run rcc_aid.resolve_references first", file=sys.stderr)
        return 1
    res = pd.read_csv(RESOLUTION_CSV)
    resolved = res[res["status"] == "resolved"]
    skipped = res[res["status"] != "resolved"]

    cases = list(resolved["case_id"])
    if args.cases:
        cases = [c for c in cases if c in set(args.cases)]
        unresolvable = set(args.cases) - set(resolved["case_id"])
        for c in sorted(unresolvable):
            print(f"  skipping {c}: not resolved", file=sys.stderr)
    if not cases:
        print("no resolved cases to convert", file=sys.stderr)
        return 1

    work = [(c, f) for c in cases for f in args.flavours]
    preexisting: list[dict] = []
    if args.skip_existing:
        todo = []
        for c, f in work:
            out = OUT_DIRS[f] / f"{c}.dcm"
            if out.exists():
                # Record a row anyway: a report that silently omits objects present on disk reads as
                # a smaller run rather than a resumed one.
                refs = REFS_DIR / f"{c}.txt"
                n_ref = len([ln for ln in refs.read_text().splitlines() if ln.strip()]) \
                    if refs.exists() else 0
                preexisting.append({
                    "case_id": c, "flavour": f, "status": "ok", "returncode": 0,
                    "n_reference_instances": n_ref,
                    "output": str(out.relative_to(ROOT)), "output_bytes": out.stat().st_size,
                    "compress": COMPRESS[f], "skip_empty": SKIP_EMPTY[f],
                    "seconds": float("nan"), "log": "",
                    "detail": "pre-existing output, not reconverted this run",
                })
            else:
                todo.append((c, f))
        print(f"resuming: {len(preexisting)} output(s) already present, {len(todo)} to do")
        work = todo

    print(f"converting {len(cases)} case(s) x {len(args.flavours)} flavour(s) "
          f"= {len(work)} object(s), {args.jobs} job(s) in parallel")
    if len(skipped):
        print(f"({len(skipped)} case(s) excluded: unresolved references)")

    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(convert_one, c, f): (c, f) for c, f in work}
        for fut in as_completed(futures):
            r = fut.result()
            rows.append(r)
            done += 1
            mark = "ok " if r["status"] == "ok" else "FAIL"
            print(f"  [{done}/{len(work)}] {mark} {r['flavour']:8s} {r['case_id']}"
                  + ("" if r["status"] == "ok" else f"  <- {r['detail']}"), flush=True)

    df = pd.DataFrame(rows + preexisting)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Always preserve results for (case, flavour) pairs this run did not touch -- runs can be
    # partial via --cases, --flavours or --skip-existing.
    if OUT_CSV.exists() and len(df):
        prev = pd.read_csv(OUT_CSV)
        key = ["case_id", "flavour"]
        prev = prev.merge(df[key], on=key, how="left", indicator=True)
        prev = prev[prev["_merge"] == "left_only"].drop(columns="_merge")
        df = pd.concat([prev, df], ignore_index=True)
    df = df.sort_values(["case_id", "flavour"]).reset_index(drop=True)
    df.to_csv(OUT_CSV, index=False)

    print(f"\nwrote {OUT_CSV.relative_to(ROOT)}")
    print(df.groupby(["flavour", "status"]).size().to_string())
    failed = df[df["status"] != "ok"]
    if len(failed):
        print(f"\n{len(failed)} conversion failure(s):", file=sys.stderr)
        for _, r in failed.iterrows():
            print(f"  {r['flavour']:8s} {r['case_id']}: {r['detail']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
