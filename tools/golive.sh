#!/usr/bin/env bash
# London Walkabout data collector: one-paste go-live. Run from the unzipped london-walkabout folder:
#   bash tools/golive.sh
# Uses YOUR gh and gcloud logins. Prints no key values. Safe to re-run (each step checks first).
set -euo pipefail
OWNER="multisportusa-git"
NAME="london-walkabout-data"          # 'London-Walkabout' (private) already exists, so a distinct name
REPO="$OWNER/$NAME"

say(){ printf '\n== %s\n' "$*"; }

say "0/7 checks"
gh auth status >/dev/null || { echo "Run: gh auth login"; exit 1; }
gcloud auth list --filter=status:ACTIVE --format='value(account)' | grep -q . || { echo "Run: gcloud auth login"; exit 1; }
[ -f collector/collect.py ] || { echo "Run this from the unzipped london-walkabout folder"; exit 1; }

say "1/7 tests"
python3 -m unittest discover -s tests 2>&1 | tail -3

say "2/7 create public repo $REPO and push (history 15 Sep -> today included)"
if ! gh repo view "$REPO" >/dev/null 2>&1; then
  [ -d .git ] || { git init -q -b main; git add -A; git commit -q -m "London Walkabout collector + history 15 Sep-24 Oct 2026"; }
  gh repo create "$REPO" --public --source=. --remote=origin --push \
     --description "Hourly TfL + National Rail record for London (Open Government Licence data)"
else
  echo "repo exists; pushing any local commits"; git push -u origin main
fi

say "3/7 GitHub Pages from Actions"
gh api -X POST "repos/$REPO/pages" -f build_type=workflow >/dev/null 2>&1 \
  || gh api -X PUT "repos/$REPO/pages" -f build_type=workflow >/dev/null
gh api "repos/$REPO/pages" --jq '"pages: " + .build_type + "  " + .html_url'

say "4/7 keyless read of tfl-app-key + rail-data-api-key from Secret Manager"
bash tools/setup-gcp-keyless.sh "$REPO"

say "5/7 first collection run (current hour)"
gh workflow enable collect.yml --repo "$REPO" >/dev/null 2>&1 || true
gh workflow enable watchdog.yml --repo "$REPO" >/dev/null 2>&1 || true
for i in 1 2 3 4 5 6; do gh workflow run collect.yml --repo "$REPO" --ref main && break; echo "waiting for GitHub to register the workflow..."; sleep 10; done
sleep 15
RUN=$(gh run list --repo "$REPO" --workflow collect.yml --limit 1 --json databaseId --jq '.[0].databaseId')
echo "run $RUN started: https://github.com/$REPO/actions/runs/$RUN (takes about 4 minutes)"

say "6/7 dashboard and app feed (live after that run)"
echo "dashboard: https://$OWNER.github.io/$NAME/"
echo "app feed:  https://$OWNER.github.io/$NAME/api/v1/manifest.json"

say "7/7 done. Tell Claude: 'live' plus this repo URL: https://github.com/$REPO"
echo "Optional, exact HH:00 starts: SETUP.md step 6 (cron-job.org)."
