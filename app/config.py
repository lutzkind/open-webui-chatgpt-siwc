from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOST_UUID_PATTERN = re.compile(
    r"^(?:urn:uuid:)?([0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
_EMAIL_PATTERN = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")
_HEADER_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def canonical_host_id(value: str) -> str:
    match = _HOST_UUID_PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("SIWC_HOST_ID must be a UUID or urn:uuid UUID.")
    return f"urn:uuid:{match.group(1).lower()}"


def _validated_url(value: str, *, origin_only: bool = False) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise ValueError("Configuration URL is invalid.") from None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Configuration URL must use HTTPS and include a hostname.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Configuration URL must not contain credentials, query, or fragment data.")
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise ValueError("HTTPS is required except for a loopback development address.")
    if origin_only and parsed.path not in {"", "/"}:
        raise ValueError("Open WebUI public origin must not contain a path.")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = "" if origin_only else parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="", case_sensitive=True, populate_by_name=True, extra="ignore"
    )

    ADAPTER_API_KEY: SecretStr
    SIWC_CREDENTIAL_KEY: SecretStr
    SIWC_HOST_ID: SecretStr
    CONNECT_ADMIN_EMAILS: SecretStr
    OPEN_WEBUI_PUBLIC_ORIGIN: SecretStr = Field(
        validation_alias=AliasChoices("OPEN_WEBUI_PUBLIC_ORIGIN", "CONNECT_ORIGIN")
    )
    CONNECT_PUBLIC_URL: SecretStr
    ADAPTER_INTERNAL_URL: str = "http://siwc-adapter:8080/v1"

    TRUSTED_EMAIL_HEADER: str = "X-Auth-Request-Email"
    DATA_DIR: Path = Path("/data")
    OPENAI_API_BASE_URL: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("OPENAI_API_BASE_URL", "OPENAI_BASE_URL"),
    )
    OPENAI_AUTH_BASE_URL: str = "https://auth.openai.com"
    CONNECT_COMPANION_DOWNLOAD_URL: str = (
        "https://github.com/lutzkind/open-webui-chatgpt-siwc/"
        "releases/latest/download/Connect-ChatGPT-macOS.zip"
    )
    TOKEN_REFRESH_SKEW_SECONDS: int = 300
    REQUEST_TIMEOUT_SECONDS: float = 120.0

    @field_validator("ADAPTER_API_KEY")
    @classmethod
    def validate_adapter_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("ADAPTER_API_KEY must contain at least 32 characters.")
        return value

    @field_validator("SIWC_CREDENTIAL_KEY")
    @classmethod
    def validate_credential_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode("ascii"))
        except (UnicodeEncodeError, ValueError):
            raise ValueError("SIWC_CREDENTIAL_KEY must be a valid Fernet key.") from None
        return value

    @field_validator("SIWC_HOST_ID")
    @classmethod
    def validate_host_id(cls, value: SecretStr) -> SecretStr:
        return SecretStr(canonical_host_id(value.get_secret_value()))

    @field_validator("CONNECT_ADMIN_EMAILS")
    @classmethod
    def validate_admin_emails(cls, value: SecretStr) -> SecretStr:
        emails = [part.strip() for part in re.split(r"[;,]", value.get_secret_value()) if part.strip()]
        if not emails or any(not _EMAIL_PATTERN.fullmatch(email) for email in emails):
            raise ValueError("CONNECT_ADMIN_EMAILS must be a comma-separated list of valid email addresses.")
        return SecretStr(",".join(email.casefold() for email in emails))

    @field_validator("OPEN_WEBUI_PUBLIC_ORIGIN")
    @classmethod
    def validate_webui_origin(cls, value: SecretStr) -> SecretStr:
        return SecretStr(_validated_url(value.get_secret_value(), origin_only=True))

    @field_validator("CONNECT_PUBLIC_URL")
    @classmethod
    def validate_connect_url(cls, value: SecretStr) -> SecretStr:
        normalized = _validated_url(value.get_secret_value())
        if urlsplit(normalized).path != "/siwc/connect":
            raise ValueError("CONNECT_PUBLIC_URL must use the /siwc/connect path.")
        return SecretStr(normalized)

    @field_validator("ADAPTER_INTERNAL_URL")
    @classmethod
    def validate_internal_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value.strip())
            port = parsed.port
        except ValueError:
            raise ValueError("ADAPTER_INTERNAL_URL is invalid.") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ADAPTER_INTERNAL_URL must be an HTTP service URL without credentials or query data.")
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host if port is None else f"{host}:{port}"
        normalized = f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"
        if not urlsplit(normalized).path.endswith("/v1"):
            raise ValueError("ADAPTER_INTERNAL_URL must end in /v1.")
        return normalized

    @field_validator("TRUSTED_EMAIL_HEADER")
    @classmethod
    def validate_identity_header(cls, value: str) -> str:
        if not _HEADER_PATTERN.fullmatch(value):
            raise ValueError("TRUSTED_EMAIL_HEADER must be a valid HTTP header name.")
        return value

    @field_validator("OPENAI_API_BASE_URL")
    @classmethod
    def validate_openai_api_url(cls, value: str) -> str:
        normalized = _validated_url(value)
        if normalized != "https://api.openai.com/v1":
            raise ValueError(
                "OPENAI_API_BASE_URL must be https://api.openai.com/v1 for the current SIWC resource."
            )
        return normalized

    @field_validator("OPENAI_AUTH_BASE_URL")
    @classmethod
    def validate_openai_auth_url(cls, value: str) -> str:
        normalized = _validated_url(value, origin_only=True)
        if normalized != "https://auth.openai.com":
            raise ValueError("OPENAI_AUTH_BASE_URL must be https://auth.openai.com for SIWC.")
        return normalized

    @field_validator("CONNECT_COMPANION_DOWNLOAD_URL")
    @classmethod
    def validate_companion_url(cls, value: str) -> str:
        normalized = _validated_url(value)
        if not normalized.endswith(".zip"):
            raise ValueError("CONNECT_COMPANION_DOWNLOAD_URL must point to a ZIP archive.")
        return normalized

    @field_validator("DATA_DIR")
    @classmethod
    def validate_data_dir(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("DATA_DIR must be an absolute path.")
        return value

    @model_validator(mode="after")
    def validate_origins_match(self) -> Settings:
        origin = self.OPEN_WEBUI_PUBLIC_ORIGIN.get_secret_value()
        connect = urlsplit(self.CONNECT_PUBLIC_URL.get_secret_value())
        if f"{connect.scheme}://{connect.netloc}" != origin:
            raise ValueError("CONNECT_PUBLIC_URL must use OPEN_WEBUI_PUBLIC_ORIGIN.")
        return self

    @model_validator(mode="before")
    @classmethod
    def derive_connect_url(cls, values):
        if isinstance(values, dict) and not values.get("CONNECT_PUBLIC_URL"):
            origin = values.get("OPEN_WEBUI_PUBLIC_ORIGIN") or values.get("CONNECT_ORIGIN")
            if isinstance(origin, SecretStr):
                origin = origin.get_secret_value()
            if isinstance(origin, str) and origin.strip():
                values["CONNECT_PUBLIC_URL"] = _validated_url(origin, origin_only=True) + "/siwc/connect"
        return values

    @property
    def encrypted_credentials_path(self) -> Path:
        return self.DATA_DIR / "credentials.enc"


@lru_cache
def get_settings() -> Settings:
    return Settings()
