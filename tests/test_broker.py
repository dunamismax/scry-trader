"""Tests for broker module."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from augur.broker import Broker, BrokerError, _build_order, _safe_float
from augur.config import IBKRConfig
from augur.models import OrderAction, OrderSpec, OrderType, TimeInForce


@dataclass
class FakePosition:
    symbol: str
    quantity: float
    avg_cost: float
    account: str

    @property
    def contract(self) -> SimpleNamespace:
        return SimpleNamespace(symbol=self.symbol)

    @property
    def position(self) -> float:
        return self.quantity

    @property
    def avgCost(self) -> float:  # noqa: N802
        return self.avg_cost


@dataclass
class FakeAccountValue:
    tag: str
    value: str


class FakeClient:
    def __init__(self) -> None:
        self._next_id = 1000

    def getReqId(self) -> int:  # noqa: N802
        self._next_id += 1
        return self._next_id


class FakeTrade:
    def __init__(
        self,
        order: Any,
        contract: Any,
        *,
        status: str,
        filled: float = 0.0,
        avg_fill_price: float = 0.0,
    ) -> None:
        self.order = order
        self.contract = contract
        self.orderStatus = SimpleNamespace(status=status, avgFillPrice=avg_fill_price)
        self.fills = []
        if filled > 0:
            self.fills.append(SimpleNamespace(execution=SimpleNamespace(shares=filled)))

    def filled(self) -> float:
        return sum(fill.execution.shares for fill in self.fills)

    def isDone(self) -> bool:  # noqa: N802
        return self.orderStatus.status in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


class FakeIB:
    def __init__(
        self,
        *,
        positions: list[FakePosition] | None = None,
        account_values: list[FakeAccountValue] | None = None,
        trade_outcomes: list[dict[str, float | str]] | None = None,
    ) -> None:
        self.client = FakeClient()
        self._positions = positions or []
        self._account_values = account_values or []
        self._trade_outcomes = trade_outcomes or []
        self.placed_orders: list[Any] = []
        self._open_trades: list[FakeTrade] = []
        self.cancelled_order_id: int | None = None

    def isConnected(self) -> bool:  # noqa: N802
        return True

    async def qualifyContractsAsync(self, *contracts: Any) -> None:  # noqa: N802
        for index, contract in enumerate(contracts, start=1):
            contract.conId = index

    async def accountSummaryAsync(self, account: str = "") -> list[FakeAccountValue]:  # noqa: N802
        if account and not any(value.tag == "NetLiquidation" for value in self._account_values):
            return []
        return list(self._account_values)

    def positions(self, account: str = "") -> list[FakePosition]:
        if not account:
            return list(self._positions)
        return [position for position in self._positions if position.account == account]

    async def reqTickersAsync(self, *contracts: Any) -> list[SimpleNamespace]:  # noqa: N802
        return [
            SimpleNamespace(
                last=101.5,
                close=100.0,
                volume=1_000_000,
                bid=101.25,
                ask=101.75,
                contract=contract,
            )
            for contract in contracts
        ]

    async def reqSecDefOptParamsAsync(  # noqa: N802
        self, symbol: str, exchange: str, sec_type: str, con_id: int
    ) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                exchange="SMART",
                expirations={"20260619"},
                strikes={95.0, 100.0, 105.0},
            )
        ]

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:  # noqa: N802
        if not order.orderId:
            order.orderId = self.client.getReqId()
        outcome = self._trade_outcomes.pop(0) if self._trade_outcomes else {"status": "Submitted"}
        trade = FakeTrade(
            order,
            contract,
            status=str(outcome["status"]),
            filled=float(outcome.get("filled", 0.0)),
            avg_fill_price=float(outcome.get("avg_fill_price", 0.0)),
        )
        self.placed_orders.append(order)
        if not trade.isDone():
            self._open_trades.append(trade)
        return trade

    def openTrades(self) -> list[FakeTrade]:  # noqa: N802
        return list(self._open_trades)

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        self.cancelled_order_id = order.orderId


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
            stop_loss_price=140.0,
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
            stop_loss_price=145.0,
        )
        order = _build_order(spec)
        assert order.lmtPrice == 155.0
        assert order.auxPrice == 150.0

    def test_trailing_stop_order(self) -> None:
        spec = OrderSpec(
            symbol="AAPL",
            action=OrderAction.SELL,
            quantity=10,
            order_type=OrderType.TRAILING_STOP,
            trailing_percent=3.5,
            time_in_force=TimeInForce.GTC,
        )
        order = _build_order(spec)
        assert order.orderType == "TRAIL"
        assert order.trailingPercent == 3.5

    def test_limit_order_missing_price_raises(self) -> None:
        spec = OrderSpec.model_construct(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.LIMIT,
        )
        with pytest.raises(BrokerError, match="limit_price"):
            _build_order(spec)

    def test_stop_order_missing_price_raises(self) -> None:
        spec = OrderSpec.model_construct(
            symbol="AAPL",
            action=OrderAction.SELL,
            quantity=10,
            order_type=OrderType.STOP,
        )
        with pytest.raises(BrokerError, match="stop_price"):
            _build_order(spec)

    def test_stop_limit_missing_prices_raises(self) -> None:
        spec = OrderSpec.model_construct(
            symbol="AAPL",
            action=OrderAction.BUY,
            quantity=10,
            order_type=OrderType.STOP_LIMIT,
            limit_price=155.0,
        )
        with pytest.raises(BrokerError, match="both"):
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
        broker = Broker(IBKRConfig())
        with pytest.raises(BrokerError, match="Not connected"):
            broker._require_connection()


@pytest.mark.asyncio
class TestBrokerAccountAndOrders:
    async def test_get_positions_filters_to_configured_account(self) -> None:
        broker = Broker(IBKRConfig(account="DU123"))
        broker.ib = FakeIB(
            positions=[
                FakePosition("AAPL", 10, 100.0, "DU123"),
                FakePosition("MSFT", 20, 200.0, "DU999"),
            ]
        )
        broker._connected = True

        positions = await broker.get_positions()

        assert [position.symbol for position in positions] == ["AAPL"]
        assert positions[0].account == "DU123"

    async def test_get_account_summary_requires_matching_account(self) -> None:
        broker = Broker(IBKRConfig(account="DU123"))
        broker.ib = FakeIB(account_values=[])
        broker._connected = True

        with pytest.raises(BrokerError, match="DU123"):
            await broker.get_account_summary()

    async def test_submit_order_sets_account_and_tif(self) -> None:
        broker = Broker(IBKRConfig(account="DU123"))
        broker.ib = FakeIB(
            trade_outcomes=[{"status": "Filled", "filled": 5, "avg_fill_price": 101.5}]
        )
        broker._connected = True

        result = await broker.submit_order(
            OrderSpec(
                symbol="AAPL",
                action=OrderAction.BUY,
                quantity=5,
                order_type=OrderType.MARKET,
                reference_price=101.0,
                stop_loss_price=98.0,
                time_in_force=TimeInForce.GTC,
            )
        )

        assert broker.ib.placed_orders[0].account == "DU123"
        assert broker.ib.placed_orders[0].tif == "GTC"
        assert result.filled_quantity == 5
        assert result.filled_price == 101.5

    async def test_submit_bracket_preserves_parent_order_type_and_fill(self) -> None:
        broker = Broker(IBKRConfig(account="DU123"))
        broker.ib = FakeIB(
            trade_outcomes=[
                {"status": "Filled", "filled": 5, "avg_fill_price": 101.5},
                {"status": "Submitted"},
                {"status": "Submitted"},
            ]
        )
        broker._connected = True

        results = await broker.submit_bracket_order(
            OrderSpec(
                symbol="AAPL",
                action=OrderAction.BUY,
                quantity=5,
                order_type=OrderType.MARKET,
                reference_price=101.0,
                take_profit_price=110.0,
                stop_loss_price=96.0,
                time_in_force=TimeInForce.GTC,
            )
        )

        assert type(broker.ib.placed_orders[0]).__name__ == "MarketOrder"
        assert broker.ib.placed_orders[0].account == "DU123"
        assert broker.ib.placed_orders[0].tif == "GTC"
        assert results[0].filled_quantity == 5
        assert results[0].filled_price == 101.5
        assert results[1].action == OrderAction.SELL
        assert results[2].action == OrderAction.SELL

    async def test_cancel_order_uses_configured_account_scope(self) -> None:
        broker = Broker(IBKRConfig(account="DU123"))
        fake_ib = FakeIB()
        foreign_trade = FakeTrade(
            SimpleNamespace(orderId=2001, account="DU999"),
            SimpleNamespace(symbol="AAPL"),
            status="Submitted",
        )
        local_trade = FakeTrade(
            SimpleNamespace(orderId=2002, account="DU123"),
            SimpleNamespace(symbol="AAPL"),
            status="Submitted",
        )
        fake_ib._open_trades = [foreign_trade, local_trade]
        broker.ib = fake_ib
        broker._connected = True

        await broker.cancel_order(2002)

        assert fake_ib.cancelled_order_id == 2002
