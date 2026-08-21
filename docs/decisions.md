# Decision log

Architecture decision records for the RCC-AID → DICOM SEG conversion. Each entry states the decision,
why, and what it costs.

---

## ADR-001 — Emit both LABELMAP and BINARY segmentations

**Decision.** Every case is converted twice: `--segmentationType labelmap` (the requested deliverable)
and `--segmentationType binary`, into `data/seg_labelmap/` and `data/seg_binary/`.

**Why.** IDC currently holds **172,908 BINARY** and **17,238 FRACTIONAL** segmentation series and
**zero LABELMAP** — verified against `seg_index`. LABELMAP support in dcmqi is three months old
(v1.5.4, 2026-05-18), and there is no deployed corpus to check our output against. BINARY is the
validation oracle: it round-trips through `segimage2itkimage` and renders in OHIF and 3D Slicer today,
so any LABELMAP defect shows up as a decode difference against a known-good reference rather than as a
silent problem discovered by a downstream consumer.

**Cost.** Roughly doubles conversion time and output size. Both are small relative to the 8.27 GB of
source imaging.

---

## ADR-002 — Do not download `images.zip`

**Decision.** Fetch only `labels.zip`, `metadata.csv` and `manifest.tcia` from Zenodo. Source imaging
comes from IDC as DICOM.

**Why.** `images.zip` (4.1 GB) holds NIfTI *copies* of imaging that already exists in IDC in its
original DICOM form. dcmqi requires the DICOM anyway — it must reference real SOP Instance UIDs — so
the NIfTI copies would be redundant. All 129 referenced series were confirmed present in IDC
(16,055 instances, 8.27 GB).

**Cost.** None functionally. We lose the ability to diff our DICOM-reconstructed volumes against the
published NIfTI images, but `resolve_references.py` already validates geometry against the label grid,
which is the property that actually matters.

---

## ADR-003 — Category code `91723000`, diverging from dcmqi's bundled list

**Decision.** Code the Kidney segment's category as `(91723000, SCT, "Anatomical Structure")`, per
DICOM PS3.16 CID 7150 as published by [dcmterms](https://fedorov.github.io/dcmterms/terminology.html).

**Why.** dcmqi's `SegmentationCategoryTypeModifier-DICOM-Master.json` uses `123037004` (SNOMED CT
*Body structure*), which occurs **zero times anywhere in DICOM Part 16**. Since dcmterms is the
designated authority for this project and is extracted straight from the standard, the standard wins.

**Cost.** The category code will not be byte-identical to dcmqi-generated kidney segmentations already
in IDC, which almost certainly carry `123037004`. Acceptable: consumers filter on
`SegmentedPropertyTypeCodeSequence` (`Kidney` / `Mass` / `Cyst`), which is unchanged, so query
compatibility with `c4kc_kits` and `bamf_aimi_annotations` holds. Full detail and the verification
query in [terminology.md](terminology.md). Worth reporting upstream to dcmqi.

---

## ADR-004 — Label 2 ("Tumor") is coded `Mass`, not an RCC concept

**Decision.** `(4147007, SCT, "Mass")` with `AnatomicRegionSequence = (64033007, SCT, "Kidney")`.

**Why.** Three reasons, in order of weight: the annotators delineated a mass on CT, and RCC subtype is
pathology-derived truth about the patient rather than something visible in the image; it matches the
454 kidney-mass segmentation series already in IDC (`c4kc_kits`, `bamf_aimi_annotations`) over these
same collections; and `(41607009, SCT, "Renal cell carcinoma")` appears in no DICOM context group, so
using it would introduce an off-standard code for no interoperability gain.

**Cost.** Subtype is not expressed in the SEG object. It is carried instead at dataset level in
`submission/rcc_aid.yaml` (`cancer_types`) and per-case in `qc/case_index.csv`.

---

## ADR-005 — Reference instances resolved by replicating upstream acquisition grouping

**Decision.** For each label file, reconstruct the exact ordered list of DICOM instances that produced
its voxel grid, and pass that list to dcmqi via `--inputDICOMList`. Never use
`--referencesGeometryCheck 0`.

**Why.** The upstream label filenames carry an `_acq_{N|unknown}` token because
[`dcm2nii.py`](https://github.com/DIAGNijmegen/oncology-RCC-AID-codebase/blob/main/src/oncology_rcc_aid_codebase/dcm2nii.py)
split each DICOM series by `AcquisitionNumber` and dropped instances whose `Rows`/`Columns` disagreed
within a group. This was necessary because these TCGA series bundle multiple contrast phases into one
series: **21 of the 129 source series fail IDC's `regularly_spaced_3d_volume` check**, on
`unique_slice_positions` and/or `uniform_slice_spacing` — never on obliquity (0 oblique series) or
pixel spacing. A label file therefore covers a *subset* of its series' instances, and handing dcmqi
the whole series directory would produce wrong per-frame references.

Suppressing the geometry check would make conversion "succeed" while emitting references to instances
the annotation does not describe — the worst possible failure mode, because it is invisible in the
output.

**Cost.** More work than pointing dcmqi at a directory, and cases whose grid cannot be reproduced are
excluded rather than force-converted. Every exclusion is recorded with its geometry deltas in
`qc/reference_resolution.csv` and named in `docs/conversion-notes.md`.

---

## ADR-006 — `--useLabelIDAsSegmentNumber`

**Decision.** Pass `--useLabelIDAsSegmentNumber` so DICOM `SegmentNumber` equals the source NIfTI
label value: 1 = Kidney, 2 = Mass, 3 = Cyst.

**Why.** Makes the DICOM object self-describing against the upstream label convention and keeps
round-trips value-stable. Without it, dcmqi numbers segments sequentially in processing order, so a
case with no cyst would silently renumber. dcmqi 1.5.7 accepts any unique positive label IDs for
labelmap output, including gaps.

**Cost.** None. For the BINARY variant dcmqi requires consecutive IDs from 1; cases missing a middle
label (kidney + cyst, no mass) would fail, so `convert.py` detects this and falls back to sequential
numbering for BINARY only, recording it per case.

---

## ADR-007 — We declare no Background segment; dcmqi adds one to labelmap anyway

**Decision.** Our metadata declares segments for labels 1–3 only. dcmqi then inserts its own segment
`#0` into LABELMAP output, and we accept and verify it rather than fight it.

**What is actually emitted**, confirmed by reading the output (not by reading release notes):

| | LABELMAP | BINARY |
|---|---|---|
| Segment `#0` | `(125040, DCM, "Background")`, `SegmentAlgorithmType = MANUAL`, no algorithm name | absent |
| `PixelPaddingValue` (0028,0120) | `0` | absent |
| `BitsAllocated` | 8 | 1 |

**Why this is fine.** `(125040, DCM, "Background")` is a standard code — a member of CID 7165 Abstract
Segmentation Type — so the object stays conformant. It is genuinely needed: a LABELMAP encodes one
integer per voxel, so pixel value 0 has to *mean* something, and dcmqi both names it via segment 0 and
marks it via `PixelPaddingValue`. Segment 0 carrying `MANUAL` with no `SegmentAlgorithmName` is also
conformant, since (0062,0009) is Type 1C and required only for `AUTOMATIC`/`SEMIAUTOMATIC`.

**Correction to an earlier assumption.** This ADR previously asserted that dcmqi ≥ 1.5.5 replaced the
Background segment with `PixelPaddingValue`, read from the v1.5.5 release notes. Inspecting real 1.5.7
output shows it does *both*. `qc.py` therefore recognises `(0, 125040)` explicitly, checks it is absent
from BINARY, and excludes it before comparing the segment map against what we declared — rather than
either failing on it or blindly ignoring segment 0.

---

## ADR-008 — `SegmentAlgorithmType` is `SEMIAUTOMATIC` for all three segments

**Decision.**

```
SegmentAlgorithmType = SEMIAUTOMATIC
SegmentAlgorithmName = "Robust Kidney Abnormality Segmentation"
```

applied to Kidney, Mass and Cyst alike.

**Why.** Resolved from the RCC-AID publication
([medRxiv 2026.04.22.26351451](https://www.medrxiv.org/content/10.64898/2026.04.22.26351451v1)), which
documents a model-initialised, human-corrected workflow — not manual annotation:

> "we first used a public segmentation model [34] to automatically delineate kidneys and an abstract
> class 'renal abnormality'"

> "Using a CT viewer, annotators simply clicked on the mask corresponding to the primary solid tumor,
> as well as any identifiable cysts"

> "Two students quality checked and corrected the data as well as annotated tumors and cysts.
> Uncertain cases were checked by a board-certified radiologist."

So every voxel boundary in this dataset originates from the model; the human contribution was
correction, and assignment of the abstract "renal abnormality" class into tumour vs cyst by clicking
(unselected masks were deleted). That is precisely what `SEMIAUTOMATIC` means. `MANUAL` would overstate
human authorship of the contours, including for Mass and Cyst — their *labels* are human, their
*boundaries* are not.

Reference [34] of that paper identifies the model, giving us a real `SegmentAlgorithmName`:

> de Boer S, Häntze H, Venkadesh KV, Buser MAD, Mamani GEH, Xu L, et al. **Robust Kidney Abnormality
> Segmentation: A Validation Study of an AI-Based Framework.** arXiv; 2025.

**Why it matters.** `SegmentAlgorithmName` (0062,0009) is Type 1C in DICOM — mandatory once the type is
`SEMIAUTOMATIC`, so this could not have been left blank. And it is the attribute a downstream consumer
reads to decide how far to trust a segmentation; getting it wrong misrepresents the dataset.

---

## ADR-012 — A non-conformant `ProcedureCodeSequence` in the source is stripped from reference copies

**Decision.** `rcc_aid.sanitize_dicom` writes copies of the affected reference instances with
`ProcedureCodeSequence` (0008,1032) removed, into `data/dicom_sanitized/<case_id>/`, and repoints that
case's reference list at them. 11 of the 129 cases need it.

**Why.** dcmqi copies study-level attributes from the reference image into the derived SEG.
`CodeMeaning` (0008,0104) is Type 1 in the Code Sequence Macro, so a malformed source item makes dcmqi
abort outright:

```
ERROR - CodeMeaning (0008,0104) absent in CodeSequenceMacro (type 1)
ERROR - Could not write item #0 in ProcedureCodeSequence: Missing Attribute(s)
FATAL ERROR: Writing of the SEG dataset failed!
```

The source TCGA CT is at fault, in two variants:

| Variant | Cases | `ProcedureCodeSequence` item |
|---|--:|---|
| empty item | 8 | `CodeValue=None, CodingSchemeDesignator=None, CodeMeaning=None` |
| private code, no meaning | 3 | `CodeValue='74160', CodingSchemeDesignator='99DRSYS134', CodeMeaning=None` |

**`--noDicomValueChecks` does not help**, even though its documentation describes this exact case
("when taking over attributes from non-compliant DICOM image files"). Tested against
`KIRC_…114780349668855871735537947410_acq_5` on dcmqi 1.5.7: identical failure. The flag relaxes
*value* checking, while this is a missing Type 1 *attribute* check inside DCMTK's `CodeSequenceMacro`
write path. Worth reporting upstream — a converter should not be stopped dead by an optional
attribute it chose to copy.

**Why removal rather than repair.** `ProcedureCodeSequence` is Type 3 in the General Study module, so
omitting it is conformant. Supplying a `CodeMeaning` would mean inventing clinical metadata the source
does not contain.

**Scope is deliberately minimal.** A stricter reading would also flag 3 further cases whose items have
a valid `CodeValue` and `CodeMeaning` but no `CodingSchemeDesignator` — also a Type 1 violation, but one
dcmqi accepts. Those carry real content (`CodeValue='6500572'`,
`CodeMeaning='CT, ABDOMEN W&W/O CONTRAST'`), so stripping their sequence would destroy clinical
metadata to fix a non-conformance nothing is tripping over. They are recorded in
`qc/sanitization_report.csv` under `non_conformant_but_tolerated` and left untouched.

**Safeguards.** Only the listed reference instances are copied; `SOPInstanceUID`, geometry and
`PixelData` are carried over verbatim, and the module asserts the ordered `SOPInstanceUID` list is
unchanged before repointing anything. The original reference list is kept as `<case_id>.txt.orig`. The
resulting SEG therefore references exactly the same instances as it would have.

---

## ADR-011 — Label maps are repaired before conversion, not converted as published

**Decision.** `rcc_aid.normalize_labels` rewrites every label map to `data/labels_norm/<case>_seg.nrrd`
before dcmqi sees it: rounded to exact integers and cast to `uint8`, z-flipped where required, and
stamped with the geometry of the resolved DICOM instances. Conversion and QC both run against these.

**Why.** Two defects in the published NIfTI files make them unusable as-is for DICOM conversion. Both
affect **the same 13 files** — all KIRC, all from irregular multi-acquisition series — which evidently
took a different upstream code path:

1. **Non-integer label values.** 13 of 129 files are `float32`, and 10 of those store label 1 as
   `1.0000152587890625` (= 1 + 1/65536, a NIfTI scale-factor artifact). Segment label values must be
   exact integers. Worst deviation across all 129 is `1.5e-5`, and every file rounds cleanly to
   `{0,1,2}` or `{0,1,2,3}`, so rounding is unambiguous — nowhere near the 0.5 that would make it a
   judgement call.

2. **Reversed slice order.** The same 13 store slices in *descending* DICOM z order while still
   declaring a `+z` direction cosine. Their declared geometry therefore places the volume outside the
   imaged range entirely — for `...311537942010300349465300481227_acq_3`, the label claims z from
   −155 to +290 against a scan covering −600 to −155, overlapping in a single slice.

**How the flip was confirmed** — not from headers alone, which are what is wrong here, but from image
content. For each case we compare the fraction of kidney-labelled voxels falling in soft tissue
(−50…400 HU) against the same fraction for the opposite orientation:

| | chosen orientation | opposite orientation |
|---|---|---|
| median soft-tissue fraction | **0.995** | 0.790 |
| minimum | 0.963 | 0.012 |

The chosen orientation wins in **129/129** cases. Among the 13 flipped ones, 0.997 versus 0.800 — with
the flip, kidney labels sit in contrast-enhanced parenchyma; without it they land on air and bone.

**Output is NRRD, not NIfTI.** Deliberate: NIfTI's inability to carry an arbitrary direction matrix is
what destroyed the slice-order information upstream in the first place. Writing NIfTI again would risk
reintroducing the same class of defect. NRRD represents the direction faithfully and dcmqi reads it
natively.

**Cost and safeguards.** The delivered DICOM is not a byte-faithful transcription of the published
NIfTI — it is a corrected one, and for 13 cases the correction moves every voxel. That is the point:
a faithful transcription would place those annotations outside the patient. `normalize_labels` asserts
per-label voxel counts are unchanged against what `build_case_index` read from the untouched source,
that the NRRD survives a write/read round-trip, and that the intensity evidence favours the chosen
orientation — all recorded per case in `qc/normalization_report.csv`.

**This is worth reporting upstream to the RCC-AID authors.**

---

## ADR-010 — Compression and empty-frame handling differ per flavour

**Decision.**

| Flavour | `--compress` | `--skip` |
|---|---|---|
| `labelmap` | `deflate` | `0` (keep every frame) |
| `binary` | `none` | `1` (skip empty frames) |

**Why labelmap is deflated.** LABELMAP PixelData is 8 bits per voxel across every frame, where BINARY
packs 1 bit per voxel. Left uncompressed, a labelmap SEG can be *larger* than the binary equivalent it
is supposed to improve on. Deflate (Deflated Explicit VR Little Endian, `1.2.840.10008.1.2.1.99`) is
lossless, so nothing is traded away.

**Why labelmap keeps every frame.** Given deflate, the empty frames are uniform `PixelPaddingValue` and
compress to almost nothing — so skipping them buys very little. What it costs is real: a sparse object
depends on dcmqi refilling the absent slice positions at read time, and a skipped slice that decoded as
anything other than background would be a silent data change. Keeping all frames yields a complete,
contiguous volume whose correctness does not rest on read-side reconstruction.

**Why binary does the opposite.** BINARY writes one frame per segment per slice, and a kidney
segmentation touches a minority of the slices in an abdominal CT — so encoding empty frames would
multiply the object size for no information, with no deflate to absorb them. It stays uncompressed
because it is the QC oracle (ADR-001): plain Explicit VR Little Endian is read by every viewer without
question, which is the whole point of having a reference.

**Verification.** The pilot records both objects' sizes, and `qc.py` asserts each round-trips
*exactly* against the source NIfTI and that the two flavours decode identically — so neither the
compression nor the frame-skipping can change a voxel without failing.

---

## ADR-009 — dcmqi from the PyPI distribution, pinned to 1.5.7

**Decision.** dcmqi is a project dependency — `dcmqi==1.5.7` in `pyproject.toml`, installed into a
`uv venv`. This is the [official Python distribution](https://github.com/ImagingDataCommons/dcmqi-python-distributions)
maintained under the ImagingDataCommons org; its version numbers track upstream dcmqi releases, and it
puts `itkimage2segimage` / `segimage2itkimage` on the venv `PATH`. `scripts/run_all.sh` asserts the
reported revision is `506306a` / `v1.5.7` before doing anything.

**Why pinned at all.** The dcmqi already on this machine (`/Users/af61/local/bin`, revision `011237d`)
has no `--segmentationType` flag and cannot emit LABELMAP. Worse, labelmap semantics changed *within*
the 1.5.x line: v1.5.4 added labelmap output, v1.5.5 moved background handling to `PixelPaddingValue`
and added gapped segment numbers. An unpinned dcmqi would silently change what the output means.

**Why the PyPI package** over a release tarball or `qiicr/dcmqi:v1.5.7` on Docker Hub: dcmqi becomes an
ordinary resolved, locked, platform-matched dependency alongside SimpleITK and pydicom, so `uv sync`
reproduces the whole toolchain on any platform with no Docker daemon and no vendored binaries in the
tree.
