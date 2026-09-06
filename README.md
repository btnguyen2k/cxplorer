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
| Public | `/login` | Provider-neutral sign-in page |
| Public | `/api/health` | Health check |
| Public | `/auth/microsoft/login` | Start Microsoft authentication |
| Public | `/auth/microsoft/callback` | Complete Microsoft authentication |
| Private | `/dashboard` | Draft workspace for company URLs |
| Private | `/api/private/me` | Current authenticated user |
| Private | `/auth/logout` | CSRF-protected local logout |

## Sign-in and draft workspace

Microsoft Entra ID is the only currently supported sign-in provider. Other social providers, such
as Facebook and LinkedIn, are not enabled. The sign-in page uses neutral product copy and names
the provider only when choosing how to continue. Successful sign-in opens the workspace at
`/dashboard` by default, while preserving validated local return destinations.

The workspace is a draft input surface, not an insight generator. URLs can be edited on the page,
but are not submitted, saved, or fetched, and cannot be resumed as a saved exploration.

One official company homepage is required; three to six public HTTPS URLs are recommended, with
six as the maximum. About/Company and Products/Services/Solutions pages are strongly recommended.
Up to three optional sources can cover investor relations or an annual report, recent news,
trust/security, industry solutions or customer case studies, or careers/engineering signals
(lower confidence). Public HTML pages and text-based PDFs are suitable; sources behind logins or
paywalls are not. These sources will eventually support company insights and separate executive
talk points.

## Configuration

Configuration is split between two independent settings classes in `src/cxplorer/config.py`:

| Shared file (committed) | Private override (ignored) | Settings class |
|---|---|---|
| `app_config.env` | `app_config.local.env` | `AppSettings` |
| `id_vendor.env` | `id_vendor.local.env` | `IdentityVendorSettings` |

The shared files document settings inline and accept configuration changes without a fixed
key/value allowlist; there are no separate example files. Sensitive settings must be empty or
clear placeholders, such as `remember to generate a secure secret for production`.
Application/session settings and `RELOAD` belong to
`AppSettings`; external identity-provider settings belong to `IdentityVendorSettings`.

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

The application stores only validated identity claims in its signed session cookie; OAuth access
and ID tokens are not persisted. In production, use a strong independent `SESSION_SECRET`, set
`ENVIRONMENT=production`, leave secure cookies enabled, and list only the deployment hosts in
`ALLOWED_HOSTS` through private overrides or environment variables. Configure the production
callback URI and supply provider credentials through `id_vendor.local.env` or environment
variables, never through the committed `id_vendor.env`.

## Quality checks

Ruff is the Python linter and formatter; pytest is the unit-test runner.

```powershell
ruff check .
ruff format --check .
pytest
```
