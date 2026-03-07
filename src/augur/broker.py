"""IBKR connection manager using ib_async."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING, Any

from ib_async import (
    IB,
    LimitOrder,
    MarketOrder,
    Order,
    Stock,
    StopLimitOrder,
    StopOrder,
    Ticker,
    Trade,
)

from augur.models import (
    AccountSummary,
    OrderAction,
    OrderResult,
    OrderSpec,
    OrderType,
    Position,
    WatchlistItem,
)

if TYPE_CHECKING:
    from augur.config import IBKRConfig

logger = logging.getLogger(__name__)

_DATA_TIMEOUT = 10  # seconds — max wait for market data


class BrokerError(Exception):
    """Raised when a broker operation fails."""


class Broker:
    """Manages connection to Interactive Brokers via ib_async."""

    def __init__(self, config: IBKRConfig) -> None:
        self.config = config
        self.ib = IB()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    async def connect(self) -> None:
        """Connect to IB Gateway or TWS."""
        if self.connected:
            return
        try:
            await self.ib.connectAsync(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.timeout,
            )
            self._connected = True
            logger.info(
                "Connected to IBKR at %s:%d (client %d)",
                self.config.host,
                self.config.port,
                self.config.client_id,
            )
        except Exception as e:
            self._connected = False
            raise BrokerError(f"Failed to connect to IBKR: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from IBKR."""
        if self.connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("Disconnected from IBKR")

    def _require_connection(self) -> None:
        if not self.connected:
            raise BrokerError("Not connected to IBKR. Call connect() first.")

    # --- Portfolio ---

    async def get_positions(self) -> list[Position]:
        """Fetch current positions, preferring marked-to-market portfolio data."""
        self._require_connection()
        portfolio_items = _get_portfolio_items(self.ib, self.config.account)
        if portfolio_items:
            return [_portfolio_item_to_position(item) for item in portfolio_items]

        ib_positions = self.ib.positions(self.config.account)
        positions: list[Position] = []
        for p in ib_positions:
            positions.append(
                Position(
                    symbol=p.contract.symbol,
                    quantity=p.position,
                    avg_cost=p.avgCost,
                    account=p.account,
                )
            )
        return positions

    async def get_account_summary(self) -> AccountSummary:
        """Fetch account summary with positions and values."""
        self._require_connection()
        account_values = await self.ib.accountSummaryAsync(self.config.account)
        if self.config.account and not account_values:
            raise BrokerError(f"IB account '{self.config.account}' was not found")

        summary = AccountSummary()
        for av in account_values:
            tag = av.tag
            val = av.value
            if tag == "NetLiquidation":
                summary.total_value = float(val)
            elif tag == "TotalCashValue":
                summary.cash = float(val)
            elif tag == "BuyingPower":
                summary.buying_power = float(val)
            elif tag == "RealizedPnL":
                summary.realized_pnl = float(val)
            elif tag == "UnrealizedPnL":
                summary.unrealized_pnl = float(val)
            elif tag == "GrossPositionValue":
                summary.margin_used = float(val)

        summary.positions = await self.get_positions()
        return summary

    # --- Market Data ---

    async def get_quote(self, symbol: str) -> WatchlistItem:
        """Get a snapshot quote for a single symbol."""
        self._require_connection()
        contract = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        ticker = (await self.ib.reqTickersAsync(contract))[0]

        last = _safe_float(ticker.last)
        close = _safe_float(ticker.close)
        return WatchlistItem(
            symbol=symbol,
            last_price=last,
            change=last - close if close > 0 else 0.0,
            change_percent=(last - close) / close * 100 if close > 0 else 0.0,
            volume=int(_safe_float(ticker.volume)),
            bid=_safe_float(ticker.bid),
            ask=_safe_float(ticker.ask),
        )

    async def get_quotes(self, symbols: list[str]) -> list[WatchlistItem]:
        """Get snapshot quotes for multiple symbols."""
        self._require_connection()
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        await self.ib.qualifyContractsAsync(*contracts)
        tickers: list[Ticker] = await self.ib.reqTickersAsync(*contracts)

        items: list[WatchlistItem] = []
        for symbol, ticker in zip(symbols, tickers, strict=True):
            last = _safe_float(ticker.last)
            close = _safe_float(ticker.close)
            items.append(
                WatchlistItem(
                    symbol=symbol,
                    last_price=last,
                    change=last - close if close > 0 else 0.0,
                    change_percent=((last - close) / close * 100) if close > 0 else 0.0,
                    volume=int(_safe_float(ticker.volume)),
                    bid=_safe_float(ticker.bid),
                    ask=_safe_float(ticker.ask),
                )
            )
        return items

    async def get_options_chain(
        self, symbol: str, right: str = "", strike: float = 0.0
    ) -> list[dict[str, Any]]:
        """Fetch options chain for a symbol."""
        self._require_connection()
        stock = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(stock)
        chains = await self.ib.reqSecDefOptParamsAsync(
            stock.symbol, "", stock.secType, stock.conId
        )

        results: list[dict[str, Any]] = []
        for chain in chains:
            results.append(
                {
                    "exchange": chain.exchange,
                    "expirations": list(chain.expirations),
                    "strikes": list(chain.strikes),
                }
            )
        return results

    # --- Order Submission ---

    async def submit_order(self, spec: OrderSpec) -> OrderResult:
        """Submit an order to IBKR. Returns the order result."""
        self._require_connection()
        contract = Stock(spec.symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        order = _build_order(spec)
        _apply_order_metadata(order, spec, self.config.account)
        trade: Trade = self.ib.placeOrder(contract, order)
        await _wait_for_trade_update(trade, symbol=spec.symbol)
        return _trade_to_result(trade, spec.symbol, spec.quantity)

    async def submit_bracket_order(self, spec: OrderSpec) -> list[OrderResult]:
        """Submit a bracket order (entry + take-profit + stop-loss)."""
        self._require_connection()
        if not spec.take_profit_price or not spec.stop_loss_price:
            raise BrokerError("Bracket order requires take_profit_price and stop_loss_price")

        contract = Stock(spec.symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        parent = _build_order(spec)
        reverse_action = OrderAction.BUY if spec.action == OrderAction.SELL else OrderAction.SELL
        parent.orderId = self.ib.client.getReqId()
        parent.transmit = False
        _apply_order_metadata(parent, spec, self.config.account)

        take_profit = LimitOrder(
            reverse_action.value,
            spec.quantity,
            spec.take_profit_price,
            orderId=self.ib.client.getReqId(),
            parentId=parent.orderId,
            transmit=False,
        )
        _apply_order_metadata(take_profit, spec, self.config.account)

        stop_loss = StopOrder(
            reverse_action.value,
            spec.quantity,
            spec.stop_loss_price,
            orderId=self.ib.client.getReqId(),
            parentId=parent.orderId,
            transmit=True,
        )
        _apply_order_metadata(stop_loss, spec, self.config.account)

        parent_trade = self.ib.placeOrder(contract, parent)
        take_profit_trade = self.ib.placeOrder(contract, take_profit)
        stop_loss_trade = self.ib.placeOrder(contract, stop_loss)

        await _wait_for_trade_update(parent_trade, symbol=spec.symbol)

        return [
            _trade_to_result(parent_trade, spec.symbol, spec.quantity),
            _trade_to_result(take_profit_trade, spec.symbol, spec.quantity),
            _trade_to_result(stop_loss_trade, spec.symbol, spec.quantity),
        ]

    async def cancel_order(self, order_id: int) -> None:
        """Cancel an open order."""
        self._require_connection()
        for trade in self.ib.openTrades():
            if trade.order.orderId != order_id:
                continue
            if self.config.account and trade.order.account != self.config.account:
                continue
            self.ib.cancelOrder(trade.order)
            return
        raise BrokerError(f"Order {order_id} not found in open orders")


def _build_order(spec: OrderSpec) -> Order:
    """Convert an OrderSpec to an ib_async Order object."""
    action = spec.action.value
    qty = spec.quantity

    if spec.order_type == OrderType.MARKET:
        return MarketOrder(action, qty)
    if spec.order_type == OrderType.LIMIT:
        if spec.limit_price is None:
            raise BrokerError("Limit order requires limit_price")
        return LimitOrder(action, qty, spec.limit_price)
    if spec.order_type == OrderType.STOP:
        if spec.stop_price is None:
            raise BrokerError("Stop order requires stop_price")
        return StopOrder(action, qty, spec.stop_price)
    if spec.order_type == OrderType.STOP_LIMIT:
        if spec.limit_price is None or spec.stop_price is None:
            raise BrokerError("Stop-limit order requires both limit_price and stop_price")
        return StopLimitOrder(action, qty, spec.limit_price, spec.stop_price)
    if spec.order_type == OrderType.TRAILING_STOP:
        if spec.trailing_percent is None:
            raise BrokerError("Trailing-stop order requires trailing_percent")
        order = Order(action=action, totalQuantity=qty, orderType="TRAIL")
        order.trailingPercent = spec.trailing_percent
        if spec.stop_price is not None:
            order.trailStopPrice = spec.stop_price
        return order
    raise BrokerError(f"Unsupported order type: {spec.order_type}")


def _safe_float(value: Any) -> float:
    """Convert ib_async nan/None values to 0.0."""
    if value is None:
        return 0.0
    try:
        f = float(value)
        if math.isnan(f):
            return 0.0
        return f
    except (ValueError, TypeError):
        return 0.0


def _get_portfolio_items(ib: IB, account: str) -> list[Any]:
    """Return portfolio items scoped to the configured account when available."""
    portfolio = getattr(ib, "portfolio", None)
    if portfolio is None:
        return []

    try:
        items = portfolio(account)
    except TypeError:
        items = portfolio(account=account) if account else portfolio()

    scoped_items: list[Any] = []
    for item in items:
        item_account = str(getattr(item, "account", "") or "")
        if account and item_account and item_account != account:
            continue
        scoped_items.append(item)
    return scoped_items


def _portfolio_item_to_position(item: Any) -> Position:
    """Convert an ib_async portfolio item to a Position model."""
    contract = getattr(item, "contract", None)
    avg_cost = getattr(item, "averageCost", getattr(item, "avgCost", 0.0))
    unrealized_pnl = getattr(item, "unrealizedPNL", getattr(item, "unrealizedPnl", 0.0))
    realized_pnl = getattr(item, "realizedPNL", getattr(item, "realizedPnl", 0.0))
    return Position(
        symbol=str(getattr(contract, "symbol", "")),
        quantity=_safe_float(getattr(item, "position", 0.0)),
        avg_cost=_safe_float(avg_cost),
        market_price=_safe_float(getattr(item, "marketPrice", 0.0)),
        market_value=_safe_float(getattr(item, "marketValue", 0.0)),
        unrealized_pnl=_safe_float(unrealized_pnl),
        realized_pnl=_safe_float(realized_pnl),
        account=str(getattr(item, "account", "") or ""),
    )


def _apply_order_metadata(order: Order, spec: OrderSpec, account: str) -> None:
    """Apply cross-cutting metadata to every outbound order."""
    order.tif = spec.time_in_force.value
    if account:
        order.account = account


async def _wait_for_trade_update(trade: Trade, symbol: str) -> None:
    """Wait until a trade has either meaningful status or a terminal outcome."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _DATA_TIMEOUT
    while True:
        if _trade_has_meaningful_update(trade):
            return
        if loop.time() >= deadline:
            logger.warning("Timed out waiting for order status for %s", symbol)
            return
        await asyncio.sleep(0.05)


def _trade_has_meaningful_update(trade: Trade) -> bool:
    status = trade.orderStatus.status
    if trade.filled() > 0:
        return True
    if trade.isDone():
        return True
    return status not in {"", "ApiPending", "PendingSubmit"}


def _trade_to_result(trade: Trade, symbol: str, quantity: float) -> OrderResult:
    return OrderResult(
        order_id=trade.order.orderId,
        symbol=symbol,
        action=OrderAction(trade.order.action),
        quantity=quantity,
        status=trade.orderStatus.status,
        filled_price=(_safe_float(trade.orderStatus.avgFillPrice) or None),
        filled_quantity=_safe_float(trade.filled()),
    )
