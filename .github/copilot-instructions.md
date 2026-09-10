# CXplorer repository instructions

CXplorer is a Python 3.12 FastAPI application with server-rendered Jinja templates and plain
HTML/CSS/JavaScript. Keep public routes in `src/cxplorer/routers/public.py`, authentication routes in
`src/cxplorer/routers/authentication.py`, and authenticated routes in
`src/cxplorer/routers/private.py`.

- Always use `Contoso` as the fictional company name in UI previews, examples, fixtures, tests,
  and documentation.
- Treat every new route as private unless its public purpose is explicit.
- Use `require_user` for private APIs and redirect unauthenticated private pages to `/login`.
- Apply `LOGIN_ALLOWED_EMAILS` only after the provider email is validated. Match exact addresses
  and whole-email `*` patterns case-insensitively; an unset, blank, or empty list allows all.
- Never persist OAuth access tokens, ID tokens, client secrets, or session secrets.
- Validate redirect targets with `safe_local_path` and protect state-changing browser requests
  with a session-bound CSRF token.
- Keep templates accessible, semantic, mobile-first, and free of inline scripts or styles so the
  Content Security Policy remains strict.
- Use production-ready UI copy. Do not label application pages as draft, preview, or
  non-production; label synthetic landing-page examples as illustrative instead.
- Maintain shared styles directly in `src/cxplorer/static/css/app.css`.
- Use the landing page and the shared `:root` tokens in `app.css` as the visual reference for
  every page: dark navy (`#020617`) background, slate surfaces, near-white text, muted slate
  secondary text, indigo/cyan accents, and white primary buttons.
- Keep the shared sans-serif typography, rounded components, subtle borders and shadows, and
  translucent header. Inherit `base.html` and its `#020617` theme color; reuse the existing
  components instead of introducing page-specific palettes, fonts, or radius tokens.
- Do not introduce light/warm-paper or serif themes on individual pages unless explicitly
  requested. Login, workspace, and future pages must remain visually consistent with the landing.
- Use dependency-free browser JavaScript only for progressive enhancement, place it under
  `src/cxplorer/static/js/`, and keep all rendering and authorization decisions on the server.
- Do not introduce Node.js, a frontend package manager, a CSS framework, or a frontend build step
  without an explicit architectural decision.
- Load generic application settings, including `RELOAD`, with `AppSettings` from `app_config.env`.
  Load external identity-provider settings with `IdentityVendorSettings` from `id_vendor.env`;
  keep the existing `MS_*` variable names. Both classes live in `src/cxplorer/config.py`.
- Commit `app_config.env` and `id_vendor.env` with only safe public defaults and inline setting
  documentation. Do not keep duplicate `.env.example` files for these configurations.
- Allow any configuration keys and non-sensitive values in shared files, not a fixed allowlist.
  Sensitive values must be empty or clear placeholders, such as
  `remember to generate a secure secret for production`; never commit real sensitive values.
- Keep secrets, API keys, client/tenant identifiers, and private deployment details out of shared
  files. Put private overrides in ignored `app_config.local.env` and `id_vendor.local.env`, or
  supply them through environment variables.
- Configuration precedence is constructor values, then environment variables, matching local
  overrides, shared files, and model defaults. The legacy `.env` is not loaded.
- Keep AI vendor/auth settings in `ai_vendors.env` and task/routing limits in `ai_tasks.env`,
  with inline safe defaults and ignored matching `.local.env` overrides. Load them with
  `AIVendorSettings`/`AITaskSettings` using Pydantic `BaseSettings` and `env_nested_delimiter="__"`.
  Use `CX_AI__<VENDOR>__<CONFIG>` and `CX_AI_TASK__<TASK>__<CONFIG>` keys; normalize task
  vendor values rather than maintaining legacy environment-key aliases.
- Use the official OpenAI Python SDK for OpenAI and AzureOpenAI. Default task routing to
  AzureOpenAI with the server's Entra ID credential chain; never reuse website OAuth tokens.
  Document `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` as SDK-owned process
  environment variables; only check their presence, without loading/passing identity values.
- Keep shared task defaults on `gpt-5.6-luna`, `gpt-5.6-terra`, or `gpt-5.6-sol`; do not route
  default tasks to GPT-6 Astra. Reasoning effort is limited to `low`, `medium`, or `high`.
- Derive AI availability from vendor configuration, never a manual `AI_ENABLED` flag.
  OpenAI requires its API key only (SDK default endpoint); Azure requires its endpoint plus
  its API key or all three Azure Identity process variables. Any enabled vendor enables AI;
  tasks targeting an unavailable vendor fail explicitly at runtime without switching vendors.
- Require a usable provider-supplied email to sign in. Cache namespaces are SHA-256 of that
  session email only; do not add provider/issuer/subject or a version to localStorage keys.
- Keep jobs/results transient and bounded in memory, without a database or permanent server
  report store. Browser drafts/reports survive sign-out and expire after seven days.
- Treat unreadable, malformed, or undecompressible cache entries as invalid; never trust
  cached HTML. Restore signed structured reports through authenticated server rendering.
- Verify supplied sources against the official homepage. Always research official company
  announcements from the last 90 days; provide no news opt-in/out or third-party fallback.
- Keep HTTPX as the primary source client. Browser fallbacks must remain bounded, isolated,
  certificate-verified, and pinned to validated public addresses; preserve source/redirect
  authorization and robots checks. Keep challenge cookies within one attempt, never persistent
  or shared with another fetch or user.
- Keep every AI stage schema-constrained JSON with source-backed references, explicit gaps,
  and finite budgets/retries. Models do not own IDs, authorization, or source-fetch permissions.
- Give a completed response that fails its JSON/Pydantic output contract one task-local typed
  correction. Keep it on the same vendor/model/schema, do not consume final-review artifact repair,
  and log only safe contract stages, schema paths, and validation types.
- Build executive talk points from accepted shared opportunities as clearly labeled analysis or
  hypotheses when role-specific public priorities are unavailable. Attempt one local coverage
  correction before retaining limited coverage; do not introduce external peer-company stories.
- Keep all pre-review domain-validation corrections local, including optional audience coverage
  and review-contract corrections; they must not consume the shared artifact-repair round reserved
  for substantive final-review findings. The reviewer must accept server-owned source metadata,
  and core rejections must cite affected fact IDs.
- Treat the verified company name as server-owned. If a dossier cites the wrong valid fact for that
  name, rebind it to an accepted included-source fact whose quotation identifies the company; still
  fail when no accepted identifying quotation exists.
- Accept only high-confidence quotation drift, canonicalize it back to the exact retained source
  substring, and show a report warning. Changed numbers, negation, ambiguous matches, entities,
  source/span references, attributions, and unsupported claims remain blocking.
- Start the local application with `python server.py`; the `RELOAD` environment setting controls
  Uvicorn code reloading.
- Run `ruff check .`, `ruff format --check .`, and `pytest` for backend changes.
- Ruff is the linter and formatter. Pytest is the unit-test framework.
