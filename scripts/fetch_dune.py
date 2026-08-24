#!/usr/bin/env python3
"""Fetch labelled venue-to-venue stablecoin flows from a saved Dune query.

The SQL itself lives in ``queries/corridor_flows.sql`` and is maintained by hand
in the Dune UI — this script never sends SQL. It reads the results of a saved
query whose id is set in ``config.json`` under ``corridor.dune_query_id``.

Credit discipline
-----------------
Dune bills for execution and for reading results, so this script is deliberately
reluctant to spend:

1. It asks for the latest cached results first, one row at a time, to read the
   execution timestamp without paying for the full result set.
2. If those results are younger than ``results_max_age_hours`` it downloads them
   and stops. No execution is triggered.
3. If they are stale it will trigger a new execution at most once per
   ``execute_min_interval_hours``, tracked in ``data/dune_state.json`` — which is
   committed, so the rate limit survives across CI runs on ephemeral runners.
4. If it is stale *and* rate limited, it serves the stale results and says so.
   A stale number that is labelled stale beats no number at all.

Every run appends what it spent to the credit log in ``data/dune_state.json``.

Outputs
-------
``data/corridor_flows.json``      Normalised rows plus provenance and freshness.
``data/dune_state.json``          Execution rate-limit state and credit log.
``data/raw/dune_corridor.json.gz`` The raw API response.

Failure behaviour
-----------------
Any failure leaves the previous ``corridor_flows.json`` in place and records the
reason in the output metadata written by the caller. This script exits 0 even on
failure, matching ``fetch_data.py``: a stale panel still deploys, and the
workflow's freshness gate is what turns a recorded error into a red build.

Usage
-----
    DUNE_API_KEY=... python scripts/fetch_dune.py [--output-dir data]
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import requests

API_BASE: Final = "https://api.dune.com/api/v1"

#: Rows per results page. Dune's maximum for a single page is larger, but paging
#: in chunks keeps memory flat and makes a partial failure cheap to retry.
PAGE_SIZE: Final = 5_000

#: Seconds between execution status polls. Dune executions of a 90-day transfer
#: scan typically finish in one to five minutes.
POLL_INTERVAL_SECONDS: Final = 15

REQUEST_TIMEOUT_SECONDS: Final = 90
MAX_ATTEMPTS: Final = 3

#: Dune prices result reads per 1,000 datapoints (rows x columns). This is used
#: only to log an estimate — the authoritative number is on the Dune billing
#: page, and the log line exists so that a query that quietly grows tenfold is
#: visible in the workflow output before it is visible on an invoice.
DATAPOINTS_PER_CREDIT: Final = 1_000

logger = logging.getLogger("fetch_dune")


class DuneError(RuntimeError):
    """The Dune API could not be reached, or returned something unusable."""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def request(
    session: requests.Session,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    not_found_ok: bool = False,
) -> dict[str, Any]:
    """Call the Dune API with retries, returning the decoded JSON body.

    ``not_found_ok`` turns a 404 into an empty dict rather than an error. Only
    the cached-results probe sets it, and only because on that endpoint a 404 is
    a fact about the query rather than a fault: it means no execution has ever
    produced a result set. Retrying that is pointless and reporting it as a
    failure would be wrong.
    """
    url = f"{API_BASE}{path}"
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.request(
                method,
                url,
                params=params,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            # 402 means out of credits and 429 means too many requests. Neither
            # is fixed by trying again in three seconds, so fail loudly instead
            # of burning the retry budget.
            if response.status_code in (401, 402, 403):
                raise DuneError(
                    f"{response.status_code} from {path} — check DUNE_API_KEY and "
                    f"the plan's credit balance: {response.text[:200]}"
                )
            if response.status_code == 429:
                raise DuneError(f"429 rate limited by Dune on {path}")
            if response.status_code == 404 and not_found_ok:
                return {}
            response.raise_for_status()
            return response.json()
        except DuneError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS:
                backoff = 2 ** attempt
                logger.warning("%s %s failed (%s); retrying in %ds",
                               method, path, exc, backoff)
                time.sleep(backoff)

    raise DuneError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last_error}")


# ---------------------------------------------------------------------------
# State: execution rate limiting and the credit log
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    """Read the committed execution state, tolerating a missing or bad file."""
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict):
            state.setdefault("credit_log", [])
            return state
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable %s (%s) — starting fresh", path.name, exc)
    return {"last_execution_at": None, "last_execution_id": None, "credit_log": []}


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp from the API, returning None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # Dune returns fractional seconds at nanosecond precision on some endpoints,
    # which fromisoformat rejects on Python 3.9. Trim to microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())[:6]
        offset = tail[len(digits):] if len(tail) > len(digits) else ""
        offset = offset.lstrip("0123456789")
        text = f"{head}.{digits or '0'}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def hours_since(moment: datetime | None, now: datetime) -> float | None:
    """Age of ``moment`` in hours, or None if there is no timestamp."""
    if moment is None:
        return None
    return (now - moment).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def latest_result_probe(session: requests.Session, query_id: int) -> dict[str, Any]:
    """Fetch one row of the latest cached results, to read its timestamps cheaply.

    Returns an empty dict when the query has never been executed. That is the
    cold-start case — a query id that was only just saved has no result set
    behind it — and the caller reads the resulting absent timestamp as "no
    cached execution", which is what triggers the first run. Treating the 404 as
    an error instead would leave a new query permanently unable to bootstrap
    itself: no results, so no execution; no execution, so no results.
    """
    return request(
        session,
        "GET",
        f"/query/{query_id}/results",
        params={"limit": 1},
        not_found_ok=True,
    )


def download_results(
    session: requests.Session,
    query_id: int,
    max_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Page through the latest cached results.

    Returns the rows, the last page's metadata, and whether ``max_rows`` cut the
    download short. Truncation is returned rather than swallowed so the caller
    can record it — a silently capped result set reads as a complete one.
    """
    rows: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    offset = 0
    truncated = False

    while True:
        page = request(
            session,
            "GET",
            f"/query/{query_id}/results",
            params={"limit": PAGE_SIZE, "offset": offset},
        )
        result = page.get("result") or {}
        metadata = result.get("metadata") or metadata
        batch = result.get("rows") or []
        rows.extend(batch)
        offset += len(batch)

        if len(rows) >= max_rows:
            truncated = len(rows) > max_rows or bool(page.get("next_offset"))
            rows = rows[:max_rows]
            break
        if not page.get("next_offset") or not batch:
            break

    return rows, metadata, truncated


def execute_query(
    session: requests.Session,
    query_id: int,
    performance: str,
    timeout_seconds: int,
) -> str:
    """Trigger an execution and wait for it to finish. Returns the execution id."""
    started = request(
        session,
        "POST",
        f"/query/{query_id}/execute",
        payload={"performance": performance},
    )
    execution_id = started.get("execution_id")
    if not execution_id:
        raise DuneError(f"Execution request returned no execution_id: {started}")

    logger.info("Triggered execution %s (performance=%s)", execution_id, performance)
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        status = request(session, "GET", f"/execution/{execution_id}/status")
        state = status.get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            logger.info("Execution %s completed", execution_id)
            return execution_id
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
            raise DuneError(f"Execution {execution_id} ended in state {state}")
        logger.info("Execution %s state=%s", execution_id, state or "unknown")

    raise DuneError(
        f"Execution {execution_id} did not complete within {timeout_seconds}s"
    )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_rows(raw_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce Dune's rows into the shape the corridor builder expects.

    Rows that are missing a venue, a token or a day are dropped rather than
    guessed at: a flow that cannot be attributed to a pair of venues has no
    meaning in this module.
    """
    required = ("from_venue", "to_venue", "token", "day")
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    dropped = 0

    for raw in raw_rows:
        if any(not raw.get(field) for field in required):
            dropped += 1
            continue
        day = str(raw["day"])[:10]
        try:
            usd = float(raw.get("usd_volume") or 0.0)
            count = int(raw.get("transfer_count") or 0)
        except (TypeError, ValueError):
            dropped += 1
            continue
        rows.append(
            {
                "from_venue": str(raw["from_venue"]).strip(),
                "to_venue": str(raw["to_venue"]).strip(),
                "token": str(raw["token"]).strip().upper(),
                "day": day,
                "usd_volume": round(usd, 2),
                "transfer_count": count,
            }
        )

    if dropped:
        notes.append(f"{dropped} row(s) dropped for missing venue, token or day.")
    return rows, notes


def write_json(path: Path, payload: Any) -> None:
    """Write JSON deterministically, so a no-change run produces no diff."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    logger.info("Wrote %s", path)


def save_raw(raw_dir: Path, name: str, payload: Any) -> None:
    """Keep the upstream response next to the derived file, gzipped."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{name}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="data", type=Path)
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument(
        "--force-execute",
        action="store_true",
        help="Ignore the once-per-day execution limit. Spends credits.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir: Path = args.output_dir
    raw_dir = out_dir / "raw"
    state_path = out_dir / "dune_state.json"
    output_path = out_dir / "corridor_flows.json"
    now = datetime.now(timezone.utc)

    try:
        with args.config.open(encoding="utf-8") as handle:
            config = json.load(handle).get("corridor", {})
    except (OSError, ValueError) as exc:
        logger.error("Cannot read %s (%s) — keeping the previous snapshot", args.config, exc)
        return 0

    query_id = config.get("dune_query_id")
    if not query_id:
        logger.error(
            "corridor.dune_query_id is not set in %s. Save queries/corridor_flows.sql "
            "in the Dune UI and put its id there. Keeping the previous snapshot.",
            args.config,
        )
        return 0

    api_key = os.environ.get("DUNE_API_KEY", "").strip()
    if not api_key:
        logger.error("DUNE_API_KEY is not set — keeping the previous snapshot")
        return 0

    state = load_state(state_path)
    session = requests.Session()
    session.headers.update({"X-Dune-API-Key": api_key, "Accept": "application/json"})

    max_age_hours = float(config.get("results_max_age_hours", 24))
    min_execute_gap = float(config.get("execute_min_interval_hours", 24))
    max_rows = int(config.get("max_rows", 200_000))

    notes: list[str] = []
    executed = False

    try:
        probe = latest_result_probe(session, query_id)
        cached_ended = parse_timestamp(
            probe.get("execution_ended_at") or (probe.get("result") or {}).get("execution_ended_at")
        )
        cached_age = hours_since(cached_ended, now)

        if cached_age is None:
            logger.info("No cached execution found for query %s", query_id)
        else:
            logger.info("Cached results are %.1fh old (threshold %.0fh)", cached_age, max_age_hours)

        if cached_age is not None and cached_age <= max_age_hours:
            logger.info("Cached results are fresh enough — not spending an execution")
        else:
            last_execute = parse_timestamp(state.get("last_execution_at"))
            gap = hours_since(last_execute, now)
            if args.force_execute:
                logger.info("--force-execute given; ignoring the once-per-day limit")
            elif gap is not None and gap < min_execute_gap:
                notes.append(
                    f"Results are stale but the last execution was {gap:.1f}h ago; "
                    f"waiting for the {min_execute_gap:.0f}h execution interval."
                )
                logger.warning(notes[-1])
            if args.force_execute or gap is None or gap >= min_execute_gap:
                execution_id = execute_query(
                    session,
                    query_id,
                    str(config.get("performance", "medium")),
                    int(config.get("execution_timeout_seconds", 900)),
                )
                executed = True
                state["last_execution_at"] = now.isoformat(timespec="seconds")
                state["last_execution_id"] = execution_id

        raw_rows, metadata, truncated = download_results(session, query_id, max_rows)
    except DuneError as exc:
        logger.error("%s — keeping the previous snapshot", exc)
        state.setdefault("credit_log", []).append(
            {"at": now.isoformat(timespec="seconds"), "error": str(exc), "executed": executed}
        )
        state["credit_log"] = state["credit_log"][-30:]
        write_json(state_path, state)
        return 0

    if not raw_rows:
        logger.error("Query %s returned no rows — keeping the previous snapshot", query_id)
        return 0

    rows, normalise_notes = normalise_rows(raw_rows)
    notes.extend(normalise_notes)
    if truncated:
        notes.append(
            f"Result set truncated at max_rows={max_rows}; the tail of the "
            "ordering was not downloaded."
        )
        logger.warning(notes[-1])

    save_raw(raw_dir, "dune_corridor", {"metadata": metadata, "row_count": len(raw_rows)})

    datapoints = int(metadata.get("datapoint_count") or len(rows) * 6)
    credits = round(datapoints / DATAPOINTS_PER_CREDIT, 2)
    logger.info(
        "Downloaded %d rows (%d datapoints, ~%.2f result credits%s)",
        len(rows), datapoints, credits, "; 1 execution spent" if executed else "",
    )

    # Re-probe for the timestamps of the execution the rows actually came from,
    # so fetched_at and data_as_of are never quietly the same number.
    execution_ended = parse_timestamp(
        (metadata.get("execution_ended_at") if isinstance(metadata, dict) else None)
    ) or parse_timestamp(state.get("last_execution_at"))
    age_hours = hours_since(execution_ended, now)
    stale = bool(age_hours is not None and age_hours > max_age_hours)
    if stale:
        notes.append(
            f"Serving results from an execution {age_hours:.1f}h old, "
            f"older than the {max_age_hours:.0f}h freshness target."
        )

    days = sorted({row["day"] for row in rows})
    write_json(
        output_path,
        {
            "fetched_at": now.isoformat(timespec="seconds"),
            "data_as_of": execution_ended.isoformat(timespec="seconds") if execution_ended else None,
            "source_name": "Dune Analytics",
            "source_url": f"https://dune.com/queries/{query_id}",
            "update_cadence": "daily",
            "query_id": query_id,
            "executed_this_run": executed,
            "stale": stale,
            "notes": notes,
            "row_count": len(rows),
            "date_range": {"start": days[0], "end": days[-1]} if days else None,
            "rows": rows,
        },
    )

    state.setdefault("credit_log", []).append(
        {
            "at": now.isoformat(timespec="seconds"),
            "executed": executed,
            "rows": len(rows),
            "datapoints": datapoints,
            "estimated_result_credits": credits,
        }
    )
    state["credit_log"] = state["credit_log"][-30:]
    write_json(state_path, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
