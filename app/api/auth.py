from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth_service import (
    AuthError,
    AuthPlatform,
    account_payload,
    build_authorization_url,
    build_error_redirect_url,
    build_login_redirect_url,
    complete_oauth_login,
    create_oauth_state,
    consume_oauth_state,
    default_redirect_uri,
    normalize_redirect_uri,
    revoke_token,
    validate_device_key,
    validate_platform,
    validate_provider,
)
from app.rate_limit import enforce_rate_limit, get_request_principal


router = APIRouter(tags=["auth"], dependencies=[Depends(enforce_rate_limit)])


@router.get("/auth", response_class=HTMLResponse, include_in_schema=False)
def auth_page(
    platform: str = Query(default="web"),
    redirect: str = Query(default=""),
    device_key: str = Query(default=""),
) -> HTMLResponse:
    try:
        auth_platform = validate_platform(platform)
        redirect_uri = normalize_redirect_uri(redirect, auth_platform)
        initial_device_key = validate_device_key(device_key) if device_key else ""
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return HTMLResponse(
        _auth_page_html(
            platform=auth_platform,
            redirect_uri=redirect_uri,
            device_key=initial_device_key,
        )
    )


@router.get("/api/v1/auth/discord-start")
def start_discord_login(
    request: Request,
    platform: str = Query(default="web"),
    redirect: str = Query(default=""),
    device_key: str = Query(...),
) -> RedirectResponse:
    return _start_oauth_login(
        request,
        provider="discord",
        platform=platform,
        redirect=redirect,
        device_key=device_key,
    )


@router.get("/api/v1/auth/google-start")
def start_google_login(
    request: Request,
    platform: str = Query(default="web"),
    redirect: str = Query(default=""),
    device_key: str = Query(...),
) -> RedirectResponse:
    return _start_oauth_login(
        request,
        provider="google",
        platform=platform,
        redirect=redirect,
        device_key=device_key,
    )


@router.get("/api/v1/auth/discord-callback")
def discord_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    return _oauth_callback(request, provider="discord", code=code, state=state, error=error)


@router.get("/api/v1/auth/google-callback")
def google_callback(
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    return _oauth_callback(request, provider="google", code=code, state=state, error=error)


@router.get("/api/v1/auth/me")
def auth_me(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return account_payload(request.app.state.settings, principal)


@router.post("/api/v1/auth/logout")
def auth_logout(request: Request) -> dict[str, object]:
    principal = get_request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    revoke_token(request.app.state.settings, principal)
    return {"ok": True}


def _start_oauth_login(
    request: Request,
    *,
    provider: str,
    platform: str,
    redirect: str,
    device_key: str,
) -> RedirectResponse:
    try:
        auth_provider = validate_provider(provider)
        auth_platform = validate_platform(platform)
        redirect_uri = normalize_redirect_uri(redirect, auth_platform)
        normalized_device_key = validate_device_key(device_key)
        settings = request.app.state.settings
        state = create_oauth_state(
            settings,
            provider=auth_provider,
            platform=auth_platform,
            redirect_uri=redirect_uri,
            device_key=normalized_device_key,
        )
        authorization_url = build_authorization_url(
            settings,
            provider=auth_provider,
            state=state,
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return RedirectResponse(authorization_url, status_code=302)


def _oauth_callback(
    request: Request,
    *,
    provider: str,
    code: str,
    state: str,
    error: str,
) -> RedirectResponse:
    auth_provider = validate_provider(provider)
    fallback_redirect = default_redirect_uri("web")
    settings = request.app.state.settings
    if error:
        redirect_uri = fallback_redirect
        if state:
            try:
                redirect_uri = consume_oauth_state(
                    settings,
                    provider=auth_provider,
                    state=state,
                ).redirect_uri
            except AuthError:
                redirect_uri = fallback_redirect
        return RedirectResponse(
            build_error_redirect_url(redirect_uri, error),
            status_code=302,
        )
    if not code or not state:
        return RedirectResponse(
            build_error_redirect_url(fallback_redirect, "missing_code_or_state"),
            status_code=302,
        )

    try:
        result = complete_oauth_login(
            settings,
            provider=auth_provider,
            code=code,
            state=state,
        )
    except AuthError as exc:
        return RedirectResponse(
            build_error_redirect_url(exc.redirect_uri or fallback_redirect, exc.message),
            status_code=302,
        )

    return RedirectResponse(build_login_redirect_url(result.redirect_uri, result), status_code=302)


def _auth_page_html(
    *,
    platform: AuthPlatform,
    redirect_uri: str,
    device_key: str,
) -> str:
    platform_json = json.dumps(platform)
    redirect_json = json.dumps(redirect_uri)
    device_key_json = json.dumps(device_key)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>YABus Login</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fb;
      --text: #17202a;
      --muted: #5f6f7e;
      --line: #d9e0e7;
      --primary: #0b7285;
      --surface: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--surface);
      box-shadow: 0 20px 50px rgba(23, 32, 42, 0.08);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
      line-height: 1.2;
    }}
    p {{
      margin: 0 0 20px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .actions {{
      display: grid;
      gap: 12px;
    }}
    a {{
      display: block;
      padding: 13px 16px;
      border-radius: 10px;
      background: var(--primary);
      color: white;
      text-align: center;
      text-decoration: none;
      font-weight: 700;
    }}
    a.secondary {{
      background: #202124;
    }}
  </style>
</head>
<body>
  <main>
    <h1>YABus Login</h1>
    <p>Choose a provider to continue. This device will receive its own token.</p>
    <div class="actions">
      <a data-provider="discord" href="#">Continue with Discord</a>
      <a class="secondary" data-provider="google" href="#">Continue with Google</a>
    </div>
  </main>
  <script>
    const platform = {platform_json};
    const redirect = {redirect_json};
    let deviceKey = {device_key_json};

    function uuidv4() {{
      return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, c =>
        (+c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> +c / 4).toString(16)
      );
    }}

    if (!deviceKey) {{
      deviceKey = localStorage.getItem("yabus_device_key") || uuidv4();
      localStorage.setItem("yabus_device_key", deviceKey);
    }}

    document.querySelectorAll("[data-provider]").forEach((link) => {{
      const provider = link.dataset.provider;
      const params = new URLSearchParams({{
        platform,
        redirect,
        device_key: deviceKey,
      }});
      link.href = `/api/v1/auth/${{provider}}-start?${{params.toString()}}`;
    }});
  </script>
</body>
</html>"""
