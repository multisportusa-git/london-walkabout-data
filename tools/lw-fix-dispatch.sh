#!/bin/bash
# Puts a new GitHub dispatch token into Secret Manager and both Cloud Scheduler jobs. Prints no token.
# Run it first, then make/copy the token in GitHub and paste it at the hidden prompt (see SETUP.md step 6).
set -euo pipefail
P=london-commuter-broker; R=multisportusa-git/london-walkabout-data; L=europe-west2
printf 'Paste the GitHub token (input hidden), then press Enter: '; read -rs T; echo
case "$T" in github_pat_*|ghp_*) ;; *) echo "That doesn't look like a GitHub token (it should start github_pat_). Nothing changed."; exit 1;; esac
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $T" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$R/actions/workflows/collect.yml")
[ "$code" = 200 ] || { echo "GitHub refused the token (HTTP $code). Check: repository london-walkabout-data only, Actions: Read and write. Nothing changed."; exit 1; }
printf '%s' "$T" | gcloud secrets versions add lw-dispatch-token --data-file=- --project=$P >/dev/null
for N in lw-collect-hourly lw-collect-backup; do
  gcloud scheduler jobs update http "$N" --project=$P --location=$L --update-headers="Authorization=Bearer $T" >/dev/null
done
unset T
gcloud scheduler jobs run lw-collect-hourly --project=$P --location=$L
echo "Token stored, both jobs updated, test run sent. DONE"
