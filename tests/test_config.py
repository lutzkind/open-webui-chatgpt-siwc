from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings


def valid_settings(**overrides):
    values = {
        "ADAPTER_API_KEY": "synthetic-adapter-key-" + "x" * 40,
        "SIWC_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "SIWC_HOST_ID": "00000000-0000-4000-8000-000000000001",
        "CONNECT_ADMIN_EMAILS": "Admin@example.test",
        "OPEN_WEBUI_PUBLIC_ORIGIN": "https://open-webui.example.test",
        "CONNECT_PUBLIC_URL": "https://open-webui.example.test/siwc/connect",
    }
    values.update(overrides)
    return Settings(**values)


def test_valid_configuration_normalizes_host_identity_and_admin_email():
    settings = valid_settings(
        SIWC_HOST_ID="URN:UUID:00000000-0000-4000-8000-000000000001",
        CONNECT_ADMIN_EMAILS="Admin@example.test; SECOND@example.test",
    )
    assert settings.SIWC_HOST_ID.get_secret_value() == "urn:uuid:00000000-0000-4000-8000-000000000001"
    assert settings.CONNECT_ADMIN_EMAILS.get_secret_value() == "admin@example.test,second@example.test"
    assert settings.OPENAI_API_BASE_URL == "https://api.openai.com/v1"


def test_connect_url_is_derived_from_public_origin_environment(monkeypatch, tmp_path):
    values = {
        "ADAPTER_API_KEY": "synthetic-adapter-key-" + "x" * 40,
        "SIWC_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "SIWC_HOST_ID": "00000000-0000-4000-8000-000000000001",
        "CONNECT_ADMIN_EMAILS": "admin@example.test",
        "OPEN_WEBUI_PUBLIC_ORIGIN": "https://open-webui.example.test",
        "DATA_DIR": str(tmp_path),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.CONNECT_PUBLIC_URL.get_secret_value() == "https://open-webui.example.test/siwc/connect"
    assert settings.ADAPTER_INTERNAL_URL == "http://siwc-adapter:8080/v1"


def test_legacy_environment_names_remain_supported(monkeypatch, tmp_path):
    values = {
        "ADAPTER_API_KEY": "synthetic-adapter-key-" + "x" * 40,
        "SIWC_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "SIWC_HOST_ID": "00000000-0000-4000-8000-000000000001",
        "CONNECT_ADMIN_EMAILS": "admin@example.test",
        "CONNECT_ORIGIN": "https://open-webui.example.test",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "DATA_DIR": str(tmp_path),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPEN_WEBUI_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE_URL", raising=False)
    settings = Settings()
    assert settings.OPEN_WEBUI_PUBLIC_ORIGIN.get_secret_value() == "https://open-webui.example.test"
    assert settings.CONNECT_PUBLIC_URL.get_secret_value() == "https://open-webui.example.test/siwc/connect"
    assert settings.OPENAI_API_BASE_URL == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ADAPTER_API_KEY", "too-short", "at least 32 characters"),
        ("SIWC_CREDENTIAL_KEY", "not-a-fernet-key", "valid Fernet key"),
        ("SIWC_HOST_ID", "not-a-uuid", "must be a UUID"),
        ("OPEN_WEBUI_PUBLIC_ORIGIN", "http://open-webui.example.test", "HTTPS is required"),
        ("CONNECT_PUBLIC_URL", "https://wrong.example.test/siwc/connect", "must use OPEN_WEBUI_PUBLIC_ORIGIN"),
        ("CONNECT_PUBLIC_URL", "https://open-webui.example.test/other", "must use the /siwc/connect path"),
        ("CONNECT_ADMIN_EMAILS", "not-an-email", "valid email addresses"),
        ("OPENAI_API_BASE_URL", "https://api.example.test/v1", "current SIWC resource"),
        ("OPENAI_AUTH_BASE_URL", "https://auth.example.test", "must be https://auth.openai.com"),
    ],
)
def test_invalid_configuration_fails_with_a_specific_message(field, value, message):
    with pytest.raises(ValidationError, match=message):
        valid_settings(**{field: value})
