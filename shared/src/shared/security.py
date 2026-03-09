from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.fernet import Fernet
import os
import base64
from typing import Tuple, Optional

def _get_master_key_bytes() -> bytes:
    """Gets the raw 32 bytes from VAULT_MASTER_KEY."""
    key = os.environ.get("VAULT_MASTER_KEY")
    if not key:
        raise ValueError("VAULT_MASTER_KEY environment variable not set")
    try:
        # Fernet keys are urlsafe base64 encoded.
        raw_bytes = base64.urlsafe_b64decode(key.encode('utf-8'))
        if len(raw_bytes) != 32:
            raise ValueError()
        return raw_bytes
    except Exception as e:
        raise ValueError("Invalid VAULT_MASTER_KEY. Must be 32 url-safe base64-encoded bytes.") from e

def _get_fernet() -> Fernet:
    """Gets the Fernet instance for migrating old secrets."""
    key = os.environ.get("VAULT_MASTER_KEY")
    return Fernet(key.encode('utf-8'))

def encrypt_secret(secret: str) -> Tuple[bytes, bytes]:
    """Encrypts a string secret using the master vault key with AES-256-GCM.
    Returns:
        (ciphertext, iv)
    """
    key_bytes = _get_master_key_bytes()
    aesgcm = AESGCM(key_bytes)
    iv = os.urandom(12)  # Recommended 96-bit IV for GCM
    ciphertext = aesgcm.encrypt(iv, secret.encode('utf-8'), None)
    return ciphertext, iv

def wrap_secret(secret: str) -> str:
    """Wraps a secret string into an encrypted format prefixed with 'enc:'."""
    ciphertext, iv = encrypt_secret(secret)
    iv_b64 = base64.urlsafe_b64encode(iv).decode('utf-8')
    ct_b64 = base64.urlsafe_b64encode(ciphertext).decode('utf-8')
    return f"enc:{iv_b64}:{ct_b64}"

def unwrap_secret(wrapped: str) -> str:
    """Unwraps a secret string if it's prefixed with 'enc:', otherwise returns it as-is."""
    if not isinstance(wrapped, str) or not wrapped.startswith("enc:"):
        return wrapped
    
    try:
        parts = wrapped.split(":")
        if len(parts) != 3:
            return wrapped
        
        iv = base64.urlsafe_b64decode(parts[1].encode('utf-8'))
        ciphertext = base64.urlsafe_b64decode(parts[2].encode('utf-8'))
        
        return decrypt_secret(ciphertext, iv)
    except Exception:
        # If decryption fails, maybe it wasn't actually an encrypted secret but just started with enc:
        return wrapped

def decrypt_secret(encrypted_secret: bytes, iv: Optional[bytes] = None) -> str:
    """Decrypts a bytes secret using the master vault key with AES-256-GCM.
    If IV is None, falls back to legacy Fernet decryption.
    """
    if iv is None:
        # Legacy fallback
        f = _get_fernet()
        return f.decrypt(encrypted_secret).decode('utf-8')
    
    key_bytes = _get_master_key_bytes()
    aesgcm = AESGCM(key_bytes)
    plaintext = aesgcm.decrypt(iv, encrypted_secret, None)
    return plaintext.decode('utf-8')
