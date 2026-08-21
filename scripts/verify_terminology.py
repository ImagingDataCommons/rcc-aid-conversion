#!/usr/bin/env python
"""Verify every code tuple in src/rcc_aid/terminology.py against DICOM PS3.16.

Source of truth: dcmterms (https://fedorov.github.io/dcmterms/terminology.html), queried in place --
no download. Fails non-zero if a code value, coding scheme, meaning or CID membership has drifted.

    uv run python scripts/verify_terminology.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rcc_aid.terminology import SEGMENTS  # noqa: E402

CODED_ENTRIES = "https://fedorov.github.io/dcmterms/data/coded_entries.parquet"

# Codes asserted NOT to exist in Part 16, i.e. the dcmqi divergences documented in
# docs/terminology.md. If one of these ever appears upstream, the docs need revisiting.
ABSENT_CODES = {
    "123037004": 'dcmqi master list uses this for "Anatomical Structure" (ADR-003)',
    "41607009": '"Renal cell carcinoma", considered and rejected for label 2 (ADR-004)',
}


def main() -> int:
    con = duckdb.connect()
    entries = con.execute(
        f"""
        SELECT CAST(code_value AS VARCHAR) AS cv, coding_scheme_designator AS scheme,
               code_meaning AS meaning, cid_number
        FROM read_parquet('{CODED_ENTRIES}')
        """
    ).fetchall()

    # (code_value, scheme) -> {cid: {meanings}}
    index: dict[tuple[str, str], dict[int, set[str]]] = {}
    all_values: set[str] = set()
    for cv, scheme, meaning, cid in entries:
        all_values.add(cv)
        index.setdefault((cv, scheme), {}).setdefault(cid, set()).add(meaning)

    failures: list[str] = []
    checks = 0

    for label_id, seg in sorted(SEGMENTS.items()):
        targets = [("category", seg.category), ("type", seg.seg_type)]
        if seg.anatomic_region is not None:
            targets.append(("anatomic_region", seg.anatomic_region))

        for role, code in targets:
            checks += 1
            key = (code["CodeValue"], code["CodingSchemeDesignator"])
            where = f"label {label_id} ({seg.name}) {role} {key[0]}/{key[1]}"

            if key not in index:
                failures.append(f"{where}: not present anywhere in DICOM Part 16")
                continue

            cids = index[key]
            expected_cid = seg.expected_cids.get(role)
            if expected_cid is not None:
                if expected_cid not in cids:
                    failures.append(
                        f"{where}: expected membership in CID {expected_cid}, "
                        f"found only CIDs {sorted(cids)}"
                    )
                    continue
                meanings = cids[expected_cid]
                if code["CodeMeaning"] not in meanings:
                    failures.append(
                        f'{where}: CID {expected_cid} gives meaning {sorted(meanings)}, '
                        f'we assert "{code["CodeMeaning"]}"'
                    )
                    continue
                print(
                    f'  OK  {where} -> CID {expected_cid}, "{code["CodeMeaning"]}"'
                )
            else:
                # AnatomicRegionSequence: CID 4 labels 64033007 "Endo-renal"; we intentionally write
                # the CID 7154 meaning. Assert the code exists, not the meaning. See ADR-004 notes.
                print(f"  OK  {where} -> present in CIDs {sorted(cids)} (meaning not asserted)")

    print()
    for cv, why in ABSENT_CODES.items():
        checks += 1
        if cv in all_values:
            failures.append(
                f"{cv} is now PRESENT in Part 16 -- docs/terminology.md needs revisiting ({why})"
            )
        else:
            print(f"  OK  {cv} absent from Part 16 as documented ({why})")

    print(f"\n{checks} checks, {len(failures)} failures")
    for f in failures:
        print(f"  FAIL  {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
