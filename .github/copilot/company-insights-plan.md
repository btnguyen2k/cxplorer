# Company insights and executive talk points

Date: 2026-09-05

Status: Proposed MVP plan. This document specifies future behavior; it does not implement the
pipeline, configuration loaders, persistence, or AI integrations.

## 1. Product outcome

After logging in, a technical seller supplies a few public company URLs. CXplorer collects
company information, builds a source-backed company profile, and generates separate AI-first
talk-point sets for the executive audiences selected by the seller.

The report should help a seller answer three questions: what matters to this company, where AI
could improve its business, and how to discuss that opportunity with a particular executive.
It must distinguish company-reported facts from analysis and hypotheses. A citation proves where
a statement came from; it does not independently prove that a company's marketing claim is true.

Prioritize report quality and time to a useful result over minimizing token prices. Keep cost
bounded through selective source collection, shared evidence, task-specific model choices, and
finite retries rather than removing the evidence or quality-review stages.

The MVP is vendor-neutral from the seller's perspective. An optional seller context can describe
the seller's offerings and meeting objective. Without it, do not invent a product portfolio,
commercial relationship, customer commitment, or product capability.

## 2. What URLs should the user supply?

Require one company homepage. Recommend three to six seed URLs, with six as the initial maximum.
The best starting set is the homepage, About page, and a Products, Services, or Solutions page.
Let users label a URL's purpose, but treat the label as a hint rather than evidence.

| Priority                   | URL                                                                | What it contributes                                                        | Most useful audiences |
|----------------------------|--------------------------------------------------------------------|----------------------------------------------------------------------------|-----------------------|
| Required                   | Official company homepage                                          | Company identity, market positioning, main offerings, navigation           | All                   |
| Strongly recommended       | About, Company, or Who We Are                                      | Business model, footprint, mission, customer segments                      | CEO, CFO              |
| Strongly recommended       | Products, Services, or Solutions                                   | What the company actually sells and which workflows create value           | CEO, CTO, CIO         |
| High-value optional        | Investor relations, latest annual report, or strategy presentation | Stated priorities, financial context, material risks, investment direction | CEO, CFO, CIO         |
| High-value optional        | Newsroom or a recent strategic announcement                        | Current initiatives, expansion, partnerships, timing for a conversation    | All                   |
| High-value optional        | Trust, Security, Compliance, or Responsible AI                     | Published controls, data commitments, assurance and governance context     | CISO, CIO, CTO        |
| Optional                   | Industry solutions or customer case studies                        | Operating context and examples of delivered value                          | CEO, CTO, CIO         |
| Optional, lower confidence | Careers or engineering blog                                        | Possible technology and capability-building signals                        | CTO, CIO              |

For a listed company, favor an annual report and a recent strategic announcement over several
generic marketing pages. For a private company, favor substantive product, industry, and customer
pages. A job advertisement is not proof of the company's deployed technology stack.

### Collection behavior

- Accept public HTTPS HTML pages and text-based PDFs. Do not require authenticated pages,
  personal executive profiles, LinkedIn login, or paid databases.
- Offer bounded discovery, enabled by default: follow relevant About, Products, Investors,
  Newsroom, and Trust links on the approved company hosts. The initial cap is 12 fetched documents,
  including user-supplied seeds; this is not an unrestricted crawl.
- Approve the supplied hosts and their validated canonical apex/`www` counterparts. Other hosts,
  including external investor portals or filing services, require an explicit seed or authorization.
  Do not authorize a destination merely because it appears in page content.
  Do not broaden a shared-hosting tenant's hostname to its parent domain.
- Prefer relevant, recent information, particularly announcements from the last 12 months.
  Preserve publication dates and reporting periods; retrieval time is not publication time.
- Prioritize substance over page count. A dated annual report can be more useful than ten
  undated pages. Deduplicate repeated content and track conflicting versions.
- Treat unrelated companies, subsidiaries, and similarly named businesses as an entity-resolution
  issue. Do not silently combine their facts into one company.
- Show rejected, blocked, stale, unreadable, and omitted sources with reasons. If a site requires
  JavaScript or a PDF is scanned, request an alternative public source rather than guessing.
- Respect site access policies and rate limits. Do not bypass authentication, paywalls, CAPTCHAs,
  robots restrictions, or other access controls.

One usable homepage can produce a limited report, but insufficient evidence must remain visible.
If no usable company evidence exists, stop with an actionable error and request better URLs.

## 3. Report contract

### Company information

Include the company's name, website, business description, industries, products/services, target
customers, geographic footprint, recent initiatives, and stated strategic priorities when sourced.
Include financial, technology, leadership, and security information only when supported by the
collected evidence. Preserve units, currency, reporting period, and whether a number is actual,
forecast, or a company claim. Unknown fields remain unknown.

The report also contains an executive summary, prioritized AI opportunities, evidence gaps,
contradictions, an as-of date, and a source list with clickable citations and short supporting
excerpts. Never infer security weaknesses from the absence of a public trust page.

### Separate executive audiences

Generate distinct sets for CEO, CTO, CIO, CFO, and CISO by default. CTO and CIO should be separate,
not a single paragraph with a changed title. Users may select a subset. COO and other roles can be
added later through explicit audience definitions. The initial security persona is the CISO;
a broader Chief Security Officer remit needs a separate definition if it includes physical security.

| Audience | Primary lens                              | AI-first conversation direction                                              | Example measures to validate                               |
|----------|-------------------------------------------|------------------------------------------------------------------------------|------------------------------------------------------------|
| CEO      | Growth, differentiation, operating model  | Redesign a valuable business workflow around AI-enabled capabilities         | Revenue per employee, time to market, customer retention   |
| CTO      | Product and engineering capability        | AI-native product experiences, evaluation, architecture, build/buy decisions | Release lead time, product adoption, quality and unit cost |
| CIO      | Enterprise delivery and information       | Data readiness, integration, workflow modernization, adoption and governance | Process cycle time, service quality, integration effort    |
| CFO      | Financial value and investment discipline | A measurable portfolio of AI investments with explicit costs and stage gates | Cost per transaction, payback assumptions, cost to serve   |
| CISO     | Risk, resilience, assurance               | Govern AI identities, data access, models, suppliers, and human approvals    | Control coverage, incident response time, residual risk    |

These measures are proposed evaluation criteria, not claims about the company's current results.

Each audience receives a short opening, three prioritized talk points, discovery questions, likely
objections with balanced responses, and a concrete next-step ask. Each talk point contains:

- The company signal and supporting evidence IDs.
- A seller-ready line explaining why the signal matters to this audience.
- An explicitly labeled AI opportunity or hypothesis, not an asserted undisclosed company plan.
- The affected workflow and business outcome.
- Data, integration, ownership, adoption, and security prerequisites.
- A discovery question and a measurable success criterion without invented savings or ROI.
- Appropriate human oversight and a feasible pilot or next decision.

### What AI-first means here

Begin with the business outcome and reconsider how the workflow could operate with AI.
Consider assistive, augmentative, and bounded autonomous approaches, not just adding a chatbot.
Evaluate data access, feedback loops, reliable evaluation, integration, human decision rights,
and operating-model change alongside model capability.

Recommend non-AI automation, a smaller intervention, or no AI where it is the better fit.
Do not suggest replacing executive judgment with an autonomous system. Talk points must be
specific to the company and audience rather than generic AI slogans.

## 4. End-to-end execution

Use a deterministic, bounded workflow with typed AI tasks, not a free-running browsing agent.
The backend decides which URLs may be fetched and which tasks run. Models cannot authorize network
access, choose credentials, execute arbitrary tools, or change workflow limits.

```text
Authenticated submission
  -> validate URLs, audiences, CSRF, ownership, and quotas
  -> persist a queued job and return its progress page
  -> fetch approved sources and extract clean text
  -> extract evidence from bounded source chunks in parallel
  -> reconcile the evidence into one company dossier
  -> synthesize company strategy and AI opportunities
  -> generate a separate talk-point set for each audience in parallel
  -> validate citations, schema, coverage, and report quality
  -> repair affected sections once if necessary, then review again
  -> persist the accepted report and render it with Jinja
```

| Stage                  | Execution                       | Output and control                                                    |
|------------------------|---------------------------------|-----------------------------------------------------------------------|
| Submission             | Python, no AI                   | Validated request, owner reference, requested audiences, job ID       |
| Collection             | Bounded `httpx` fetching, no AI | Source metadata, content hashes, dates, fetch outcomes                |
| Text preparation       | HTML/PDF parsing, no AI         | Clean sections and chunks with source/page/section references         |
| `extract_evidence`     | Luna, per selected chunk        | Structured candidate facts with exact supporting excerpts             |
| `consolidate_evidence` | Terra                           | Deduplicated company dossier, contradictions, evidence gaps           |
| `synthesize_strategy`  | Astra                           | Shared strategic brief, prioritized AI hypotheses, prerequisites      |
| `generate_talk_points` | Sol, one call per audience      | Role-specific talk points grounded in the shared dossier and strategy |
| `review_report`        | Astra, fresh review context     | Structured pass/revise decision with section IDs and evidence issues  |
| Publication            | Python and Jinja, no AI         | Authorized, escaped HTML from validated report data                   |

The evidence ledger retains source-backed facts and quotations, not just lossy page summaries.
Downstream tasks receive relevant evidence and the shared strategy, avoiding repeated full crawls
and separate, contradictory company research for each audience.

Task prompts require a complete registered output schema. Unknowns and discovery questions are
report data, not an interactive model conversation that pauses the job waiting for an answer.

Chunk and rank long documents before model calls. Reserve input/output budget for synthesis,
all requested audiences, and review before scheduling extraction. Record excluded sections and
sources; never silently truncate evidence and claim full coverage.

### Failure and repair behavior

- Retry transient timeouts, `429`, and selected `5xx` errors with bounded backoff and jitter,
  respecting `Retry-After`, the task deadline, and the remaining job budget.
- Do not retry authentication failures, invalid model names, invalid configuration, or prohibited
  URLs as if they were transient.
- Validate every model response against a task-specific schema. Handle refusals, incomplete
  output, and invalid JSON explicitly; structured output support is not proof of factual accuracy.
- Return review issues only to the task responsible for the affected section. Permit one repair
  round and one subsequent review, not an unlimited conversation between models.
- Never silently substitute a cheaper model, another vendor, or a generic successful-looking
  report. A different route requires an explicit configuration change or approved policy.
- Retain successful stage artifacts so retries do not re-fetch or regenerate unaffected work.
  Record attempts separately; a timed-out provider request may still have been billed.
- When repaired evidence or strategy changes, invalidate and regenerate its dependent artifacts
  within the remaining budgets. Never publish talk points built from superseded facts.
- Publish only accepted sections. A report with missing requested audiences is `partial`, not
  `completed`. If no useful, source-backed report can be produced, mark the job `failed`.

Astra's review uses a fresh context and an auditor prompt, but it is not independent proof of
correctness. Deterministic checks and human-reviewed evaluation examples remain necessary.

## 5. Recommended model assignment

OpenAI describes Luna as the fastest and most affordable GPT-5.6 tier, Terra as balanced, and Sol
as its GPT-5.6 flagship [1]. Astra is documented for the hardest reasoning, research, and document
creation tasks, with structured outputs and configurable reasoning effort [2][3].

These are the recommended starting assignments based on those roles. Application-specific quality
and latency still need measurement on the actual vendor deployments; a model name alone does not
establish the best cost per accepted report.

| Task                                                 | Model           | Initial reasoning effort | Why this allocation                                                                                         |
|------------------------------------------------------|-----------------|--------------------------|-------------------------------------------------------------------------------------------------------------|
| Evidence extraction                                  | `gpt-5.6-luna`  | `low`                    | Repetitive, bounded extraction with direct quotations and deterministic validation                          |
| Evidence consolidation                               | `gpt-5.6-terra` | `medium`                 | Reconcile entities, dates, duplicates, and conflicting statements                                           |
| Company strategy and AI opportunities                | `gpt-6-astra`   | `high`                   | The most consequential synthesis: connect evidence, business priorities, and feasible AI change             |
| Audience-specific talk points                        | `gpt-5.6-sol`   | `medium`                 | Strong professional writing and reasoning over an already established strategy                              |
| Final factual and strategic review                   | `gpt-6-astra`   | `high`                   | Challenge unsupported claims, generic recommendations, missing controls, and cross-audience inconsistencies |
| Fetching, arithmetic, citation resolution, rendering | No AI           | Not applicable           | Deterministic work should remain deterministic                                                              |

Do not enable `max`, multi-agent modes, or provider-hosted browsing by default. Escalate effort or
promote a task to a stronger model only when evaluation shows a meaningful benefit.
If Luna loses important facts, promote extraction to Terra rather than accepting poor evidence.
If Sol's role-specific output trails Astra materially, use Astra for that task despite the extra
per-token cost.

Astra's higher token rates do not necessarily mean a higher cost per completed task [2][3].
Compare the accepted outcome, total attempts, reasoning tokens, latency, and billable usage.
Published prices can change, including temporary promotions; do not treat launch-post prices
as the deployment's rate card.

The backend must have its own authorized API access. A Copilot or chat subscription does not by
itself establish that the FastAPI service can call these models. Confirm vendor endpoints, API model
or deployment IDs, quotas, data handling, and supported parameters before implementation.

## 6. Configuration contract

All variables and files in this section are proposed for implementation, not currently supported
by the application. Supply documented `.example` files when implementing the loaders.

### File responsibilities and loading

| File                    | Responsibility                                                                            | Repository policy                                        |
|-------------------------|-------------------------------------------------------------------------------------------|----------------------------------------------------------|
| `.env`                  | Existing application/auth settings; AI feature flag and optional configuration-file paths | Local/deployment secrets only                            |
| `ai_vendor.env`         | Vendor adapters, endpoints, credentials, transport and concurrency settings               | Ignore; never commit populated credentials               |
| `ai_tasks.env`          | Task-to-vendor/model routing, generation settings, workflow limits                        | Ignore local deployment values; no credentials permitted |
| `ai_vendor.env.example` | Documented vendor configuration with placeholders                                         | Commit when implementing                                 |
| `ai_tasks.env.example`  | Documented routing and workflow defaults                                                  | Commit when implementing                                 |

Add explicit ignore rules for `ai_vendor.env` and `ai_tasks.env` before creating live files.
The current `.gitignore` rule for `.env` does not cover these filenames.
Production should provide `ai_vendor.env` through an access-restricted secret mount or deployment
secret delivery mechanism, not through a tracked file or image layer.

The application-level settings will be:

| Variable                | Type and default      | Meaning                                                                                    |
|-------------------------|-----------------------|--------------------------------------------------------------------------------------------|
| `AI_ENABLED`            | Boolean, `false`      | Enable the feature explicitly; disabled installations retain existing application behavior |
| `AI_VENDOR_CONFIG_FILE` | Path, `ai_vendor.env` | Vendor file; relative paths resolve from the configured application root                   |
| `AI_TASK_CONFIG_FILE`   | Path, `ai_tasks.env`  | Task file; same resolution rule for web and worker processes                               |

Load each file separately using standard UTF-8 dotenv syntax and validate into typed configuration
objects. Do not evaluate shell commands or mix vendor secrets into task prompts.
For recognized fields, precedence is process environment, then the designated file, then a
documented default. Do not require the current working directory to happen to be the repository root.

When AI is enabled, missing files, missing required fields, duplicate IDs, unknown configuration
keys, unresolved vendor references, unsupported parameters, and invalid bounds fail configuration
validation. Report field names without secret values. Do not silently fall back to an arbitrary model.
When disabled, show that insights generation is unavailable rather than returning a simulated report.

Load a configuration version at process startup. Freeze the non-secret task configuration and its
fingerprint for each job so a deployment change does not silently alter an in-flight workflow.
Keep credentials outside those snapshots; rotate them independently.

### `ai_vendor.env`

Use a registry of named vendors. A task refers to a vendor ID instead of carrying an endpoint or
API key. The example below uses direct OpenAI access; replace it with the actual approved endpoint
and adapter for the deployment.

```dotenv
AI_VENDORS=primary

AI_VENDOR_PRIMARY_ADAPTER=openai_responses
AI_VENDOR_PRIMARY_BASE_URL=https://api.openai.com/v1
AI_VENDOR_PRIMARY_API_KEY=REPLACE_WITH_RUNTIME_SECRET
AI_VENDOR_PRIMARY_CONNECT_TIMEOUT_SECONDS=10
AI_VENDOR_PRIMARY_REQUEST_TIMEOUT_SECONDS=120
AI_VENDOR_PRIMARY_MAX_RETRIES=2
AI_VENDOR_PRIMARY_MAX_CONCURRENCY=6
```

`AI_VENDORS` is a required comma-separated list of unique lowercase vendor IDs. Restrict IDs to
letters, digits, and underscores, starting with a letter. Convert an ID to uppercase for its
environment-variable prefix: `primary` becomes `AI_VENDOR_PRIMARY_`.

| Vendor suffix             | Type / required or default                      | Meaning                                                                          |
|---------------------------|-------------------------------------------------|----------------------------------------------------------------------------------|
| `ADAPTER`                 | String, required                                | Implemented protocol adapter; initial recommendation is `openai_responses`       |
| `BASE_URL`                | HTTPS URL, required                             | API base URL, not a company source URL; configured only by an operator           |
| `API_KEY`                 | Secret string, required for the initial adapter | Backend credential; reject empty and example placeholder values when enabled     |
| `CONNECT_TIMEOUT_SECONDS` | Positive number, default `10`                   | Connection-establishment timeout                                                 |
| `REQUEST_TIMEOUT_SECONDS` | Positive number, default `120`                  | Timeout per request attempt, further bounded by the remaining task deadline      |
| `MAX_RETRIES`             | Integer, default `2`, range `0..3`              | Additional transient-failure attempts, not additional repair rounds              |
| `MAX_CONCURRENCY`         | Positive integer, default `6`                   | Aggregate in-flight calls to this vendor in the initial single-worker deployment |
| `ORGANIZATION`            | Optional string, omitted by default             | OpenAI organization identifier, only when applicable                             |
| `PROJECT`                 | Optional string, omitted by default             | OpenAI project identifier, only when applicable                                  |

Start with one verified adapter, not a speculative universal vendor framework. If access is through
Azure, Bedrock, or a gateway with different authentication, deployment naming, or API versions, add
and document the matching adapter first. Do not assume an "OpenAI-compatible" endpoint implements
all Responses API features or use OpenAI's wire format against a different protocol.

Require TLS certificate verification. Treat credentials as `SecretStr` values and redact them from
errors, logs, database records, job snapshots, reports, cookies, and frontend responses.
Do not expose provider tools to report-generation tasks. Disable provider-side response storage
where supported; this alone does not guarantee zero data retention under the vendor's policy.

### `ai_tasks.env`

Task IDs are a fixed, versioned application registry. The configured model value is the exact API
model/deployment ID understood by the referenced vendor. Switching a vendor or model should not
require changing business logic.

```dotenv
AI_TASKS=extract_evidence,consolidate_evidence,synthesize_strategy,generate_talk_points,review_report

AI_TASK_EXTRACT_EVIDENCE_VENDOR=primary
AI_TASK_EXTRACT_EVIDENCE_MODEL=gpt-5.6-luna
AI_TASK_EXTRACT_EVIDENCE_PROMPT_VERSION=v1
AI_TASK_EXTRACT_EVIDENCE_REASONING_EFFORT=low
AI_TASK_EXTRACT_EVIDENCE_MAX_INPUT_TOKENS=12000
AI_TASK_EXTRACT_EVIDENCE_MAX_OUTPUT_TOKENS=4000
AI_TASK_EXTRACT_EVIDENCE_TIMEOUT_SECONDS=90

AI_TASK_CONSOLIDATE_EVIDENCE_VENDOR=primary
AI_TASK_CONSOLIDATE_EVIDENCE_MODEL=gpt-5.6-terra
AI_TASK_CONSOLIDATE_EVIDENCE_PROMPT_VERSION=v1
AI_TASK_CONSOLIDATE_EVIDENCE_REASONING_EFFORT=medium
AI_TASK_CONSOLIDATE_EVIDENCE_MAX_INPUT_TOKENS=30000
AI_TASK_CONSOLIDATE_EVIDENCE_MAX_OUTPUT_TOKENS=6000
AI_TASK_CONSOLIDATE_EVIDENCE_TIMEOUT_SECONDS=150

AI_TASK_SYNTHESIZE_STRATEGY_VENDOR=primary
AI_TASK_SYNTHESIZE_STRATEGY_MODEL=gpt-6-astra
AI_TASK_SYNTHESIZE_STRATEGY_PROMPT_VERSION=v1
AI_TASK_SYNTHESIZE_STRATEGY_REASONING_EFFORT=high
AI_TASK_SYNTHESIZE_STRATEGY_MAX_INPUT_TOKENS=24000
AI_TASK_SYNTHESIZE_STRATEGY_MAX_OUTPUT_TOKENS=8000
AI_TASK_SYNTHESIZE_STRATEGY_TIMEOUT_SECONDS=240

AI_TASK_GENERATE_TALK_POINTS_VENDOR=primary
AI_TASK_GENERATE_TALK_POINTS_MODEL=gpt-5.6-sol
AI_TASK_GENERATE_TALK_POINTS_PROMPT_VERSION=v1
AI_TASK_GENERATE_TALK_POINTS_REASONING_EFFORT=medium
AI_TASK_GENERATE_TALK_POINTS_MAX_INPUT_TOKENS=14000
AI_TASK_GENERATE_TALK_POINTS_MAX_OUTPUT_TOKENS=4500
AI_TASK_GENERATE_TALK_POINTS_TIMEOUT_SECONDS=150

AI_TASK_REVIEW_REPORT_VENDOR=primary
AI_TASK_REVIEW_REPORT_MODEL=gpt-6-astra
AI_TASK_REVIEW_REPORT_PROMPT_VERSION=v1
AI_TASK_REVIEW_REPORT_REASONING_EFFORT=high
AI_TASK_REVIEW_REPORT_MAX_INPUT_TOKENS=32000
AI_TASK_REVIEW_REPORT_MAX_OUTPUT_TOKENS=6000
AI_TASK_REVIEW_REPORT_TIMEOUT_SECONDS=240

AI_PIPELINE_DEFAULT_AUDIENCES=ceo,cto,cio,cfo,ciso
AI_PIPELINE_TALK_POINTS_PER_AUDIENCE=3
AI_PIPELINE_MAX_SEED_URLS=6
AI_PIPELINE_MAX_FETCHED_PAGES=12
AI_PIPELINE_JOB_TIMEOUT_SECONDS=900
```

`AI_TASKS` is required and must contain each required task exactly once. A task ID becomes an
uppercase prefix, for example `review_report` becomes `AI_TASK_REVIEW_REPORT_`.

| Task suffix | Type / requirement | Meaning |
|---|---|---|
| `VENDOR` | Required vendor ID | Must resolve to `AI_VENDORS` |
| `MODEL` | Required string | Exact vendor model or deployment ID; no default substitution |
| `PROMPT_VERSION` | Required registered version | Versioned application prompt and compatible output schema, not an arbitrary file or URL |
| `REASONING_EFFORT` | Optional vendor-supported value | Example values are initial recommendations; omit when unsupported, never silently translate |
| `MAX_INPUT_TOKENS` | Required positive integer | Budget includes instructions, evidence, and schema; reject or reduce input before the call |
| `MAX_OUTPUT_TOKENS` | Required positive integer | Provider output budget, including reasoning tokens where the provider counts them |
| `TIMEOUT_SECONDS` | Required positive number | Whole-task deadline across attempts, bounded by the job deadline |
| `TEMPERATURE` | Optional number, omitted by default | Send only when supported by this model and adapter; do not force `0` onto reasoning models |

Validate parameters and input-plus-output limits against the actual deployment's capabilities.
Map settings to the adapter's real request fields; Responses API and Chat Completions formats are
not interchangeable. Use schema-constrained output and validate it again with Pydantic.
Pass the audience as validated task input to `generate_talk_points`; do not duplicate the entire
pipeline configuration for each role.

All `AI_PIPELINE_` variables below belong in `ai_tasks.env`. Defaults are initial operational
proposals, not measured latency guarantees.

| Pipeline suffix | Default | Purpose |
|---|---|---|
| `DEFAULT_AUDIENCES` | `ceo,cto,cio,cfo,ciso` | Nonempty list of supported, unique audience IDs |
| `TALK_POINTS_PER_AUDIENCE` | `3` | Integer `1..5`; require this count for each requested audience |
| `MAX_SEED_URLS` | `6` | Maximum distinct submitted URLs |
| `MAX_FETCHED_PAGES` | `12` | Maximum fetched documents, including seeds and discovered pages |
| `FETCH_CONCURRENCY` | `4` | Aggregate source-fetch concurrency; also enforce one request at a time per host |
| `EXTRACTION_CONCURRENCY` | `4` | Concurrent extraction tasks, additionally limited by vendor capacity |
| `AUDIENCE_CONCURRENCY` | `3` | Parallel audience generation, additionally limited by vendor capacity |
| `FETCH_TIMEOUT_SECONDS` | `20` | Per-source request deadline |
| `MAX_REDIRECTS` | `3` | Each hop requires a fresh authorization and address check |
| `MAX_HTML_BYTES` | `2097152` | 2 MiB decompressed HTML ceiling |
| `MAX_PDF_BYTES` | `15728640` | 15 MiB PDF download ceiling |
| `MAX_PDF_PAGES` | `200` | PDF parsing ceiling; oversized/scanned documents get an explicit source outcome |
| `MAX_ACTIVE_JOBS_PER_USER` | `1` | Bound user-initiated workload before enqueueing |
| `MAX_MODEL_CALLS` | `40` | Counts all attempts, reviews, and repairs; never an agent recursion allowance |
| `MAX_TOTAL_INPUT_TOKENS` | `300000` | Aggregate job input budget, including repeated context |
| `MAX_TOTAL_OUTPUT_TOKENS` | `120000` | Aggregate output budget, including billed reasoning where applicable |
| `MAX_REPAIR_ROUNDS` | `1` | Integer `0..1`; one repair round followed by review at most |
| `JOB_TIMEOUT_SECONDS` | `900` | Hard end-to-end processing deadline after a worker claims the job |

Numeric limits must be positive except explicitly permitted zero values. Require fetched-page
capacity to cover the seed cap. Enforce safe implementation ceilings, not merely administrator
preferences, on download sizes, concurrency, and parsing resources.
Count retries and repairs against all applicable budgets. Before a call, reserve its allowed
budget; use provider-reported usage afterwards. Treat uncertain usage after a timeout
conservatively. Use a compatible tokenizer or documented conservative estimator, not an assumed
universal characters-per-token conversion.

## 7. Application architecture and persistence

The current application already supplies FastAPI, Jinja, `httpx`, Pydantic settings, Microsoft login,
private route groups, and session-bound CSRF tokens. It does not yet contain an insights pipeline,
AI provider configuration, a database layer, or durable job execution.

Preserve the existing server-rendered architecture. Keep new authenticated routes in
`src\cxplorer\routers\private.py`; keep service logic out of route functions. Use `require_user`
for private APIs and authenticated mutations, and redirect unauthenticated private pages to
`/login`. Reuse or extract the existing logout CSRF validation for every state-changing request.
Validate redirects with `safe_local_path`.

### Proposed routes and experience

| Method and URL | Purpose |
|---|---|
| `GET /insights/new` | URL entry, optional seller context, audience selection |
| `POST /insights` | CSRF-protected validation and enqueue; redirect to the progress page |
| `GET /insights/{report_id}` | Owner-authorized progress, source outcomes, and completed/partial report |
| `GET /api/private/insights/{report_id}/status` | Owner-authorized structured status for progressive enhancement |
| `POST /insights/{report_id}/retry` | CSRF-protected, quota-controlled retry of eligible failed work |
| `POST /insights/{report_id}/cancel` | CSRF-protected cancellation with visible state |
| `POST /insights/{report_id}/delete` | CSRF-protected deletion of the owner's report and associated retained artifacts |

The form and report work without JavaScript. Optional dependency-free JavaScript can poll status
and announce progress accessibly, while rendering and authorization remain on the server.
Use semantic sections for each audience, a source drawer/list, clear evidence-gap notices, and
print-friendly styles in the shared stylesheet. Do not render model-produced HTML or expose
unreviewed token streams to the browser.

### Durable jobs

Use a separate Python worker and a durable relational job store. Do not rely on FastAPI
`BackgroundTasks` or a request-local coroutine for work that must survive reloads and restarts.

The proposed persistence choice is SQLAlchemy with migrations, SQLite for local development, and
PostgreSQL for production. This is a new dependency/deployment decision to confirm before coding.
For the MVP, use one worker with bounded asynchronous I/O and database-backed job claims; a Redis
broker, Celery, vector database, and autonomous-agent framework are not required.

Claim jobs atomically with a lease, heartbeat, and bounded recovery after worker loss. Persist
stage results idempotently and perform network/model calls outside database transactions.
Use provider idempotency support where available, but do not promise exactly-once external
execution. Scaling to multiple workers requires shared vendor rate limiting and production-safe
claiming; a process-local semaphore alone would not enforce global limits.

Track `queued`, `running` plus the current stage, `completed`, `partial`, `failed`, and `cancelled`.
Keep cancellation separate from provider failure; an already submitted model request may still
finish or incur cost. A duplicate browser submission must not create duplicate jobs or charges.

### Persistent data contracts

| Record          | Essential contents                                                                                                        |
|-----------------|---------------------------------------------------------------------------------------------------------------------------|
| Request/job     | Owner ID, approved seeds/hosts, audiences, seller context, state, lease, deadlines, redacted configuration version        |
| Source document | Source ID, original/final URL, title, media type, retrieval/publication dates, hash, parse coverage and outcome           |
| Evidence fact   | Fact ID, source references, short exact excerpts with page/section locations, company-reported/inferred status, conflicts |
| Company dossier | Structured company fields, fact references, reporting periods, explicit unknowns                                          |
| Strategy brief  | Prioritized hypotheses, business outcomes, prerequisites, controls, evidence references                                   |
| Audience set    | Audience ID, opening, requested talk-point count, questions, objections, next-step ask                                    |
| Quality review  | Decision, affected section IDs, issue codes, evidence references, repair outcome                                          |
| Task run        | Task/prompt/schema version, vendor/model, attempts, provider request ID, timing, usage, sanitized error                   |
| Report          | Accepted structured sections, audience coverage, sources, as-of date, owner, schema version                               |

Derive ownership from validated identity, never a submitted owner ID, email address, or display
name. Before introducing durable data, define the identity mapping and issuer/subject namespace.
The current session exposes `provider` and `subject`, but not issuer; retain any additional
validated identity claim needed for safe multi-tenant ownership without persisting OAuth tokens.
Every read, retry, cancellation, deletion, and eventual export must enforce report ownership.

Propose short-lived normalized source caching, up to 24 hours, with no cache sharing of seller
context or generated reports across users. Retain only necessary supporting excerpts with a report,
not indefinite copies of entire websites. A proposed 90-day report retention and deletion policy
requires product/hosting approval before release. Store review findings, not hidden chain-of-thought.

### Suggested implementation locations

```text
src\cxplorer\ai\config.py          Typed vendor/task registries and validation
src\cxplorer\ai\providers.py       Explicit provider adapters and usage normalization
src\cxplorer\ai\tasks.py           Typed task execution, deadlines, schema handling
src\cxplorer\ai\prompts\           Versioned task and audience instructions
src\cxplorer\insights\schemas.py   Request, evidence, strategy, audience, report contracts
src\cxplorer\insights\sources.py   URL policy, fetching, extraction, provenance
src\cxplorer\insights\pipeline.py  Bounded workflow and repair rules
src\cxplorer\insights\repository.py Job/report persistence and ownership queries
src\cxplorer\worker.py             Durable worker entry point
src\cxplorer\routers\private.py    Authenticated pages and APIs
src\cxplorer\templates\           Server-rendered forms, progress, reports
src\cxplorer\static\css\app.css    Shared responsive and print styles
src\cxplorer\static\js\           Optional dependency-free progress enhancement
```

These are proposed locations, not existing modules. Continue starting the web app with
`python server.py`; a separate worker entry point will be added when implementing job execution.

## 8. Safety, provenance, and operational boundaries

### URL and document ingestion

Apply SSRF defenses at both the application and egress-network layers [4]. Restrict schemes and
ports; reject URL credentials, private/local/reserved addresses, cloud metadata destinations,
and unsupported encodings. Validate all resolved IPv4/IPv6 addresses and every redirect hop.
Disable automatic redirects and authorize each hop explicitly.

Protect against DNS rebinding by using the validated resolution for the connection, retaining
correct TLS hostname verification, and enforcing an egress policy. A one-time DNS check followed
by an unrestricted second resolution is insufficient.

Bound streaming/decompressed bytes, document counts, parsing time, memory, and PDF pages. Match
declared content types with actual supported formats. Parse documents without executing scripts,
embedded actions, external entities, or document-supplied network requests. Do not expose stored
source documents as active same-origin HTML.

### Prompt injection and output handling

All fetched text and seller-supplied context are untrusted data [5]. Keep them separate from
application instructions. Never obey embedded requests to reveal secrets, fetch additional URLs,
change models, or alter the report-generation rules. Delimiters and filtering alone are not a
security boundary: models receive no credentials or unrestricted tools in the first place.

Resolve citations against server-owned source/fact IDs, not arbitrary links invented by a model.
Verify quotations against normalized source text and check numerical units and dates. Distinguish
direct evidence, derived calculations, inference, and unknowns. A model-generated confidence
percentage is not a substitute for evidence.

Escape all content through Jinja, validate external link schemes, and preserve the strict CSP.
Do not use model output as SQL, a template, shell code, a filesystem path, or an authorization rule.

### Privacy and operating controls

Send providers only the public company evidence and explicitly supplied seller context needed for
the task. Explain that transfer to the user. Do not send authentication/session data or collect
personal contact details merely because they occur on a page.

Confirm vendor retention, region, contractual data use, and logging policy before production.
Enforce per-user quotas and job budgets before work starts. Log identifiers, status, timing, and
usage rather than full prompts or secret-bearing provider errors. Make source gaps, model failures,
quota limits, and disabled configuration visible without leaking internal credentials or topology.

## 9. Efficiency and quality acceptance

Cache identical normalized source content by hash within the allowed scope and freshness policy.
Reuse a shared dossier and strategy for every audience. Parallelize independent extraction and
audience tasks within vendor limits, and use supported prompt caching when permitted by the
deployment's data policy. No embeddings or vector store are needed for the initial bounded corpus.

Measure provider-reported input, output, reasoning, cached-read/write usage where available, retry
counts, stage latency, queue time, and total time to an accepted report. Normalize overlapping token
categories so reasoning or cached tokens are not double-counted. Dollar estimates require an
operator-verified, dated vendor rate card including cache and long-context rules; otherwise show
usage without inventing a price.

An initial performance target is a useful complete report within five minutes at the 95th
percentile for eight ordinary HTML sources and five audiences, excluding queue wait. This is a
target to measure, not a promised capability. The 15-minute processing deadline is a separate
failure bound. Prefer a faster accepted report over merely a faster first model response.

### Acceptance criteria

- Every company-specific assertion has resolvable evidence references or is explicitly marked
  as a hypothesis/unknown. Direct quotations match retained source excerpts.
- No invented financial values, technology deployments, security controls, incidents, customer
  relationships, product capabilities, or ROI percentages appear as facts.
- Every requested audience has its own complete talk-point set with the configured point count,
  relevant business language, discovery questions, and a feasible next step.
- The CEO, CTO, CIO, CFO, and CISO narratives are meaningfully distinct but share consistent facts.
- Each recommended AI opportunity identifies prerequisites, ownership, measurable value, and
  relevant controls; "use AI" by itself does not pass.
- Source failures, insufficient evidence, invalid model output, budget exhaustion, and incomplete
  audience coverage produce explicit states rather than fabricated completion.
- Ownership and CSRF protections cover all new routes; user URLs cannot access internal services.
- A worker restart can recover eligible work without losing accepted artifacts or silently
  restarting the entire billable workflow.

Use a small evaluation corpus spanning public/private companies, several industries, sparse sites,
financial PDFs, contradictory dated claims, and hostile document instructions. Compare candidate
models on the same task inputs, prompts, schemas, and human-reviewed expected evidence.
Require no material factual errors on the release evaluation set and a target human rating of at
least 4/5 for audience relevance, business clarity, and actionability. These are release gates,
not a guarantee of perfect results on unseen websites.

Use existing pytest and Ruff tooling during implementation. Cover config parsing, missing secrets,
unknown task/vendor IDs, unsupported parameters, URL/redirect/DNS policy, extraction, source IDs,
cross-user access, CSRF, retries, budgets, cancellation, restart recovery, and role coverage.
Keep normal tests offline with fixtures; live model evaluation is explicit and separately budgeted.

## 10. Delivery sequence and decisions before implementation

| Phase                              | Deliverable                                                                                               | Exit condition                                                                                            |
|------------------------------------|-----------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| 0. Confirm deployment and evaluate | Actual vendor/API mappings, capability checks, small extraction/strategy/writing comparison               | Working authorized model access, documented quotas/data policy, evidence that routing meets quality needs |
| 1. Foundation                      | Typed config loaders and examples, secret ignore rules, output schemas, persistence and durable jobs      | Configuration errors are explicit and authenticated jobs survive restarts                                 |
| 2. Thin end-to-end slice           | Safe source collection through a sourced company profile, one audience, review, and SSR report            | A real request completes without manual handoffs or fabricated fallback output                            |
| 3. Full report                     | Five separate audience sets, shared strategy, contradiction handling, bounded repair                      | Every selected audience passes the evidence and usefulness gates                                          |
| 4. Release readiness               | Ownership, quotas, deletion, operational visibility, failure recovery, performance and quality evaluation | The acceptance criteria and approved hosting/data policies are met                                        |

Before implementation, confirm the actual vendor/API access and model deployment names, hosting
and database choice, source-fetch egress controls, report retention, and whether seller-specific
offerings are needed for the first release. The current proposed defaults are five audiences,
English output, optional seller context, and public company sources only.

Defer unrestricted web search, scanned-PDF OCR, browser automation for JavaScript-only sites,
CRM enrichment, personal executive profiling, email generation/sending, and slide-deck exports.
Add them only as explicit later scope, with their own access, quality, and operating requirements.

## 11. Reference basis

The model references below were consulted on 2026-09-05. Recheck deployment-specific capabilities
and commercial terms before implementing; task routing remains configuration-driven.

1. [OpenAI: GPT-5.6 family positioning](https://openai.com/index/gpt-5-6/)
2. [OpenAI: GPT-6 Astra model details, supported features, and pricing](https://developers.openai.com/api/docs/models/gpt-6-astra)
3. [OpenAI: GPT-6 Astra model guidance](https://developers.openai.com/api/docs/guides/latest-model)
4. [OWASP: SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
5. [OWASP: LLM prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)

Repository baseline: `README.md`, `pyproject.toml`, `.github\copilot-instructions.md`,
`src\cxplorer\config.py`, `src\cxplorer\routers\private.py`,
`src\cxplorer\auth\models.py`, and `src\cxplorer\auth\dependencies.py`.
