# CXplorer

CXplorer is a server-rendered web application built with FastAPI, Jinja templates, plain CSS,
optional dependency-free JavaScript, and external account authentication. Users sign in with an
existing provider account; CXplorer has no separate account registration or password.

## Architecture

```text
.
|-- .github/
|   |-- agents/                 # Repository-level GitHub Copilot custom agents
|   `-- workflows/ci.yml
|-- src/cxplorer/
|   |-- ai/                     # Typed AI configuration and OpenAI SDK adapters
|   |-- auth/                   # Session identity, OAuth client, redirect validation
|   |-- insights/               # Safe sources, structured research, transient jobs, cache codec
|   |-- routers/                # Public, private, and authentication URL groups
|   |-- static/                 # Plain CSS and optional browser JavaScript
|   |-- templates/              # Server-rendered, mobile-first Jinja UI
|   |-- config.py
|   `-- main.py                 # FastAPI application factory
`-- tests/
```

HTML is rendered by FastAPI and Jinja. Static assets are served directly from
`src/cxplorer/static/`; there is no Node.js runtime, package manager, bundler, or frontend build
step.

Public pages and APIs do not require a session. Private pages redirect unauthenticated browser
requests to the login page, while private APIs return `401 Unauthorized`.

| Access | URL | Purpose |
|---|---|---|
| Public | `/` | Landing page |
| Public | `/login` | Provider-neutral sign-in page |
| Public | `/api/health` | Health check |
| Public | `/auth/microsoft/login` | Start Microsoft authentication |
| Public | `/auth/microsoft/callback` | Complete Microsoft authentication |
| Private | `/dashboard` | Company source entry and browser-saved reports |
| Private | `POST /insights` | Verify sources and start a bounded generation job |
| Private | `/insights/{report_id}` | Progress, sourced report, or browser-cache restoration |
| Private | `POST /insights/restore` | Restore an authenticated user's signed browser report |
| Private | `/api/private/insights/{report_id}/status` | Owner-authorized job status |
| Private | `/api/private/insights/{report_id}/cache` | Signed, compressed report for localStorage |
| Private | `/insights/{report_id}/download` | Download an owned report as JSON |
| Private | `/api/private/me` | Current authenticated user |
| Private | `/auth/logout` | CSRF-protected local logout |

## Sign-in and company insights

Microsoft Entra ID is the only currently supported sign-in provider. Other social providers, such
as Facebook and LinkedIn, are not enabled. The sign-in page uses neutral product copy and names
the provider only when choosing how to continue. Successful sign-in opens the workspace at
`/dashboard` by default, while preserving validated local return destinations.

The provider must supply a usable email address. A username is not automatically an email, and
CXplorer does not prompt for an email or create an internal account when the provider omits it.
Sign-in fails with an actionable message instead.

`LOGIN_ALLOWED_EMAILS` optionally restricts sign-in after the provider email has been validated.
It accepts a JSON array or comma-separated list of exact addresses and whole-email `*` patterns,
such as `["seller@contoso.com","*@contoso.com"]`. Matching is case-insensitive and covers the
entire address, so `*@contoso.com` does not match `seller@contoso.com.example`. If the setting is
unset, blank, or an empty list, every provider-validated email address may sign in.

One official company homepage is required; three to six public HTTPS URLs are recommended, with
six as the maximum. About/Company and Products/Services/Solutions pages are strongly recommended.
Up to three optional sources can cover investor relations or an annual report, recent news,
trust/security, industry solutions or customer case studies, or careers/engineering signals
(lower confidence). Public HTML pages and text-based PDFs are suitable; sources behind logins or
paywalls are not. Supplied URLs must be accessible and refer to the same company as the official
homepage; an About page for another company is rejected before evidence generation. Fetching
protects against private-network/metadata access, unsafe redirects, oversized content, and
unsupported pages. A matching hostname alone is not proof that page content concerns the company.
Initial `www`/apex redirects are recognized only for public registrable-domain pairs. Later
research is restricted to company-verified final hosts, not every supplied or redirected-from host.

HTTPX is the primary source client, using a Windows Chrome/Edge browser user-agent. An HTTP 403
or a recognized browser-challenge response triggers one bounded `cloudscraper` fallback with
`browser={"browser": "chrome", "platform": "windows", "desktop": True}`. The synchronous fallback
runs in a disposable Python worker, within the existing request deadline and concurrency limits,
with at most four HTTP requests for its challenge exchange. Both clients verify TLS with Certifi's
CA bundle and pin connections to validated public addresses. Redirects still require explicit
host approval and robots checks; browser headers do not change the `CXplorerInsights` robots
identity. Fallback cookies exist only within that attempt and are never persisted or shared.
Authentication, paywalls, CAPTCHAs, robots restrictions, and HTTP 429 rate limits are not bypassed.
Unsupported or unresolved challenges fail explicitly instead of being ingested as company content.

Official company announcements are always included in research, using a default 90-day window
and up to three accepted articles, especially executive announcements. There is no news opt-in
or opt-out. Search candidates must stay within approved official company hosts, be fetched and
matched to the company, and have a supported publication date. Third-party news coverage,
personal profiles, login-only content, and undated items are not substituted for official news.
Add the company's official newsroom or investor-relations URL when it uses a separate host.

The Python pipeline collects bounded source text, extracts evidence, consolidates a company
dossier, develops a shared strategy, generates separate CEO/CTO/CIO/CFO/CISO talk points, and
reviews the result. Every AI stage returns native schema-constrained JSON; Python owns IDs,
citations, authorization, quotas, timestamps, and publication. Missing evidence and incomplete
audience coverage remain visible rather than being filled with invented facts. Repairs and
retries are bounded and count toward the same job budgets.
Accepted shared opportunities can support discovery-oriented executive analysis and hypotheses
even when public sources do not state a role's priorities. CXplorer attempts one optional,
audience-local coverage correction before retaining limited coverage; this does not consume the
shared artifact-repair round reserved for substantive final-review findings. It does not add
external peer-company stories.
A reviewer must accept server-owned source metadata. Core profile or evidence rejections must
identify concrete affected fact IDs; vague insufficient-evidence decisions receive one local
review-contract correction before they can affect publication.
Every other schema-valid but domain-invalid pre-review artifact may also receive one task-local
validation correction. These local corrections do not consume the shared artifact-repair round;
only substantive final-review findings can consume it and trigger dependent regeneration.
A retry is available only when its remaining work and a fresh quality review fit those budgets.
Filling missing sections keeps the report ID and its original generation/expiry dates.

An extracted quantity whose numeric text does not exactly match its supporting quotation is
retained with a **warning**, rather than stopping generation. The report shows a notice and marks
the affected quantities as unverified; compare them with the cited excerpts before use. These
warnings travel with the facts in downloaded JSON and signed browser copies. Equivalent numeric
formatting such as `500`/`500.0` and `1,000`/`1000` is normalized for comparison.

Other extracted numeric or reporting-period inconsistencies receive one task-local correction
attempt. If they remain inconsistent, CXplorer omits only the affected candidate fact and adds a
**numeric and period warning** to the report instead of failing the whole job. The unsupported
number, unit, currency, or period is never published as accepted evidence. Source references,
entities, attribution, and all numeric metadata on retained facts remain strictly validated.
CXplorer may recover a model-provided quotation with minor textual drift only when the match is
high confidence, unambiguous, and preserves numbers and meaning-sensitive terms. It replaces the
model text with the exact retained source substring and adds a **quotation matching warning**.
Unsupported numbers introduced by later dossier, strategy, audience, or review stages still block
those artifacts.

A related source no longer fails solely because its selected relation quotation omits the company
name. When the company is present elsewhere in the retained page title or text, CXplorer accepts
the source and shows a **source identity warning** in the report and source notes. The source still
needs an exact supporting quotation, and matching hostnames alone never establish company identity;
pages with no retained company-name evidence remain rejected.

The verified company name itself remains source-backed. If dossier generation references a valid
fact whose excerpt does not name the company, CXplorer deterministically rebinds that one reference
to an accepted fact from an included source whose quotation does identify the company. It does not
change the company name or proceed when no accepted identifying quotation exists.

### Browser storage and stateless execution

Input drafts and signed, gzip-compressed reports are retained in localStorage for seven days,
including across sign-out. Keys use only the SHA-256 hash of the authenticated session email:

```text
cxplorer:<email_hash>:draft
cxplorer:<email_hash>:report:<report_id>
```

There is no version in the keys. The same email uses the same namespace across sign-in providers;
changing email changes the namespace. Unreadable, malformed, or undecompressible entries are
invalid, not candidates for guessed migrations. Temporary network or expired-login failures do
not justify deleting readable cached data. Expiry cleanup occurs when the application runs;
localStorage cannot remove entries while the application is closed.

Browser storage is the only persistent report copy. It is not encrypted, is not synchronized
between browser profiles/devices, and can be lost through expiry, eviction, or clearing site data.
Neither the email hash nor a report signature is a login credential. OAuth tokens, CSRF tokens,
email, and the login profile are not stored in these caches. Storage failures are shown explicitly;
valid reports are not silently evicted to make space. A download provides a user-controlled copy
outside the seven-day browser-cache policy.

Saved reports are posted back to a private endpoint for signature, ownership, expiry, size, and
schema enforcement, then rendered by Jinja. No AI calls or source fetching are needed to restore
a valid report, even when new generation is disabled. JavaScript is necessary for localStorage
save/restore; ordinary server-rendered generation/progress works while transient data exists.
Keep `SESSION_SECRET` stable across restarts: it also protects saved-report signatures. Rotating
it invalidates existing browser copies; a previous-key verification grace policy is not implemented.

Jobs, accepted stage artifacts, and temporary results live only in bounded process memory.
Run one application process for the initial deployment. Reloads/restarts lose unfinished work
and unsaved results; the application never silently starts another billable generation. Multiple
replicas require an explicit routing/coordination decision, not an assumption that job memory is
shared. Temporary results/checkpoints expire after the configured retention period.

## Configuration

Application and identity configuration use independent settings classes in
`src/cxplorer/config.py`. AI vendor/task registries are loaded separately by
`src/cxplorer/ai/config.py`:

| Shared file (committed) | Private override (ignored) | Settings class |
|---|---|---|
| `app_config.env` | `app_config.local.env` | `AppSettings` |
| `id_vendor.env` | `id_vendor.local.env` | `IdentityVendorSettings` |
| `ai_vendors.env` | `ai_vendors.local.env` | `AIVendorSettings` |
| `ai_tasks.env` | `ai_tasks.local.env` | `AITaskSettings` |

The shared files document settings inline and accept configuration changes without a fixed
key/value allowlist; there are no separate example files. Sensitive settings must be empty or
clear placeholders, such as `remember to generate a secure secret for production`.
Application/session settings, `LOGIN_ALLOWED_EMAILS`, and `RELOAD` belong to `AppSettings`;
external identity-provider settings belong to `IdentityVendorSettings`.

Each class reads its shared UTF-8 dotenv file followed by its optional private override file.
Precedence is **constructor values > environment > local overrides > shared files > model defaults**.
Application and identity configuration remain separate. The filenames are
relative to the working directory, so start the application from the repository root.
Production can provide private settings through environment variables without local files.

Never put secrets, API keys, client/tenant identifiers, or private hostnames in the committed
files. Keep those values in the ignored local overrides or supply them through environment
variables. Do not commit or deploy developer-specific local files.

Settings are loaded at startup. Restart the application after changing configuration files or
environment-variable overrides; `RELOAD` controls Python source reloading, not dotenv-file changes.

The application exposes the separate instances as `app.state.app_settings` and
`app.state.identity_settings`. The server launcher also uses `AppSettings` for `RELOAD`; it does
not load identity-provider credentials.

Supply a real, secure `SESSION_SECRET` through private overrides before deployment; a committed
placeholder is documentation, not a production secret. Identity-provider configuration is
optional: missing or empty credentials disable sign-in; supplying only one of the Microsoft
client ID and secret is a configuration error.

**Migrating an existing installation:** move private or deployment-specific values from the old
`.env` or populated shared files into the matching `.local.env` files before tracking the shared
files. Preserve `SESSION_SECRET` and provider credentials to keep the existing setup working.
The legacy `.env` is not loaded.

### AI vendors and task routing

AI availability is detected automatically at startup; there is no manual `AI_ENABLED` setting.
OpenAI is enabled by `CX_AI__OPENAI__API_KEY` alone and uses the SDK's default endpoint.
AzureOpenAI requires `CX_AI__AZURE_OPENAI__ENDPOINT` plus either its API key or all three
Azure Identity process environment variables documented below. Empty/placeholder API keys
do not enable a vendor. If at least one vendor is enabled, generation is enabled; otherwise
startup logs a warning listing the missing configuration requirements.

Tasks retain their configured routing. A task selecting an unavailable vendor fails at runtime
with an explicit vendor/task error; the application does not substitute another vendor or model.
Availability checks do not acquire tokens or prove API permissions. Restart after changing
configuration. `AI_VENDOR_CONFIG_FILE` defaults to `ai_vendors.env`, and
`AI_TASK_CONFIG_FILE` defaults to `ai_tasks.env`. Legacy `AI_ENABLED` values are ignored.

Both AI settings classes use Pydantic `BaseSettings`, `env_nested_delimiter="__"`,
`nested_model_default_partial_update=True`, and `extra="ignore"`. Pydantic handles dotenv files,
environment variables, JSON configuration trees, source precedence, and scalar conversion.
Unrelated configuration roots are ignored; typed nested fields and operational bounds are still
validated. Partial overrides retain the other vendor/task defaults.

Vendor variables use **`CX_AI__<VENDOR>__<CONFIG>`**, with canonical vendor keys `OPENAI` and
`AZURE_OPENAI`. Task and pipeline roots remain `CX_AI_TASK` and `CX_AI_PIPELINE`.
Both OpenAI and AzureOpenAI use the official OpenAI Python SDK. Task `VENDOR` values are normalized:
`OpenAI`, `Open AI`, and `Open-AI` select OpenAI; `AzureOpenAI`, `Azure OpenAI`, and
`Azure-OpenAI` select AzureOpenAI. Underscore/case variants are also accepted.
Task routing defaults to **AzureOpenAI**. If both Azure credential forms are configured,
`AUTH_MODE` selects the preference (default `entra_id`); if only one is configured, it is used
automatically. Authentication failures do not trigger a fallback to another credential form.
The SDK uses the server's Azure credential chain; the user's website sign-in and the existing
`MS_*` login configuration do not supply Azure AI authorization.

For Azure, put the real resource endpoint in the ignored `ai_vendors.local.env`, for example:

```dotenv
CX_AI__AZURE_OPENAI__ENDPOINT="https://contoso.openai.azure.com/openai/v1/"
CX_AI__AZURE_OPENAI__AUTH_MODE="entra_id"
```

The endpoint above is illustrative. Configure an Azure API key or the Azure Identity environment
variables below with access to the AI resource. No Azure access token is written into configuration
or browser storage.

For client-secret service-principal authentication, set `AZURE_TENANT_ID` (directory/tenant ID),
`AZURE_CLIENT_ID` (application/client ID), and `AZURE_CLIENT_SECRET` (the secret **value**, not its
ID) in the **process environment**. CXplorer checks only that all three are nonblank;
`DefaultAzureCredential` discovers and handles their values directly. The application does not
parse, validate, store, or pass identity values itself. A managed/workload/local Azure session
without these variables does not by itself satisfy the application's availability rule.
The commented examples in `ai_vendors.env` are
documentation only: neither shared nor local dotenv settings files export variables into the
process environment. Keep Azure AI authorization separate from website `MS_*` login settings.

**Migrating AI configuration:** rename old vendor keys such as `CX_AI_AZURE_OPENAI__ENDPOINT`
to `CX_AI__AZURE_OPENAI__ENDPOINT` in private overrides and environment settings. Legacy
vendor-key shapes are no longer aliases. Move any `AZURE_*` identity values previously placed
in `ai_vendors.local.env` into the process environment; private files are not rewritten automatically.

To use an Azure API key, supply `CX_AI__AZURE_OPENAI__API_KEY` privately with the Azure endpoint.
Set `AUTH_MODE="api_key"` only if both forms are configured and you want to prefer the key.
OpenAI needs only `CX_AI__OPENAI__API_KEY`; no endpoint setting is required.

Task settings use this structure in `ai_tasks.env` or its private override:

```dotenv
CX_AI_TASK__GENERATE_TALK_POINTS__VENDOR="AzureOpenAI"
CX_AI_TASK__GENERATE_TALK_POINTS__MODEL="gpt-5.6-sol"
CX_AI_TASK__GENERATE_TALK_POINTS__REASONING_EFFORT="High"
CX_AI_TASK__GENERATE_TALK_POINTS__USE_WEB_SEARCH=False
CX_AI_TASK__GENERATE_TALK_POINTS__MAX_TOOL_CALLS=25
```

`MODEL` is the vendor's exact model ID or Azure deployment name; the shared labels are starting
configuration, not proof that those deployments exist in your account. `DISCOVER_NEWS` has web
search enabled and official-domain restrictions; generation tasks default to no web tools.
The registry also includes `VERIFY_SOURCES`, `EXTRACT_EVIDENCE`, `CONSOLIDATE_EVIDENCE`,
`SYNTHESIZE_STRATEGY`, and `REVIEW_REPORT`. Reasoning effort is normalized case-insensitively.
The files document task budgets, tool-call bounds, and `CX_AI_PIPELINE__*` operating limits inline.
Missing credentials, unsupported routes/parameters, refusals, and exhausted budgets are surfaced
explicitly; there is no simulated-report or cheaper-model fallback.
A completed model response that fails its registered JSON/Pydantic output contract receives one
task-local contract correction using the same vendor, model, typed input, and schema. A second
contract failure stops the task. This correction does not consume the shared final-review
artifact-repair round; refusals and incomplete responses are not converted into contract retries.

The shared configuration allows a model request to run for up to 600 seconds. Vendor
`REQUEST_TIMEOUT_SECONDS` is the SDK per-attempt ceiling, while task `TIMEOUT_SECONDS` is the
whole-task deadline across retries. The existing 900-second job deadline remains the final
end-to-end bound, so a call can receive less time when little job time remains.

CXplorer initializes root logging at `INFO` with
`%(asctime)s - %(levelname)s - %(name)s: %(message)s`. `CustomLoggerConfig` defaults newly created
`azure.*` loggers to `WARNING` while application loggers retain normal levels.

Each model attempt emits paired `AI task before` / `AI task after` records. Before logs include
the task, vendor, model/deployment, reasoning effort, web-search setting, tool-call limit, and
effective timeout. After logs include status, duration, sanitized request ID,
input/output/total tokens, cached-input, cache-write, and reasoning tokens when the provider
reports them. The Responses API does not expose a separate reliable web-search token total, so
CXplorer records `web_search_calls` instead of inventing one. Prompts, source content, model
output, credentials, and raw provider exception text are never included in these lifecycle logs.
Output-contract failures add a separate safe diagnostic with the parser/validation stage, error
count, registered schema paths, and validation error types; rejected values are never logged.
Insights jobs emit paired `step before` / `step after` records for every server-owned stage, plus
queueing, start, accepted/rejected pipeline task status, correction rounds, safe result counts,
completion, cancellation, and failure duration. Validation-correction logs identify the task,
artifact, and safe issue code. Final-review logs identify issue sections, referenced-fact counts,
repair target owners, repair availability, and repair origin. They do not log user inputs, source
URLs or text, report content, reviewer prose, owner hashes, or progress-message content.

The configured discovery deployment must support native hosted web search. Azure searches
indexed content rather than browsing live pages; access can be subscription-restricted, separately
billed, and subject to Bing processing policies. `store=false` is not a zero-retention guarantee.
CXplorer independently fetches and verifies accepted candidates and never silently substitutes a
broader search provider when the configured capability is unavailable.

## Local development

Python 3.12+ is required. For a new checkout, run these commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Create the ignored `app_config.local.env` and set `SESSION_SECRET` to the generated value there,
or provide it as an environment variable. Put only the settings you need to override in the
local file. Set `RELOAD=true` there to restart the development server when Python source changes,
or keep the shared `false` default for a single process. Then start the app:

```powershell
.\.venv\Scripts\python server.py
```

Open <http://localhost:8000>. The API documentation is available at
<http://localhost:8000/api/docs> by default outside production. `DOCS_ENABLED` can override this
through `app_config.local.env` or the environment.

## Microsoft authentication

Create an application registration in Microsoft Entra ID and add this Web redirect URI:

```text
http://localhost:8000/auth/microsoft/callback
```

Choose the supported account types appropriate for the application. The default tenant value,
`common`, accepts both organizational and personal Microsoft accounts. Add the application
(client) ID and a client secret to the ignored `id_vendor.local.env`, or provide them through
environment variables:

```dotenv
MS_CLIENT_ID=your-application-id
MS_CLIENT_SECRET=your-client-secret
MS_TENANT=common
```

`MS_TENANT=organizations` limits sign-in to organizational tenants, while `consumers` limits it
to personal accounts. A directory tenant UUID restricts sign-in to that tenant; it is distinct
from the application/client ID.

Microsoft's shared discovery documents advertise an issuer containing `{tenantid}`. CXplorer
checks the token's GUID `tid`, its exact tenant-specific issuer, the configured tenant audience,
and the signing key's issuer scope, following
[Microsoft's issuer validation guidance](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#validate-the-issuer).
Signature, application audience, expiry, and nonce validation remain enabled. Invalid tokens
return to the sign-in error page rather than creating a session.

After a failed callback, start a fresh sign-in instead of reusing the callback URL: authorization
codes are short-lived and single-use. Do not share full callback query strings or token values
in logs, screenshots, or issue reports.

The application stores only validated identity claims in its signed session cookie; OAuth access
and ID tokens are not persisted. In production, use a strong independent `SESSION_SECRET`, set
`ENVIRONMENT=production`, leave secure cookies enabled, and list only the deployment hosts in
`ALLOWED_HOSTS` through private overrides or environment variables. Configure
`LOGIN_ALLOWED_EMAILS` when access must be restricted, configure the production callback URI,
and supply provider credentials through `id_vendor.local.env` or environment variables, never
through the committed `id_vendor.env`.

The same application secret derives a purpose-separated report-signing key. Keep it stable across
restarts if browser reports must remain restorable. Rotating it invalidates existing signed
sessions and report copies unless a separately managed verification-key grace policy is provided.

## Quality checks

Ruff is the Python linter and formatter; pytest is the unit-test runner.

```powershell
ruff check .
ruff format --check .
pytest
```
