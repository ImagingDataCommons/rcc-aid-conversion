# Conversion notes

What actually happened when the pipeline ran, and everything a reader needs in order to distrust the
right things. Decisions and their rationale live in [decisions.md](decisions.md); the code tuples live
in [terminology.md](terminology.md).

Run environment: macOS 25.5 (x86_64), dcmqi 1.5.7 from the PyPI distribution (revision `506306a`),
idc-index v24, IDC data release v24.

---

## Inputs

All three Zenodo files matched the checksums published in the record:

| File | md5 | Bytes |
|---|---|---|
| `labels.zip` | `6d453071037640a79042033723442c1a` | 4,132,572 |
| `metadata.csv` | `7297e5ea8201eddb0c3aae55c1068952` | 72,909 |
| `manifest.tcia` | `adfa32c16ef9287711cab0702083f3e7` | 8,585 |

`images.zip` was not downloaded ([ADR-002](decisions.md)).

`labels.zip` holds exactly **129** label maps: KIRC 85, KIRP 26, KICH 18 — matching the counts in the
Zenodo description. Their SeriesInstanceUIDs are **exactly** the 129 in `manifest.tcia`, no extras or
omissions on either side.

## Source imaging

All **129/129** referenced CT series were found in IDC — 91 patients, 16,055 instances, 8.27 GB. Every
label file's cohort prefix agreed with the `collection_id` its UID resolved to.

## Reference resolution

Every one of the **129 cases resolved via the acquisition group named in its own filename**. No
fallback path was used, and no case was left unresolved.

The important result is what the acquisition split does to the geometry. 21 of the 129 source series
fail IDC's `volume_geometry_index.regularly_spaced_3d_volume` check:

| Failure mode | Series |
|---|--:|
| duplicate slice positions **and** non-uniform spacing | 13 |
| non-uniform spacing only | 8 |
| oblique acquisition | 0 |

After grouping by `AcquisitionNumber`, the matched subsets show **zero non-uniform slice gaps** and
**zero duplicate slice positions**. The irregularity is entirely an artifact of several contrast phases
sharing one DICOM series; each acquisition within them is a clean, regularly-spaced volume. This is why
[ADR-005](decisions.md) insists on an explicit `--inputDICOMList` and refuses
`--referencesGeometryCheck 0`.

Worst origin discrepancy between a label grid and its resolved DICOM grid: **3.04e-05 mm**.

## Two defects in the published label maps

Documented in full in [ADR-011](decisions.md). Both affect **the same 13 files** — all TCGA-KIRC, all
drawn from the irregular multi-acquisition series — which evidently took a different upstream code
path. Worth reporting to the RCC-AID authors.

**Non-integer label values.** 13 of 129 files are `float32`; 10 of those store label 1 as
`1.0000152587890625` (= 1 + 1/65536, a NIfTI scale-factor artifact). Worst deviation from an integer
across all 129 files is `1.526e-05`, and every file rounds cleanly to `{0,1,2}` or `{0,1,2,3}` — so
rounding is safe, but truncation would not have been.

**Reversed slice order.** The same 13 store slices in *descending* DICOM z order while declaring a `+z`
direction cosine, so their declared geometry places the annotation outside the imaged volume. Example —
`KIRC_…311537942010300349465300481227_acq_3`:

```
label map declares : origin z = -155.0, direction z = +1, 90 slices at 5 mm  ->  z spans -155 .. +290
DICOM acq=3 covers : z = -600.0 .. -155.0
overlap            : one slice
```

Because the header is the thing that is wrong, the correction was validated against image content
instead. For every case we measured the fraction of kidney-labelled voxels falling in soft tissue
(−50…400 HU) under both orientations:

| | chosen orientation | opposite orientation |
|---|--:|--:|
| median | **0.995** | 0.790 |
| minimum | 0.963 | 0.012 |

The chosen orientation was better in **129/129** cases. Among the 13 flipped ones, 0.997 versus 0.800.

A caveat worth stating plainly: the published *image* NIfTIs in `images.zip` were produced by the same
upstream code, so they very likely carry the same reversal. Label and image would then be mutually
consistent, and a user working purely in NIfTI would see a correct overlay — the defect only surfaces
when mapping back to DICOM. We did not download `images.zip` to confirm this.

## Segment content

| Labels present | Cases |
|---|--:|
| Kidney + Mass | 91 |
| Kidney + Mass + Cyst | 38 |

No case has a gap in its label values, so `--useLabelIDAsSegmentNumber` is safe for BINARY as well as
LABELMAP, and the fallback contemplated in [ADR-006](decisions.md) was never needed. No case contains a
label value outside 0–3.

Contrast phase, from `metadata.csv`: delayed 53, venous 50, arterial 26.

## Output

Two flavours per case ([ADR-001](decisions.md)), with the settings from
[ADR-010](decisions.md).

| | LABELMAP | BINARY |
|---|---|---|
| Transfer syntax | Deflated Explicit VR Little Endian | Explicit VR Little Endian |
| `BitsAllocated` | 8 | 1 |
| Frames | all (`--skip 0`) | non-empty only (`--skip 1`) |
| Segment `#0` | `(125040, DCM, "Background")`, inserted by dcmqi | absent |
| `PixelPaddingValue` | `0` | absent |

The size comparison is the striking part — LABELMAP with deflate and *every* frame retained is far
smaller than BINARY with empty frames skipped:

| Case | LABELMAP | BINARY | Ratio |
|---|--:|--:|--:|
| `KICH_…107096179260147523759389579292_acq_11` (251 slices) | 129 KB | 6,752 KB | 52× |
| `KIRC_…118168941853469915520207083596_acq_unknown` (133) | 54 KB | 1,673 KB | 31× |
| `KIRC_…311537942010300349465300481227_acq_3` (90) | 37 KB | 905 KB | 25× |

### Deflate is not optional for LABELMAP

Transcoding the 251-slice case out of deflate makes the point sharply:

| Same object, same content | Bytes |
|---|--:|
| LABELMAP, Deflated Explicit VR LE | 128,982 |
| LABELMAP, Explicit VR LE (uncompressed) | 65,970,302 |
| BINARY, Explicit VR LE, empty frames skipped | 6,751,758 |

That is a **511× compression ratio**, and an uncompressed LABELMAP would be **10× larger than the
BINARY equivalent** — 66 MB per case, roughly 8.5 GB across the dataset, for objects that are 129 KB
deflated. LABELMAP's 8-bits-per-voxel-per-frame encoding only pays off once the uniform background is
compressed away. This is the empirical basis for [ADR-010](decisions.md).

## DICOM conformance

Validated with `dciodvfy` (dicom3tools). Both flavours are recognised as the `Segmentation` IOD and
both are clean apart from findings that are benign for this dataset and allowlisted with reasons in
`qc.py` (`DCIODVFY_EXPECTED`): retired single-component Person Name form for `PatientName` and
`ContentCreatorName`; zero `PatientSize`/`PatientWeight` and absent `StudyID`, all inherited from the
source TCGA CT.

Two LABELMAP-specific observations, both pointing at tooling rather than at the data:

**`dciodvfy` cannot read the Deflated transfer syntax.** Pointed at a deflated object it reports
`Implicit VR encoding even though supposed to be explicit` and `Dicom dataset read failed` — it is
parsing the compressed bytes as though they were a raw dataset. `qc.py` therefore transcodes deflated
objects to Explicit VR Little Endian in a temporary file before validating. Worth knowing as an
interoperability caveat for deflate generally.

**`PatientOrientation` is flagged for LABELMAP but not BINARY.** After transcoding, the only error is
`Missing attribute Type 2C Conditional Element=<PatientOrientation> Module=<GeneralImage>`. The
attribute is absent from **both** flavours, yet only LABELMAP is flagged. `PatientOrientation`
(0020,0020) is Type 2C in General Image, conditional on the image not being part of a Frame of
Reference — and ours is, referencing the source CT series with per-frame Plane Position. Combined with
the deflate gap, this suggests `dciodvfy`'s Segmentation IOD model predates the LABELMAP supplement. We
record it rather than treating it as a defect, and have not concluded which side is at fault.

## Repairs applied to the reference DICOM

Two attributes in the source TCGA CT are non-conformant in ways that reach our output, so
`rcc_aid.sanitize_dicom` writes repaired copies of the affected reference instances into
`data/dicom_sanitized/<case_id>/` and repoints the case's reference list, keeping the original list as
`<case_id>.txt.orig`. Rationale in [ADR-012](decisions.md).

The authoritative record is `qc/sanitization_audit.csv`, produced by
`python -m rcc_aid.sanitize_dicom --audit`, which diffs every sanitized copy against its original on
disk rather than trusting the run log:

| Attribute | Cases | Instances | Change |
|---|--:|--:|---|
| `BodyPartExamined` (0018,0015) | 19 | 2,421 | `'Kidney'` → `'KIDNEY'` |
| `ProcedureCodeSequence` (0008,1032) | 14 | 2,394 | single-item sequence removed |

33 distinct cases (no overlap), 4,815 instances rewritten, **no other attribute changed**, and
`SOPInstanceUID` preserved in every case. `--audit` re-runs cleanly with 0 cases outstanding.

`ProcedureCodeSequence` removal is the only change that discards information. It was unusable in all 14
cases — 8 wholly empty items, 3 with a private code and no `CodeMeaning`, 3 with a code and meaning but
no `CodingSchemeDesignator` — and it is Type 3, so omitting it is conformant. The source remains
available in IDC for anyone who needs those local procedure codes.

## Quality control

`qc/qc_report.csv`, one row per case. **129/129 pass.**

| Check | Result |
|---|---|
| LABELMAP round-trip vs the label map dcmqi consumed | exact, 129/129 |
| BINARY round-trip, offset-aware | exact, 129/129 |
| LABELMAP ≡ BINARY, embedded in full source geometry | 129/129 |
| `dciodvfy`, both flavours | clean, 129/129; IOD recognised as `Segmentation` |
| Labelled voxels outside BINARY's encoded extent | 0 in every case |

BINARY skips a median of 39 empty slices (max 619), every one verified to contain no labels.

Output totals: LABELMAP **7.6 MB** for all 129 objects (median 42 KB, max 0.4 MB); BINARY **334.8 MB**
(median 1.6 MB, max 21.3 MB) — a 44× difference across the dataset. Median frames per object: 85
LABELMAP (all slices), 49 BINARY (non-empty only). Median referenced instances: 85 and 30 respectively.

Every case must satisfy all of:

- LABELMAP decodes **exactly** equal to the label map dcmqi consumed;
- BINARY likewise, after locating its crop from the origin — and the source must be pure background
  outside the encoded extent, which is what proves `--skip 1` lost nothing;
- LABELMAP and BINARY, embedded back into full source geometry, equal each other;
- segment numbers map to the expected codes, with `SEMIAUTOMATIC` /
  `Robust Kidney Abnormality Segmentation` on every declared segment;
- `PatientID` and `StudyInstanceUID` match the source, `ReferencedSeriesSequence` is exactly the source
  series, and every referenced instance is in the resolved reference list.

## Viewer verification

The three pilot cases were loaded into a Google Healthcare DICOM store to check them in a real DICOM
viewer, rather than only through `segimage2itkimage`.

**Store**

```
projects/idc-sandbox-000/locations/us-central1/datasets/fedorov-dev-healthcare/dicomStores/rcc-aid-pilot
```

DICOMweb endpoint:
`https://healthcare.googleapis.com/v1/<store path above>/dicomWeb`

**How it was loaded.** Objects were staged to `gs://af-dev-storage/rcc-aid-pilot/` and imported with
`gcloud healthcare dicom-stores import gcs`, which reported **565/565 succeeded** (559 source CT
instances plus the 6 SEG objects). The staging prefix was deleted afterwards. The CT series were then
removed from the store with DICOMweb `DELETE` on each series, leaving the 6 SEG series only — see the
caveat below.

**Viewer URLs.** Pattern:

```
https://viewers-sandbox-gha-testing.web.app/viewer?StudyInstanceUIDs=<StudyInstanceUID>&gcp=<store path>
```

| Patient | Case | What it exercises | StudyInstanceUID |
|---|---|---|---|
| TCGA-DV-5567 | `KIRC_…311537942010300349465300481227_acq_3` | irregular multi-acquisition series **and** z-flip | `1.3.6.1.4.1.14519.5.2.1.1357.4004.259839416696453949057995272918` |
| TCGA-DV-5568 | `KIRC_…118168941853469915520207083596_acq_unknown` | missing `AcquisitionNumber`, all three segments | `1.3.6.1.4.1.14519.5.2.1.1357.4004.239194542022979982905743234780` |
| TCGA-KM-8440 | `KICH_…107096179260147523759389579292_acq_11` | clean regular series, all three segments | `1.3.6.1.4.1.14519.5.2.1.1357.4011.258217882991704941618731006997` |

Full URLs:

- TCGA-DV-5567 — <https://viewers-sandbox-gha-testing.web.app/viewer?StudyInstanceUIDs=1.3.6.1.4.1.14519.5.2.1.1357.4004.259839416696453949057995272918&gcp=projects/idc-sandbox-000/locations/us-central1/datasets/fedorov-dev-healthcare/dicomStores/rcc-aid-pilot>
- TCGA-DV-5568 — <https://viewers-sandbox-gha-testing.web.app/viewer?StudyInstanceUIDs=1.3.6.1.4.1.14519.5.2.1.1357.4004.239194542022979982905743234780&gcp=projects/idc-sandbox-000/locations/us-central1/datasets/fedorov-dev-healthcare/dicomStores/rcc-aid-pilot>
- TCGA-KM-8440 — <https://viewers-sandbox-gha-testing.web.app/viewer?StudyInstanceUIDs=1.3.6.1.4.1.14519.5.2.1.1357.4011.258217882991704941618731006997&gcp=projects/idc-sandbox-000/locations/us-central1/datasets/fedorov-dev-healthcare/dicomStores/rcc-aid-pilot>

Each study carries both flavours, distinguishable by `SeriesNumber` — 300 is LABELMAP, 301 is BINARY
and also carries a `[BINARY]` suffix in `SeriesDescription` — so the two can be compared on the same
images.

**Caveat: the CT was removed from this store.** The SEG objects reference CT SOP Instances that are no
longer present, so a viewer resolving referenced images from the same store will not composite the
overlay. `ReferencedSeriesSequence` points at the real TCGA series in IDC, so this is a property of
this particular review store, not of the objects. Re-import the referenced instances (90, 133 and 251
respectively) if the URLs need to render.

**Local overlay review instead.** Because the store holds no images, the overlay is checked from files
in 3D Slicer rather than in OHIF. `scripts/export_review_tree.py --out review/pilot --ct series` puts
the source CT next to the segmentations:

```
review/pilot/<PatientID>/CT/<SeriesNumber>-<SeriesDescription>/     full series, 175 / 133 / 251 instances
                        /SEG/300-…/                                LABELMAP
                        /SEG/301-…-BINARY/                         BINARY
                        /NIFTI/original-published/                 as published on Zenodo
                        /NIFTI/normalized-for-conversion/          after ADR-011 repair
```

565 files, 292 MB — the same 559 CT instances plus 6 SEG objects that were imported into the store, so
this is also the file set to re-import if the viewer URLs are ever wanted to render.

Verified after export: every instance referenced by a SEG is present (0 missing in all three cases),
the referenced subset reproduces the label grid in ascending slice order (max origin deviation
3.75e-04 mm, spacing and direction exact), and TCGA-DV-5568 / TCGA-KM-8440 export exactly their
referenced sets. TCGA-DV-5567 exports 85 instances beyond the 90 referenced — its acquisitions are
`{3: 90, 1: 55, 2: 30}` and only acquisition 3 was segmented, so **that folder is not a single
loadable volume**; Slicer will report inconsistent geometry and must be allowed to split it by
acquisition. Use `--ct referenced` to get the loadable subset directly.

**Outcome.** Both flavours load and are listed correctly, with `SeriesDescription`, `SeriesNumber` and
`PatientID` as intended. One difference was observed and is recorded under Known limitations below:
the two flavours render in different colors despite encoding identical ones.

## Known limitations

**Colors may not render as encoded in every viewer.** Both flavours carry identical
`RecommendedDisplayCIELabValue` per segment, decoding back to exactly the requested RGB
(Kidney 212/126/151, Mass 144/238/144, Cyst 205/205/100). Neither object carries a Palette Color LUT;
both are `MONOCHROME2`. Despite the encoded values being identical, OHIF was observed rendering the two
flavours with different colors, which suggests it does not read
`RecommendedDisplayCIELabValue` from a LABELMAP `SegmentSequence`. This is a viewer-side gap, not a
property of the data — under active investigation, and unsurprising given LABELMAP has no precedent in
IDC.

**LABELMAP has no deployed precedent.** As of IDC v24 the archive holds 172,908 BINARY and 17,238
FRACTIONAL segmentation series and zero LABELMAP. The BINARY variant exists precisely so that this
claim can be checked rather than assumed.

**The delivered DICOM is a corrected transcription, not a byte-faithful one.** For 13 cases the
correction moves every voxel. See [ADR-011](decisions.md).

**`ContentCreatorName` is a group attribution.** Set to `RCC-AID` because two annotators worked across
the dataset and per-case attribution is not published.
