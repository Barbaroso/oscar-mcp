"""Read-only access layer for the OSCAR SQLite database."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .discovery import OscarLocation, discover

# OSCAR groups sessions into therapy "nights". A session that starts before this
# hour counts towards the previous calendar day.
DEFAULT_DAY_SPLIT_HOUR = 12

# Wall-clock ceiling for a caller-supplied query. sqlite3's own ``timeout``
# argument only covers waiting for a lock, so a query that is merely expensive
# runs until it finishes -- an accidental cross join has no natural end. The
# progress handler below is the only hook that can interrupt work in progress.
DEFAULT_SQL_TIMEOUT = 10.0

# How many SQLite VM instructions between deadline checks. Small enough that the
# deadline is honoured promptly, large enough that the check costs nothing.
_PROGRESS_INTERVAL = 10_000

# Only single read-only SELECT/WITH statements are accepted by run_sql.
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|vacuum|pragma|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)

# Columns that identify the person rather than the therapy.
PII_COLUMNS = frozenset(
    {
        "first_name",
        "last_name",
        "dob",
        "address",
        "phone",
        "email",
        "password_hash",
        "serial_number",
    }
)

# OSCAR registers its own "Journal" pseudo-device alongside real machines.
JOURNAL_MACHINE_TYPE = 4

# Tables that hold nothing but identity data. Column filtering alone is not
# enough for these, since an expression could rename a column past the filter.
PII_TABLES = frozenset({"user_info", "doctor_info"})

# The SQL that reproduces OSCAR's night attribution. Sessions starting before
# noon belong to the previous night, so shifting back twelve hours before
# taking the date gives the same answer OSCAR stores in daily_summaries.
NIGHT_SQL = "date((sessions.start_time/1000)-43200,'unixepoch','localtime')"

# Query shapes that return a wrong answer *without* raising an error. Each one
# has actually been hit in practice; a query that fails loudly needs no warning,
# but one that silently inverts a result does.
_SQL_TRAPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\.session_id\s*=\s*\w+\.session_id\b", re.IGNORECASE),
        "Joining on sessions.session_id returns zero rows without error. "
        "sessions.session_id is OSCAR's own identifier; every foreign key "
        "references sessions.id instead.",
    ),
    (
        re.compile(
            r"\.channel_id\s*=\s*\w+\.id\b|\w+\.id\s*=\s*\w+\.channel_id\b",
            re.IGNORECASE,
        ),
        "channels.id is a row number, not the channel identifier. Join on "
        "channels.channel_id, and join channel_options on channel_id too.",
    ),
    (
        re.compile(r"\bsession_settings\b", re.IGNORECASE),
        "session_settings.value holds device-specific codes whose meaning "
        "depends on the channel: 1 is 'Nasal' on MaskType but 'Full Face' on "
        "RMS9_Mask. Decode via channel_options (option_key is the code, "
        "option_value is the label), or use get_therapy_settings, which does "
        "it for you.",
    ),
    (
        re.compile(r"\bevent_type\b", re.IGNORECASE),
        "respiratory_events.event_type only distinguishes a flag (0) from a "
        "span (1). The kind of event is given by channel_id.",
    ),
)

_DATE_ON_START = re.compile(r"date\s*\(\s*[^)]*start_time", re.IGNORECASE)


def sql_warnings(sql: str) -> list[str]:
    """Flag query shapes known to produce silently wrong results."""
    found = [message for pattern, message in _SQL_TRAPS if pattern.search(sql)]
    if _DATE_ON_START.search(sql) and "43200" not in sql:
        found.append(
            "Taking the date of start_time splits nights at midnight, but OSCAR "
            f"splits them at noon, so a 02:00 session lands on the wrong day. Use {NIGHT_SQL}."
        )
    return found



class ReadOnlyViolation(ValueError):
    """Raised when a caller supplies SQL that could modify the database."""


class QueryTimeout(RuntimeError):
    """Raised when a query exceeds its wall-clock budget and is interrupted."""


def sql_timeout_default() -> float:
    """Return the configured query budget in seconds."""
    raw = os.environ.get("OSCAR_MCP_SQL_TIMEOUT", "")
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_SQL_TIMEOUT
    return seconds if seconds > 0 else DEFAULT_SQL_TIMEOUT


def to_datetime(epoch_ms: int | None) -> dt.datetime | None:
    """Convert OSCAR's millisecond epoch timestamps to local datetimes."""
    if epoch_ms is None:
        return None
    return dt.datetime.fromtimestamp(epoch_ms / 1000.0)


def to_iso(epoch_ms: int | None) -> str | None:
    value = to_datetime(epoch_ms)
    return value.isoformat(timespec="seconds") if value else None


def therapy_date(epoch_ms: int, split_hour: int = DEFAULT_DAY_SPLIT_HOUR) -> dt.date:
    """Return the therapy night a session belongs to."""
    started = to_datetime(epoch_ms)
    assert started is not None
    if started.hour < split_hour:
        return started.date() - dt.timedelta(days=1)
    return started.date()


class OscarDatabase:
    """Thread-safe, strictly read-only wrapper around ``oscar.db``."""

    def __init__(
        self,
        location: OscarLocation | None = None,
        *,
        data_dir: str | Path | None = None,
        include_pii: bool = False,
        day_split_hour: int = DEFAULT_DAY_SPLIT_HOUR,
    ) -> None:
        self.location = location or discover(data_dir)
        self.include_pii = include_pii
        self.day_split_hour = day_split_hour
        self._local = threading.local()

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        """Return this thread's read-only connection, opening it on demand."""
        con = getattr(self._local, "con", None)
        if con is None:
            uri = f"file:{self.location.db_path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10.0)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout = 5000")
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    @contextlib.contextmanager
    def _timebox(self, seconds: float) -> Iterator[None]:
        """Interrupt any query still running after ``seconds``.

        SQLite has no statement timeout. The progress handler is called every
        few thousand VM instructions and aborts the statement when it returns
        non-zero, which is what makes an expensive query interruptible at all --
        row limits cannot help, because producing the first row of a runaway
        join already requires the whole scan.
        """
        con = self.connection
        deadline = time.monotonic() + seconds
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_INTERVAL)
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise QueryTimeout(
                    f"Query exceeded the {seconds:g}s budget and was cancelled. "
                    "This usually means an unintended cross join: a missing or wrong join "
                    "condition makes SQLite scan every combination of rows before it can "
                    "return even one. Check that every table in FROM is joined on a key, "
                    "and see describe_database for which keys actually match."
                ) from exc
            raise
        finally:
            con.set_progress_handler(None, 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        cur = self.connection.execute(sql, params)
        return [self._scrub(dict(row)) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def _scrub(self, row: dict) -> dict:
        """Drop personally identifying columns unless explicitly allowed."""
        if self.include_pii:
            return row
        return {k: v for k, v in row.items() if k not in PII_COLUMNS}

    # ------------------------------------------------------------------
    # reference data
    # ------------------------------------------------------------------
    def profiles(self) -> list[dict]:
        return self.query(
            "SELECT id, username, data_folder, status, created_at FROM profiles ORDER BY id"
        )

    def resolve_profile_id(self, profile: str | int | None = None) -> int:
        """Resolve a profile name or id, defaulting to the only/first profile."""
        rows = self.profiles()
        if not rows:
            raise LookupError("No profiles found in the OSCAR database.")

        if profile is None:
            return int(rows[0]["id"])

        if isinstance(profile, int) or str(profile).isdigit():
            wanted = int(profile)
            for row in rows:
                if int(row["id"]) == wanted:
                    return wanted
            raise LookupError(f"No profile with id {wanted}.")

        for row in rows:
            if str(row["username"]).lower() == str(profile).lower():
                return int(row["id"])
        names = ", ".join(str(r["username"]) for r in rows)
        raise LookupError(f"No profile named {profile!r}. Available: {names}")

    def channel_map(self, profile_id: int) -> dict[int, dict]:
        rows = self.query(
            """SELECT channel_id, channel_code, fullname, label, description, type
               FROM channels WHERE profile_id = ?""",
            (profile_id,),
        )
        return {int(r["channel_id"]): r for r in rows}

    def machines(self, profile_id: int) -> list[dict]:
        return self.query(
            """SELECT id, machine_id, loader_name, brand, model, series, machine_type,
                      serial_number, model_number, last_imported
               FROM machines WHERE profile_id = ? ORDER BY id""",
            (profile_id,),
        )

    def option_map(self) -> dict[int, dict[int, str]]:
        """Map channel ids to their enum labels, e.g. PAPMode 2 -> "APAP (Variable)".

        OSCAR stores these labels in the database itself, so settings can be
        decoded without hard-coding device-specific enums.
        """
        options: dict[int, dict[int, str]] = {}
        for row in self.query("SELECT channel_id, option_key, option_value FROM channel_options"):
            options.setdefault(int(row["channel_id"]), {})[int(row["option_key"])] = row[
                "option_value"
            ]
        return options

    def describe_setting(
        self,
        channel_id: int,
        value: Any,
        channels: dict[int, dict],
        options: dict[int, dict[int, str]],
    ) -> dict:
        """Render one raw setting value with its channel name and decoded label."""
        info = channels.get(channel_id, {})
        labels = options.get(channel_id, {})
        label = None
        if value is not None and float(value).is_integer():
            label = labels.get(int(value))
        return {
            "channel": info.get("channel_code") or str(channel_id),
            "name": info.get("fullname"),
            "value": value,
            "label": label,
        }

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def sessions(
        self,
        profile_id: int,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
        therapy_only: bool = True,
    ) -> list[dict]:
        """Return sessions annotated with the therapy night they belong to."""
        sql = """
            SELECT s.id, s.session_id, s.start_time, s.end_time, s.duration, s.enabled,
                   s.summary_only, s.events_loaded,
                   m.loader_name, m.brand, m.model, m.machine_type,
                   ss.ahi, ss.rdi, ss.hours_used,
                   ss.obstructive_count, ss.hypopnea_count, ss.clear_airway_count,
                   ss.unclassified_count, ss.rera_count,
                   ss.pressure_avg, ss.pressure_max, ss.pressure_95th,
                   ss.leak_total_avg, ss.leak_total_95th, ss.leak_total_max,
                   ss.spo2_avg, ss.spo2_min, ss.pulse_avg
            FROM sessions s
            JOIN machines m ON m.id = s.machine_id
            LEFT JOIN session_summaries ss ON ss.session_id = s.id
            WHERE m.profile_id = ?
            ORDER BY s.start_time
        """
        rows = self.query(sql, (profile_id,))

        result = []
        for row in rows:
            if therapy_only and row.get("machine_type") == JOURNAL_MACHINE_TYPE:
                continue
            night = therapy_date(row["start_time"], self.day_split_hour)
            if start and night < start:
                continue
            if end and night > end:
                continue
            row["date"] = night.isoformat()
            row["start"] = to_iso(row["start_time"])
            row["end"] = to_iso(row["end_time"])
            row["hours"] = round((row["duration"] or 0) / 3_600_000.0, 3)
            result.append(row)
        return result

    def daily_summaries(
        self,
        profile_id: int,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> list[dict]:
        clauses = ["profile_id = ?"]
        params: list[Any] = [profile_id]
        if start:
            clauses.append("date >= ?")
            params.append(start.isoformat())
        if end:
            clauses.append("date <= ?")
            params.append(end.isoformat())
        return self.query(
            f"SELECT * FROM daily_summaries WHERE {' AND '.join(clauses)} ORDER BY date",
            params,
        )

    # ------------------------------------------------------------------
    # arbitrary read-only SQL
    # ------------------------------------------------------------------
    def run_select(self, sql: str, limit: int = 200, timeout: float | None = None) -> dict:
        """Execute a single read-only SELECT, enforcing a row limit and a time budget."""
        cleaned = sql.strip().rstrip(";").strip()
        if not cleaned:
            raise ReadOnlyViolation("Empty query.")
        if ";" in cleaned:
            raise ReadOnlyViolation("Only a single statement is allowed.")
        if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
            raise ReadOnlyViolation("Only SELECT or WITH queries are allowed.")
        if _FORBIDDEN_SQL.search(cleaned):
            raise ReadOnlyViolation("Query contains a non-read-only keyword.")
        if not self.include_pii:
            for table in PII_TABLES:
                if re.search(rf"\b{table}\b", cleaned, re.IGNORECASE):
                    raise ReadOnlyViolation(
                        f"Table {table!r} holds personal identifiers and is not exposed."
                    )

        limit = max(1, min(int(limit), 5000))
        budget = sql_timeout_default() if timeout is None else float(timeout)
        with self._timebox(budget):
            cur = self.connection.execute(cleaned)
            rows = cur.fetchmany(limit)
            columns = [d[0] for d in cur.description] if cur.description else []
            truncated = len(cur.fetchmany(1)) > 0
        scrubbed = [self._scrub(dict(r)) for r in rows]

        result = {
            "columns": columns,
            "rows": scrubbed,
            "row_count": len(scrubbed),
            "truncated": truncated,
        }
        resolved = self._label_settings(cleaned, scrubbed)
        if resolved:
            result["columns"] = [*columns, "value_label"]
            result["note"] = (
                "value_label was added by decoding value through channel_options. "
                "The raw numbers are device-specific codes."
            )
        warnings = sql_warnings(cleaned)
        if warnings:
            result["warnings"] = warnings
        return result

    def _label_settings(self, sql: str, rows: list[dict]) -> bool:
        """Decode raw setting codes in a result set, in place.

        A number like 1 in ``session_settings.value`` means different things on
        different channels, so returning it undecoded invites a confident wrong
        reading. When the caller selected enough columns to identify the
        channel, the label is attached rather than left to be guessed.
        """
        if not rows or not re.search(r"\bsession_settings\b", sql, re.IGNORECASE):
            return False
        first = rows[0]
        if "value" not in first:
            return False

        options = self.option_map()
        if "channel_id" in first:
            key_of = lambda row: row.get("channel_id")  # noqa: E731
        elif "channel_code" in first:
            codes = {
                r["channel_code"]: int(r["channel_id"])
                for r in self.query("SELECT DISTINCT channel_code, channel_id FROM channels")
            }
            key_of = lambda row: codes.get(row.get("channel_code"))  # noqa: E731
        else:
            return False

        labelled = False
        for row in rows:
            row["value_label"] = None
            channel_id = key_of(row)
            value = row.get("value")
            if channel_id is None or value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not numeric.is_integer():
                continue
            label = options.get(int(channel_id), {}).get(int(numeric))
            if label is not None:
                row["value_label"] = label
                labelled = True
        return labelled

    def schema(self) -> list[dict]:
        tables = self.query(
            """SELECT name, sql FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"""
        )
        out = []
        for table in tables:
            name = table["name"]
            if not self.include_pii and name in PII_TABLES:
                continue
            count = self.connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            columns = [
                {"name": c["name"], "type": c["type"]}
                for c in self.query(f'PRAGMA table_info("{name}")')
                if self.include_pii or c["name"] not in PII_COLUMNS
            ]
            # Join keys are the single biggest source of silently wrong queries
            # here, so they are reported alongside the columns rather than left
            # to be inferred from matching column names.
            foreign_keys = [
                f'{name}.{fk["from"]} -> {fk["table"]}.{fk["to"]}'
                for fk in self.query(f'PRAGMA foreign_key_list("{name}")')
                if self.include_pii or fk["table"] not in PII_TABLES
            ]
            entry = {"table": name, "rows": count, "columns": columns}
            if foreign_keys:
                entry["foreign_keys"] = foreign_keys
            out.append(entry)
        return out


def parse_date(value: str | None, *, field: str = "date") -> dt.date | None:
    """Parse an ISO date, accepting ``None`` for open-ended ranges."""
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date such as 2024-01-31.") from exc
