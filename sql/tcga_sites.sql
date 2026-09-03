-- Clinical Trial Subject module values for the 91 RCC-AID patients, as IDC itself publishes them.
--
-- The RCC-AID segmentations carry the DOI of the converted dataset in
-- OtherClinicalTrialProtocolIDsSequence (0012,0023). That attribute lives in the Clinical Trial
-- Subject module, whose Type 1 and Type 2 attributes must accompany it -- see docs/decisions.md
-- ADR-013. Rather than invent those values, take the ones IDC already carries for these exact
-- patients in the source collections, so a consumer sees the same sponsor, protocol and tissue
-- source site on the segmentation as on the images it describes.
--
-- Regenerate metadata/tcga_sites.csv with:
--
--   bq query --project_id=<your project> --use_legacy_sql=false --format=csv --max_rows=2000 \
--     --nouse_cache < sql/tcga_sites.sql > metadata/tcga_sites.csv
--
-- The query is deliberately not restricted to the 91 RCC-AID patients, so it stands alone: it
-- returns every patient in the three collections that IDC gives a site for. Against IDC v24 that
-- is 941 rows, exactly one per patient -- the (SiteID, SiteName) pair is unique per patient, and
-- all 91 RCC-AID patients are present. rcc_aid.clinical_trial re-checks all of that at load time.

SELECT
  PatientID,
  collection_id,
  ClinicalTrialSponsorName,
  ClinicalTrialProtocolID,
  ClinicalTrialProtocolName,
  ClinicalTrialSiteID,
  ClinicalTrialSiteName
FROM `bigquery-public-data.idc_current.dicom_all`
WHERE collection_id IN ('tcga_kirc', 'tcga_kirp', 'tcga_kich')
  AND ClinicalTrialSiteID IS NOT NULL AND ClinicalTrialSiteID != ''
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY PatientID
