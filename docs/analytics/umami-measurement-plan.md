# Umami measurement plan

Product analytics for the hosted EzDistro deployment (`app.ezdistro.space`)
and the marketing site (`ezdistro.space`). One website entity covers both, so a
visitor who lands on the marketing site and later signs in is one journey.

Umami is **optional**: with `UMAMI_WEBSITE_ID` empty, no tracker is rendered,
no event is emitted, and the application behaves exactly as before.

## 1. The question this answers

**Where do interested visitors stop moving — at signup, at connecting
WordPress, or before publishing a first article?** Every event below exists to
locate that leak, not to decorate a dashboard.

## 2. Website / entity model

| Host | Status |
|---|---|
| `ezdistro.space` + `/blog` | Marketing pages are already instrumented by `EzDistro-Marketing` with the same website ID. The WordPress blog theme (`EzDistro-Blog`) is **not** instrumented yet — add the same snippet to `header.php` |
| `app.ezdistro.space` (this app) | Instrumented by `base.html` with the same website ID and `data-domains=app.ezdistro.space` |

Separate website entities are unnecessary: one product, one owner, one journey.
Segment by hostname and path inside the single entity.

## 3. Tracker configuration

Environment (see also `docs/CONFIGURATION.md`):

| Variable | Default | Meaning |
|---|---|---|
| `UMAMI_WEBSITE_ID` | *(empty)* | Website ID from the Umami dashboard. Empty disables all tracking |
| `UMAMI_SCRIPT_URL` | `https://cloud.umami.is/script.js` | Tracker URL. Self-hosted: `https://stats.example.com/script.js` |
| `UMAMI_DOMAINS` | *(empty)* | Comma-separated hostnames allowed to report (`app.ezdistro.space`) |

Live deployment: `UMAMI_SCRIPT_URL=https://metrics.rastin.cloud/script.js`,
website id `0535d710-6d23-4ed7-9cd2-d4abb877aa23`, domains
`app.ezdistro.space`.

The tracker is embedded whenever a website ID is set; `data-domains` keeps
localhost, preview and staging hostnames out of production data. There is no
consent manager in this app today; Umami is cookieless by design (no cookies,
no cross-site tracking) and honours Do Not Track (`data-do-not-track`), matching
the marketing site and the published privacy policy.

Implementation:

- `app/templates/base.html` — the tracker + `app/static/js/analytics.js`.
- Pageviews and HTMX navigations need **no** code: Umami patches
  `history.pushState`, which is what `hx-boost` calls, so one pageview is
  recorded per boosted navigation.
- Server-confirmed mutations return an `analytics-event` HX-Trigger entry
  (`app/utils.py::analytics_event`); `analytics.js` forwards it to
  `umami.track`. Events therefore fire in the user's browser session, which
  keeps funnel attribution intact.
- No distinct IDs are sent: Umami's anonymous session (IP + user agent) spans
  the OAuth redirect, and a distinct ID would split the journey in two.

## 4. Event dictionary

All names are lowercase `snake_case`, under Umami's 50-character limit.

| Event | Fires when | Source | Safe properties | Decision it supports |
|---|---|---|---|---|
| `signup_completed` | PocketBase creates a brand-new `users` record during the OAuth callback (the account is confirmed at that point) | Client — the callback redirects to `/dashboard?welcome=1`, `analytics.js` strips the flag and records the event | — | Is the login page converting? |
| `project_created` | A website (project) row was created successfully | Client — HX-Trigger on `POST /projects` | `cloned` (bool) | Do new accounts reach their first website? |
| `wordpress_connected` | A **new** `publisher` integration is saved (first connection for that website; later edits do not re-fire) | Client — HX-Trigger on `POST /projects/{id}/integrations` | `provider` (e.g. `wordpress`) | Is the account ready to publish? The real activation event. |
| `article_publish_requested` | A publish job was queued for an article with content (the action succeeds) | Client — HX-Trigger on `POST …/articles/{id}/publish` | — | Does the operator ship a first article? |
| `purchase_completed` | The checkout-return page renders a **provider-reconciled** paid invoice | Client — `/billing/checkout/return`, once per browser session per invoice | `plan` (product code, e.g. `cloud-monthly`) | Is activation turning into revenue? |

Deliberately not tracked: clicks, hovers, keystrokes, per-section edits, prompt
content, raw URLs connected to WordPress, article titles, customer content,
emails, invoice amounts. No PII or credentials are ever sent.

`article_publish_requested` is named for what it is: the queue accepted the request.
Actual publish success is asynchronous and already visible in the app's own
`publishing_runs` data; it is not inferred here.

## 5. Funnels

Create these as saved Umami funnels (**Add funnel → steps**); the window is
minutes between consecutive steps.

| Funnel | Steps (in order) | Window | Question |
|---|---|---|---|
| Marketing → Signup | `app_login_click` (marketing site) → `signup_completed` → `project_created` | 7 days (`10080`) | Does marketing traffic become accounts that start setup? |
| Acquisition → Signup | Viewed page `/login` → `signup_completed` | 24 h (`1440`) | Which acquisition sources reach signup, and where do they bounce? |
| Signup → Activation | `signup_completed` → `project_created` → `wordpress_connected` | 7 days (`10080`) | Where do new accounts stall before they can publish? |
| Activation → Paid | `wordpress_connected` → `purchase_completed` | 30 days (`43200`) | Does a publish-ready account convert? |
| Output adoption | `project_created` → `article_publish_requested` | 30 days (`43200`) | Do accounts get from setup to a first article? |

`app_login_click` is emitted by the marketing site for every link to the app,
so "Marketing → Signup" includes returning users; cross-check the signup page's
referrer when the rate looks implausibly high.

## 6. Reports worth opening

- **Traffic by hostname**: split marketing vs app; the app is authenticated,
  so its pageviews measure return usage, not acquisition.
- **Referrers / UTM source, medium, campaign**: keep `utm_*` parameters when
  linking from the marketing site and external channels.
- **Entry pages** on the marketing host and `/login` on the app host.
- **Events table**: counts for the five events, filtered by hostname.
- **Retention** on `article_publish_requested` once there are four or more weeks of
  data — the closest proxy for repeated product value.
- Ignore pages with fewer than ~30 visitors per period; direction is noise.

## 7. Caveats

- **The blog is not yet tracked.** Posts under `/blog` are a separate WordPress
  theme; until it loads the tracker, journal traffic is missing from the journey.
- **Anonymous sessions.** Umami identifies a visitor by IP + user agent (no
  cookies). Office/VPN/shared networks can merge visitors, and a browser change
  splits one person into two.
- **Ad blockers** can drop the tracker; server-side mutations still succeed, so
  event counts are a floor, not a census.
- **Do Not Track** is honoured: browsers sending it are not counted at all.
- **Not a billing ledger.** Revenue truth lives in the PayRam invoices and the
  append-only credits ledger; `purchase_completed` is a conversion signal.
- **Self-hosted installs** have no website ID and report nothing — intentional.
- **Dev/staging** is excluded by `UMAMI_DOMAINS`: on any hostname not listed,
  the tracker loads but sends nothing. Don't add staging hosts to the production
  website's domain list — give staging its own Umami website instead.

## 8. Validation checklist

1. Set `UMAMI_WEBSITE_ID`, `UMAMI_SCRIPT_URL` and `UMAMI_DOMAINS` and restart
   the web process; confirm the tracker URL is reachable from the browser.
2. Load `/login`, open DevTools → Network: exactly one `script.js` and one
   `POST …/api/send` (type `pageview`).
3. Click any HTMX link; confirm exactly one additional pageview (no duplicate,
   no full reload).
4. Create a website on a fresh account: `project_created` appears in Umami.
5. Add a WordPress connection: `wordpress_connected` appears once; editing the
   same connection does not repeat it.
6. Queue a publish with an approved article: `article_publish_requested` appears.
7. Complete a test checkout and land on the return page: `purchase_completed`
   appears once, including `plan`; refreshing the page does not repeat it.
8. Sign in with a brand-new OAuth account: `signup_completed` appears, and the
   address bar shows `/dashboard` without `welcome=1`.
9. Block the tracker (DevTools → request blocking) and repeat steps 4–6: the
   app must keep working and log no JS errors.

## 9. Decision guide

| Observation | Inspect | Hypothesis to test |
|---|---|---|
| `/login` high, `signup_completed` low | Provider error flags (`?error=`), OAuth callback registration, account-create rule | Login copy or a broken provider; verify the callback URL at both providers before changing the page |
| `signup_completed` high, `project_created` low | Dashboard first-run state, empty state, onboarding copy | The next action is not obvious enough for a new account |
| `project_created` high, `wordpress_connected` low | Website setup tab, connection form friction | Setup asks for too much before the first value moment |
| Activated, `article_publish_requested` low | Content plan → article workspace → review gate | The approval/publish path has too many steps for a first article |
| Activated, `purchase_completed` low | Billing page, plan fit, price | Monetization timing or pricing, not activation, is the blocker |

These are hypotheses. Confirm with a session recording, a support conversation
or a test change — never read a funnel percentage as a cause.
