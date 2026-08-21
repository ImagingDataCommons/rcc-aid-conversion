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

## Quality control

`qc/qc_report.csv`, one row per case. Every case must satisfy all of:

- LABELMAP decodes **exactly** equal to the label map dcmqi consumed;
- BINARY likewise, after locating its crop from the origin — and the source must be pure background
  outside the encoded extent, which is what proves `--skip 1` lost nothing;
- LABELMAP and BINARY, embedded back into full source geometry, equal each other;
- segment numbers map to the expected codes, with `SEMIAUTOMATIC` /
  `Robust Kidney Abnormality Segmentation` on every declared segment;
- `PatientID` and `StudyInstanceUID` match the source, `ReferencedSeriesSequence` is exactly the source
  series, and every referenced instance is in the resolved reference list.

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
