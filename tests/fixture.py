"""Build a small synthetic OSCAR database for tests.

Real therapy data is never used in the test suite.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY, username TEXT, data_folder TEXT,
    status TEXT, created_at TEXT
);
CREATE TABLE machines (
    id INTEGER PRIMARY KEY, profile_id INTEGER, machine_id INTEGER,
    loader_name TEXT, machine_type INTEGER, brand TEXT, model TEXT, series TEXT,
    serial_number TEXT, model_number TEXT, last_imported TEXT
);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY, session_id INTEGER, machine_id INTEGER,
    start_time INTEGER, end_time INTEGER, duration INTEGER,
    enabled INTEGER DEFAULT 1, summary_only INTEGER DEFAULT 0,
    events_loaded INTEGER DEFAULT 1
);
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY, session_id INTEGER, profile_id INTEGER,
    ahi REAL, rdi REAL, obstructive_count INTEGER, unclassified_count INTEGER,
    hypopnea_count INTEGER, rera_count INTEGER, clear_airway_count INTEGER,
    pressure_avg REAL, pressure_min REAL, pressure_max REAL, pressure_95th REAL,
    leak_total_avg REAL, leak_total_95th REAL, leak_total_max REAL,
    spo2_avg REAL, spo2_min REAL, pulse_avg REAL, hours_used REAL
);
CREATE TABLE daily_summaries (
    id INTEGER PRIMARY KEY, profile_id INTEGER, date TEXT, session_count INTEGER,
    enabled_session_count INTEGER, total_hours REAL, mask_on_hours REAL,
    ahi REAL, rdi REAL, obstructive_count INTEGER, unclassified_count INTEGER,
    hypopnea_count INTEGER, rera_count INTEGER, clear_airway_count INTEGER,
    pressure_avg REAL, pressure_min REAL, pressure_max REAL, pressure_95th REAL,
    leak_total_avg REAL, leak_total_95th REAL, leak_total_max REAL,
    spo2_avg REAL, spo2_min REAL, pulse_avg REAL, pulse_min REAL, pulse_max REAL,
    is_compliant INTEGER, has_oximetry INTEGER, calculated_at TEXT, sessions_hash TEXT
);
CREATE TABLE channels (
    id INTEGER PRIMARY KEY, profile_id INTEGER, channel_id INTEGER,
    channel_code TEXT, type INTEGER, fullname TEXT, label TEXT, description TEXT
);
CREATE TABLE channel_options (
    id INTEGER PRIMARY KEY, channel_id INTEGER, option_key INTEGER, option_value TEXT
);
CREATE TABLE session_settings (
    id INTEGER PRIMARY KEY, session_id INTEGER, profile_id INTEGER,
    channel_id INTEGER, value REAL, data_type TEXT, json_value TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions (id),
    FOREIGN KEY (profile_id) REFERENCES profiles (id)
);
CREATE TABLE session_channels (
    id INTEGER PRIMARY KEY, session_id INTEGER, profile_id INTEGER, channel_id INTEGER,
    count INTEGER, sum REAL, avg REAL, wavg REAL, min REAL, max REAL,
    median REAL, p90 REAL, p95 REAL, cph REAL, sph REAL
);
CREATE TABLE respiratory_events (
    id INTEGER PRIMARY KEY, session_id INTEGER, profile_id INTEGER, channel_id INTEGER,
    event_type INTEGER, start_time INTEGER, end_time INTEGER, duration INTEGER,
    desaturation REAL, severity REAL
);
CREATE TABLE user_info (
    id INTEGER PRIMARY KEY, profile_id INTEGER, first_name TEXT, last_name TEXT,
    dob TEXT, address TEXT, phone TEXT, email TEXT, password_hash TEXT
);
CREATE TABLE schema_version (version INTEGER);
"""

CHANNELS = [
    (4096, "CSR", "Cheyne Stokes Respiration (CSR)", "CSR"),
    (4097, "ClearAirway", "Clear Airway (CA)", "CA"),
    (4098, "Obstructive", "Obstructive Apnea (OA)", "OA"),
    (4099, "Hypopnea", "Hypopnea (H)", "H"),
    (4102, "RERA", "RERA (RE)", "RE"),
    (4128, "PressureMin", "Min Pressure", "Min Pressure"),
    (4129, "PressureMax", "Max Pressure", "Max Pressure"),
    (4374, "AHI", "Apnea Hypopnea Index (AHI)", "AHI"),
    (4440, "LeakSpan", "Large Leak (LL)", "LL"),
    (4608, "PAPMode", "PAP Mode", "PAP Mode"),
    (57857, "EPR", "EPR", "EPR"),
    (57868, "RMS9_Mask", "Mask", "Mask"),
    (59707, "MaskType", "Mask Type", "Mask Type"),
]

# Two mask channels whose codes are shifted against each other. Carrying a
# mapping from one to the other inverts the answer without any error, so the
# fixture reproduces the collision rather than a single tidy channel.
OPTIONS = [
    (4608, 1, "CPAP"),
    (4608, 2, "APAP (Variable)"),
    (57857, 0, "Off"),
    (57857, 2, "Full Time"),
    (57868, 0, "Pillows"),
    (57868, 1, "Full Face"),
    (57868, 2, "Nasal"),
    (59707, 0, "Full Face"),
    (59707, 1, "Nasal"),
    (59707, 2, "Nasal Pillows"),
    (59707, 3, "Unknown"),
]


def _epoch_ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def build(path: Path, nights: int = 6, with_oximetry: bool = False) -> Path:
    """Create a synthetic ``oscar.db`` at ``path`` and return the file path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)

    con.execute("INSERT INTO schema_version (version) VALUES (17)")
    con.execute(
        "INSERT INTO profiles (id, username, data_folder, status, created_at)"
        " VALUES (1, 'TestUser', '%PROFDIR%/TestUser', 'active', '2024-01-01 00:00:00')"
    )
    con.executemany(
        "INSERT INTO machines (id, profile_id, machine_id, loader_name, machine_type,"
        " brand, model, series, serial_number, model_number, last_imported)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, 1, 100, "Journal", 4, "OSCAR", "Journal", None, "j-0001", None, "2024-01-01"),
            (2, 1, 200, "ResMed", 1, "ResMed", "AirSense11AutoSet", "AirSense 11",
             "SECRET-SERIAL", "39410", "2024-01-10"),
        ],
    )
    con.executemany(
        "INSERT INTO channels (id, profile_id, channel_id, channel_code, type, fullname, label)"
        " VALUES (?,1,?,?,2,?,?)",
        [(i + 1, cid, code, full, label) for i, (cid, code, full, label) in enumerate(CHANNELS)],
    )
    con.executemany(
        "INSERT INTO channel_options (channel_id, option_key, option_value) VALUES (?,?,?)",
        OPTIONS,
    )
    con.execute(
        "INSERT INTO user_info (id, profile_id, first_name, last_name, dob, address, phone,"
        " email, password_hash) VALUES (1, 1, 'Ada', 'Lovelace', '1815-12-10',"
        " '1 Test Street', '+10000000000', 'ada@example.com', 'hash')"
    )

    base = dt.date(2024, 3, 1)
    for night_index in range(nights):
        night = base + dt.timedelta(days=night_index)
        # Start at 22:30 on the night's date and run past midnight.
        start = dt.datetime.combine(night, dt.time(22, 30))
        hours = 7.0 + (night_index % 3) * 0.5
        end = start + dt.timedelta(hours=hours)
        # One session per night, so the primary key tracks the night index.
        session_pk = night_index + 1
        con.execute(
            "INSERT INTO sessions (id, session_id, machine_id, start_time, end_time, duration)"
            " VALUES (?,?,2,?,?,?)",
            (session_pk, 900 + session_pk, _epoch_ms(start), _epoch_ms(end),
             int(hours * 3_600_000)),
        )

        # Event counts drive the indices, exactly as OSCAR derives them, so the
        # fixture stays internally consistent: AHI counts apneas and hypopneas,
        # RDI adds RERAs. Counts rise over the period to give a usable trend.
        obstructive = 2 + night_index
        hypopnea = 3 + night_index
        clear_airway = 4
        unclassified = 0
        rera = 1
        ahi_events = obstructive + hypopnea + clear_airway + unclassified
        ahi = ahi_events / hours
        rdi = (ahi_events + rera) / hours
        spo2 = 94.0 if with_oximetry else 0.0
        con.execute(
            "INSERT INTO session_summaries (session_id, profile_id, ahi, rdi,"
            " obstructive_count, unclassified_count, hypopnea_count, rera_count,"
            " clear_airway_count, pressure_avg, pressure_min, pressure_max, pressure_95th,"
            " leak_total_avg, leak_total_95th, leak_total_max, spo2_avg, spo2_min,"
            " pulse_avg, hours_used) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_pk, ahi, rdi, obstructive, unclassified, hypopnea, rera,
             clear_airway, 8.5, 4.0, 12.0, 11.0,
             3.0, 8.0, 20.0, spo2, spo2 - 4 if with_oximetry else 0.0,
             62.0 if with_oximetry else 0.0, hours),
        )

        # The last night is intentionally left out of daily_summaries so the
        # computed fallback path is exercised.
        if night_index < nights - 1:
            con.execute(
                "INSERT INTO daily_summaries (profile_id, date, session_count,"
                " enabled_session_count, total_hours, mask_on_hours, ahi, rdi,"
                " obstructive_count, unclassified_count, hypopnea_count, rera_count,"
                " clear_airway_count, pressure_avg, pressure_min, pressure_max,"
                " pressure_95th, leak_total_avg, leak_total_95th, leak_total_max,"
                " spo2_avg, spo2_min, pulse_avg, pulse_min, pulse_max, is_compliant,"
                " has_oximetry, calculated_at, sessions_hash)"
                " VALUES (1,?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (night.isoformat(), hours, hours, ahi, rdi,
                 obstructive, unclassified, hypopnea, rera, clear_airway,
                 8.5, 4.0, 12.0, 11.0, 3.0, 8.0, 20.0,
                 spo2, spo2 - 4 if with_oximetry else 0.0,
                 62.0 if with_oximetry else 0.0, 55.0 if with_oximetry else 0.0,
                 90.0 if with_oximetry else 0.0,
                 1, int(with_oximetry), "2024-03-10 00:00:00", "hash"),
            )

        for channel_id, offset, duration in (
            (4097, 30, 15), (4098, 90, 20), (4099, 150, 0), (4102, 210, 0), (4440, 270, 40)
        ):
            event_start = start + dt.timedelta(minutes=offset)
            con.execute(
                "INSERT INTO respiratory_events (session_id, profile_id, channel_id,"
                " event_type, start_time, end_time, duration)"
                " VALUES (?,1,?,?,?,?,?)",
                (session_pk, channel_id, 0 if channel_id == 4440 else 1,
                 _epoch_ms(event_start),
                 _epoch_ms(event_start + dt.timedelta(seconds=duration)), duration),
            )

        mask = 2 if night_index % 2 == 0 else 1
        for channel_id, value in ((4608, 2.0), (57857, 2.0), (4128, 4.0),
                                  (4129, 12.0), (57868, float(mask))):
            con.execute(
                "INSERT INTO session_settings (session_id, profile_id, channel_id, value,"
                " data_type) VALUES (?,1,?,?,'numeric')",
                (session_pk, channel_id, value),
            )

        con.execute(
            "INSERT INTO session_channels (session_id, profile_id, channel_id, count, sum,"
            " avg, wavg, min, max, median, p90, p95, cph, sph)"
            " VALUES (?,1,4374,100,150.0,?,?,0.0,4.0,1.0,3.0,3.5,?,0.0)",
            (session_pk, ahi, ahi, ahi),
        )

    con.commit()
    con.close()
    return path
