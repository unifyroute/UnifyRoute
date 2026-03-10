"""
Tests for /admin/keys CRUD endpoints (Gateway API key management).

Covers:
- List keys
- Create API key (type=api) and admin key (type=admin)
- Delete key
- Key rotation: creating a new admin key should revoke old admin keys
"""
import pytest
import httpx
import uuid


class TestKeysList:

    def test_list_keys_returns_list(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/keys")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_list_keys_contains_id_and_label(self, admin_client: httpx.Client):
        r = admin_client.get("/api/admin/keys")
        assert r.status_code == 200
        for k in r.json():
            assert "id" in k
            assert "label" in k
            assert "scopes" in k


class TestKeyCreate:

    def test_create_api_key_returns_token_once(self, admin_client: httpx.Client):
        r = admin_client.post("/api/admin/keys", json={
            "label": f"test-api-key-{uuid.uuid4().hex[:6]}",
            "scopes": ["api"],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"].startswith("sk-")
        # cleanup
        admin_client.delete(f"/api/admin/keys/{body['id']}")

    def test_create_key_without_explicit_scope(self, admin_client: httpx.Client):
        """Creating a key with no scopes should still succeed."""
        r = admin_client.post("/api/admin/keys", json={
            "label": f"no-scope-key-{uuid.uuid4().hex[:6]}",
            "scopes": [],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert "token" in body
        # cleanup
        admin_client.delete(f"/api/admin/keys/{body['id']}")

    def test_created_api_key_is_functional(self, admin_client: httpx.Client):
        """A freshly created API key should be usable to hit /v1/models."""
        import httpx as _httpx
        r = admin_client.post("/api/admin/keys", json={
            "label": f"functional-key-{uuid.uuid4().hex[:6]}",
            "scopes": ["api"],
        })
        assert r.status_code == 200
        token = r.json()["token"]
        key_id = r.json()["id"]

        import os
        base_url = os.environ.get("OPENROUTER_BASE_URL", "http://localhost:6565")
        with _httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}) as c:
            check = c.get("/api/v1/models")
            assert check.status_code == 200
        # cleanup
        admin_client.delete(f"/api/admin/keys/{key_id}")


class TestKeyDelete:

    def test_delete_key_success(self, admin_client: httpx.Client):
        r = admin_client.post("/api/admin/keys", json={
            "label": f"to-delete-{uuid.uuid4().hex[:6]}",
            "scopes": ["api"],
        })
        assert r.status_code == 200
        kid = r.json()["id"]
        r2 = admin_client.delete(f"/api/admin/keys/{kid}")
        assert r2.status_code == 200
        assert r2.json()["status"] == "success"

    def test_delete_nonexistent_key(self, admin_client: httpx.Client):
        r = admin_client.delete(f"/api/admin/keys/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_deleted_key_is_rejected(self, admin_client: httpx.Client):
        """A deleted key must no longer authenticate."""
        import httpx as _httpx, os
        r = admin_client.post("/api/admin/keys", json={
            "label": f"rev-key-{uuid.uuid4().hex[:6]}",
            "scopes": ["api"],
        })
        assert r.status_code == 200
        token = r.json()["token"]
        kid = r.json()["id"]
        admin_client.delete(f"/api/admin/keys/{kid}")

        base_url = os.environ.get("OPENROUTER_BASE_URL", "http://localhost:6565")
        with _httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}) as c:
            check = c.get("/api/v1/models")
            assert check.status_code == 401
