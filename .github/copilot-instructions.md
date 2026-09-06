# CXplorer repository instructions

CXplorer is a Python 3.12 FastAPI application with server-rendered Jinja templates and plain
HTML/CSS/JavaScript. Keep public routes in `src/cxplorer/routers/public.py`, authentication routes in
`src/cxplorer/routers/authentication.py`, and authenticated routes in
`src/cxplorer/routers/private.py`.

- Always use `Contoso` as the fictional company name in UI previews, examples, fixtures, tests,
  and documentation.
- Treat every new route as private unless its public purpose is explicit.
- Use `require_user` for private APIs and redirect unauthenticated private pages to `/login`.
- Never persist OAuth access tokens, ID tokens, client secrets, or session secrets.
- Validate redirect targets with `safe_local_path` and protect state-changing browser requests
  with a session-bound CSRF token.
- Keep templates accessible, semantic, mobile-first, and free of inline scripts or styles so the
  Content Security Policy remains strict.
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
- Start the local application with `python server.py`; the `RELOAD` environment setting controls
  Uvicorn code reloading.
- Run `ruff check .`, `ruff format --check .`, and `pytest` for backend changes.
- Ruff is the linter and formatter. Pytest is the unit-test framework.
