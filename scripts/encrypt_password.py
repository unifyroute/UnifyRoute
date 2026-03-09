#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).parent.parent.resolve()
sys.path.append(str(ROOT / "shared" / "src"))

try:
    from shared.security import wrap_secret
    from shared.database import load_dotenv # if exists, else manual
except ImportError:
    # Fallback to manual setup if shared package not installed in path
    import base64
    def wrap_secret(secret: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = os.environ.get("VAULT_MASTER_KEY")
        if not key:
            raise ValueError("VAULT_MASTER_KEY not set")
        key_bytes = base64.urlsafe_b64decode(key.encode('utf-8'))
        aesgcm = AESGCM(key_bytes)
        iv = os.urandom(12)
        ciphertext = aesgcm.encrypt(iv, secret.encode('utf-8'), None)
        iv_b64 = base64.urlsafe_b64encode(iv).decode('utf-8')
        ct_b64 = base64.urlsafe_b64encode(ciphertext).decode('utf-8')
        return f"enc:{iv_b64}:{ct_b64}"

def main():
    # Load .env manually
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        import getpass
        password = getpass.getpass("Enter password to encrypt: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match!")
            sys.exit(1)

    try:
        encrypted = wrap_secret(password)
        print("\nEncrypted Password (paste this into your .env as MASTER_PASSWORD):")
        print(f"\033[1;32m{encrypted}\033[0m\n")
    except Exception as e:
        print(f"Error encrypting password: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
