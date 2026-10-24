#!/usr/bin/env bash
# One-time: let GitHub Actions in ONE repo read TWO secrets from Secret Manager, with no stored key.
# Run on your Mac with your own gcloud and gh logins. Prints no secret values.
set -euo pipefail

REPO="${1:-multisportusa-git/london-walkabout-data}"   # owner/repo, exact case as on GitHub
PROJECT="london-commuter-broker"
POOL="github-actions"
PROVIDER="london-walkabout"
SA_NAME="lw-collector"
SECRETS=("tfl-app-key" "rail-data-api-key" "rail-kb-incidents-key" "rail-kb-stations-key")   # read-only; broker key not granted

SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

echo "1/6 enable APIs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com secretmanager.googleapis.com --project "$PROJECT"

echo "2/6 service account $SA (no keys are ever created for it)"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "London Walkabout collector: reads 2 secrets"

echo "3/6 read access to exactly these secrets: ${SECRETS[*]}"
for s in "${SECRETS[@]}"; do
  if gcloud secrets describe "$s" --project "$PROJECT" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT" \
      --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null && echo "   granted: $s"
  else
    echo "   not created yet (skipped; re-run this script after creating it): $s"
  fi
done

echo "4/6 workload identity pool + GitHub provider (only $REPO, branch main)"
gcloud iam workload-identity-pools describe "$POOL" --project "$PROJECT" --location global >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --project "$PROJECT" --location global --workload-identity-pool "$POOL" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT" --location global \
    --workload-identity-pool "$POOL" --display-name "london-walkabout" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "assertion.repository=='${REPO}' && assertion.ref=='refs/heads/main'"

echo "5/6 allow only that repo to act as the service account"
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" >/dev/null

echo "6/6 tell the repo where to sign in (these are not secrets)"
WIF="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
gh variable set GCP_WIF_PROVIDER --repo "$REPO" --body "$WIF"
gh variable set GCP_SA_EMAIL --repo "$REPO" --body "$SA"

echo "Done. Next: Actions > collect > Run workflow, then check data/_health/<today>.ndjson shows \"keyUsed\":true."
