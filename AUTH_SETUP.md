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

For Android/iOS native Google Sign-In, also set the native client IDs that the
API server should accept in Google ID token `aud` claims:

```env
GOOGLE_NATIVE_OAUTH_CLIENT_IDS=android-client-id.apps.googleusercontent.com,ios-client-id.apps.googleusercontent.com
```

The server also accepts `GOOGLE_OAUTH_CLIENT_ID` as a valid Google ID token
audience, which is useful when the app uses the web client ID as
`serverClientId`.

Optional:

```env
AUTH_STATE_TTL_SECONDS=600
AUTH_SNOWFLAKE_NODE_ID=0
```

Existing server env such as `TDX_CLIENT_ID`, `TDX_CLIENT_SECRET`, `BUS_DB_PATH`, `BUS_DOWNLOAD_DB_PATH`, and `CORS_ORIGINS` still apply.

## Flutter App Defines

The Flutter app does **not** need Discord or Google client secrets. Do not put
OAuth client secrets in Flutter. Public Google client IDs are OK in the app for
native Android/iOS Google Sign-In.

Defaults are already wired for production:

```text
YABUS_API_BASE_URL=https://bus.avianjay.sbs
YABUS_APP_AUTH_REDIRECT_URI=yabus://auth-callback
YABUS_WEB_AUTH_REDIRECT_URI=https://busapp.avianjay.sbs/auth-callback
```

Builds can override them:

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

Google native Sign-In defines for Android/iOS:

```bash
--dart-define=YABUS_GOOGLE_WEB_CLIENT_ID=web-client-id.apps.googleusercontent.com
--dart-define=YABUS_GOOGLE_IOS_CLIENT_ID=ios-client-id.apps.googleusercontent.com
```

`YABUS_GOOGLE_WEB_CLIENT_ID` is used as the Google Sign-In `serverClientId`.
Android does not need its Android client ID in Dart; it is registered in Google
Cloud with package name and SHA fingerprints. iOS uses
`YABUS_GOOGLE_IOS_CLIENT_ID` as its app client ID.

Platform behavior:

```text
Android: Google native Sign-In inside the app
iOS:     Google native Sign-In inside the app
Web:     Browser OAuth flow
Desktop: Browser OAuth flow
Discord: Browser OAuth flow on every platform
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

## Google OAuth Clients

Create a Web OAuth client for the API server and add this Authorized redirect URI:

```text
https://bus.avianjay.sbs/api/v1/auth/google-callback
```

Use the same host as `AUTH_PUBLIC_BASE_URL`. Required scopes are:

```text
openid email profile
```

The Web client ID/secret goes in the API server env as
`GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`. The web client ID can
also be passed to Flutter as `YABUS_GOOGLE_WEB_CLIENT_ID`; the secret must stay
server-side only.

You do not need to add `yabus://auth-callback` to Google Cloud. Google only
needs the API callback above; the API server performs the final redirect to the
app after it has exchanged the authorization code.

For Android native Sign-In, create an Android OAuth client:

```text
Package name: tw.avianjay.taiwanbus.flutter
SHA-1:        your debug and release signing certificate fingerprints
```

For iOS native Sign-In, create an iOS OAuth client:

```text
Bundle ID: tw.avianjay.taiwanbus.flutter
```

Then add the iOS reversed client ID from Google Cloud or
`GoogleService-Info.plist` to `ios/YABus/Info.plist` under
`CFBundleURLTypes`. Keep the existing `yabus` URL scheme and add a second scheme
for Google, for example:

```xml
<dict>
  <key>CFBundleTypeRole</key>
  <string>Editor</string>
  <key>CFBundleURLSchemes</key>
  <array>
    <string>com.googleusercontent.apps.your-ios-reversed-client-id</string>
  </array>
</dict>
```

No Authorized redirect URI is needed for the Android/iOS OAuth clients.

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

Windows packaged builds register the `yabus://` protocol in the NSIS installer:

```text
HKCU\Software\Classes\yabus
```

Linux `.deb` builds include:

```text
MimeType=x-scheme-handler/yabus;
Exec=yabus %u
```

The post-install script refreshes the desktop database and attempts to set the
default handler. AppImage URL scheme handling depends on the desktop integration
tool used by the system; if it is not integrated automatically, install the
`.deb` package or register the bundled `.desktop` entry manually.

The app process receives the full callback URL as an argument. The Dart
entrypoint already parses `yabus://auth-callback#...` from startup arguments.

For local Windows development without running the installer, register the debug
or release executable manually:

```powershell
reg add HKCU\Software\Classes\yabus /ve /d "URL:YABus Protocol" /f
reg add HKCU\Software\Classes\yabus /v "URL Protocol" /d "" /f
reg add HKCU\Software\Classes\yabus\shell\open\command /ve /d "\"D:\yetanotherbusapp\build\windows\x64\runner\Debug\YetAnotherBusApp.exe\" \"%1\"" /f
```

Change the executable path if you are testing a release build.

## App Account Page

The app has an Account page under Settings. Use it to:

```text
Sign in with Discord
Sign in with Google
Link Discord or Google to the current device-backed account
Refresh linked provider status
Logout only the current device token
```

If you only saw a Discord button before, that was the old Settings inline card:
Google was hidden on non-web/mobile builds. Rebuild the Flutter app after this
change; the Account page now shows both providers. Google uses native app
Sign-In on Android/iOS and browser OAuth everywhere else.

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
