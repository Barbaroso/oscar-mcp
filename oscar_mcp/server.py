"""MCP server exposing OSCAR CPAP therapy data to LLM clients."""

from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import analysis, knowledge
from .database import (
    JOURNAL_MACHINE_TYPE,
    OscarDatabase,
    QueryTimeout,
    ReadOnlyViolation,
    parse_date,
    to_iso,
)

# Every tool here only reads: the database is opened with SQLite's mode=ro and
# run_sql accepts nothing but a single SELECT. Saying so in the tool metadata
# lets a client grant the whole server a read-only trust policy instead of
# interrupting for approval on each call, which is the difference between a
# usable session and twenty prompts.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

INSTRUCTIONS = """\
Query a local OSCAR database of CPAP/BiPAP therapy data (ResMed, Philips
Respironics and other supported devices).

Before interpreting results, read the model resources. They define what the
numbers mean and how they are derived, and they are cheap to read:
* oscar://model/metrics -- exact AHI and RDI formulas and which events count.
* oscar://model/interpretation -- the caveats that turn a correct number into a
  wrong conclusion, including why large leak makes a night's AHI unreliable.
* oscar://model/entities -- table relationships and join keys, required before
  writing SQL with run_sql.
* oscar://model/glossary -- therapy vocabulary as OSCAR itself defines it.

Guidance:
* A "night" is identified by an ISO date. Sessions starting after midnight are
  attributed to the previous night, matching OSCAR's own reporting.
* Start with get_statistics for an overview, then get_daily_summaries for a
  night-by-night table, then get_daily_detail or get_respiratory_events to drill
  into a specific night.
* AHI is events/hour, pressure is cmH2O and leak is L/min. Reference bands are
  returned with the data so numbers can be read in context.
* Access is strictly read-only, and personal identifiers are withheld unless the
  operator enabled them.
"""

server = MCPServer(
    name="oscar",
    title="OSCAR CPAP Data",
    version="0.1.0",
    instructions=INSTRUCTIONS,
)

_db: OscarDatabase | None = None

# Leak spans live in the same table as apneas but are not respiratory events.
LEAK_CHANNELS = frozenset({"LeakSpan", "LargeLeak"})

# Fields worth returning per night, in reading order. Bookkeeping columns such as
# row ids and cache hashes are dropped to keep responses compact.
NIGHT_FIELDS: tuple[str, ...] = (
    "date",
    "source",
    "session_count",
    "total_hours",
    "mask_on_hours",
    "ahi",
    "rdi",
    "severity",
    "obstructive_count",
    "hypopnea_count",
    "clear_airway_count",
    "unclassified_count",
    "rera_count",
    "pressure_avg",
    "pressure_min",
    "pressure_max",
    "pressure_95th",
    "leak_total_avg",
    "leak_total_95th",
    "leak_total_max",
    "spo2_avg",
    "spo2_min",
    "pulse_avg",
    "is_compliant",
)

OXIMETRY_FIELDS = ("spo2_avg", "spo2_min", "pulse_avg", "pulse_min", "pulse_max")


def _clean_night(row: dict) -> dict:
    """Trim a night record to the reportable fields and round noisy floats."""
    has_oximetry = bool(row.get("has_oximetry")) or any(
        (row.get(f) or 0) > 0 for f in OXIMETRY_FIELDS
    )
    cleaned: dict[str, Any] = {}
    for field in NIGHT_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if field in OXIMETRY_FIELDS and not has_oximetry:
            value = None
        if isinstance(value, float):
            value = round(value, 2)
        cleaned[field] = value
    if row.get("is_compliant") is not None:
        cleaned["is_compliant"] = bool(row["is_compliant"])
    return cleaned


def get_db() -> OscarDatabase:
    """Return the shared database handle, opening it on first use."""
    global _db
    if _db is None:
        # OSCAR_DATA_DIR is deliberately not read here. Discovery owns it, and
        # passing it on as an argument would misreport where the database came
        # from -- the first thing worth knowing when the data looks wrong.
        _db = OscarDatabase(
            include_pii=os.environ.get("OSCAR_MCP_INCLUDE_PII", "").lower()
            in {"1", "true", "yes"},
        )
    return _db


def set_db(db: OscarDatabase | None) -> None:
    """Inject a database handle (used by tests)."""
    global _db
    _db = db


def _daily_rows(
    db: OscarDatabase,
    profile_id: int,
    start: dt.date | None,
    end: dt.date | None,
) -> list[dict]:
    """Return one row per night, filling gaps OSCAR has not summarised yet.

    OSCAR writes ``daily_summaries`` lazily, so the most recent night is often
    missing. Those nights are recomputed from their sessions and flagged via the
    ``source`` field so callers can tell the two apart.
    """
    stored = {row["date"]: row for row in db.daily_summaries(profile_id, start=start, end=end)}
    sessions = db.sessions(profile_id, start=start, end=end)

    by_night: dict[str, list[dict]] = {}
    for session in sessions:
        by_night.setdefault(session["date"], []).append(session)

    rows: list[dict] = []
    for date in sorted(set(stored) | set(by_night)):
        night_sessions = by_night.get(date, [])
        if date in stored:
            row = dict(stored[date])
            row["source"] = "oscar"
        else:
            hours = sum(s["hours"] for s in night_sessions)
            weighted = [
                (s["ahi"], s["hours"])
                for s in night_sessions
                if s.get("ahi") is not None and s["hours"]
            ]
            total_weight = sum(w for _, w in weighted)
            row = {
                "date": date,
                "session_count": len(night_sessions),
                "total_hours": round(hours, 3),
                "ahi": round(sum(a * w for a, w in weighted) / total_weight, 3)
                if total_weight
                else None,
                "obstructive_count": sum(int(s.get("obstructive_count") or 0) for s in night_sessions),
                "hypopnea_count": sum(int(s.get("hypopnea_count") or 0) for s in night_sessions),
                "clear_airway_count": sum(int(s.get("clear_airway_count") or 0) for s in night_sessions),
                "unclassified_count": sum(int(s.get("unclassified_count") or 0) for s in night_sessions),
                "rera_count": sum(int(s.get("rera_count") or 0) for s in night_sessions),
                "pressure_95th": max(
                    (s["pressure_95th"] for s in night_sessions if s.get("pressure_95th") is not None),
                    default=None,
                ),
                "leak_total_95th": max(
                    (s["leak_total_95th"] for s in night_sessions if s.get("leak_total_95th") is not None),
                    default=None,
                ),
                "is_compliant": int(hours >= analysis.COMPLIANCE_HOURS),
                "source": "computed",
            }
        row["severity"] = analysis.severity_band(row.get("ahi"))
        rows.append(_clean_night(row))
    return rows


def _resolve(profile: str | None) -> tuple[OscarDatabase, int]:
    db = get_db()
    return db, db.resolve_profile_id(profile)


# ----------------------------------------------------------------------
# discovery / reference
# ----------------------------------------------------------------------
@server.tool(
    description="List OSCAR profiles with their data coverage. Call this first "
    "when you do not know which profile or date range is available.",
    annotations=READ_ONLY,
)
def list_profiles() -> dict:
    db = get_db()
    profiles = []
    for profile in db.profiles():
        pid = int(profile["id"])
        nights = _daily_rows(db, pid, None, None)
        profiles.append(
            {
                **profile,
                "nights_with_data": len(nights),
                "first_night": nights[0]["date"] if nights else None,
                "last_night": nights[-1]["date"] if nights else None,
                "devices": [
                    f"{m.get('brand')} {m.get('model')}".strip()
                    for m in db.machines(pid)
                    if m.get("machine_type") != JOURNAL_MACHINE_TYPE
                ],
            }
        )
    return {"database": db.location.as_dict(), "profiles": profiles}


@server.tool(
    description="Get the therapy devices recorded for a profile, including model and last import time.",
    annotations=READ_ONLY,
)
def get_device_info(profile: str | None = None) -> dict:
    db, pid = _resolve(profile)
    devices = []
    for machine in db.machines(pid):
        machine["is_therapy_device"] = machine.get("machine_type") != JOURNAL_MACHINE_TYPE
        session_count = db.query_one(
            "SELECT COUNT(*) AS n FROM sessions WHERE machine_id = ?", (machine["id"],)
        )
        machine["session_count"] = session_count["n"] if session_count else 0
        devices.append(machine)
    return {"profile_id": pid, "devices": devices}


@server.tool(
    description="List the data channels OSCAR recorded, mapping numeric channel ids to "
    "human readable names such as AHI, Pressure, Leak Rate or Flow Limitation.",
    annotations=READ_ONLY,
)
def list_channels(only_used: bool = True, profile: str | None = None) -> dict:
    db, pid = _resolve(profile)
    if only_used:
        rows = db.query(
            """SELECT c.channel_id, c.channel_code, c.fullname, c.label, COUNT(sc.id) AS sessions
               FROM channels c
               JOIN session_channels sc ON sc.channel_id = c.channel_id AND sc.profile_id = c.profile_id
               WHERE c.profile_id = ?
               GROUP BY c.channel_id ORDER BY sessions DESC, c.channel_code""",
            (pid,),
        )
    else:
        rows = db.query(
            """SELECT channel_id, channel_code, fullname, label, description
               FROM channels WHERE profile_id = ? ORDER BY channel_code""",
            (pid,),
        )
    return {"profile_id": pid, "channels": rows, "count": len(rows)}


@server.tool(
    description="Describe the OSCAR database: tables, columns, foreign keys, and the join "
    "and decoding rules that make a query correct. Read this before writing run_sql.",
    annotations=READ_ONLY,
)
def describe_database() -> dict:
    db = get_db()
    return {
        "database": db.location.as_dict(),
        "read_only": True,
        "pii_included": db.include_pii,
        "tables": db.schema(),
        "query_rules": knowledge.sql_rules(),
    }


# ----------------------------------------------------------------------
# night level data
# ----------------------------------------------------------------------
@server.tool(
    description="Return a night-by-night table of therapy results (usage hours, AHI, "
    "event counts, pressure and leak). Dates are ISO format; omit them for all data.",
    annotations=READ_ONLY,
)
def get_daily_summaries(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 120,
    profile: str | None = None,
) -> dict:
    db, pid = _resolve(profile)
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    rows = _daily_rows(db, pid, start, end)

    limit = max(1, min(int(limit), 2000))
    truncated = len(rows) > limit
    if truncated:
        rows = rows[-limit:]  # most recent nights are the most relevant

    return {
        "profile_id": pid,
        "nights": rows,
        "count": len(rows),
        "truncated": truncated,
        "units": analysis.UNITS,
        "disclaimer": analysis.DISCLAIMER,
    }


@server.tool(
    description="Aggregate statistics for a period: usage and compliance, AHI distribution "
    "and severity band, event totals, pressure and leak summaries, plus simple trends.",
    annotations=READ_ONLY,
)
def get_statistics(
    start_date: str | None = None,
    end_date: str | None = None,
    profile: str | None = None,
) -> dict:
    db, pid = _resolve(profile)
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")
    rows = _daily_rows(db, pid, start, end)

    if rows:
        start = start or dt.date.fromisoformat(rows[0]["date"])
        end = end or dt.date.fromisoformat(rows[-1]["date"])

    report = analysis.summarise_nights(rows, start, end)
    report["profile_id"] = pid
    return report


@server.tool(
    description="Everything recorded for one night: each session, machine settings in "
    "effect, event counts and per-channel statistics. Date must be ISO format.",
    annotations=READ_ONLY,
)
def get_daily_detail(date: str, profile: str | None = None) -> dict:
    db, pid = _resolve(profile)
    night = parse_date(date, field="date")
    if night is None:
        raise ValueError("date is required, for example 2024-01-31.")

    sessions = db.sessions(pid, start=night, end=night)
    summary = next(iter(_daily_rows(db, pid, night, night)), None)
    channels = db.channel_map(pid)

    session_ids = [s["id"] for s in sessions]
    settings: dict[str, dict] = {}
    channel_stats: list[dict] = []
    if session_ids:
        options = db.option_map()
        placeholders = ",".join("?" * len(session_ids))
        for row in db.query(
            f"""SELECT channel_id, value FROM session_settings
                WHERE session_id IN ({placeholders}) GROUP BY channel_id, value""",
            session_ids,
        ):
            described = db.describe_setting(int(row["channel_id"]), row["value"], channels, options)
            key = described["channel"]
            if key in settings:
                # More than one distinct value means the setting changed mid-night.
                existing = settings[key]
                existing.setdefault("values", [existing["value"]])
                existing["values"].append(described["value"])
            else:
                settings[key] = described

        for row in db.query(
            f"""SELECT channel_id, SUM(count) AS n, AVG(avg) AS avg, MIN(min) AS min,
                       MAX(max) AS max, AVG(p95) AS p95
                FROM session_channels WHERE session_id IN ({placeholders})
                GROUP BY channel_id""",
            session_ids,
        ):
            info = channels.get(int(row["channel_id"]), {})
            channel_stats.append(
                {
                    "channel": info.get("channel_code"),
                    "name": info.get("fullname"),
                    "avg": round(row["avg"], 3) if row["avg"] is not None else None,
                    "min": row["min"],
                    "max": row["max"],
                    "p95": round(row["p95"], 3) if row["p95"] is not None else None,
                }
            )

    return {
        "profile_id": pid,
        "date": night.isoformat(),
        "summary": summary,
        "sessions": [
            {
                "session_db_id": s["id"],
                "start": s["start"],
                "end": s["end"],
                "hours": s["hours"],
                "ahi": round(s["ahi"], 2) if s.get("ahi") is not None else None,
                "device": f"{s.get('brand')} {s.get('model')}".strip(),
            }
            for s in sessions
        ],
        "settings": dict(sorted(settings.items())),
        "channel_stats": sorted(channel_stats, key=lambda c: c["channel"] or ""),
        "units": analysis.UNITS,
        "disclaimer": analysis.DISCLAIMER,
    }


@server.tool(
    description="Individual respiratory events (apneas, hypopneas, RERAs) for one night, "
    "with counts by type and an hourly histogram showing when they clustered. "
    "Large leak spans are reported separately from respiratory events.",
    annotations=READ_ONLY,
)
def get_respiratory_events(
    date: str,
    include_events: bool = False,
    profile: str | None = None,
) -> dict:
    db, pid = _resolve(profile)
    night = parse_date(date, field="date")
    if night is None:
        raise ValueError("date is required, for example 2024-01-31.")

    sessions = db.sessions(pid, start=night, end=night)
    session_ids = [s["id"] for s in sessions]
    if not session_ids:
        return {"profile_id": pid, "date": night.isoformat(), "count": 0, "note": "No sessions."}

    placeholders = ",".join("?" * len(session_ids))
    rows = db.query(
        f"""SELECT channel_id, start_time, duration, desaturation, severity
            FROM respiratory_events WHERE session_id IN ({placeholders})
            ORDER BY start_time""",
        session_ids,
    )

    channels = db.channel_map(pid)
    respiratory: list[dict] = []
    leaks: list[dict] = []
    for row in rows:
        info = channels.get(int(row["channel_id"]), {})
        row["type"] = info.get("channel_code") or f"channel_{row['channel_id']}"
        row["name"] = info.get("fullname")
        row["start"] = to_iso(row["start_time"])
        (leaks if row["type"] in LEAK_CHANNELS else respiratory).append(row)

    by_type: dict[str, int] = {}
    durations: dict[str, list[float]] = {}
    for event in respiratory:
        by_type[event["type"]] = by_type.get(event["type"], 0) + 1
        durations.setdefault(event["type"], []).append(float(event["duration"] or 0))

    # OSCAR records some flag-style events (RERA, Hypopnea on ResMed) without a
    # span, so a zero mean is absence of duration data rather than a real value.
    mean_durations = {}
    for name, values in sorted(durations.items()):
        mean = sum(values) / len(values)
        mean_durations[name] = round(mean, 1) if mean > 0 else None

    hours = sum(s["hours"] for s in sessions) or None
    result = {
        "profile_id": pid,
        "date": night.isoformat(),
        "count": len(respiratory),
        "counts_by_type": dict(sorted(by_type.items())),
        "mean_duration_seconds": mean_durations,
        "events_per_hour": round(len(respiratory) / hours, 2) if hours else None,
        "hourly_distribution": analysis.hourly_histogram(respiratory),
        "large_leak_spans": {
            "count": len(leaks),
            "total_seconds": sum(int(e["duration"] or 0) for e in leaks),
        },
        "disclaimer": analysis.DISCLAIMER,
    }
    if include_events:
        result["events"] = [
            {k: e.get(k) for k in ("type", "name", "start", "duration", "desaturation")}
            for e in respiratory
        ]
    return result


@server.tool(
    description="Machine settings over time (pressure limits, EPR, mode, ramp, humidity). "
    "Set changes_only to report just the nights where a setting changed.",
    annotations=READ_ONLY,
)
def get_therapy_settings(
    start_date: str | None = None,
    end_date: str | None = None,
    changes_only: bool = False,
    profile: str | None = None,
) -> dict:
    db, pid = _resolve(profile)
    start = parse_date(start_date, field="start_date")
    end = parse_date(end_date, field="end_date")

    sessions = db.sessions(pid, start=start, end=end)
    if not sessions:
        return {"profile_id": pid, "settings_by_night": [], "changes": []}

    channels = db.channel_map(pid)
    options = db.option_map()
    by_session = {s["id"]: s for s in sessions}
    placeholders = ",".join("?" * len(by_session))
    rows = db.query(
        f"""SELECT session_id, channel_id, value FROM session_settings
            WHERE session_id IN ({placeholders})""",
        list(by_session),
    )

    nights: dict[str, dict[str, Any]] = {}
    for row in rows:
        night = by_session[row["session_id"]]["date"]
        described = db.describe_setting(int(row["channel_id"]), row["value"], channels, options)
        nights.setdefault(night, {})[described["channel"]] = (
            described["label"] if described["label"] is not None else described["value"]
        )

    ordered = [{"date": d, "settings": nights[d]} for d in sorted(nights)]

    changes = []
    previous: dict[str, Any] | None = None
    for entry in ordered:
        if previous is not None:
            diff = {
                key: {"from": previous.get(key), "to": value}
                for key, value in entry["settings"].items()
                if previous.get(key) != value
            }
            if diff:
                changes.append({"date": entry["date"], "changed": diff})
        previous = entry["settings"]

    return {
        "profile_id": pid,
        "settings_by_night": [] if changes_only else ordered,
        "changes": changes,
        "note": "Values are raw device settings; pressures are cmH2O.",
    }


@server.tool(
    description="Per-channel statistics for a single session, identified by its session_db_id.",
    annotations=READ_ONLY,
)
def get_session_details(session_db_id: int, profile: str | None = None) -> dict:
    db, pid = _resolve(profile)
    session = db.query_one(
        """SELECT s.id, s.start_time, s.end_time, s.duration, m.brand, m.model
           FROM sessions s JOIN machines m ON m.id = s.machine_id
           WHERE s.id = ? AND m.profile_id = ?""",
        (int(session_db_id), pid),
    )
    if not session:
        raise LookupError(f"No session with id {session_db_id} for this profile.")

    channels = db.channel_map(pid)
    stats = []
    for row in db.query(
        """SELECT channel_id, count, avg, wavg, min, max, median, p90, p95, cph
           FROM session_channels WHERE session_id = ?""",
        (int(session_db_id),),
    ):
        info = channels.get(int(row["channel_id"]), {})
        entry = {"channel": info.get("channel_code"), "name": info.get("fullname")}
        entry.update(
            {
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in row.items()
                if key != "channel_id"
            }
        )
        stats.append(entry)

    return {
        "profile_id": pid,
        "session": {
            "session_db_id": session["id"],
            "start": to_iso(session["start_time"]),
            "end": to_iso(session["end_time"]),
            "hours": round((session["duration"] or 0) / 3_600_000.0, 3),
            "device": f"{session.get('brand')} {session.get('model')}".strip(),
        },
        "channels": sorted(stats, key=lambda c: c["channel"] or ""),
        "units": analysis.UNITS,
    }


@server.tool(
    description="Run a read-only SELECT against the OSCAR database for analysis the other "
    "tools do not cover. Only SELECT/WITH statements are permitted, and a query is cancelled "
    "if it exceeds its time budget. Call describe_database first: this schema reuses column "
    "names across tables, so a wrong join key returns zero rows instead of an error. For mask, "
    "mode and other categorical settings prefer get_therapy_settings, because raw values are "
    "device-specific codes whose meaning depends on the channel.",
    annotations=READ_ONLY,
)
def run_sql(sql: str, limit: int = 200) -> dict:
    db = get_db()
    try:
        return db.run_select(sql, limit=limit)
    except ReadOnlyViolation as exc:
        raise ValueError(f"Rejected: {exc}") from exc
    except QueryTimeout as exc:
        raise ValueError(f"Cancelled: {exc}") from exc


# ---------------------------------------------------------------------------
# Resources: what the data means
#
# Tools return values; these return the domain model needed to read those
# values correctly. Clients can attach them as context without spending a tool
# call, and they stay constant across queries.
# ---------------------------------------------------------------------------


def _json(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


@server.resource(
    "oscar://model/entities",
    name="Entity and relationship model",
    description="How OSCAR's tables map onto real concepts and how they join. "
    "Read before writing SQL with run_sql.",
    mime_type="application/json",
)
def entities_resource() -> str:
    return _json(knowledge.entity_model())


@server.resource(
    "oscar://model/metrics",
    name="Metric definitions",
    description="Exact formulas and provenance for AHI, RDI, therapy hours, "
    "leak and compliance.",
    mime_type="application/json",
)
def metrics_resource() -> str:
    return _json(knowledge.metric_model())


@server.resource(
    "oscar://model/glossary",
    name="Therapy glossary",
    description="Apnea, hypopnea, clear airway, RERA, CSR, leak and other terms "
    "as OSCAR itself defines them.",
    mime_type="application/json",
)
def glossary_resource() -> str:
    return _json(knowledge.glossary())


@server.resource(
    "oscar://model/interpretation",
    name="Interpretation caveats",
    description="The traps that turn a correct number into a wrong conclusion: "
    "leak invalidating AHI, clear-airway events, cross-brand comparison, "
    "single-night noise.",
    mime_type="application/json",
)
def interpretation_resource() -> str:
    return _json(knowledge.interpretation_guide())


@server.resource(
    "oscar://model",
    name="Complete data model",
    description="Entities, metrics, glossary and caveats in one document.",
    mime_type="application/json",
)
def model_resource() -> str:
    return _json(knowledge.overview())


# ---------------------------------------------------------------------------
# Prompts: how to run a review
#
# These encode the order of questions that produces a sound reading, so the
# analysis does not depend on the user knowing which tool to ask for first.
# ---------------------------------------------------------------------------


@server.prompt(
    name="review_therapy",
    title="Review recent therapy",
    description="Structured review of the last N nights, with leak checked before AHI.",
)
def review_therapy_prompt(nights: int = 30) -> str:
    return f"""\
Review the last {nights} nights of my CPAP therapy.

Read oscar://model/interpretation and oscar://model/metrics first, and apply
them throughout.

Work in this order:
1. get_statistics for the period, to establish usage and the overall AHI.
2. Check leak before discussing AHI. Nights above the large-leak threshold have
   unreliable event detection, so say so rather than comparing them as equals.
3. get_daily_summaries for the night-by-night pattern, and report the trend
   direction rather than reacting to any single night.
4. Break down event types. Note the balance between obstructive and clear
   airway events, and whether that balance is shifting.
5. Check whether settings or the device changed during the period, since that
   can explain a step change in the numbers.

Give me a short plain-language summary, then the evidence. Flag anything worth
raising with my clinician, and do not recommend setting changes.
"""


@server.prompt(
    name="investigate_leak",
    title="Investigate mask leak",
    description="Find which nights leaked, how badly, and what changed around them.",
)
def investigate_leak_prompt(nights: int = 30) -> str:
    return f"""\
Investigate mask leak over the last {nights} nights.

1. get_leak_analysis for the period.
2. Identify the worst nights and how far above the large-leak threshold they went.
3. For those nights, compare pressure and mask setting against the clean nights,
   and check get_settings_history for a mask or device change that lines up.
4. Say explicitly whether AHI on the leaking nights can be trusted.

Report what the data shows and what it cannot show. Leak causes such as mask
fit, wear and mouth leak are worth naming as possibilities, but do not present
a cause as established when the data cannot distinguish between them. If leak
persists across many nights, say that it is worth raising with the equipment
provider or clinician, since it is compromising the therapy itself.
"""


@server.prompt(
    name="compare_periods",
    title="Compare two periods",
    description="Test whether something actually changed between two date ranges.",
)
def compare_periods_prompt(
    baseline_start: str,
    baseline_end: str,
    recent_start: str,
    recent_end: str,
) -> str:
    return f"""\
Compare {baseline_start}..{baseline_end} against {recent_start}..{recent_end}.

Call get_statistics for each period separately, then compare AHI, event mix,
usage hours, pressure and leak.

Before attributing any difference to a therapy change:
- Confirm the periods are long enough that the difference is not night-to-night
  noise, and say so if they are not.
- Check get_settings_history for a settings or device change, which would make
  the two periods non-comparable rather than improved or worsened.
- Check whether leak differed, since that alone can move AHI.

State plainly whether the data supports a real change, or whether it is
inconclusive. Inconclusive is an acceptable answer.
"""


@server.prompt(
    name="prepare_for_appointment",
    title="Prepare for a clinician appointment",
    description="A factual summary to bring to an appointment, with questions to ask.",
)
def appointment_prompt(nights: int = 90) -> str:
    return f"""\
Prepare a summary of the last {nights} nights for a clinician appointment.

Include usage and compliance, average and trend of AHI, the breakdown of event
types, leak quality, and pressure levels. Note any device or settings changes
and when they happened.

Keep it factual and compact, state the date range and the number of nights, and
name the device. Mark any night whose data is unreliable rather than silently
averaging it in.

Finish with the questions this data raises that only a clinician can answer.
Ask questions; do not answer them, and do not suggest settings.
"""


def main() -> None:
    """Entry point for ``python -m oscar_mcp``."""
    server.run()


if __name__ == "__main__":
    main()
