# augur — Build Tracker

**Status:** Phase 1 — IBKR Connection & Market Data
**Last Updated:** 2026-03-04
**Branch:** `main`

---

## What This Repo Is

AI-assisted, human-directed trading system. Claude analyzes market data and positions; the human makes every trade decision. Built on Interactive Brokers (via ib-async) with Anthropic's API for analysis. CLI-first — no web UI.

## Architecture Snapshot

```
augur/
├── src/augur/
│   ├── cli.py              # Click-based CLI entry point
│   ├── config.py           # TOML config loader (Pydantic validated)
│   ├── models.py           # Core domain models
│   ├── broker.py           # IBKR connection via ib-async
│   ├── analyst.py          # Claude-powered market analysis
│   ├── risk.py             # Position sizing and risk management
│   ├── journal.py          # Trade journal / decision log
│   └── prompts/
│       ├── system.py       # System prompt for Claude analyst
│       └── tools.py        # Tool definitions for Claude
├── tests/                  # pytest + pytest-asyncio
├── config.toml             # Runtime configuration
├── data/                   # Local data storage (market data, journals)
└── pyproject.toml          # uv-managed, Python 3.12+
```

**Stack:** Python 3.12+, uv, ib-async, anthropic SDK, Click, Rich, Pydantic, Ruff, mypy.

---

## Phase Plan

### Phase 1 — IBKR Connection & Market Data (Current)

**Goal:** Reliable connection to IBKR Gateway/TWS with live market data streaming and portfolio snapshot.

**Success criteria:** Run `augur status` → see account summary, positions, and P&L. Run `augur watch AAPL` → see live bid/ask/last streaming to terminal.

- [ ] Verify `broker.py` connects to IBKR Gateway (paper account first)
- [ ] Implement `augur status` — account summary, buying power, positions with P&L
- [ ] Implement `augur watch <symbol>` — live streaming quotes (Rich live display)
- [ ] Portfolio snapshot: fetch all positions with cost basis, market value, unrealized P&L
- [ ] Historical data fetch for a given symbol + timeframe
- [ ] Connection resilience: auto-reconnect on disconnect, connection state logging
- [ ] Config validation: ensure `config.toml` has required IBKR connection params
- [ ] Test with IBKR paper trading account
- [ ] Verify: `augur status` returns real data, `augur watch` streams live

### Phase 2 — Claude Analysis Engine

**Goal:** Claude analyzes positions and market conditions, produces actionable insights.

- [ ] `augur analyze <symbol>` — fetch data, send to Claude with market context, display analysis
- [ ] `augur review` — portfolio-wide analysis (risk concentration, sector exposure, opportunities)
- [ ] Prompt engineering: system prompt with trading context, tool use for data retrieval
- [ ] Analysis output: structured (risk score, conviction level, key factors, suggested actions)
- [ ] Rate limiting / cost tracking for Anthropic API calls
- [ ] Journal integration: log every analysis with timestamp and market snapshot

### Phase 3 — Risk Management & Journaling

- [ ] Position sizing calculator (Kelly criterion or fixed-fractional)
- [ ] Risk rules: max position size, max sector concentration, max drawdown alerts
- [ ] Trade journal: record decisions, rationale, entry/exit, outcome
- [ ] `augur journal` — view/search decision history
- [ ] P&L attribution: which analyses led to profitable decisions

### Phase 4 — Alerting & Automation

- [ ] Price alerts (notify via Signal through OpenClaw)
- [ ] Scheduled portfolio reviews (OpenClaw cron → `augur review` → Signal summary)
- [ ] Watchlist management
- [ ] Paper trade execution (human confirms, system submits to paper account)

---

## Verification Snapshot

```
uv run ruff check src/   ✅  (all checks passed)
uv run mypy src/          ✅  (11 source files, no issues)
```

Last verified: 2026-03-04

---

## Agent Instructions

- This is a **Python** project — use `uv` for all package management, never pip directly.
- Run verification with `uv run ruff check src/` and `uv run mypy src/`.
- IBKR connection requires TWS or IB Gateway running — tests should mock `ib-async` connections.
- **Never place real trades** without explicit human confirmation. Paper account only during development.
- Rich library for terminal output — use `rich.console`, `rich.table`, `rich.live` for streaming data.
- `config.toml` holds runtime config — never commit secrets (API keys, account numbers) to it.
- Update this BUILD.md in the same commit as meaningful changes.
