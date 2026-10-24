# SETUP: go live in about 15 minutes

**Fast path (recommended):** unzip, `cd london-walkabout`, then `bash tools/golive.sh`. It runs the tests, creates the public repo `multisportusa-git/london-walkabout-data`, pushes the history, turns on Pages, sets up keyless access to your Secret Manager keys and starts the first run. It uses your own `gh` and `gcloud` logins and prints no keys. The numbered steps below are the same thing by hand, for reference.

What you get: twelve datasets covering every TfL mode (tube, DLR, Overground, Elizabeth line, tram, cable car, river bus, buses), National Rail operator status, departure boards at 25 stations the National Rail incidents and engineering-works feed and, daily, the National Rail stations feed, collected every hour on the hour, 24/7, stored in this git repo (never pruned), a dashboard on GitHub Pages rebuilt every hour, and a JSON feed for the London Commuter v3 app. Collection and dashboard use **0 LLM tokens**.

Keep the old Claude scheduled task running until step 9. Nothing in the old database is ever deleted.

You need: a GitHub account, Terminal on your Mac, an email address for TfL and cron-job.org sign-ups.

---

## Keys and security (read once)
- **Never send a key to Claude, by chat, email, a file or the project.** Keys stay in Google Secret Manager (`london-commuter-broker`); the workflow reads them at run time through keyless sign-in (step 5) and GitHub masks them in logs. Claude never needs to see them.
- The collector puts the TfL key only into the request it sends. It never writes the key to data, health lines, logs or the dashboard (tested: a planted key was searched for in every output, compressed files included, and not found).
- A wrong key does not lose data: TfL refuses it, the collector drops it, carries on anonymously and turns the run red ("TfL rejected TFL_APP_KEY") so you get an email.
- The only credential stored outside GitHub is the token in step 6. It goes in cron-job.org's request header. It can do one thing: start this repo's workflows (Actions read/write on this repo only, nothing else). If it ever leaks, revoke it in GitHub and make a new one.
- National Rail: `rail-data-api-key` (departure boards) and `rail-kb-incidents-key` (incidents feed, checked working 24 Oct: 928 incidents) and `rail-kb-stations-key` (stations feed, daily, checked working 24 Oct: 2,614 stations) are read the same way, as `NR_API_KEY`, `NR_KB_INCIDENTS_KEY` and `NR_KB_STATIONS_KEY`.
- The repo is public, so its Actions logs are public: keys are masked there and never printed by the collector.

## 1. Unpack (2 min)
```
cd ~/Downloads
unzip london-walkabout.zip
cd london-walkabout
python3 -m unittest discover -s tests
```
Expect `Ran 44 tests` and `OK`. If not, stop and send me the output.

## 2. Create the public repo and push (5 min)
With the GitHub CLI (`gh auth status` shows you logged in):
```
git init -b main
git add -A
git commit -m "initial import: collector + history 18 Sep-24 Oct 2026"
gh repo create london-walkabout --public --source=. --remote=origin --push
```
Without the CLI: create an empty **public** repo named `london-walkabout` at <https://github.com/new> (no README, no licence), then run the first three lines above and:
```
git remote add origin https://github.com/multisportusa-git/london-walkabout-data.git
git push -u origin main
```
Check: the repo page shows folders `collector`, `data`, `docs`, `.github`.

## 3. Turn on Pages (1 min)
Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.

## 4. Run the collector once (3 min)
Repo → **Actions** (enable workflows if asked) → **collect** → **Run workflow** → branch `main` → **Run workflow**.
After 3–4 minutes expect:
- jobs `collect` and `deploy` green, `alert` grey (skipped);
- a new commit `data: hourly collection …`;
- the dashboard at `https://multisportusa-git.github.io/london-walkabout-data/` (first deploy can take a few minutes).

A yellow warning "TFL_APP_KEY is not set" is expected until step 5.

## 5. Keys: read from your existing Secret Manager (10 min)
Your TfL and National Rail keys already live in Google Secret Manager (project `london-commuter-broker`). The collector reads them there at run time; **no key is copied to GitHub, your Mac or any file.**

1. Open `tools/setup-gcp-keyless.sh`, set `REPO` on line 6 to your `owner/london-walkabout` (exact case).
2. Run it: `bash tools/setup-gcp-keyless.sh` (uses your own gcloud and gh logins; prints no secret values). It:
   - creates service account `lw-collector` with no keys of its own, allowed to read only `tfl-app-key` and `rail-data-api-key`;
   - lets only this repo's `main` branch on GitHub act as it (Workload Identity Federation: short-lived tokens, nothing stored);
   - sets two repo *variables* (not secrets): where to sign in, and which account.
3. Actions → **collect** → **Run workflow**. Expect green, and in `data/_health/<today>.ndjson` the newest line shows `"keyUsed":true,"keyRejected":false`.

Fallbacks, automatic: if the sign-in or read fails, the run shows a yellow warning and collects anonymously, so no hour is lost. A red run saying "TfL rejected TFL_APP_KEY" means the key itself was refused.

*Only if you'd rather not use Secret Manager from GitHub:* copy the key into a GitHub secret without it ever showing on screen:
`gcloud secrets versions access latest --secret=tfl-app-key --project=london-commuter-broker | gh secret set TFL_APP_KEY --repo multisportusa-git/london-walkabout-data`
(This makes a second copy, which you would then rotate in two places.)

### National Rail Knowledgebase keys (incidents hourly, stations weekly)
Each Rail Data Marketplace product has its own consumer key. Store them without showing them (paste, then Ctrl-D):
Both are already stored (24 Oct). For a future key, copy it, then:
`printf '%s' "$(pbpaste)" | gcloud secrets create <name> --data-file=- --project=london-commuter-broker`
Then re-run `bash tools/setup-gcp-keyless.sh` to grant read access. Until a key exists, that dataset is simply not collected (no gap, no failure).

## 6. Exact on-the-hour trigger (10 min)
GitHub's own schedule (runs at :50 and :05) is best-effort. This adds an exact HH:00 start; the GitHub schedule stays on as the backup. Duplicate starts are harmless (each hour is written once).

**6a. Token that can only start this workflow**
GitHub → your avatar → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
- Name: `london-walkabout-hourly`
- Expiration: the longest offered (put a calendar reminder a week before it expires)
- Repository access: **Only select repositories** → `london-walkabout`
- Permissions → Repository permissions → **Actions: Read and write** (nothing else)
- Generate, copy the token (`github_pat_…`).

**6b. cron-job.org job** (free): sign up at <https://console.cron-job.org>, then **Create cronjob**
- Title: `London Walkabout hourly`
- URL: `https://api.github.com/repos/multisportusa-git/london-walkabout-data/actions/workflows/collect.yml/dispatches`
- Execution schedule: **Custom** → minutes `0`, every hour, every day; time zone **UTC**
- **Advanced** tab:
  - Request method: **POST**
  - Headers:
    - `Authorization` = `Bearer github_pat_…` (your token)
    - `Accept` = `application/vnd.github+json`
    - `X-GitHub-Api-Version` = `2026-03-10`
    - `Content-Type` = `application/json`
  - Request body: `{"ref":"main"}`
- **Notifications**: tick "execution of the cronjob fails".
- Save, then use **Test run**. Expect HTTP **200** or **204**, and a new `collect` run in your Actions tab within a minute.

## 7. Tell me the repo URL
I check, without any token: today's data lines, `_health` showing all datasets ok (12 with all National Rail keys; stations once a day) and 0 HTTP 429s, the dashboard and app feed loading, `verify.py` clean, and one hour compared against the old database.

## 8. Let 2–3 hours land
Each hour you should see one `collect` run (plus quick no-op runs) and one data commit. The dashboard's "Now" board updates every hour.

## 9. Cut over (you say "go")
1. I disable the old Claude scheduled task `trig_01DuLXwq7QYtDfJKnUXSUAjY`. Its token cost stops (about 3.7M tokens per hour).
2. I export the readings it made after this zip was built (17:00 UTC 24 Oct onwards) and send you a small `delta` folder. You run:
   ```
   python3 tools/backfill_from_export.py --export delta --out data --append-missing
   git add data && git commit -m "data: import old-collector hours before cutover" && git pull --rebase && git push
   ```
   This only appends hours the repo does not already have; it never changes or removes a line.
3. The old database stays untouched as an archive.

---

## Daily token budget
| Item | Tokens/day |
|---|---|
| Collection (GitHub Actions, Python) | 0 |
| Dashboard and app feed (GitHub Pages) | 0 |
| Watchdog, gap issues, failure emails (GitHub) | 0 |
| Old Claude scheduled task, until step 9 | about 89M (stops at cutover) |
| Asking Claude to check or change something | only when you ask; a typical check is 50–200K |

Target 250K/day is met as soon as the old task is off, with no recurring AI job at all.

## If something goes wrong
- **Red `collect` run or GitHub email**: open the run; `data/_health/<today>.ndjson` shows which dataset failed and why. The hour's other data is still committed.
- **cron-job.org failure email**: usually an expired token (repeat 6a, update the header). The GitHub schedule keeps collecting meanwhile.
- **Gap issue opened by the watchdog**: more than 2 hours missing in 48 h; check the Actions tab for that time.
- **Never** run `tools/backfill_from_export.py` without `--append-missing` on this repo; it refuses anyway once live data exists.
