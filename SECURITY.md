# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public issue.

- Use GitHub's private vulnerability reporting on the repository
  (`Rastin-Amani/EzDistro` → **Security** → **Report a vulnerability** / Security
  Advisories).
- No dedicated security email address is published. If you cannot use GitHub, open a
  minimal public issue that says only that you have a security report, and a
  maintainer will arrange a private channel.

Please include: affected component, reproduction steps, impact, and any suggested
fix. We aim to acknowledge reports promptly and will credit reporters who wish to be
named.

There is currently no formal bug-bounty program.

## Supported versions

The project versions the application with `APP_VERSION` in
`app/config.py`, kept equal to the release tag (`vX.Y.Z`). Security fixes target the latest
tag on the default branch; older tags are best-effort.

## Security model (what is actually implemented)

These are concrete, code-verified mechanisms, not guarantees:

- **Authentication** — PocketBase-native **OAuth2 only (Google, GitHub)**. There
  is no password login: bootstrap disables `passwordAuth`/`otp`/`mfa` on the
  `users` collection. The provider is chosen on `/login`/`/signup`; the one-off
  OAuth `state` + PKCE `codeVerifier` are kept in a signed (HMAC), `HttpOnly`,
  `SameSite=Lax`, 10-minute `oauth_state` cookie scoped to `/auth`. The callback
  (`/auth/oauth2/callback`) verifies that state and exchanges the code with
  `users.auth_with_oauth2`; the token is stored in the same `HttpOnly`,
  `SameSite=Lax` `pb_auth` cookie (`Secure` in production or over TLS; 30-day
  max-age). `AuthMiddleware` gates every route; unauthenticated requests redirect
  to `/login` (303) except a small allow-list (`/login`, `/signup`,
  `/auth/oauth2/*`, `/static/*`, `/manifest.json`, `/sw.js`, `/favicon.ico`,
  `/health`, `/openapi.json`, `/docs`, `/offline`, `/`).
- **Authorization** — platform roles `admin`/`member`; per-project membership roles
  `owner > admin > editor > viewer`, enforced per route (`app/api/deps.py`). An
  empty membership list means no access. `ensure_record_in_project()` rejects
  cross-project id substitution.
- **CSRF-lite** — mutations are POST/PUT gated by `require_hx` (the `HX-Request`
  header must be present).
- **Secrets at rest** — integration API keys/passwords (and the Google Ads refresh
  token) are Fernet-encrypted into `integrations.secretsEnc` /
  `google_ads_connections.refreshTokenEnc` under `SECRETS_KEY`. Production refuses
  to start without `SECRETS_KEY`; dev derives a deterministic key. Only masked
  previews reach the UI; plaintext is decrypted server-side only for provider calls
  and is never logged or sent to analytics.
- **SSRF guard** — provider URLs are validated for scheme (http/https) and rejected
  for private/loopback/link-local targets unless `ALLOW_PRIVATE_NETWORKS=1`
  (development only).
- **Content safety** — LLM HTML is sanitized with bleach allow-lists before storage
  and display; forbidden-pattern validators reject scripts/iframes/event handlers.
- **PocketBase rules** — all app collections are `null` (superuser-only/locked;
  `""` would mean public). All data access uses the superuser client; tenant
  isolation lives in the app layer, so **never** relax a collection rule to
  `@request.auth.id != ''`.

## Known limitations (not claimed)

- No penetration test, no formal threat model, and **no rate limiting on login**.
- `job_events` and `publishing_runs` are append-only by application convention, not
  enforced by PocketBase rules.
- The unpublish flow currently writes enum values the `publishing_runs` schema does
  not allow (see `docs/ARCHITECTURE.md` §11) — a correctness bug, not a security
  one, but avoid relying on unpublish.
- Rotating `SECRETS_KEY` makes previously stored credentials undecryptable; they
  must be re-entered.

## Hardening checklist for operators

- [ ] Run with `ENV=production` (JSON logs, `/docs`/debug disabled, `SECRETS_KEY`
      required).
- [ ] Terminate TLS in front of the web process (sets the cookie `Secure` flag).
- [ ] Keep `ALLOW_PRIVATE_NETWORKS=0`.
- [ ] Store `SECRETS_KEY` in a secret manager, separate from PocketBase backups.
- [ ] Use a dedicated PocketBase superuser for the app, not your dashboard login.
- [ ] Keep password/OTP/MFA auth disabled on `users` (bootstrap enforces it) and
      register the exact `/auth/oauth2/callback` URL at both providers.
- [ ] Sign in as `SEED_ADMIN_EMAIL` once, then re-run bootstrap if the account
      needs promoting to `role=admin`.
