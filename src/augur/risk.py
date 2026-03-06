"""Risk management rules and checks — circuit breakers, not suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from augur.models import Direction, OrderAction, OrderType

if TYPE_CHECKING:
    from augur.config import RiskConfig
    from augur.models import AccountSummary, OrderSpec


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.passed


@dataclass(frozen=True)
class OrderExposure:
    """How an order changes exposure relative to the live broker position."""

    current_quantity: float
    reducing_quantity: float
    opening_quantity: float
    post_trade_quantity: float
    reducing_direction: Direction | None
    opening_direction: Direction | None

    @property
    def is_reducing_only(self) -> bool:
        return self.reducing_quantity > 0 and self.opening_quantity == 0


class RiskManager:
    """Enforces hard risk rules before order submission."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def check_order(self, order: OrderSpec, portfolio: AccountSummary) -> RiskCheckResult:
        """Run all risk checks against a proposed order. Returns pass/fail with details.

        Exposure-reducing slices of an order can proceed through the hard gate so the
        operator can always exit risk. Any exposure-increasing slice is checked like a
        new entry, even when it shares the same order as a position reduction.
        """
        violations: list[str] = []
        warnings: list[str] = []
        exposure = classify_order_exposure(order, portfolio)
        opening_quantity = exposure.opening_quantity
        is_reducing_only = exposure.is_reducing_only
        entry_order = order.model_copy(update={"quantity": opening_quantity})
        opening_value = _estimate_order_value(entry_order)

        # Paper trading gate
        if self.config.paper_trading:
            warnings.append("Paper trading mode is ON. Orders go to paper account.")

        if exposure.reducing_quantity > 0 and opening_quantity > 0:
            warnings.append(
                f"Order reduces {exposure.reducing_quantity:,.0f} shares and opens "
                f"{opening_quantity:,.0f} shares in the opposite direction."
            )

        # Market orders must have a reference price for risk estimation.
        if (
            opening_quantity > 0
            and order.order_type == OrderType.MARKET
            and order.reference_price is None
        ):
            violations.append(
                "Market orders require a reference_price for risk estimation. "
                "Fetch a quote before submitting."
            )

        # Stop-loss requirement applies to exposure-increasing slices.
        if (
            opening_quantity > 0
            and self.config.require_stop_loss
            and order.stop_loss_price is None
            and order.trailing_percent is None
        ):
            violations.append(
                "Stop-loss required. Set stop_loss_price or use a trailing_stop order."
            )

        # Position size and concentration checks apply only to new exposure.
        if opening_quantity > 0 and portfolio.total_value > 0:
            position_pct = (opening_value / portfolio.total_value) * 100

            if position_pct > self.config.max_position_pct:
                violations.append(
                    f"Position size {position_pct:.1f}% exceeds maximum "
                    f"{self.config.max_position_pct}% of portfolio "
                    f"(${opening_value:,.0f} of ${portfolio.total_value:,.0f})"
                )

            projected_symbol_value = abs(exposure.post_trade_quantity) * _estimate_price(order)
            total_pct = (projected_symbol_value / portfolio.total_value) * 100
            if projected_symbol_value > 0 and total_pct > self.config.max_position_pct:
                violations.append(
                    f"Total position in {order.symbol} would be {total_pct:.1f}% "
                    f"of portfolio (exceeds {self.config.max_position_pct}% limit)"
                )

        # Buying power check applies to the exposure-increasing slice.
        if (
            opening_quantity > 0
            and portfolio.buying_power > 0
            and opening_value > portfolio.buying_power
        ):
            violations.append(
                f"Order value ${opening_value:,.0f} exceeds buying power "
                f"${portfolio.buying_power:,.0f}"
            )

        # Leverage check applies to the exposure-increasing slice.
        if opening_quantity > 0 and portfolio.total_value > 0 and portfolio.margin_used > 0:
            invested = portfolio.margin_used + opening_value
            leverage = invested / portfolio.total_value
            if leverage > self.config.max_leverage:
                violations.append(
                    f"Leverage would be {leverage:.1f}x "
                    f"(exceeds {self.config.max_leverage}x limit)"
                )

        # Daily loss check never blocks a pure reduction, but it does block new exposure.
        if portfolio.total_value > 0 and portfolio.unrealized_pnl < 0:
            daily_loss_pct = abs(portfolio.unrealized_pnl) / portfolio.total_value * 100
            if daily_loss_pct > self.config.max_daily_loss_pct:
                if is_reducing_only:
                    warnings.append(
                        f"Daily loss is {daily_loss_pct:.1f}% "
                        f"(exceeds {self.config.max_daily_loss_pct}% circuit breaker). "
                        "Order proceeds because it only reduces exposure."
                    )
                elif opening_quantity > 0:
                    violations.append(
                        f"Daily loss is {daily_loss_pct:.1f}% "
                        f"(exceeds {self.config.max_daily_loss_pct}% circuit breaker). "
                        "Consider closing positions before opening new ones."
                    )

        passed = len(violations) == 0
        return RiskCheckResult(passed=passed, violations=violations, warnings=warnings)

    def check_portfolio_health(self, portfolio: AccountSummary) -> RiskCheckResult:
        """Check overall portfolio health without a specific order."""
        violations: list[str] = []
        warnings: list[str] = []

        if portfolio.total_value <= 0:
            return RiskCheckResult(passed=True, warnings=["No portfolio data available."])

        # Check largest position
        largest_pct = 0.0
        largest_sym = ""
        for pos in portfolio.positions:
            pos_value = (
                abs(pos.market_value) if pos.market_value else abs(pos.quantity * pos.avg_cost)
            )
            pct = (pos_value / portfolio.total_value) * 100 if portfolio.total_value > 0 else 0.0
            if pct > largest_pct:
                largest_pct = pct
                largest_sym = pos.symbol

        if largest_pct > self.config.max_position_pct:
            warnings.append(
                f"Largest position: {largest_sym} at {largest_pct:.1f}% "
                f"(exceeds {self.config.max_position_pct}% limit)"
            )

        # Cash level check
        if portfolio.total_value > 0:
            cash_pct = (portfolio.cash / portfolio.total_value) * 100
            if cash_pct < 5:
                warnings.append(f"Low cash: {cash_pct:.1f}% of portfolio. Limited flexibility.")
            elif cash_pct > 80:
                warnings.append(f"High cash: {cash_pct:.1f}%. Capital is sitting idle.")

        # Daily P&L check
        if portfolio.total_value > 0 and portfolio.unrealized_pnl < 0:
            loss_pct = abs(portfolio.unrealized_pnl) / portfolio.total_value * 100
            if loss_pct > self.config.max_daily_loss_pct:
                violations.append(
                    f"Daily loss circuit breaker: down {loss_pct:.1f}% "
                    f"(limit: {self.config.max_daily_loss_pct}%). "
                    "Consider reducing exposure."
                )
            elif loss_pct > self.config.max_daily_loss_pct * 0.7:
                warnings.append(
                    f"Approaching daily loss limit: down {loss_pct:.1f}% "
                    f"(limit: {self.config.max_daily_loss_pct}%)"
                )

        passed = len(violations) == 0
        return RiskCheckResult(passed=passed, violations=violations, warnings=warnings)


def _estimate_order_value(order: OrderSpec) -> float:
    """Estimate the dollar value of an order.

    Uses limit_price, stop_price, or reference_price (for market orders).
    Returns 0.0 only if no price is available — callers must treat this as
    an error for market orders (enforced by check_order).
    """
    price = _estimate_price(order)
    return abs(order.quantity * price)


def classify_order_exposure(
    order: OrderSpec,
    portfolio: AccountSummary,
    quantity: float | None = None,
) -> OrderExposure:
    """Classify how much of an order reduces versus opens exposure."""
    order_quantity = quantity if quantity is not None else order.quantity
    current_quantity = 0.0
    for pos in portfolio.positions:
        if pos.symbol == order.symbol:
            current_quantity += pos.quantity

    signed_order_quantity = order_quantity if order.action == OrderAction.BUY else -order_quantity
    reducing_quantity = 0.0
    reducing_direction: Direction | None = None
    opening_direction: Direction | None = None

    if order.action == OrderAction.BUY and current_quantity < 0:
        reducing_quantity = min(order_quantity, abs(current_quantity))
        reducing_direction = Direction.SHORT
    elif order.action == OrderAction.SELL and current_quantity > 0:
        reducing_quantity = min(order_quantity, current_quantity)
        reducing_direction = Direction.LONG

    opening_quantity = max(order_quantity - reducing_quantity, 0.0)
    if opening_quantity > 0:
        opening_direction = Direction.LONG if order.action == OrderAction.BUY else Direction.SHORT

    return OrderExposure(
        current_quantity=current_quantity,
        reducing_quantity=reducing_quantity,
        opening_quantity=opening_quantity,
        post_trade_quantity=current_quantity + signed_order_quantity,
        reducing_direction=reducing_direction,
        opening_direction=opening_direction,
    )


def _estimate_price(order: OrderSpec) -> float:
    return order.limit_price or order.stop_price or order.reference_price or 0.0
