# Stablecoin Flows

[![Daily data refresh](https://github.com/byzfault/stablecoin-tracker/actions/workflows/daily.yml/badge.svg)](https://github.com/byzfault/stablecoin-tracker/actions/workflows/daily.yml)
[![Monthly World Bank refresh](https://github.com/byzfault/stablecoin-tracker/actions/workflows/monthly.yml/badge.svg)](https://github.com/byzfault/stablecoin-tracker/actions/workflows/monthly.yml)

A static dashboard tracking stablecoin supply across chains and issuers, sized
against US monetary aggregates, with an explicitly labelled proxy for
cross-border corridor flow.

The page makes no API calls. Everything it renders is JSON committed under
`data/`, refreshed by scheduled GitHub Actions. The site cannot break because an
upstream service is slow, rate limited or down, and it costs nothing to serve.

## Data pipeline

| Stage | Script | Sources | Cadence | Outputs |
| --- | --- | --- | --- | --- |
| Supply | `scripts/fetch_data.py` | DefiLlama, FRED | daily | `summary` `chains` `issuers` `matrix` `reference` `meta` |
| Corridor flows | `scripts/fetch_dune.py` | Dune (saved query) | daily | `corridor_flows` `dune_state` |
| Corridor build | `scripts/build_corridors.py` | the above + `venue_markets.csv` | daily | `corridors` |
| Remittance context | `scripts/fetch_worldbank.py` | World Bank, KNOMAD | monthly | `remittance_costs` `knomad_matrix` `countries` |
| Freshness gate | `scripts/check_freshness.py` | the committed snapshots | every run | exit status |

Two workflows drive it: `daily.yml` at 06:12 UTC and `monthly.yml` on the 3rd of
each month. Both commit whatever they retrieved, then run the freshness gate.

### Failure behaviour, and why the build goes red anyway

Every fetcher keeps the last good snapshot when an endpoint fails, records what
went wrong, and exits 0. A stale chart is worth far more than a broken one, and
a partial refresh should still publish the parts that worked.

That is right for the site and wrong for the operator, who would otherwise never
hear about it. So `check_freshness.py` runs last, *after* the data has been
committed and pushed, and fails the job if any snapshot has stopped refreshing
or if a fetcher recorded an error. The deploy is unaffected; the email is the
point. Affected panels carry a `stale` badge on the page itself.

### Freshness on the page

Every snapshot carries `fetched_at` in ISO 8601 UTC, and every panel prints
"Data as of …" with its source and refresh cadence. Two timestamps are kept
apart on purpose: what is *printed* is the age of the data, while *staleness* is
judged on when the pipeline last refreshed the file. FRED publishes M2 with a
multi-month lag — that is not a pipeline failure. FRED not being fetched for six
weeks is.

## Setup

Pages is served from the `main` branch (Settings → Pages → Deploy from a branch
→ `main` / root). The workflows push data commits to `main`; there is no
separate deploy step.

### DUNE_API_KEY

The corridor module needs a Dune API key. It is read from the environment and is
never committed.

1. Create a key at <https://dune.com/settings/api>.
2. Add it in the repo: Settings → Secrets and variables → Actions → New
   repository secret, named `DUNE_API_KEY`.
3. Paste `queries/corridor_flows.sql` into the Dune UI, save it, and copy the
   numeric query id from its URL.
4. Put that id in `config.json` as `corridor.dune_query_id`.

Until step 4 is done the corridor panel renders an explanatory empty state, the
freshness gate skips its files, and the rest of the dashboard is unaffected.

Locally: `DUNE_API_KEY=... python scripts/fetch_dune.py`.

### Credit discipline

Dune bills for execution and for reading results, so `fetch_dune.py` is
deliberately reluctant to spend. It probes a single row to read the cached
execution timestamp, downloads the cached result if it is under 24 hours old,
and triggers a new execution at most once per day. That rate-limit clock lives
in `data/dune_state.json`, which is committed so it survives ephemeral CI
runners. Every run appends what it spent to a credit log in the same file.

## Running locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/fetch_data.py
python3 -m http.server 8000        # then open http://localhost:8000
```

Serve over HTTP rather than opening `index.html` directly — browsers block
`fetch()` on `file://` URLs.

## Layout

```
index.html              The dashboard.
assets/                 Styles and the one script. No build step, no framework.
config.json             Query id, freshness windows, cost assumptions.
data/                   Committed snapshots. This is the point of the repo.
data/venue_markets.csv  Hand-curated venue → home market map.
queries/                SQL kept next to the code that consumes it.
scripts/                The pipeline.
METHODOLOGY.md          What every number means and what it does not.
```

## A note on the corridor module

It is a proxy, and the dashboard says so before it says anything else. Both ends
of every transfer must already be a labelled exchange address, so it measures
exchanges settling with each other rather than people sending money. Flows
touching a global venue are never assigned to a corridor. Coverage excludes
Tron, the largest retail USDT rail in most of the markets involved.

The numbers are a floor and a shape, not a measurement.
[METHODOLOGY.md](METHODOLOGY.md#corridor-proxy) sets out the whole method and
every limit worth knowing.

## Licence

MIT. Data belongs to its respective sources; this dashboard is not affiliated
with any of them.
