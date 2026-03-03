# Code Review: Augur

**Date:** 2026-03-03
**Scope:** Full repository review — all source files, tests, configuration, dependencies
**Commit:** `002c086` (HEAD of `main`)

---

## Tooling Results

| Tool | Result |
|------|--------|
| `pytest` | **40/40 passed** (0.38s) |
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

Minor style issues:
- `_safe_float` in `broker.py:298` uses `f != f` for NaN check — `math.isnan(f)` is more readable
- `strict=False` in `zip(symbols, tickers, strict=False)` at `broker.py:164` — should be `strict=True` to catch length mismatches between symbols and tickers

---

## 3. Trading Logic Safety

**Verdict: Has a critical gap in market order risk estimation.**

### CRITICAL — Market orders bypass risk checks

`risk.py:163-166`:
```python
def _estimate_order_value(order: OrderSpec) -> float:
    price = order.limit_price or order.stop_price or 0.0
    return abs(order.quantity * price)
```

For `MARKET` orders, both `limit_price` and `stop_price` are `None`. The estimated value is **$0**, which means:
- Position size check passes (0% of portfolio)
- Buying power check passes ($0 < any buying power)
- Leverage check passes (adds $0 to invested)

**Fix:** For market orders, fetch or require a reference price (last quote, or require the caller to supply one). Alternatively, reject market orders without a reference price in the risk check.

### HIGH — Sell incorrectly maps to SHORT direction

`cli.py:402`:
```python
direction=Direction.LONG if action == OrderAction.BUY else Direction.SHORT,
```

Selling an existing long position is not a short trade. The journal will record closing a long as opening a short. This corrupts trade history and statistics.

### MEDIUM — `close_trade` P&L: zero counts as LOSS

`journal.py:112`:
```python
outcome = TradeOutcome.WIN if pnl > 0 else TradeOutcome.LOSS
```

A break-even trade (P&L = $0.00) is recorded as a LOSS. Consider adding `TradeOutcome.BREAKEVEN` or treating zero as a win.

### MEDIUM — Journal entry_price can be None for market orders

`cli.py:404`:
```python
entry_price=order_spec.limit_price,
```

For market orders, `limit_price` is `None`, so the journal entry has no entry price. The subsequent `close_trade` P&L calculation uses `entry_price or 0.0`, which produces wildly wrong results.

---

## 4. Broker Integration (IBKR)

**Verdict: Functional but fragile due to hardcoded sleeps and sync calls.**

### HIGH — `qualifyContracts` called synchronously

`broker.py:134`, `155`, `188`, `209`, `234`:
```python
self.ib.qualifyContracts(contract)
```

This is the synchronous version — it blocks the event loop. Should be:
```python
await self.ib.qualifyContractsAsync(contract)
```

### HIGH — Hardcoded `asyncio.sleep()` for market data

`broker.py:136`:
```python
await asyncio.sleep(2)  # allow data to arrive
```

`broker.py:161`:
```python
await asyncio.sleep(3)  # allow data for all tickers
```

Market data may not arrive within the sleep window (network latency, IBKR throttling), or the sleep may be unnecessarily long. Should use event-based waiting:
```python
await asyncio.wait_for(ticker.updateEvent.wait(), timeout=self.config.timeout)
```

### MEDIUM — Order submission waits only 1 second

`broker.py:215`:
```python
await asyncio.sleep(1)
```

After placing an order, the code waits 1 second then reads the status. If IBKR hasn't acknowledged the order yet, the status will be stale. Should wait for an `orderStatusEvent` or use `ib.waitOnUpdate()`.

### LOW — No reconnection logic

If the IBKR connection drops mid-operation, every method will throw `BrokerError("Not connected")` with no retry. For a CLI tool this is acceptable, but worth noting.

---

## 5. Claude / LLM Integration

**Verdict: Well-structured dual-backend design. Some fragility in CLI backend.**

### MEDIUM — No ANTHROPIC_API_KEY validation

`analyst.py:72-73`:
```python
self._api_client = anthropic.Anthropic()
```

If `ANTHROPIC_API_KEY` is not set, the error will surface as a cryptic SDK exception deep in a request. Should validate at init time when `backend == "api"`.

### MEDIUM — CLI backend passes `--tools ""`

`analyst.py:286-287`:
```python
"--tools",
"",
```

Passing an empty string to `--tools` may have unintended behavior depending on Claude CLI version. If the intent is "no tools", verify this is the correct invocation.

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

**Verdict: Consistent pattern, but missing edge cases.**

Positives:
- Custom exception classes (`BrokerError`, `AnalystError`) with clean propagation
- CLI commands catch domain errors and exit with informative messages
- `try/finally` pattern for broker disconnect ensures cleanup

Gaps:
- **No retry logic anywhere.** A transient network failure kills the entire operation. For API calls and IBKR connections, at least 1 retry with backoff would help.
- **`_row_to_trade` will crash on schema changes.** Uses positional tuple indices (`row[0]`, `row[1]`, etc.) — any column addition or reorder breaks silently. Should use `sqlite3.Row` or a column-name mapping.
- **`close_trade` notes concatenation bug.** `journal.py:118`: `notes = COALESCE(notes || '\n' || ?, notes)` appends even when the new notes parameter is empty, adding trailing newlines.

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

**Verdict: Functional. Some design issues.**

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

**Verdict: Good coverage of core logic. Major gaps in integration layers.**

Covered (40 tests):
- `test_config.py` — config loading, defaults, missing file
- `test_journal.py` — CRUD operations, trade closing, P&L calculation, stats, snapshots, analysis log
- `test_models.py` — Pydantic model construction, enum coercion, computed properties
- `test_risk.py` — all risk rules (position size, leverage, buying power, stop-loss, daily loss, concentration, paper trading)

**Not covered:**
- `analyst.py` — zero tests. Claude integration (both backends) is untested.
- `broker.py` — zero tests. IBKR operations are untested.
- `cli.py` — zero tests. Click commands are untested.
- `prompts/` — no tests that tool schemas are valid or match models.

The analyst and broker modules should have unit tests with mocked dependencies.

---

## 10. Dependencies & Tech Debt

### Unused dependencies

- **`pandas`** — listed in `[project.dependencies]` but never imported in source code.
- **`httpx`** — listed as a direct dependency but only used transitively by `anthropic`. Should be in `anthropic`'s own deps, not a direct dependency.

### Stale venv

The `.venv` had broken shebangs pointing to `/Users/sawyer/github/scry-trader/` (the old repo name). This was fixed by recreating the venv, but it indicates the rename from `scry-trader` to `augur` didn't include a venv cleanup step.

### Missing `config.toml.example`

README references `cp config.toml.example config.toml` but no `.example` file exists. The actual `config.toml` is committed directly.

### `pytest-asyncio` version

Using `pytest-asyncio==1.3.0` which is very old (current is 0.24+). The `asyncio_mode = "auto"` config works but may have compatibility issues with future pytest versions.

---

## Prioritized Improvements

### P0 — Safety Critical (fix before any live trading)

1. **Fix `_estimate_order_value` for market orders** — market orders currently bypass all position-size and buying-power risk checks because estimated value is $0.
2. **Fix sell → SHORT journal mapping** — selling an existing long is not a short trade. Corrupts trade history.
3. **Use `qualifyContractsAsync`** — sync version blocks the event loop.

### P1 — High Value

4. **Replace `asyncio.sleep()` with event-based waiting** — market data and order submission timing is fragile.
5. **Add reference price requirement** — market orders should carry a reference price for risk estimation and journal logging.
6. **Add analyst/broker unit tests** — zero coverage on the two most critical modules.
7. **Validate API key at `Analyst.__init__`** — fail fast when `backend == "api"` and key is missing.

### P2 — Medium Value

8. **Use `sqlite3.Row` in journal** — eliminate fragile positional indexing.
9. **Resolve relative DB path** — anchor to project root or XDG data directory.
10. **Add `config.toml.example`** — gitignore the real `config.toml`, ship an example.
11. **Fix `close_trade` notes concatenation** — avoid empty-string newline append.
12. **Remove unused `pandas` and `httpx` deps** — reduce install footprint.
13. **Add schema migration strategy** — `CREATE TABLE IF NOT EXISTS` won't handle column additions.

### P3 — Low Value / Polish

14. **Add `TradeOutcome.BREAKEVEN`** — zero P&L shouldn't count as a loss.
15. **Use `math.isnan()` instead of `f != f`** — readability.
16. **Use `strict=True` in `zip()`** — catch length mismatches.
17. **Generate tool schemas from Pydantic models** — eliminate drift risk between `prompts/tools.py` and `models.py`.
18. **Add broker context manager** — `async with Broker(config) as broker:` pattern.
19. **Upgrade `pytest-asyncio`** — 1.3.0 is very old.
