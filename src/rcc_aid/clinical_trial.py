#!/usr/bin/env python
"""Stamp the published dataset's DOI, and the Clinical Trial Subject module it belongs to.

Why
---
It is IDC convention to record the DOI of the published dataset in the DICOM metadata, as one item
of OtherClinicalTrialProtocolIDsSequence (0012,0023) issued by "DOI". Every IDC dataset that carries
a DOI this way does it identically -- verified against `bigquery-public-data.idc_current.dicom_all`
at IDC v24, 27 distinct DOIs over 74574 series:

    (0012,0023) OtherClinicalTrialProtocolIDsSequence
      (0012,0020) ClinicalTrialProtocolID           10.5281/zenodo.22284133
      (0012,0022) IssuerOfClinicalTrialProtocolID   DOI

dcmqi has no way to express this -- its metadata JSON covers ClinicalTrialSeriesID,
ClinicalTrialTimePointID and ClinicalTrialCoordinatingCenterName and nothing else in group 0012 --
so the stamp is applied to the object dcmqi wrote, with pydicom. rcc_aid.convert does it inline
after every successful conversion; scripts/patch_clinical_trial.py does it to objects converted
before this existed. Both call stamp() here, so there is one definition of what gets written.

The DOI alone is not enough
---------------------------
(0012,0023) and its issuer (0012,0022) live in the **Clinical Trial Subject** module, alongside
Type 1 ClinicalTrialSponsorName / ClinicalTrialProtocolID and Type 2 ClinicalTrialSiteID /
ClinicalTrialSiteName. Writing the sequence on its own leaves the module incomplete, and dciodvfy
then declines to recognise the module at all, reporting the attributes as extras:

    Warning - Attribute is not present in standard DICOM IOD - (0x0012,0x0023) SQ Other Clinical ...
    Warning - Dicom dataset contains attributes not present in standard DICOM IOD -
              this is a Standard Extended SOP Class

which is why the IDC objects that carry a DOI also carry the rest of the module -- including
zero-length ClinicalTrialSiteID / ClinicalTrialSiteName where the site is unknown, which is what
Type 2 asks for. With the module complete, dciodvfy reports exactly the baseline warnings and the
object stays a plain Segmentation SOP Class. See docs/decisions.md ADR-013.

Where the values come from
--------------------------
Not invented: IDC already publishes a sponsor, protocol and tissue source site for all 91 of these
patients in the source collections, and metadata/tcga_sites.csv is a snapshot of exactly that (see
sql/tcga_sites.sql). A consumer therefore sees the same trial identity on the segmentation as on
the images it describes. This mirrors tcga_sbu_til_maps, IDC's other derived SEG dataset over TCGA,
which carries the TCGA protocol of its source collection plus its own DOI in (0012,0023).

    uv run python -c "from rcc_aid.clinical_trial import summary; print(summary())"
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pydicom
from pydicom.dataset import Dataset

ROOT = Path(__file__).resolve().parents[2]
SITES_CSV = ROOT / "metadata" / "tcga_sites.csv"

# DOI of the converted dataset -- the DICOM SEG objects this pipeline produces, not the RCC-AID
# NIfTI release they derive from (that one is 10.5281/zenodo.20719257, recorded separately in
# submission/rcc_aid.yaml as source_doi and as an isDerivedFrom related identifier).
#
# Reserved on Zenodo; it does not resolve until the record is published.
DATASET_DOI = "10.5281/zenodo.22284133"

# Bare, not "doi:"-prefixed. IDC carries both forms -- 17 distinct DOIs prefixed, 10 bare, at v24 --
# but the bare form is what the recent additions use (nlstseg, cgci_htmcp_lc, cddp_eagle_1,
# htan_tnp_sardana), and the issuer already says the string is a DOI.
DOI_ISSUER = "DOI"

# Attributes this module owns. Anything here is fully determined by the values below plus the
# patient, so stamping is idempotent and a patched object is indistinguishable from a freshly
# converted one. Deliberately NOT including (0012,0050) ClinicalTrialTimePointID, (0012,0060)
# ClinicalTrialCoordinatingCenterName or (0012,0071) ClinicalTrialSeriesID: those belong to the
# Clinical Trial Study and Clinical Trial Series modules and dcmqi already writes them from
# data/meta/*.json.
OWNED_ATTRS = [
    "ClinicalTrialSponsorName",       # (0012,0010) Type 1
    "ClinicalTrialProtocolID",        # (0012,0020) Type 1
    "ClinicalTrialProtocolName",      # (0012,0021) Type 2
    "ClinicalTrialSiteID",            # (0012,0030) Type 2
    "ClinicalTrialSiteName",          # (0012,0031) Type 2
    "ClinicalTrialSubjectID",         # (0012,0040) Type 1C
    "OtherClinicalTrialProtocolIDsSequence",  # (0012,0023) Type 3
]

_sites: pd.DataFrame | None = None


def sites() -> pd.DataFrame:
    """The IDC site table, indexed by PatientID. Loaded once, validated on first use."""
    global _sites
    if _sites is None:
        if not SITES_CSV.exists():
            raise SystemExit(
                f"missing {SITES_CSV.relative_to(ROOT)}; regenerate it with sql/tcga_sites.sql"
            )
        df = pd.read_csv(SITES_CSV, dtype=str).fillna("")
        dup = df[df.duplicated("PatientID", keep=False)]["PatientID"].unique()
        if len(dup):
            # One patient with two different sites would make the stamp depend on row order.
            raise SystemExit(
                f"{SITES_CSV.relative_to(ROOT)}: {len(dup)} patient(s) with conflicting sites: "
                f"{', '.join(sorted(dup)[:5])}"
            )
        _sites = df.set_index("PatientID")
    return _sites


def attributes(patient_id: str, doi: str = DATASET_DOI) -> dict:
    """Every attribute this module writes, for one patient. Raises if the patient is unknown.

    Failing loudly matters: falling back to a blank sponsor and protocol would silently emit an
    incomplete Type 1 module, which is the exact defect this module exists to avoid.
    """
    table = sites()
    if patient_id not in table.index:
        raise KeyError(
            f"{patient_id} is not in {SITES_CSV.relative_to(ROOT)} -- refresh it from "
            f"sql/tcga_sites.sql, or the object is not from a TCGA renal collection"
        )
    row = table.loc[patient_id]
    return {
        "ClinicalTrialSponsorName": row["ClinicalTrialSponsorName"],
        "ClinicalTrialProtocolID": row["ClinicalTrialProtocolID"],
        "ClinicalTrialProtocolName": row["ClinicalTrialProtocolName"],
        "ClinicalTrialSiteID": row["ClinicalTrialSiteID"],
        "ClinicalTrialSiteName": row["ClinicalTrialSiteName"],
        # Type 1C: required when the subject is identified by a trial ID. IDC sets it equal to
        # PatientID throughout the TCGA collections, so this asserts nothing new.
        "ClinicalTrialSubjectID": patient_id,
        "OtherClinicalTrialProtocolIDsSequence": doi,
    }


def doi_sequence(doi: str = DATASET_DOI) -> list[Dataset]:
    item = Dataset()
    item.ClinicalTrialProtocolID = doi
    item.IssuerOfClinicalTrialProtocolID = DOI_ISSUER
    return [item]


def current(ds: pydicom.Dataset) -> dict:
    """What the dataset carries today, in the same shape attributes() returns."""
    out = {}
    for name in OWNED_ATTRS:
        if name == "OtherClinicalTrialProtocolIDsSequence":
            seq = ds.get(name)
            out[name] = str(seq[0].ClinicalTrialProtocolID) if seq else None
        else:
            val = ds.get(name)
            out[name] = None if val is None else str(val)
    return out


def stamp(ds: pydicom.Dataset, doi: str = DATASET_DOI) -> tuple[str, dict]:
    """Write the module into `ds` in place.

    Returns (verdict, before) where verdict is 'unchanged' if the object already said exactly this,
    and 'stamped' otherwise. `before` is the prior value of every owned attribute, which is what
    lets the caller report precisely what it changed.
    """
    want = attributes(str(ds.PatientID), doi)
    before = current(ds)

    for name in OWNED_ATTRS:
        if name == "OtherClinicalTrialProtocolIDsSequence":
            # Replace rather than append: one dataset, one DOI. Appending would leave a superseded
            # DOI in place on a re-stamp, and nothing downstream would know which item was current.
            ds.OtherClinicalTrialProtocolIDsSequence = doi_sequence(want[name])
        else:
            setattr(ds, name, want[name])

    return ("unchanged" if before == want else "stamped"), before


def stamp_file(path: Path, doi: str = DATASET_DOI) -> tuple[str, dict]:
    """Stamp a DICOM file in place, preserving its transfer syntax and everything else.

    Written to a sibling temp file and moved into place, so an interrupted run cannot leave a
    truncated object where a valid one was. pydicom preserves File Meta verbatim, including the
    Implementation Class UID dcmqi's DCMTK wrote -- unlike dcmodify, which restamps it with
    whichever DCMTK happens to be on the machine running the patch.
    """
    ds = pydicom.dcmread(str(path))
    verdict, before = stamp(ds, doi)
    tmp = path.with_suffix(path.suffix + ".stamping")
    try:
        ds.save_as(str(tmp))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return verdict, before


def stamped_doi(path: Path) -> str:
    """The DOI an object on disk carries, or '' if it carries none. Header read only."""
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    seq = ds.get("OtherClinicalTrialProtocolIDsSequence")
    return str(seq[0].ClinicalTrialProtocolID) if seq else ""


def summary() -> str:
    table = sites()
    protocols = (table.groupby(["ClinicalTrialProtocolID", "ClinicalTrialProtocolName"])
                 .size().to_string())
    return (f"DATASET_DOI = {DATASET_DOI!r} (issuer {DOI_ISSUER!r})\n"
            f"{len(table)} patients in {SITES_CSV.relative_to(ROOT)}\n{protocols}")


if __name__ == "__main__":
    print(summary())
