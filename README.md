# London Walkabout – TfL hourly collector

Collects nine datasets (every TfL mode plus National Rail operator status) **every hour, on the hour**, and stores them as plain files in this git repo. It runs on GitHub Actions: **cost $0, zero LLM tokens**. TfL has no historical backfill, so this repo *is* the historical record.

| Dataset | What | Calls/hour |
|---|---|---|
| `liftDisruptions` | network-wide lift outages | 1 |
| `lineStatus` | status of 50 lines: tube, DLR, Overground, Elizabeth line, tram, cable car, replacement bus, river bus and 25 National Rail operators (incl. Southeastern, Thameslink, Heathrow/Gatwick Express) | 2 |
| `stopDisruptions` | disruptions at 75 stations + 23 piers | 5 |
| `crowdingReadings` | live crowding % of baseline, 33 tracked stations | 33 |
| `arrivalsReadings` | predicted arrivals summary (count + next train per line), 33 stations, every tube/Elizabeth line/Overground/DLR platform group at each | 47 |
| `arrivalsDetail` | every live prediction (line, platform, direction, destination, seconds to arrival, vehicle), gzip-compressed per hour | shared |
| `busLineStatus` | all 675 bus routes: good-service routes counted, the rest with full detail (gzip); history from 15 Sep 2026 | 1 |
| `networkDisruptions` | every station, pier, tram stop and bus stop notice network-wide, plus bus and National Rail line disruptions (gzip) | 5 |
| `crowdingNetwork` | live crowding at all 315 tube and Elizabeth line stations (gzip) | 315 |

Everything is collected every hour of every day (nothing is skipped overnight); a `closureWindow` flag (London 00:00–04:59, 23:30–23:59) is stored as metadata so analysis can filter it out.

**Go-live runbook: [SETUP.md](SETUP.md).** App feed for London Commuter v3: [API.md](API.md).

## Setup (about 15 minutes)

1. On GitHub create a **new public repository** (e.g. `london-walkabout`). Do not add a README/licence there.
2. Push this folder: `git init -b main && git add -A && git commit -m "initial import" && git remote add origin https://github.com/multisportusa-git/london-walkabout-data.git && git push -u origin main`.
3. **TfL API key (do this before the first run).** Free at <https://api-portal.tfl.gov.uk> (register → Products → "500 Requests per min" → subscribe → copy the primary key). In the repo: **Settings → Secrets and variables → Actions → New repository secret**, name `TFL_APP_KEY`. Without it the collector still works anonymously, but GitHub's runners share IP addresses with many other users, and TfL does not publish its anonymous quota, so 429s are a real risk. Every run without a key shows a yellow warning.
4. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
5. **Actions tab** → enable workflows if prompted.
6. **Actions → collect → Run workflow** once. A manual run collects the current hour only and finishes in about 1–2 minutes: a green run, a new commit `data: hourly collection …`, and the dashboard at `https://multisportusa-git.github.io/london-walkabout-data/`.

From then on it runs by itself.

## How the timing works

GitHub's cron is best-effort and can start runs 5–30 minutes late, and occasionally drops a run. So the workflow fires at **:50** and the script *sleeps until exactly HH:00:00* before polling (`slot` = that hour). A **backup run at :05** fills in whatever is missing. Any run that starts within 45 minutes of an hour whose data is missing fills that hour late (the real `polledAt` is recorded); a run starting at minute 30 or later also waits for the next hour. Every record carries `slot` (the hour it represents) and `polledAt` (the real time).

If a dataset is still missing for an hour after retries, the run is marked failed (GitHub emails you) but the data and health line for that hour are still committed. A daily **watchdog** workflow opens a GitHub issue (label `collector-gap`) if more than 2 hourly slots are missing for any dataset in the last 48 h.

Optional, for exact on-the-hour starts: a free external cron (e.g. a Cloudflare Worker cron trigger at `0 * * * *`) calling this workflow's `workflow_dispatch` API with a fine-grained token limited to Actions on this repo. The GitHub schedules stay as the backup.

Hourly commits keep the repo active, which should keep GitHub's 60-day inactivity auto-disable of scheduled workflows from triggering; GitHub emails the owner before disabling one.

## Data layout

```
data/<dataset>/<YYYY-MM-DD>.ndjson   one JSON line per hourly record (UTC day of the slot)
data/arrivalsDetail/<YYYY-MM-DD>.ndjson.gz   same, gzip: one gzip member appended per hour (read with gzip -dc)
data/_health/<YYYY-MM-DD>.ndjson     one line per collected slot: per-dataset status, requests, retries, 429s, duration
config/registry.json                 watched stations/piers/lines (edit to add or remove stations)
docs/                                static dashboard (GitHub Pages), summary.json + latest.json rebuilt each run
collector/collect.py                 the collector (Python 3.10+, stdlib only)
collector/verify.py                  gap checker used by the watchdog
tests/test_collect.py                offline unit tests:  python -m unittest discover -s tests
```

Each record: `{"slot":"2026-10-24T16:00:00Z","polledAt":"…","authMode":"anonymous-no-key"|"app-key","source":"…", …dataset body}`. Record shapes match the history exported from the original database, so old and new data concatenate directly.

## Operating it

- **Health at a glance:** Actions tab (green/red), `data/_health/`, or run `python collector/verify.py --data data --hours 48`.
- **Run locally:** `python collector/collect.py --out /tmp/test --no-sleep --current-only` (add `--dry-run` to preview; `--datasets lineStatus,liftDisruptions` for a subset; `TFL_APP_KEY` env var for a key).
- **Backup / restore:** the git history *is* the backup – `git clone` (or Settings → download ZIP) gives everything. To restore, push the clone to a new repo and follow the setup steps again.
- **Adding stations:** edit `config/registry.json`; new stations appear from the next run (older rows simply lack them).
- **Why public?** The data is public TfL open data, and public repos get unlimited free Actions minutes and Pages. A private repo would use ~120 Actions minutes/day (the pre-hour sleep), above the free 2,000 min/month.

## Known limits

- **National Rail is not covered.** TfL's API has no departures for National Rail-only stations (Charlton, Fenchurch Street, Gatwick, Luton, Stansted, and the National Rail side of London Bridge, Victoria, Waterloo, Charing Cross, Cannon Street, Blackfriars, Marylebone). That needs a separate National Rail (Darwin) source and key; see the plan in the project docs.
- Crowding exists only where TfL publishes it (mostly tube stations; e.g. none for DLR at Woolwich Arsenal).

- GitHub cron can drift or drop runs; handled by pre-sleep, late fill, the :05 backup, failure emails and the watchdog. If both scheduled runs for an hour are dropped, that hour is lost (the optional external trigger removes this risk).
- A TfL outage means a gap that cannot be recovered; failed datasets get one retry pass after 45 s, individual calls retry 4× with backoff and `Retry-After` support.
- The collector never writes the API key to logs, records or URLs in stored data.

## Licence / attribution

Contains public sector information licensed under the Open Government Licence v3.0. Powered by TfL Open Data: <https://tfl.gov.uk/corporate/terms-and-conditions/transport-data-service>. Collector code: MIT.

## Retention and generated files

Nothing is ever pruned: the repo is the permanent record. Measured growth is about 33–38 KB of NDJSON per hour, roughly **300–340 MB a year uncompressed in the working tree and about 50 MB a year compressed in git**, well within GitHub's limits for many years. `docs/summary.json`, `docs/summary.js` and `docs/latest.json` are generated on every run and published to Pages but not committed. To preview locally: `python3 collector/aggregate.py --data data --out docs`, then open `docs/index.html`.
`data/_legacy/` holds the Interchange Navigator's own content (interchanges and the three registry collections) exactly as exported on 24 Oct 2026. `docs/report.html` is the one-off audit report of the history up to that date.
