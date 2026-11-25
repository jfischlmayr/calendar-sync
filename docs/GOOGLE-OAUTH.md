# Google Calendar OAuth and iCloud CalDAV credentials

calsync needs its own credentials for both providers. Secrets are passed only through environment variables
(or `*_FILE` variables pointing to files) and are never written to the configuration file or logs.

## iCloud

Create a dedicated **app-specific password** in your Apple account security settings. Provide it as
`ICLOUD_APP_PASSWORD` and the corresponding iCloud login as `ICLOUD_USER`. Do not use your normal Apple ID
password. Verify calendar ownership before any write; read-only or shared calendars must not be paired.

## Google

Create a dedicated Google Cloud project with the Calendar API enabled and an OAuth client/consent screen suitable for a
local/desktop login. Request only the minimum Calendar scopes supported by the vdirsyncer OAuth flow, review precisely
what Google asks for, and provide `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (these exact variable names, also in
`compose.yaml`) as secrets.

Run `scripts/google-oauth-local.sh /secure/local/path/google.json` **on a workstation with a browser** with these
variables set. It performs calendar discovery with vdirsyncer into a throw-away directory and writes the token file with
mode 600. Do not paste tokens into chats, issues, logs or shell history.

While the Compose container is running inert (all pairs disabled), transfer the token directly as the service user:

```sh
docker compose exec -T calsync sh -c 'umask 077; mkdir -p /data/tokens; chmod 700 /data/tokens; cat > /data/tokens/.google.json.tmp && mv /data/tokens/.google.json.tmp /data/tokens/google.json' < /secure/local/path/google.json
```

This keeps the token at `/data/tokens/google.json` (owner UID 10001, mode 600, directory mode 700).
Do not use a root-owned copy. Only then enable a mirror pair. Check that refresh still works after a
container restart (`calsync auth-status`).

Until both providers authenticate and read-only exports match the expected calendars, do not enable writes.

## Refresh-token lifetime (Testing status)

While the OAuth consent screen of an external app is in **Testing**, Google expires the refresh token after **7 days**
(`refresh_token_expires_in = 604800` in the token file). After that every Google call fails with `invalid_grant`. The
engine reports this as `AUTH:` in `calsync status` (pair `blocked`, health degraded) and alerts immediately; mirrors and
audits stop until a new token is created.

* Long-term fix: in the Google Cloud Console (OAuth consent screen) set the publishing status to **In production**. For a
  personal, unverified app the Calendar scope then shows an "unverified app" warning once during consent. Tokens issued
  while in Testing keep the 7-day expiry, so re-run `scripts/google-oauth-local.sh` after switching and replace the token
  file. Check Google's current documentation for limits and policies; whether to publish is your decision.
* Other causes of invalid tokens: unused for 6 months, user revocation, too many tokens per account/client, admin policy.
* `calsync auth-status` prints expiry data without secrets (`refresh_token_expires_hint` when known).
* Token refresh is done by `calsync.gauth.GoogleTokenSource`: atomic 0600 rewrite that keeps the refresh token and unknown
  keys, re-reads the file before refreshing (vdirsyncer shares it) and never logs token values.
* The built-in backup includes token files in the age-encrypted archive. Protect the archive and private decryption identity accordingly; a restored token may already be expired or revoked.
