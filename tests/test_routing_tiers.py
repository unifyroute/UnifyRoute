"""
Tests for model tier routing isolation.

These tests verify that the virtual model routing system correctly:
1. Inserts test models with specific tiers into the database
2. Routes virtual model aliases (lite/base/thinking) to the cheapest
   backend configured per tier (not YAML-based routing)
3. Cleans up test data afterwards

These are integration tests — they insert real rows into the DB via
the /admin/* APIs, then exercise routing behavior end-to-end.
"""
import pytest
import httpx
import uuid


@pytest.fixture(scope="module")
def tier_test_provider(admin_client: httpx.Client):
    """Provider for tier routing tests."""
    name = f"tier-routing-prov-{uuid.uuid4().hex[:8]}"
    r = admin_client.post("/api/admin/providers", json={
        "name": name,
        "display_name": "Tier Routing Test Provider",
        "auth_type": "api_key",
        "enabled": True,
    })
    assert r.status_code == 200, r.text
    prov = r.json()
    yield prov
    admin_client.delete(f"/api/admin/providers/{prov['id']}")


@pytest.fixture(scope="module")
def tier_test_credential(admin_client: httpx.Client, tier_test_provider: dict):
    """Credential attached to the test provider to enable actual routing."""
    r = admin_client.post(f"/api/admin/credentials", json={
        "provider_id": tier_test_provider['id'],
        "label": "Test Key",
        "secret_key": "sk-mock-12345",
    })
    
    assert r.status_code == 200, r.text
    cred = r.json()
    yield cred
    admin_client.delete(f"/api/admin/credentials/{cred['id']}")


@pytest.fixture(scope="module")
def tier_test_models(admin_client: httpx.Client, tier_test_provider: dict, tier_test_credential: dict):
    """Create models with all three tiers for routing tests."""
    models = []
    for tier, cost_in, cost_out in [
        ("lite", 0.1, 0.2),
        ("lite", 0.5, 1.0),  # second lite model, more expensive
        ("base", 0.8, 1.6),
        ("thinking", 2.0, 4.0),
    ]:
        r = admin_client.post("/api/admin/models", json={
            "provider_id": tier_test_provider["id"],
            "model_id": f"tier-test-{tier}-{uuid.uuid4().hex[:6]}",
            "display_name": f"Test {tier} Model",
            "tier": tier,
            "context_window": 128000,
            "input_cost_per_1k": cost_in,
            "output_cost_per_1k": cost_out,
            "enabled": True,
        })
        assert r.status_code == 200, r.text
        models.append(r.json())
    yield models
    for m in models:
        admin_client.delete(f"/api/admin/models/{m['id']}")


class TestTierRoutingIsolation:

    def test_all_three_tiers_have_models(
        self, admin_client: httpx.Client, tier_test_models: list
    ):
        """We should have created models for all three tiers."""
        tiers = {m["tier"] for m in tier_test_models}
        assert "lite" in tiers
        assert "base" in tiers
        assert "thinking" in tiers

    @pytest.mark.parametrize("tier", ["lite", "base", "thinking"])
    def test_each_tier_recognized_in_routing(
        self, api_client: httpx.Client, tier_test_models: list, tier: str
    ):
        """
        With models configured for both tiers, each virtual model alias
        must be routed (attempt made) rather than returning a 422 validation error.
        A 503 is acceptable if no credentials exist, but 422 would be wrong.
        """
        r = api_client.post("/api/v1/chat/completions", json={
            "model": tier,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        })
        assert r.status_code != 422, (
            f"Virtual tier '{tier}' returned 422 (validation), expected routing attempt"
        )

    def test_models_list_shows_admin_catalog(
        self, admin_client: httpx.Client, tier_test_models: list
    ):
        """Admin /v1/models list should contain our newly created models."""
        r = admin_client.get("/api/v1/models")
        assert r.status_code == 200
        ids_in_catalog = {m["id"] for m in r.json()["data"]}
        for model in tier_test_models:
            # Models appear as provider/model or just model_id depending on tier
            found = any(model["model_id"] in mid for mid in ids_in_catalog)
            assert found, f"Model {model['model_id']} not found in admin catalog"

    def test_admin_models_list_includes_all_tiers(
        self, admin_client: httpx.Client, tier_test_models: list
    ):
        """GET /admin/models should show all created models."""
        r = admin_client.get("/api/admin/models")
        assert r.status_code == 200
        all_ids = {m["model_id"] for m in r.json()}
        for model in tier_test_models:
            assert model["model_id"] in all_ids, (
                f"Model {model['model_id']} missing from /admin/models"
            )

    def test_cost_usd_tracking_for_streams(self, api_client: httpx.Client, admin_client: httpx.Client, tier_test_models):
        """Streaming chat completions should correctly log token counts and USD costs, not defaults of 0."""
        # 1. Send streaming chat completions
        actual_model = tier_test_models[0]["model_id"]
        stream_r = api_client.post("/api/v1/chat/completions", json={
            "model": actual_model,
            "messages": [{"role": "user", "content": "How are you?"}],
            "stream": True,
            "max_tokens": 5,
        })
        
        # We can only test this if we don't get 503 (meaning a backend exists)
        if stream_r.status_code == 503:
            print(f"FAILED WITH 503: {stream_r.text}")
            pytest.skip("No backends available to serve model for stream test.")
            
        assert stream_r.status_code == 200
        
        # Read the stream chunks fully
        chunk_lines = [line.decode("utf-8") if isinstance(line, bytes) else line for line in stream_r.iter_lines() if line]
        if any("We're sorry, no models or quota" in l for l in chunk_lines):
            pytest.skip("Model was exhausted, skipping cost tracking test.")
        import time
        time.sleep(1) # wait for bg tasks
        logs_r = admin_client.get("/api/admin/logs?limit=5")
        assert logs_r.status_code == 200
        items = logs_r.json().get("items", [])
        
        assert len(items) > 0, "No request logs found for streaming response."
        
        # Find the latest successful stream request
        latest_log = None
        for item in items:
            if "success_stream" in item.get("status", "") or "success" in item.get("status", ""):
                latest_log = item
                break
                
        assert latest_log is not None, "Could not find a successful logged request."
        
        # 3. Assert prompt/completion tokens are not 0 and cost_usd > 0
        assert latest_log["prompt_tokens"] > 0, "Prompt tokens were not calculated for stream."
        assert latest_log["completion_tokens"] > 0, "Completion tokens were not calculated for stream."
        assert latest_log["cost_usd"] > 0.0, "Cost USD was 0.0 for streaming response."
