# Code Review: Augur

**Date:** 2026-03-03
**Scope:** Full repository review — all source files, tests, configuration, dependencies
**Commit:** `002c086` (HEAD of `main`)

---

## Tooling Results

| Tool | Result |
|------|--------|
| `pytest` | **83/83 passed** (0.22s) |
| `ruff check` | **All checks passed** |
| `mypy --strict` | **No issues found** (11 source files) |

The venv was broken on review start — shebangs pointed to the old `scry-trader` path after the repo rename. Recreated via `uv sync --all-extras` to run tools.

---

## 1. Architecture

**Verdict: Good foundation, well-separated.**

The module layout is clean and intentional:

```
cli.py      → Click commands, display logic, user confirmation
broker.py   → IBKR connection, order submission, market data
analyst.py  → Claude integration (CLI + API backends)
risk.py     → Rule-based risk checks (pre-trade + portfolio health)
journal.py  → SQLite trade logging
models.py   → Pydantic models (orders, positions, analysis)
config.py   → TOML config with Pydantic validation
prompts/    → System prompt + tool schemas for Claude
```

Design principles are solid: human-in-the-loop confirmation before every order, risk checks as hard gates (not warnings), dual Claude backend support (CLI for Max subscription, API for key-based).

**Concern:** No dependency injection or service layer. Each CLI command constructs its own `Broker`, `Analyst`, `RiskManager`, and `Journal`. This is fine for a CLI tool but makes testing and reuse harder as the system grows.

---

## 2. Code Quality

**Verdict: High. Clean, idiomatic Python 3.12+.**

Positives:
- Consistent `from __future__ import annotations` throughout
- `StrEnum` for all enumerations — clean serialization
- PEP 695 type parameter syntax (`_run[T]`)
- `TYPE_CHECKING` guards for import-time-only types
- Strict mypy passing on all 11 source files
- Ruff passing with a sensible rule set (`E`, `F`, `I`, `N`, `UP`, `B`, `SIM`, `TCH`)

~~Minor style issues:~~
- ~~`_safe_float` in `broker.py:298` uses `f != f` for NaN check — `math.isnan(f)` is more readable~~ **RESOLVED**
- ~~`strict=False` in `zip(symbols, tickers, strict=False)` at `broker.py:164` — should be `strict=True` to catch length mismatches between symbols and tickers~~ **RESOLVED**

---

## 3. Trading Logic Safety

~~**Verdict: Has a critical gap in market order risk estimation.**~~
**Verdict: All critical gaps resolved.**

### ~~CRITICAL~~ — Market orders bypass risk checks — **RESOLVED**

Fixed: `_estimate_order_value` now uses `reference_price` as fallback. `check_order` rejects market orders without a `reference_price`. CLI trade flow fetches a live quote to populate `reference_price` before risk checks.

### ~~HIGH~~ — Sell incorrectly maps to SHORT direction — **RESOLVED**

Fixed: `_trade_flow` now checks journal for existing open LONG trades when selling. If found, closes the existing trade via `close_trade` instead of creating a spurious SHORT entry.

### ~~MEDIUM~~ — `close_trade` P&L: zero counts as LOSS — **RESOLVED**

Fixed: Added `TradeOutcome.BREAKEVEN`. P&L == 0 now records as BREAKEVEN instead of LOSS.

### ~~MEDIUM~~ — Journal entry_price can be None for market orders — **RESOLVED**

Fixed: Entry price now falls back to `reference_price` when `limit_price` is None.

---

## 4. Broker Integration (IBKR)

~~**Verdict: Functional but fragile due to hardcoded sleeps and sync calls.**~~
**Verdict: Functional. Sync calls and hardcoded sleeps resolved.**

### ~~HIGH~~ — `qualifyContracts` called synchronously — **RESOLVED**

Fixed: All 5 call sites replaced with `await self.ib.qualifyContractsAsync(...)`.

### ~~HIGH~~ — Hardcoded `asyncio.sleep()` for market data — **RESOLVED**

Fixed: Replaced with `asyncio.wait_for(self.ib.updateEvent.wait(), timeout=_DATA_TIMEOUT)` across all market data and order submission methods.

### ~~MEDIUM~~ — Order submission waits only 1 second — **RESOLVED**

Fixed: Uses event-based waiting with configurable timeout.

### LOW — No reconnection logic

If the IBKR connection drops mid-operation, every method will throw `BrokerError("Not connected")` with no retry. For a CLI tool this is acceptable, but worth noting.

---

## 5. Claude / LLM Integration

~~**Verdict: Well-structured dual-backend design. Some fragility in CLI backend.**~~
**Verdict: Well-structured dual-backend design. Key issues resolved.**

### ~~MEDIUM~~ — No ANTHROPIC_API_KEY validation — **RESOLVED**

Fixed: `Analyst.__init__` now raises `AnalystError` immediately when `backend == "api"` and `ANTHROPIC_API_KEY` is not set.

### ~~MEDIUM~~ — CLI backend passes `--tools ""` — **RESOLVED**

Fixed: Removed the `--tools ""` flag from both `_cli_text` and `_cli_structured`.

### LOW — Conversation history grows unbounded

`analyst.py:61`:
```python
self._conversation: list[dict[str, Any]] = []
```

For the CLI backend, conversation history is manually prepended to every prompt (`_cli_text`). Since a new `Analyst` is created per CLI command, this doesn't matter today, but it's a latent issue if `Analyst` is ever reused.

### LOW — Tool schema / model drift risk

`TRADING_TOOLS` in `prompts/tools.py` must manually stay in sync with Pydantic models in `models.py`. There's no validation that the JSON schemas match the Pydantic field definitions. A change to one without the other will cause silent deserialization failures.

---

## 6. Error Handling

**Verdict: Consistent pattern. Key gaps resolved.**

Positives:
- Custom exception classes (`BrokerError`, `AnalystError`) with clean propagation
- CLI commands catch domain errors and exit with informative messages
- `try/finally` pattern for broker disconnect ensures cleanup

~~Gaps:~~
- **No retry logic anywhere.** A transient network failure kills the entire operation. For API calls and IBKR connections, at least 1 retry with backoff would help.
- ~~**`_row_to_trade` will crash on schema changes.** Uses positional tuple indices (`row[0]`, `row[1]`, etc.) — any column addition or reorder breaks silently. Should use `sqlite3.Row` or a column-name mapping.~~ **RESOLVED** — now uses `sqlite3.Row` with column-name access.
- ~~**`close_trade` notes concatenation bug.** `journal.py:118`: `notes = COALESCE(notes || '\n' || ?, notes)` appends even when the new notes parameter is empty, adding trailing newlines.~~ **RESOLVED** — empty notes no longer appended.

---

## 7. Security & Credential Handling

**Verdict: Reasonable for a local CLI tool.**

Positives:
- `.env` is in `.gitignore`
- `*.db` files are in `.gitignore`
- API key comes from environment variable (not config file)
- `_cli_env()` strips `CLAUDECODE` to prevent nested-session issues
- Config file contains no secrets (IBKR connection params only)

Concerns:
- **`config.toml` has `account = ""`** — if someone fills in their IBKR account ID and commits, that's leaked. The `.gitignore` doesn't cover `config.toml`. A `config.toml.example` + gitignored `config.toml` pattern would be safer.
- **`_cli_env()` passes full environment to subprocess** — only strips `CLAUDECODE`. This leaks all env vars to the `claude` subprocess. For a local tool this is acceptable, but worth noting.

---

## 8. Database / Journal

**Verdict: Functional. Key issues resolved.**

### MEDIUM — New connection per operation

`journal.py:67-68`:
```python
def _connect(self) -> sqlite3.Connection:
    return sqlite3.connect(self.db_path)
```

Every method creates a new connection. For a CLI tool with single operations this is fine, but it means no connection pooling and no WAL mode optimization.

### MEDIUM — Relative database path

`config.toml`:
```toml
path = "data/trades.db"
```

This is relative to CWD, not to the project root. Running `augur portfolio` from a different directory creates the DB in an unexpected location.

### LOW — No migration strategy

The schema is defined as `CREATE TABLE IF NOT EXISTS`. Adding columns later will silently fail (tables already exist). Need a migration approach before shipping.

---

## 9. Testing

~~**Verdict: Good coverage of core logic. Major gaps in integration layers.**~~
**Verdict: Good coverage across all critical modules.**

Covered (83 tests):
- `test_config.py` — config loading, defaults, missing file
- `test_journal.py` — CRUD operations, trade closing, P&L calculation, breakeven, notes handling, stats, snapshots, analysis log
- `test_models.py` — Pydantic model construction, enum coercion, computed properties
- `test_risk.py` — all risk rules (position size, leverage, buying power, stop-loss, daily loss, concentration, paper trading, **market order risk validation**)
- `test_broker.py` — `_build_order` (all order types + error cases), `_safe_float`, connection guard (**NEW**)
- `test_analyst.py` — tool schema lookup, API key validation, context building, CLI JSON unwrapping, CLI subprocess mocking (**NEW**)

**Not covered:**
- `cli.py` — Click commands are untested (integration-level, harder to unit test).
- `prompts/` — no tests that tool schemas are valid or match models.

---

## 10. Dependencies & Tech Debt

### ~~Unused dependencies~~ — **RESOLVED**

- ~~**`pandas`** — listed in `[project.dependencies]` but never imported in source code.~~ **Removed.**
- ~~**`httpx`** — listed as a direct dependency but only used transitively by `anthropic`.~~ **Removed.**

### Stale venv

The `.venv` had broken shebangs pointing to `/Users/sawyer/github/scry-trader/` (the old repo name). This was fixed by recreating the venv, but it indicates the rename from `scry-trader` to `augur` didn't include a venv cleanup step.

### Missing `config.toml.example`

README references `cp config.toml.example config.toml` but no `.example` file exists. The actual `config.toml` is committed directly.

### `pytest-asyncio` version

Using `pytest-asyncio==1.3.0` which is very old (current is 0.24+). The `asyncio_mode = "auto"` config works but may have compatibility issues with future pytest versions.

---

## Prioritized Improvements

### P0 — Safety Critical (fix before any live trading)

1. ~~**Fix `_estimate_order_value` for market orders**~~ — **RESOLVED.** Market orders now require `reference_price`; blocked by risk manager without one.
2. ~~**Fix sell → SHORT journal mapping**~~ — **RESOLVED.** Selling an existing long now closes the trade instead of opening a spurious short.
3. ~~**Use `qualifyContractsAsync`**~~ — **RESOLVED.** All sync calls replaced.

### P1 — High Value

4. ~~**Replace `asyncio.sleep()` with event-based waiting**~~ — **RESOLVED.** Uses `asyncio.wait_for` with timeout.
5. ~~**Add reference price requirement**~~ — **RESOLVED.** `OrderSpec.reference_price` added; CLI fetches quote; risk manager enforces.
6. ~~**Add analyst/broker unit tests**~~ — **RESOLVED.** 21 new tests for `_build_order`, `_safe_float`, `_unwrap_cli_json`, API key validation, CLI subprocess mocking, context building.
7. ~~**Validate API key at `Analyst.__init__`**~~ — **RESOLVED.** Raises `AnalystError` immediately when key is missing.

### P2 — Medium Value

8. ~~**Use `sqlite3.Row` in journal**~~ — **RESOLVED.** Column-name access throughout.
9. **Resolve relative DB path** — anchor to project root or XDG data directory.
10. **Add `config.toml.example`** — gitignore the real `config.toml`, ship an example.
11. ~~**Fix `close_trade` notes concatenation**~~ — **RESOLVED.** Empty notes no longer appended.
12. ~~**Remove unused `pandas` and `httpx` deps**~~ — **RESOLVED.**
13. **Add schema migration strategy** — `CREATE TABLE IF NOT EXISTS` won't handle column additions.

### P3 — Low Value / Polish

14. ~~**Add `TradeOutcome.BREAKEVEN`**~~ — **RESOLVED.**
15. ~~**Use `math.isnan()` instead of `f != f`**~~ — **RESOLVED.**
16. ~~**Use `strict=True` in `zip()`**~~ — **RESOLVED.**
17. **Generate tool schemas from Pydantic models** — eliminate drift risk between `prompts/tools.py` and `models.py`.
18. **Add broker context manager** — `async with Broker(config) as broker:` pattern.
19. **Upgrade `pytest-asyncio`** — 1.3.0 is very old.
