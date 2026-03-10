import pytest
import httpx
import uuid

class TestKeyReveal:
    def test_reveal_key_returns_hash_prefix(self, admin_client: httpx.Client):
        import os
        from dotenv import load_dotenv
        load_dotenv()
        from shared.security import unwrap_secret
        pwd = unwrap_secret(os.environ.get("MASTER_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "admin"))
        
        r_create = admin_client.post("/api/admin/keys", json={
            "label": f"label-reveal-{uuid.uuid4().hex[:6]}",
            "scopes": ["api"],
        })
        assert r_create.status_code == 200
        key_id = r_create.json()["id"]
        
        r_reveal = admin_client.post(f"/api/admin/keys/{key_id}/reveal", json={"password": pwd})
        assert r_reveal.status_code == 200
        reveal_info = r_reveal.json()["reveal_info"]
        assert reveal_info.startswith("sk-")
        
        # cleanup
        admin_client.delete(f"/api/admin/keys/{key_id}")

    def test_reveal_nonexistent_key_404(self, admin_client: httpx.Client):
        import os
        from dotenv import load_dotenv
        load_dotenv()
        from shared.security import unwrap_secret
        pwd = unwrap_secret(os.environ.get("MASTER_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "admin"))
        
        r = admin_client.post(f"/api/admin/keys/{uuid.uuid4()}/reveal", json={"password": pwd})
        assert r.status_code == 404
