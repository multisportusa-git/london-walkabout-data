# Data feed for apps (London Commuter v3)

Everything is plain HTTPS JSON, no key, rebuilt every hour a few minutes after HH:00. GitHub Pages and raw.githubusercontent.com both send `Access-Control-Allow-Origin: *` (cache: Pages 10 min, raw 5 min).

Role: this is the **history** (typical crowding, reliability by hour, lift outage records, past disruptions). The app's live board should call TfL directly every 10–30 s as its spec says; an hourly archive is never fresh enough for "next train".

Base: `https://multisportusa-git.github.io/london-walkabout-data/api/v1/`

| File | Use | Size (Oct 2026) |
|---|---|---|
| `latest.json` | the newest full record of each dataset: line status, lift outages, station notices, crowding, arrivals | ~32 KB |
| `summary.json` | the dashboard's aggregates: coverage, per-line good-service %, lifts, stops, crowding curves by hour, arrivals, `now` board, `observations` | ~60 KB |
| `manifest.json` | what history exists: per dataset `firstSlot`, `lastSlot`, `slots`, `days[]` (or `firstDay`/`lastDay`/`fileSuffix`), plus `rawDataBase` | ~3 KB |
| `registry.json` | watched stations and piers: slug, NaPTAN ids (disruption, crowding, every arrivals id), categories, tracking flags; lines with operating hours | ~24 KB |

Full history (one file per dataset per UTC day):
`{rawDataBase}{dataset}/{YYYY-MM-DD}.ndjson`, e.g. `https://raw.githubusercontent.com/multisportusa-git/london-walkabout-data/main/data/lineStatus/2026-10-24.ndjson`

Datasets: `arrivalsNetwork` (from 25 Oct 2026, `.ndjson.gz`: every live prediction on the whole network, rows per mode in `modes.{tube,elizabeth-line,overground,dlr,tram,river-bus}`, in the record's `fields` order; cable car and Woolwich Ferry publish none), `liftDisruptions`, `lineStatus`, `stopDisruptions`, `crowdingReadings`, `arrivalsReadings`, `arrivalsDetail` (file suffix `.ndjson.gz`, multi-member gzip: decompress the whole file as one stream), `busLineStatus`, `networkDisruptions` and `crowdingNetwork` (all `.ndjson.gz`; busLineStatus also has plain `.ndjson` history for 15–30 Sep 2026). Use the manifest's `files` list for exact file names.

National Rail (`.ndjson.gz`): `nationalRailDepartures` (from 25 Oct 2026 the full next 60 minutes of departures from HH:00 UK time at 33 stations, earlier records the next 10 trains; rows in the record's `fields` order, which from 25 Oct adds `trainid`, `category`, `loadingPct` (typical loading of the train, e.g. Overground) and `coachLoading` ([[coach, %], …] where the operator publishes it)) and `nationalRailIncidents` (Knowledgebase incidents feed). Each incidents record lists every incident in force in `active` (rows in `activeFields` order: id, version, planned, priority, start, end, operators, cleared) plus `count`, `planned`, `byOperator`; the full incident XML is stored once, in `newOrChanged`, the first hour an incident or a new version of it appears. To rebuild any hour: take its `active` ids and versions and look up each one's XML in the latest earlier `newOrChanged` entry with that id and version.

`nationalRailStations` (`.ndjson.gz`, once a day from 03:00 UTC): the Knowledgebase stations feed (2,614 stations: facilities, lifts, step-free access, staffing, ticket buying, transport links, alerts). Each record has `index` (CRS code → fingerprint of that station's entry), `changed` (the full entry of every station new or changed since the previous day; all stations on the first day) and `removed`. A station's entry on any day is its latest `changed` entry up to that day.

`stationAccess` (plain `.ndjson`, once a day from 03:00 UTC): TfL's station accessibility data. Each record names a ZIP (`file`, e.g. `_reference/tfl-stationdata/<sha>.zip` under the raw data base) holding `Lifts.csv` (every lift, the areas it links), `Stations.csv`, `Platforms.csv`, step-free paths and toilets. A new ZIP is stored only when TfL changes the content. The dashboard's lift stoplight (`summary.json` → `lifts.board`: per station `working`, `total`, `colour` red/amber/green, `stepFreeLost`, `since`, `detail`) is computed from it plus `liftDisruptions`.

- `lineStatus` statuses carry `validity` ([from, to] pairs) and `disruption` (category, created, additionalInfo, affectedStops NaPTAN ids) from 24 Oct 2026; older records have severity/description/reason only.
- `reason` is TfL's own message, stored in full (never truncated) for every status on every mode, buses included. For National Rail operators TfL's `reason` is only a nationalrail.co.uk link, so from 25 Oct 2026 (10:00 UTC) such a status also carries `nrIncident` = {`id`, `version`, `summary`, `routes`, `message`, `start`, `end`, `planned`}: the National Rail Knowledgebase incident that link names, in words (`message` is plain text, one paragraph per line, list items start with "• "). It is absent when no incident matches; nothing is invented. Older hours can be joined the same way: the link appears in that incident's `InfoLinks/Uri` in `nationalRailIncidents` `newOrChanged[].xml`.
- `arrivalsDetail` rows are arrays in the order given by the record's `fields` list: naptanId, lineId, platformName, direction, destinationNaptanId, timeToStation (seconds after `polledAt`), vehicleId.

Record rules:
- One JSON object per line. `slot` = the UTC hour it represents (`2026-10-24T18:00:00Z`); `polledAt` = when it was actually fetched.
- A slot can appear twice (e.g. a top-up); keep the last one.
- Records imported from the old database carry `legacyDocId`; older records can use earlier field shapes (see `collector/aggregate.py` for the tolerated variants).
- `closureWindow: true` marks London 00:00–04:59 and 23:30–23:59.

Versioning: `v1` only ever gains fields. A breaking change would ship as `api/v2/` alongside v1.

Swift sketch:
```swift
let url = URL(string: "https://multisportusa-git.github.io/london-walkabout-data/api/v1/latest.json")!
let (data, _) = try await URLSession.shared.data(from: url)
let latest = try JSONSerialization.jsonObject(with: data) as! [String: Any]
```

## Daily rollups (from 25 Oct 2026)
`…/rollups/index.json` lists the days (`days`), when each dataset was first recorded (`covFirst`), archive bytes per dataset (`bytes`), and `live` = hours recorded since go-live (24 Oct 21:00 UTC) per dataset plus the current complete-hour `streak`. `…/rollups/day/<YYYY-MM-DD>.json` (UTC day) holds, per hour (array index = UTC hour): `lines.<id>.c` status category (0 good … 5 closed), `lines.<id>.r` message → hours in force, `m` network metrics (buses, NR boards, NR incidents, lifts, station notices), `crowd` % of normal per tracked station, `cov` which datasets were recorded, `runs` (each collector run: start, seconds, requests, retries, 429s, NR status codes, failed datasets) and `bytes` stored per dataset. Built by `collector/rollup.py` each hourly run; derived from the raw files, never removed.
