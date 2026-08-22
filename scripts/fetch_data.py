#!/usr/bin/env python3
"""Fetch stablecoin supply data from DefiLlama and write normalised snapshots.

The dashboard is static: it makes no API calls of its own and renders entirely
from the JSON this script commits into ``data/``. That is a deliberate trade —
the site cannot break because an upstream API is slow, rate limited or down,
and it costs nothing to serve.

Outputs
-------
``data/summary.json``   Headline KPIs.
``data/chains.json``    Supply by chain over time, plus an "Other" bucket.
``data/issuers.json``   USDT / USDC / Other over time.
``data/meta.json``      Run metadata: when, from where, and what failed.
``data/raw/*.json.gz``  The upstream responses the outputs were derived from.

Failure behaviour
-----------------
An endpoint that fails is logged and skipped; the previous snapshot for the
affected output is left in place rather than being overwritten with a partial
or empty file. A stale chart is far better than a broken one, and ``meta.json``
records exactly what went wrong so the staleness is never silent.

Usage
-----
    python scripts/fetch_data.py [--output-dir data] [--skip-issuers]
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import requests

BASE_URL: Final = "https://stablecoins.llama.fi"

#: Chains broken out individually. Everything else is summed into "Other".
#:
#: This is the one list worth editing as the market moves — it is presentation,
#: not methodology. Ordered roughly by current supply so the stacked charts read
#: largest-first.
TRACKED_CHAINS: Final[tuple[str, ...]] = (
    "Ethereum",
    "Tron",
    "Solana",
    "BSC",
    "Base",
    "Arbitrum",
    "Polygon",
    "TON",
)

#: How many issuers to break out individually, ranked by current supply.
#:
#: Resolved from the live list rather than hard-coded ids, so the dashboard follows
#: the market instead of freezing 2026's league table into the source.
TOP_ISSUER_COUNT: Final = 6

#: Chains offered in the network dropdown, alongside "All" and a catch-all.
#:
#: Deliberately shorter than TRACKED_CHAINS: a filter with twenty options is a
#: worse filter, and these three carry 84% of all supply. The cube itself is built
#: across every tracked chain, so selecting a coin still yields the full
#: eight-chain breakdown in the chart — the short list constrains the control, not
#: the data behind it.
FILTER_CHAINS: Final[tuple[str, ...]] = ("Ethereum", "Tron", "Solana")

#: Monetary aggregates the market-size comparison is measured against.
#:
#: FRED serves these as CSV with no API key and no account, which keeps the "no
#: keys anywhere" rule intact. Values are USD billions. Each carries its own
#: as-of date because the series update on different schedules — currency in
#: circulation lags M2 by several months, and presenting them as equally current
#: would be misleading.
FRED_SERIES: Final[dict[str, dict[str, str]]] = {
    "CURRCIR": {
        "label": "US currency in circulation",
        "short": "US cash",
        "note": "Physical notes and coin — the closest analogue to a bearer digital dollar.",
    },
    "M1SL": {
        "label": "US M1 money supply",
        "short": "US M1",
        "note": "Currency plus demand deposits and other liquid deposits.",
    },
    "M2SL": {
        "label": "US M2 money supply",
        "short": "US M2",
        "note": "M1 plus savings, small time deposits and retail money-market funds.",
    },
}

FRED_CSV: Final = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

#: The peg type the dashboard reports.
#:
#: DefiLlama tracks EUR, JPY, RUB and others alongside USD. USD-pegged supply is
#: 99.5% of the total, and mixing currencies into one "supply" number would mean
#: silently applying FX rates. Reporting one peg type keeps the headline figure
#: something that can be stated precisely.
PEG_TYPE: Final = "peggedUSD"

#: Politeness delay between requests to a free, unauthenticated API.
REQUEST_DELAY_SECONDS: Final = 0.5
REQUEST_TIMEOUT_SECONDS: Final = 90
MAX_ATTEMPTS: Final = 3

logger = logging.getLogger("fetch_data")


class FetchError(RuntimeError):
    """An upstream endpoint could not be retrieved after retries."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch_json(session: requests.Session, path: str) -> Any:
    """GET ``path`` from the DefiLlama stablecoins API and return parsed JSON.

    Retries on network errors and 5xx responses with exponential backoff. A 4xx
    is treated as final: retrying a bad path only wastes time.

    Raises:
        FetchError: If the endpoint could not be retrieved.
    """
    url = f"{BASE_URL}{path}"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            if attempt == MAX_ATTEMPTS:
                raise FetchError(f"{path}: {exc}") from exc
            backoff = 2**attempt
            logger.warning("%s failed (%s), retrying in %ss", path, exc, backoff)
            time.sleep(backoff)
            continue

        if response.status_code >= 500:
            if attempt == MAX_ATTEMPTS:
                raise FetchError(f"{path}: HTTP {response.status_code}")
            backoff = 2**attempt
            logger.warning(
                "%s returned %s, retrying in %ss", path, response.status_code, backoff
            )
            time.sleep(backoff)
            continue

        if not response.ok:
            raise FetchError(f"{path}: HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"{path}: response was not valid JSON") from exc

    raise FetchError(f"{path}: exhausted retries")


def save_raw(raw_dir: Path, name: str, payload: Any) -> None:
    """Persist an upstream response for auditability, gzipped.

    Only the latest response is kept, and it is overwritten each run. Retaining
    every day's raw payload would add roughly 14 MB per run to the repository —
    several gigabytes a year — for a marginal gain over keeping the most recent
    snapshot alongside the outputs derived from it.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{name}.json.gz"
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def peg_value(container: Any, peg_type: str = PEG_TYPE) -> float:
    """Read one peg type out of a DefiLlama ``{pegType: amount}`` mapping.

    The API omits a peg type entirely rather than reporting zero, and has been
    observed returning ``null`` for it, so both mean "nothing here".
    """
    if not isinstance(container, dict):
        return 0.0
    value = container.get(peg_type)
    return float(value) if isinstance(value, (int, float)) else 0.0


def to_series(chart: list[dict[str, Any]]) -> dict[int, float]:
    """Convert a ``/stablecoincharts/*`` response into ``{unix_date: supply}``.

    Dates arrive as strings on some endpoints and integers on others.
    """
    series: dict[int, float] = {}
    for point in chart:
        raw_date = point.get("date")
        try:
            date = int(raw_date)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        series[date] = peg_value(point.get("totalCirculatingUSD"))
    return series


def round_supply(value: float) -> int:
    """Round to whole dollars.

    Sub-dollar precision on a multi-billion-dollar figure is noise, and float
    noise would otherwise produce a diff on every line of every file on every
    run, making the commit history useless for spotting real movements.
    """
    return int(round(value))


# --------------------------------------------------------------------------- #
# Builders — one per output file
# --------------------------------------------------------------------------- #


def build_chains(
    all_chart: list[dict[str, Any]],
    chain_charts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build the supply-by-chain time series, with a residual "Other" bucket.

    "Other" is the total minus the tracked chains rather than a sum of the
    remaining ~200 chains. That guarantees the stack always adds up to the
    reported total, so the 100%-share chart cannot drift.
    """
    totals = to_series(all_chart)
    per_chain = {chain: to_series(chart) for chain, chart in chain_charts.items()}

    dates = sorted(totals)
    series: dict[str, list[int]] = {chain: [] for chain in per_chain}
    series["Other"] = []

    for date in dates:
        tracked_total = 0.0
        for chain, values in per_chain.items():
            value = values.get(date, 0.0)
            series[chain].append(round_supply(value))
            tracked_total += value
        # Clamped at zero: a chain series can briefly exceed the total when the
        # upstream aggregates land at slightly different times.
        series["Other"].append(round_supply(max(totals[date] - tracked_total, 0.0)))

    return {
        "dates": dates,
        "series": series,
        "total": [round_supply(totals[date]) for date in dates],
    }


def build_issuers(
    all_chart: list[dict[str, Any]],
    issuer_series: dict[str, dict[int, float]],
) -> dict[str, Any]:
    """Build the issuer time series: each tracked issuer plus a residual "Other"."""
    totals = to_series(all_chart)
    dates = sorted(totals)

    series: dict[str, list[int]] = {name: [] for name in issuer_series}
    series["Other"] = []

    for date in dates:
        tracked_total = 0.0
        for name, values in issuer_series.items():
            value = values.get(date, 0.0)
            series[name].append(round_supply(value))
            tracked_total += value
        series["Other"].append(round_supply(max(totals[date] - tracked_total, 0.0)))

    return {"dates": dates, "series": series}


def build_summary(
    all_chart: list[dict[str, Any]],
    chains_now: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the headline KPIs.

    The 30-day change is measured against the series 30 points back rather than
    a calendar lookup, because the upstream series is daily and contiguous.
    """
    totals = to_series(all_chart)
    dates = sorted(totals)
    if not dates:
        raise FetchError("total supply chart contained no usable points")

    latest_date = dates[-1]
    latest = totals[latest_date]

    change_30d: float | None = None
    change_30d_pct: float | None = None
    if len(dates) > 30:
        previous = totals[dates[-31]]
        change_30d = latest - previous
        if previous:
            change_30d_pct = (latest - previous) / previous * 100

    ranked_chains = sorted(
        (
            (entry.get("name", "?"), peg_value(entry.get("totalCirculatingUSD")))
            for entry in chains_now
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    ranked_issuers = sorted(
        (
            (asset.get("symbol", "?"), peg_value(asset.get("circulating")))
            for asset in assets
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    chain_total = sum(value for _, value in ranked_chains) or 1.0
    issuer_total = sum(value for _, value in ranked_issuers) or 1.0

    top_chain, top_chain_value = ranked_chains[0] if ranked_chains else ("?", 0.0)
    top_issuer, top_issuer_value = ranked_issuers[0] if ranked_issuers else ("?", 0.0)

    return {
        "as_of": latest_date,
        "total_supply": round_supply(latest),
        "change_30d": round_supply(change_30d) if change_30d is not None else None,
        "change_30d_pct": round(change_30d_pct, 2) if change_30d_pct is not None else None,
        "top_chain": {
            "name": top_chain,
            "supply": round_supply(top_chain_value),
            "share_pct": round(top_chain_value / chain_total * 100, 2),
        },
        "top_issuer": {
            "name": top_issuer,
            "supply": round_supply(top_issuer_value),
            "share_pct": round(top_issuer_value / issuer_total * 100, 2),
        },
        "peg_type": PEG_TYPE,
    }


def build_matrix(
    all_chart: list[dict[str, Any]],
    chain_charts: dict[str, list[dict[str, Any]]],
    issuer_totals: dict[str, dict[int, float]],
    issuer_by_chain: dict[str, dict[str, dict[int, float]]],
) -> dict[str, Any]:
    """Build the issuer x chain cube that lets both filters apply at once.

    The two dropdowns are not independent views of the same numbers: answering
    "USDC on Solana" needs the cross-section, not a chain total and an issuer
    total. DefiLlama exposes it per asset under ``chainBalances``.

    Residual buckets on both axes are derived rather than summed, using
    inclusion-exclusion against the known totals::

        other_issuer[chain]  = total[chain]  - sum(tracked issuers on that chain)
        other_chain[issuer]  = total[issuer] - sum(tracked chains for that issuer)
        other_other          = grand total   - tracked chains - tracked issuers
                               + tracked cross-section

    Deriving them this way guarantees the cube reconciles to the reported total
    exactly, so a filtered view can never quietly disagree with the headline.
    """
    totals = to_series(all_chart)
    dates = sorted(totals)
    chain_totals = {chain: to_series(chart) for chain, chart in chain_charts.items()}

    issuers = sorted(issuer_totals)
    chains = [chain for chain in TRACKED_CHAINS if chain in chain_totals]

    cube: dict[str, dict[str, list[int]]] = {
        issuer: {chain: [] for chain in chains + ["Other"]} for issuer in issuers
    }
    cube["Other"] = {chain: [] for chain in chains + ["Other"]}

    # Upstream occasionally reports a negative balance for a chain — bridge
    # accounting during a token migration, for instance USDC on Arbitrum on
    # 2025-03-26. A negative segment makes a stacked area meaningless, so it is
    # floored at zero and the fact is counted and reported in meta.json rather
    # than being silently corrected away.
    clamped = 0

    for date in dates:
        grand = totals[date]
        tracked_chain_sum = 0.0
        tracked_issuer_sum = 0.0
        cross_sum = 0.0

        for chain in chains:
            chain_total = chain_totals.get(chain, {}).get(date, 0.0)
            tracked_chain_sum += chain_total
            on_chain = 0.0
            for issuer in issuers:
                value = issuer_by_chain.get(issuer, {}).get(chain, {}).get(date, 0.0)
                if value < 0:
                    clamped += 1
                    value = 0.0
                cube[issuer][chain].append(round_supply(value))
                on_chain += value
                cross_sum += value
            cube["Other"][chain].append(round_supply(max(chain_total - on_chain, 0.0)))

        for issuer in issuers:
            issuer_total = issuer_totals[issuer].get(date, 0.0)
            tracked_issuer_sum += issuer_total
            on_tracked = sum(
                issuer_by_chain.get(issuer, {}).get(chain, {}).get(date, 0.0) for chain in chains
            )
            cube[issuer]["Other"].append(round_supply(max(issuer_total - on_tracked, 0.0)))

        residual = grand - tracked_chain_sum - tracked_issuer_sum + cross_sum
        cube["Other"]["Other"].append(round_supply(max(residual, 0.0)))

    return {
        "dates": dates,
        "chains": chains + ["Other"],
        "issuers": issuers + ["Other"],
        "cube": cube,
        "total": [round_supply(totals[date]) for date in dates],
        "negative_cells_clamped": clamped,
    }


def fetch_reference(session: requests.Session, raw_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Fetch monetary aggregates from FRED for the market-size comparison.

    FRED serves these as CSV without a key or an account. Each series reports its
    own observation date: they update on different schedules, and currency in
    circulation typically lags M2 by months, so a single "as of" across all three
    would overstate how current the comparison is.
    """
    aggregates: list[dict[str, Any]] = []
    errors: list[str] = []

    for series_id, meta in FRED_SERIES.items():
        url = FRED_CSV.format(series_id=series_id)
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.error("FRED %s: %s", series_id, exc)
            errors.append(f"FRED/{series_id}: {exc}")
            continue

        rows = [line for line in response.text.strip().splitlines() if line]
        if len(rows) < 2:
            errors.append(f"FRED/{series_id}: no observations returned")
            continue

        # Trailing rows can be "." when an observation is not yet published.
        observation_date: str | None = None
        value: float | None = None
        for line in reversed(rows[1:]):
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                value = float(parts[1])
            except ValueError:
                continue
            observation_date = parts[0]
            break

        if value is None or observation_date is None:
            errors.append(f"FRED/{series_id}: no usable observation")
            continue

        aggregates.append(
            {
                "id": series_id,
                "label": meta["label"],
                "short": meta["short"],
                "note": meta["note"],
                # FRED reports these series in USD billions.
                "value_usd": round_supply(value * 1e9),
                "as_of": observation_date,
                "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
            }
        )
        save_raw(raw_dir, f"fred_{series_id}", {"csv": response.text})
        time.sleep(REQUEST_DELAY_SECONDS)

    return {
        "aggregates": sorted(aggregates, key=lambda item: item["value_usd"]),
        "source": "Federal Reserve Economic Data (FRED)",
        "source_url": "https://fred.stlouisfed.org/",
    }, errors


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def write_json(
    path: Path,
    payload: Any,
    *,
    source_name: str | None = None,
    source_url: str | None = None,
    cadence: str | None = None,
) -> None:
    """Write JSON with a trailing newline and stable key order.

    Sorted keys and a fixed separator keep daily commits to genuine changes
    rather than incidental reordering.

    Every snapshot is stamped with ``fetched_at`` and, where the caller supplies
    them, its source and refresh cadence. The dashboard prints those on the
    panel that renders the file: a figure shown without a date is asking to be
    believed on trust it has not earned, and a single generated_at in meta.json
    cannot say that reference.json is four months old while chains.json is four
    hours old.
    """
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("fetched_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if source_name:
            payload.setdefault("source_name", source_name)
        if source_url:
            payload.setdefault("source_url", source_url)
        if cadence:
            payload.setdefault("update_cadence", cadence)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")


def resolve_top_issuers(assets: list[dict[str, Any]]) -> dict[str, str]:
    """Pick the largest issuers by current supply, as ``{id: symbol}``.

    Resolved from live data so the breakout follows the market. Assets without an
    id or a symbol are skipped rather than guessed at.
    """
    ranked = sorted(
        (
            (str(asset.get("id")), str(asset.get("symbol")), peg_value(asset.get("circulating")))
            for asset in assets
            if asset.get("id") is not None and asset.get("symbol")
        ),
        key=lambda item: item[2],
        reverse=True,
    )
    return {asset_id: symbol for asset_id, symbol, _ in ranked[:TOP_ISSUER_COUNT]}


def fetch_issuer_series(
    session: requests.Session, raw_dir: Path, issuers: dict[str, str]
) -> tuple[dict[str, dict[int, float]], dict[str, dict[str, dict[int, float]]], list[str]]:
    """Fetch per-issuer history, both in total and broken down by chain.

    ``/stablecoin/{id}`` is by far the heaviest endpoint — roughly 20 MB for USDT
    — because it carries a full balance history for every chain the asset has
    ever touched. Only the aggregate series and the handful of filterable chains
    are kept; everything else is discarded before anything is written, which is
    also what keeps the archived raw copy small.
    """
    totals: dict[str, dict[int, float]] = {}
    by_chain: dict[str, dict[str, dict[int, float]]] = {}
    errors: list[str] = []

    def series_from(points: Any, key: str) -> dict[int, float]:
        out: dict[int, float] = {}
        if not isinstance(points, list):
            return out
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                date = int(point.get("date"))
            except (TypeError, ValueError):
                continue
            out[date] = peg_value(point.get(key))
        return out

    for asset_id, label in issuers.items():
        try:
            payload = fetch_json(session, f"/stablecoin/{asset_id}")
        except FetchError as exc:
            logger.error("issuer %s: %s", label, exc)
            errors.append(f"/stablecoin/{asset_id}: {exc}")
            continue

        if not isinstance(payload, dict):
            errors.append(f"/stablecoin/{asset_id}: unexpected response shape")
            continue

        tokens = payload.get("tokens")
        totals[label] = series_from(tokens, "circulating")

        chain_balances = payload.get("chainBalances") or {}
        kept_chains: dict[str, Any] = {}
        by_chain[label] = {}
        for chain in TRACKED_CHAINS:
            entry = chain_balances.get(chain)
            if not isinstance(entry, dict):
                continue
            by_chain[label][chain] = series_from(entry.get("tokens"), "circulating")
            kept_chains[chain] = entry.get("tokens")

        save_raw(
            raw_dir,
            f"stablecoin_{asset_id}",
            {
                "id": payload.get("id"),
                "name": payload.get("name"),
                "symbol": payload.get("symbol"),
                "pegType": payload.get("pegType"),
                "_note": (
                    "Trimmed: only the aggregate token series and the filterable chains are "
                    "retained. The full response carries every chain and is ~20 MB."
                ),
                "tokens": tokens,
                "chainBalances": kept_chains,
            },
        )
        logger.info("  %s: %d points", label, len(totals[label]))
        time.sleep(REQUEST_DELAY_SECONDS)

    return totals, by_chain, errors


def main() -> int:
    """Run the pipeline. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data"),
        help="Directory to write snapshots into (default: data)",
    )
    parser.add_argument(
        "--skip-issuers",
        action="store_true",
        help="Skip the per-issuer history, the slowest part of the run",
    )
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir: Path = args.output_dir
    raw_dir = out_dir / "raw"
    errors: list[str] = []
    written: list[str] = []
    data_notes: list[str] = []

    session = requests.Session()
    session.headers.update({"User-Agent": "stablecoin-flows/1.0 (+github.com/byzfault)"})

    def attempt(path: str, raw_name: str) -> Any | None:
        """Fetch and archive one endpoint, recording rather than raising failures."""
        try:
            payload = fetch_json(session, path)
        except FetchError as exc:
            logger.error("%s", exc)
            errors.append(str(exc))
            return None
        save_raw(raw_dir, raw_name, payload)
        time.sleep(REQUEST_DELAY_SECONDS)
        return payload

    logger.info("Fetching overview endpoints")
    assets_payload = attempt("/stablecoins?includePrices=true", "stablecoins")
    chains_now = attempt("/stablecoinchains", "stablecoinchains")
    all_chart = attempt("/stablecoincharts/all", "stablecoincharts_all")

    logger.info("Fetching %d chain histories", len(TRACKED_CHAINS))
    chain_charts: dict[str, list[dict[str, Any]]] = {}
    for chain in TRACKED_CHAINS:
        payload = attempt(f"/stablecoincharts/{chain}", f"stablecoincharts_{chain}")
        if isinstance(payload, list):
            chain_charts[chain] = payload

    # Each output is written only when everything it needs arrived intact. A
    # missing input leaves the previous file untouched, so the dashboard keeps
    # showing the last good data instead of going blank.
    if isinstance(all_chart, list) and chain_charts:
        write_json(out_dir / "chains.json", build_chains(all_chart, chain_charts),
                   source_name="DefiLlama", source_url=BASE_URL, cadence="daily")
        written.append("chains.json")
    else:
        logger.error("Skipping chains.json — keeping the previous snapshot")

    if isinstance(all_chart, list) and isinstance(chains_now, list) and isinstance(assets_payload, dict):
        assets = assets_payload.get("peggedAssets", [])
        write_json(out_dir / "summary.json", build_summary(all_chart, chains_now, assets),
                   source_name="DefiLlama", source_url=BASE_URL, cadence="daily")
        written.append("summary.json")
    else:
        logger.error("Skipping summary.json — keeping the previous snapshot")

    issuer_labels: list[str] = []
    if args.skip_issuers:
        logger.info("Skipping issuer history (--skip-issuers)")
    else:
        issuers = (
            resolve_top_issuers(assets_payload.get("peggedAssets", []))
            if isinstance(assets_payload, dict)
            else {}
        )
        issuer_labels = list(issuers.values())
        logger.info("Fetching %d issuer histories (large payloads): %s",
                    len(issuers), ", ".join(issuer_labels))
        issuer_totals, issuer_by_chain, issuer_errors = fetch_issuer_series(
            session, raw_dir, issuers
        )
        errors.extend(issuer_errors)

        if isinstance(all_chart, list) and issuer_totals:
            write_json(out_dir / "issuers.json", build_issuers(all_chart, issuer_totals),
                       source_name="DefiLlama", source_url=BASE_URL, cadence="daily")
            written.append("issuers.json")
        else:
            logger.error("Skipping issuers.json — keeping the previous snapshot")

        if isinstance(all_chart, list) and issuer_totals and chain_charts:
            matrix = build_matrix(all_chart, chain_charts, issuer_totals, issuer_by_chain)
            write_json(out_dir / "matrix.json", matrix, source_name="DefiLlama", source_url=BASE_URL, cadence="daily")
            written.append("matrix.json")
            if matrix["negative_cells_clamped"]:
                data_notes.append(
                    f"{matrix['negative_cells_clamped']} negative issuer/chain balance(s) "
                    "reported upstream were floored at zero for the stacked charts."
                )
        else:
            logger.error("Skipping matrix.json — keeping the previous snapshot")

    logger.info("Fetching monetary aggregates from FRED")
    reference, reference_errors = fetch_reference(session, raw_dir)
    errors.extend(reference_errors)
    if reference["aggregates"]:
        write_json(out_dir / "reference.json", reference,
                   source_name="FRED, St. Louis Fed",
                   source_url="https://fred.stlouisfed.org/",
                   cadence="monthly, with a multi-month publication lag")
        written.append("reference.json")
    else:
        logger.error("Skipping reference.json — keeping the previous snapshot")

    write_json(
        out_dir / "meta.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": BASE_URL,
            "source_name": "DefiLlama",
            "peg_type": PEG_TYPE,
            "tracked_chains": list(TRACKED_CHAINS),
            "filter_chains": list(FILTER_CHAINS),
            "tracked_issuers": issuer_labels,
            "data_notes": data_notes,
            "files_written": sorted(written),
            "errors": errors,
            "status": "ok" if not errors else "partial",
        },
    )

    if errors:
        logger.warning("Completed with %d error(s); stale files kept", len(errors))
        # Deliberately exit 0: a partial refresh is a normal outcome for a free
        # upstream API, the failure is recorded in meta.json, and failing the
        # job would block the deploy of data that is still perfectly good.
        return 0

    logger.info("Completed cleanly: %s", ", ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
