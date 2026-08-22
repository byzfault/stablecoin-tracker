#!/usr/bin/env python3
"""Join labelled venue flows to a curated venue-market map and build corridors.

This is the proxy, and it is worth being precise about what the join actually
claims. Dune gives us "X USD moved from an address labelled Bitso to an address
labelled Coins.ph". ``data/venue_markets.csv`` says Bitso's home market is
Mexico and Coins.ph's is the Philippines. Putting those together produces a
number filed under "Mexico to Philippines" — but the flow is a settlement
between two exchanges, not a remittance between two people, and neither venue
is confined to its home market.

So the output is built to make the caveats unavoidable rather than optional:

* Any flow touching a venue whose home market is GLOBAL — or a venue missing
  from the map entirely — is counted in ``unattributed`` and never assigned to a
  corridor. Unknown defaults to unattributed, so forgetting to add a venue
  understates corridors rather than inventing one.
* Flows between two venues in the same market are ``domestic``, not a corridor.
* Every corridor carries the ``confidence`` of its weakest venue mapping and the
  list of venue pairs it was built from, so a corridor resting on one shaky
  label can be seen to be doing so.
* ``attribution`` reports what share of all labelled volume survived the join.
  If that number is small — it will be — the dashboard says so out loud.

Inputs
------
``data/corridor_flows.json``    From fetch_dune.py.
``data/venue_markets.csv``      Curated by hand. venue,home_market,region,confidence
``data/remittance_costs.json``  Optional, from fetch_worldbank.py.
``data/knomad_matrix.json``     Optional, from fetch_worldbank.py.
``data/countries.json``         Optional, ISO3 -> display name.

Output
------
``data/corridors.json``

Usage
-----
    python scripts/build_corridors.py [--output-dir data] [--config config.json]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

#: Markets that are not markets. A venue mapped to GLOBAL serves everywhere, so
#: attributing its flow to a corridor would be inventing a geography.
GLOBAL_MARKET: Final = "GLOBAL"

#: Ranked worst-first, because a corridor inherits the weakest link.
CONFIDENCE_ORDER: Final[tuple[str, ...]] = ("low", "medium", "high")

#: Trend window. 90 days of data gives one clean 30-day comparison against the
#: 30 days before it, and leaves the oldest 30 days as context rather than
#: stretching the comparison across a window the query does not cover.
TREND_WINDOW_DAYS: Final = 30

logger = logging.getLogger("build_corridors")


def load_json(path: Path) -> Any:
    """Read a JSON file, returning None if it is missing or unreadable."""
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable %s (%s)", path.name, exc)
        return None


def load_venue_map(path: Path) -> dict[str, dict[str, str]]:
    """Read venue_markets.csv into a lookup keyed by lowercased venue name.

    Matching is case- and whitespace-insensitive because the CSV is hand-curated
    against labels from a community-maintained Dune table, and an invisible
    trailing space should not silently drop a venue into the unattributed bucket.
    """
    venues: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            name = (row.get("venue") or "").strip()
            if not name:
                continue
            confidence = (row.get("confidence") or "medium").strip().lower()
            if confidence not in CONFIDENCE_ORDER:
                logger.warning("Venue %s has confidence %r; treating as low", name, confidence)
                confidence = "low"
            venues[name.lower()] = {
                "venue": name,
                "home_market": (row.get("home_market") or GLOBAL_MARKET).strip().upper(),
                "region": (row.get("region") or GLOBAL_MARKET).strip().upper(),
                "confidence": confidence,
            }
    return venues


def weakest(*confidences: str) -> str:
    """The lowest confidence among the inputs."""
    return min(confidences, key=lambda c: CONFIDENCE_ORDER.index(c) if c in CONFIDENCE_ORDER else 0)


def build(
    flows: dict[str, Any],
    venue_map: dict[str, dict[str, str]],
    costs: dict[str, Any] | None,
    knomad: dict[str, Any] | None,
    countries: dict[str, Any] | None,
    cost_config: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate daily venue-pair rows into directional market corridors."""
    rows = flows.get("rows") or []
    days = sorted({row["day"] for row in rows})
    if not days:
        raise ValueError("corridor_flows.json contains no dated rows")

    latest = datetime.strptime(days[-1], "%Y-%m-%d").date()
    recent_cutoff = latest.toordinal() - TREND_WINDOW_DAYS
    previous_cutoff = recent_cutoff - TREND_WINDOW_DAYS

    corridors: dict[tuple[str, str], dict[str, Any]] = {}
    unattributed = {"usd_volume": 0.0, "transfer_count": 0, "venues": defaultdict(float)}
    domestic = {"usd_volume": 0.0, "transfer_count": 0}
    seen_venues: dict[str, float] = defaultdict(float)
    unmapped_venues: dict[str, float] = defaultdict(float)
    total_volume = 0.0

    for row in rows:
        usd = float(row["usd_volume"])
        count = int(row["transfer_count"])
        total_volume += usd
        day_ordinal = datetime.strptime(row["day"], "%Y-%m-%d").date().toordinal()
        bucket = (
            "recent" if day_ordinal > recent_cutoff
            else "previous" if day_ordinal > previous_cutoff
            else "older"
        )

        src = venue_map.get(row["from_venue"].lower())
        dst = venue_map.get(row["to_venue"].lower())
        for name in (row["from_venue"], row["to_venue"]):
            seen_venues[name] += usd
        for name, mapped in ((row["from_venue"], src), (row["to_venue"], dst)):
            if mapped is None:
                unmapped_venues[name] += usd

        # Credit the unattributed volume to the side that caused it — the GLOBAL
        # or unmapped venue — not to the regional venue on the other end. The
        # point of the list is "what is absorbing volume into this bucket", and
        # blaming Coins.ph for sending to Binance answers the wrong question.
        blockers = [
            name
            for name, mapped in ((row["from_venue"], src), (row["to_venue"], dst))
            if mapped is None or mapped["home_market"] == GLOBAL_MARKET
        ]
        if blockers:
            unattributed["usd_volume"] += usd
            unattributed["transfer_count"] += count
            for name in blockers:
                unattributed["venues"][name] += usd
            continue

        if src["home_market"] == dst["home_market"]:
            domestic["usd_volume"] += usd
            domestic["transfer_count"] += count
            continue

        key = (src["home_market"], dst["home_market"])
        corridor = corridors.setdefault(
            key,
            {
                "from_market": src["home_market"],
                "to_market": dst["home_market"],
                "from_region": src["region"],
                "to_region": dst["region"],
                "usd_volume": 0.0,
                "transfer_count": 0,
                "usd_volume_recent_30d": 0.0,
                "usd_volume_previous_30d": 0.0,
                "confidence": "high",
                "venue_pairs": defaultdict(float),
                "tokens": defaultdict(float),
            },
        )
        corridor["usd_volume"] += usd
        corridor["transfer_count"] += count
        corridor["tokens"][row["token"]] += usd
        corridor["venue_pairs"][f"{src['venue']} → {dst['venue']}"] += usd
        corridor["confidence"] = weakest(
            corridor["confidence"], src["confidence"], dst["confidence"]
        )
        if bucket == "recent":
            corridor["usd_volume_recent_30d"] += usd
        elif bucket == "previous":
            corridor["usd_volume_previous_30d"] += usd

    country_names = (countries or {}).get("names", {}) if isinstance(countries, dict) else {}
    cost_by_country = (costs or {}).get("inbound_cost_pct", {}) if isinstance(costs, dict) else {}
    knomad_pairs = (knomad or {}).get("pairs", {}) if isinstance(knomad, dict) else {}
    knomad_totals = (knomad or {}).get("received_total_usd", {}) if isinstance(knomad, dict) else {}

    output_corridors = []
    for corridor in corridors.values():
        recent = corridor["usd_volume_recent_30d"]
        previous = corridor["usd_volume_previous_30d"]
        # A trend from a zero base is a divide-by-zero dressed up as insight.
        trend = round((recent - previous) / previous * 100, 1) if previous > 0 else None

        to_market = corridor["to_market"]
        cost = cost_by_country.get(to_market)
        pair_key = f"{corridor['from_market']}-{to_market}"

        output_corridors.append(
            {
                "from_market": corridor["from_market"],
                "to_market": to_market,
                "from_name": country_names.get(corridor["from_market"], corridor["from_market"]),
                "to_name": country_names.get(to_market, to_market),
                "from_region": corridor["from_region"],
                "to_region": corridor["to_region"],
                "proxy_volume_usd": round(corridor["usd_volume"], 2),
                "transfer_count": corridor["transfer_count"],
                "proxy_volume_30d_usd": round(recent, 2),
                "trend_30d_pct": trend,
                "confidence": corridor["confidence"],
                "tokens": {k: round(v, 2) for k, v in sorted(corridor["tokens"].items())},
                "venue_pairs": [
                    {"pair": pair, "usd_volume": round(vol, 2)}
                    for pair, vol in sorted(
                        corridor["venue_pairs"].items(), key=lambda kv: -kv[1]
                    )
                ],
                "remittance_cost_pct": cost.get("value") if isinstance(cost, dict) else None,
                "remittance_cost_year": cost.get("year") if isinstance(cost, dict) else None,
                "onchain_cost_pct": onchain_cost_pct(cost_config),
                "traditional_annual_usd": knomad_pairs.get(pair_key),
                "traditional_received_total_usd": knomad_totals.get(to_market),
            }
        )

    output_corridors.sort(key=lambda c: -c["proxy_volume_usd"])
    attributed = sum(c["proxy_volume_usd"] for c in output_corridors)
    top_unattributed = sorted(unattributed["venues"].items(), key=lambda kv: -kv[1])[:10]

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flows_fetched_at": flows.get("fetched_at"),
        "data_as_of": flows.get("data_as_of") or flows.get("fetched_at"),
        "source_name": "Dune Analytics + curated venue map",
        "source_url": flows.get("source_url"),
        "update_cadence": "daily",
        "stale": bool(flows.get("stale")),
        "window": {"start": days[0], "end": days[-1], "days": len(days)},
        "trend_window_days": TREND_WINDOW_DAYS,
        "corridors": output_corridors,
        "attribution": {
            "total_labelled_usd": round(total_volume, 2),
            "corridor_usd": round(attributed, 2),
            "corridor_share_pct": round(attributed / total_volume * 100, 1) if total_volume else 0.0,
            "domestic_usd": round(domestic["usd_volume"], 2),
            "unattributed_usd": round(unattributed["usd_volume"], 2),
            "unattributed_share_pct": (
                round(unattributed["usd_volume"] / total_volume * 100, 1) if total_volume else 0.0
            ),
            "top_unattributed_venues": [
                {"venue": name, "usd_volume": round(vol, 2)} for name, vol in top_unattributed
            ],
        },
        # Reconciliation aids. The Dune labels move underneath this file, so the
        # two lists below are how the curated map is kept honest: venues seen in
        # the data but absent from the CSV, and CSV rows that never match.
        "venue_diagnostics": {
            "unmapped_venues": [
                {"venue": name, "usd_volume": round(vol, 2)}
                for name, vol in sorted(unmapped_venues.items(), key=lambda kv: -kv[1])[:25]
            ],
            "mapped_but_unseen": sorted(
                entry["venue"]
                for key, entry in venue_map.items()
                if entry["venue"] not in seen_venues
            ),
        },
        "cost_assumptions": cost_config,
    }


def onchain_cost_pct(cost_config: dict[str, Any]) -> float | None:
    """Approximate on-chain cost of sending the reference amount, as a percent.

    Deliberately a configured assumption rather than a measurement. It covers a
    network fee and a venue withdrawal fee and nothing else — not the spread on
    getting cash into the stablecoin, and not the spread on getting it out,
    which is where most of the real-world cost of this route actually sits.
    """
    amount = float(cost_config.get("send_amount_usd") or 0)
    if amount <= 0:
        return None
    total = float(cost_config.get("network_fee_usd") or 0) + float(
        cost_config.get("venue_withdrawal_fee_usd") or 0
    )
    return round(total / amount * 100, 2)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    logger.info("Wrote %s", path)


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
    flows = load_json(out_dir / "corridor_flows.json")
    if not isinstance(flows, dict) or not flows.get("rows"):
        logger.error(
            "No usable corridor_flows.json — run fetch_dune.py first. "
            "Keeping the previous corridors.json."
        )
        return 0

    venue_path = out_dir / "venue_markets.csv"
    try:
        venue_map = load_venue_map(venue_path)
    except (OSError, csv.Error) as exc:
        logger.error("Cannot read %s (%s) — keeping the previous snapshot", venue_path, exc)
        return 0
    if not venue_map:
        logger.error("%s has no venues — keeping the previous snapshot", venue_path)
        return 0
    logger.info("Loaded %d venue mappings", len(venue_map))

    config = load_json(args.config) or {}
    cost_config = (config.get("corridor") or {}).get("cost_assumptions", {})

    try:
        corridors = build(
            flows,
            venue_map,
            load_json(out_dir / "remittance_costs.json"),
            load_json(out_dir / "knomad_matrix.json"),
            load_json(out_dir / "countries.json"),
            cost_config,
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Could not build corridors (%s) — keeping the previous snapshot", exc)
        return 0

    write_json(out_dir / "corridors.json", corridors)
    attribution = corridors["attribution"]
    logger.info(
        "%d corridors from %.1f%% of labelled volume; %.1f%% unattributed",
        len(corridors["corridors"]),
        attribution["corridor_share_pct"],
        attribution["unattributed_share_pct"],
    )
    unmapped = corridors["venue_diagnostics"]["unmapped_venues"]
    if unmapped:
        logger.warning(
            "%d venue(s) in the data are not in venue_markets.csv; largest: %s",
            len(unmapped),
            ", ".join(entry["venue"] for entry in unmapped[:5]),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
