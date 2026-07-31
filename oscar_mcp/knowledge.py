"""Semantic layer describing what OSCAR's data *means*.

Tools answer "what are the numbers"; this module answers "what do the numbers
mean, how are they derived, and how can they be misread". It is the equivalent
of an ontology or semantic model: entity types and their relationships, metric
definitions with their formulas, domain vocabulary, and the caveats a reader
needs in order not to draw a wrong conclusion.

Every clinical or arithmetic statement here is traceable to a primary source in
the OSCAR project itself -- its bundled help glossary or its C++ implementation
-- recorded in the ``source`` field of each entry. Nothing is invented, because
an unsourced claim about medical data is worse than no claim at all.

OSCAR lives at https://gitlab.com/pholy/OSCAR-code.
"""

from __future__ import annotations

from typing import Any

from .database import DEFAULT_SQL_TIMEOUT, NIGHT_SQL

OSCAR_PROJECT = "https://gitlab.com/pholy/OSCAR-code"
GLOSSARY_SOURCE = "OSCAR: help/help_en/glossary.html"
DAY_SOURCE = "OSCAR: SleepLib/day.h"
SCHEMA_SOURCE = "OSCAR: SleepLib/schema.cpp"

# ---------------------------------------------------------------------------
# Entity types
# ---------------------------------------------------------------------------

# Relationships mirror the foreign keys declared in the OSCAR 2.x SQLite schema.
# They are recorded explicitly because the join keys are not guessable: the
# column named `session_id` on `sessions` is OSCAR's own identifier, while every
# foreign key in the database points at `sessions.id` instead.
ENTITIES: tuple[dict[str, Any], ...] = (
    {
        "name": "profile",
        "table": "profiles",
        "key": "id",
        "describes": "A person whose therapy data is stored in this database.",
        "note": (
            "Most installations have exactly one profile. Every other table is "
            "scoped by profile_id, so multi-profile databases must be filtered."
        ),
    },
    {
        "name": "machine",
        "table": "machines",
        "key": "id",
        "describes": "A therapy or recording device that produced sessions.",
        "relationships": ["machines.profile_id -> profiles.id"],
        "note": (
            "machine_type 4 is OSCAR's internal Journal pseudo-device used for "
            "notes and bookmarks. It records no therapy and has no sessions."
        ),
    },
    {
        "name": "session",
        "table": "sessions",
        "key": "id",
        "describes": (
            "One continuous run of the device, from mask-on to mask-off. A "
            "night may contain several sessions if the mask was removed."
        ),
        "relationships": ["sessions.machine_id -> machines.id"],
        "note": (
            "sessions.id is the primary key and the target of every foreign "
            "key. sessions.session_id is OSCAR's own identifier and must not "
            "be used to join. Timestamps are epoch milliseconds."
        ),
    },
    {
        "name": "night",
        "table": "daily_summaries",
        "key": "date",
        "describes": (
            "All sessions belonging to one sleep period, keyed by ISO date. "
            "This is the unit a person means when they say 'last night'."
        ),
        "relationships": ["daily_summaries.profile_id -> profiles.id"],
        "note": (
            "A night is not a calendar day. Sessions that start before noon "
            "are attributed to the previous date, so a 23:00-06:00 sleep is "
            "one night. Rows are written lazily by OSCAR, so the most recent "
            "night is often absent; the server then computes it from sessions "
            "and marks the result source='computed'."
        ),
    },
    {
        "name": "respiratory_event",
        "table": "respiratory_events",
        "key": "id",
        "describes": "One detected breathing event, such as an apnea or hypopnea.",
        "relationships": [
            "respiratory_events.session_id -> sessions.id",
            "respiratory_events.channel_id -> channels.channel_id",
        ],
        "note": (
            "The event kind is given by channel_id, NOT by the column named "
            "event_type -- that column only distinguishes a flag (0) from a "
            "span (1). Leak spans are stored in this table too but are not "
            "respiratory events and never count toward AHI."
        ),
    },
    {
        "name": "channel",
        "table": "channels",
        "key": "channel_id",
        "describes": (
            "A named measurement or event type, for example Hypopnea, "
            "Obstructive, LeakTotal or Pressure."
        ),
        "relationships": ["channels.profile_id -> profiles.id"],
        "note": (
            "channel_options holds the human-readable labels for enumerated "
            "values and joins on channels.channel_id, not on channels.id."
        ),
    },
    {
        "name": "setting",
        "table": "session_settings",
        "key": "id",
        "describes": "A device setting captured for one session, such as mode or mask type.",
        "relationships": ["session_settings.session_id -> sessions.id"],
        "note": (
            "Values are device-specific codes, and the same number means "
            "different things on different channels: 1 is 'Nasal' on MaskType "
            "but 'Full Face' on RMS9_Mask. Decode through channel_options, "
            "where option_key holds the code and option_value the label."
        ),
    },
)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

METRICS: tuple[dict[str, Any], ...] = (
    {
        "name": "ahi",
        "label": "Apnea-Hypopnea Index",
        "unit": "events/hour",
        "definition": (
            "Count of apneas and hypopneas during the night divided by the "
            "hours of therapy."
        ),
        "formula": "(clear_airway + obstructive + hypopnea + unclassified) / total_hours",
        "counts": ["ClearAirway", "Obstructive", "Hypopnea", "Apnea (unclassified)"],
        "excludes": ["RERA", "FlowLimitation", "Snore", "CSR", "LeakSpan"],
        "source": f"{DAY_SOURCE} calcAHI(); channel list in {SCHEMA_SOURCE}",
        "verified": "Reproduces OSCAR's own stored value exactly on this database.",
    },
    {
        "name": "rdi",
        "label": "Respiratory Disturbance Index",
        "unit": "events/hour",
        "definition": "The AHI events plus RERAs, divided by the hours of therapy.",
        "formula": "(clear_airway + obstructive + hypopnea + unclassified + rera) / total_hours",
        "counts": ["everything in AHI", "RERA"],
        "source": f"{DAY_SOURCE} calcRDI()",
        "verified": "Reproduces OSCAR's own stored value exactly on this database.",
        "caveat": (
            "RDI is always >= AHI. They are two views of the same night, not "
            "two independent findings, and must never be added together."
        ),
    },
    {
        "name": "total_hours",
        "label": "Therapy hours",
        "unit": "hours",
        "definition": "Summed duration of the night's sessions; the denominator of AHI and RDI.",
        "source": f"{DAY_SOURCE}",
        "caveat": (
            "A very short night makes the index unstable: two events in 30 "
            "minutes reads as an AHI of 4, which is not comparable to the "
            "same rate measured over eight hours."
        ),
    },
    {
        "name": "leak_total_95th",
        "label": "Leak, 95th percentile",
        "unit": "L/min",
        "definition": "The leak rate exceeded during only 5% of the night.",
        "source": GLOSSARY_SOURCE,
        "caveat": (
            "More informative than the average, which a few large spikes "
            "barely move."
        ),
    },
    {
        "name": "is_compliant",
        "label": "Compliance",
        "unit": "boolean",
        "definition": "Whether therapy use reached 4 hours for the night.",
        "source": "Insurer and equipment-provider convention.",
        "caveat": (
            "This is a payer reporting threshold, not a clinical target. "
            "Meeting it does not mean the night was well treated, and the "
            "usual clinical aim is to use the device for the whole sleep."
        ),
    },
)

# ---------------------------------------------------------------------------
# Domain vocabulary
# ---------------------------------------------------------------------------

# Condensed from OSCAR's own help glossary so that the wording an LLM sees
# matches the wording the application itself uses.
GLOSSARY: tuple[dict[str, str], ...] = (
    {
        "term": "Apnea",
        "meaning": (
            "A cessation or near-cessation of airflow lasting at least 10 "
            "seconds. Devices flag it when airflow falls by roughly 80% "
            "(Respironics) or 75% (ResMed) against the recent baseline."
        ),
    },
    {
        "term": "Obstructive Apnea",
        "meaning": (
            "An apnea caused by the airway collapsing while the effort to "
            "breathe continues. This is the type CPAP pressure is intended to "
            "prevent."
        ),
    },
    {
        "term": "Clear Airway",
        "meaning": (
            "An apnea detected while the airway appears open. The device "
            "probes with small pressure pulses and infers a clear airway when "
            "it sees a flow response. Often reported as a central apnea, but "
            "it is a device inference and not a scored clinical finding."
        ),
    },
    {
        "term": "Hypopnea",
        "meaning": (
            "A partial reduction in airflow lasting 10 to 60 seconds, followed "
            "by recovery breaths. Respironics flags it at about a 40% "
            "reduction and ResMed at about 50%, so the same breathing can "
            "yield different counts on different brands."
        ),
    },
    {
        "term": "RERA",
        "meaning": (
            "Respiratory Effort Related Arousal: increasing effort to breathe "
            "that ends in an arousal from sleep without meeting apnea or "
            "hypopnea criteria. Counts toward RDI but not AHI."
        ),
    },
    {
        "term": "Flow Limitation",
        "meaning": (
            "A flattening of the inspiratory flow shape indicating partially "
            "restricted breathing that has not become a scored event."
        ),
    },
    {
        "term": "CSR",
        "meaning": (
            "Cheyne-Stokes Respiration: a cyclic crescendo and decrescendo of "
            "breathing depth, typically over cycles of 30 seconds to 2 "
            "minutes. Any sustained appearance is worth raising with a "
            "clinician."
        ),
    },
    {
        "term": "Large Leak",
        "meaning": (
            "Air escaping in quantities that will compromise therapy, usually "
            "from mask fit, mask condition or mouth leak."
        ),
    },
    {
        "term": "EPR",
        "meaning": (
            "Expiratory Pressure Relief: a ResMed comfort feature that lowers "
            "pressure during exhalation by a set number of cmH2O."
        ),
    },
    {
        "term": "Ramp",
        "meaning": (
            "A gentle start period during which pressure rises to the therapy "
            "setting, intended to make falling asleep easier."
        ),
    },
    {
        "term": "APAP",
        "meaning": (
            "Automatic positive airway pressure: the device varies pressure "
            "within a configured range in response to detected events."
        ),
    },
)

# ---------------------------------------------------------------------------
# Interpretation rules
# ---------------------------------------------------------------------------

# Each caveat names the failure it prevents. These are the conclusions a
# competent reader would avoid but a purely numeric reader would not.
CAVEATS: tuple[dict[str, str], ...] = (
    {
        "topic": "Device AHI is not a sleep study",
        "guidance": (
            "The device infers events from airflow alone. It cannot tell sleep "
            "from wakefulness, so mask-on time awake dilutes the index, and it "
            "does not measure arousals or oxygen desaturation. A device AHI is "
            "therefore not equivalent to a diagnostic sleep-study AHI and must "
            "not be used to claim a diagnosis or a cure."
        ),
        "source": GLOSSARY_SOURCE,
    },
    {
        "topic": "Large leak invalidates the night's numbers",
        "guidance": (
            "Leak large enough to compromise therapy also degrades event "
            "detection, because the device is inferring from an airflow signal "
            "that is no longer trustworthy. Check leak before drawing any "
            "conclusion from AHI, and never compare a high-leak night with a "
            "sealed one as though the two indices meant the same thing."
        ),
        "source": GLOSSARY_SOURCE,
    },
    {
        "topic": "Clear airway events are not treated by more pressure",
        "guidance": (
            "Obstructive events respond to pressure; clear-airway events "
            "generally do not, and raising pressure can increase them. A "
            "rising share of clear-airway events is a reason to consult a "
            "clinician, not a reason to suggest a pressure change."
        ),
        "source": GLOSSARY_SOURCE,
    },
    {
        "topic": "AHI and RDI overlap",
        "guidance": (
            "RDI already contains every event in AHI. Report them side by "
            "side, never summed, and attribute a gap between them to RERAs."
        ),
        "source": DAY_SOURCE,
    },
    {
        "topic": "Cross-brand comparison is unsafe",
        "guidance": (
            "Detection thresholds differ between manufacturers, notably 40% "
            "versus 50% for hypopnea. AHI from a ResMed and a Respironics "
            "machine are not directly comparable, so check whether the machine "
            "changed before explaining a step change in the numbers."
        ),
        "source": GLOSSARY_SOURCE,
    },
    {
        "topic": "Single nights are noisy",
        "guidance": (
            "Night-to-night variation is normal and is affected by alcohol, "
            "illness, body position and sleep duration. Judge direction from a "
            "week or more, and treat one bad night as an observation rather "
            "than a trend."
        ),
    },
    {
        "topic": "Missing is not zero",
        "guidance": (
            "Oximetry fields hold 0 when no oximeter was attached, and short "
            "nights make rates unstable. Absent data is reported as "
            "unavailable and must never be read as a measured value."
        ),
    },
    {
        "topic": "Not medical advice",
        "guidance": (
            "This data supports conversations with a clinician. Do not "
            "recommend pressure settings, diagnose, or advise stopping or "
            "changing prescribed therapy."
        ),
    },
)


def entity_model() -> dict[str, Any]:
    """The entity/relationship view of the database."""
    return {
        "description": (
            "How OSCAR's tables map onto real-world concepts, and how they "
            "join. Consult this before writing SQL with run_sql."
        ),
        "entities": list(ENTITIES),
        "join_warnings": SQL_RULES["join_keys"],
    }


# ---------------------------------------------------------------------------
# Rules for writing correct SQL
# ---------------------------------------------------------------------------

# This schema reuses column names across tables, so the usual guess -- join the
# columns whose names match -- produces zero rows or, worse, a plausible but
# wrong answer. These rules are returned by describe_database rather than kept
# in a resource, because a passive resource is only read if someone asks for it,
# and the queries that need it are written by someone who did not.
SQL_RULES: dict[str, Any] = {
    "join_keys": [
        "Foreign keys reference sessions.id. sessions.session_id is OSCAR's own "
        "identifier and joining on it silently returns zero rows.",
        "Join channels on channels.channel_id. channels.id is only a row number.",
        "channel_options joins on channel_id as well, and has no profile_id column.",
        "session_channel_values references session_channels.id, not sessions.id.",
    ],
    "coded_values": [
        "session_settings.value is a device-specific code, not a label.",
        "Decode it through channel_options, where option_key is the numeric code "
        "and option_value is the label -- note that the names read backwards.",
        "The same number means different things on different channels: 1 is "
        "'Nasal' on MaskType but 'Full Face' on RMS9_Mask. Never carry a mapping "
        "from one channel over to another.",
        "get_therapy_settings already applies the correct mapping.",
    ],
    "time": [
        "Timestamps are epoch milliseconds, so divide by 1000 before using "
        "SQLite date functions.",
        "A night runs from noon to noon. Taking the plain date of start_time "
        "splits at midnight and misfiles every session that began after it.",
        f"Use {NIGHT_SQL} to reproduce the dates in daily_summaries.",
    ],
    "events": [
        "respiratory_events.channel_id gives the kind of event. The column "
        "named event_type only says whether it is a flag (0) or a span (1).",
        "Leak spans live in respiratory_events too but are not respiratory "
        "events and never count toward AHI.",
    ],
    "limits": [
        "run_sql is cancelled if it exceeds its time budget "
        f"({DEFAULT_SQL_TIMEOUT:g}s by default, set by OSCAR_MCP_SQL_TIMEOUT).",
        "The row limit does not bound the work: producing the first row of an "
        "unintended cross join still requires scanning every combination, so a "
        "missing join condition is what a cancellation usually means.",
        "session_channel_values is by far the largest table. Join it on "
        "session_channels.id and filter before aggregating.",
    ],
}


def sql_rules() -> dict[str, Any]:
    """Join, decoding and time rules needed to write a correct query."""
    return {
        "description": (
            "Read before writing SQL. These are the rules whose violation "
            "produces a wrong answer rather than an error."
        ),
        "night_expression": NIGHT_SQL,
        **SQL_RULES,
    }


def metric_model() -> dict[str, Any]:
    """Metric definitions with their formulas and provenance."""
    return {
        "description": (
            "What each headline number means and exactly how it is derived "
            "from event counts and therapy hours."
        ),
        "metrics": list(METRICS),
    }


def glossary() -> dict[str, Any]:
    """Domain vocabulary, aligned with OSCAR's own help text."""
    return {
        "description": "Therapy terms as OSCAR itself defines them.",
        "source": GLOSSARY_SOURCE,
        "terms": list(GLOSSARY),
    }


def interpretation_guide() -> dict[str, Any]:
    """The caveats that keep a numeric reading from becoming a wrong one."""
    return {
        "description": (
            "Read this before interpreting any figure. Each entry describes a "
            "conclusion that the raw numbers would otherwise invite."
        ),
        "caveats": list(CAVEATS),
    }


def overview() -> dict[str, Any]:
    """Everything above in one payload, for clients that read a single resource."""
    return {
        "entity_model": entity_model(),
        "metric_model": metric_model(),
        "glossary": glossary(),
        "interpretation": interpretation_guide(),
        "sql_rules": sql_rules(),
    }
