# RCC-AID → DICOM LABELMAP Segmentation

Converts the [RCC-AID](https://doi.org/10.5281/zenodo.20719257) renal cell carcinoma annotations from
NIfTI to **DICOM LABELMAP Segmentation** using [dcmqi](https://github.com/QIICR/dcmqi), coding the
segmented structures with SNOMED tuples taken from
[dcmterms](https://fedorov.github.io/dcmterms/terminology.html), and prepares an
[idc-comet](https://github.com/ImagingDataCommons/idc-comet) submission package so the result can be
ingested by [NCI Imaging Data Commons](https://imaging.datacommons.cancer.gov/).

## What the dataset is

RCC-AID v2.0.0 publishes voxel-level kidney / tumour / cyst annotations for **129 CT scans from 91
patients** across three TCGA renal cell carcinoma cohorts — clear cell (TCGA-KIRC, 85 scans), papillary
(TCGA-KIRP, 26) and chromophobe (TCGA-KICH, 18). The annotations ship as NIfTI label maps with values
`0 = Background, 1 = Kidney, 2 = Tumor, 3 = Cyst`.

As NIfTI they are disconnected from the images they describe: not loadable in DICOM viewers, carrying no
standard terminology, and not queryable alongside the imaging. **All 129 referenced CT series are
present in IDC**, so the annotations can be re-expressed as DICOM Segmentation objects that reference
the exact source instances.

## Why the conversion is not trivial

The label filenames carry an `_acq_{N|unknown}` token because upstream split each DICOM series by
`AcquisitionNumber` before converting to NIfTI. These TCGA series bundle several contrast phases into a
single series — **21 of the 129 fail IDC's `regularly_spaced_3d_volume` check** (13 on duplicate slice
positions, 8 on non-uniform spacing; none are oblique). A label map therefore describes a *subset* of
its series' instances, and pointing dcmqi at the whole series directory would emit per-frame references
to instances the annotation does not describe.

`resolve_references.py` reproduces the upstream grouping and hands dcmqi an exact instance list.
dcmqi's own geometry check stays enabled. See [ADR-005](docs/decisions.md).

## Documentation

| | |
|---|---|
| [docs/plan.md](docs/plan.md) | The project plan, with all verified groundwork |
| [docs/terminology.md](docs/terminology.md) | Code tuples, their provenance, and two places dcmqi diverges from DICOM PS3.16 |
| [docs/decisions.md](docs/decisions.md) | Decision log (ADR-001 … ADR-010) |
| [docs/conversion-notes.md](docs/conversion-notes.md) | Run results and per-case exceptions |

## Segment coding

| Label | Segment | Category | Type | Anatomic region |
|------:|---------|----------|------|-----------------|
| 1 | Kidney | `(91723000, SCT, "Anatomical Structure")` | `(64033007, SCT, "Kidney")` | — |
| 2 | Tumor | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(4147007, SCT, "Mass")` | `(64033007, SCT, "Kidney")` |
| 3 | Cyst | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(367643001, SCT, "Cyst")` | `(64033007, SCT, "Kidney")` |

`SegmentAlgorithmType = SEMIAUTOMATIC`, `SegmentAlgorithmName = "Robust Kidney Abnormality
Segmentation"` — the RCC-AID publication documents a model-initialised, human-corrected workflow, so
`MANUAL` would overstate human authorship of the contours ([ADR-008](docs/decisions.md)).

`Mass` rather than an RCC concept because that is what was annotated on CT, and because it matches the
454 kidney-mass segmentation series already in IDC (`c4kc_kits`, `bamf_aimi_annotations`)
([ADR-004](docs/decisions.md)).

## Running it

Requires [uv](https://docs.astral.sh/uv/). dcmqi is an ordinary pinned dependency — no Docker, no
vendored binaries.

```bash
uv sync                                     # installs dcmqi==1.5.7 + SimpleITK, pydicom, idc-index
source .venv/bin/activate

python scripts/verify_terminology.py        # check every code tuple against DICOM PS3.16
python -m rcc_aid.fetch_inputs              # Zenodo labels.zip + metadata.csv + manifest.tcia (4 MB)
python -m rcc_aid.build_case_index          # -> qc/case_index.csv
python -m rcc_aid.fetch_dicom --dry-run     # report download size before committing to it
python -m rcc_aid.fetch_dicom               # 8.27 GB of CT from IDC -> data/dicom/<SeriesInstanceUID>/
python -m rcc_aid.resolve_references        # -> qc/reference_resolution.csv, data/refs/*.txt
python -m rcc_aid.normalize_labels          # -> data/labels_norm/*.nrrd, qc/normalization_report.csv
python -m rcc_aid.make_metadata             # -> data/meta/*.json
python -m rcc_aid.convert                   # -> data/seg_labelmap/*.dcm, data/seg_binary/*.dcm
python -m rcc_aid.qc                        # -> qc/qc_report.csv
```

`scripts/run_all.sh` chains these. `--cases <case_id> ...` restricts the per-case steps to a pilot
subset, and `--jobs N` (or `RCC_JOBS=N`) sets how many dcmqi processes run at once — `convert` and `qc`
default to the physical core count.

`normalize_labels` is not cosmetic: 13 of the 129 published label maps must be repaired before they
describe the DICOM correctly ([ADR-011](docs/decisions.md)).

To review the output as files rather than in a viewer:

```bash
python scripts/export_review_tree.py --out review/pilot --ct series --cases <case_id> ...
```

which lays the objects out as `PatientID / Modality / SeriesName`, with the original published NIfTI
and the normalized NRRD alongside for comparison.

`--ct series` also copies the segmented CT, making each patient folder self-contained enough to load
in 3D Slicer and check that the segmentation overlays the images. It is off by default because the CT
dominates the size — ~292 MB for the three pilot cases against 9.9 MB without it, and ~8 GB for all
129. `--ct referenced` copies only the instances the segmentation references, which for a
multi-acquisition series is the subset that loads as a single regularly-spaced volume.

`images.zip` (4.1 GB) is deliberately **not** downloaded — it holds NIfTI copies of imaging that IDC
already serves as DICOM ([ADR-002](docs/decisions.md)).

## Outputs

Two flavours per case ([ADR-001](docs/decisions.md)): `labelmap` is the deliverable; `binary` is the
validation oracle, since IDC holds no LABELMAP series to check against and dcmqi's labelmap support is
recent. `qc.py` requires both to round-trip *exactly* against the source NIfTI and to decode
identically to each other.

```
data/                       # gitignored; regenerate with the commands above
  labels/                   #   129 NIfTI label maps from Zenodo
  dicom/<SeriesUID>/        #   source CT from IDC
  refs/<case_id>.txt        #   resolved reference instance list per case
  meta/<case_id>.json       #   dcmqi metadata per case
  seg_labelmap/*.dcm        #   deliverable: LABELMAP SEG, deflate, all frames
  seg_binary/*.dcm          #   oracle: BINARY SEG, uncompressed, empty frames skipped
qc/                         # committed
  case_index.csv            #   one row per case: provenance backbone
  reference_resolution.csv  #   which instances each label map maps to, and geometry deltas
  conversion_report.csv     #   dcmqi outcome per case and flavour
  qc_report.csv             #   round-trip, geometry, label stats, header checks
submission/rcc_aid.yaml     # idc-comet analysis_result metadata
```

## Provenance

Input files, verified by md5 against the Zenodo record:

| File | md5 |
|---|---|
| `labels.zip` | `6d453071037640a79042033723442c1a` |
| `metadata.csv` | `7297e5ea8201eddb0c3aae55c1068952` |
| `manifest.tcia` | `adfa32c16ef9287711cab0702083f3e7` |

## Attribution

Annotations: RCC-AID, [10.5281/zenodo.20719257](https://doi.org/10.5281/zenodo.20719257), CC BY 4.0 —
de Boer, Häntze, Ziegelmayer, Russo, van Ginneken, Prokop, Bressem, Hering.

Publication: de Boer S, Häntze H, et al. *Accessible and Reproducible Renal Cell Carcinoma Research
Through Open-Sourcing Data and Annotations.*
[medRxiv 2026.04.22.26351451](https://www.medrxiv.org/content/10.64898/2026.04.22.26351451v1).

Source imaging: TCGA-KIRC ([10.7937/K9/TCIA.2016.V6PBVTDR](https://doi.org/10.7937/K9/TCIA.2016.V6PBVTDR)),
TCGA-KIRP ([10.7937/K9/TCIA.2016.ACWOGBEF](https://doi.org/10.7937/K9/TCIA.2016.ACWOGBEF)),
TCGA-KICH ([10.7937/K9/TCIA.2016.YU3RBCZN](https://doi.org/10.7937/K9/TCIA.2016.YU3RBCZN)) via TCIA,
CC BY 3.0, accessed through NCI Imaging Data Commons.
