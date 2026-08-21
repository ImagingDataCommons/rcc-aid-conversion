# RCC-AID → DICOM LABELMAP Segmentation conversion

## Context

[RCC-AID](https://zenodo.org/records/20719257) (Zenodo DOI `10.5281/zenodo.20719257`, v2.0.0) publishes
voxel-level kidney / tumor / cyst annotations for **129 CT scans from 91 patients** across three TCGA
renal cell carcinoma cohorts (TCGA-KIRC, TCGA-KIRP, TCGA-KICH). The annotations ship as **NIfTI**
label maps, which severs them from the DICOM images they describe: they cannot be loaded in DICOM
viewers, carry no standard terminology for what was annotated, and cannot be queried alongside the
images in IDC.

All 129 referenced CT series are already in IDC (verified — see below), so the annotations can be
re-expressed as DICOM Segmentation objects that point back at the exact source instances. This project
converts them to **DICOM LABELMAP SEG** using **dcmqi**, codes the three structures with SNOMED
tuples sourced from **[dcmterms](https://fedorov.github.io/dcmterms/terminology.html)**, and prepares
an **idc-comet** submission package so the result can be ingested as an IDC analysis result.

Outcome: a git repo in `/Users/af61/github/rcc-aid-conversion` holding the pipeline, the terminology
and design decisions, per-case QC/provenance, and the generated DICOM objects.

---

## Verified groundwork

Everything below was confirmed during planning — treat as established, do not re-derive.

### Zenodo record contents

| File | Size | Needed? |
|---|---|---|
| `labels.zip` | 4.1 MB | **yes** — the NIfTI annotations |
| `metadata.csv` | 72.9 kB (318 rows) | **yes** — TCIA series metadata + added `phase_name` column |
| `manifest.tcia` | 8.6 kB | **yes** — exactly 129 `SeriesInstanceUID`s |
| `images.zip` | 4.1 GB | **no** — NIfTI copies of images; we pull DICOM from IDC instead |

`metadata.csv` is a TCIA series-metadata export (`PatientID`, `StudyInstanceUID`, `SeriesInstanceUID`,
`Collection`, `SeriesDescription`, `BodyPartExamined`, `SliceThickness(mm)`, `ImageCount`,
`LicenseName`, `DataDescriptionURI`, …) plus `phase_name` ∈ {arterial, venous, delayed, …}. It has 318
rows — a superset of the 129 annotated series.

### Label file naming — the join key

```
labels/{KICH|KIRC|KIRP}_{SeriesInstanceUID with '.' → '_'}_acq_{N|unknown}_seg.nii.gz
```

e.g. `KICH_1_3_6_1_4_1_14519_5_2_1_1357_4011_265058192222365034582861098862_acq_12_seg.nii.gz`

Recovered by: strip cohort prefix, split on `_acq`, replace `_` → `.`. Label values:
**0 = Background, 1 = Kidney, 2 = Tumor, 3 = Cyst.**

### Source imaging in IDC — all present

Queried via the IDC MCP against all 129 manifest UIDs:

| | |
|---|---|
| UIDs in manifest | 129 |
| Found in IDC `index` | **129 / 129** |
| Distinct patients | 91 (matches Zenodo) |
| Collections | `tcga_kirc`, `tcga_kirp`, `tcga_kich` |
| Total instances | 16,055 |
| Download size | **8.27 GB** (70 GB free on this machine) |
| `regularly_spaced_3d_volume` = true | **108** |
| `regularly_spaced_3d_volume` = false | **21** |
| Oblique acquisitions | 0 |

The 21 irregular series fail on `unique_slice_positions` and/or `uniform_slice_spacing` — never on
obliquity or pixel spacing. These are **multi-acquisition/multi-phase series bundled into one DICOM
series**, which is precisely why the upstream dataset split its NIfTIs by `AcquisitionNumber`. This is
the central technical risk and drives step 5 below.

### How the upstream NIfTIs were made

[`dcm2nii.py`](https://github.com/DIAGNijmegen/oncology-RCC-AID-codebase/blob/main/src/oncology_rcc_aid_codebase/dcm2nii.py):
**SimpleITK `ImageSeriesReader`**, grouped by `AcquisitionNumber`, dropping slices whose `Rows`/`Columns`
disagree within a group. **No resampling, no reorientation.** Geometry is therefore preserved, and the
label grid is reproducible from the DICOM — which is what makes an exact `--inputDICOMList` possible.

### dcmqi

- Locally installed `/Users/af61/local/bin/itkimage2segimage` is revision `011237d` and **has no
  `--segmentationType` flag** — it cannot emit LABELMAP. Must upgrade.
- LABELMAP landed upstream in **v1.5.4** (2026-05-18); **v1.5.5** switched labelmap background to
  `PixelPaddingValue (0028,0120)` instead of an inserted Background segment, and added gapped segment
  numbers for `--useLabelIDAsSegmentNumber`. Latest is **v1.5.7** (2026-08-11).
- Two ways to get it, both fine on this x86_64 Mac: Docker `qiicr/dcmqi:v1.5.7` (amd64, native) or the
  release tarball `dcmqi-1.5.7-mac-x86_64.tar.gz`. **Pin the version either way.**
- Relevant flags: `--segmentationType labelmap|binary`, `--useLabelIDAsSegmentNumber`,
  `--inputDICOMList`, `--referencesGeometryCheck`, `--skip`, `--compress`.

### LABELMAP is new territory

IDC today holds **172,908 BINARY** and **17,238 FRACTIONAL** segmentation series and **zero LABELMAP**.
This conversion would be the first. Hence the decision to emit BINARY alongside as a validation oracle.

### IDC precedent for these exact structures

- `c4kc_kits` — 210 series, `Kidney | Mass`
- `bamf_aimi_annotations` — 244 series over `tcga_kirc`/`tcga_kirp`/`tcga_kich` among others, coding
  `Kidney | Mass | Cyst` with `AnatomicRegionSequence = Kidney`

---

## Terminology decisions

Sourced from **dcmterms** (`https://fedorov.github.io/dcmterms/data/coded_entries.parquet`, queryable
directly with DuckDB — no download needed), which is extracted from DICOM PS3.16.

| Label | Segment | Category (CID 7150) | Type | Anatomic Region |
|---|---|---|---|---|
| 1 | Kidney | `(91723000, SCT, "Anatomical Structure")` | `(64033007, SCT, "Kidney")` — CID 7154 | — |
| 2 | Tumor | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(4147007, SCT, "Mass")` — CID 7159 | `(64033007, SCT, "Kidney")` |
| 3 | Cyst | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(367643001, SCT, "Cyst")` — CID 7159 | `(64033007, SCT, "Kidney")` |

**Tumor → `Mass`** (your call): matches `c4kc_kits` + `bamf_aimi_annotations`, so RCC-AID answers the
same IDC queries as 454 existing kidney-mass series, and keeps the SEG faithful to what was read off
the CT rather than asserting histology from pathology.

### Two discrepancies to record in `docs/terminology.md`

1. **"Anatomical Structure" is `91723000`, not `123037004`.** dcmqi's bundled
   `SegmentationCategoryTypeModifier-DICOM-Master.json` uses `123037004`, which occurs **zero times**
   anywhere in DICOM Part 16. dcmterms/CID 7150 gives `(91723000, SCT, "Anatomical Structure")`
   (SNOMED-RT `T-D0005`, UMLS `C1268086`). Existing dcmqi-generated IDC content almost certainly
   carries `123037004`. **Go with `91723000`** per the authoritative source, and document the
   divergence explicitly — flag it upstream to dcmqi as a follow-up.
2. **`49755003` means "Morphologically Abnormal Structure"** in CID 7150. dcmqi's list and IDC's
   `seg_index` both report "Morphologically **Altered** Structure". Same CodeValue + scheme, so
   identity and interoperability are unaffected — only the informative meaning string differs. Use the
   DICOM spelling.

Also worth a footnote: dcmqi lists `Neoplasm, Primary` as `86049000`; CID 7159 says `372087000`. Not
used here, but the same class of drift. And CID 4 (Anatomic Region) lists `64033007` with the meaning
`"Endo-renal"` — for `AnatomicRegionSequence` use `(64033007, SCT, "Kidney")`, meaning being
informative only.

### Open item to resolve in step 1

`SegmentAlgorithmType` is required by dcmqi's schema. Zenodo credits Ziegelmayer and Russo as
"Annotator", implying manual work, but the associated medRxiv preprint may describe AI-assisted
pre-segmentation. **Read the preprint and set `MANUAL` or `SEMIAUTOMATIC` (+ `SegmentAlgorithmName`)
accordingly; default to `MANUAL` if unstated.** Record the finding in `docs/decisions.md`.

---

## Repository layout

```
rcc-aid-conversion/
├── README.md                        # what this is, how to run it, provenance
├── .gitignore                       # data/ excluded
├── pyproject.toml                   # uv-managed; SimpleITK, pydicom, pandas, duckdb, idc-index
├── docs/
│   ├── terminology.md               # code tuples, dcmterms provenance, the two discrepancies
│   ├── decisions.md                 # ADR log (LABELMAP+BINARY, Mass, 91723000, acq handling, …)
│   └── conversion-notes.md          # geometry/acquisition handling + per-case exceptions
├── metadata/
│   └── seg_template.json            # dcmqi metadata template, 3 segments
├── src/rcc_aid/
│   ├── fetch_inputs.py              # Zenodo files + md5 verification
│   ├── build_case_index.py          # filenames → (UID, acq); join metadata.csv + IDC index
│   ├── fetch_dicom.py               # pull the 129 series from IDC
│   ├── resolve_references.py        # ← the crux: exact ordered instance list per label file
│   ├── make_metadata.py             # per-case dcmqi metadata JSON
│   ├── convert.py                   # itkimage2segimage driver (labelmap + binary)
│   └── qc.py                        # geometry, round-trip, label stats, validator
├── scripts/run_all.sh               # end-to-end, pilot or full
├── submission/
│   └── rcc_aid.yaml                 # idc-comet analysis_result YAML
├── qc/                              # committed CSV/JSON QC reports (small)
└── data/                            # GITIGNORED
    ├── zenodo/  dicom/  seg_labelmap/  seg_binary/
```

Known md5s for input verification: `labels.zip` `6d453071037640a79042033723442c1a`,
`metadata.csv` `7297e5ea8201eddb0c3aae55c1068952`, `manifest.tcia` `adfa32c16ef9287711cab0702083f3e7`.

---

## Implementation

### 1. Repo + decisions skeleton
`git init`, layout above, `.gitignore` for `data/`. Write `docs/terminology.md` from the table above
(the code tuples are already verified — no re-lookup needed, but include the DuckDB one-liner against
`coded_entries.parquet` so it is reproducible). Resolve the `SegmentAlgorithmType` open item.

### 2. Acquire inputs
Download `labels.zip`, `metadata.csv`, `manifest.tcia` from Zenodo; verify md5s; unzip. **Skip
`images.zip`.** Assert the label file count matches the 129 manifest UIDs and report any mismatch
rather than proceeding silently.

### 3. Build the case index
Parse each label filename → `(cohort, SeriesInstanceUID, acq_token)`. Left-join `metadata.csv` on
`SeriesInstanceUID`, and the IDC `index` (via `idc-index` locally, no MCP needed at runtime) for
`collection_id`, `PatientID`, `StudyInstanceUID`, `instanceCount`, `series_aws_url`. Also join
`volume_geometry_index` so the 21 irregular series are flagged in the index up front. Emit
`qc/case_index.csv` — one row per label file, the provenance backbone for everything downstream.

### 4. Download source DICOM
129 series / 8.27 GB into `data/dicom/<SeriesInstanceUID>/`. Use `idc-index`
(`IDCClient.download_from_selection(seriesInstanceUID=[...])`); `s5cmd` is installed as a fallback
against the S3 URLs in `index`. Report progress and total bytes; this is the only expensive step.

### 5. Resolve reference instances — the crux
For each label file, produce the **exact ordered list of DICOM files** whose grid equals the label
grid, by replicating the upstream grouping:

1. Read all instances of the series.
2. Group by `AcquisitionNumber (0020,0012)`; missing → the `unknown` group. Select the group matching
   the filename's `acq` token.
3. Within the group, drop instances whose `(Rows, Columns)` differ from the modal value (mirrors
   `dcm2nii.py`).
4. Sort by `ImagePositionPatient` projected onto the slice normal.
5. Build a SimpleITK volume from that file list and **assert** origin / spacing / direction / size
   match the label NIfTI (tolerance ~1e-3 mm, ~1e-4 on direction cosines).

On match, that list is passed verbatim to `--inputDICOMList`, which guarantees dcmqi's per-frame
references are correct even for the 21 irregular series. On mismatch, try documented fallbacks (all
instances ungrouped; `dcm2niix`-style grouping), and if none match, **record the case as failed in
`qc/reference_resolution.csv` and exclude it** — never paper over it with
`--referencesGeometryCheck 0`. Write `qc/reference_resolution.csv` (case, group, n_instances,
geometry deltas, status).

### 6. Per-case dcmqi metadata JSON
From `metadata/seg_template.json`, filling: the three segment code tuples, `SegmentAlgorithmType`,
`recommendedDisplayRGBValue` (Kidney a muted red-pink, Mass red, Cyst yellow-olive — take
`bamf`/dcmqi-consistent values), `BodyPartExamined: KIDNEY`, `ContentCreatorName`,
`ClinicalTrialSeriesID`, `SeriesNumber`, and a `SeriesDescription` naming the cohort and contrast
phase from `phase_name`. Only emit segments for label values actually present in that case (cyst is
absent in many).

### 7. Convert
```
itkimage2segimage \
  --inputImageList  data/labels/<case>_seg.nii.gz \
  --inputDICOMList  <exact ordered file list from step 5> \
  --inputMetadata   data/meta/<case>.json \
  --outputDICOM     data/seg_labelmap/<case>.dcm \
  --segmentationType labelmap \
  --useLabelIDAsSegmentNumber \
  --verbose
```
`--useLabelIDAsSegmentNumber` keeps SegmentNumber == label value (1/2/3), which matters for round-trip
fidelity and readability; our labels are consecutive from 1, so it is safe. Re-run with
`--segmentationType binary` into `data/seg_binary/`. Decide `--skip` and `--compress` empirically in
the pilot (dcmqi ≥1.5.5 fills absent labelmap slice positions with `PixelPaddingValue`, so no
Background segment is created — confirm that in the pilot output).

### 8. QC
Per case, into `qc/qc_report.csv`:
- **Round-trip**: `segimage2itkimage` on both outputs → compare voxel-wise against the input NIfTI.
  Must be **exact**; any nonzero diff is a hard failure.
- **Label stats**: per-label voxel counts, NIfTI vs LABELMAP vs BINARY — all three must agree.
- **LABELMAP vs BINARY diff**: the two flavors must decode identically. This is the whole point of
  emitting both, given zero LABELMAP precedent in IDC.
- **Header checks** (pydicom): `SegmentationType`, `SegmentNumber`↔code mapping,
  `PixelPaddingValue` presence for labelmap, `ReferencedSeriesSequence`/`ReferencedInstanceSequence`
  covering exactly the step-5 instance list, `PatientID`/`StudyInstanceUID` matching the source.
- **DICOM validation**: `dciodvfy` (dicom3tools) — not installed on this machine; obtain the binary or
  run it from a container. Record its output per case.
- **Visual spot-check**: load a few cases in 3D Slicer against the source series.

### 9. IDC submission package
Write `submission/rcc_aid.yaml` against `idc-comet`'s `schemas/analysis_result.schema.json`, modelled
on `collections/analysis/pancreas_ct_seg.yaml` (same shape: NIfTI→DICOM-SEG conversion of a
third-party annotation set) and `bamf_aimi_annotations.yaml` (same anatomy and cohorts). Required
fields: `analysis_result_id` (`rcc_aid`, must match `^[a-z][a-z0-9_]*$`), `analysis_result_name`,
`title`, `cancer_types`, `tumor_locations`, `collections`, `license`, `summary`, `description`.

Reuse `bamf_aimi_annotations.yaml`'s controlled-vocabulary values verbatim where they apply —
`cancer_types: [Kidney Renal Clear Cell Carcinoma, Kidney Renal Papillary Cell Carcinoma, Kidney
Chromophobe]`, `tumor_locations: [Kidney]`, `collections: [tcga_kirc, tcga_kirp, tcga_kich]`.
`license: CC BY 4.0` (the Zenodo annotations' license; note in the description that the underlying
TCGA images are CC BY 3.0). `source_doi: 10.5281/zenodo.20719257`. Populate `zenodo.creators` from the
eight Zenodo authors with their affiliations, and `references.related_datasets` with the three TCIA
source DOIs (`10.7937/K9/TCIA.2016.V6PBVTDR`, `10.7937/K9/TCIA.2016.ACWOGBEF`,
`10.7937/K9/TCIA.2016.YU3RBCZN`) as `relationship: source`, plus the medRxiv preprint under
`publications`.

Validate locally with idc-comet's own tooling (`uv run python scripts/validate.py <file>`) by working
in a clone of that repo — the YAML lives in our repo under `submission/` as the source of truth and is
copied over when the PR is opened. `idc-comet` is private; the `gh` CLI is authenticated as `fedorov`
with `repo` scope, so it is reachable.

---

## Verification

**Pilot first — 3 cases**, chosen deliberately:
1. a clean `tcga_kirc` case with `regularly_spaced_3d_volume = true`;
2. one of the **21 irregular** series, to prove step-5 acquisition grouping actually works;
3. an `acq_unknown` case (e.g. `KIRC_1_3_6_1_4_1_14519_5_2_1_1357_4004_118168941853469915520207083596_acq_unknown_seg.nii.gz`),
   to prove the missing-`AcquisitionNumber` path works.

Gate on the pilot: all three round-trip exactly in both flavors, `dciodvfy` clean, label stats match,
and one case visually confirmed in 3D Slicer against its source series. Only then run all 129.

**Full-run acceptance:**
- `qc/case_index.csv` has 129 rows; `qc/reference_resolution.csv` shows a resolved instance list for
  every case, or an explicitly recorded failure with its geometry deltas.
- `qc/qc_report.csv`: every converted case round-trips exactly and LABELMAP ≡ BINARY on decode.
- Any case that could not be converted is named in `docs/conversion-notes.md` with the reason — a
  partial result is fine, a silently smaller output set is not.
- `submission/rcc_aid.yaml` passes `idc-comet`'s `scripts/validate.py`.
- `README.md` states the final converted count, the dcmqi version pinned, and the exact input md5s.
