# OSCAR MCP Server

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets an
LLM assistant — Claude Desktop, GitHub Copilot CLI, VS Code, or anything else that speaks
MCP — analyse your own CPAP/BiPAP therapy data from
[OSCAR](https://gitlab.com/pholy/OSCAR-code).

It talks directly to the SQLite database that OSCAR 2.x writes (`oscar.db`), so no export
step is needed and OSCAR can stay open while you use it.

```
Claude / Copilot  ──stdio──►  oscar-mcp  ──read-only──►  oscar.db
```

Your data never leaves your machine. The server opens the database read-only, withholds
personal identifiers by default, and cannot write to it even if asked.

> **Not medical advice.** This is an informational tool for reviewing your own data. It is
> not a medical device, and it is not affiliated with or endorsed by the OSCAR project.

## What you can ask

Once connected, questions like these work:

- *"Summarise my last 30 nights of CPAP therapy."*
- *"Is my AHI trending up or down?"*
- *"Which nights had the worst leaks, and did the mask setting change on those nights?"*
- *"Show me when apneas clustered during the night of 12 March."*
- *"Did changing my minimum pressure make a difference?"*

## Requirements

- Python 3.10+
- OSCAR 2.x, which stores data in `oscar.db`
  (OSCAR 1.x uses a different on-disk format and is not supported)

## Install

```bash
git clone https://github.com/Barbaroso/oscar-mcp.git
cd oscar-mcp
pip install -e .
```

Check that it can find your data:

```bash
python -c "from oscar_mcp import discover; print(discover().as_dict())"
```

Expected output:

```
{'data_dir': 'C:\\Users\\you\\Documents\\OSCAR20_Data',
 'db_path':  'C:\\Users\\you\\Documents\\OSCAR20_Data\\oscar.db',
 'discovered_via': 'registry:HKCU\\Software\\OSCAR_Team\\OSCAR 2.0\\Settings'}
```

The data folder is resolved in this order: `OSCAR_DATA_DIR` → the path OSCAR recorded in the
Windows registry → common documents folders (including OneDrive-redirected ones). If none of
those work, set `OSCAR_DATA_DIR` explicitly.

## Connect it

### Claude Desktop

Edit `claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "oscar": {
      "command": "python",
      "args": ["-m", "oscar_mcp"],
      "env": {
        "OSCAR_DATA_DIR": "C:\\Users\\you\\Documents\\OSCAR20_Data"
      }
    }
  }
}
```

Restart Claude Desktop. The OSCAR tools appear in the tools menu.

### GitHub Copilot CLI / VS Code

Add to `mcp.json`:

```json
{
  "servers": {
    "oscar": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "oscar_mcp"],
      "env": {
        "OSCAR_DATA_DIR": "C:\\Users\\you\\Documents\\OSCAR20_Data"
      }
    }
  }
}
```

### Not installed with pip?

Point at the source directory instead:

```json
{
  "command": "python",
  "args": ["-m", "oscar_mcp"],
  "cwd": "C:\\path\\to\\oscar-mcp"
}
```

On Windows, use the full interpreter path (for example `C:\\Python314\\python.exe`) if
`python` is not on the PATH seen by the client application.

## Tools

| Tool | Purpose |
|---|---|
| `list_profiles` | Profiles, devices and the date range that has data. Start here. |
| `get_device_info` | Therapy devices, model and last import time. |
| `get_daily_summaries` | Night-by-night table: usage, AHI, events, pressure, leak. |
| `get_statistics` | Aggregates for a period: compliance, AHI distribution, trends. |
| `get_daily_detail` | One night in full: sessions, settings, per-channel statistics. |
| `get_respiratory_events` | Individual apneas/hypopneas/RERAs and when they clustered. |
| `get_therapy_settings` | Machine settings over time, and what changed on which night. |
| `get_session_details` | Per-channel statistics for a single session. |
| `list_channels` | Maps numeric channel ids to names such as AHI or Leak Rate. |
| `describe_database` | Tables, columns, foreign keys and the rules for writing a correct query. |
| `run_sql` | A single read-only `SELECT` for anything the other tools miss. |

### run_sql guardrails

This schema reuses column names across tables, so the natural guess — join the columns whose
names match — returns zero rows or a plausible but wrong answer rather than an error. The
traps are real: `sessions.session_id` is not the primary key, `channels.id` is not the
channel identifier, and `session_settings.value` is a device-specific code where `1` means
*Nasal* on `MaskType` but *Full Face* on `RMS9_Mask`.

`run_sql` therefore does four things beyond running the query:

- **Warns on query shapes known to return silently wrong results** — wrong join key, a date
  taken from `start_time` without the noon shift, or reading `respiratory_events.event_type`
  as though it were the event kind.
- **Decodes setting codes automatically**, adding a `value_label` column resolved through
  `channel_options`, so a raw number is never left to be guessed.
- **Points to `get_therapy_settings`** for categorical settings, which applies the mapping
  itself.
- **Cancels a query that overruns its time budget** (10 s by default), rather than hanging.
  The row limit cannot prevent this on its own: producing the *first* row of an unintended
  cross join already requires scanning every combination, so a missing join condition runs
  forever no matter how few rows you asked for. The cancellation message says so, because
  that is nearly always the cause.

`describe_database` returns the foreign keys and the same rules, because guidance that lives
only in a passive resource is not read by the caller who needs it.

Responses carry their units and reference bands (AHI < 5 normal, 5–15 mild, 15–30 moderate,
≥ 30 severe; 4 h/night compliance; 24 L/min large-leak threshold) so the assistant reads the
numbers in context rather than guessing.

## Resources: what the numbers mean

Tools return values. Resources return the domain model needed to read those values
correctly, so the assistant is not left inferring clinical meaning from column names.
They are static, cheap to read, and cost no tool call.

| Resource | Contents |
|---|---|
| `oscar://model/metrics` | Exact AHI and RDI formulas, which events count toward each, and the source in OSCAR's code. |
| `oscar://model/interpretation` | The caveats that turn a correct number into a wrong conclusion. |
| `oscar://model/entities` | Tables as real-world concepts, with the join keys and the traps in them. |
| `oscar://model/glossary` | Apnea, hypopnea, clear airway, RERA, CSR, leak — as OSCAR itself defines them. |
| `oscar://model` | All of the above in one document. |

Every clinical or arithmetic claim cites a primary source in the OSCAR project — its own
`help/help_en/glossary.html` or its C++ implementation in `SleepLib/` — rather than restating
received wisdom. The AHI and RDI formulas are taken from `SleepLib/day.h` and reproduce
OSCAR's own stored values exactly. A test enforces that no claim ships without a citation.

This matters because the raw numbers invite specific wrong readings, for example:

- **Large leak invalidates a night.** Leak severe enough to compromise therapy also degrades
  event detection, so that night's AHI is not comparable with a well-sealed night's.
- **RDI already contains AHI.** They are two views of one night, separated by RERAs, and must
  never be summed.
- **Clear-airway events are not treated by more pressure**, unlike obstructive ones.
- **Cross-brand AHI is not comparable**: ResMed flags hypopnea at ~50% flow reduction,
  Respironics at ~40%.
- **A device AHI is not a sleep study.** The machine cannot tell sleep from wakefulness.

## Prompts: how to run a review

Reusable workflows that encode the order of questions producing a sound reading, so the
analysis does not depend on knowing which tool to ask for first.

| Prompt | Purpose |
|---|---|
| `review_therapy` | Review recent nights, checking leak before drawing conclusions from AHI. |
| `investigate_leak` | Find which nights leaked, how badly, and what changed around them. |
| `compare_periods` | Test whether something really changed between two date ranges. |
| `prepare_for_appointment` | A factual summary to bring to a clinician, with questions to ask. |

### Nights, not calendar days

A session that starts after midnight belongs to the previous night, matching how OSCAR
itself reports data. A session starting at 02:00 on 12 March is part of the night of
11 March. The cut-off is noon.

### Where the numbers come from

OSCAR computes `daily_summaries` lazily, so the most recent night is often missing from it.
Those nights are recomputed from the underlying sessions. Every night carries a `source`
field: `oscar` for values OSCAR itself calculated, `computed` for values derived here.

## Privacy

Your therapy data is medical data. This server is built to keep the exposure minimal:

- **Read-only.** The database is opened with SQLite's `mode=ro` URI, so writes fail at the
  driver level. `run_sql` additionally accepts only a single `SELECT`/`WITH` statement and
  rejects `PRAGMA`, `ATTACH` and every write keyword.
- **No identifiers by default.** `first_name`, `last_name`, `dob`, `address`, `phone`,
  `email`, `password_hash` and device `serial_number` are stripped from every response, and
  the `user_info` / `doctor_info` tables are not reachable at all — including through
  renamed expressions in `run_sql`.
- **Local only.** The server runs on your machine and speaks stdio to the client next to it.
  It opens no network connections of its own.
- **OSCAR is untouched.** The database uses WAL journalling, so reading while OSCAR is
  running is safe and changes nothing.

Set `OSCAR_MCP_INCLUDE_PII=1` to lift the identifier filtering. Only do that if you
understand that the data then leaves your machine as part of the conversation with whatever
model your client is using.

## Configuration

| Variable | Effect |
|---|---|
| `OSCAR_DATA_DIR` | Path to the folder containing `oscar.db`. Overrides auto-detection. |
| `OSCAR_MCP_SQL_TIMEOUT` | Seconds a `run_sql` query may run before it is cancelled. Default 10. |
| `OSCAR_MCP_INCLUDE_PII` | `1` to include personal identifiers. Off by default. |

## Development

```bash
pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

Tests run against a synthetic database built by `tests/fixture.py`. No real therapy data is
used or committed. The fixture derives AHI and RDI from its own event counts using OSCAR's
formulas, so the semantic layer's documented arithmetic is verified rather than asserted, and
it reproduces the schema's join and enum hazards so the guardrails can be tested against them.

Contributions are welcome. Two rules matter more than style here:

1. **No unsourced clinical claims.** Anything the server asserts about therapy must cite a
   primary source in the OSCAR project, and a test enforces this.
2. **Read-only, always.** The database is someone's medical record. No code path may open it
   writable or widen what `run_sql` accepts.

## Credits

Built for [OSCAR](https://gitlab.com/pholy/OSCAR-code) (Open Source CPAP Analysis Reporter),
whose developers did the hard work of decoding CPAP device formats. The clinical definitions
and the AHI/RDI formulas used here come from OSCAR's own help glossary and source code, cited
inline in `oscar_mcp/knowledge.py`.

This project is an independent companion tool. It is not affiliated with, endorsed by, or
supported by the OSCAR project or by any device manufacturer.

## License

GPL-3.0-or-later, matching OSCAR, from which the clinical reference material is derived.
See [LICENSE](LICENSE).

## Disclaimer

This is an informational tool for reviewing your own data. It is not a medical device and
does not provide medical advice. Discuss any therapy change with your clinician.
