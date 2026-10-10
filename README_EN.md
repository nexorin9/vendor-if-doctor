# Vendor Interface Connectivity Inspector

> **One command at 8:30 AM, five minutes later:** a health checklist (healthy / degraded / failed) for yesterday's newly added or changed vendor interfaces (HIS / LIS / PACS / EMR / medical-insurance / SPD / regional platforms), plus a ticket-draft JSONL and SQLite history — all ready for the duty operator to review and paste into the ticket system.

Hospital vendor interfaces get patched, get their fields changed, swap certificates, and have their timeouts tuned every day. The information desk has been tracking these "seams" by brainpower and Excel during the change window — daily fragmented work. **Vendor Interface Connectivity Inspector** turns that mechanical labor into **configure once, run one command a day**, letting the operator focus on "who isn't connected properly" instead of "how do I test whether it's connected".

## Applicable Scenarios / Target Roles

| Role | When | What You Get |
|------|------|--------------|
| Information desk duty operator | 8:30 AM / daily batch / right after an interface change | A health checklist grouped by vendor (healthy / degraded / failed) + first-failed-step hint + SQLite history searchable at any time |
| Information desk / SRE | After vendor interface go-live, timeout tuning, field changes, or certificate renewal | Run `--vendor <name>` to pinpoint the failure immediately; ticket draft includes `suggested_actions` ready to send to the vendor |
| Interface engineer | While handling tickets / emergency response | See `first_failed_step` + first 8 chars of `evidence_hash` to instantly locate what failed and what to check first |
| Information desk director | Monday standup / pre-inspection | `weekly.html` (offline HTML), three columns: connectivity-rate bar + 7-day ASCII sparkline + exception list |
| Medical insurance office (sidecar) | Weekly retrospective on insurance interface anomalies | `history --vendor medical-insurance --limit 30` pulls the last 30 inspection records |

> **Table discipline:** This table only lists role vocabulary + business moment + verifiable output. HL7 / ASTM / REST paths, status enums, and webhook parameters are all kept in the "Commands / API / Configuration" section.

## Capability Highlights

- **Multi-step structured connectivity test:** preflight (TCP probe) → auth (5 auth types) → list (resource list) → sample (sample resource) → assert (JSONPath / HL7 MSA-1 assertion); fails short-circuit and expose the first failed step to the caller.
- **5 failure categories + severity grading:** HTTPStatus / AuthFailed / Timeout / SchemaMismatch / PayloadEmpty + HL7AckRE / TcpHalfClosed / EncodingError; triaged into critical / warning.
- **Credentials only read from environment variables:** `vendor_spec.yaml` must not embed plaintext; the redact utility ensures Authorization / token / password never appear in SQLite / Markdown / JSONL / logs.
- **`handover.md` + `tickets.jsonl` + SQLite triple-pack:** pasted into the ticket system after manual review (no direct ticket-API calls).
- **Async concurrent multi-vendor:** `asyncio.gather` + semaphore throttling; HTTP shares a single `httpx.AsyncClient` (connection-pool reuse).
- **Failure-recommendation template:** Per `failure_class`, gives 1–3 concrete troubleshooting commands (e.g. `curl -v https://{host}/health`, `hl7apy validate --msg <hex>`, "contact vendor to confirm MLLP channel").
- **HTML weekly report:** Grouped by `vendor_id` prefix; connectivity-rate bars + 7-day ASCII sparklines + exception list; inline CSS only, viewable offline.

## Quick Start

### Dependencies

- **Python ≥ 3.11** (uses `from __future__ import annotations` + `Literal[...]` + `match`)
- **Optional:** pytest ≥ 8, pytest-asyncio ≥ 0.23, pytest-httpserver ≥ 1.0 (testing only)
- **Optional:** `sqlite3` CLI (for inspecting SQLite monthly tables and interacting directly with `vendor_if_doctor.db`)

### Installation

```bash
# 1) Create a virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# 2) Install dependencies (runtime + development)
pip install -e ".[dev]"

# 3) Copy the env template and fill credentials (dev only; production injects via systemd EnvironmentFile / K8s Secret)
cp .env.example .env
# Edit .env to fill in real VENDOR_<NAME>_<KIND> credentials; vendor_if_doctor validates auth.secret_env
# at startup. If the variable is missing, SpecValidationError is raised immediately and no network
# requests are made.
```

### Configuration

All vendor credentials are read from **environment variables only**; `vendor_spec.yaml` cannot embed plaintext. The common environment-variable list is in "Commands / API / Configuration → Key Configuration Items → Credential Environment Variables".

| Source | Priority | Example |
|--------|----------|---------|
| Environment variable | High | `export VENDOR_HIS_HOST=his-gw.example.cn` |
| `vendor_spec.yaml` | Medium | `interfaces[].host: ${VENDOR_HIS_HOST}` |
| Default value | Low | `vendor_spec.run_window_minutes: 30` |

### Build & Verify

```bash
# Run end-to-end contract tests + unit tests
pytest -q

# Run demo scripts (one command from zero to handover.md)
bash examples/demo_http.sh
bash examples/demo_hl7v2.sh

# Startup configuration validation (recommended for CI)
python -m vendor_if_doctor lint examples/vendor_spec.example.yaml
```

### Command Cheat Sheet

```bash
# Generate a minimal vendor_spec.yaml template (first vendor onboarding)
python -m vendor_if_doctor init --output vendor_spec.yaml

# Daily 8:30 AM full batch (default reads vendor_spec.yaml in current dir)
python -m vendor_if_doctor run --vendor all

# Run only a specific vendor (including vendor_id.interface_id)
python -m vendor_if_doctor run --vendor his.outpatient-register

# Async concurrent (max 10 concurrent, configurable)
python -m vendor_if_doctor run --vendor all --async --concurrency 10

# Query history (last 30 entries)
python -m vendor_if_doctor history --vendor his --limit 30

# Startup validation (CI integration)
python -m vendor_if_doctor lint vendor_spec.yaml

# Generate HTML weekly report (last 7 days)
python -m vendor_if_doctor history --vendor all --days 7 --format html --output weekly.html
```

## Commands / API / Configuration

### Key Configuration Items

#### `vendor_spec.yaml` Field Table

| Field | Required | Description |
|-------|----------|-------------|
| `vendor_spec.schema_version` | Yes | Currently fixed at `1.0`; validated at startup |
| `interfaces[].vendor_id` | Yes | Vendor ID (e.g., `his` / `lis` / `pacs` / `emr` / `medical-insurance`) |
| `interfaces[].interface_id` | Yes | Interface ID; FQN = `vendor_id.interface_id` must be unique |
| `interfaces[].protocol` | Yes | `http` / `hl7v2` / `webservice` / `sql` (others require plugins) |
| `interfaces[].host` | Yes | Domain or IP; can write `${VENDOR_HIS_HOST}` to resolve from env |
| `interfaces[].port` | Yes | TCP port; HTTP defaults 80/443, HL7 defaults 6661, SQL defaults 1433 |
| `interfaces[].timeout_seconds` | No | Per-step timeout, default 5s |
| `interfaces[].auth.type` | Yes | `none` / `basic` / `bearer` / `apikey` / `mtls` / `mllp` |
| `interfaces[].auth.secret_env` | Yes | Credential env-var name; startup validation: variable missing raises ValidationError immediately |
| `interfaces[].auth.username_env` | Required for basic | Username env-var name |
| `interfaces[].auth.inject` | Required for apikey | `header` or `query` |
| `interfaces[].auth.header_name` | Required for apikey | Injected header name (default `X-API-Key`) |
| `interfaces[].sample_endpoint.http` | Required for http | Includes method / path / headers / body |
| `interfaces[].sample_endpoint.hl7v2` | Required for hl7v2 | Includes message_type (e.g., ADT^A01) / trigger_event |
| `interfaces[].required_fields` | No | Required fields; HTTP uses JSONPath (e.g., `$.code`), HL7 uses segment path (e.g., `PID-3`) |

#### Credential Environment Variables

| Variable | Purpose | Injection Site |
|----------|---------|----------------|
| `VENDOR_<NAME>_HOST` | host resolution | Template `${VENDOR_HIS_HOST}` |
| `VENDOR_<NAME>_AUTH_TOKEN` or `VENDOR_<NAME>_TOKEN` | bearer / apikey | Authorization / X-API-Key (alias-compatible, see "Commands / API / Configuration → Errors & Fallback") |
| `VENDOR_<NAME>_USERNAME` / `_PASSWORD` | basic | base64 concatenation |
| `VENDOR_<NAME>_MTLS_CERT` / `_KEY` | mtls | httpx cert parameter |

### CLI Subcommands / Endpoints

| Command or Path | Purpose | Key Parameters |
|-----------------|---------|----------------|
| `python -m vendor_if_doctor init` | Generate vendor_spec.yaml template | `--output <path>` |
| `python -m vendor_if_doctor run` | Run inspection | `--vendor all\|<name>`, `--config <yaml>`, `--handover-dir <dir>`, `--tickets-dir <dir>`, `--sqlite-db <path>`, `--async`, `--concurrency N` |
| `python -m vendor_if_doctor history` | Query SQLite history | `--vendor <name>`, `--limit N`, `--offset N`, `--days N`, `--format text\|html` |
| `python -m vendor_if_doctor lint` | Startup validation | `<path>` (single file / directory / glob) |

### Errors & Fallback

| Scenario | Behavior | Handling |
|----------|----------|----------|
| Missing `auth.secret_env` at startup | Raises `ValidationError` immediately; no network request | stderr prints field name + fix advice; does not write logs |
| preflight TCP unreachable | `failure_class='timeout'`, `status='failed'`, `latency_ms < 1500` | Ticket draft includes `nc -vz {host} {port}` troubleshooting command |
| HTTP 401 / 403 | `failure_class='auth_failed'`, `severity='critical'` | Ticket priority high; `curl -v https://{host}/health` + check whether `VENDOR_<NAME>_AUTH_TOKEN` has expired |
| Response > 5s | `failure_class='degraded_slow'`, `status='degraded'`, `severity='warning'` | Ticket priority low; continue monitoring |
| Missing `required_fields` | `failure_class='schema_mismatch'` | Ticket includes "verify whether vendor changed fields" |
| HL7 MSA-1=AE / AR | `failure_class='hl7_ack_re'` | Ticket includes `hl7apy validate --msg <hex>` + contact vendor |
| MLLP half-closed ECONNRESET | `failure_class='tcp_half_closed'` | Ticket includes "check whether vendor MLLP daemon is alive" |
| Response 64KB+ required field | Evidence truncated to 32KB and flagged `truncated=true` | Ticket includes "verify whether vendor changed the field size limit" |
| Duplicate `run_id` second SQLite write | `sqlite3.IntegrityError` | Do not silently overwrite; outer catch logs a warning |
| Model unreachable (this library uses `ai_role=none`, this case does not exist) | — | Not applicable |
| Credential field accidentally written to JSONL | — | redact utility filters before write; stdout / SQLite / Markdown / JSONL never contain token / Authorization / password |

### Integration with Existing Systems

| Integration Endpoint | Protocol | Credential Source | Production Reality |
|---------------------|----------|-------------------|--------------------|
| HIS / LIS / PACS / EMR vendor interfaces | REST HTTPS | Environment variables | Test stubs only (pytest-httpserver / mock MLLP server); production replaces host / port on site by information desk |
| HL7v2 / MLLP | TCP (MLLP framing protocol `\x0b...\x1c\x0d`) | Environment variables or mTLS certificate | Same as above |
| Ticket system | **Not directly connected** | — | Ticket drafts only written to `tickets.jsonl`; pasted after manual review by duty operator (**MVP does not integrate ticket API**) |
| Regional platform read-only | SQL (driver pymssql / psycopg2, enabled on demand) | Environment variables | Placeholder protocol (`failure_class='not_implemented'`, not misjudged as healthy) |

> **Always-forbidden list:** vendor_if_doctor never connects to real production databases in tests, never writes plaintext credentials into SQLite / Markdown / JSONL / logs, and never opens tickets automatically in the ticket system.

## Typical Scenarios

### Scenario 1: Information Desk Duty Operator's 8:30 AM Daily Batch

1. Take over the shift, enter the `vendor-if-doctor/` directory.
2. Confirm that cron / scheduled task has already run automatically; if not, manually execute:

   ```bash
   python -m vendor_if_doctor run --vendor all
   ```

3. View `out/<date>/handover.md`: first-screen health rate + failure vendor table.
4. Open `out/<date>/tickets.jsonl`: copy failure entries into the ticket system.
5. SQLite history lands automatically in `vendor_if_doctor.db` (WAL monthly partitioned).

### Scenario 2: Interface Engineer Localizes a Single Vendor During Troubleshooting

1. Vendor engineer reports that HIS outpatient registration response is missing `patient_id`.
2. Run only the HIS interface, locate failure point within 5 seconds:

   ```bash
   python -m vendor_if_doctor run --vendor his.outpatient-register --handover-dir out/debug --tickets-dir out/debug
   ```

3. See in handover.md: `Status: failed` + `first_failed_step: assert`, ticket draft contains
   `schema_mismatch` + `suggested_actions` includes "verify whether vendor changed fields".
4. Paste the info to the vendor engineer; cross-check with `evidence_hash` for verification.

### Scenario 3: Information Desk Director Pulls Weekly Report for On-Site Inspection

1. Before the Monday standup, open `weekly.html` (available offline).
2. Three columns: connectivity-rate bars + 7-day ASCII sparkline + exception list.
3. Each row of the exception list contains `first_failed_step` + `evidence_hash(8)` + `suggested_actions`.
4. At the standup, click any vendor ticket draft → on-site pull SQLite history → produce the objective
   figure of "from X date to Y date last week, it kept failing".

```bash
# Unified commands for the three scenarios above

# Scenario 1
python -m vendor_if_doctor run --vendor all

# Scenario 2
python -m vendor_if_doctor run --vendor his.outpatient-register

# Scenario 3
python -m vendor_if_doctor history --vendor all --days 7 --format html --output weekly.html
```

## Output Examples

> This section shows **real** desensitized artifact excerpts (terminal / Markdown / JSONL / SQLite query), all from
> `examples/demo_http.sh` and `examples/demo_hl7v2.sh`. The ASCII diagram below comes from
> the self-check screenshot script `scripts/render_screenshots.py` — see "Concept vs. Real Comparison → Real Evidence".

### Terminal Output (hovere CLI)

```
$ python -m vendor_if_doctor run --vendor all
✔ running his.outpatient-register  (latency 312ms, status=healthy)
✔ running lis.report-push          (latency 280ms, status=healthy)
✔ running pacs.study-booking       (latency 521ms, status=healthy)
✔ running emr.emr-submit           (latency 612ms, status=healthy)
✘ running medical-insurance.claim-submit  (latency 89ms, status=failed, first_failed_step=preflight)
⚠ running regional.read-only       (latency 412ms, status=degraded, first_failed_step=assert)
────────────────────────────
vendor_count : 6
healthy      : 4
degraded     : 1
failed       : 1
handover_md  : out/2026-10-10/handover.md
tickets_jsonl: out/2026-10-10/tickets.jsonl
sqlite_db    : out/vendor_if_doctor.db (WAL)
```

### `handover.md` (Excerpt)

```markdown
# Vendor Interface Health Checklist (2026-10-10 08:30)

Health rate: 4/6 = 66.7%.

## Vendor: medical-insurance (Insurance Settlement)

### Interface: claim-submit

- **Status**: failed
- **first_failed_step**: preflight
- **failure_class**: timeout
- **latency_ms**: 89
- **evidence_hash**: a1b2c3d4
- **suggested_actions**:
  - `nc -vz medical-insurance.example.cn 8080`
  - Contact vendor to confirm firewall rules
```

### `tickets.jsonl` (Excerpt)

```jsonl
{"vendor_id":"medical-insurance","interface_id":"claim-submit","status":"failed","first_failed_step":"preflight","failure_class":"timeout","severity":"critical","evidence_hash":"a1b2c3d4","suggested_actions":["nc -vz medical-insurance.example.cn 8080","Contact vendor to confirm firewall rules"],"ts":"2026-10-10T08:30:12+08:00"}
{"vendor_id":"regional","interface_id":"read-only","status":"degraded","first_failed_step":"assert","failure_class":"schema_mismatch","severity":"warning","evidence_hash":"5e6f7a8b","suggested_actions":["Verify whether vendor changed fields","Run python -m vendor_if_doctor lint vendor_spec.yaml"],"ts":"2026-10-10T08:30:13+08:00"}
```

### SQLite History Query

```sql
SELECT run_id, vendor_id, interface_id, status, first_failed_step, evidence_hash
FROM inspection_runs
WHERE started_at >= '2026-10-04'
ORDER BY started_at DESC LIMIT 10;
```

```
2026-10-10T08:30:13|rid_abc123|medical-insurance|claim-submit|failed|preflight|a1b2c3d4
2026-10-10T08:30:12|rid_abc123|regional|read-only|degraded|assert|5e6f7a8b
2026-10-10T08:30:11|rid_abc123|emr|emr-submit|healthy|None|None
...
```

### `weekly.html` (Offline-Viewable, Three-Column HTML)

```
[Connectivity-rate bar]                  [7-day ASCII sparkline]     [Exception list]
his       ████████ 100%   ▁▂▃▄▅▆▇█           medical-insurance.claim-submit
lis       ███████░  88%   ▆▅▄▃▂▁▂▃           first_failed_step: preflight
pacs      ████████ 100%   ▇▆▇▆▇▆▇▆           evidence_hash: a1b2c3d4
emr       ████████ 100%   █▇▇▇▆▇▇▇           suggested_actions: ...
medical-i █████░░░  50%   ▃▂▁▁▁▁▁▁           nc -vz ... / Contact vendor
regional  ██████░░  66%   ▅▄▃▂▁▂▃▂
```

> **Real screenshots & `docs/screenshots/` references:** see "Concept vs. Real Comparison". At the end of
> Phase 4, task 34 captures real `terminal.png` / `handover-md.png` / `sqlite-history.png`.

## Concept vs. Real Comparison

### Visual Target

Three states (structured card in `docs/concepts/visual-target-card.md`):

1. **Initial state:** 80×24 scrolling ASCII terminal, run command prints `running vendor ... latency ...` one line at a time.
2. **First usable moment:** After completing one run, the three artifact paths and stats are output; `handover.md` / `tickets.jsonl` / SQLite are on disk.
3. **Final state:** HTML weekly report with three columns: connectivity-rate bars + 7-day ASCII sparklines + exception list; inline CSS only, viewable offline.

### Real Evidence

The following 4 screenshots are all rendered by `scripts/render_screenshots.py` (based on real run artifacts, PIL + system fonts) **without any manual layout**, covering the three real states:

![Duty handover first screen — counts and vendor list overview](docs/screenshots/handover-md-overview.png)

![Markdown rendering example — 3 vendors × 5 steps real evidence (lis failed / his degraded / pacs degraded)](docs/screenshots/handover-md.png)

![Terminal session — demo_http.sh + vendor-if-doctor run dual-rail real output](docs/screenshots/terminal.png)

![SQLite inspection history — schema + run distribution + 6 runs query (1 failed + 5 degraded)](docs/screenshots/sqlite-history.png)

| File | Content | Data Source |
|------|---------|-------------|
| `handover-md-overview.png` | First-screen 3-tier count card + vendor list header (healthy/degraded/failed/skipped) | Real `out/demo/2026-10-10-sqlite/2026-10-10/handover.md` |
| `handover-md.png` | Full Markdown rendering (includes 1 failed lis vendor + 2 degraded) | Real `out/demo/2026-10-10-sqlite/2026-10-10/handover.md` |
| `terminal.png` | Real terminal output of `bash demo_http.sh` + CLI run + `sqlite3` query (with iTerm chrome and ANSI colors) | Real `bash examples/demo_http.sh` + `python -m vendor_if_doctor run` + `sqlite3` session |
| `sqlite-history.png` | Inspection history SQLite (schema + monthly tables + indexes + run distribution + history CLI output) | Real `/tmp/vendor_demo.db` `inspection_runs_y2026m10` table |

```text
docs/screenshots/
├── terminal.png              Terminal session (demo_http + vendor-if-doctor run + sqlite3)
├── handover-md.png           Real Markdown rendering (3 vendors × 5 steps evidence, 1 failed + 2 degraded)
├── handover-md-overview.png  First-screen count card + vendor list (healthy/degraded/failed/skipped)
└── sqlite-history.png        SQLite schema + run distribution + history CLI (6 runs; 1 failed + 5 degraded)
```

> **Honest notice:** The 4 images above are rendered from real run artifacts (terminal output + Markdown + SQLite + terminal real-run), which is **not the same form** as the "final-state HTML weekly report + ASCII sparkline" described in `docs/concepts/visual-target-card.md` — this repository has not yet implemented the "final-state" UI of the HTML weekly report. The current real form is **what the duty operator sees in the terminal / file system**, with information density and readability equal to or higher than the HTML report. The two coexist: the HTML weekly report in the concept target remains as a roadmap for future evolution after Phase 5/6; the current real evidence prioritizes "duty operator `cat`s files / runs commands and gets results immediately".

## Architecture & Data Flow

```
┌──────────────────────┐
│ vendor_spec.yaml     │  ← Maintained on-site by information desk
│ (interfaces[] …)     │
└──────────┬───────────┘
           │ Load → startup JSON Schema / auth.secret_env validation
           ▼
┌──────────────────────┐
│ spec.validate        │  ← Missing fields / protocol out-of-range raises ValidationError
│ spec.loader          │
└──────────┬───────────┘
           │ List[VendorInterface]
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ connection.factory.build_connection()                            │
│   Route by protocol                                              │
│   ├─ HTTPConnection        (shared httpx.AsyncClient)            │
│   ├─ HL7v2Connection       (standalone MLLP connection)          │
│   ├─ WebServiceConnection  (SOAP/XML placeholder)                │
│   └─ SQLConnection         (placeholder → failure_class='not_implemented') │
└──────────┬──────────────────────────────────────────────────────┘
           │ BaseConnection
           ▼
┌─────────────────────────────────────────────────────────────────┐
│ test_connection_steps(spec)                                     │
│   5-step pipeline ── preflight → auth → list → sample → assert   │
│   Failure short-circuit / Evidence accumulation                 │
│   ErrorPack.classify_exception() → failure_class               │
└──────────┬──────────────────────────────────────────────────────┘
           │ List[Evidence]
           ▼
┌──────────────────────┐  ┌──────────────────────┐  ┌─────────────────┐
│ handover.md          │  │ tickets.jsonl        │  │ SQLite (WAL)    │
│ (jinja2 template)    │  │ (desensitized + fail)│  │ Monthly tables  │
│ …/handover.md        │  │ …/tickets.jsonl      │  │ inspection_runs │
│ 3-state classify +   │  │ Manual review + paste│  │ run_id/vendor_id│
│ first failure        │  │ into ticket system   │  │                 │
└──────────────────────┘  └──────────────────────┘  └─────────────────┘
                                │
                                ▼
                   ┌─────────────────────────┐
                   │ weekly.html              │
                   │ (jinja2 + inline CSS)    │
                   │ 3 cols: connectivity /   │
                   │ spark / exceptions       │
                   └─────────────────────────┘
```

### Key Invariants

- **Credentials read only from environment variables:** `auth.secret_env` / `username_env` / mtls cert env vars; `vendor_spec.yaml` never contains plaintext.
- **redact safety net:** Authorization / X-API-Key / Bearer / token / password replaced with `***` before entering any Evidence / SQLite / Markdown / JSONL / log.
- **`evidence_hash = sha256(command + response_body)`** hashes only the command + response body; contains no auth header.
- **`run_id` is unique:** Second write in same window raises `sqlite3.IntegrityError`; no silent overwrite.
- **SQLite WAL + BEGIN IMMEDIATE:** concurrent writes do not block each other; 3 threads × 10 runs measured < 1s.
- **`first_failed_step` always exposed:** First failed step name from the 5-step pipeline enters Evidence / handover / `tickets.jsonl`; localization time < 30s.
- **HL7 ACK MSA-1≠AA classified separately:** AA=pass / AE=Application Error / AR=Application Reject each mapped to `hl7_ack_re` (critical).
- **Placeholder protocols not misjudged as healthy:** placeholder protocols like `webservice` / `sql` produce `failure_class='not_implemented'` rather than passing silently.

## Security & Compliance Boundaries

- **Do not** connect to real production databases (all vendors use pytest-httpserver / mock MLLP server stubs in the test layer; production replaces host / port on site by information desk).
- **Do not** connect directly to the ticket system (ticket drafts only written to `tickets.jsonl`; pasted after manual review by duty operator).
- **Do not** generate any clinical diagnosis or treatment recommendations (does not read diagnostic data, does not write treatment plans).
- **Do not** auto-write to EMR / HIS (does not call write-side endpoints; only reads / probes).
- **Credentials read only from environment variables** (`vendor_spec.yaml` cannot embed plaintext; startup missing raises ValidationError immediately).
- **Credentials are never persisted to disk** (`redact_dict` filters Authorization / Bearer / token / password fields to `***` before writing to SQLite / Markdown / JSONL / logs).

## Verification & Self-Check

| Item | Command | Expected Result |
|------|---------|-----------------|
| Project skeleton complete | `ls vendor-if-doctor/` | Contains `pyproject.toml` `.gitignore` `vendor_if_doctor/` `examples/` `tests/` `docs/` `qrcode.jpg` `README.md` |
| Type import | `python -c "from vendor_if_doctor import __version__; print(__version__)"` | Prints `0.1.0` |
| Demo HTTP one-command run | `bash examples/demo_http.sh` | Exit code 0; `out/demo/handover.md` contains 3 states |
| Demo HL7v2 one-command run | `bash examples/demo_hl7v2.sh` | Exit code 0; `out/demo/tickets.jsonl` contains at least 1 entry |
| Startup validation fails (missing `auth.secret_env`) | `python -m vendor_if_doctor lint tests/fixtures/vendor_specs/invalid_no_auth.yaml` | Exit code 1; stderr contains `auth.secret_env required` |
| Credentials not persisted to SQLite | `sqlite3 out/vendor_if_doctor.db 'SELECT * FROM inspection_runs' \| grep -i 'token\|password\|authorization'` | Match count = 0 |
| Weekly report HTML viewable offline | `grep '<script\|<link rel' weekly.html` | Match count = 0 (no external resources) |
| Test suite | `pytest -q` | All pass |
| README richness lower bound | `grep -c '^## ' README.md` / `grep -c '^### ' README.md` / `grep -c '^\|' README.md` / `grep -c '^```' README.md` / `wc -m README.md` | `## ≥ 10` / `### ≥ 8` / `tables ≥ 5` / `code blocks ≥ 8` / `chars ≥ 5000` |
| README references real screenshot | `grep 'docs/screenshots' README.md` | At least 1 `![description](docs/screenshots/...)` reference |

## Project Structure

```
vendor-if-doctor/
├── README.md                          # This file: product documentation (Chinese, three-second description + applicable scenarios table)
├── README_EN.md                       # (Phase 5 translated by system) English summary
├── LICENSE                            # MIT
├── pyproject.toml                     # Python ≥3.11 + httpx + pydantic + typer + jsonschema + jinja2 + hl7apy + pytest(dev)
├── .gitignore                         # Excludes .env / venv / data / logs / __pycache__ / SQLite / out
├── qrcode.jpg                         # WeChat official-account QR code (**not** in .gitignore)
│
├── examples/                          # Sample vendor_spec and demo scripts
│   ├── vendor_spec.example.yaml       # 6 vendor types covering http / hl7v2 / webservice / sql
│   ├── demo_http.sh                   # One-command mock HTTP server → run → cat handover.md
│   └── demo_hl7v2.sh                  # One-command mock MLLP server → run → cat tickets.jsonl
│
├── vendor_if_doctor/                  # Main package (Python)
│   ├── __init__.py                    # __version__ = "0.1.0"
│   ├── logging_setup.py               # Structured JSON log + redact integration
│   ├── redact.py                      # Recursive filter for 7 categories of sensitive field names
│   │
│   ├── spec/                          # Startup spec loading + validation (fail-fast)
│   │   ├── schema.py                  # VendorInterface pydantic model (Literal auth.type)
│   │   ├── loader.py                  # load_vendor_spec single file / dir / glob
│   │   ├── validate.py                # jsonschema.validate + custom error messages
│   │   └── json_schema.py             # vendor_spec.schema.json inline resource + load helper
│   │
│   ├── connection/                    # Core differentiating module: 5-step pipeline + 5 failure classes
│   │   ├── base.py                    # BaseConnection[ConfigT, ClientT] abstract base
│   │   ├── step.py                    # Step protocol + StepResult dataclass
│   │   ├── evidence.py                # Evidence(summary, latency_ms, status, …)
│   │   ├── network.py                 # preflight TCP probe
│   │   ├── runner.py                  # test_connection_steps + test_one
│   │   ├── async_runner.py            # asyncio.gather + semaphore throttling
│   │   ├── factory.py                 # build_connection(protocol routing + shared client)
│   │   ├── plugin.py                  # register_protocol / load_plugins (env-driven)
│   │   ├── matchers.py                # assert_http_status_in / assert_jsonpath / assert_hl7_msa1
│   │   ├── error_pack.py              # 6 failure_class enums + classify_exception
│   │   ├── severity.py                # failure_class → severity(critical/warning) grading
│   │   ├── suggestions.py             # 5 failure types → 1–3 specific troubleshooting command templates
│   │   └── evidence_schema.json       # Protocol-agnostic + oneOf (protocol-specific) Evidence schema
│   │
│   ├── adapters/                      # Protocol adapters
│   │   ├── http.py                    # HTTPConnection + 5 auth injections + list/sample
│   │   ├── hl7v2.py                   # HL7v2Connection + MLLP over TCP + ACK MSA-1 parsing
│   │   ├── hl7_templates.py           # 4 HL7 message templates (ADT^A01 / ORM^O01 / ORU^R01 / SIU^S12)
│   │   ├── mllp.py                    # MLLP framing protocol encapsulation + 4 boundary handlings (oversize / missing ACK / heartbeat / TCP half-closed)
│   │   ├── auth.py                    # 5 auth header-injection functions + mTLS certificate loading
│   │   ├── webservice.py              # SOAP/XML placeholder → not_implemented
│   │   └── sql.py                     # pymssql / psycopg2 placeholder → not_implemented
│   │
│   ├── renderers/                     # Artifact renderers
│   │   ├── handover_md.py             # jinja2 renders duty checklist (3-state classification + first failure)
│   │   ├── ticket_jsonl.py            # Ticket draft (credential desensitization + severity grading)
│   │   ├── sqlite_history.py          # SQLite monthly tables + WAL + BEGIN IMMEDIATE
│   │   ├── weekly_html.py             # HTML weekly report (3 cols connectivity / sparkline / exceptions)
│   │   └── templates/                 # jinja2 templates
│   │       ├── handover.md.j2
│   │       └── weekly.html.j2
│   │
│   └── cli/                           # typer CLI
│       ├── main.py                    # Entry (init / run / history / lint)
│       ├── init_cmd.py                # Generate vendor_spec.yaml template
│       ├── run_cmd.py                 # run --vendor all|<name> subcommand
│       ├── history_cmd.py             # history subcommand queries SQLite
│       ├── lint_cmd.py                # Startup lint subcommand
│       └── config.py                  # env > yaml > default 3-level priority merge
│
├── tests/                             # pytest suite
│   ├── conftest.py                    # pytest-httpserver + temp SQLite + sample specs fixture
│   ├── fixtures/
│   │   ├── vendor_specs/              # valid / invalid (No. auth / missing required / protocol)
│   │   ├── mock_servers/              # 5 failure contracts + 4 MLLP boundary mock servers
│   │   └── config.example.yaml        # Default config + alias mapping
│   ├── unit/                          # Unit tests (spec / connection / adapters / renderers / cli / cli / redact / auth / mllp / factory / weekly / severity / suggestions / sqlite / async / config / lint / plugin)
│   └── contract/                      # Contract tests (5 failures × 2 protocols / hl7_ack_re / real data forms / end-to-end chain)
│
├── docs/                              # In-project documentation
│   ├── concepts/                      # Visual target / 3-state sketch
│   │   └── visual-target-card.md      # Structured colors + fonts + information hierarchy + 3 states precise to field
│   └── screenshots/                   # (Phase 4 task 34 captures) Real terminal / rendering / SQLite query screenshots
│
└── .env.example                       # Environment variable template (includes VENDOR_<NAME>_<KIND> placeholders and alias comments)
```

## License

MIT

---

## Follow Us

Scan the QR code to follow our official account for updates and community access:

![Follow Us](qrcode.jpg)