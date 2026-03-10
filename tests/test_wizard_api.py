"""
tests/test_wizard_api.py — Integration tests for the Setup Wizard API endpoints.

Exercises:
  - GET  /admin/wizard/providers/available
  - GET  /admin/wizard/models/{provider_name}
  - POST /admin/wizard/onboard  (full wizard payload)

Requires the LLMWay API gateway to be running on localhost:6565.
"""
import uuid
import pytest
import httpx


# ── Helper: build a unique provider name to avoid pollution ───────────────────

def _provname(suffix: str = "") -> str:
    uid = uuid.uuid4().hex[:6]
    return f"wiztest-{uid}{suffix}"


# ── GET /admin/wizard/providers/available ────────────────────────────────────

class TestAvailableProviders:

    def test_returns_200_list(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/providers/available")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0

    def test_includes_known_providers(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/providers/available")
        names = {p["name"] for p in r.json()}
        for expected in ("openai", "anthropic", "groq", "unifyroute"):
            assert expected in names, f"Expected '{expected}' in provider list"

    def test_provider_shape(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/providers/available")
        item = next((p for p in r.json() if p["name"] == "openai"), None)
        assert item is not None
        assert item["display_name"] == "OpenAI"
        assert item["auth_type"] == "api_key"
        assert isinstance(item["has_credentials"], bool)
        assert isinstance(item["credentials_count"], int)
        assert isinstance(item["has_catalog"], bool)

    def test_catalog_providers_flagged(self, admin_client: httpx.Client):
        """Providers with a static model catalog must be flagged."""
        r = admin_client.get("/api/admin/wizard/providers/available")
        catalog_entries = [p for p in r.json() if p["has_catalog"]]
        assert len(catalog_entries) > 0, "At least one provider should have a pre-built catalog"

    def test_requires_auth(self, raw_client: httpx.Client):
        r = raw_client.get("/api/admin/wizard/providers/available")
        assert r.status_code in (401, 403), r.text


# ── GET /admin/wizard/models/{provider_name} ─────────────────────────────────

class TestWizardModels:

    def test_known_provider_returns_catalog(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/models/openai")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["provider"] == "openai"
        assert data["has_catalog"] is True
        assert isinstance(data["models"], list)
        assert len(data["models"]) > 0

    def test_model_shape(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/models/openai")
        model = r.json()["models"][0]
        expected_fields = [
        "model_id", "display_name", "tier",
        "context_window", "input_cost_per_1k", "output_cost_per_1k",
        "supports_streaming", "supports_functions", "enabled"
    ]
        for field in expected_fields:
            assert field in model, f"Expected field '{field}' in model entry"

    def test_anthropic_catalog(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/wizard/models/anthropic")
        assert r.status_code == 200
        data = r.json()
        assert data["has_catalog"] is True
        model_ids = [m["model_id"] for m in data["models"]]
        assert any("claude" in m for m in model_ids)

    def test_unknown_provider_returns_empty_catalog(self, admin_client: httpx.Client):
        """Non-catalog providers return has_catalog=False and empty list."""
        r = admin_client.get("/api/admin/wizard/models/nonexistent-provider-xyz")
        assert r.status_code == 200
        data = r.json()
        assert data["has_catalog"] is False
        assert data["models"] == []

    def test_requires_auth(self, raw_client: httpx.Client):
        r = raw_client.get("/api/admin/wizard/models/openai")
        assert r.status_code in (401, 403), r.text


# ── POST /admin/wizard/onboard ────────────────────────────────────────────────

class TestWizardOnboard:

    def _make_payload(self, provider_name: str, cred_label: str, model_id: str) -> dict:
        return {
            "providers": [
                {
                    "provider_name": provider_name,
                    "credentials": [
                        {
                            "label": cred_label,
                            "secret_key": "wiz-test-secret-key-placeholder",
                            "auth_type": "api_key",
                        }
                    ],
                    "models": [
                        {
                            "model_id": model_id,
                            "display_name": "Test Model",
                            "tier": "lite",
                            "context_window": 128000,
                            "input_cost_per_1k": 0.001,
                            "output_cost_per_1k": 0.002,
                            "supports_streaming": True,
                            "supports_functions": True,
                            "enabled": True,
                        }
                    ],
                }
            ],
            "routing_tiers": {
                "lite": {
                    "strategy": "cheapest_available",
                    "fallback_on": ["429", "503", "timeout"],
                    "models": [{"provider": provider_name, "model": model_id}],
                }
            },
            "brain_entries": [],
        }

    def test_onboard_creates_provider_credential_model(self, admin_client: httpx.Client):
        pname = _provname()
        cred_label = f"cred-{uuid.uuid4().hex[:6]}"
        model_id = f"wiz-model-{uuid.uuid4().hex[:6]}"
        payload = self._make_payload(pname, cred_label, model_id)

        r = admin_client.post("/api/admin/wizard/onboard", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True

        summary = data["summary"]
        assert len(summary["providers"]) == 1
        assert summary["providers"][0]["name"] == pname
        assert len(summary["credentials"]) == 1
        assert summary["credentials"][0]["label"] == cred_label
        assert len(summary["models"]) == 1
        assert summary["models"][0]["model_id"] == model_id

        # Verify routing was persisted
        assert summary["routing"] is not None
        assert "lite" in summary["routing"]["tiers"]

        # Cleanup
        provider_id = summary["providers"][0]["id"]
        admin_client.delete(f"/api/admin/providers/{provider_id}")

    def test_onboard_with_routing_updates_config(self, admin_client: httpx.Client):
        pname = _provname("-rt")
        payload = self._make_payload(pname, "key-1", "some/model")
        r = admin_client.post("/api/admin/wizard/onboard", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["summary"]["routing"] is not None
        # Verify the routing config was saved
        rr = admin_client.get("/api/admin/routing")
        assert rr.status_code == 200
        assert "tiers" in rr.json()["yaml_content"]
        # Cleanup
        pid = r.json()["summary"]["providers"][0]["id"]
        admin_client.delete(f"/api/admin/providers/{pid}")

    def test_onboard_skips_duplicate_credential(self, admin_client: httpx.Client):
        """Calling onboard twice with same provider+label should not create duplicates."""
        pname = _provname("-dup")
        cred_label = "dup-cred"
        payload = self._make_payload(pname, cred_label, "dup-model")

        r1 = admin_client.post("/api/admin/wizard/onboard", json=payload)
        assert r1.status_code == 200, r1.text
        r2 = admin_client.post("/api/admin/wizard/onboard", json=payload)
        assert r2.status_code == 200, r2.text  # Should not error

        # Verify only one credential exists for this provider
        pid = r1.json()["summary"]["providers"][0]["id"]
        cred_r = admin_client.get("/api/admin/credentials")
        creds_for_provider = [c for c in cred_r.json() if c["provider_id"] == pid]
        assert len(creds_for_provider) == 1, "Duplicate credential should not be created"
        admin_client.delete(f"/api/admin/providers/{pid}")

    def test_onboard_empty_payload_succeeds(self, admin_client: httpx.Client):
        """An empty onboard request is valid (no-op)."""
        payload = {"providers": [], "routing_tiers": {}, "brain_entries": []}
        r = admin_client.post("/api/admin/wizard/onboard", json=payload)
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_onboard_requires_auth(self, raw_client: httpx.Client):
        payload = {"providers": [], "routing_tiers": {}, "brain_entries": []}
        r = raw_client.post("/api/admin/wizard/onboard", json=payload)
        assert r.status_code in (401, 403), r.text
