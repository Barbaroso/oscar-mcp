import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oscar_mcp import analysis, discovery, knowledge, server
from oscar_mcp.database import (
    NIGHT_SQL,
    OscarDatabase,
    ReadOnlyViolation,
    parse_date,
    therapy_date,
)
from oscar_mcp.discovery import DataFolderNotFound, OscarLocation, discover

from . import fixture

NIGHTS = 6
FIRST_NIGHT = "2024-03-01"
LAST_NIGHT = "2024-03-06"


@pytest.fixture(scope="module")
def location(tmp_path_factory) -> OscarLocation:
    data_dir = tmp_path_factory.mktemp("oscar_data")
    db_path = fixture.build(data_dir / "oscar.db", nights=NIGHTS)
    return OscarLocation(data_dir, db_path, "test")


@pytest.fixture
def db(location: OscarLocation) -> OscarDatabase:
    database = OscarDatabase(location)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def bound_server(location: OscarLocation):
    """Point the module-level server at the synthetic database."""
    database = OscarDatabase(location)
    server.set_db(database)
    yield
    database.close()
    server.set_db(None)


# ----------------------------------------------------------------------
# discovery
# ----------------------------------------------------------------------
def test_discover_uses_explicit_path(location):
    found = discover(location.data_dir)
    assert found.db_path == location.db_path
    assert found.source == "argument"


def test_discover_reads_env(monkeypatch, location):
    monkeypatch.setenv("OSCAR_DATA_DIR", str(location.data_dir))
    assert discover().source == "env:OSCAR_DATA_DIR"


def test_discover_reports_failure(tmp_path, monkeypatch):
    # Isolate discovery from this machine's real OSCAR installation.
    monkeypatch.delenv("OSCAR_DATA_DIR", raising=False)
    monkeypatch.setattr(discovery, "_registry_candidates", list)
    monkeypatch.setattr(discovery, "_documents_dirs", lambda: [tmp_path])
    with pytest.raises(DataFolderNotFound):
        discover(tmp_path / "nowhere")


def test_discover_finds_documents_folder(tmp_path, monkeypatch):
    data_dir = tmp_path / "OSCAR20_Data"
    fixture.build(data_dir / "oscar.db", nights=1)
    monkeypatch.delenv("OSCAR_DATA_DIR", raising=False)
    monkeypatch.setattr(discovery, "_registry_candidates", list)
    monkeypatch.setattr(discovery, "_documents_dirs", lambda: [tmp_path])
    assert discover().source == "documents-folder"


# ----------------------------------------------------------------------
# day mapping
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "moment,expected",
    [
        (dt.datetime(2024, 3, 1, 22, 30), dt.date(2024, 3, 1)),
        (dt.datetime(2024, 3, 2, 3, 15), dt.date(2024, 3, 1)),
        (dt.datetime(2024, 3, 2, 11, 59), dt.date(2024, 3, 1)),
        (dt.datetime(2024, 3, 2, 12, 0), dt.date(2024, 3, 2)),
        (dt.datetime(2024, 3, 2, 13, 30), dt.date(2024, 3, 2)),
    ],
)
def test_therapy_date_splits_at_noon(moment, expected):
    assert therapy_date(int(moment.timestamp() * 1000)) == expected


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_date("not-a-date")
    assert parse_date(None) is None


# ----------------------------------------------------------------------
# read-only guard
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "",
        "DELETE FROM sessions",
        "UPDATE sessions SET duration = 0",
        "DROP TABLE sessions",
        "INSERT INTO sessions (id) VALUES (1)",
        "PRAGMA table_info(sessions)",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT 1; DROP TABLE sessions",
        "SELECT first_name FROM user_info",
        "SELECT last_name AS x FROM user_info",
    ],
)
def test_run_select_rejects_unsafe_sql(db, sql):
    with pytest.raises(ReadOnlyViolation):
        db.run_select(sql)


def test_run_select_allows_reads(db):
    result = db.run_select("SELECT date, ahi FROM daily_summaries ORDER BY date")
    assert result["columns"] == ["date", "ahi"]
    assert result["rows"][0]["date"] == FIRST_NIGHT
    assert result["truncated"] is False


def test_run_select_enforces_limit(db):
    result = db.run_select("SELECT date FROM daily_summaries ORDER BY date", limit=2)
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_database_opens_read_only(db):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        db.connection.execute("DELETE FROM sessions")


# ----------------------------------------------------------------------
# privacy
# ----------------------------------------------------------------------
def test_pii_columns_are_withheld(db):
    row = db.query_one("SELECT * FROM machines WHERE id = 2")
    assert "serial_number" not in row
    assert row["model"] == "AirSense11AutoSet"


def test_pii_tables_hidden_from_schema(db):
    tables = {t["table"] for t in db.schema()}
    assert "user_info" not in tables
    assert "daily_summaries" in tables


def test_pii_available_when_enabled(location):
    database = OscarDatabase(location, include_pii=True)
    try:
        row = database.query_one("SELECT * FROM machines WHERE id = 2")
        assert row["serial_number"] == "SECRET-SERIAL"
        assert database.run_select("SELECT first_name FROM user_info")["row_count"] == 1
    finally:
        database.close()


# ----------------------------------------------------------------------
# analysis helpers
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "ahi,band",
    [(0.0, "normal"), (4.9, "normal"), (5.0, "mild"), (14.9, "mild"),
     (15.0, "moderate"), (30.0, "severe"), (None, None)],
)
def test_severity_band(ahi, band):
    assert analysis.severity_band(ahi) == band


def test_percentile_interpolates():
    assert analysis.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert analysis.percentile([5], 95) == 5
    assert analysis.percentile([], 95) is None


def test_trend_needs_enough_nights():
    rows = [{"date": f"2024-03-0{i}", "ahi": 1.0} for i in range(1, 4)]
    assert analysis.trend(rows, "ahi") is None


def test_trend_detects_direction():
    rows = [{"date": f"2024-03-0{i}", "ahi": float(i)} for i in range(1, 7)]
    result = analysis.trend(rows, "ahi")
    assert result["direction"] == "increasing"
    assert result["change"] > 0


def test_trend_reports_stable_when_flat():
    rows = [{"date": f"2024-03-0{i}", "ahi": 2.0} for i in range(1, 7)]
    assert analysis.trend(rows, "ahi")["direction"] == "stable"


def test_zero_oximetry_is_treated_as_missing():
    rows = [{"date": "2024-03-01", "spo2_avg": 0.0, "pulse_avg": 0.0}]
    report = analysis.summarise_nights(rows, dt.date(2024, 3, 1), dt.date(2024, 3, 1))
    assert report["oximetry"]["available"] is False
    assert report["oximetry"]["spo2_avg"] is None


def test_oximetry_reported_when_present():
    rows = [{"date": "2024-03-01", "spo2_avg": 94.0, "pulse_avg": 60.0}]
    report = analysis.summarise_nights(rows, dt.date(2024, 3, 1), dt.date(2024, 3, 1))
    assert report["oximetry"]["available"] is True
    assert report["oximetry"]["spo2_avg"]["mean"] == 94.0


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------
def test_list_profiles_reports_coverage():
    result = server.list_profiles()
    profile = result["profiles"][0]
    assert profile["nights_with_data"] == NIGHTS
    assert profile["first_night"] == FIRST_NIGHT
    assert profile["last_night"] == LAST_NIGHT
    assert profile["devices"] == ["ResMed AirSense11AutoSet"]


def test_device_info_flags_journal():
    devices = {d["model"]: d for d in server.get_device_info()["devices"]}
    assert devices["Journal"]["is_therapy_device"] is False
    assert devices["Journal"]["session_count"] == 0
    assert devices["AirSense11AutoSet"]["is_therapy_device"] is True
    assert devices["AirSense11AutoSet"]["session_count"] == NIGHTS


def test_daily_summaries_fill_missing_nights():
    nights = server.get_daily_summaries()["nights"]
    assert len(nights) == NIGHTS
    assert nights[0]["source"] == "oscar"
    # The fixture omits the final night from daily_summaries on purpose.
    assert nights[-1]["source"] == "computed"
    assert nights[-1]["date"] == LAST_NIGHT
    assert nights[-1]["session_count"] == 1


def test_daily_summaries_drop_bookkeeping_columns():
    night = server.get_daily_summaries()["nights"][0]
    for noise in ("id", "profile_id", "sessions_hash", "calculated_at"):
        assert noise not in night
    assert night["severity"] == "normal"
    assert night["is_compliant"] is True


def test_daily_summaries_respect_range():
    nights = server.get_daily_summaries(start_date="2024-03-02", end_date="2024-03-03")["nights"]
    assert [n["date"] for n in nights] == ["2024-03-02", "2024-03-03"]


def test_daily_summaries_keep_most_recent_when_truncated():
    result = server.get_daily_summaries(limit=2)
    assert result["truncated"] is True
    assert [n["date"] for n in result["nights"]] == ["2024-03-05", LAST_NIGHT]


def test_statistics_summarise_period():
    stats = server.get_statistics()
    assert stats["period"]["nights_with_data"] == NIGHTS
    assert stats["usage"]["compliance_rate_pct"] == 100.0
    assert stats["ahi"]["nights_by_severity"]["normal"] == NIGHTS
    assert stats["events_total"]["obstructive"] == sum(2 + i for i in range(NIGHTS))
    assert "disclaimer" in stats


def test_statistics_report_empty_period():
    stats = server.get_statistics(start_date="2020-01-01", end_date="2020-01-05")
    assert stats["period"]["nights_with_data"] == 0
    assert stats["note"]


def test_daily_detail_decodes_settings():
    detail = server.get_daily_detail(FIRST_NIGHT)
    assert detail["summary"]["date"] == FIRST_NIGHT
    assert len(detail["sessions"]) == 1
    assert detail["settings"]["PAPMode"]["label"] == "APAP (Variable)"
    assert detail["settings"]["EPR"]["label"] == "Full Time"
    # Numeric settings have no enum labels.
    assert detail["settings"]["PressureMin"]["label"] is None
    assert detail["settings"]["PressureMin"]["value"] == 4.0


def test_daily_detail_requires_valid_date():
    with pytest.raises(ValueError):
        server.get_daily_detail("yesterday")


def test_respiratory_events_use_channel_mapping():
    events = server.get_respiratory_events(FIRST_NIGHT, include_events=True)
    assert events["counts_by_type"] == {
        "ClearAirway": 1,
        "Hypopnea": 1,
        "Obstructive": 1,
        "RERA": 1,
    }
    # Leak spans share the table but must not count as respiratory events.
    assert events["count"] == 4
    assert events["large_leak_spans"]["count"] == 1
    assert len(events["events"]) == 4


def test_respiratory_events_hide_absent_durations():
    events = server.get_respiratory_events(FIRST_NIGHT)
    assert events["mean_duration_seconds"]["ClearAirway"] == 15.0
    assert events["mean_duration_seconds"]["RERA"] is None


def test_respiratory_events_handle_empty_night():
    result = server.get_respiratory_events("2020-01-01")
    assert result["count"] == 0


def test_therapy_settings_detect_changes():
    result = server.get_therapy_settings(changes_only=True)
    masks = [c for c in result["changes"] if "RMS9_Mask" in c["changed"]]
    assert masks, "alternating mask setting should be reported"
    assert masks[0]["changed"]["RMS9_Mask"] == {"from": "Nasal", "to": "Full Face"}
    assert result["settings_by_night"] == []


def test_therapy_settings_list_nights():
    result = server.get_therapy_settings()
    assert len(result["settings_by_night"]) == NIGHTS
    assert result["settings_by_night"][0]["settings"]["PAPMode"] == "APAP (Variable)"


def test_session_details_name_channels():
    detail = server.get_daily_detail(FIRST_NIGHT)
    session_id = detail["sessions"][0]["session_db_id"]
    result = server.get_session_details(session_id)
    assert result["session"]["device"] == "ResMed AirSense11AutoSet"
    assert result["channels"][0]["channel"] == "AHI"


def test_session_details_reject_unknown_id():
    with pytest.raises(LookupError):
        server.get_session_details(9999)


def test_list_channels_counts_usage():
    channels = server.list_channels()["channels"]
    assert channels[0]["channel_code"] == "AHI"
    assert channels[0]["sessions"] == NIGHTS


def test_describe_database_lists_tables():
    tables = {t["table"]: t for t in server.describe_database()["tables"]}
    assert "user_info" not in tables
    assert tables["daily_summaries"]["rows"] == NIGHTS - 1


def test_run_sql_tool_wraps_guard():
    with pytest.raises(ValueError):
        server.run_sql("DELETE FROM sessions")
    assert server.run_sql("SELECT COUNT(*) AS n FROM sessions")["rows"][0]["n"] == NIGHTS


def test_unknown_profile_is_rejected():
    with pytest.raises(LookupError):
        server.get_statistics(profile="nobody")


# --- semantic layer -------------------------------------------------------


def test_metric_formulas_match_oscar_source():
    """AHI must exclude RERA and RDI must include it, as OSCAR's day.h defines."""
    metrics = {m["name"]: m for m in knowledge.METRICS}
    assert "RERA" in metrics["ahi"]["excludes"]
    assert "RERA" in metrics["rdi"]["counts"]
    assert "rera" in metrics["rdi"]["formula"]
    assert "rera" not in metrics["ahi"]["formula"]


def test_metric_formulas_reproduce_stored_values(db):
    """The documented formulas must agree with the numbers OSCAR itself stored."""
    for row in db.query("SELECT * FROM daily_summaries"):
        counted = (
            row["clear_airway_count"]
            + row["obstructive_count"]
            + row["hypopnea_count"]
            + row["unclassified_count"]
        )
        hours = row["total_hours"]
        assert counted / hours == pytest.approx(row["ahi"], abs=0.01)
        assert (counted + row["rera_count"]) / hours == pytest.approx(row["rdi"], abs=0.01)


def test_entity_model_join_keys_are_real(db):
    """Every documented relationship must be a column that actually exists."""
    for entity in knowledge.ENTITIES:
        columns = {r["name"] for r in db.query(f"PRAGMA table_info({entity['table']})")}
        assert entity["key"] in columns, entity["name"]
        for rel in entity.get("relationships", ()):
            source, target = rel.split(" -> ")
            table, column = source.split(".")
            target_table, target_column = target.split(".")
            assert column in {r["name"] for r in db.query(f"PRAGMA table_info({table})")}
            assert target_column in {
                r["name"] for r in db.query(f"PRAGMA table_info({target_table})")
            }


def test_every_clinical_claim_cites_a_source():
    """Unsourced medical statements are the failure this layer must not have."""
    sourced = [c for c in knowledge.CAVEATS if "source" in c]
    assert len(sourced) >= 5
    for entry in sourced:
        assert entry["source"].startswith("OSCAR:")
    for metric in knowledge.METRICS:
        assert metric.get("source")


def test_interpretation_covers_the_known_traps():
    topics = " ".join(c["topic"] + c["guidance"] for c in knowledge.CAVEATS).lower()
    for expected in ("leak", "clear airway", "sleep study", "brand", "medical advice"):
        assert expected in topics


def test_glossary_defines_the_events_that_drive_ahi():
    terms = {t["term"] for t in knowledge.GLOSSARY}
    assert {"Apnea", "Hypopnea", "Clear Airway", "RERA", "Large Leak"} <= terms


def test_resources_are_registered_and_valid_json():
    """The model must be reachable as MCP resources, not merely importable."""
    registered = {str(r.uri): r for r in asyncio.run(server.server.list_resources())}
    expected = {
        "oscar://model",
        "oscar://model/entities",
        "oscar://model/metrics",
        "oscar://model/glossary",
        "oscar://model/interpretation",
    }
    assert expected <= set(registered)
    for uri in expected:
        contents = asyncio.run(server.server.read_resource(uri))
        payload = json.loads(next(iter(contents)).content)
        assert payload and isinstance(payload, dict)


def test_prompts_are_registered():
    names = {p.name for p in asyncio.run(server.server.list_prompts())}
    assert {
        "review_therapy",
        "investigate_leak",
        "compare_periods",
        "prepare_for_appointment",
    } <= names


def test_prompts_reference_the_caveats_they_depend_on():
    assert "oscar://model/interpretation" in server.review_therapy_prompt()
    assert "leak" in server.investigate_leak_prompt().lower()
    text = server.compare_periods_prompt("2024-01-01", "2024-01-31", "2024-02-01", "2024-02-28")
    assert "2024-01-01" in text and "2024-02-28" in text
    assert "inconclusive" in text.lower()


def test_prompts_do_not_invite_prescriptive_answers():
    for text in (
        server.review_therapy_prompt(),
        server.investigate_leak_prompt(),
        server.appointment_prompt(),
    ):
        assert "clinician" in text.lower()
    assert "do not suggest settings" in server.appointment_prompt().lower()



# --- run_sql guardrails ---------------------------------------------------
#
# Each of these reproduces a query that returned a confidently wrong answer in
# real use. The tool behaved correctly every time; what was missing was any
# signal that the *question* was malformed.


def test_wrong_session_join_is_flagged():
    """Joining on sessions.session_id yields zero rows without any error."""
    result = server.run_sql(
        "SELECT ss.value FROM session_settings ss"
        " JOIN sessions s ON s.session_id = ss.session_id"
    )
    assert result["row_count"] == 0
    assert any("sessions.id" in w for w in result["warnings"])


def test_wrong_channel_join_is_flagged():
    result = server.run_sql(
        "SELECT ss.value FROM session_settings ss JOIN channels c ON c.id = ss.channel_id"
    )
    assert any("channels.channel_id" in w for w in result["warnings"])


def test_midnight_date_split_is_flagged():
    result = server.run_sql("SELECT date(start_time/1000,'unixepoch') d FROM sessions")
    assert any("noon" in w for w in result["warnings"])


def test_correct_night_expression_is_not_flagged(db):
    """The documented expression must reproduce daily_summaries exactly."""
    sql = f"SELECT {NIGHT_SQL} AS night, COUNT(*) n FROM sessions GROUP BY 1 ORDER BY 1"
    result = server.run_sql(sql)
    assert not any("noon" in w for w in result.get("warnings", []))
    computed = {r["night"]: r["n"] for r in result["rows"]}
    for row in db.query("SELECT date, session_count FROM daily_summaries"):
        assert computed[row["date"]] == row["session_count"]


def test_setting_codes_are_decoded_not_left_raw():
    """A bare code invites the reader to guess, and guessing inverted a result."""
    result = server.run_sql(
        "SELECT c.channel_code, ss.value FROM session_settings ss"
        " JOIN channels c ON c.channel_id = ss.channel_id"
    )
    assert "value_label" in result["columns"]
    labelled = [r for r in result["rows"] if r["value_label"]]
    assert labelled


def test_same_code_decodes_differently_per_channel(db):
    """The trap itself: one number, two channels, two different meanings."""
    options = db.option_map()
    codes = {
        r["channel_code"]: int(r["channel_id"])
        for r in db.query("SELECT DISTINCT channel_code, channel_id FROM channels")
    }
    mask_type = options[codes["MaskType"]]
    resmed = options[codes["RMS9_Mask"]]
    assert mask_type[1] == "Nasal"
    assert resmed[1] == "Full Face"
    assert mask_type[1] != resmed[1]


def test_event_type_column_is_flagged():
    result = server.run_sql("SELECT event_type FROM respiratory_events")
    assert any("channel_id" in w for w in result["warnings"])


def test_describe_database_exposes_join_keys():
    tables = {t["table"]: t for t in server.describe_database()["tables"]}
    fks = tables["session_settings"]["foreign_keys"]
    assert "session_settings.session_id -> sessions.id" in fks


def test_describe_database_carries_the_query_rules():
    """The rules must reach the caller through a tool, not only a passive resource."""
    rules = server.describe_database()["query_rules"]
    assert "43200" in rules["night_expression"]
    assert any("sessions.session_id" in r for r in rules["join_keys"])
    assert any("MaskType" in r for r in rules["coded_values"])
