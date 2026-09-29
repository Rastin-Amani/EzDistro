# EzDistro Team Features — Product Requirements & Implementation Brief

```text
Status:        Draft for implementation
Date:          2026-09-25
Source of truth: Code verified against the repository on 2026-09-25.
                File/line references below were read directly; anything not
                verified is marked UNVERIFIED or Assumption.
Decisions:     (1) Invitations are LINK-ONLY — no email is ever sent by the app.
               (2) Full suite scope: global user administration + per-project
                   member management, delivered in phases.
```

---

## 1. Objective

Build team capabilities for EzDistro: the ability to create and administer platform
users, invite teammates into projects with explicit roles, and manage
memberships (roles, removal, ownership transfer) — all through the existing
FastAPI + HTMX + Jinja2 + DaisyUI stack, with PocketBase remaining the sole
data store.

## 2. Problem

Today the platform is multi-user in schema but single-user in practice:

- There is **no way to create a user from the app**. The only account is the
  seeded admin (`app/scripts/bootstrap_pb.py:1038` `seed_admin_user`). There is
  no signup route anywhere (`app/api/auth.py` exposes only `/login`, `/logout`).
- `project_members` exists with a full role model
  (`owner | admin | editor | viewer`, `app/scripts/bootstrap_pb.py:681-693`) and
  is correctly enforced in `app/api/deps.py:87-102`, but it has **zero
  management UI or routes**. The only membership ever created is the implicit
  creator→owner row at project creation (`app/api/projects.py:148`).
- `MemberRepo` (`app/repositories/members.py`) already implements
  add/upsert/remove/list/role_of — reusable as-is.

Consequence: an operator must create users through the PocketBase admin
console by hand, and can never grant a teammate access to a project without
direct database surgery.

## 3. Users / Roles

Two orthogonal role systems already exist — do not invent new ones:

| Level | Values | Source | Meaning |
|---|---|---|---|
| Global (`users.role`) | `admin`, `member` | `bootstrap_pb.py:913-947` (`ensure_users_fields`) | Platform administration. Global admins bypass all project-role checks (`deps.py:96-97`). |
| Project (`project_members.role`) | `owner`, `admin`, `editor`, `viewer` | `bootstrap_pb.py:687` | Per-project authorization; hierarchy owner > admin > editor > viewer. |

Actors for this feature:

- **Global admin** (`users.role == "admin"`): creates/disables/deletes users,
  assigns global roles, resets passwords. Sees every project (existing
  behavior, `deps.py:61-62`).
- **Project owner**: full control of one project's membership incl. ownership
  transfer; already has delete-project rights (`PROJECT_ADMIN_ROLES`,
  `deps.py:84`).
- **Project admin**: manages members/invites below owner level.
- **Project editor / viewer**: may view the member list only; cannot invite,
  change roles, or remove anyone.
- **Invited person (not yet a user)**: holds a single-use invite link; may
  register through it.
- **Member (`users.role == "member"`)**: self-service account maintenance
  (own password, display name), leave projects they belong to.

No other roles may be introduced.

## 4. Desired Outcome & Success Criteria

After implementation, an operator can, without touching PocketBase directly:

1. Create a user account from the UI (email, display name, role, initial
   password or generated one).
2. Open a project → **Members** tab → invite a teammate by email:
   - existing account → added directly with the chosen role;
   - new person → an invitation record is created and a single-use,
     expiring invite link is displayed for out-of-band sharing.
3. The invitee opens the link, registers (email pre-locked), and lands in the
   project with the invited role.
4. Owners/admins change roles, remove members, revoke invites, transfer
   ownership; members can leave; every mutation is role-checked and toasted in
   Persian per existing HTMX conventions.
5. Global admins can disable a user (session dies on next request), re-enable,
   set a new password, change global role, delete a user (memberships cascade).

Observable proof: the new test files (§22) pass under `make check`, and every
acceptance criterion in §25 holds.

## 5. Scope

### In scope

- **A. Members tab** per project: list, add existing user by email, change
  role, remove, leave, transfer ownership, pending-invite list with revoke.
- **B. Invitations**: link-only, single-use, TTL-based, revocable records with
  a public acceptance + registration flow.
- **C. Global admin user management**: `/admin/users` page (list, create,
  role change, disable/enable, set password, delete).
- **D. Account self-service**: change own password, edit own display name.
- **E. Security prerequisite** (§18-A): PocketBase API-rule hardening so that
  project roles cannot be bypassed by talking to PocketBase directly. This is
  required for A–C to mean anything.
- **F. Docs + i18n + tests** for all of the above.

### Out of scope (non-goals)

- **No email/SMTP of any kind** — explicit product decision. The app never
  sends mail; invite links are copied and shared out-of-band. Do not add
  SMTP config, email templates, or PocketBase mail hooks.
- No open self-registration — accounts are created only by a global admin or
  through a valid invite token.
- No teams/organizations above projects (project remains the tenancy unit).
- No per-article or per-resource ACLs; roles stay project-scoped.
- No SSO/OAuth/LDAP, no 2FA, no SCIM.
- No quotas/billing/seat limits, no usage dashboards per member.
- No activity-feed UI (audit exists implicitly via record `created`/`updated`
  and structlog; a feed is deferred).
- No changes to job system, providers, prompts, indexing/writing/publishing
  pipelines.

### Deferred

- Invite "resend/rotate" (v1: revoke + create new invite).
- Optional rich invite message field.
- Per-member notification preferences.
- Export of membership/audit history.

### Assumptions

- **A1**: The web process will gain a legitimate use of `get_admin_pb()`
  (`app/pb.py:20`) for user creation and hardened membership writes.
  `PB_ADMIN_EMAIL`/`PB_ADMIN_PASSWORD` are already required by `.env.example:18-19`
  and used by bootstrap/worker, so the env contract does not change — but
  Phase 0 must confirm the web deployment actually has them set.
- **A2**: PocketBase ≥ 0.23 (per `docs/SCHEMA.md` header) — the flat field
  shape in `bootstrap_pb.py` is authoritative.
- **A3**: Invite TTL default 7 days, configurable via a new
  `invite_ttl_hours` setting (`app/config.py`, default `168`).
- **A4**: Jinja autoescaping of `.html` templates holds (Starlette
  `Jinja2Templates` default); Phase 0 verifies `app/templates.py` — if it
  constructs a raw `jinja2.Environment`, confirm `autoescape` explicitly.

**Phase 0 verification (2026-09-25):**

- **A1 confirmed**: `.env` contains `PB_ADMIN_EMAIL` / `PB_ADMIN_PASSWORD`
  (web deployment has the admin credentials; the env contract does not
  change when the web process starts using `get_pb` admin access).
- **A4 confirmed**: `app/templates.py` uses Starlette `Jinja2Templates` —
  autoescape is on for `.html` templates (default `select_autoescape`);
  no raw `jinja2.Environment` is constructed.
- **A2 confirmed** against `bootstrap_pb.py` (flat field shape, `_base_field`
  with PB ≥ 0.23 options); the live instance could not be re-verified in
  Phase 0 (see §18-A findings — PocketBase was unreachable at inspection
  time).

## 6. Change Protection — DO NOT

- Do **not** introduce Redis/Celery/n8n or any queue/actor framework.
- Do **not** add email/SMTP capabilities (decision (1)).
- Do **not** create new job types (member/invite/user operations are fast,
  synchronous request work; the worker whitelist in
  `app/workers/worker.py` stays at its 8 types).
- Do **not** rewrite authentication (cookie `pb_auth`, `AuthMiddleware`,
  login/logout flows in `app/api/auth.py` stay; only additive changes
  allowed — §14 lists the exact additions).
- Do **not** rename PocketBase fields to snake_case; camelCase is invariant
  (`app/pb.py`, AGENTS.md).
- Do **not** change the semantics of `PROJECT_WRITE_ROLES` /
  `PROJECT_ADMIN_ROLES` / `require_project_role` — only extend around them.
- Do **not** hand-edit `app/static/app.js` (committed minified bundle); use
  inline `hx-on:` / Alpine attributes already shipped inside it.
- Do **not** open registration, weaken `require_hx`, or remove the
  `ensure_record_in_project` guard.
- Do **not** add a React/SPA surface, or a new CSS framework; DaisyUI
  components already available.
- Do **not** mix unrelated refactors into feature commits (use `git-hygiene`).

## 7. Existing System (verified)

| Area | Fact | Evidence |
|---|---|---|
| Session | Login sets httponly `pb_auth` cookie (30d, samesite=lax, secure in prod/HTTPS); native form POST uses 303 redirect for iOS Safari cookie reliability | `app/api/auth.py:50-71` |
| Auth gate | `AuthMiddleware` resolves locale, binds request context, validates cookie via `auth_refresh()`, sets `request.state.user`, redirects unauthenticated non-public paths to `/login` | `app/middleware.py:29-58` |
| Public paths | `PUBLIC_PATHS` allowlist (`/login`, `/static`, `/health`, …) | `app/middleware.py:14-24` |
| Request PB client | `get_pb()` is **unauthenticated per request**; middleware then saves the *user's* token into it → all repos run with **user-level** auth | `app/pb.py:15-17`, `app/middleware.py:44-49` |
| Admin PB client | `get_admin_pb()` uses `_superusers`→`_admins` fallback; today used only by the worker | `app/pb.py:20-37`, `app/workers/worker.py:49` |
| Project scope | `project_scope()` → `None` = global admin, list = member ids, `[]` = no access (documented auth-bypass fix) | `app/api/deps.py:52-64` |
| Role guard | `require_project_role()`; global admins bypass and return `"admin"` | `app/api/deps.py:87-102` |
| Role tuples | `PROJECT_WRITE_ROLES = ("owner","admin","editor")`, `PROJECT_ADMIN_ROLES = ("owner","admin")` | `app/api/deps.py:83-84` |
| MemberRepo | `MEMBER_ROLES`, upsert `add()`, `remove()`, `list_for_project`, `list_for_user`, `role_of()` | `app/repositories/members.py` |
| Membership bootstrap | Creator becomes `owner` on project creation | `app/api/projects.py:148` |
| Tabs | `TABS` list (12 tabs) drives `GET /projects/{id}` and `GET /projects/{id}/tabs/{tab}`; tab templates live in `app/templates/pages/projects/tabs/` | `app/api/projects.py:63-76,430-471` |
| Tab labels | Persian label dict is **inside the template** | `app/templates/pages/projects/detail.html:67-75` |
| Schema helpers | `col()/t()/num()/boolean()/date()/select()/rel()/json_field()`; default API rules = locked (`null`, superuser-only) on **every** collection (no collection overrides it). **Never `AUTH_RULE`** — see the §18-A correction | `app/scripts/bootstrap_pb.py:28-51,67-125` |
| Users schema | `users` gains `role` (`admin`\|`member`) + `displayName` idempotently | `app/scripts/bootstrap_pb.py:913-947` |
| Schema mirror | `pb_collections_import.json` must be kept in sync with bootstrap | `docs/SCHEMA.md:4-6` |
| Doc'd security model | "All collection API rules are locked; the server accesses PocketBase as a superuser and project-level authorization lives in the app layer" | `docs/SCHEMA.md:26-27` |
| HTMX contract | Mutations: `require_hx` + `@hx_error("message")` + helpers `ok_with_redirect/success_response/error_response/hx_trigger` | `app/api/deps.py:22-25`, `app/api/errors.py`, `app/utils.py` |
| i18n | `_()` / `ngettext` (Babel); Persian default + `en` catalog; `make i18n-extract/add/update/compile` | `Makefile:31-42`, `app/locales/` |
| Nav | Sidebar `nav` list + mobile dock, logout in user menu | `app/templates/layouts/platform.html:29-57,143-185` |
| Tests | Fake PocketBase with unique-constraint + filter emulation; helpers `make_user`, `make_member`, `make_project`; role-matrix tests already exist | `tests/fakes.py`, `tests/helpers.py:23,87`, `tests/test_authorization.py`, `tests/test_routes_security.py` |
| Email | **No** email/SMTP/notification code anywhere in `app/` | verified by grep |
| User creation | None in app code; only `seed_admin_user` | `app/scripts/bootstrap_pb.py:1038-1060` |

## 8. Requirements

### Product

- R1: Global admins can perform full user lifecycle from the UI (§4.1, §4.5).
- R2: Project owners/admins can invite by email; existing accounts are added
  directly, unknown emails get an invite link.
- R3: Invite links are single-use, expire (default 7 days), are revocable,
  and are never sent anywhere by the system.
- R4: Roles are enforced exactly as today; new UI never bypasses
  `require_project_role`.
- R5: A project always has ≥ 1 owner; the platform always has ≥ 1 enabled
  global admin.

### User/UX

- R6: All UI strings Persian (`_()`), RTL-safe, dates through `jalali_*`
  filters; new strings extracted to `en` catalog.
- R7: Every mutation answers with the standard toast helpers; destructive
  actions (remove member, delete user, transfer ownership, revoke invite)
  require an explicit confirmation step.
- R8: The invite link is shown once in a copyable field immediately after
  creation, with an inline copy button (`hx-on:click` →
  `navigator.clipboard.writeText`) and a "Copied" feedback. If lost:
  revoke + re-invite (no re-display — only the hash is stored).

### Technical

- R9: All data access through repositories extending `BaseRepo`
  (camelCase, paginated); member list uses `expand=user` in **one** query
  (no N+1).
- R10: New schema lives in `bootstrap_pb.py` (idempotent) + mirrored into
  `pb_collections_import.json`.
- R11: `app/api/` stays thin: parse HTTP → guard → repo/service → response
  helpers (layering per AGENTS.md).
- R12: Any new module using structlog keyword logging must be added to the
  `[[tool.mypy.overrides]]` whitelist in `pyproject.toml` or `make typecheck`
  fails.

### Reliability

- R13: Accepting an invite is idempotent-safe: a consumed token can never
  create a second membership (unique token + status check + upsert via
  `MemberRepo.add`).
- R14: Ownership transfer performs promote-then-demote; if step 2 fails the
  project temporarily has two owners (safe state) and the UI reports the
  partial failure — never zero owners.

### Operational

- R15: `make check` green (lint → typecheck → test). Full suite ~3 min; give
  it a long timeout. Known flaky: `tests/test_jobs_advanced.py::test_retry_uses_jittered_backoff`.
- R16: `make css` must be re-run (and the committed `app/static/app.css`
  updated) if `app/static/css/input.css` changes.

## 9. User Journeys (with failure paths)

### J1 — Admin creates a user
1. Global admin opens `/admin/users`.
2. Fills email, display name, role, optional password (else server generates
   one and shows it once).
3. Server validates (email format, unique, password ≥ 8 chars), creates via
   admin client, toasts success. Failure: duplicate email → Persian toast,
   form state preserved.

### J2 — Owner invites an existing teammate
1. Owner → project → **Members** tab → enters teammate email + role ≤ own role.
2. Server finds the user → `MemberRepo.add(...)` → toast + list refresh.
3. Failure paths: unknown email → switches to invite-link flow (J3); already
   a member → "this user is already a member" toast (upsert to new role is offered
   only to owner/admin explicitly via the role dropdown, not silently);
   role > granter → "you cannot grant a role above your own".

### J3 — Owner invites a new person
1. Same form; email matches no user → invitation record created, modal shows
   `/invite/{token}` link + copy button.
2. Owner shares the link out-of-band (Telegram/email — anything, not the app).
3. Failures: pending invite already exists for that email → toast + link to
   revoke/replace; invitee opens expired/revoked/used link → dedicated Persian
   error page states (§14 C-series routes).

### J4 — Invitee registers and joins
1. `GET /invite/{token}` (public): valid + pending → landing page showing
   project name, inviter display name, role, expiry (jalali).
2. Not logged in, email unregistered → registration form (email readonly and
   server-enforced, display name + password + confirm).
3. Native form POST → server creates user (admin client), creates membership
   with invited role, marks invite `accepted`, logs the user in (set `pb_auth`
   cookie), 303 → project page. Reuses the native-POST/303 cookie pattern from
   `app/api/auth.py:59-70`.
4. Email already registered → page shows "sign in first" with `next`
   redirect back to the invite URL after login.
5. Logged in as the *wrong* account → error: invite belongs to another email;
   offer logout → login-as-invited → reopen link.
6. Failures: token unknown / revoked / expired / already accepted → distinct
   Persian messages; no token value echoed back.

### J5 — Role change / removal / leave
- Owner/admin changes a role via inline select → POST → toast + row refresh.
  Failure: demoting the last owner → blocked; editor/viewer attempting → 403
  toast via existing guard; changing the owner's row unless you are owner →
  blocked.
- Member clicks "leave project" → confirm → membership deleted → redirect to
  `/projects`. Failure: last owner → blocked with explanation.
- Removing a user (project scope) deletes only the membership row — the
  account and all project data remain (data preservation invariant).

### J6 — Global admin disables a user
1. `/admin/users` → disable → confirm → `users.disabled = true`.
2. On the victim's next request, `AuthMiddleware` sees `disabled` → clears
   auth store → behaves as logged out (redirect to `/login` with a Persian
   "your account is disabled" notice — not a silent bounce loop).
3. Failure: attempting to disable the last enabled global admin (including
   self) → blocked.

## 10. Authorization Model & Matrix

Global admin bypass (existing, keep): global `admin` passes every
`require_project_role` check. All new routes must use the same helpers — no
bespoke permission code.

Project capability matrix (actor → allowed actions):

| Action | owner | admin | editor | viewer | global admin |
|---|---|---|---|---|---|
| View member + invite lists | ✓ | ✓ | ✓ | ✓ | ✓ |
| Add existing user / create invite | ✓ | ✓ | ✗ | ✗ | ✓ |
| Grant role `owner` (transfer) | ✓ | ✗ | ✗ | ✗ | ✓ |
| Grant role `admin`/`editor`/`viewer` | ✓ | ✓ (≤ own level) | ✗ | ✗ | ✓ |
| Change any member's role | ✓ | ✓ (not owner's row) | ✗ | ✗ | ✓ |
| Remove member | ✓ (not last owner) | ✓ (not owner, not last owner) | ✗ | ✗ | ✓ |
| Revoke/replace invite | ✓ | ✓ | ✗ | ✗ | ✓ |
| Leave project (self) | ✓ unless last owner | ✓ | ✓ | ✓ | n/a |
| Delete project (existing) | ✓ | ✓ | ✗ | ✗ | ✓ |

Invariants (enforce in a service, assert in tests):

- **I1**: ≥ 1 owner per project at all times (covers create, demote, remove,
  leave, transfer).
- **I2**: ≥ 1 enabled global admin at all times (covers role change, disable,
  delete; deletion cascades memberships — check it is not the last admin).
- **I3**: A grantable role is strictly ≤ the granter's role in the hierarchy
  owner > admin > editor > viewer; only an owner (or global admin) may grant
  `owner`.
- **I4**: You may never modify *your own* role through a role-change endpoint
  (self-demotion/`member` promotion side-steps I3); self changes happen only
  via transfer (owner→admin self-demotion bundled with promote) or leave.

## 11. Architecture

No new frameworks. Component map for the feature:

```text
app/api/teams.py (new router)          — project members + invites HTTP/HTMX
app/api/admin_users.py (new router)    — global user admin + account self-service
app/services/team.py (new service)     — invariants I1–I4, invite lifecycle,
                                          user creation; the ONLY place that
                                          combines MemberRepo + users writes
app/repositories/invitations.py (new)  — BaseRepo subclass, collection invitations
app/repositories/users.py (new)        — thin users access via admin client
                                          (create/update/disable/lookup by email)
app/api/deps.py                        — additive helpers only (e.g. current
                                          member role for UI conditionals)
app/middleware.py                      — additive: PUBLIC_PATHS += invite routes;
                                          disabled-user check after auth_refresh
scripts_PB: bootstrap_pb.py            — invitations collection + users.disabled
```

- `app/main.py`: register the two routers (inspect existing registration
  pattern first).
- Service layering (AGENTS.md): routers parse/guard/render; `team.py`
  orchestrates; repos touch PocketBase only. No long-running work → no jobs.
- **Session vs data client** (interacts with §18-A): keep `request.state.pb`
  semantics exactly as they are *unless* the Phase-1 hardening decision is
  adopted; if adopted, split validation (user token → `auth_refresh` on a
  throwaway client) from data access (admin client assigned to
  `request.state.pb`). This is the single most delicate change in the brief —
  it must be its own commit with the full test suite green.

## 12. Data Model

### New collection: `invitations` (base)

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | relation → `projects`, cascade | ✓ | invite dies with project |
| `email` | text | ✓ | normalized lowercase; matched against registration/login email |
| `role` | select `owner\|admin\|editor\|viewer` | ✓ | validation in service (I3); store only what was actually grantable |
| `tokenHash` | text | ✓ | SHA-256 hex of the raw token; **raw token never stored** |
| `status` | select `pending\|accepted\|revoked` | ✓ | `expired` is derived, not stored (§13) |
| `invitedBy` | relation → `users`, **not** required, **no cascade | — | survives inviter deletion; null → "deleted user" in UI |
| `acceptedBy` | relation → `users`, not required | — | audit on acceptance |
| `expiresAt` | date | ✓ | created + `settings.invite_ttl_hours` |
| `createdAt`/`updatedAt` | PB auto | ✓ | PB built-ins for display |

Indexes:
- `CREATE UNIQUE INDEX idx_invites_token ON invitations (tokenHash)`
- `CREATE INDEX idx_invites_project_status ON invitations (project, status)`

Creation uses `col(...)` with the default rules **only if** Phase 1 lands
first; otherwise it inherits the too-permissive default — see §18-A.

### `users` additions (extend `ensure_users_fields`)

- `disabled` (boolean, default false) — alongside existing `role`,
  `displayName` (`bootstrap_pb.py:917-943` pattern: add only if missing,
  idempotent).
- PB's built-in fields (`email`, `password`, `emailVisibility`,
  `lastResetAt`…) are used as-is; **UNVERIFIED**: live `users` collection API
  rules — inspect in Phase 0 (`pb.collections.get_one("users")`).

### Derived / no table

- "Expired" invites: computed `expiresAt < now()` at read time.
- Membership audit: `project_members.created/updated` + inviter relation +
  structlog events (`invite.created`, `invite.accepted`, `member.role_changed`,
  `member.removed`, `user.disabled`, …).

## 13. State Machines

### Invitation

```text
(none) --create--> pending
pending --accept (valid token, not expired)--> accepted      [terminal]
pending --revoke (owner/admin)--> revoked                    [terminal]
pending --expiresAt < now()--> expired (derived)             [terminal]
```

- Trigger/preconditions/side effects:
  - **create**: granter is owner/admin (or global admin); email valid &
    normalized; no *pending* invite for same (project, email); optional: no
    existing membership. Side effect: returns raw URL once.
  - **accept**: token matches `tokenHash`, status `pending`, not expired,
    email matches the (new or logged-in) account. Side effects: create user
    if absent (single-use account creation), `MemberRepo.add(project, user,
    invite.role)` (upsert-safe), status → `accepted`, `acceptedBy` set, session
    cookie issued. Re-accept → reject ("this invite has already been used").
  - **revoke**: owner/admin of that project. Idempotent.
  - **expire**: no write needed; every read/accept checks `expiresAt`.
- Terminal states are never transitioned; re-inviting always mints a new row.

### Membership

```text
(none) --invite accept / direct add--> role
role  --change role--> role'          (I3/I4 guard)
role  --remove / leave--> (none)      (I1 guard)
owner --transfer--> admin  +  target: admin --> owner   (promote first)
```

## 14. HTTP / HTMX Contract

Conventions (existing, mandatory): mutations are `POST` + `require_hx` +
`@hx_error(...)`; responses are `ok_with_redirect / success_response /
error_response / hx_trigger`; reads return HTML fragments via
`templates.TemplateResponse`. Non-HTMX mutation → `PermissionError` path.
Truthful statuses: fragment reads 200; HTMX mutation failures stay 200 +
toast (existing pattern — do not invent new codes); full-page invite errors
render dedicated pages, not JSON.

### Project scope — new router `teams.py`

| # | Method & route | Auth | Inputs | Success | Failure (Persian toast/page) |
|---|---|---|---|---|---|
| T1 | `GET /projects/{pid}/tabs/members` | project member (existing tab guard) | — | members tab fragment | existing not-found path |
| T2 | `POST /projects/{pid}/members` | owner/admin/global admin | `email`, `role` | toast; `hx-trigger` refresh list; **or** invite-modal fragment when email unknown | not found / already member / role > own / invalid email |
| T3 | `POST /projects/{pid}/members/{uid}/role` | per matrix §10 | `role` | toast + row refresh | last owner / owner's row / > own role |
| T4 | `POST /projects/{pid}/members/{uid}/remove` | per matrix | — | toast + row refresh | last owner / not permitted |
| T5 | `POST /projects/{pid}/members/leave` | any member | — | redirect `/projects` (303, `ok_with_redirect`) | last owner |
| T6 | `POST /projects/{pid}/members/transfer` | owner only | `uid` | toast + refresh (both rows change) | target not a member / not owner |
| T7 | `POST /projects/{pid}/invites/{iid}/revoke` | owner/admin | — | toast + refresh | already accepted (show info) |
| T8 | `GET /projects/{pid}/invites/{iid}/link` | owner/admin | — | **one-shot** modal fragment containing raw URL (only acceptable right after create — see note) | gone/expired |

Note on T8: because only `tokenHash` is stored, the raw URL exists only in
the creation response. Implement creation so the returned HTML *is* the link
modal (T2's invite outcome); drop T8 entirely if no safe re-display path
exists. Re-display from DB is forbidden (R8).

### Invite redemption — public

Add to `PUBLIC_PATHS` (`app/middleware.py:14`): `/invite` (prefix match like
`/static`).

| # | Method & route | Auth | Inputs | Success | Failure |
|---|---|---|---|---|---|
| C1 | `GET /invite/{token}` | public | — | landing page: states (logged-out-new / logged-out-existing / logged-in-accept / logged-in-wrong-email / accept-form) | error page: unknown / revoked / expired / already accepted |
| C2 | `POST /invite/{token}/accept` | public (registration+accept) | `displayName`, `password`, `passwordConfirm` (email NOT posted — taken from invite) | create user + membership, set `pb_auth`, 303 → `/projects/{pid}` | email-already-exists → render C1 with "log in first" state; weak password / mismatch → inline form errors |
| C3 | `POST /invite/{token}/join` | logged-in, email match | — | HTMX toast + 303 redirect to project | wrong email / token invalid |

- C2 must be a **native form POST with 303** (mirror
  `app/api/auth.py:55-70` cookie rationale), with an HTMX fallback branch like
  the login handler if `HX-Request` is present.
- Registration enforces server-side: email = `invite.email` exactly (after
  lowercase/strip), password ≥ 8 (PB's own minimum also applies), display
  name required.

### Login additions (additive only)

- `GET /login?next=/invite/...`: after successful login redirect to `next`
  **iff** it starts with `/` and not `//` or `/\` (open-redirect guard —
  test explicitly). Default remains `/dashboard?welcome=1`.

### Global admin — new router `admin_users.py`

| # | Method & route | Auth | Inputs | Success | Failure |
|---|---|---|---|---|---|
| A1 | `GET /admin/users` | `require_admin` | — | full page: table (email, displayName, role, status, created jalali) + create form | non-admin → permission toast |
| A2 | `POST /admin/users` | `require_admin` | `email`, `displayName`, `role`, `password?` | toast + refresh; generated password shown once if omitted | duplicate email / invalid |
| A3 | `POST /admin/users/{uid}/role` | `require_admin` | `role` | toast | I2 (last admin) / self-change I4 |
| A4 | `POST /admin/users/{uid}/toggle-disabled` | `require_admin` | — | toast + row refresh | I2 |
| A5 | `POST /admin/users/{uid}/password` | `require_admin` | `password` | toast (never log/echo it) | I2-irrelevant; validation errors |
| A6 | `POST /admin/users/{uid}/delete` | `require_admin` | confirm field must match | toast; memberships cascade | I2; deleting self |

### Account self-service

| # | Method & route | Auth | Inputs | Success | Failure |
|---|---|---|---|---|---|
| S1 | `GET /account` | any user | — | page: displayName + change-password form | — |
| S2 | `POST /account/profile` | any user | `displayName` | toast | empty name |
| S3 | `POST /account/password` | any user | `current`, `new`, `confirm` | toast | wrong current / weak / mismatch |

Add an "Account" link to the user menu in
`app/templates/layouts/platform.html` (near line 87).

## 15. UI / UX

- **Members tab**: add `"members"` to `TABS` (`app/api/projects.py:63-76`,
  suggested position after `"settings"`) **and** to the labels dict in
  `app/templates/pages/projects/detail.html:67-75`
  (`'members': _('Members')`). New template
  `app/templates/pages/projects/tabs/members.html`. Data: extend
  `_tab_context` (`app/api/projects.py`, inspect ~line 330-427) to fetch
  members (`expand=user`), pending invites, and the current user's role for
  conditional rendering — build labels lazily per request (pattern:
  `health_labels()`, `projects.py:91-97`).
- Sections in the tab: ① member table (avatar/initials, displayName, email,
  role badge, joined date `jalali_*`, actions gated by §10), ② invite form
  (email + role select with only grantable options), ③ pending invites table
  (email, role, expiry, revoke), ④ "leave project" button.
- Role badges: DaisyUI `badge` variants (owner=primary, admin=secondary,
  editor=info, viewer=ghost) — consistent with existing badges; no new CSS
  unless unavoidable (then `input.css` + `make css`).
- Destructive confirmations: reuse the project's existing confirm pattern
  (inspect how project delete confirms; mirror it).
- `/admin/users`: full page in `platform.html` layout, added to sidebar nav
  **only when** `current_user.role == "admin"` (wrap the nav tuple, don't
  show it to members).
- Empty states: "no members yet — invite the first one" / "no pending invites". Loading: existing `hx-indicator` pattern. Errors: toasts.

### UI state matrix (invite form — representative)

| State | Behavior |
|---|---|
| Initial | email + role select; submit enabled |
| Loading | `hx-disabled` / indicator text "loading…" |
| Success (existing user) | toast + row appended |
| Success (new email) | invite modal with copyable link |
| Validation error (bad/empty email) | inline error, values preserved |
| Permission error (editor) | form hidden entirely (server also rejects) |
| Conflict (already member / pending invite) | specific Persian toast |
| Server error | `@hx_error` generic Persian toast (existing) |
| Expired/used invite (C1) | dedicated full-page Persian error states |

## 16. Background Jobs

**None.** Invitation expiry is derived at read time; user/membership
operations are sub-second PocketBase writes. Do not register job types; do
not touch `app/workers/worker.py`.

## 17. Integrations

None new. PocketBase only. No email provider (decision (1)); no LLM; no
provider-registry changes. Invite delivery is "human copies a link".

## 18. Security

### 18-A. PREREQUISITE HARDENING (flagged conflict — decide in Phase 0/1)

**Evidence (verified by code reading):**

1. Every collection's API rules default to
   `AUTH_RULE = "@request.auth.id != ''"` with **no overrides anywhere** in
   `bootstrap_pb.py:28-51` (grep confirmed) — i.e. *any logged-in user* may
   list/create/update/delete records in **every** app collection, including
   `project_members`, directly through PocketBase's REST API.
2. `docs/SCHEMA.md:26-27` documents this as intentional ("project-level
   authorization lives in the app layer") — but the app layer is only
   enforceable when clients go through the app.
3. A member can obtain a valid PB token without the app by calling
   `POST /api/collections/users/auth-with-password` with their own
   credentials (PocketBase's public auth endpoint). With that token they can
   upsert `project_members` rows giving themselves `owner` on **any** project,
   and read every project's records — bypassing `require_project_role`
   entirely.
4. Exploitability depends on network exposure of the PocketBase HTTP API
   (UNVERIFIED for production: `PB_URL=https://db.ezdistro.rastin.cloud` in
   `.env.example:17` suggests it *is* internet-reachable — Phase 0 must
   confirm).

**Why it blocks this feature:** role management becomes theater if roles can
be self-granted at the data layer. Shipping the members UI without this fix
creates a false sense of authorization.

**Recommended fix (staged as its own phase/commit):**

- (a) Set all app collections' rules to admin-only (`""` — superuser access
  only; `AUTH_RULE` default removed) in `bootstrap_pb.py`, mirrored to
  `pb_collections_import.json`. Keep the `users` collection rules under
  consideration separately (login must keep working — auth endpoints are
  exempt from `listRule`; verify against live PB in Phase 0).
- (b) Move request data access to the admin client: validate the session
  with the user's token (`auth_refresh` on a throwaway/unauthenticated client
  purely to resolve `request.state.user`), then hand repos an
  admin-authenticated client as `request.state.pb`. All authorization stays
  in `deps.py` — exactly where `SCHEMA.md` says it lives; PB stops being a
  second, weaker authorization layer.
- (c) In the same pass, enforce `users.disabled` inside `AuthMiddleware`
  after refresh (clear store when disabled).
- **Rollback**: rules and client wiring are config/code, no data migration —
  revert commit restores prior behavior.
- **Stop condition**: if (b) proves too invasive mid-implementation (e.g.
  worker/queue interplay), fall back to locking *only*
  `project_members` + `invitations` to admin-only rules and routing their
  writes through `get_admin_pb()` in `team.py` — and explicitly report the
  remaining read-disclosure risk instead of silently proceeding.

#### Phase 0 findings & decision (2026-09-25)

**Decision: ADOPT full 18-A hardening (a + b + c) as Phase 1.**

Findings:

1. **Evidence 1 re-verified**: grep for explicit `*_rule=` arguments in
   `bootstrap_pb.py` finds **none** — all collections take the `col()`
   defaults, so changing the defaults flips every collection. Note the code
   now defines **20** collections (incl. `article_images`,
   `worker_heartbeats`), while `pb_collections_import.json` still lists
   **18** (pre-existing drift: the two v1.1/v1.3 collections were never
   mirrored; `users` is PB-built-in and appears in neither file).
2. **Deployed rules**: `pb_collections_import.json` — all 18 entries have
   `listRule = createRule = "@request.auth.id != ''"` (same pattern for
   view/update/delete). Confirms any logged-in user can CRUD every
   collection via PB REST.
   **CORRECTION (later):** this finding is wrong — the artefact actually
   contained `""`, not `AUTH_RULE`. PocketBase rule semantics are
   `null` = locked (superusers only) and `""` = **public, guests included**,
   so every collection was open to the internet, anonymously. The `AUTH_RULE`
   premise in this section (and the `""`-means-superuser-only reading in #4)
   is inverted; see `docs/SCHEMA.md:26`. Fixed in the bootstrap defaults,
   `lock_pb_rules.py`, `pb_collections_import.json` and the assertions in
   `tests/test_security_hardening.py`.
3. **PB network exposure (evidence 4)**: `PB_URL=https://db.ezdistro.rastin.cloud`
   is internet-routed (front proxy answers), but the backend was
   **unreachable during inspection**: initial probe returned 200, all
   subsequent probes returned Go `404 page not found` for every path and
   the TLS cert became self-signed → live superuser auth and live rules
   inspection **could not be performed**; no local PocketBase exists on
   this host (no `pb` binary, no `pb_data/`). **Open item: re-run live
   verification (rules, `users` collection rules, bootstrap idempotency)
   as soon as the instance is reachable — required for Phase 1 acceptance
   (§28 stop-and-report applies if still down).**
4. **`users` collection**: not managed by bootstrap's `COLLECTIONS`; only
   `ensure_users_fields()` adds `role`/`displayName`. Its live rules are
   unknown (blocked by #3). Plan: new idempotent `ensure_users_rules()`
   setting all five rules to `null` (locked = superuser-only; `""` would have
   meant public). PB auth endpoints
   (`auth-with-password`, `auth-refresh`) do not consult `listRule`, so
   login/refresh should keep working — **must be verified live**; fallback
   if broken: `listRule/viewRule = "id = @request.auth.id || @request.auth.role = 'admin'"`,
   `create/update/delete = ""` (never a self-update rule — that would allow
   self-promotion to `role=admin` via REST).
5. **Blast radius of (b) is tiny**: grep shows the only auth-store users are
   `app/middleware.py` (session refresh), `app/api/auth.py:39,50` (login
   `auth_with_password` + token read), `app/pb.py`, worker/bootstrap
   (`get_admin_pb`, unaffected). Plan: middleware validates the cookie on a
   throwaway `get_pb()` purely to resolve `request.state.user`, then sets
   `request.state.pb = get_data_pb()` — a process-cached admin client
   (re-auth at most every 300 s, lock-guarded); `auth.py` login switches to
   its own fresh `get_pb()` so it can never clobber the shared admin
   client's auth store.
6. **PB files are unaffected by rule tightening**: `pb_file` URLs
   (`app/templates.py`) are fetched by browsers and server code with **no
   auth token**, yet images work in production today under
   `viewRule=AUTH_RULE` → `/api/files` does not enforce API rules;
   additionally `article_images` is not bootstrap-managed (finding #1).
7. **SDK shape**: `auth_store.model` is a `pocketbase` `Record` (pydantic
   model), not a dict — the `disabled` check must handle dict *and*
   attribute access.
8. **Hermetic-test impact of (b)**: TestClient-based tests
   (`test_auth_http.py`, `test_app.py`, `test_i18n.py`,
   `test_routes_misc.py`) would otherwise hit the real PB through the new
   `get_data_pb()` → conftest gains an autouse patch of
   `app.middleware.get_pb` + `get_data_pb`, and the auth fixture also
   patches `app.api.auth.get_pb`.
9. **(c) disabled enforcement**: after a successful `auth_refresh`,
   `AuthMiddleware` clears the store when the user is disabled (anonymous →
   existing 303 to `/login`, plus a `?disabled=1` Persian notice);
   `POST /login` re-checks the freshly authenticated model and refuses with
   a distinct Persian error.

Rollback stays as stated above (config/code only, no data migration).

### 18-B. Feature-level requirements

- **Authentication**: no new auth paths; C2 creates accounts only with a
  valid 256-bit (`secrets.token_urlsafe(32)`) single-use token — entropy makes
  enumeration infeasible; no separate rate limiter required (deferred).
- **Authorization**: every new route uses `require_hx`, `require_user`,
  `require_admin`, `require_project_role`, `ensure_record_in_project` as
  appropriate; matrix §10 in the service layer, not in templates.
- **Object-level**: role/remove/invite endpoints are addressed by
  `project_id` in the path; verify the target member/invite *belongs* to that
  project before mutation (mirror `ensure_record_in_project`).
- **CSRF**: HTMX-only mutations + `samesite=lax` cookie (existing model);
  C2 native POST protected by same-site cookie + token capability.
- **XSS**: displayName/email/invite fields rendered escaped; never `|safe`
  (Phase 0 verifies autoescape — A4).
- **Open redirect**: `next` validation (§14) — regression test mandatory.
- **Secrets**: invite raw token shown once, stored hashed, **never logged**
  (structlog events carry only invite id/email/project); admin-set passwords
  never echoed after submit nor logged.
- **Privilege escalation**: I3/I4 invariants; role select options restricted
  server-side, not just hidden in HTML.
- **Information disclosure**: user directory (A1) is admin-only; invite flow
  does exact-email lookup server-side and returns "user exists" only inside
  the owner/admin-gated members tab; member emails visible within the same
  project only.
- **Injection**: filters built with the repo's quoting pattern (see existing
  `first(filter=...)` usage) — never f-string raw user input into PB filter
  expressions without quoting; email is validated before use.
- **Replay**: single-use token (status + unique hash); direct-add upsert is
  intentional but gated by §10.
- Use the `app-sec` skill during Phase 1/7 review.

## 19. Performance

- Member list: single `list_records(expand="user", per_page=…)` — no N+1.
- Invite acceptance: ~4 PB round-trips (find, create-or-find user, membership,
  invite update) — bounded.
- `/admin/users`: paginated (`page_size` setting), never `list_all` on a
  growing users table.
- Exact-email lookups only (indexed by PB on `email`); no fuzzy search over
  the user directory in v1.
- Tab context must not fetch members data for non-members tabs (guard on
  `tab == "members"` inside `_tab_context`).

## 20. Reliability

- PB outages surface as the existing `@hx_error` toast; no partial states —
  membership upsert is one write; invite accept order: create user → create
  membership → mark accepted (if step 3 fails, token remains `pending` but
  membership exists → re-accept must detect existing membership and complete
  idempotently rather than erroring).
- Ownership transfer: promote first, demote second (§R14).
- Concurrent role changes: last-write-wins on `project_members` row; the
  I1 owner-count check re-reads immediately before demote/remove (best-effort
  under race; document as accepted limitation — PB has no transactions).
- Disabled-user race: a request already past middleware completes; next
  request is blocked (accepted).

## 21. Migration

- `make bootstrap` is idempotent: new `invitations` collection + `users.disabled`
  are added only if missing (existing `ensure_users_fields` pattern).
- **No data backfill**: existing memberships untouched; existing users get
  `disabled=false` implicitly (unset → falsy).
- Keep `pb_collections_import.json` in sync (manual-import path).
- Rules change (18-A) affects only new/existing collection configs — verify
  with `make bootstrap` against a dev instance and confirm app routes still
  read/write (that is the migration validation).
- Rollback: revert code + re-run bootstrap with old definitions; no destructive
  steps ever run first (§27 phase order enforces this).

## 22. Testing

New files (patterns from `tests/test_routes_security.py`,
`tests/test_authorization.py`, `tests/test_prompts_members.py`):

- `tests/test_members_routes.py`
  - role matrix: parameterized viewer/editor/admin/owner × T2–T6 expected
    outcomes (mirror `test_mutations_reject_viewer_role`).
  - I1: last owner cannot be demoted/removed/leave; transfer always leaves
    ≥ 1 owner; promote-before-demote order.
  - I4: self role-change endpoint rejected.
  - Non-member + anonymous rejected on every T-route (existing security table
    style).
  - `expand=user` response contains member emails (single query).
- `tests/test_invitations.py`
  - create → raw URL returned once; DB stores only `tokenHash` (assert raw
    token absent from record), `expiresAt` ≈ now + TTL.
  - accept: happy path creates user+membership+status accepted; second accept
    fails; expired/revoked/wrong-email fail with distinct outcomes.
  - C2 rejects posted email mismatch (email taken from invite, not the form).
  - duplicate pending invite for same (project, email) blocked.
  - Public access: C1/C2 reachable without session (middleware bypass), all
    other invite mutations require auth.
- `tests/test_admin_users.py`
  - `require_admin`: member gets permission error on A1–A6.
  - I2: last enabled admin cannot be disabled/deleted/demoted (incl. self).
  - create duplicate email → error; disable sets flag; delete cascades
    memberships (fake PB cascade emulation — check `tests/fakes.py` supports
    it; if not, assert via service-level behavior).
  - S3 wrong current password → error, password unchanged.
- `tests/test_login_next.py` (or fold into existing auth tests)
  - `next=/invite/x` honored; `next=//evil.com`, `next=/\evil`,
    `next=https://evil` fall back to `/dashboard`.
- Middleware: disabled user's valid token → treated as logged out.
- i18n: new UI strings asserted in Persian (existing convention).

Also update existing tests only if behavior contracts changed (they should
not — §6).

Run: focused files while iterating
(`.venv/bin/pytest tests/test_invitations.py -q`), then `make check` with a
long timeout (~3 min suite; rerun the known-flaky
`test_retry_uses_jittered_backoff` before believing a failure).

## 23. Documentation

- `docs/SCHEMA.md`: new `invitations` section (§ numbering), `users` section
  (+`disabled`), update principle 7 if 18-A lands, update "Last verified".
- `docs/USER-GUIDE.md`: "Team management" section — invite flow, role meanings,
  link-only caveat (share the link yourself), account self-service.
- `docs/CONFIGURATION.md` + `.env.example`: `INVITE_TTL_HOURS` (if made
  env-configurable) — and note web now (also) requires `PB_ADMIN_*` if 18-A(b)
  adopted.
- `docs/ARCHITECTURE.md`: authorization section — where session validation vs
  data access now happen; new routers.
- `README.md`: feature bullet (team/roles) if user-facing features are listed.
- Run `make i18n-extract && make i18n-update` then translate new `en` strings
  and `make i18n-compile`.

## 24. Implementation Phases

Each phase: implement → run relevant tests → `make lint`/`make typecheck` →
report (files, checks, decisions, limitations) before starting the next.
Commit per phase via `git-hygiene` (logically grouped, no drive-by refactors).

**Phase 0 — Discovery & decision gate (read-only + report)**
- Verify: live PB exposure & current rules; `users` collection rules +
  `emailVisibility`; `app/templates.py` autoescape (A4); web env has
  `PB_ADMIN_*`; `tests/fakes.py` capabilities (auth, cascade, unique);
  existing confirm-dialog pattern; `app/main.py` router registration;
  `_tab_context` internals.
- Deliver the 18-A decision: adopt full hardening / minimal fallback / defer
  (defer = report accepted risk explicitly; not recommended).
- Acceptance: written findings appended to this doc's §18-A / Assumptions.

**Phase 1 — Security prerequisite (18-A) if adopted**
- Scope: rules in `bootstrap_pb.py` + import json; session-vs-data client
  split in `middleware.py`/`pb.py`; disabled check in middleware; docs note.
- Acceptance: full existing suite green (it runs on fakes — also manually
  verify `make web` + `make bootstrap` against dev PB: read a project, write a
  topic, login/logout); no route regressions.
- Tests: middleware disabled-user unit test; a bootstrap-rules assertion test
  (rules == admin-only for app collections).

**Phase 2 — Schema**
- `invitations` collection, `users.disabled`, `invite_ttl_hours` setting,
  import-json mirror.
- Acceptance: idempotent `make bootstrap` twice; SCHEMA.md updated.

**Phase 3 — Members tab core (A)**
- `team.py` invariants, `TABS`+labels+template, routes T1–T6, nav gating.
- Acceptance: J2 (existing user), J5 journeys work end-to-end; role matrix
  tests green.

**Phase 4 — Invitations (B)**
- `InvitationRepo`, routes T7 (+T8 if kept), C1–C3, `PUBLIC_PATHS`,
  login `next`, invite landing/error pages.
- Acceptance: J3/J4 journeys; `tests/test_invitations.py` green.

**Phase 5 — Global users + self-service (C, D)**
- `admin_users.py` A1–A6, `/account` S1–S3, sidebar/user-menu links.
- Acceptance: J1/J6; `tests/test_admin_users.py` green; I2 proven.

**Phase 6 — i18n, CSS, docs (F)**
- Persian pass over every new string; `make i18n-*`; `make css` only if CSS
  changed; all §23 docs.
- Acceptance: no untranslated literals in new templates (grep for Latin-only
  visible strings); docs consistent with code.

**Phase 7 — Tests & security review**
- Complete §22 matrix; run `app-sec` review of the new surface; fix findings.
- Acceptance: `make check` green end-to-end.

**Phase 8 — Final hostile QA (§27)**

## 25. Acceptance Criteria

- **AC1** Given a global admin on `/admin/users`, when they create a user,
  then the user can log in with the given/generated password and appears in
  the list; a duplicate email is rejected with a Persian toast.
- **AC2** Given an owner on the members tab, when they submit an unknown
  email with role `editor`, then an `invitations` row exists (status
  `pending`, `tokenHash` only — no raw token anywhere in the DB), and a copy
  link modal shows `/invite/{token}`.
- **AC3** Given that link opened by a logged-out stranger, when they register
  with the invited email, then an account + `editor` membership are created,
  the invite is `accepted` and single-use, and they land on the project page
  authenticated; a second visit to the link shows the "already accepted"
  page.
- **AC4** Given a viewer session, when they POST T2–T7 (parameterized), then
  every request is rejected and no membership/invite row changes.
- **AC5** Given a project with one owner, when that owner tries to demote or
  remove themselves or leave, then the action is blocked with a Persian
  explanation and the owner count stays 1.
- **AC6** Given an owner + admin member, when ownership is transferred, then
  afterwards target=owner and initiator=admin (both rows updated), never
  zero owners.
- **AC7** Given a disabled user with a still-valid session cookie, when they
  make any request, then they are treated as logged out and login is refused
  with an "account disabled" message.
- **AC8** Given the last enabled global admin, when an admin tries to
  disable/demote/delete them (even themselves), then it is blocked.
- **AC9** Given `?next=//evil.com` on login, then the redirect target is
  `/dashboard`, never an external host.
- **AC10** Given the adopted 18-A decision, when a member attempts to write
  `project_members` directly via the PocketBase REST API with their own
  token, then the write is refused (or, under the fallback, refused for
  `project_members`/`invitations` with the residual risk documented).
- **AC11** `make check` passes; new templates render Persian/RTL; new
  strings are in the `en` catalog; docs updated.

## 26. Definition of Done

- All phases 0–8 complete with per-phase reports.
- `make check` green; flaky test re-verified on any reported failure.
- i18n compiled; CSS rebuilt if touched; docs + schema mirror updated.
- §25 AC1–AC11 demonstrated (tests or manual evidence).
- No unresolved critical/high findings from the hostile QA pass (§27) —
  anything unresolved is listed explicitly with risk.
- Clean `git-hygiene` history: one coherent commit per phase/milestone.

## 27. Final Hostile QA Pass

Act as a hostile senior staff engineer doing a production-readiness review of
the whole implementation. Inspect and report (fix when safe, add regression
tests, verify, list anything unresolved):

- **Architecture**: routers thin? service owns invariants? no authorization
  logic leaking into templates? no job/email/provider creep?
- **Data**: unique token index live? cascade directions right? `invitedBy`
  survives user deletion? I1/I2 enforced at every write path incl. cascade
  deletions (project delete, user delete)? camelCase everywhere?
- **Security**: every new route guarded (§10 matrix × routes table)? public
  routes minimal? token hashed + single-use + TTL? no token/password in
  logs or toasts? open-redirect test present? XSS (escaped displayName)?
  injection via email filter strings? PB rules per 18-A actually deployed?
  disabled check in middleware?
- **Jobs/AI**: none added (assert).
- **UI**: empty/loading/error/permission states all Persian? mobile dock and
  RTL layouts sane? confirm dialogs on destructive actions? copy-link works
  without editing `app.js`? tab label wired in both `TABS` and labels dict?
- **Performance**: no N+1 on members; no `list_all` on users; tab context
  lazy.
- **Testing**: matrix complete? weakest assertions replaced (e.g. "returns
  200" → assert row state)? AC coverage traceable to test names?
- **Operations**: bootstrap idempotent (run twice)? import json synced?
  `INVITE_TTL_HOURS` documented? web `PB_ADMIN_*` documented if needed?
  structlog modules whitelisted for mypy?
- **Docs**: SCHEMA/USER-GUIDE/CONFIGURATION/ARCHITECTURE consistent with the
  shipped code; i18n compiled.

## 28. Execution Rules

- Work phase by phase; never batch phases 0–8 into one change. Run
  `make check` (long timeout) before claiming done; report actual results —
  never claim unrun checks passed.
- **Stop and report** (don't guess) when: the Phase 0 facts contradict §7;
  the 18-A decision changes mid-flight; any step could destroy data; the
  fake-PB cannot express a needed test; a requirement here conflicts with
  code you find.
- Use `git-hygiene` for commit format; `app-sec` for the security pass;
  `text-craft`-style thinking for test design; `doc-craft` for §23 docs.
- Quality gate before delivery: another engineer can follow this brief
  without reading the original conversation; every assumption is visible
  (§5 Assumptions, §18-A); every failure path has an expected outcome.
