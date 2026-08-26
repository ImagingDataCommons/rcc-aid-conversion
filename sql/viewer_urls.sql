-- RCC-AID segmentations in the review DICOM store: one row per segmentation series,
-- with an OHIF viewer URL and the metadata worth reviewing.
--
-- Source table is the BigQuery export of the Healthcare DICOM store:
--   gcloud healthcare dicom-stores export bq rcc-aid-pilot \
--     --dataset=fedorov-dev-healthcare --location=us-central1 --project=idc-sandbox-000 \
--     --bq-table=bq://idc-sandbox-000.andrey_cohorts.rcc_aid_dicom_metadata
--
-- Expect 258 rows: 129 LABELMAP (SeriesNumber 300) + 129 BINARY (301), over 106 studies
-- and 91 patients. Re-run the export after any re-conversion -- it is a snapshot, not a view.

WITH params AS (
  SELECT
    'https://viewers-sandbox-gha-testing.web.app/viewer' AS viewer_base,
    'projects/idc-sandbox-000/locations/us-central1/datasets/fedorov-dev-healthcare/dicomStores/rcc-aid-pilot'
      AS dicom_store
)
SELECT
  m.PatientID,
  FORMAT('%s?StudyInstanceUIDs=%s&gcp=%s', p.viewer_base, m.StudyInstanceUID, p.dicom_store)
    AS viewer_url,

  -- Which flavour. 300 = LABELMAP, 301 = BINARY; both are present for every case, so a
  -- study opens with the two side by side on the same images.
  m.SegmentationType,
  SAFE_CAST(m.SeriesNumber AS INT64) AS SeriesNumber,
  m.SeriesDescription,
  m.BodyPartExamined,

  -- Segment content.
  -- LABELMAP objects also carry segment 0 = Background (125040, DCM), which BINARY does not.
  -- It is excluded everywhere below so the two flavours are directly comparable: without this
  -- the same case reads as 3 segments as LABELMAP and 2 as BINARY.
  (SELECT COUNT(*)
     FROM UNNEST(m.SegmentSequence) s
    WHERE IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue, '') != '125040')
    AS n_segments,
  (SELECT STRING_AGG(s.SegmentLabel, ' + ' ORDER BY s.SegmentNumber)
     FROM UNNEST(m.SegmentSequence) s
    WHERE IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue, '') != '125040')
    AS segments,
  (SELECT STRING_AGG(
            FORMAT('%d:%s=(%s,%s,"%s")',
                   s.SegmentNumber,
                   IFNULL(s.SegmentLabel, '?'),
                   IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue, '?'),
                   IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodingSchemeDesignator, '?'),
                   IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeMeaning, '?')),
            ' | ' ORDER BY s.SegmentNumber)
     FROM UNNEST(m.SegmentSequence) s
    WHERE IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue, '') != '125040')
    AS segment_coded_types,

  -- Flags for filtering. Cyst is absent from many cases, so this is the quick way to find
  -- the ones that exercise all three structures.
  EXISTS(SELECT 1 FROM UNNEST(m.SegmentSequence) s
          WHERE s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue = '64033007')  AS has_kidney,
  EXISTS(SELECT 1 FROM UNNEST(m.SegmentSequence) s
          WHERE s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue = '4147007')   AS has_mass,
  EXISTS(SELECT 1 FROM UNNEST(m.SegmentSequence) s
          WHERE s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue = '367643001') AS has_cyst,

  -- STRING_AGG(DISTINCT ...) rather than ANY_VALUE: a case whose segments genuinely disagreed
  -- would show as 'MANUAL,SEMIAUTOMATIC' instead of silently reporting whichever came first.
  (SELECT STRING_AGG(DISTINCT s.SegmentAlgorithmType, ',')
     FROM UNNEST(m.SegmentSequence) s
    WHERE IFNULL(s.SegmentedPropertyTypeCodeSequence[SAFE_OFFSET(0)].CodeValue, '') != '125040')
    AS SegmentAlgorithmType,
  m.ContentCreatorName.Alphabetic.FamilyName AS ContentCreatorName,

  -- Extent. BINARY skips empty frames, so its frame count is far below LABELMAP's for the
  -- same case; both reference only the instances they actually encode.
  SAFE_CAST(m.NumberOfFrames AS INT64) AS n_frames,
  ARRAY_LENGTH(m.ReferencedSeriesSequence[SAFE_OFFSET(0)].ReferencedInstanceSequence)
    AS n_referenced_ct_instances,
  m.`Rows`, m.`Columns`,

  -- Identity / provenance.
  m.StudyInstanceUID,
  m.SeriesInstanceUID,
  m.SOPInstanceUID,
  m.ReferencedSeriesSequence[SAFE_OFFSET(0)].SeriesInstanceUID AS referenced_ct_series_uid,
  m.StudyDescription,
  m.ContentDate

FROM `idc-sandbox-000.andrey_cohorts.rcc_aid_dicom_metadata` AS m
CROSS JOIN params AS p
WHERE m.Modality = 'SEG'
ORDER BY m.PatientID, m.StudyInstanceUID, SeriesNumber
