# Stablecoin Flows

[![Daily data refresh](https://github.com/byzfault/stablecoin-tracker/actions/workflows/daily.yml/badge.svg)](https://github.com/byzfault/stablecoin-tracker/actions/workflows/daily.yml)
[![Monthly World Bank refresh](https://github.com/byzfault/stablecoin-tracker/actions/workflows/monthly.yml/badge.svg)](https://github.com/byzfault/stablecoin-tracker/actions/workflows/monthly.yml)

A static dashboard tracking stablecoin supply across chains and issuers, sized
against US monetary aggregates, with an explicitly labelled proxy for
cross-border corridor flow.

Everything the page renders is JSON committed under `data/`, refreshed by
scheduled GitHub Actions. That snapshot paints first and is what the site
falls back to, so it cannot break because an upstream service is slow, rate
limited or down, and it costs nothing to serve.

Once that render is complete, the browser makes one further attempt to fetch the
headline figures live from DefiLlama. See [Live data](#live-data).

## Data pipeline

| Stage | Script | Sources | Cadence | Outputs |
| --- | --- | --- | --- | --- |
| Supply | `scripts/fetch_data.py` | DefiLlama, FRED | daily | `summary` `chains` `issuers` `matrix` `reference` `meta` |
| Corridor flows | `scripts/fetch_dune.py` | Dune (saved query) | daily | `corridor_flows` `dune_state` |
| Corridor build | `scripts/build_corridors.py` | the above + `venue_markets.csv` | daily | `corridors` |
| Remittance context | `scripts/fetch_worldbank.py` | World Bank, KNOMAD | monthly | `remittance_costs` `knomad_matrix` `countries` |
| Freshness gate | `scripts/check_freshness.py` | the committed snapshots | every run | exit status |
| Live headline | `assets/app.js` (`LIVE`) | DefiLlama, in the browser | every page load | replaces the headline KPIs in place |

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

## Live data

The committed snapshot renders the page. After that render finishes, the browser
fires one unawaited attempt to rebuild the headline KPIs — total supply, 30-day
change, top chain, top issuer — directly from DefiLlama. On success the headline
numbers are replaced in place and the panel meta line reads `fetched live` with a
green `live` badge. On any failure at all, nothing happens and the reader keeps
looking at the committed numbers.

Ordering is the entire design. Because the snapshot has already painted, the live
call is never on the critical path: a slow, rate-limited, CORS-blocked or garbled
response costs the reader nothing. Live is an upgrade applied to a page that
already works, never a precondition for it working. The failure path is a
`console.warn` and a no-op — deliberately visible to the operator, invisible to
the reader.

**What goes live, and what does not.**

| Panel | Live? | Why |
| --- | --- | --- |
| Headline KPIs | yes | Three requests, ~300KB compressed. These are the numbers people read and quote. |
| Supply charts | no | Backed by a date × issuer × chain cube. Rebuilding it in the browser means dozens of requests and several megabytes, to move a daily series forward by at most one point. |
| Share of US money | recomputed | The stablecoin numerator goes live; the FRED denominator does not need to. M2 publishes monthly with a multi-month lag. |
| Corridor signals | no | Dune requires an API key, which can never be shipped to a browser. Server-side only, refreshed by the Action. |

Each panel prints its own "Data as of" line, so a live headline sitting above a
snapshot-backed chart states both ages rather than conflating them.

**Be honest about what this buys.** DefiLlama's stablecoin supply updates roughly
daily, so on a healthy day live gains a few hours over the 06:12 UTC refresh. The
real value is the unhealthy day: if the Action fails, or its commit does not land,
the headline is still correct because the reader's own browser went and asked. It
closes the gap between "the pipeline is broken" and "the operator noticed" — a
gap the freshness gate reports but cannot itself repair.

`LIVE` at the top of `assets/app.js` holds the switch, the endpoint and the
timeout. Setting `enabled: false` restores the original zero-API-call behaviour
exactly.

### Why the key cannot simply go in the browser

Corridors stay server-side because a Dune API key shipped to a browser is a
published key — "minified" is not "hidden", and the network tab shows it either
way. Anyone loading the page could spend the account's credits. A serverless
proxy would fix that, at the cost of a runtime dependency the rest of this
design exists to avoid.

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
assets/app.js           Dashboard, plus the LIVE block and the live layer.
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
