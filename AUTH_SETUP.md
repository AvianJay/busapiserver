# YABus OAuth Auth Setup

This deployment uses OAuth only. There is no username/password login.

## API Server Environment

Required:

```env
AUTH_PUBLIC_BASE_URL=https://bus.avianjay.sbs
DISCORD_OAUTH_CLIENT_ID=...
DISCORD_OAUTH_CLIENT_SECRET=...
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

Optional:

```env
AUTH_STATE_TTL_SECONDS=600
AUTH_SNOWFLAKE_NODE_ID=0
```

Existing server env such as `TDX_CLIENT_ID`, `TDX_CLIENT_SECRET`, `BUS_DB_PATH`, `BUS_DOWNLOAD_DB_PATH`, and `CORS_ORIGINS` still apply.

## Flutter App Defines

Defaults are already wired for production, but builds can override them:

```bash
flutter build windows --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
flutter build macos --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
flutter build apk --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
```

Optional redirect overrides:

```bash
--dart-define=YABUS_APP_AUTH_REDIRECT_URI=yabus://auth-callback
--dart-define=YABUS_WEB_AUTH_REDIRECT_URI=https://busapp.avianjay.sbs/auth-callback
```

The app generates a UUIDv4 device key on first launch and stores it in persistent app storage. Reinstalling the app creates a new device. The app does not read MAC addresses, serial numbers, or hardware fingerprints.

## Discord OAuth App

Add these Redirects in the Discord Developer Portal:

```text
https://bus.avianjay.sbs/api/v1/auth/discord-callback
```

Use the same host as `AUTH_PUBLIC_BASE_URL`. Required scope is:

```text
identify email
```

## Google OAuth Client

Create a Web OAuth client for the API server and add this Authorized redirect URI:

```text
https://bus.avianjay.sbs/api/v1/auth/google-callback
```

Use the same host as `AUTH_PUBLIC_BASE_URL`. Required scopes are:

```text
openid email profile
```

The Flutter app does not need to store Google or Discord client secrets. OAuth code exchange happens on the API server.

## Redirect Whitelist

The API server accepts only:

```text
yabus://...
https://busapp.avianjay.sbs/...
```

Auth success and error payloads are returned in the URL fragment, for example:

```text
yabus://auth-callback#token=...&account_id=...&device_id=...&role=user
```

Fragments are used so the token is not sent to the web callback server as a normal query string.

## Desktop App URL Scheme

macOS and mobile app scheme handling are wired in the app project.

For Windows and Linux packaged builds, register the `yabus://` protocol in the installer so the browser can reopen the app after OAuth. Pass the full callback URL to the app process as an argument; the Dart entrypoint already parses `yabus://auth-callback#...` from startup arguments.

## Token And Rate Limit Behavior

Token format:

```text
base64(snowflake).base64(timestamp).random_secret
```

The server stores only `sha256(token)`, not the raw token. Each device has one active token; logging in again on the same device revokes the previous device token.

Rate limit:

```text
Authenticated: 30 req/min per account id
Anonymous:     30 req/min per IP
```

Roles are stored on `accounts.role` and can be one of:

```text
admin
mod
user
```

New accounts default to `user`. Promote trusted accounts manually in SQLite for now:

```sql
UPDATE accounts SET role = 'admin', updated_at = strftime('%s', 'now')
WHERE id = 123;
```
