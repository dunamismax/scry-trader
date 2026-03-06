"""Tests for Pydantic models."""

import pytest

from augur.models import (
    AccountSummary,
    ConvictionLevel,
    Direction,
    OrderAction,
    OrderSpec,
    OrderType,
    Position,
    RiskLevel,
    TimeInForce,
    TradeAnalysis,
    TradeJournalEntry,
    TradeOutcome,
)


class TestPosition:
    def test_pnl_percent_positive(self) -> None:
        pos = Position(
            symbol="AAPL",
            quantity=100,
            avg_cost=100.0,
            unrealized_pnl=500.0,
        )
        assert pos.pnl_percent == 5.0

    def test_pnl_percent_negative(self) -> None:
        pos = Position(
            symbol="AAPL",
            quantity=100,
            avg_cost=100.0,
            unrealized_pnl=-300.0,
        )
        assert pos.pnl_percent == -3.0

    def test_pnl_percent_zero_cost(self) -> None:
        pos = Position(symbol="AAPL", quantity=0, avg_cost=0.0)
        assert pos.pnl_percent == 0.0


class TestOrderSpec:
    def test_basic_order(self) -> None:
        order = OrderSpec(
            symbol=" aapl ",
            action=OrderAction.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
            stop_loss_price=140.0,
        )
        assert order.symbol == "AAPL"
        assert order.action == OrderAction.BUY
        assert order.limit_price == 150.0

    def test_bracket_order(self) -> None:
        order = OrderSpec(
            symbol="MSFT",
            action=OrderAction.BUY,
            quantity=50,
            order_type=OrderType.LIMIT,
            limit_price=300.0,
            take_profit_price=330.0,
            stop_loss_price=285.0,
        )
        assert order.take_profit_price == 330.0
        assert order.stop_loss_price == 285.0

    def test_invalid_negative_quantity_fails(self) -> None:
        with pytest.raises(ValueError, match="greater than 0"):
            OrderSpec(
                symbol="AAPL",
                action=OrderAction.BUY,
                quantity=-1,
                order_type=OrderType.MARKET,
                reference_price=150.0,
                stop_loss_price=140.0,
            )

    def test_inverted_long_bracket_fails(self) -> None:
        with pytest.raises(ValueError, match="take_profit_price"):
            OrderSpec(
                symbol="AAPL",
                action=OrderAction.BUY,
                quantity=10,
                order_type=OrderType.LIMIT,
                limit_price=150.0,
                take_profit_price=145.0,
                stop_loss_price=140.0,
            )

    def test_trailing_stop_requires_trailing_percent(self) -> None:
        with pytest.raises(ValueError, match="trailing_percent"):
            OrderSpec(
                symbol="AAPL",
                action=OrderAction.SELL,
                quantity=10,
                order_type=OrderType.TRAILING_STOP,
            )

    def test_time_in_force_is_validated(self) -> None:
        order = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
            stop_loss_price=140.0,
            time_in_force=TimeInForce.GTC,
        )
        assert order.time_in_force == TimeInForce.GTC


class TestTradeJournalEntry:
    def test_open_trade_defaults_open_shares_to_shares(self) -> None:
        entry = TradeJournalEntry(
            ticker="aapl",
            direction=Direction.LONG,
            entry_price=100.0,
            shares=5,
            outcome=TradeOutcome.OPEN,
        )
        assert entry.ticker == "AAPL"
        assert entry.open_shares == 5

    def test_closed_trade_forces_open_shares_zero(self) -> None:
        entry = TradeJournalEntry(
            ticker="TSLA",
            direction=Direction.SHORT,
            entry_price=200.0,
            exit_price=180.0,
            shares=5,
            outcome=TradeOutcome.WIN,
            open_shares=1,
        )
        assert entry.open_shares == 0.0


class TestTradeAnalysis:
    def test_from_dict(self) -> None:
        analysis = TradeAnalysis(
            symbol="XLE",
            direction="long",  # type: ignore[arg-type]
            conviction="high",  # type: ignore[arg-type]
            risk_level="moderate",  # type: ignore[arg-type]
            bull_case="Oil supply disruption",
            bear_case="Ceasefire negotiations",
            reasoning="Geopolitical risk premium in energy",
            entry_price=85.0,
            target_price=95.0,
            stop_loss_price=80.0,
        )
        assert analysis.symbol == "XLE"
        assert analysis.direction == Direction.LONG
        assert analysis.conviction == ConvictionLevel.HIGH
        assert analysis.risk_level == RiskLevel.MODERATE


class TestAccountSummary:
    def test_defaults(self) -> None:
        summary = AccountSummary()
        assert summary.total_value == 0.0
        assert summary.positions == []
