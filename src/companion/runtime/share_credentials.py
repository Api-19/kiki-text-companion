from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import keyring
from keyring.backend import KeyringBackend

SERVICE = "kiki-companion"


class CredentialError(RuntimeError):
    """Fixed failure at the native credential-store boundary."""


def _native_keyring() -> KeyringBackend:
    backend = keyring.get_keyring()
    backend_type = type(backend)
    if (backend_type.__module__, backend_type.__name__) not in (
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
    ):
        raise CredentialError("credential store unavailable")
    return backend


def normalize_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url.strip())
    userinfo, separator, host_port = parsed.netloc.rpartition("@")
    return urlunsplit((
        parsed.scheme.lower(), f"{userinfo}{separator}{host_port.lower()}",
        parsed.path.rstrip("/"), parsed.query, parsed.fragment,
    ))


def _account(env_name: str, base_url: str) -> str:
    return f"{env_name}|{normalize_base_url(base_url)}"


def store_api_key(env_name: str, base_url: str, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("empty api key")
    account = _account(env_name, base_url)
    try:
        _native_keyring().set_password(SERVICE, account, api_key)
    except Exception:
        raise CredentialError("credential store unavailable") from None


def load_api_key(env_name: str, base_url: str) -> str:
    account = _account(env_name, base_url)
    try:
        api_key = _native_keyring().get_password(SERVICE, account)
    except Exception:
        raise CredentialError("credential store unavailable") from None
    if api_key is None:
        raise CredentialError("api key not found")
    return api_key


def delete_api_key(env_name: str, base_url: str) -> None:
    account = _account(env_name, base_url)
    try:
        backend = _native_keyring()
        if backend.get_password(SERVICE, account) is not None:
            backend.delete_password(SERVICE, account)
    except Exception:
        raise CredentialError("credential store unavailable") from None
