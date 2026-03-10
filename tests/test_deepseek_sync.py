"""
Tests for DeepSeek adapter model listing with cost enrichment.

Verifies:
  1. list_models() returns models with non-zero input/output costs
  2. Known models get their tier and context_window populated correctly
  3. Unknown models get the sensible default pricing
  4. If the API returns nothing, an empty list is returned (no crash)

Note: tests use asyncio.run() directly because pytest-asyncio is not installed
in this environment (same pattern as test_quota_pollers.py).
"""

import asyncio
import sys, os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'router', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared', 'src'))

from router.adapters.compat_adapters import DeepSeekAdapter, _DEEPSEEK_MODEL_META, _DEEPSEEK_DEFAULT_META


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api_response(model_ids: list) -> dict:
    """Simulate a DeepSeek /v1/models response."""
    return {
        "object": "list",
        "data": [{"id": mid, "object": "model", "created": 1700000000} for mid in model_ids],
    }


async def _run_list_models(api_response) -> list:
    """Run DeepSeekAdapter._list_models_impl with a mocked HTTP client."""
    adapter = DeepSeekAdapter()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    if api_response is not None:
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=api_response)
    else:
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Connection error"))

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        return await adapter._list_models_impl("sk-test-key")


# ---------------------------------------------------------------------------
# Tests: known models get correct pricing
# ---------------------------------------------------------------------------

def test_deepseek_chat_gets_correct_pricing():
    models = asyncio.run(_run_list_models(_make_api_response(["deepseek-chat"])))
    assert len(models) == 1
    m = models[0]
    assert m.model_id == "deepseek-chat"
    assert m.input_cost_per_1k == _DEEPSEEK_MODEL_META["deepseek-chat"]["input"]
    assert m.output_cost_per_1k == _DEEPSEEK_MODEL_META["deepseek-chat"]["output"]
    assert m.context_window == _DEEPSEEK_MODEL_META["deepseek-chat"]["ctx"]
    assert m.supports_functions is True
    print(f"  ✅ deepseek-chat: input={m.input_cost_per_1k} output={m.output_cost_per_1k} ctx={m.context_window}")


def test_deepseek_reasoner_gets_thinking_pricing():
    models = asyncio.run(_run_list_models(_make_api_response(["deepseek-reasoner"])))
    assert len(models) == 1
    m = models[0]
    assert m.model_id == "deepseek-reasoner"
    assert m.input_cost_per_1k == _DEEPSEEK_MODEL_META["deepseek-reasoner"]["input"]
    assert m.output_cost_per_1k == _DEEPSEEK_MODEL_META["deepseek-reasoner"]["output"]
    # Reasoner doesn't support function calling
    assert m.supports_functions is False
    print(f"  ✅ deepseek-reasoner: functions={m.supports_functions} (expected False)")


def test_deepseek_coder_gets_correct_pricing():
    models = asyncio.run(_run_list_models(_make_api_response(["deepseek-coder"])))
    assert len(models) == 1
    m = models[0]
    assert m.input_cost_per_1k > 0.0
    assert m.output_cost_per_1k > 0.0
    assert m.context_window == 16000
    print(f"  ✅ deepseek-coder: ctx={m.context_window}")


def test_multiple_deepseek_models_all_enriched():
    model_ids = ["deepseek-chat", "deepseek-reasoner", "deepseek-coder", "deepseek-v2.5"]
    models = asyncio.run(_run_list_models(_make_api_response(model_ids)))
    assert len(models) == 4
    for m in models:
        assert m.input_cost_per_1k > 0.0, f"{m.model_id} has zero input cost"
        assert m.output_cost_per_1k > 0.0, f"{m.model_id} has zero output cost"
        assert m.context_window > 0
    print(f"  ✅ All {len(models)} models enriched with non-zero pricing")


def test_unknown_deepseek_model_gets_default_pricing():
    """Models not in the known price map should get the default pricing."""
    models = asyncio.run(_run_list_models(_make_api_response(["deepseek-future-model-xyz"])))
    assert len(models) == 1
    m = models[0]
    assert m.input_cost_per_1k == _DEEPSEEK_DEFAULT_META["input"]
    assert m.output_cost_per_1k == _DEEPSEEK_DEFAULT_META["output"]
    assert m.context_window == _DEEPSEEK_DEFAULT_META["ctx"]
    print(f"  ✅ Unknown model got defaults: input={m.input_cost_per_1k}")


def test_empty_api_response_returns_empty_list():
    models = asyncio.run(_run_list_models({"object": "list", "data": []}))
    assert models == []
    print("  ✅ Empty API response correctly returns []")


def test_none_fetch_returns_empty_list():
    """If the HTTP call fails entirely (fetch_json_safe returns None), return empty list."""
    adapter = DeepSeekAdapter()

    async def _run():
        with patch("router.adapters.base.fetch_json_safe", return_value=None):
            return await adapter._list_models_impl("sk-bad-key")

    result = asyncio.run(_run())
    assert result == []
    print("  ✅ Failed HTTP fetch returns []")


# ---------------------------------------------------------------------------
# Cost calculation sanity tests
# ---------------------------------------------------------------------------

def test_cost_per_1k_values_are_sane():
    """All known DeepSeek model pricing should be reasonable (not zero, not absurd)."""
    for model_id, meta in _DEEPSEEK_MODEL_META.items():
        assert 0 < meta["input"] < 1.0, f"{model_id} input cost {meta['input']} out of range"
        assert 0 < meta["output"] < 1.0, f"{model_id} output cost {meta['output']} out of range"
        assert meta["ctx"] > 0
    print(f"  ✅ All {len(_DEEPSEEK_MODEL_META)} model price entries are sane")


if __name__ == "__main__":
    tests = [
        test_deepseek_chat_gets_correct_pricing,
        test_deepseek_reasoner_gets_thinking_pricing,
        test_deepseek_coder_gets_correct_pricing,
        test_multiple_deepseek_models_all_enriched,
        test_unknown_deepseek_model_gets_default_pricing,
        test_empty_api_response_returns_empty_list,
        test_none_fetch_returns_empty_list,
        test_cost_per_1k_values_are_sane,
    ]
    for t in tests:
        t()
        print(f"  ✅ {t.__name__}")
    print(f"\n✅ All {len(tests)} DeepSeek sync tests passed!")
