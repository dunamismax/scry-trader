"""Tests for broker module — pure functions and unit-level behavior."""

from __future__ import annotations

import pytest

from augur.broker import BrokerError, _build_order, _safe_float
from augur.models import OrderAction, OrderSpec, OrderType


class TestBuildOrder:
    def test_market_order(self) -> None:
        spec = OrderSpec(
            symbol="AAPL", action=OrderAction.BUY, quantity=100, order_type=OrderType.MARKET
        )
        order = _build_order(spec)
        assert order.action == "BUY"
        assert order.totalQuantity == 100

    def test_limit_order(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=50,
            order_type=OrderType.LIMIT,
            limit_price=150.0,
        )
        order = _build_order(spec)
        assert order.action == "BUY"
        assert order.totalQuantity == 50
        assert order.lmtPrice == 150.0

    def test_stop_order(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.SELL,
            quantity=25,
            order_type=OrderType.STOP,
            stop_price=140.0,
        )
        order = _build_order(spec)
        assert order.action == "SELL"
        assert order.auxPrice == 140.0

    def test_stop_limit_order(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.STOP_LIMIT,
            limit_price=155.0,
            stop_price=150.0,
        )
        order = _build_order(spec)
        assert order.lmtPrice == 155.0
        assert order.auxPrice == 150.0

    def test_limit_order_missing_price_raises(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
        )
        with pytest.raises(BrokerError, match="limit_price"):
            _build_order(spec)

    def test_stop_order_missing_price_raises(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.SELL,
            quantity=10,
            order_type=OrderType.STOP,
        )
        with pytest.raises(BrokerError, match="stop_price"):
            _build_order(spec)

    def test_stop_limit_missing_prices_raises(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.STOP_LIMIT,
            limit_price=155.0,
        )
        with pytest.raises(BrokerError, match="both"):
            _build_order(spec)

    def test_unsupported_order_type_raises(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.TRAILING_STOP,
        )
        with pytest.raises(BrokerError, match="Unsupported"):
            _build_order(spec)


class TestSafeFloat:
    def test_none_returns_zero(self) -> None:
        assert _safe_float(None) == 0.0

    def test_nan_returns_zero(self) -> None:
        assert _safe_float(float("nan")) == 0.0

    def test_valid_float(self) -> None:
        assert _safe_float(42.5) == 42.5

    def test_int_converts(self) -> None:
        assert _safe_float(100) == 100.0

    def test_string_number(self) -> None:
        assert _safe_float("3.14") == 3.14

    def test_invalid_string_returns_zero(self) -> None:
        assert _safe_float("not_a_number") == 0.0

    def test_inf_is_preserved(self) -> None:
        assert _safe_float(float("inf")) == float("inf")

    def test_negative(self) -> None:
        assert _safe_float(-7.5) == -7.5


class TestBrokerRequireConnection:
    def test_not_connected_raises(self) -> None:
        from augur.broker import Broker
        from augur.config import IBKRConfig

        config = IBKRConfig()
        broker = Broker(config)
        with pytest.raises(BrokerError, match="Not connected"):
            broker._require_connection()
