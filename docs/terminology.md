# Terminology: SNOMED / DICOM coding of the RCC-AID segments

The RCC-AID NIfTI label maps encode four values:

| Value | Upstream name |
|------:|---------------|
| 0 | Background |
| 1 | Kidney |
| 2 | Tumor |
| 3 | Cyst |

DICOM Segmentation requires each segment to be described by coded concepts rather than a free-text
name. This document records the tuples used, where they came from, and the places where the
authoritative source disagrees with widely-deployed tooling.

## Authoritative source

**[dcmterms](https://fedorov.github.io/dcmterms/terminology.html)** — coded terminology extracted
directly from DICOM PS3.16, published as Parquet. It is queryable in place, without downloading:

```python
import duckdb
U = "https://fedorov.github.io/dcmterms/data/coded_entries.parquet"
duckdb.sql(f"""
  SELECT code_value, coding_scheme_designator, code_meaning, cid_number, cid_name
  FROM read_parquet('{U}')
  WHERE cid_number IN (7150, 7154, 7159)
  ORDER BY cid_number, code_meaning
""").show()
```

`scripts/verify_terminology.py` runs exactly this check against the tuples below and fails loudly if
the standard has moved.

## The tuples in use

| Label | Segment | `SegmentedPropertyCategoryCodeSequence` | `SegmentedPropertyTypeCodeSequence` | `AnatomicRegionSequence` |
|------:|---------|------------------------------|--------------------------|---------------|
| 1 | Kidney | `(91723000, SCT, "Anatomical Structure")` | `(64033007, SCT, "Kidney")` | — |
| 2 | Tumor | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(4147007, SCT, "Mass")` | `(64033007, SCT, "Kidney")` |
| 3 | Cyst | `(49755003, SCT, "Morphologically Abnormal Structure")` | `(367643001, SCT, "Cyst")` | `(64033007, SCT, "Kidney")` |

Context group provenance:

- Both category codes are members of **CID 7150 Segmentation Property Category**.
- `Kidney` is a member of **CID 7154 Abdominal Segmentation Type**.
- `Mass` and `Cyst` are members of **CID 7159 Lesion Segmentation Type**.

Label 0 (Background) gets **no segment**. dcmqi ≥ 1.5.5 represents unlabelled labelmap voxels with
*Pixel Padding Value* (0028,0120) instead of inserting a Background segment, which is the correct
DICOM representation.

## Why `Mass` for label 2

The RCC-AID cohorts have known histology (TCGA-KIRC clear cell, TCGA-KIRP papillary, TCGA-KICH
chromophobe), so more specific codes were available — e.g. `(41607009, SCT, "Renal cell carcinoma")`
or per-subtype concepts. `Mass` was chosen deliberately:

1. **It matches what was annotated.** The annotators delineated a mass on CT. The RCC subtype is
   pathology-derived truth about the *patient*, not something read off the image; asserting it inside
   the segmentation conflates the two.
2. **It matches IDC precedent**, so RCC-AID answers the same queries as the kidney-mass segmentations
   already in the archive:

   | IDC source | Segmented property types | Series |
   |---|---|---|
   | `c4kc_kits` | `Kidney \| Mass` | 210 |
   | `bamf_aimi_annotations` | `Kidney \| Mass \| Cyst` (AnatomicRegion `Kidney`) | 244 |

   `bamf_aimi_annotations` covers `tcga_kirc`, `tcga_kirp` and `tcga_kich` among others — the same
   collections and the same three structures.
3. **`Renal cell carcinoma` is not in any DICOM context group.** `41607009` occurs nowhere in Part 16,
   so using it would mean introducing an off-standard code for no interoperability gain.

The RCC subtype is not lost — it is carried at the dataset level in `submission/rcc_aid.yaml`
(`cancer_types`) and per-case in `qc/case_index.csv`, which is the right altitude for it.

## Divergences from dcmqi's bundled code list

dcmqi ships `doc/segContexts/SegmentationCategoryTypeModifier-DICOM-Master.json`, which is what most
dcmqi users copy from and therefore what much existing DICOM SEG content in the wild contains. It
disagrees with DICOM PS3.16 in ways that matter here.

### 1. "Anatomical Structure" is `91723000`, not `123037004`

| Source | Code |
|---|---|
| DICOM PS3.16 CID 7150 (via dcmterms) | `(91723000, SCT, "Anatomical Structure")` |
| dcmqi master list | `(123037004, SCT, "Anatomical Structure")` |

`123037004` — SNOMED CT *Body structure* — occurs **zero times anywhere in DICOM Part 16**. Verified:

```python
duckdb.sql(f"SELECT COUNT(*) FROM read_parquet('{U}') WHERE CAST(code_value AS VARCHAR)='123037004'")
# 0
```

`91723000` carries SNOMED-RT id `T-D0005` and UMLS `C1268086`.

**Decision: use `91723000`.** This repo follows the standard. Consequence: the Kidney segment's
category code will not be byte-identical to dcmqi-generated segmentations already in IDC (which
almost certainly carry `123037004`). Category codes are coarse and are not what consumers filter on —
`SegmentedPropertyTypeCodeSequence` is — so query compatibility with `c4kc_kits` /
`bamf_aimi_annotations` is preserved. See [decisions.md](decisions.md) ADR-003.

Worth reporting upstream to dcmqi.

### 2. `49755003` means "Morphologically **Abnormal** Structure"

| Source | Meaning for `49755003` |
|---|---|
| DICOM PS3.16 CID 7150 | `Morphologically Abnormal Structure` |
| dcmqi master list, and IDC's `seg_index` | `Morphologically Altered Structure` |

Same code value, same coding scheme — so concept identity and all interoperability are unaffected.
`CodeMeaning` is informative only. This repo uses the DICOM spelling.

### 3. Related drift, not used here

- `Neoplasm, Primary`: dcmqi says `86049000`; CID 7159 says `372087000`.
- **CID 4 Anatomic Region lists `64033007` with the meaning `"Endo-renal"`** (an endoscopy-flavoured
  label for the same SNOMED concept). For `AnatomicRegionSequence` this repo writes
  `(64033007, SCT, "Kidney")`: the code value is what identifies the concept, and `Kidney` is the
  meaning used for the same code in CID 7154 and by every kidney segmentation already in IDC.

## Recommended display colours

Taken from dcmqi's master list so rendering matches existing tooling defaults:

| Segment | RGB |
|---|---|
| Kidney | `[212, 126, 151]` |
| Mass | `[144, 238, 144]` |
| Cyst | `[205, 205, 100]` |
