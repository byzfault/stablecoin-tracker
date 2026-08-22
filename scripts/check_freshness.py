#!/usr/bin/env python3
"""Fail the workflow when a snapshot has stopped refreshing.

The fetchers deliberately exit 0 when an upstream endpoint fails: they keep the
last good snapshot, record what went wrong, and let the deploy proceed, because
a stale chart is worth far more than a broken one. That is the right behaviour
for the site and the wrong behaviour for the operator, who would never hear
about it.

This script is the other half of that bargain. It runs last, after the data has
been committed and published, and turns a recorded failure into a red build and
an email. Nothing it does affects what the site serves.

A snapshot is judged on ``fetched_at`` — when the pipeline last wrote it — not
on the age of the data inside it. FRED publishes M2 with a three-month lag and
that is not a pipeline failure; FRED not being fetched for six weeks is.

Usage
-----
    python scripts/check_freshness.py [--data-dir data] [--config config.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

#: file -> (maximum age in hours, whether its absence is itself a failure)
#:
#: The daily files get 36 hours rather than 24: a run that starts at 06:00 and
#: an upstream that is briefly down should not page anyone, but two missed days
#: should.
POLICY: Final[dict[str, tuple[float, bool]]] = {
    "summary.json": (36, True),
    "chains.json": (36, True),
    "issuers.json": (36, True),
    "matrix.json": (36, True),
    "reference.json": (36, True),
    # Monthly sources. Generous windows — these series change annually, and the
    # monthly job exists to notice a URL that has rotted, not to chase updates.
    "countries.json": (24 * 45, False),
    "remittance_costs.json": (24 * 45, False),
    "knomad_matrix.json": (24 * 45, False),
}

#: Checked only when a Dune query id is configured. Without one the corridor
#: module is switched off, and complaining about a file that was never meant to
#: exist would train everyone to ignore this check.
CORRIDOR_POLICY: Final[dict[str, tuple[float, bool]]] = {
    "corridor_flows.json": (36, True),
    "corridors.json": (36, True),
}


def load(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def age_hours(stamp: Any, now: datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", type=Path)
    parser.add_argument("--config", default="config.json", type=Path)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    failures: list[str] = []
    warnings: list[str] = []

    policy = dict(POLICY)
    config = load(args.config) or {}
    if (config.get("corridor") or {}).get("dune_query_id"):
        policy.update(CORRIDOR_POLICY)
    else:
        warnings.append(
            "corridor.dune_query_id is not set — the corridor module is switched off "
            "and its snapshots are not checked."
        )

    for name, (max_hours, required) in sorted(policy.items()):
        path = args.data_dir / name
        payload = load(path)
        if payload is None:
            (failures if required else warnings).append(f"{name}: missing or unreadable")
            continue

        age = age_hours(payload.get("fetched_at"), now)
        if age is None:
            failures.append(f"{name}: no usable fetched_at stamp")
        elif age > max_hours:
            failures.append(
                f"{name}: last refreshed {age:.1f}h ago, limit is {max_hours:.0f}h"
            )
        else:
            print(f"ok      {name}: {age:.1f}h old (limit {max_hours:.0f}h)")

    # The pipeline's own account of what went wrong, surfaced here so the email
    # names the endpoint rather than only the symptom.
    meta = load(args.data_dir / "meta.json")
    if isinstance(meta, dict) and meta.get("errors"):
        for error in meta["errors"]:
            failures.append(f"meta.json: {error}")

    flows = load(args.data_dir / "corridor_flows.json")
    if isinstance(flows, dict):
        if flows.get("stale"):
            failures.append("corridor_flows.json: serving results older than the freshness target")
        for note in flows.get("notes") or []:
            warnings.append(f"corridor_flows.json: {note}")

    for warning in warnings:
        print(f"warn    {warning}")

    if failures:
        print("\nFreshness check failed:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nThe site still deploys with its last good snapshots; the panels above "
            "carry a stale badge. This job is red so the failure is not silent."
        )
        return 1

    print("\nAll snapshots within their freshness windows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
