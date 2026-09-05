# CXplorer

CXplorer is a server-rendered web application built with FastAPI, Jinja templates, plain CSS,
optional dependency-free JavaScript, and Microsoft OpenID Connect authentication.

## Architecture

```text
.
|-- .github/
|   |-- agents/                 # Repository-level GitHub Copilot custom agents
|   `-- workflows/ci.yml
|-- src/cxplorer/
|   |-- auth/                   # Session identity, OAuth client, redirect validation
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
| Public | `/login` | Microsoft login page |
| Public | `/api/health` | Health check |
| Public | `/auth/microsoft/login` | Start Microsoft authentication |
| Public | `/auth/microsoft/callback` | Complete Microsoft authentication |
| Private | `/dashboard` | Authenticated application shell |
| Private | `/api/private/me` | Current authenticated user |
| Private | `/auth/logout` | CSRF-protected local logout |

## Local development

Python 3.12+ is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated value in `SESSION_SECRET` in the root `.env`. Set `RELOAD=true` to restart the
development server automatically when source files change, or leave it `false` for a single
process. Then start the app:

```powershell
.\.venv\Scripts\python server.py
```

Open <http://localhost:8000>. The API documentation is available at
<http://localhost:8000/api/docs> when `DOCS_ENABLED=true`.

## Microsoft authentication

Create an application registration in Microsoft Entra ID and add this Web redirect URI:

```text
http://localhost:8000/auth/microsoft/callback
```

Choose the supported account types appropriate for the application. The default tenant value,
`common`, accepts both organizational and personal Microsoft accounts. Add the application
(client) ID and a client secret to `.env`:

```dotenv
MS_CLIENT_ID=your-application-id
MS_CLIENT_SECRET=your-client-secret
MS_TENANT=common
```

The application stores only validated identity claims in its signed session cookie; OAuth access
and ID tokens are not persisted. In production, use a strong independent `SESSION_SECRET`, set
`ENVIRONMENT=production`, leave secure cookies enabled, configure the production callback URI,
and list only the deployment hosts in `ALLOWED_HOSTS`.

## Quality checks

Ruff is the Python linter and formatter; pytest is the unit-test runner.

```powershell
ruff check .
ruff format --check .
pytest
```
