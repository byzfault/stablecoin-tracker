#!/usr/bin/env python3
"""Fetch World Bank remittance cost and volume data for the corridor context.

Three outputs, all keyed on ISO-3166 alpha-3 so they join to
``data/venue_markets.csv`` without any country-name matching:

``data/countries.json``         ISO3 -> display name and region.
``data/remittance_costs.json``  Average cost of sending remittances, by country.
``data/knomad_matrix.json``     Annual traditional remittance volumes.

Where the numbers come from, and what they are not
--------------------------------------------------
Remittance Prices Worldwide publishes true corridor-level costs quarterly, but
its site rejects scripted requests, so it cannot be fetched from CI. What is
fetched instead is the World Bank's WDI indicators SI.RMT.COST.IB.ZS and
SI.RMT.COST.OB.ZS, which are compiled *from* RPW — the same underlying survey,
aggregated to a country average across all of that country's corridors rather
than reported per corridor. A corridor panel showing this number is showing
"the average cost of sending money to this country from anywhere", not "the
cost of this specific corridor", and it has to say so.

If you want the real corridor-level series, export it from RPW by hand and drop
it in as ``data/rpw_corridors.csv`` with columns
``from_market,to_market,cost_pct,period``; the corridor builder prefers it over
the country average when it is present.

The KNOMAD bilateral matrix has the same problem in a different shape: it is
published as a spreadsheet on a site that has since been folded into the World
Bank CMS, and its URL changes annually. Rather than hard-code a URL that will
rot, this script reads it from ``config.json`` and also accepts a hand-dropped
``data/knomad_matrix_source.csv``. When neither is available the corridor
context line falls back to total remittances received by the destination
country (BX.TRF.PWKR.CD.DT), which is always fetchable.

Failure behaviour
-----------------
These series change annually. A failed fetch keeps the last good copy in place
and records the failure, because a file that has not changed in eleven months
is not a problem — a file overwritten with an empty one is.

Usage
-----
    python scripts/fetch_worldbank.py [--output-dir data] [--config config.json]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import requests

API_BASE: Final = "https://api.worldbank.org/v2"

#: WDI series fetched, and the output key each lands under.
INDICATORS: Final[dict[str, dict[str, str]]] = {
    "SI.RMT.COST.IB.ZS": {
        "key": "inbound_cost_pct",
        "label": "Average cost of sending remittances to this country (%)",
    },
    "SI.RMT.COST.OB.ZS": {
        "key": "outbound_cost_pct",
        "label": "Average cost of sending remittances from this country (%)",
    },
    "BX.TRF.PWKR.CD.DT": {
        "key": "received_total_usd",
        "label": "Personal remittances received (current US$)",
    },
}

#: The World Bank pages at 50 by default; countries and a full indicator series
#: both fit comfortably inside one page at this size.
PAGE_SIZE: Final = 20_000

REQUEST_TIMEOUT_SECONDS: Final = 60
MAX_ATTEMPTS: Final = 3
REQUEST_DELAY_SECONDS: Final = 0.5

#: Country-name spellings in the KNOMAD sheet that do not match the World Bank's
#: own country list. Extend this rather than loosening the matching — a fuzzy
#: match that silently pairs the wrong country is worse than an unmatched row.
NAME_ALIASES: Final[dict[str, str]] = {
    "russia": "RUS",
    "south korea": "KOR",
    "korea, rep.": "KOR",
    "korea, republic of": "KOR",
    "egypt": "EGY",
    "iran": "IRN",
    "syria": "SYR",
    "venezuela": "VEN",
    "vietnam": "VNM",
    "turkey": "TUR",
    "turkiye": "TUR",
    "united states": "USA",
    "united kingdom": "GBR",
    "hong kong sar, china": "HKG",
    "macao sar, china": "MAC",
    "taiwan": "TWN",
    "china, macao sar": "MAC",
    "china, hong kong sar": "HKG",
    "ivory coast": "CIV",
    "cote d'ivoire": "CIV",
    "congo, dem. rep.": "COD",
    "democratic republic of the congo": "COD",
    "laos": "LAO",
    "slovakia": "SVK",
    "kyrgyzstan": "KGZ",
    "moldova": "MDA",
    "tanzania": "TZA",
    "bolivia": "BOL",
    "brunei": "BRN",
    "cape verde": "CPV",
    "gambia": "GMB",
    "yemen": "YEM",
}

logger = logging.getLogger("fetch_worldbank")


class FetchError(RuntimeError):
    """An upstream endpoint could not be retrieved after retries."""


def get(session: requests.Session, url: str, params: dict[str, Any] | None = None) -> requests.Response:
    """GET with retries and a small politeness delay."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            time.sleep(REQUEST_DELAY_SECONDS)
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                backoff = 2 ** attempt
                logger.warning("GET %s failed (%s); retrying in %ds", url, exc, backoff)
                time.sleep(backoff)
    raise FetchError(f"GET {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")


def api_rows(session: requests.Session, path: str) -> list[dict[str, Any]]:
    """Call the Indicators API and return its data rows.

    The API answers with ``[metadata, rows]``; a bad request answers with
    ``[metadata, null]`` and an HTTP 200, so the shape has to be checked rather
    than trusted.
    """
    response = get(session, f"{API_BASE}{path}", params={"format": "json", "per_page": PAGE_SIZE})
    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchError(f"{path} returned non-JSON: {exc}") from exc
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[1], list):
        message = payload[0].get("message") if isinstance(payload, list) and payload else payload
        raise FetchError(f"{path} returned no rows: {message}")
    return payload[1]


def fetch_countries(session: requests.Session) -> dict[str, Any]:
    """ISO3 -> name and region, for display labels and sanity checks.

    Aggregates (the World Bank returns "Sub-Saharan Africa" and "World" in the
    same list as actual countries) are filtered out by their region id.
    """
    names: dict[str, str] = {}
    regions: dict[str, str] = {}
    for row in api_rows(session, "/country"):
        iso3 = (row.get("id") or "").strip().upper()
        region = (row.get("region") or {}).get("id", "")
        if len(iso3) != 3 or region in ("", "NA"):
            continue
        names[iso3] = row.get("name", iso3)
        regions[iso3] = region
    if not names:
        raise FetchError("Country list came back empty")
    return {"names": names, "regions": regions}


def latest_values(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce an indicator series to the most recent non-null value per country.

    These series are sparse and lag by two or three years, so "latest" has to
    mean "latest that exists" rather than "this year". The year travels with the
    value so the dashboard can print it — a 2023 cost figure presented as
    current would be its own small dishonesty.
    """
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        iso3 = (row.get("countryiso3code") or "").strip().upper()
        value = row.get("value")
        year = row.get("date")
        if len(iso3) != 3 or value is None or not year:
            continue
        current = best.get(iso3)
        if current is None or int(year) > int(current["year"]):
            best[iso3] = {"value": round(float(value), 3), "year": int(year)}
    return best


def parse_knomad_csv(text: str, name_to_iso3: dict[str, str]) -> tuple[dict[str, float], list[str]]:
    """Parse a KNOMAD bilateral matrix exported to CSV.

    Layout: the first column is the receiving country, the header row is the
    sending countries, and cells are US$ millions. Rows and columns that cannot
    be resolved to an ISO3 code are counted and reported rather than dropped
    silently, because an unmatched country name means a missing corridor.
    """
    reader = csv.reader(io.StringIO(text))
    header: list[str] = []
    for candidate in reader:
        # Skip the title and blank rows spreadsheets tend to carry above the grid.
        if len(candidate) > 5 and sum(1 for cell in candidate[1:] if cell.strip()) > 5:
            header = candidate
            break
    if not header:
        raise FetchError("Could not find a header row in the KNOMAD CSV")

    senders = [name_to_iso3.get(cell.strip().lower()) for cell in header]
    pairs: dict[str, float] = {}
    unmatched: set[str] = {cell.strip() for cell, iso in zip(header[1:], senders[1:])
                           if cell.strip() and iso is None}

    for row in reader:
        if not row or not row[0].strip():
            continue
        receiver = name_to_iso3.get(row[0].strip().lower())
        if receiver is None:
            unmatched.add(row[0].strip())
            continue
        for index, cell in enumerate(row[1:], start=1):
            sender = senders[index] if index < len(senders) else None
            if sender is None or sender == receiver:
                continue
            text_value = cell.strip().replace(",", "")
            if not text_value or text_value in ("-", "..", "n/a"):
                continue
            try:
                millions = float(text_value)
            except ValueError:
                continue
            if millions <= 0:
                continue
            pairs[f"{sender}-{receiver}"] = round(millions * 1_000_000, 2)

    return pairs, sorted(unmatched)[:25]


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, ValueError):
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    logger.info("Wrote %s", path)


def save_raw(raw_dir: Path, name: str, payload: Any) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(raw_dir / f"{name}.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data", type=Path)
    parser.add_argument("--config", default="config.json", type=Path)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir: Path = args.output_dir
    raw_dir = out_dir / "raw"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config = (load_json(args.config) or {}).get("worldbank", {})
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    errors: list[str] = []

    # --- countries -------------------------------------------------------
    countries: dict[str, Any] | None = None
    try:
        countries = fetch_countries(session)
        write_json(
            out_dir / "countries.json",
            {
                "fetched_at": now,
                "source_name": "World Bank",
                "source_url": f"{API_BASE}/country",
                "update_cadence": "rarely",
                **countries,
            },
        )
        logger.info("Resolved %d countries", len(countries["names"]))
    except FetchError as exc:
        errors.append(f"countries: {exc}")
        logger.error("%s — keeping the previous countries.json", exc)
        countries = load_json(out_dir / "countries.json")

    # --- indicators ------------------------------------------------------
    known_iso3 = set(((countries or {}).get("names") or {}).keys())
    series: dict[str, dict[str, Any]] = {}
    for indicator, spec in INDICATORS.items():
        try:
            rows = api_rows(session, f"/country/all/indicator/{indicator}")
            values = latest_values(rows)
            if not values:
                raise FetchError("no non-null observations")
            # The Indicators API returns aggregates (WLD, EAP, LMY) alongside
            # actual countries. Keeping them would put "World" in a corridor
            # lookup, so they are dropped against the country list.
            if known_iso3:
                values = {iso3: entry for iso3, entry in values.items() if iso3 in known_iso3}
            series[spec["key"]] = values
            save_raw(raw_dir, f"worldbank_{indicator}", rows[:5000])
            years = sorted({entry["year"] for entry in values.values()})
            logger.info(
                "%s: %d countries, years %s-%s", indicator, len(values), years[0], years[-1]
            )
        except FetchError as exc:
            errors.append(f"{indicator}: {exc}")
            logger.error("%s failed (%s)", indicator, exc)

    if "inbound_cost_pct" in series or "outbound_cost_pct" in series:
        previous = load_json(out_dir / "remittance_costs.json") or {}
        write_json(
            out_dir / "remittance_costs.json",
            {
                "fetched_at": now,
                "data_as_of": now,
                "source_name": "World Bank WDI (compiled from Remittance Prices Worldwide)",
                "source_url": "https://remittanceprices.worldbank.org/",
                "update_cadence": "quarterly survey, published annually",
                "note": (
                    "Country averages across all of a country's corridors, not "
                    "corridor-specific costs. RPW publishes true corridor costs but "
                    "blocks automated download; drop data/rpw_corridors.csv in to "
                    "override these with real corridor figures."
                ),
                "indicators": {
                    spec["key"]: {"id": indicator, "label": spec["label"]}
                    for indicator, spec in INDICATORS.items()
                    if spec["key"] in ("inbound_cost_pct", "outbound_cost_pct")
                },
                "inbound_cost_pct": series.get(
                    "inbound_cost_pct", previous.get("inbound_cost_pct", {})
                ),
                "outbound_cost_pct": series.get(
                    "outbound_cost_pct", previous.get("outbound_cost_pct", {})
                ),
            },
        )
    else:
        logger.error("No cost indicators retrieved — keeping the previous remittance_costs.json")

    # --- KNOMAD bilateral matrix ----------------------------------------
    name_to_iso3: dict[str, str] = {
        name.lower(): iso3 for iso3, name in ((countries or {}).get("names") or {}).items()
    }
    name_to_iso3.update(NAME_ALIASES)

    pairs: dict[str, float] = {}
    unmatched: list[str] = []
    matrix_note = ""
    matrix_url = config.get("knomad_matrix_url")
    local_csv = out_dir / "knomad_matrix_source.csv"

    try:
        if matrix_url:
            logger.info("Downloading the KNOMAD matrix from %s", matrix_url)
            response = get(session, matrix_url)
            body = response.content
            if body[:2] == b"PK" or "spreadsheet" in response.headers.get("content-type", ""):
                raise FetchError(
                    "the configured URL served a spreadsheet, not CSV — export the "
                    "sheet and commit it as data/knomad_matrix_source.csv instead"
                )
            pairs, unmatched = parse_knomad_csv(response.text, name_to_iso3)
            matrix_note = f"Parsed from {matrix_url}"
        elif local_csv.exists():
            logger.info("Reading the KNOMAD matrix from %s", local_csv.name)
            pairs, unmatched = parse_knomad_csv(
                local_csv.read_text(encoding="utf-8-sig"), name_to_iso3
            )
            matrix_note = f"Parsed from {local_csv.name}"
        else:
            matrix_note = (
                "No bilateral matrix configured. Corridor context falls back to total "
                "remittances received by the destination country."
            )
            logger.warning("%s", matrix_note)
    except (FetchError, OSError, csv.Error) as exc:
        errors.append(f"knomad: {exc}")
        matrix_note = f"Bilateral matrix unavailable: {exc}"
        logger.error("KNOMAD matrix failed (%s)", exc)

    if unmatched:
        logger.warning("%d country name(s) unmatched in the matrix: %s",
                       len(unmatched), ", ".join(unmatched[:8]))

    received = series.get("received_total_usd")
    previous_matrix = load_json(out_dir / "knomad_matrix.json") or {}
    if pairs or received:
        write_json(
            out_dir / "knomad_matrix.json",
            {
                "fetched_at": now,
                "data_as_of": now,
                "source_name": "KNOMAD / World Bank",
                "source_url": "https://www.knomad.org/data/remittances",
                "update_cadence": "annual",
                "note": matrix_note,
                "unmatched_country_names": unmatched,
                "pairs": pairs or previous_matrix.get("pairs", {}),
                "received_total_usd": received or previous_matrix.get("received_total_usd", {}),
            },
        )
        logger.info("Bilateral pairs: %d; countries with a received total: %d",
                    len(pairs), len(received or {}))
    else:
        logger.error("Nothing to write for knomad_matrix.json — keeping the previous copy")

    if errors:
        logger.warning("Completed with %d error(s); previous copies kept", len(errors))
    else:
        logger.info("Completed cleanly")

    # Exit 0 either way. These series update annually; a failed monthly refresh
    # is not a broken dashboard, and the freshness gate decides what is worth
    # failing the build over.
    return 0


if __name__ == "__main__":
    sys.exit(main())
