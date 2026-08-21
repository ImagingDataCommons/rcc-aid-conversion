"""Coded terminology for the RCC-AID segments.

Single source of truth. Every tuple here is traceable to DICOM PS3.16 via dcmterms
(https://fedorov.github.io/dcmterms/terminology.html); `scripts/verify_terminology.py` re-checks them
against the published Parquet and fails if the standard has moved.

See docs/terminology.md for provenance and for the two places where dcmqi's bundled code list
disagrees with the standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# CID 7150 Segmentation Property Category.
# NOTE: "Anatomical Structure" is 91723000, NOT dcmqi's 123037004 -- see docs/decisions.md ADR-003.
CAT_ANATOMICAL = {
    "CodeValue": "91723000",
    "CodingSchemeDesignator": "SCT",
    "CodeMeaning": "Anatomical Structure",
}
CAT_MORPH_ABNORMAL = {
    "CodeValue": "49755003",
    "CodingSchemeDesignator": "SCT",
    "CodeMeaning": "Morphologically Abnormal Structure",
}

# CID 7154 Abdominal Segmentation Type / CID 7159 Lesion Segmentation Type.
TYPE_KIDNEY = {
    "CodeValue": "64033007",
    "CodingSchemeDesignator": "SCT",
    "CodeMeaning": "Kidney",
}
TYPE_MASS = {
    "CodeValue": "4147007",
    "CodingSchemeDesignator": "SCT",
    "CodeMeaning": "Mass",
}
TYPE_CYST = {
    "CodeValue": "367643001",
    "CodingSchemeDesignator": "SCT",
    "CodeMeaning": "Cyst",
}

# AnatomicRegionSequence. CID 4 labels 64033007 "Endo-renal"; we write the CID 7154 meaning, which is
# what every kidney segmentation in IDC uses for this code. See docs/terminology.md.
REGION_KIDNEY = dict(TYPE_KIDNEY)


@dataclass(frozen=True)
class Segment:
    """One RCC-AID label and its DICOM coding."""

    label_id: int
    name: str  # upstream RCC-AID name
    category: dict
    seg_type: dict
    rgb: list[int]
    anatomic_region: dict | None = None
    # CIDs this segment's codes are drawn from, asserted by scripts/verify_terminology.py.
    expected_cids: dict = field(default_factory=dict)

    def to_dcmqi(self, algorithm_type: str, algorithm_name: str | None = None) -> dict:
        """Render as one dcmqi `segmentAttributes` entry."""
        entry: dict = {
            "labelID": self.label_id,
            "SegmentLabel": self.dicom_label,
            "SegmentDescription": self.description,
            "SegmentAlgorithmType": algorithm_type,
            "SegmentedPropertyCategoryCodeSequence": self.category,
            "SegmentedPropertyTypeCodeSequence": self.seg_type,
            "recommendedDisplayRGBValue": self.rgb,
        }
        if self.anatomic_region is not None:
            entry["AnatomicRegionSequence"] = self.anatomic_region
        if algorithm_name:
            entry["SegmentAlgorithmName"] = algorithm_name
        return entry

    @property
    def dicom_label(self) -> str:
        """Short label shown by viewers -- the DICOM code meaning, not the upstream name."""
        return self.seg_type["CodeMeaning"]

    @property
    def description(self) -> str:
        if self.anatomic_region is not None:
            return f'{self.seg_type["CodeMeaning"]} in {self.anatomic_region["CodeMeaning"]}'
        return self.seg_type["CodeMeaning"]


# Keyed by the voxel value in the RCC-AID NIfTI label maps.
# Label 0 (Background) is deliberately absent -- see docs/decisions.md ADR-007.
SEGMENTS: dict[int, Segment] = {
    1: Segment(
        label_id=1,
        name="Kidney",
        category=CAT_ANATOMICAL,
        seg_type=TYPE_KIDNEY,
        rgb=[212, 126, 151],
        anatomic_region=None,
        expected_cids={"category": 7150, "type": 7154},
    ),
    2: Segment(
        label_id=2,
        name="Tumor",
        category=CAT_MORPH_ABNORMAL,
        seg_type=TYPE_MASS,
        rgb=[144, 238, 144],
        anatomic_region=REGION_KIDNEY,
        expected_cids={"category": 7150, "type": 7159},
    ),
    3: Segment(
        label_id=3,
        name="Cyst",
        category=CAT_MORPH_ABNORMAL,
        seg_type=TYPE_CYST,
        rgb=[205, 205, 100],
        anatomic_region=REGION_KIDNEY,
        expected_cids={"category": 7150, "type": 7159},
    ),
}

BODY_PART_EXAMINED = "KIDNEY"

# Cohort prefix in the RCC-AID label filenames -> (IDC collection_id, RCC subtype).
# Subtype strings are the idc-comet controlled-vocabulary values, reused verbatim from
# collections/analysis/bamf_aimi_annotations.yaml.
COHORTS: dict[str, tuple[str, str]] = {
    "KIRC": ("tcga_kirc", "Kidney Renal Clear Cell Carcinoma"),
    "KIRP": ("tcga_kirp", "Kidney Renal Papillary Cell Carcinoma"),
    "KICH": ("tcga_kich", "Kidney Chromophobe"),
}
