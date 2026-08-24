# Methodology

<!--
Section headings only. Each section is a placeholder to be written.
-->

## Data source

## What "stablecoin supply" means here

## Peg types included

## Chain attribution

## Issuer attribution

## The "Other" bucket

## Bridged and unreleased supply

## Refresh cadence and as-of timing

Two mechanisms, and they are not the same thing.

The **snapshot** is refreshed by a GitHub Action at 06:12 UTC daily (monthly for
World Bank series) and committed to the repository. It is what renders the page,
and it is what every chart on the page is drawn from.

The **live layer** runs in the reader's browser after that render completes. It
re-derives the headline KPIs — total supply, 30-day change, top chain, top issuer
— straight from DefiLlama, using the same rules the pipeline uses, and replaces
them in place. It touches nothing else.

This means a single page can legitimately show two different as-of dates: a
headline fetched minutes ago, above charts drawn from this morning's snapshot.
That is not an inconsistency to be tidied away. Each panel prints its own "Data
as of" line naming its own source and age, and a headline badged `live` is
stating precisely that it did not come from the same place as the chart under it.

The live figure and the snapshot figure are computed by duplicated logic — Python
in `scripts/fetch_data.py`, JavaScript in `assets/app.js` — because the pipeline
and the page share no runtime and the project has no build step. The rounding and
peg-type rules are deliberately kept identical between them. If they ever drift,
the symptom is a headline total that visibly jumps when the live fetch lands, and
the fix is to reconcile the two, not to hide the jump.

A failed live fetch is silent to the reader by design and logged to the console
for the operator. The page falls back to the snapshot, which is the same thing it
would have shown before the live layer existed.

## Known limitations

## Corridor Proxy

This section covers the "Corridor signals" panel only. Everything in it is
inferred, and the inference is worth stating in full before any number from it
is quoted.

### What is actually measured

A saved Dune query (`queries/corridor_flows.sql`) sums USDT and USDC transfers
over the last 90 days where **both** the sending and the receiving address
resolve to a labelled centralised exchange in Dune's `cex.addresses` table,
grouped by venue pair, token and day. Transfers within a single venue are
excluded as wallet housekeeping.

So the underlying measurement is: *value moving between exchanges*. It is not
remittance data, and no step downstream turns it into remittance data.

### Venue mapping

`data/venue_markets.csv` maps each venue to a home market (ISO-3166 alpha-3) and
a World Bank region, with a confidence rating. A venue-pair flow becomes a
market-pair flow by looking up both ends. The mapping is hand-curated, it is the
weakest link in the module, and it is committed as data so it can be argued
with.

Every venue name in it has been checked against Dune's published label seed
(`cex_evms_addresses` in duneanalytics/spellbook) and has at least two labelled
EVM addresses behind it. This matters more than it sounds: a venue name that
does not match a Dune label exactly matches nothing at all, and a row that never
matches is indistinguishable from a market with no flow. Several obvious
candidates — VALR, Yellow Card, Rain, BitOasis, Buenbit — are absent from the
map for exactly this reason. They have no labelled addresses, so including them
would have implied coverage of South Africa, pan-African and Gulf flow that does
not exist.

Three rules constrain it:

* A flow touching a venue whose home market is `GLOBAL`, or a venue absent from
  the map, is **unattributed** and never assigned to a corridor. Unknown
  defaults to unattributed, so an unmaintained map understates corridors rather
  than inventing them.
* Two venues in the same market are **domestic**, not a corridor.
* A corridor inherits the **confidence of its weakest venue mapping**, shown in
  the table. `low` means the home-market claim itself is shaky — Panda Exchange
  operates across several Latin American countries, and pinning it to Colombia
  is a simplification, not a fact.

The panel leads with the share of labelled volume that survived these rules.
On real data that share is small. That is the honest result, not a defect to be
tuned away.

### The GLOBAL bucket

Binance, OKX, Bybit, Coinbase and Kraken serve every market at once. A transfer
from Bitso to Binance says nothing about where the money went next: it may have
been a Mexican user moving to a deeper order book, a market maker rebalancing,
or treasury movement with no user behind it at all. Attributing that flow to a
corridor would be inventing a geography, so it is counted, reported, and left
out of every corridor.

This is the single largest reason the attributed share is low. Most
exchange-to-exchange stablecoin volume touches a global venue at one end.

### Exchange-to-exchange undercount

The proxy misses more than it catches, and in a biased direction:

* **Both ends must be labelled.** A user withdrawing to self-custody, paying a
  merchant, or cashing out to a bank is invisible. Retail remittance mostly does
  not look like an exchange-to-exchange transfer.
* **Tron is not covered.** Dune's token transfer coverage is EVM chains plus
  Solana. Tron is the largest retail USDT rail in most of the markets this panel
  cares about, so every regional total is a floor.
* **Labels are community-maintained.** Global venues are labelled thoroughly;
  regional venues patchily or not at all. Two of the regions this panel most
  wants to describe are the worst served: sub-Saharan Africa has one labelled
  venue beyond Luno, and the Gulf has one. A venue with no labels is
  indistinguishable from a venue with no flow. The build writes out the venues
  it saw but could not map, and the mapped venues it never saw, so the gap stays
  visible.
* **Venues net internally and settle in batches.** One on-chain transfer can
  represent thousands of user transactions, or none.

### Cost comparison

The traditional figure is the World Bank's average cost of sending remittances
to the destination country. Remittance Prices Worldwide publishes true
corridor-level costs quarterly but blocks automated download, so what is fetched
is WDI `SI.RMT.COST.IB.ZS` — compiled from the same survey, averaged across all
of that country's corridors. Real corridor figures can be dropped in as
`data/rpw_corridors.csv` and take precedence; the panel labels which is in use
and prints the observation year, which currently runs two to three years behind.

The on-chain figure is an **assumption, not a measurement**: a blended network
fee plus a typical exchange withdrawal fee on a $200 send, set in `config.json`.
It excludes the on-ramp and off-ramp spreads, which is where most of the real
cost of this route sits. It is a floor.

The two are shown as separate bars rather than a ratio on purpose. A "12x
cheaper" headline would be the most quotable claim on the page and the least
defensible, since one side is an assumed fee and the other is a measured all-in
price including cash handling and FX.

### Traditional volume context

Where a KNOMAD bilateral matrix is available, the corridor shows that pair's
estimated annual volume. Where it is not, it falls back to total remittances
received by the destination country from all sources (`BX.TRF.PWKR.CD.DT`),
labelled as such. The two are an order of magnitude apart and the table says
which one it is showing.

### What this panel is for

A shape and a floor: which market pairs show meaningful exchange-to-exchange
stablecoin settlement, and how that is trending. It is not a measure of
stablecoin remittance volume, and any comparison to the traditional figures is a
comparison between a proxy and a measurement.

## Changelog
