from __future__ import annotations

import keyring
import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from companion.runtime.share_credentials import (
    SERVICE,
    CredentialError,
    delete_api_key,
    load_api_key,
    store_api_key,
)


class FakeKeyring:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.failure: str | None = None
        self.error_type: type[Exception] = KeyringError

    def get_password(self, service: str, account: str) -> str | None:
        self._check_failure("get")
        return self.passwords.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._check_failure("set")
        self.passwords[service, account] = password

    def delete_password(self, service: str, account: str) -> None:
        self._check_failure("delete")
        if (service, account) not in self.passwords:
            raise PasswordDeleteError("missing password")
        del self.passwords[service, account]

    def _check_failure(self, operation: str) -> None:
        if self.failure == operation:
            raise self.error_type("synthetic-api-key")


@pytest.fixture(params=[
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    module, name = request.param
    fake_type = type(name, (FakeKeyring,), {"__module__": module})
    fake = fake_type()
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake)
    return fake


def test_store_load_delete_round_trip_with_provider_accounts(backend: FakeKeyring) -> None:
    assert SERVICE == "kiki-companion"
    store_api_key("OPENAI_API_KEY", "https://a.example/v1", "  synthetic-openai-key\n")
    store_api_key("DEEPSEEK_API_KEY", "https://b.example/v1", "synthetic-deepseek-key")

    assert backend.passwords == {
        (SERVICE, "OPENAI_API_KEY|https://a.example/v1"): "synthetic-openai-key",
        (SERVICE, "DEEPSEEK_API_KEY|https://b.example/v1"): "synthetic-deepseek-key",
    }
    assert load_api_key("OPENAI_API_KEY", "https://a.example/v1") == "synthetic-openai-key"
    assert load_api_key("DEEPSEEK_API_KEY", "https://b.example/v1") == "synthetic-deepseek-key"

    delete_api_key("OPENAI_API_KEY", "https://a.example/v1")
    delete_api_key("OPENAI_API_KEY", "https://a.example/v1")
    assert load_api_key("DEEPSEEK_API_KEY", "https://b.example/v1") == "synthetic-deepseek-key"
    delete_api_key("DEEPSEEK_API_KEY", "https://b.example/v1")
    assert backend.passwords == {}


@pytest.mark.parametrize("api_key", ["", " \t\n"])
def test_store_rejects_empty_key(backend: FakeKeyring, api_key: str) -> None:
    with pytest.raises(ValueError, match=r"^empty api key$"):
        store_api_key("OPENAI_API_KEY", "https://a.example/v1", api_key)

    assert backend.passwords == {}


@pytest.mark.parametrize("module,name", [
    ("keyrings.alt.file", "PlaintextKeyring"),
    ("keyring.backends.fail", "Keyring"),
    ("third_party", "WinVaultKeyring"),
    ("keyring.backends.macOS", "OtherKeyring"),
    ("keyring.backends.Windows", "OtherKeyring"),
])
@pytest.mark.parametrize("operation", ["store", "load", "delete"])
def test_non_native_backend_is_rejected(
    monkeypatch: pytest.MonkeyPatch, module: str, name: str, operation: str,
) -> None:
    # No credential methods: rejecting the type must precede any credential access.
    fake = type(name, (), {"__module__": module})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: fake)

    with pytest.raises(CredentialError, match=r"^credential store unavailable$"):
        if operation == "store":
            store_api_key("OPENAI_API_KEY", "https://a.example/v1", "synthetic-api-key")
        elif operation == "load":
            load_api_key("OPENAI_API_KEY", "https://a.example/v1")
        else:
            delete_api_key("OPENAI_API_KEY", "https://a.example/v1")


def test_load_missing_key(backend: FakeKeyring) -> None:
    with pytest.raises(CredentialError, match=r"^api key not found$"):
        load_api_key("OPENAI_API_KEY", "https://a.example/v1")


def test_delete_missing_key_succeeds(backend: FakeKeyring) -> None:
    delete_api_key("OPENAI_API_KEY", "https://a.example/v1")

    assert backend.passwords == {}


@pytest.mark.parametrize("error_type", [KeyringError, OSError, RuntimeError, ValueError])
@pytest.mark.parametrize("operation,failure", [
    ("store", "set"),
    ("load", "get"),
    ("delete", "get"),
    ("delete", "delete"),
    ("store", "backend"),
    ("load", "backend"),
    ("delete", "backend"),
])
def test_keyring_errors_are_fixed_and_do_not_expose_key(
    backend: FakeKeyring, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str,
    error_type: type[Exception],
) -> None:
    api_key = "synthetic-api-key"
    backend.passwords[SERVICE, "OPENAI_API_KEY|https://a.example/v1"] = api_key
    backend.failure = failure
    backend.error_type = error_type

    if failure == "backend":
        def fail_backend() -> FakeKeyring:
            raise error_type(api_key)

        monkeypatch.setattr(keyring, "get_keyring", fail_backend)

    with pytest.raises(CredentialError, match=r"^credential store unavailable$") as captured:
        if operation == "store":
            store_api_key("OPENAI_API_KEY", "https://a.example/v1", api_key)
        elif operation == "load":
            load_api_key("OPENAI_API_KEY", "https://a.example/v1")
        else:
            delete_api_key("OPENAI_API_KEY", "https://a.example/v1")

    assert api_key not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__


@pytest.mark.parametrize("operation", ["store", "load", "delete"])
def test_invalid_address_is_not_credential_backend_failure(
    backend: FakeKeyring, operation: str,
) -> None:
    address = "https://[synthetic"
    with pytest.raises(ValueError, match=r"^Invalid IPv6 URL$"):
        if operation == "store":
            store_api_key("OPENAI_API_KEY", address, "synthetic-api-key")
        elif operation == "load":
            load_api_key("OPENAI_API_KEY", address)
        else:
            delete_api_key("OPENAI_API_KEY", address)


def test_same_environment_keys_are_isolated_by_address(backend: FakeKeyring) -> None:
    store_api_key("OPENAI_API_KEY", "https://a.example/v1", "synthetic-a")
    store_api_key("OPENAI_API_KEY", "https://b.example/v1", "synthetic-b")

    assert load_api_key("OPENAI_API_KEY", "https://a.example/v1") == "synthetic-a"
    assert load_api_key("OPENAI_API_KEY", "https://b.example/v1") == "synthetic-b"
    delete_api_key("OPENAI_API_KEY", "https://a.example/v1")
    assert load_api_key("OPENAI_API_KEY", "https://b.example/v1") == "synthetic-b"


@pytest.mark.parametrize("address", [
    "https://a.example/v1", "https://a.example/v1/", "  HTTPS://A.EXAMPLE/v1/// ",
])
def test_address_normalization_applies_to_all_operations(
    backend: FakeKeyring, address: str,
) -> None:
    store_api_key("OPENAI_API_KEY", address, "synthetic-a")
    assert backend.passwords == {(SERVICE, "OPENAI_API_KEY|https://a.example/v1"): "synthetic-a"}
    assert load_api_key("OPENAI_API_KEY", address) == "synthetic-a"
    assert load_api_key("OPENAI_API_KEY", "https://a.example/v1") == "synthetic-a"
    delete_api_key("OPENAI_API_KEY", address)
    assert backend.passwords == {}


def test_legacy_account_is_not_read_or_migrated(backend: FakeKeyring) -> None:
    backend.passwords[SERVICE, "OPENAI_API_KEY"] = "synthetic-legacy"
    with pytest.raises(CredentialError, match=r"^api key not found$"):
        load_api_key("OPENAI_API_KEY", "https://a.example/v1")
    delete_api_key("OPENAI_API_KEY", "https://a.example/v1")
    assert backend.passwords == {(SERVICE, "OPENAI_API_KEY"): "synthetic-legacy"}


@pytest.mark.parametrize(("address_a", "address_b"), [
    ("https://api.example.com/teamA/v1", "https://api.example.com/teama/v1"),
    ("https://api.example.com/v1?team=A", "https://api.example.com/v1?team=a"),
    ("https://api.example.com/v1?team=A/", "https://api.example.com/v1?team=A"),
])
def test_path_and_query_are_preserved_in_credential_accounts(
    backend: FakeKeyring, address_a: str, address_b: str,
) -> None:
    store_api_key("OPENAI_API_KEY", address_a, "synthetic-a")
    store_api_key("OPENAI_API_KEY", address_b, "synthetic-b")

    assert load_api_key("OPENAI_API_KEY", address_a) == "synthetic-a"
    assert load_api_key("OPENAI_API_KEY", address_b) == "synthetic-b"
    assert backend.passwords == {
        (SERVICE, f"OPENAI_API_KEY|{address_a}"): "synthetic-a",
        (SERVICE, f"OPENAI_API_KEY|{address_b}"): "synthetic-b",
    }
    delete_api_key("OPENAI_API_KEY", address_a)
    assert load_api_key("OPENAI_API_KEY", address_b) == "synthetic-b"


@pytest.mark.parametrize(("address", "normalized"), [
    ("  HTTPS://API.Example.com/v1/  ", "https://api.example.com/v1"),
    ("HTTPS://API.Example.com/teamA/v1/?team=AbC/", "https://api.example.com/teamA/v1?team=AbC/"),
])
def test_scheme_host_and_trailing_path_slash_normalize_to_same_account(
    backend: FakeKeyring, address: str, normalized: str,
) -> None:
    store_api_key("OPENAI_API_KEY", address, "synthetic-a")
    assert backend.passwords == {(SERVICE, f"OPENAI_API_KEY|{normalized}"): "synthetic-a"}
    assert load_api_key("OPENAI_API_KEY", normalized) == "synthetic-a"
    delete_api_key("OPENAI_API_KEY", address)
    assert backend.passwords == {}
