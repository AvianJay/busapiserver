from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
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
DEFAULT_WEB_REDIRECT_URI = "https://busapp.avianjay.sbs/"
DEFAULT_APP_REDIRECT_URI = "yabus://auth-callback"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
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


@dataclass(frozen=True)
class AuthLinkResult:
    account_id: int
    provider: AuthProvider
    status: Literal["linked", "already_linked", "merge_required"]
    redirect_uri: str
    merge_token: str | None = None


@dataclass(frozen=True)
class AuthFlowResult:
    kind: Literal["login", "link"]
    redirect_uri: str
    login_result: AuthLoginResult | None = None
    link_result: AuthLinkResult | None = None


@dataclass(frozen=True)
class AccountIdentitySummary:
    provider: AuthProvider
    email: str | None
    display_name: str | None


@dataclass(frozen=True)
class PendingAccountMergePreview:
    merge_token: str
    provider: AuthProvider
    target_account_id: int
    source_account_id: int
    expires_at: int
    source_identities: tuple[AccountIdentitySummary, ...]
    active_device_count: int


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
_ROLE_PRIORITY = {"user": 0, "mod": 1, "admin": 2}


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


def validate_role(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in ALLOWED_ROLES:
        raise AuthError("Invalid account role.", status_code=400)
    return normalized


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


_DEVICE_NAME_APP_UA_PATTERN = re.compile(
    r"^YABus/[^\s()]+ \((?P<platform>[^)]+)\)\s*$"
)
_DEVICE_NAME_BROWSER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"Edg(?:A|iOS)?/(?P<ver>\d+)")),
    ("Opera", re.compile(r"OPR/(?P<ver>\d+)")),
    ("Chrome", re.compile(r"Chrome/(?P<ver>\d+)")),
    ("Firefox", re.compile(r"Firefox/(?P<ver>\d+)")),
    ("Safari", re.compile(r"Version/(?P<ver>[\d.]+).+Safari/")),
)
_DEVICE_NAME_ANDROID_VER = re.compile(r"Android (?P<ver>\d+)")
_DEVICE_NAME_IOS_VER = re.compile(
    r"(?:iPhone|iPad|iPod)(?: OS|; CPU(?: iPhone)? OS) (?P<ver>\d+)", re.IGNORECASE,
)
_DEVICE_NAME_WINDOWS_NT = re.compile(r"Windows NT (?P<ver>[\d.]+)")
_DEVICE_NAME_MACOS_VER = re.compile(r"Mac OS X (?P<ver>[\d_]+)")

_WINDOWS_NT_DISPLAY: dict[str, str] = {
    "10.0": "10/11",
    "6.3": "8.1",
    "6.2": "8",
    "6.1": "7",
}


def generate_device_name_from_user_agent(user_agent: str | None) -> str | None:
    """Derive a short, human-readable device name from a User-Agent header.

    Examples:
        Browser:  "Chrome Windows 137", "Safari macOS", "Firefox Android 138"
        App UA:   "Android (App)", "iPhone (App)", "iPad (App)"
    """
    normalized = (user_agent or "").strip()
    if not normalized:
        return None

    # YABus app user-agent: "YABus/1.0.0-abc1234 (android)"
    app_match = _DEVICE_NAME_APP_UA_PATTERN.match(normalized)
    if app_match:
        platform = app_match.group("platform").strip().lower()
        _APP_PLATFORM_NAMES = {
            "android": "Android",
            "ios": "iPhone",
            "macos": "macOS",
            "windows": "Windows",
            "linux": "Linux",
            "web": "Web",
        }
        display = _APP_PLATFORM_NAMES.get(platform, platform.title())
        return f"{display} (App)"

    # Browser user-agent
    browser_name = None
    browser_ver = None
    for name, pattern in _DEVICE_NAME_BROWSER_PATTERNS:
        match = pattern.search(normalized)
        if match:
            browser_name = name
            browser_ver = match.group("ver")
            break

    if not browser_name:
        return None

    # Detect OS
    os_name = None
    android_match = _DEVICE_NAME_ANDROID_VER.search(normalized)
    if android_match:
        os_name = f"Android {android_match.group('ver')}"
    elif "android" in normalized.lower():
        os_name = "Android"
    else:
        ios_match = _DEVICE_NAME_IOS_VER.search(normalized)
        if ios_match:
            if "ipad" in normalized.lower():
                os_name = f"iPadOS {ios_match.group('ver')}"
            else:
                os_name = f"iOS {ios_match.group('ver')}"
        elif "ipad" in normalized.lower():
            os_name = "iPadOS"
        elif any(token in normalized.lower() for token in ("iphone", "ios")):
            os_name = "iOS"
        else:
            win_match = _DEVICE_NAME_WINDOWS_NT.search(normalized)
            if win_match:
                nt_ver = win_match.group("ver")
                os_name = f"Windows {_WINDOWS_NT_DISPLAY.get(nt_ver, nt_ver)}"
            elif "windows" in normalized.lower():
                os_name = "Windows"
            else:
                mac_match = _DEVICE_NAME_MACOS_VER.search(normalized)
                if mac_match:
                    os_name = "macOS"
                elif "mac os" in normalized.lower() or "macos" in normalized.lower():
                    os_name = "macOS"
                elif "cros" in normalized.lower() or "chrome os" in normalized.lower():
                    os_name = "ChromeOS"
                elif "linux" in normalized.lower():
                    os_name = "Linux"

    parts = [browser_name]
    if os_name:
        parts.append(os_name)
    if browser_ver:
        parts.append(browser_ver)
    return " ".join(parts)


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
    with get_connection(settings.app_db_path) as connection:
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


def create_link_oauth_state_context(
    settings: Settings,
    *,
    state: str,
    target_account_id: int,
) -> None:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_link_state_contexts
                (state_hash, target_account_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(state_hash) DO UPDATE SET
                target_account_id = excluded.target_account_id,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (
                _sha256(state),
                target_account_id,
                now,
                now + settings.auth_state_ttl_seconds,
            ),
        )
        connection.execute(
            "DELETE FROM auth_link_state_contexts WHERE expires_at < ?",
            (now,),
        )
        connection.commit()


def consume_oauth_state(
    settings: Settings,
    *,
    provider: AuthProvider,
    state: str,
) -> OAuthState:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
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


def _consume_link_oauth_state_context(
    settings: Settings,
    *,
    state: str,
) -> int | None:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        row = connection.execute(
            """
            SELECT target_account_id, expires_at
            FROM auth_link_state_contexts
            WHERE state_hash = ?
            """,
            (_sha256(state),),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "DELETE FROM auth_link_state_contexts WHERE state_hash = ?",
            (_sha256(state),),
        )
        connection.commit()
    if int(row["expires_at"]) < now:
        raise AuthError("OAuth state expired.")
    return int(row["target_account_id"])


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
    result = complete_oauth_flow(
        settings,
        provider=provider,
        code=code,
        state=state,
    )
    if result.login_result is None:
        raise AuthError("OAuth callback was not a login flow.", status_code=400)
    return result.login_result


def complete_oauth_flow(
    settings: Settings,
    *,
    provider: AuthProvider,
    code: str,
    state: str,
    user_agent: str | None = None,
) -> AuthFlowResult:
    oauth_state = consume_oauth_state(settings, provider=provider, state=state)
    target_account_id = _consume_link_oauth_state_context(settings, state=state)
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
    if target_account_id is None:
        device_name = generate_device_name_from_user_agent(user_agent)
        login_result = _upsert_login(
            settings,
            identity=identity,
            device_key=oauth_state.device_key,
            redirect_uri=oauth_state.redirect_uri,
            device_name=device_name,
        )
        return AuthFlowResult(
            kind="login",
            redirect_uri=oauth_state.redirect_uri,
            login_result=login_result,
        )

    link_result = link_oauth_identity(
        settings,
        target_account_id=target_account_id,
        identity=identity,
        redirect_uri=oauth_state.redirect_uri,
    )
    return AuthFlowResult(
        kind="link",
        redirect_uri=oauth_state.redirect_uri,
        link_result=link_result,
    )


def complete_google_native_login(
    settings: Settings,
    *,
    id_token: str,
    device_key: str,
    device_name: str | None = None,
) -> AuthLoginResult:
    normalized_device_key = validate_device_key(device_key)
    identity = _google_identity_from_id_token(settings, id_token=id_token)
    return _upsert_login(
        settings,
        identity=identity,
        device_key=normalized_device_key,
        redirect_uri=DEFAULT_APP_REDIRECT_URI,
        device_name=device_name,
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


def _allowed_google_id_token_audiences(settings: Settings) -> set[str]:
    audiences = set(settings.google_native_oauth_client_ids)
    if settings.google_oauth_client_id:
        audiences.add(settings.google_oauth_client_id)
    return {audience.strip() for audience in audiences if audience.strip()}


def _google_identity_from_id_token(settings: Settings, *, id_token: str) -> OAuthIdentity:
    cleaned_id_token = id_token.strip()
    if not cleaned_id_token:
        raise AuthError("Missing Google ID token.")

    allowed_audiences = _allowed_google_id_token_audiences(settings)
    if not allowed_audiences:
        raise AuthError("Google native OAuth client ids are not configured.", status_code=500)

    tokeninfo_response = requests.get(
        GOOGLE_TOKENINFO_URL,
        params={"id_token": cleaned_id_token},
        headers={"Accept": "application/json"},
        timeout=AUTH_REQUEST_TIMEOUT_SECONDS,
    )
    if tokeninfo_response.status_code != 200:
        raise AuthError("Google ID token verification failed.", status_code=401)

    claims = tokeninfo_response.json()
    issuer = str(claims.get("iss") or "").strip()
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise AuthError("Google ID token issuer was invalid.", status_code=401)

    audience = str(claims.get("aud") or "").strip()
    if audience not in allowed_audiences:
        raise AuthError("Google ID token audience was invalid.", status_code=401)

    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise AuthError("Google ID token subject was missing.", status_code=401)

    email_verified = claims.get("email_verified")
    if str(email_verified).lower() == "false":
        raise AuthError("Google account email is not verified.", status_code=401)

    return OAuthIdentity(
        provider="google",
        provider_user_id=user_id,
        email=claims.get("email"),
        display_name=claims.get("name"),
        avatar_url=claims.get("picture"),
    )


def _upsert_login(
    settings: Settings,
    *,
    identity: OAuthIdentity,
    device_key: str,
    redirect_uri: str,
    device_name: str | None = None,
) -> AuthLoginResult:
    now = int(time.time())
    safe_device_name = (device_name or "").strip()[:200] or None
    with get_connection(settings.app_db_path) as connection:
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
            else:
                account_id = generate_snowflake(settings)
                connection.execute(
                    """
                    INSERT INTO accounts (id, role, created_at, updated_at)
                    VALUES (?, 'user', ?, ?)
                    """,
                    (account_id, now, now),
                )

            _upsert_oauth_identity_row(
                connection,
                account_id=account_id,
                identity=identity,
                now=now,
            )

            if device_row is None:
                device_id = generate_snowflake(settings)
                connection.execute(
                    """
                    INSERT INTO account_devices (id, account_id, device_key, device_name, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (device_id, account_id, device_key, safe_device_name, now, now),
                )
            else:
                device_id = int(device_row["id"])
                connection.execute(
                    """
                    UPDATE account_devices
                    SET account_id = ?, device_name = COALESCE(?, device_name), last_seen_at = ?
                    WHERE id = ?
                    """,
                    (account_id, safe_device_name, now, device_id),
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


def link_oauth_identity(
    settings: Settings,
    *,
    target_account_id: int,
    identity: OAuthIdentity,
    redirect_uri: str,
) -> AuthLinkResult:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            target_row = connection.execute(
                "SELECT id FROM accounts WHERE id = ?",
                (target_account_id,),
            ).fetchone()
            if target_row is None:
                raise AuthError("Authentication required.", status_code=401)

            identity_row = connection.execute(
                """
                SELECT account_id
                FROM account_oauth_identities
                WHERE provider = ? AND provider_user_id = ?
                """,
                (identity.provider, identity.provider_user_id),
            ).fetchone()

            if identity_row is None:
                _upsert_oauth_identity_row(
                    connection,
                    account_id=target_account_id,
                    identity=identity,
                    now=now,
                )
                connection.execute(
                    "UPDATE accounts SET updated_at = ? WHERE id = ?",
                    (now, target_account_id),
                )
                connection.commit()
                return AuthLinkResult(
                    account_id=target_account_id,
                    provider=identity.provider,
                    status="linked",
                    redirect_uri=redirect_uri,
                )

            source_account_id = int(identity_row["account_id"])
            if source_account_id == target_account_id:
                _upsert_oauth_identity_row(
                    connection,
                    account_id=target_account_id,
                    identity=identity,
                    now=now,
                )
                connection.execute(
                    "UPDATE accounts SET updated_at = ? WHERE id = ?",
                    (now, target_account_id),
                )
                connection.commit()
                return AuthLinkResult(
                    account_id=target_account_id,
                    provider=identity.provider,
                    status="already_linked",
                    redirect_uri=redirect_uri,
                )

            merge_token = _create_pending_account_merge(
                connection,
                settings,
                target_account_id=target_account_id,
                source_account_id=source_account_id,
                identity=identity,
                now=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return AuthLinkResult(
        account_id=target_account_id,
        provider=identity.provider,
        status="merge_required",
        redirect_uri=redirect_uri,
        merge_token=merge_token,
    )


def get_pending_account_merge(
    settings: Settings,
    *,
    target_account_id: int,
    merge_token: str,
) -> PendingAccountMergePreview:
    row = _get_pending_account_merge_row(
        settings,
        target_account_id=target_account_id,
        merge_token=merge_token,
    )
    source_account_id = int(row["source_account_id"])
    with get_connection(settings.app_db_path) as connection:
        identity_rows = connection.execute(
            """
            SELECT provider, email, display_name
            FROM account_oauth_identities
            WHERE account_id = ?
            ORDER BY provider, provider_user_id
            """,
            (source_account_id,),
        ).fetchall()
        active_device_row = connection.execute(
            """
            SELECT COUNT(*) AS device_count
            FROM account_devices d
            JOIN account_device_tokens t
              ON t.device_id = d.id
             AND t.revoked_at IS NULL
            WHERE d.account_id = ?
            """,
            (source_account_id,),
        ).fetchone()

    if not identity_rows:
        raise AuthError("Linked account is no longer available.", status_code=409)

    return PendingAccountMergePreview(
        merge_token=merge_token,
        provider=row["provider"],
        target_account_id=target_account_id,
        source_account_id=source_account_id,
        expires_at=int(row["expires_at"]),
        source_identities=tuple(
            AccountIdentitySummary(
                provider=identity_row["provider"],
                email=identity_row["email"],
                display_name=identity_row["display_name"],
            )
            for identity_row in identity_rows
        ),
        active_device_count=int(active_device_row["device_count"] if active_device_row else 0),
    )


def confirm_account_merge(
    settings: Settings,
    *,
    target_account_id: int,
    merge_token: str,
) -> AuthLinkResult:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _get_pending_account_merge_row(
                settings,
                target_account_id=target_account_id,
                merge_token=merge_token,
                connection=connection,
            )
            source_account_id = int(row["source_account_id"])
            provider = row["provider"]
            provider_user_id = row["provider_user_id"]

            target_account_row = connection.execute(
                "SELECT role FROM accounts WHERE id = ?",
                (target_account_id,),
            ).fetchone()
            source_account_row = connection.execute(
                "SELECT role FROM accounts WHERE id = ?",
                (source_account_id,),
            ).fetchone()
            if target_account_row is None or source_account_row is None:
                raise AuthError("Linked account is no longer available.", status_code=409)

            identity_row = connection.execute(
                """
                SELECT 1
                FROM account_oauth_identities
                WHERE provider = ? AND provider_user_id = ? AND account_id = ?
                """,
                (provider, provider_user_id, source_account_id),
            ).fetchone()
            if identity_row is None:
                raise AuthError("Linked identity is no longer available.", status_code=409)

            connection.execute(
                """
                UPDATE account_oauth_identities
                SET account_id = ?, updated_at = ?
                WHERE provider = ? AND provider_user_id = ? AND account_id = ?
                """,
                (target_account_id, now, provider, provider_user_id, source_account_id),
            )
            connection.execute(
                "UPDATE accounts SET role = ?, updated_at = ? WHERE id = ?",
                (
                    _merge_roles(target_account_row["role"], source_account_row["role"]),
                    now,
                    target_account_id,
                ),
            )
            connection.execute(
                "UPDATE auth_pending_account_merges SET consumed_at = ? WHERE token_hash = ?",
                (now, _sha256(merge_token)),
            )
            connection.execute("DELETE FROM accounts WHERE id = ?", (source_account_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return AuthLinkResult(
        account_id=target_account_id,
        provider=provider,
        status="linked",
        redirect_uri="",
    )


def build_login_redirect_url(redirect_uri: str, result: AuthLoginResult) -> str:
    return build_fragment_redirect_url(
        redirect_uri,
        {
            "token": result.token,
            "account_id": str(result.account_id),
            "device_id": str(result.device_id),
            "role": result.role,
            "provider": result.provider,
            "display_name": result.display_name or "",
        },
    )


def build_error_redirect_url(redirect_uri: str, message: str) -> str:
    return build_fragment_redirect_url(redirect_uri, {"error": message})


def build_link_redirect_url(redirect_uri: str, result: AuthLinkResult) -> str:
    payload = {
        "link_status": result.status,
        "provider": result.provider,
    }
    if result.merge_token:
        payload["merge_token"] = result.merge_token
    return build_fragment_redirect_url(redirect_uri, payload)


def build_fragment_redirect_url(redirect_uri: str, params: dict[str, str]) -> str:
    parsed = urlparse(redirect_uri)
    payload = urlencode(params)
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
    with get_connection(settings.app_db_path) as connection:
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
    with get_connection(settings.app_db_path) as connection:
        connection.execute(
            """
            UPDATE account_device_tokens
            SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (now, principal.token_id),
        )
        connection.commit()


def revoke_all_tokens(settings: Settings, principal: AuthPrincipal) -> None:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        connection.execute(
            """
            UPDATE account_device_tokens
            SET revoked_at = ?
            WHERE account_id = ? AND revoked_at IS NULL
            """,
            (now, principal.account_id),
        )
        connection.commit()


def get_device_key(settings: Settings, device_id: int) -> str:
    with get_connection(settings.app_db_path) as connection:
        row = connection.execute(
            "SELECT device_key FROM account_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    if row is None:
        raise AuthError("Authentication required.", status_code=401)
    return row["device_key"]


def account_payload(settings: Settings, principal: AuthPrincipal) -> dict[str, Any]:
    with get_connection(settings.app_db_path) as connection:
        account_row = connection.execute(
            "SELECT created_at, updated_at FROM accounts WHERE id = ?",
            (principal.account_id,),
        ).fetchone()
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
            SELECT device_key, device_name, created_at, last_seen_at
            FROM account_devices
            WHERE id = ?
            """,
            (principal.device_id,),
        ).fetchone()
    return {
        "account_id": str(principal.account_id),
        "device_id": str(principal.device_id),
        "role": principal.role,
        "created_at": int(account_row["created_at"]) if account_row else None,
        "updated_at": int(account_row["updated_at"]) if account_row else None,
        "device": {
            "device_key": device_row["device_key"] if device_row else None,
            "device_name": device_row["device_name"] if device_row else None,
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


def account_devices_payload(settings: Settings, principal: AuthPrincipal) -> dict[str, Any]:
    with get_connection(settings.app_db_path) as connection:
        device_rows = connection.execute(
            """
            SELECT
                d.id,
                d.device_key,
                d.device_name,
                d.created_at,
                d.last_seen_at,
                t.created_at AS token_created_at,
                t.last_used_at AS token_last_used_at
            FROM account_devices d
            JOIN account_device_tokens t
              ON t.device_id = d.id
             AND t.revoked_at IS NULL
            WHERE d.account_id = ?
            ORDER BY
                CASE WHEN d.id = ? THEN 0 ELSE 1 END,
                t.last_used_at DESC,
                d.created_at DESC
            """,
            (principal.account_id, principal.device_id),
        ).fetchall()
    return {
        "account_id": str(principal.account_id),
        "current_device_id": str(principal.device_id),
        "devices": [
            {
                "device_id": str(row["id"]),
                "device_key": row["device_key"],
                "device_name": row["device_name"],
                "created_at": int(row["created_at"]),
                "last_seen_at": int(row["last_seen_at"]),
                "token_created_at": int(row["token_created_at"]),
                "token_last_used_at": int(row["token_last_used_at"]),
                "is_current": int(row["id"]) == principal.device_id,
            }
            for row in device_rows
        ],
    }


def admin_accounts_payload(settings: Settings) -> dict[str, Any]:
    with get_connection(settings.app_db_path) as connection:
        accounts = _load_admin_account_rows(connection)

    summary = {
        "total_accounts": len(accounts),
        "admin_count": sum(1 for account in accounts if account["role"] == "admin"),
        "mod_count": sum(1 for account in accounts if account["role"] == "mod"),
        "user_count": sum(1 for account in accounts if account["role"] == "user"),
        "active_device_count": sum(int(account["active_device_count"]) for account in accounts),
    }
    return {
        "accounts": accounts,
        "summary": summary,
    }


def update_account_role(
    settings: Settings,
    *,
    account_id: int,
    role: str,
) -> dict[str, Any]:
    normalized_role = validate_role(role)
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT role FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if current_row is None:
                raise AuthError("Account not found.", status_code=404)

            current_role = current_row["role"]
            if current_role == "admin" and normalized_role != "admin":
                admin_count_row = connection.execute(
                    "SELECT COUNT(*) AS admin_count FROM accounts WHERE role = 'admin'",
                ).fetchone()
                admin_count = int(admin_count_row["admin_count"] if admin_count_row else 0)
                if admin_count <= 1:
                    raise AuthError(
                        "Cannot demote the last admin account.",
                        status_code=409,
                    )

            connection.execute(
                "UPDATE accounts SET role = ?, updated_at = ? WHERE id = ?",
                (normalized_role, now, account_id),
            )
            updated_accounts = _load_admin_account_rows(connection, account_id=account_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    if not updated_accounts:
        raise AuthError("Account not found.", status_code=404)
    return updated_accounts[0]


def revoke_account_tokens(
    settings: Settings,
    *,
    account_id: int,
) -> int:
    now = int(time.time())
    with get_connection(settings.app_db_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if existing is None:
                raise AuthError("Account not found.", status_code=404)

            result = connection.execute(
                """
                UPDATE account_device_tokens
                SET revoked_at = ?
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (now, account_id),
            )
            connection.execute(
                "UPDATE accounts SET updated_at = ? WHERE id = ?",
                (now, account_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return int(result.rowcount)


def _upsert_oauth_identity_row(
    connection: Any,
    *,
    account_id: int,
    identity: OAuthIdentity,
    now: int,
) -> None:
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


def _create_pending_account_merge(
    connection: Any,
    settings: Settings,
    *,
    target_account_id: int,
    source_account_id: int,
    identity: OAuthIdentity,
    now: int,
) -> str:
    merge_token = secrets.token_urlsafe(32)
    connection.execute(
        "DELETE FROM auth_pending_account_merges WHERE expires_at < ? OR consumed_at IS NOT NULL",
        (now,),
    )
    connection.execute(
        """
        INSERT INTO auth_pending_account_merges
            (token_hash, target_account_id, source_account_id, provider, provider_user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _sha256(merge_token),
            target_account_id,
            source_account_id,
            identity.provider,
            identity.provider_user_id,
            now,
            now + settings.auth_state_ttl_seconds,
        ),
    )
    return merge_token


def _get_pending_account_merge_row(
    settings: Settings,
    *,
    target_account_id: int,
    merge_token: str,
    connection: Any | None = None,
) -> Any:
    now = int(time.time())
    owns_connection = connection is None
    if connection is None:
        context = get_connection(settings.app_db_path)
        connection = context.__enter__()
    try:
        row = connection.execute(
            """
            SELECT source_account_id, provider, provider_user_id, expires_at, consumed_at
            FROM auth_pending_account_merges
            WHERE token_hash = ? AND target_account_id = ?
            """,
            (_sha256(merge_token), target_account_id),
        ).fetchone()
        if row is None:
            raise AuthError("Account merge request was not found.", status_code=404)
        if row["consumed_at"] is not None:
            raise AuthError("Account merge request was already used.", status_code=409)
        if int(row["expires_at"]) < now:
            raise AuthError("Account merge request expired.", status_code=410)
        return row
    finally:
        if owns_connection:
            context.__exit__(None, None, None)


def _merge_roles(current_role: str, source_role: str) -> str:
    if _ROLE_PRIORITY.get(source_role, 0) > _ROLE_PRIORITY.get(current_role, 0):
        return source_role
    return current_role


def _load_admin_account_rows(
    connection: Any,
    *,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    params: tuple[Any, ...] = ()
    where_clause = ""
    if account_id is not None:
        where_clause = "WHERE a.id = ?"
        params = (account_id,)

    account_rows = connection.execute(
        f"""
        SELECT
            a.id,
            a.role,
            a.created_at,
            a.updated_at,
            COUNT(DISTINCT d.id) AS device_count,
            COUNT(DISTINCT CASE WHEN t.revoked_at IS NULL THEN d.id END) AS active_device_count,
            MAX(COALESCE(t.last_used_at, d.last_seen_at)) AS last_seen_at
        FROM accounts a
        LEFT JOIN account_devices d
          ON d.account_id = a.id
        LEFT JOIN account_device_tokens t
          ON t.device_id = d.id
        {where_clause}
        GROUP BY a.id, a.role, a.created_at, a.updated_at
        ORDER BY
            CASE a.role WHEN 'admin' THEN 0 WHEN 'mod' THEN 1 ELSE 2 END,
            COALESCE(MAX(COALESCE(t.last_used_at, d.last_seen_at)), a.updated_at) DESC,
            a.created_at DESC
        """,
        params,
    ).fetchall()
    if not account_rows:
        return []

    account_ids = [int(row["id"]) for row in account_rows]
    placeholders = ", ".join("?" for _ in account_ids)
    identity_rows = connection.execute(
        f"""
        SELECT account_id, provider, provider_user_id, email, display_name, avatar_url
        FROM account_oauth_identities
        WHERE account_id IN ({placeholders})
        ORDER BY account_id DESC, provider, provider_user_id
        """,
        tuple(account_ids),
    ).fetchall()

    identities_by_account: dict[int, list[dict[str, Any]]] = {}
    for row in identity_rows:
        identity_list = identities_by_account.setdefault(int(row["account_id"]), [])
        identity_list.append(
            {
                "provider": row["provider"],
                "provider_user_id": row["provider_user_id"],
                "email": row["email"],
                "display_name": row["display_name"],
                "avatar_url": row["avatar_url"],
            }
        )

    return [
        {
            "account_id": str(row["id"]),
            "role": row["role"],
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            "device_count": int(row["device_count"] or 0),
            "active_device_count": int(row["active_device_count"] or 0),
            "last_seen_at": int(row["last_seen_at"]) if row["last_seen_at"] is not None else None,
            "identities": identities_by_account.get(int(row["id"]), []),
        }
        for row in account_rows
    ]
