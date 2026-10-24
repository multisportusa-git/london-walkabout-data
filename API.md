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

Datasets: `liftDisruptions`, `lineStatus`, `stopDisruptions`, `crowdingReadings`, `arrivalsReadings`, `arrivalsDetail` (file suffix `.ndjson.gz`, multi-member gzip: decompress the whole file as one stream), `busLineStatus`, `networkDisruptions` and `crowdingNetwork` (all `.ndjson.gz`; busLineStatus also has plain `.ndjson` history for 15–30 Sep 2026). Use the manifest's `files` list for exact file names.

National Rail (`.ndjson.gz`): `nationalRailDepartures` (boards at the watched stations as at HH:00 UK time; rows in the record's `fields` order) and `nationalRailIncidents` (Knowledgebase incidents feed). Each incidents record lists every incident in force in `active` (rows in `activeFields` order: id, version, planned, priority, start, end, operators, cleared) plus `count`, `planned`, `byOperator`; the full incident XML is stored once, in `newOrChanged`, the first hour an incident or a new version of it appears. To rebuild any hour: take its `active` ids and versions and look up each one's XML in the latest earlier `newOrChanged` entry with that id and version.

`nationalRailStations` (`.ndjson.gz`, once a day from 03:00 UTC): the Knowledgebase stations feed (2,614 stations: facilities, lifts, step-free access, staffing, ticket buying, transport links, alerts). Each record has `index` (CRS code → fingerprint of that station's entry), `changed` (the full entry of every station new or changed since the previous day; all stations on the first day) and `removed`. A station's entry on any day is its latest `changed` entry up to that day.

- `lineStatus` statuses carry `validity` ([from, to] pairs) and `disruption` (category, created, additionalInfo, affectedStops NaPTAN ids) from 24 Oct 2026; older records have severity/description/reason only.
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
