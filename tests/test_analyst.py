"""Tests for analyst module — unit tests with mocked dependencies."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from augur.analyst import Analyst, AnalystError, _get_tool_schema
from augur.config import ClaudeConfig, RiskConfig
from augur.models import AccountSummary, Position


def _make_analyst(backend: str = "cli") -> Analyst:
    """Create an Analyst with default configs for testing."""
    claude = ClaudeConfig(backend=backend)
    risk = RiskConfig()
    if backend == "api":
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            return Analyst(claude, risk)
    return Analyst(claude, risk)


class TestGetToolSchema:
    def test_valid_tool(self) -> None:
        schema = _get_tool_schema("analyze_trade")
        assert "properties" in schema
        assert "symbol" in schema["properties"]

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(AnalystError, match="Unknown tool"):
            _get_tool_schema("nonexistent_tool")

    def test_construct_order_schema(self) -> None:
        schema = _get_tool_schema("construct_order")
        assert "symbol" in schema["properties"]
        assert "action" in schema["properties"]

    def test_assess_portfolio_risk_schema(self) -> None:
        schema = _get_tool_schema("assess_portfolio_risk")
        assert "overall_risk" in schema["properties"]


class TestAnalystInit:
    def test_cli_backend_no_key_required(self) -> None:
        analyst = _make_analyst("cli")
        assert analyst.backend == "cli"

    def test_api_backend_missing_key_raises(self) -> None:
        claude = ClaudeConfig(backend="api")
        risk = RiskConfig()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(AnalystError, match="ANTHROPIC_API_KEY"):
                Analyst(claude, risk)

    def test_api_backend_with_key_succeeds(self) -> None:
        analyst = _make_analyst("api")
        assert analyst.backend == "api"


class TestBuildContext:
    def test_none_portfolio_returns_empty(self) -> None:
        analyst = _make_analyst()
        assert analyst._build_context(None) == ""

    def test_portfolio_returns_json(self) -> None:
        analyst = _make_analyst()
        portfolio = AccountSummary(
            total_value=100_000,
            cash=30_000,
            buying_power=60_000,
            positions=[
                Position(
                    symbol="AAPL",
                    quantity=100,
                    avg_cost=150.0,
                    market_price=155.0,
                    unrealized_pnl=500.0,
                )
            ],
        )
        context = analyst._build_context(portfolio)
        data = json.loads(context)
        assert data["account"]["total_value"] == 100_000
        assert len(data["positions"]) == 1
        assert data["positions"][0]["symbol"] == "AAPL"


class TestUnwrapCliJson:
    def test_envelope_with_json_string(self) -> None:
        data = {"type": "result", "result": '{"symbol": "AAPL", "direction": "long"}'}
        result = Analyst._unwrap_cli_json(data)
        assert result["symbol"] == "AAPL"

    def test_envelope_with_dict(self) -> None:
        data = {"type": "result", "result": {"symbol": "MSFT"}}
        result = Analyst._unwrap_cli_json(data)
        assert result["symbol"] == "MSFT"

    def test_direct_dict(self) -> None:
        data = {"symbol": "TSLA"}
        result = Analyst._unwrap_cli_json(data)
        assert result["symbol"] == "TSLA"

    def test_invalid_json_string_raises(self) -> None:
        data = {"type": "result", "result": "not json at all"}
        with pytest.raises(AnalystError, match="not valid JSON"):
            Analyst._unwrap_cli_json(data)

    def test_non_dict_result_raises(self) -> None:
        data = {"type": "result", "result": '["a", "b"]'}
        with pytest.raises(AnalystError, match="Expected JSON object"):
            Analyst._unwrap_cli_json(data)

    def test_unexpected_result_type_raises(self) -> None:
        data = {"type": "result", "result": 42}
        with pytest.raises(AnalystError, match="Unexpected result type"):
            Analyst._unwrap_cli_json(data)

    def test_unexpected_output_type_raises(self) -> None:
        with pytest.raises(AnalystError, match="Unexpected CLI output"):
            Analyst._unwrap_cli_json([1, 2, 3])  # type: ignore[arg-type]


class TestCliEnv:
    def test_strips_claudecode(self) -> None:
        analyst = _make_analyst()
        with patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home/test"}):
            env = analyst._cli_env()
            assert "CLAUDECODE" not in env
            assert "HOME" in env


class TestCliBackend:
    def test_cli_text_calls_subprocess(self) -> None:
        analyst = _make_analyst("cli")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Analysis response text"
        mock_result.stderr = ""

        with patch("augur.analyst.subprocess.run", return_value=mock_result) as mock_run:
            response = analyst._cli_text("What about AAPL?")
            assert response == "Analysis response text"
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "claude" in cmd
            assert "-p" in cmd

    def test_cli_not_found_raises(self) -> None:
        analyst = _make_analyst("cli")
        with (
            patch("augur.analyst.subprocess.run", side_effect=FileNotFoundError),
            pytest.raises(AnalystError, match="claude CLI not found"),
        ):
            analyst._cli_text("test")

    def test_cli_nonzero_exit_raises(self) -> None:
        analyst = _make_analyst("cli")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"
        with (
            patch("augur.analyst.subprocess.run", return_value=mock_result),
            pytest.raises(AnalystError, match="Claude CLI error"),
        ):
            analyst._cli_text("test")

    def test_cli_structured_returns_parsed(self) -> None:
        analyst = _make_analyst("cli")
        tool_result = {
            "symbol": "AAPL",
            "action": "BUY",
            "quantity": 10,
            "order_type": "limit",
            "reason": "test",
        }
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"type": "result", "result": json.dumps(tool_result)})
        mock_result.stderr = ""

        with patch("augur.analyst.subprocess.run", return_value=mock_result):
            result = analyst._cli_structured("construct_order", "Buy AAPL")
            assert result["symbol"] == "AAPL"
            assert result["action"] == "BUY"
