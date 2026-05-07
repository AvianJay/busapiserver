from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import secrets
import threading
import time
from typing import Any, Literal
from urllib.parse import urlencode, urlparse, urlunparse
import uuid

import requests

from app.config import Settings
from app.db import get_connection


AuthProvider = Literal["discord", "google"]
AuthPlatform = Literal["web", "app"]

ALLOWED_ROLES = {"admin", "mod", "user"}
DEFAULT_WEB_REDIRECT_URI = "https://busapp.avianjay.sbs/auth-callback"
DEFAULT_APP_REDIRECT_URI = "yabus://auth-callback"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ME_URL = "https://openidconnect.googleapis.com/v1/userinfo"
AUTH_REQUEST_TIMEOUT_SECONDS = 10
SNOWFLAKE_EPOCH_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


class AuthError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        redirect_uri: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.redirect_uri = redirect_uri


@dataclass(frozen=True)
class AuthPrincipal:
    account_id: int
    device_id: int
    token_id: int
    role: str


@dataclass(frozen=True)
class OAuthIdentity:
    provider: AuthProvider
    provider_user_id: str
    email: str | None
    display_name: str | None
    avatar_url: str | None


@dataclass(frozen=True)
class OAuthState:
    provider: AuthProvider
    platform: AuthPlatform
    redirect_uri: str
    device_key: str


@dataclass(frozen=True)
class AuthLoginResult:
    account_id: int
    device_id: int
    role: str
    token: str
    provider: AuthProvider
    display_name: str | None
    redirect_uri: str


class SnowflakeGenerator:
    def __init__(self, node_id: int) -> None:
        if node_id < 0 or node_id > 1023:
            raise ValueError("Snowflake node id must be between 0 and 1023.")
        self._node_id = node_id
        self._lock = threading.Lock()
        self._last_ms = -1
        self._sequence = 0

    def next_id(self) -> int:
        with self._lock:
            now_ms = int(time.time() * 1000)
            if now_ms < self._last_ms:
                now_ms = self._last_ms
            if now_ms == self._last_ms:
                self._sequence = (self._sequence + 1) & 0xFFF
                if self._sequence == 0:
                    while now_ms <= self._last_ms:
                        now_ms = int(time.time() * 1000)
            else:
                self._sequence = 0
            self._last_ms = now_ms
            return ((now_ms - SNOWFLAKE_EPOCH_MS) << 22) | (self._node_id << 12) | self._sequence


_snowflake_generators: dict[int, SnowflakeGenerator] = {}
_snowflake_generators_lock = threading.Lock()


def generate_snowflake(settings: Settings) -> int:
    node_id = settings.auth_snowflake_node_id
    with _snowflake_generators_lock:
        generator = _snowflake_generators.get(node_id)
        if generator is None:
            generator = SnowflakeGenerator(node_id)
            _snowflake_generators[node_id] = generator
    return generator.next_id()


def default_redirect_uri(platform: AuthPlatform) -> str:
    return DEFAULT_WEB_REDIRECT_URI if platform == "web" else DEFAULT_APP_REDIRECT_URI


def validate_platform(value: str) -> AuthPlatform:
    normalized = value.strip().lower()
    if normalized in {"web", "app"}:
        return normalized  # type: ignore[return-value]
    raise AuthError("Invalid auth platform.")


def validate_provider(value: str) -> AuthProvider:
    normalized = value.strip().lower()
    if normalized in {"discord", "google"}:
        return normalized  # type: ignore[return-value]
    raise AuthError("Invalid auth provider.")


def normalize_redirect_uri(value: str | None, platform: AuthPlatform) -> str:
    redirect_uri = (value or "").strip() or default_redirect_uri(platform)
    parsed = urlparse(redirect_uri)
    if platform == "app":
        if parsed.scheme.lower() != "yabus" or not parsed.netloc:
            raise AuthError("Invalid app redirect URI.")
        return redirect_uri

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "busapp.avianjay.sbs"
        or not parsed.path.startswith("/")
    ):
        raise AuthError("Invalid web redirect URI.")
    return redirect_uri


def validate_device_key(value: str) -> str:
    cleaned = value.strip()
    try:
        parsed = uuid.UUID(cleaned)
    except ValueError as exc:
        raise AuthError("Invalid device key.") from exc
    if parsed.version != 4:
        raise AuthError("Device key must be a UUIDv4 value.")
    return str(parsed)


def callback_url(settings: Settings, provider: AuthProvider) -> str:
    return f"{settings.auth_public_base_url}/api/v1/auth/{provider}-callback"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _base64_segment(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _build_token(account_id: int, issued_at: int) -> str:
    return ".".join(
        (
            _base64_segment(str(account_id)),
            _base64_segment(str(issued_at)),
            secrets.token_urlsafe(32),
        )
    )


def create_oauth_state(
    settings: Settings,
    *,
    provider: AuthProvider,
    platform: AuthPlatform,
    redirect_uri: str,
    device_key: str,
) -> str:
    now = int(time.time())
    state = secrets.token_urlsafe(32)
    with get_connection(settings.db_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_oauth_states
                (state_hash, provider, platform, redirect_uri, device_key, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _sha256(state),
                provider,
                platform,
                redirect_uri,
                device_key,
                now,
                now + settings.auth_state_ttl_seconds,
            ),
        )
        connection.execute(
            "DELETE FROM auth_oauth_states WHERE expires_at < ? OR used_at IS NOT NULL",
            (now - settings.auth_state_ttl_seconds,),
        )
        connection.commit()
    return state


def consume_oauth_state(
    settings: Settings,
    *,
    provider: AuthProvider,
    state: str,
) -> OAuthState:
    now = int(time.time())
    with get_connection(settings.db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT provider, platform, redirect_uri, device_key, expires_at, used_at
                FROM auth_oauth_states
                WHERE state_hash = ?
                """,
                (_sha256(state),),
            ).fetchone()
            if row is None:
                raise AuthError("Invalid OAuth state.")
            if row["provider"] != provider:
                raise AuthError("OAuth state provider mismatch.")
            if row["used_at"] is not None:
                raise AuthError("OAuth state was already used.")
            if int(row["expires_at"]) < now:
                raise AuthError("OAuth state expired.")
            connection.execute(
                "UPDATE auth_oauth_states SET used_at = ? WHERE state_hash = ?",
                (now, _sha256(state)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return OAuthState(
        provider=provider,
        platform=row["platform"],
        redirect_uri=row["redirect_uri"],
        device_key=row["device_key"],
    )


def build_authorization_url(
    settings: Settings,
    *,
    provider: AuthProvider,
    state: str,
) -> str:
    redirect_uri = callback_url(settings, provider)
    if provider == "discord":
        if not settings.discord_oauth_client_id:
            raise AuthError("Discord OAuth client id is not configured.", status_code=500)
        return f"{DISCORD_AUTHORIZE_URL}?{urlencode({
            'client_id': settings.discord_oauth_client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'identify email',
            'state': state,
        })}"

    if not settings.google_oauth_client_id:
        raise AuthError("Google OAuth client id is not configured.", status_code=500)
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode({
        'client_id': settings.google_oauth_client_id,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    })}"


def complete_oauth_login(
    settings: Settings,
    *,
    provider: AuthProvider,
    code: str,
    state: str,
) -> AuthLoginResult:
    oauth_state = consume_oauth_state(settings, provider=provider, state=state)
    try:
        identity = _exchange_code_for_identity(
            settings,
            provider=provider,
            code=code,
            redirect_uri=callback_url(settings, provider),
        )
    except AuthError as exc:
        exc.redirect_uri = oauth_state.redirect_uri
        raise
    return _upsert_login(
        settings,
        identity=identity,
        device_key=oauth_state.device_key,
        redirect_uri=oauth_state.redirect_uri,
    )


def _exchange_code_for_identity(
    settings: Settings,
    *,
    provider: AuthProvider,
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    if provider == "discord":
        return _exchange_discord_code(settings, code=code, redirect_uri=redirect_uri)
    return _exchange_google_code(settings, code=code, redirect_uri=redirect_uri)


def _exchange_discord_code(
    settings: Settings,
    *,
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    if not settings.discord_oauth_client_id or not settings.discord_oauth_client_secret:
        raise AuthError("Discord OAuth credentials are not configured.", status_code=500)
    token_response = requests.post(
        DISCORD_TOKEN_URL,
        data={
            "client_id": settings.discord_oauth_client_id,
            "client_secret": settings.discord_oauth_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=AUTH_REQUEST_TIMEOUT_SECONDS,
    )
    if token_response.status_code != 200:
        raise AuthError("Discord OAuth token exchange failed.", status_code=502)
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise AuthError("Discord OAuth token response was invalid.", status_code=502)

    profile_response = requests.get(
        DISCORD_ME_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        timeout=AUTH_REQUEST_TIMEOUT_SECONDS,
    )
    if profile_response.status_code != 200:
        raise AuthError("Discord profile fetch failed.", status_code=502)
    profile = profile_response.json()
    user_id = str(profile.get("id") or "").strip()
    if not user_id:
        raise AuthError("Discord profile response was invalid.", status_code=502)
    avatar_hash = profile.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
        if avatar_hash
        else None
    )
    display_name = profile.get("global_name") or profile.get("username")
    return OAuthIdentity(
        provider="discord",
        provider_user_id=user_id,
        email=profile.get("email"),
        display_name=str(display_name) if display_name else None,
        avatar_url=avatar_url,
    )


def _exchange_google_code(
    settings: Settings,
    *,
    code: str,
    redirect_uri: str,
) -> OAuthIdentity:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise AuthError("Google OAuth credentials are not configured.", status_code=500)
    token_response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=AUTH_REQUEST_TIMEOUT_SECONDS,
    )
    if token_response.status_code != 200:
        raise AuthError("Google OAuth token exchange failed.", status_code=502)
    access_token = token_response.json().get("access_token")
    if not access_token:
        raise AuthError("Google OAuth token response was invalid.", status_code=502)

    profile_response = requests.get(
        GOOGLE_ME_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        timeout=AUTH_REQUEST_TIMEOUT_SECONDS,
    )
    if profile_response.status_code != 200:
        raise AuthError("Google profile fetch failed.", status_code=502)
    profile = profile_response.json()
    user_id = str(profile.get("sub") or "").strip()
    if not user_id:
        raise AuthError("Google profile response was invalid.", status_code=502)
    return OAuthIdentity(
        provider="google",
        provider_user_id=user_id,
        email=profile.get("email"),
        display_name=profile.get("name"),
        avatar_url=profile.get("picture"),
    )


def _upsert_login(
    settings: Settings,
    *,
    identity: OAuthIdentity,
    device_key: str,
    redirect_uri: str,
) -> AuthLoginResult:
    now = int(time.time())
    with get_connection(settings.db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            identity_row = connection.execute(
                """
                SELECT account_id
                FROM account_oauth_identities
                WHERE provider = ? AND provider_user_id = ?
                """,
                (identity.provider, identity.provider_user_id),
            ).fetchone()
            device_row = connection.execute(
                """
                SELECT id, account_id
                FROM account_devices
                WHERE device_key = ?
                """,
                (device_key,),
            ).fetchone()

            if identity_row is not None:
                account_id = int(identity_row["account_id"])
            elif device_row is not None:
                account_id = int(device_row["account_id"])
            else:
                account_id = generate_snowflake(settings)
                connection.execute(
                    """
                    INSERT INTO accounts (id, role, created_at, updated_at)
                    VALUES (?, 'user', ?, ?)
                    """,
                    (account_id, now, now),
                )

            connection.execute(
                """
                INSERT INTO account_oauth_identities
                    (provider, provider_user_id, account_id, email, display_name, avatar_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    email = excluded.email,
                    display_name = excluded.display_name,
                    avatar_url = excluded.avatar_url,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.provider,
                    identity.provider_user_id,
                    account_id,
                    identity.email,
                    identity.display_name,
                    identity.avatar_url,
                    now,
                    now,
                ),
            )

            if device_row is None:
                device_id = generate_snowflake(settings)
                connection.execute(
                    """
                    INSERT INTO account_devices (id, account_id, device_key, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (device_id, account_id, device_key, now, now),
                )
            else:
                device_id = int(device_row["id"])
                connection.execute(
                    """
                    UPDATE account_devices
                    SET account_id = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (account_id, now, device_id),
                )

            connection.execute(
                """
                UPDATE account_device_tokens
                SET revoked_at = ?
                WHERE device_id = ? AND revoked_at IS NULL
                """,
                (now, device_id),
            )
            token = _build_token(account_id, now)
            token_id = generate_snowflake(settings)
            connection.execute(
                """
                INSERT INTO account_device_tokens
                    (id, account_id, device_id, token_hash, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token_id, account_id, device_id, _sha256(token), now, now),
            )
            account_row = connection.execute(
                "SELECT role FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            role = account_row["role"] if account_row is not None else "user"
            connection.execute(
                "UPDATE accounts SET updated_at = ? WHERE id = ?",
                (now, account_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return AuthLoginResult(
        account_id=account_id,
        device_id=device_id,
        role=role,
        token=token,
        provider=identity.provider,
        display_name=identity.display_name,
        redirect_uri=redirect_uri,
    )


def build_login_redirect_url(redirect_uri: str, result: AuthLoginResult) -> str:
    parsed = urlparse(redirect_uri)
    payload = urlencode(
        {
            "token": result.token,
            "account_id": str(result.account_id),
            "device_id": str(result.device_id),
            "role": result.role,
            "provider": result.provider,
            "display_name": result.display_name or "",
        }
    )
    fragment = f"{parsed.fragment}&{payload}" if parsed.fragment else payload
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            fragment,
        )
    )


def build_error_redirect_url(redirect_uri: str, message: str) -> str:
    parsed = urlparse(redirect_uri)
    payload = urlencode({"error": message})
    fragment = f"{parsed.fragment}&{payload}" if parsed.fragment else payload
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            fragment,
        )
    )


def authenticate_token(settings: Settings, token: str) -> AuthPrincipal | None:
    cleaned = token.strip()
    if not cleaned:
        return None
    now = int(time.time())
    with get_connection(settings.db_path) as connection:
        row = connection.execute(
            """
            SELECT
                t.id AS token_id,
                t.account_id AS account_id,
                t.device_id AS device_id,
                a.role AS role
            FROM account_device_tokens t
            JOIN accounts a ON a.id = t.account_id
            JOIN account_devices d ON d.id = t.device_id
            WHERE t.token_hash = ?
              AND t.revoked_at IS NULL
            LIMIT 1
            """,
            (_sha256(cleaned),),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE account_device_tokens
            SET last_used_at = ?
            WHERE id = ?
            """,
            (now, int(row["token_id"])),
        )
        connection.execute(
            "UPDATE account_devices SET last_seen_at = ? WHERE id = ?",
            (now, int(row["device_id"])),
        )
        connection.commit()
    return AuthPrincipal(
        account_id=int(row["account_id"]),
        device_id=int(row["device_id"]),
        token_id=int(row["token_id"]),
        role=row["role"],
    )


def revoke_token(settings: Settings, principal: AuthPrincipal) -> None:
    now = int(time.time())
    with get_connection(settings.db_path) as connection:
        connection.execute(
            """
            UPDATE account_device_tokens
            SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (now, principal.token_id),
        )
        connection.commit()


def account_payload(settings: Settings, principal: AuthPrincipal) -> dict[str, Any]:
    with get_connection(settings.db_path) as connection:
        identity_rows = connection.execute(
            """
            SELECT provider, provider_user_id, email, display_name, avatar_url
            FROM account_oauth_identities
            WHERE account_id = ?
            ORDER BY provider
            """,
            (principal.account_id,),
        ).fetchall()
        device_row = connection.execute(
            """
            SELECT device_key, created_at, last_seen_at
            FROM account_devices
            WHERE id = ?
            """,
            (principal.device_id,),
        ).fetchone()
    return {
        "account_id": str(principal.account_id),
        "device_id": str(principal.device_id),
        "role": principal.role,
        "device": {
            "device_key": device_row["device_key"] if device_row else None,
            "created_at": int(device_row["created_at"]) if device_row else None,
            "last_seen_at": int(device_row["last_seen_at"]) if device_row else None,
        },
        "identities": [
            {
                "provider": row["provider"],
                "provider_user_id": row["provider_user_id"],
                "email": row["email"],
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
            }
            for row in identity_rows
        ],
    }
