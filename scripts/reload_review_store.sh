#!/usr/bin/env bash
# Replace the contents of the review DICOM store with the objects currently in data/seg_*/.
#
#   scripts/reload_review_store.sh --check     # report the diff, change nothing
#   scripts/reload_review_store.sh             # stage, delete, import, verify
#
# Why a full replace rather than an import on top. A Cloud Healthcare import rejects an instance only
# when the UIDs are identical, so it never overwrites: re-importing changed objects that kept their
# UIDs is refused outright, and re-importing reconverted objects (dcmqi mints fresh UIDs every run)
# silently accumulates orphans alongside the superseded series. Both failure modes were hit during
# this project -- see "Viewer verification" in docs/conversion-notes.md. The only correct update is
# to delete first.
#
# scripts/patch_clinical_trial.py preserves every SOPInstanceUID, so after the DOI patch the delete
# and the import cover exactly the same UIDs and the store ends up an exact replacement.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python

PROJECT=idc-sandbox-000
LOCATION=us-central1
DATASET=fedorov-dev-healthcare
STORE_ID=rcc-aid-pilot
STORE="projects/$PROJECT/locations/$LOCATION/datasets/$DATASET/dicomStores/$STORE_ID"
DICOMWEB="https://healthcare.googleapis.com/v1/$STORE/dicomWeb"
STAGING="gs://af-dev-storage/rcc-aid-all"

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
token() { gcloud auth print-access-token; }

step "local objects"
N_LOCAL=$(ls data/seg_labelmap/*.dcm data/seg_binary/*.dcm 2>/dev/null | wc -l | tr -d ' ')
echo "$N_LOCAL object(s) in data/seg_labelmap/ + data/seg_binary/"
[[ "$N_LOCAL" -eq 0 ]] && { echo "nothing to load" >&2; exit 1; }

step "current store contents"
TOKEN=$(token)
curl -sf -H "Authorization: Bearer $TOKEN" \
  "$DICOMWEB/instances?includefield=00080018&limit=5000" > /tmp/rcc_store_instances.json
$PY - <<'PY'
import json, glob, pydicom, sys
store = {i['00080018']['Value'][0] for i in json.load(open('/tmp/rcc_store_instances.json'))}
studies = {i['0020000D']['Value'][0] for i in json.load(open('/tmp/rcc_store_instances.json'))}
local = {}
for f in glob.glob("data/seg_labelmap/*.dcm") + glob.glob("data/seg_binary/*.dcm"):
    ds = pydicom.dcmread(f, stop_before_pixels=True)
    local[str(ds.SOPInstanceUID)] = f
print(f"store: {len(store)} instance(s) over {len(studies)} study(ies)")
print(f"local: {len(local)} object(s)")
print(f"in local but not in store: {len(set(local) - store)}")
print(f"in store but not in local: {len(store - set(local))}")
print("verdict: " + ("exact UID match -- this is an in-place replacement"
                     if store == set(local) else
                     "UID SETS DIFFER -- read the counts above before proceeding"))
json.dump(sorted(studies), open('/tmp/rcc_store_studies.json', 'w'))
PY

if [[ "$CHECK" -eq 1 ]]; then
  echo; echo "--check: nothing changed."
  exit 0
fi

step "stage to $STAGING"
# Two prefixes: the flavours share a basename, so a single prefix would collide.
gcloud storage cp data/seg_labelmap/*.dcm "$STAGING/labelmap/"
gcloud storage cp data/seg_binary/*.dcm "$STAGING/binary/"

step "DELETE every study currently in the store"
echo "This removes the superseded objects. They are recoverable from data/seg_*/ and from $STAGING."
read -r -p "type 'delete' to proceed: " CONFIRM
[[ "$CONFIRM" == "delete" ]] || { echo "aborted; staging left in place" >&2; exit 1; }

TOKEN=$(token)
$PY - "$DICOMWEB" "$TOKEN" <<'PY'
import json, sys, urllib.request, urllib.error
base, token = sys.argv[1], sys.argv[2]
studies = json.load(open('/tmp/rcc_store_studies.json'))
ok = fail = 0
for n, uid in enumerate(studies, 1):
    req = urllib.request.Request(f"{base}/studies/{uid}", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req) as r:
            r.read()
        ok += 1
    except urllib.error.HTTPError as e:
        fail += 1
        print(f"  FAIL {uid}: {e.code} {e.read()[:200]}", file=sys.stderr)
    if n % 25 == 0:
        print(f"  [{n}/{len(studies)}]", flush=True)
print(f"deleted {ok} study(ies), {fail} failure(s)")
sys.exit(1 if fail else 0)
PY

step "import the staged objects"
gcloud healthcare dicom-stores import gcs "$STORE_ID" \
  --dataset="$DATASET" --location="$LOCATION" --project="$PROJECT" \
  --gcs-uri="$STAGING/**.dcm"

step "verify by SOPInstanceUID, not by count"
# An instance count alone once read a partially-orphaned store as "everything imported".
TOKEN=$(token)
curl -sf -H "Authorization: Bearer $TOKEN" \
  "$DICOMWEB/instances?includefield=00080018&includefield=00120023&limit=5000" \
  > /tmp/rcc_store_after.json
$PY - <<'PY'
import json, glob, pydicom, sys
after = json.load(open('/tmp/rcc_store_after.json'))
store = {i['00080018']['Value'][0] for i in after}
local = {}
for f in glob.glob("data/seg_labelmap/*.dcm") + glob.glob("data/seg_binary/*.dcm"):
    ds = pydicom.dcmread(f, stop_before_pixels=True)
    local[str(ds.SOPInstanceUID)] = f
missing, extra = set(local) - store, store - set(local)
print(f"store: {len(store)} | local: {len(local)} | missing: {len(missing)} | extra: {len(extra)}")

# The point of the reload: the store must now serve the DOI.
def doi(inst):
    seq = inst.get('00120023', {}).get('Value') or []
    return seq[0].get('00120020', {}).get('Value', [''])[0] if seq else ''
dois = {}
for i in after:
    dois[doi(i)] = dois.get(doi(i), 0) + 1
print("dataset DOI as served by the store:", dois)
sys.exit(0 if not missing and not extra else 1)
PY

step "refresh the BigQuery export"
echo "gcloud healthcare dicom-stores export bq $STORE_ID \\"
echo "  --dataset=$DATASET --location=$LOCATION --project=$PROJECT \\"
echo "  --bq-table=bq://$PROJECT.andrey_cohorts.rcc_aid_dicom_metadata --overwrite-table"
echo
echo "then, once satisfied:"
echo "  gcloud storage rm -r $STAGING"
echo "  $PY scripts/patch_clinical_trial.py --drop-backups"
