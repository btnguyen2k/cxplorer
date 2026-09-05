# CXplorer repository instructions

CXplorer is a Python 3.12 FastAPI application with server-rendered Jinja templates and plain
HTML/CSS/JavaScript. Keep public routes in `src/cxplorer/routers/public.py`, authentication routes in
`src/cxplorer/routers/authentication.py`, and authenticated routes in
`src/cxplorer/routers/private.py`.

- Treat every new route as private unless its public purpose is explicit.
- Use `require_user` for private APIs and redirect unauthenticated private pages to `/login`.
- Never persist OAuth access tokens, ID tokens, client secrets, or session secrets.
- Validate redirect targets with `safe_local_path` and protect state-changing browser requests
  with a session-bound CSRF token.
- Keep templates accessible, semantic, mobile-first, and free of inline scripts or styles so the
  Content Security Policy remains strict.
- Maintain shared styles directly in `src/cxplorer/static/css/app.css`.
- Use dependency-free browser JavaScript only for progressive enhancement, place it under
  `src/cxplorer/static/js/`, and keep all rendering and authorization decisions on the server.
- Do not introduce Node.js, a frontend package manager, a CSS framework, or a frontend build step
  without an explicit architectural decision.
- Start the local application with `python server.py`; the `RELOAD` environment setting controls
  Uvicorn code reloading.
- Run `ruff check .`, `ruff format --check .`, and `pytest` for backend changes.
- Ruff is the linter and formatter. Pytest is the unit-test framework.
