# Company insights and executive talk points

Date: 2026-09-05

Status: Implementation approved. This document records the research and report contracts;
README.md and the inline-documented configuration files describe runtime setup and limitations.

Confirmed product decisions: reject login without a usable provider-supplied email; keep the
server free of persistent job/report storage; cache input URLs and generated insights per user in
browser localStorage; retain that cache across sign-out with a seven-day expiry. Cache namespaces
use only SHA-256 of the user's email, with no version in storage keys. Unreadable, malformed, or
undecompressible cache entries are invalid. AI stages exchange structured JSON, not free-form
documents. Official company announcements from the last 90 days are always researched, without
an opt-in/out or third-party coverage fallback. Both vendors use the OpenAI Python SDK; default
routing is AzureOpenAI with the server's Entra ID authentication.

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
- Prefer a relation quotation that names the homepage company. If the retained page title or other
  retained text identifies the company, allow an exact relevant quotation that omits the name and
  publish a server-generated source-identity warning. A hostname alone remains insufficient, and
  sources with no retained company-name evidence remain rejected.
- Show rejected, blocked, stale, unreadable, and omitted sources with reasons. If a site requires
  JavaScript or a PDF is scanned, request an alternative public source rather than guessing.
- Respect site access policies and rate limits. Do not bypass authentication, paywalls, CAPTCHAs,
  robots restrictions, or rate limits. Browser-compatible transport and a bounded supported
  challenge exchange may be used for otherwise-public documents.

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

Accepted shared opportunities normally provide enough grounding for discovery-oriented talk
points even when public sources do not describe a particular executive's priorities. In that case,
use `analysis` or `hypothesis` backed by the opportunity's motivating fact IDs rather than returning
no content or asserting an undisclosed company plan. Attempt one audience-local coverage correction
before retaining limited coverage. Different audiences may use the same opportunity only through
meaningfully distinct executive lenses. Do not introduce external peer-company or industry stories.

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
External-provider sign-in
  -> validate identity and required email, or fail login with an actionable error
  -> establish the session and derive SHA-256 of the user's email for the cache namespace
  -> restore the user's URL draft when available
  -> authenticated Generate Insights submission
  -> validate URLs, audiences, CSRF, ownership, and quotas
  -> create a bounded in-memory job and return its progress page
  -> fetch approved sources and extract clean text
  -> verify that supplied sources concern the official homepage's company
  -> search approved official hosts for dated company/executive announcements
  -> safely fetch candidates and verify company, publication date, and attribution
  -> extract evidence from bounded source chunks in parallel
  -> reconcile the evidence into one company dossier
  -> synthesize company strategy and AI opportunities
  -> generate a separate talk-point set for each audience in parallel
  -> validate citations, schema, coverage, and report quality
  -> repair affected sections once if necessary, then review again
  -> retain the accepted report temporarily in memory and render it with Jinja
  -> issue a signed, compressed report envelope for the user's browser cache
```

| Stage                  | Execution                       | Output and control                                                    |
|------------------------|---------------------------------|-----------------------------------------------------------------------|
| Submission             | Python, no AI                   | Validated request, owner reference, requested audiences, job ID       |
| Collection             | Bounded HTTPX with browser fallback, no AI | Source metadata, content hashes, dates, fetch outcomes                |
| Text preparation       | HTML/PDF parsing, no AI         | Clean sections and chunks with source/page/section references         |
| `extract_evidence`     | Luna, per selected chunk        | Structured candidate facts with exact supporting excerpts             |
| `consolidate_evidence` | Terra                           | Deduplicated company dossier, contradictions, evidence gaps           |
| `synthesize_strategy`  | Astra                           | Shared strategic brief, prioritized AI hypotheses, prerequisites      |
| `generate_talk_points` | Sol, one call per audience      | Role-specific talk points grounded in the shared dossier and strategy |
| `review_report`        | Astra, fresh review context     | Structured pass/revise decision with section IDs and evidence issues  |
| Publication            | Python and Jinja, no AI         | Authorized HTML and a signed, compressed browser-cache envelope       |

The evidence ledger retains source-backed facts and quotations, not just lossy page summaries.
Downstream tasks receive relevant evidence and the shared strategy, avoiding repeated full crawls
and separate, contradictory company research for each audience.

Each model returns one registered JSON artifact, not a Markdown report to scrape or a conversation
to interpret. Unknowns and discovery questions are structured report data, not an interactive model
conversation that pauses the job waiting for an answer.

Chunk and rank long documents before model calls. Reserve input/output budget for synthesis,
all requested audiences, and review before scheduling extraction. Record excluded sections and
sources; never silently truncate evidence and claim full coverage.

### Structured-output boundary

Use five explicit output contracts: `EvidenceBatch`, `CompanyDossier`, `StrategyBrief`,
`AudienceTalkPoints`, and `QualityReview`. Python composes the final `AcceptedReport`; there is no
additional model call to rewrite the whole report after review.

Define the contracts with Pydantic as the source of truth. Generate the provider-compatible JSON
Schema from those types rather than maintaining an unrelated handwritten schema. Use native
schema-constrained output, not only a prompt saying "return JSON" or JSON mode that guarantees
syntax without the required structure. Both vendor clients use the Responses API's `text.format`
with `type: "json_schema"`, a registered schema name, `strict: true`, and the schema [4].
Do not confuse this with Chat Completions' `response_format` request field.

- Every output has a fixed object at its root, closed nested objects, and
  `additionalProperties: false`. All declared fields are required; unknown optional values use
  explicit `null`, and empty collections use `[]`.
- Use enums for audiences, evidence basis, source categories, issue codes, and review decisions.
  Apply bounded strings, lists, nesting, and total payload sizes. Where a vendor does not support
  a schema constraint, enforce it explicitly in Python; do not pretend the provider enforced it.
- A model/adapter without the required structured-output support is not a valid task route.
  Do not silently fall back to prose, weaker JSON mode, or arbitrary function/tool execution.
- Parse exactly one complete response artifact and validate it in Python before downstream use.
  Do not strip Markdown fences, recover a JSON-looking substring, coerce wrong types, invent
  missing fields, or turn an invalid response into an empty successful result.
- If a completed response fails JSON shape or Python-only contract validation, permit one
  task-local re-generation with safe contract-stage, schema-path, and validation-type feedback.
  Use the same vendor, model, typed input, and schema; never repair or coerce the rejected value.
- Separate provider completion/refusal/error metadata from the model's artifact. Python owns
  task state, IDs, timestamps, token usage, ownership, cache keys, and publication decisions.
  Those values are not supplied or overridden by model output.
- Natural-language content belongs in bounded, named JSON fields such as `seller_line` or
  `discovery_question`. Do not request hidden chain-of-thought, HTML, or a second prose answer.

Every contract has a valid way to express insufficient evidence through explicit unknowns,
empty collections, and evidence gaps. In particular, an audience may return a limited set or no
usable points rather than inventing content to fill the requested count. Schema validity is not
the same as a complete report; Python determines coverage from the actual accepted artifacts.

Every call has a server-owned instruction layer, a stage-specific task definition, and a JSON
input data block. The data block contains only the allowed source material and preceding typed
artifacts. Fetched text and seller context remain untrusted data even inside JSON. Models have no
network tools, credentials, authentication data, or authority to schedule additional work.

### Shared evidence and claim types

Use a shared `GroundedText` type for company assertions, analysis, and hypotheses. Its structural
schema is illustrated below; additional length and cross-field rules are enforced in Python.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["text", "basis", "fact_ids"],
  "properties": {
    "text": {"type": ["string", "null"]},
    "basis": {
      "type": "string",
      "enum": ["source_backed", "analysis", "hypothesis", "unknown"]
    },
    "fact_ids": {"type": "array", "items": {"type": "string"}}
  }
}
```

`source_backed` and company-specific `analysis` require nonempty references to accepted facts.
Company-specific hypotheses also reference their motivating facts, but those references do not
prove the hypothesis. `unknown` requires `text: null` and an empty reference list; it must not
become a guessed value, zero, or an empty string disguised as a fact. Source attribution remains
on each fact: a company-reported claim is not independent proof that the claim is true.

Python creates all source, chunk, span, fact, opportunity, point, and report-section IDs. Models
may reference IDs supplied in their input, but do not mint persistent identifiers or citation URLs.
References are resolved only within the current job's artifact set. Facts retain exact supporting
quotes, source/page/section locations, dates when known, and entity attribution.

The verified company name is server-owned. If the dossier selects a valid fact reference whose
quotation does not identify that name, Python rebinds only the company-name reference to a stable
accepted fact from an included source whose quotation does. Do not change the verified name, use
an excluded source, or continue when no accepted identifying quotation exists.

When a model-provided quotation differs only by high-confidence textual drift, Python may match it
against one unambiguous retained source substring, replace the model text with that exact substring,
and add a visible `Quotation matching warnings` evidence gap. This recovery is limited to minor
formatting or spelling variation. Changed numbers, negation or other meaning-sensitive terms,
ambiguous matches, wrong source/span references, false attribution, and entity mismatch still fail.

Quantities retain quoted numeric text, unit, currency, period, and actual/forecast/target status
when supported. Missing components remain null. Python performs unambiguous numeric normalization,
unit conversion, and arithmetic; models do not manufacture calculated financial values or ROI.
An extracted quantity's numeric-text mismatch is non-blocking: retain the value with a
server-generated `quantity_numeric_mismatch` warning on its evidence fact. Show a report notice
and mark the affected quantity as unverified beside its source excerpts. Preserve warnings in
JSON downloads and signed browser restoration, and do not reject a quality-review section solely
for this flagged metadata mismatch. Normalize only unambiguous formatting equivalents such as
`500`/`500.0` and grouped/ungrouped thousands before comparison.

For other extraction-stage numeric, unit, currency, or reporting-period mismatches, use the one
task-local validation correction first. If the corrected extraction still fails, omit only the
affected candidate fact, record a `Numeric and period warnings` evidence gap, and continue when
enough valid facts remain. Never publish the unsupported metadata under a warning. Source/span
references and citations that cannot be safely canonicalized remain blocking, as do unsupported
numbers introduced in retained dossier, strategy, audience, announcement, or review artifacts.

### Source preparation: Python, before any model call

Build a source manifest from the approved fetch results. It contains original/final URLs, content
hashes, titles, media types, retrieval times, publication dates when supported, and source outcomes.
Only this server-owned manifest supplies report links and retrieval timestamps.

Extract clean text into bounded spans with stable IDs and page/section locations. Chunk and rank
those spans using the requested source purpose and relevant headings, preserving the exact text
used for quote matching. Record pages/sections that were not included; a source is not fully
covered merely because one of its chunks was processed. No usable evidence means no AI synthesis.

The examples below are synthetic Contoso data, not fetched company information. For the compact
audience example, the request selects only `ceo` and one talk point, an allowed configuration.
The application defaults remain five audiences and three points per audience.

The example source `src_001` has two spans:

| Span ID | Source text |
|---|---|
| `span_001` | Contoso provides invoice-processing software for distributors. |
| `span_002` | Reviewers approve invoice exceptions before an invoice proceeds. |

### Task 1: `extract_evidence` -> `EvidenceBatch`

Run once per selected chunk, with independent calls bounded by extraction and vendor concurrency.
Input contains the source/chunk metadata, exact spans and their IDs, and the allowed fact categories.
The source-purpose label is a hint, not evidence.

Output contains `facts` and `evidence_gaps`. Each fact candidate has a category, entity, statement,
attribution, supporting span/quote pairs, quantities, and nullable time context. It has no `fact_id`
yet. This stage extracts what a source reports; it does not recommend AI projects or infer a
deployed technology stack from recruitment language.

```json
{
  "facts": [
    {
      "category": "offering",
      "entity": "Contoso",
      "statement": "Contoso provides invoice-processing software for distributors.",
      "attribution": "company_reported",
      "evidence": [
        {
          "span_id": "span_001",
          "quote": "Contoso provides invoice-processing software for distributors."
        }
      ],
      "quantities": [],
      "time_context": null
    },
    {
      "category": "product_workflow",
      "entity": "Contoso",
      "statement": "The described product workflow includes reviewer approval of invoice exceptions.",
      "attribution": "company_reported",
      "evidence": [
        {
          "span_id": "span_002",
          "quote": "Reviewers approve invoice exceptions before an invoice proceeds."
        }
      ],
      "quantities": [],
      "time_context": null
    }
  ],
  "evidence_gaps": [
    {
      "area": "measurement",
      "detail": "No exception-volume or review-time baseline is stated in these spans."
    }
  ]
}
```

Before accepting candidates, Python checks allowed span membership, exact quote matches against
the supplied normalized text, attribution, quantity references/units/periods, and duplicate content.
Quantity numeric-text mismatches become visible, non-blocking warnings rather than extraction
failures; unsupported numbers in fact statements and time context still fail validation.
It attaches authoritative source metadata and assigns `fact_001` and `fact_002` in this example.
A quote match proves traceability, not that a paraphrase is semantically justified; the later
review still compares claims with the original excerpts. Rejected candidates never silently enter
the evidence ledger, and chunk failures/omissions remain visible as coverage gaps.

### Task 2: `consolidate_evidence` -> `CompanyDossier`

Run once over accepted facts, source metadata, known company boundaries, and coverage gaps.
Do not re-send the entire raw corpus or allow this task to create unsupported new facts.

| Output field | Typed contents and responsibility |
|---|---|
| `entity_resolution` | `matched` or `ambiguous`, grounded company name, included source IDs, and explicit exclusions with reasons |
| `company_profile` | Fixed fields for business description, industries, offerings, customer segments, geographies, initiatives, priorities, financials, technology, leadership, and security; use `GroundedText` values/lists |
| `executive_summary` | Bounded list of grounded statements, not a free-form report |
| `fact_groups` | Existing fact IDs grouped as duplicates, supporting evidence, or a time series |
| `contradictions` | Conflicting fact IDs, the conflicting subject, and a concise explanation |
| `evidence_gaps` | Missing topics or coverage with explicit reasons, not assumed negative findings |

All fields exist even when their values are unknown or their lists are empty. Facts from different
entities or periods must not be combined into a single claim. For example, two financial values
from different years are not automatically a contradiction. Do not arbitrarily select a convenient
value when sources genuinely conflict.

Python requires all referenced facts/sources to exist and preserves the original evidence ledger.
Ambiguous company identity blocks company-specific strategy until usable boundaries are available;
unknown finance or technology fields alone do not prevent a clearly labeled limited report.

### Task 3: `synthesize_strategy` -> `StrategyBrief`

Input is the dossier, relevant accepted facts/excerpts, requested audiences, and optional seller
context explicitly labeled as user-supplied. Seller claims must not be promoted into independently
sourced company facts or invented product capabilities.

Output contains `strategic_summary`, a ranked `opportunities` list, and `evidence_gaps`.
Each opportunity specifies its approach, grounded company signal, labeled hypothesis, affected
workflow, proposed business outcome, prerequisites, success criteria, human controls, non-AI
alternative, and feasible next step. Supported approaches are `ai_assisted`, `ai_augmented`,
`bounded_autonomy`, `non_ai`, and `discovery_only`; the schema must not force AI where it is a poor fit.

One complete opportunity candidate, before Python assigns `opp_001`:

```json
{
  "priority": 1,
  "title": "Explore assistance for invoice exception review",
  "approach": "ai_assisted",
  "company_signal": {
    "text": "The described product workflow includes reviewer approval of invoice exceptions.",
    "basis": "source_backed",
    "fact_ids": ["fact_002"]
  },
  "hypothesis": {
    "text": "AI-assisted triage may reduce review effort while preserving human approval.",
    "basis": "hypothesis",
    "fact_ids": ["fact_001", "fact_002"]
  },
  "workflow": "Invoice exception review in the product experience",
  "business_outcome": {
    "text": "Evaluate whether users can resolve exceptions with less effort and acceptable error rates.",
    "basis": "hypothesis",
    "fact_ids": ["fact_002"]
  },
  "prerequisites": {
    "data": ["Approved representative exception examples and outcome labels"],
    "integration": ["Confirm the existing review workflow and available integration points"],
    "business_owner": ["Identify an accountable product owner and participating reviewers"],
    "adoption": ["Agree a reviewer-led pilot and feedback process"],
    "security": ["Define access, privacy, and data-retention controls"],
    "governance": ["Establish evaluation criteria and escalation responsibilities"]
  },
  "success_criteria": [
    {
      "metric": "Review time per exception alongside incorrect-triage rate",
      "baseline_fact_ids": [],
      "target": null,
      "measurement_plan": "Measure the current workflow before agreeing a pilot target."
    }
  ],
  "human_controls": ["Keep final approval with a reviewer and allow overrides"],
  "non_ai_alternative": "Assess whether clearer rules and routing would address the same friction.",
  "next_step": "Confirm existing capabilities and whether exception review is a priority before designing a pilot."
}
```

Prerequisites and proposed controls are recommendations, not claims that the company already has
them. Baseline references must resolve to actual measurements. Numeric savings, ROI, and targets
are not invented to fill required fields; targets stay null unless explicitly supported by the
request or evidence. Absence of public AI information is not proof that the company lacks AI.

Python accepts only grounded, structurally complete opportunities, assigns IDs, and freezes the
shared brief used by every audience. If the brief changes later, its dependent audience artifacts
must be regenerated rather than mixing old and new strategies.

### Task 4: `generate_talk_points` -> `AudienceTalkPoints`

Make a separate call for each requested audience, with the configured audience concurrency.
Each call receives one server-defined audience brief, the shared strategy/dossier, applicable
opportunity IDs, relevant fact excerpts, and the required point count. The audience brief fixes
the lens from section 3; changing only a heading does not produce a distinct executive narrative.

Return `audience`, `coverage` (`complete`, `limited`, or `insufficient_evidence`), a grounded
`opening`, `talk_points`, `discovery_questions`, `objections`, `next_step_ask`, and `evidence_gaps`.
Each point references a shared opportunity and adds a company signal, seller line, role-specific
relevance, discovery question, success measure, and next step.

```json
{
  "audience": "ceo",
  "coverage": "complete",
  "opening": {
    "text": "Contoso provides invoice-processing software for distributors.",
    "basis": "source_backed",
    "fact_ids": ["fact_001"]
  },
  "talk_points": [
    {
      "opportunity_id": "opp_001",
      "company_signal": {
        "text": "The described product workflow includes reviewer approval of invoice exceptions.",
        "basis": "source_backed",
        "fact_ids": ["fact_002"]
      },
      "seller_line": {
        "text": "Explore whether review assistance could create a more valuable product experience.",
        "basis": "hypothesis",
        "fact_ids": ["fact_001", "fact_002"]
      },
      "audience_relevance": {
        "text": "A bounded product experiment could inform differentiation and investment priorities.",
        "basis": "analysis",
        "fact_ids": ["fact_001", "fact_002"]
      },
      "discovery_question": "Where, if anywhere, does exception review create meaningful customer friction?",
      "success_measure": "Establish review-time and error-rate baselines before selecting a target.",
      "next_step": "Identify a product owner and a small reviewer group for a discovery session."
    }
  ],
  "discovery_questions": ["What capabilities are already available in this workflow?"],
  "objections": [
    {
      "objection": "The existing workflow may already meet customer needs.",
      "response": {
        "text": "Confirm a material problem before investing; a non-AI improvement may be sufficient.",
        "basis": "analysis",
        "fact_ids": ["fact_001", "fact_002"]
      }
    }
  ],
  "next_step_ask": "Agree whether a short discovery session is worthwhile.",
  "evidence_gaps": []
}
```

Python requires the requested audience, known opportunity/fact IDs, and bounded fields, then
assigns point and section IDs. `complete` requires exactly the requested point count; `limited`
requires at least one but fewer than requested, and `insufficient_evidence` has none, with explicit
gaps in both cases. Do not pad the list with unsupported or duplicate points. The final renderer
joins each point with the referenced opportunity's workflow, outcome, prerequisites, controls,
and alternative. Do not duplicate those
shared details into every model response or let a role writer silently change them.

Questions and objections must not smuggle in unsupported assumptions about the company. The
reviewer inspects those fields too. All-default audiences receive their own artifacts; one missing
or rejected audience must remain visible rather than being replaced by another audience's text.

### Task 5: `review_report` -> `QualityReview`

Python assembles a candidate report and an immutable section manifest, then supplies a fresh
review context with the candidate, requested audiences/counts, cited facts and original excerpts,
coverage gaps, and deterministic gate results. The reviewer does not receive hidden reasoning
from earlier calls and cannot supply a replacement final report.

Return `decision` (`accept`, `repair`, or `insufficient_evidence`), `section_reviews`, and `issues`.
Every reviewable section has exactly one `accept`, `repair`, or `reject` disposition. Each issue
uses an allowed code, a supplied section ID, relevant fact IDs, a concise description, and a
bounded correction instruction. Review factual support, attribution, dates/units, entity mixing,
cross-audience consistency, persona relevance, actionable prerequisites, and unsubstantiated ROI.
Do not invent numerical confidence scores.

Illustrative repair feedback for a deliberately unsupported assertion in a candidate section:

```json
{
  "decision": "repair",
  "section_reviews": [
    {"section_id": "section_ceo_point_001", "decision": "repair"}
  ],
  "issues": [
    {
      "code": "unsupported_claim",
      "section_id": "section_ceo_point_001",
      "fact_ids": ["fact_001", "fact_002"],
      "description": "The supplied evidence does not establish that AI triage is already deployed.",
      "correction": "Frame triage assistance as a hypothesis and ask about existing capabilities."
    }
  ]
}
```

This example reviews a one-section manifest. A real report must account for every reviewable
section, including other audiences, summaries, and opportunities. Supported issue codes include
`unsupported_claim`, `incorrect_attribution`, `unknown_reference`, `quote_mismatch`,
`entity_mismatch`, `numeric_or_period_mismatch`, `missing_audience`, `wrong_point_count`,
`generic_talk_point`, `missing_prerequisite`, `contradiction_unresolved`, and
`insufficient_evidence`. Map missing-audience issues to a server-created coverage section.

Python rejects unknown/duplicate section references and inconsistent review decisions. A model
`accept` does not override a failed deterministic gate, absent requested audience, or invalid
citation. Every repair/reject disposition needs a matching issue, and `accept` cannot coexist with
an unresolved blocking issue. An unresolved contradiction may be published only as an explicit
conflict/unknown, not as one side's unsupported asserted value.

### Publication: Python composes `AcceptedReport`

Assemble the final JSON from reviewed artifacts rather than asking a model to rewrite them.
Publication and cached restoration both use this same typed contract and Jinja rendering.

| Field | Owner and content |
|---|---|
| `report_id`, `generated_at`, `as_of_date` | Python-generated identity and timestamps |
| `status`, `requested_audiences`, `audience_coverage` | Python-derived completion/partial state |
| `company`, `executive_summary` | Accepted dossier sections |
| `opportunities` | Accepted shared strategy items |
| `audiences` | Accepted audience artifacts retaining references to shared opportunities |
| `evidence_gaps`, `contradictions` | Explicit retained limitations, including source and task failures |
| `facts`, `sources` | Only referenced facts, supporting excerpts, and authoritative source metadata |

Owner hash, input/configuration fingerprints, internal schema metadata, and expiry are attached by
Python in the cache envelope, never supplied by the models. Internal prompt/schema metadata does
not form part of the localStorage key. All retained references must resolve inside the cached
report itself so restoration needs neither a database nor another website/model request.
Store shared opportunities, facts, and source metadata once; Jinja resolves the audience references
at rendering time. Do not inflate the browser payload by copying the same shared details into every
talk point.

Review decisions are tied to the candidate's exact artifact revisions. Do not modify accepted
claims after review apart from deterministic reference resolution, formatting, and escaping.
Keep unreviewed candidates, full source documents, raw prompts, and provider response internals
out of the browser report.

### Scheduling, budgets, and typed stage gates

For `E` extraction chunks and `N` audiences, the normal path uses `E + N + 3` model calls:
extraction, one consolidation, one strategy, one call per audience, and one review. Twelve chunks
and five audiences therefore need 20 calls before transport retries or repairs. A 12-document
fetch limit is not a 12-chunk limit; long PDFs must be ranked and selected within the job budget.

Reserve enough input/output/call budget for consolidation, strategy, all requested audiences, and
review before scheduling extraction. The illustrative maxima in section 6 sum to 300,000 input
tokens for 12 maximum-sized extraction calls and five maximum-sized audience calls, leaving no
retry allowance. They are individual ceilings, not allocations every stage can consume together.
Use actual packed inputs and the deployment's tokenizer/estimator, including schema/instructions,
and leave an explicit reserve for eligible recovery work.

Pack final review with the candidate and its cited evidence, not redundant copies of every full
upstream artifact. Its 32,000-token input ceiling can be exceeded if all writers fill their output
ceilings. Bound upstream fields and plan reviewer capacity before generating them. Do not silently
drop an audience or omit evidence from review to make the request fit; return an explicit budget
outcome when the approved limits cannot support the requested report.

At every stage, use the same acceptance sequence:

```text
Reserve remaining task/job budget and concurrency capacity
  -> call the configured model with JSON input and strict output schema
  -> inspect provider completion, refusal, and truncation/error state
  -> parse the complete artifact and enforce its typed structure
  -> enforce references, grounding rules, counts, and size limits
  -> assign Python-owned IDs and retain the accepted stage artifact in memory
  -> schedule only dependents whose required input artifacts are accepted
```

Progress reports stage names and counts, such as reviewed sources or completed audiences, not
unreviewed generated text. The same schemas apply to initial and corrective calls. Config, prompts,
and accepted inputs are fixed for the job; repair cannot expand network permissions or pick a new
vendor/model.

### Failure and repair behavior

- Retry transient timeouts, `429`, and selected `5xx` errors with bounded backoff and jitter,
  respecting `Retry-After`, the task deadline, and the remaining job budget.
- Do not retry authentication failures, invalid model names, invalid configuration, or prohibited
  URLs as if they were transient.
- Validate every model response against a task-specific schema. Handle refusals, incomplete
  output, and invalid JSON explicitly; structured output support is not proof of factual accuracy.
- Refusals and truncated output fail the affected task with distinct outcomes. A completed response
  with malformed JSON, an invalid native shape, or a Python contract violation may receive exactly
  one task-local typed re-generation; a second failure stops the task. Do not feed rejected output
  downstream, create a prose/substring/coercion repair loop, or increase a task limit.
- A typed insufficient-evidence outcome is not a format failure. An incomplete audience may receive
  one optional audience-local coverage correction using the same accepted opportunities. If it
  remains limited, retain the explicit limitation without consuming the shared artifact repair.
- An invalid review contract may receive one local correction. Server-owned source metadata must
  be accepted; a core profile/evidence rejection must use a specific issue code and affected fact
  IDs rather than vague `insufficient_evidence`. This local correction does not consume the shared
  artifact-repair round.
- Every other schema-valid but domain-invalid pre-review artifact may receive one task-local
  validation correction with the original typed input, deterministic validation feedback, and the
  same output schema. Promote a successful corrected artifact to its canonical task key. This
  correction does not consume the shared artifact-repair round.
- Only substantive final-review findings may use the one shared artifact-repair allowance. This is
  a job-wide round, not one independent repair allowance per stage. Batch known review issues, map
  section IDs to their owning task in Python, and rerun affected artifacts in dependency order.
- A repair round can contain multiple calls and necessary dependent regenerations, all counted
  against the same call/token/deadline limits. Permit at most one post-repair final review. If an
  issue remains after that review, another shared round cannot start. Pre-review validation,
  optional audience, and review-contract corrections do not consume it.
- If the remaining budget cannot cover the required repairs and review, or issues remain after
  the allowance is used, retain only useful accepted/reviewed sections as an explicit partial
  report; otherwise fail. Do not publish a dependent section whose required evidence was rejected.
- Never silently substitute a cheaper model, another vendor, or a generic successful-looking
  report. A different route requires an explicit configuration change or approved policy.
- Retain successful stage artifacts in bounded process memory so retries during that lifetime
  do not re-fetch or regenerate unaffected work. Record attempts separately; a timed-out provider
  request may still have been billed. A restart loses this state; do not claim durable recovery.
- When repaired evidence or strategy changes, invalidate and regenerate its dependent artifacts
  within the remaining budgets. Never publish talk points built from superseded facts.
- Publish only accepted sections. A report with missing requested audiences is `partial`, not
  `completed`. If no useful, source-backed report can be produced, mark the job `failed`.
- Do not automatically resubmit a missing or expired job after a server restart. Explain that
  transient work is unavailable and let the user explicitly start again, since previous provider
  requests may already have incurred cost. A previously saved browser report restores without
  fetching sources or making model calls.

Astra's review uses a fresh context and an auditor prompt, but it is not independent proof of
correctness. Deterministic checks and human-reviewed evaluation examples remain necessary.

## 5. Recommended model assignment

OpenAI describes Luna as the fastest and most affordable GPT-5.6 tier, Terra as balanced, and Sol
as its GPT-5.6 flagship [1]. Current CXplorer task routing stays within those three GPT-5.6 tiers.

These are the recommended starting assignments based on those roles. Application-specific quality
and latency still need measurement on the actual vendor deployments; a model name alone does not
establish the best cost per accepted report.

| Task                                                 | Model           | Initial reasoning effort | Why this allocation                                                                                         |
|------------------------------------------------------|-----------------|--------------------------|-------------------------------------------------------------------------------------------------------------|
| Evidence extraction                                  | `gpt-5.6-luna`  | `low`                    | Repetitive, bounded extraction with direct quotations and deterministic validation                          |
| Evidence consolidation                               | `gpt-5.6-terra` | `medium`                 | Reconcile entities, dates, duplicates, and conflicting statements                                           |
| Company strategy and AI opportunities                | `gpt-5.6-terra` | `high`                   | The most consequential synthesis: connect evidence, business priorities, and feasible AI change             |
| Audience-specific talk points                        | `gpt-5.6-sol`   | `medium`                 | Strong professional writing and reasoning over an already established strategy                              |
| Final factual and strategic review                   | `gpt-5.6-terra` | `high`                   | Challenge unsupported claims, generic recommendations, missing controls, and cross-audience inconsistencies |
| Fetching, arithmetic, citation resolution, rendering | No AI           | Not applicable           | Deterministic work should remain deterministic                                                              |

Do not use GPT-6 Astra or reasoning levels outside `low`, `medium`, and `high` for current default
task routing. Hosted web search is enabled for the mandatory
official-announcement discovery task, not as unrestricted browsing for other stages. Escalate effort or
promote a task to a stronger model only when evaluation shows a meaningful benefit.
If Luna loses important facts, promote extraction to Terra rather than accepting poor evidence.
If Sol's role-specific output needs improvement, tune the prompt or evaluate Terra before changing
the approved model-family policy.

Model tier rates do not necessarily predict the cost per completed task. Compare the accepted
outcome, total attempts, reasoning tokens, latency, and billable usage.
Published prices can change, including temporary promotions; do not treat launch-post prices
as the deployment's rate card.

The backend must have its own authorized API access. A Copilot or chat subscription does not by
itself establish that the FastAPI service can call these models. Confirm vendor endpoints, API model
or deployment IDs, quotas, data handling, and supported parameters before implementation.

## 6. Configuration contract

AI settings follow the inline-documented shared-default and ignored-local-override pattern,
without duplicate `.example` files. The committed `ai_vendors.env` and `ai_tasks.env` files are
the authoritative reference for supported setting names, defaults, and hard bounds.

### File responsibilities and loading

| File                    | Responsibility                                                                            | Repository policy                                        |
|-------------------------|-------------------------------------------------------------------------------------------|----------------------------------------------------------|
| `app_config.env`        | Shared application defaults and AI configuration-file paths                             | Commit public defaults with inline documentation only    |
| `app_config.local.env`  | Private application/session settings and deployment overrides                              | Ignore; never commit secrets or private deployment data  |
| `id_vendor.env`         | Shared external identity-provider defaults                                                | Commit public defaults with inline documentation only    |
| `id_vendor.local.env`   | Private identity-provider credentials and deployment overrides                             | Ignore; never commit credentials or private identifiers  |
| `ai_vendors.env`        | Shared vendor adapter, public endpoint, transport and concurrency defaults                  | Commit inline documentation and safe defaults/placeholders only |
| `ai_vendors.local.env`  | Private vendor credentials, endpoint details and deployment overrides                      | Ignore; never commit credentials or private deployment data |
| `ai_tasks.env`          | Shared task routing, generation settings and workflow limits                               | Commit inline documentation and safe defaults; no credentials |
| `ai_tasks.local.env`    | Private task/model deployment mappings and local tuning                                    | Ignore; no credentials permitted |

The AI `.local.env` files are explicitly ignored. Shared-file
safety checks accept arbitrary non-sensitive settings and empty or clearly marked
placeholder sensitive values, not assert a fixed settings dictionary.
Production should provide credentials through environment variables or an access-restricted
secret mount, not through a tracked file or image layer.

The application-level settings are:

| Variable                | Type and default      | Meaning                                                                                    |
|-------------------------|-----------------------|--------------------------------------------------------------------------------------------|
| `AI_VENDOR_CONFIG_FILE` | Path, `ai_vendors.env` | Vendor file; run from the repository root or provide an explicit path                    |
| `AI_TASK_CONFIG_FILE`   | Path, `ai_tasks.env`  | Task file; same path-resolution rule as the vendor file                                   |

Use `AIVendorSettings(BaseSettings)` and `AITaskSettings(BaseSettings)` with UTF-8 dotenv files,
`env_nested_delimiter="__"`, `nested_model_default_partial_update=True`, and `extra="ignore"`.
Let Pydantic/python-dotenv handle sources, interpolation, parsing, and precedence:
constructor values, process environment, matching local file, shared file, then typed defaults.
Retain per-task/vendor defaults when a dictionary entry is only partially overridden.
Do not mix vendor secrets into task prompts or export dotenv values into the process environment.

Unrelated configuration roots are ignored. Typed nested fields, supported registry entries, and
operational bounds remain validated. Inconsistent task budgets fail explicitly.
Environment-only deployments need no dotenv files.
Report field names without secret values; do not silently fall back to an arbitrary model.
When disabled, show that new insights generation is unavailable rather than returning a simulated
report. Authenticated restoration of a valid browser-cached report does not require an AI provider.

There is no manual `AI_ENABLED` flag. OpenAI is enabled by its API key alone, using the SDK's
default endpoint. AzureOpenAI is enabled by an endpoint plus an API key or the complete
`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` process environment. At least one
enabled vendor enables generation; no configured vendors produces a startup warning with the
requirements. A task routed to an unavailable vendor fails explicitly at runtime, without silently
switching providers. Presence checks do not acquire tokens or verify service permissions.

Load a configuration version at process startup. Freeze the non-secret task configuration and its
fingerprint in memory for each job so an in-flight workflow uses consistent settings. Keep
credentials outside those snapshots; rotate them independently.

### `ai_vendors.env`

Support the two named vendors OpenAI and AzureOpenAI through the same official OpenAI SDK and
Responses API. Tasks refer to vendors rather than carrying endpoints or credentials. Default
AzureOpenAI authentication is the server's Entra ID credential chain, not the website user's OAuth
token or the `MS_*` login settings. Async Azure Identity requires the declared aiohttp transport.

```dotenv
CX_AI__AZURE_OPENAI__ENDPOINT=
CX_AI__AZURE_OPENAI__AUTH_MODE=entra_id
CX_AI__AZURE_OPENAI__API_KEY=
CX_AI__OPENAI__AUTH_MODE=api_key
CX_AI__OPENAI__API_KEY=
```

Canonical vendor keys are `CX_AI__OPENAI__<CONFIG>` and `CX_AI__AZURE_OPENAI__<CONFIG>`.
Task `VENDOR` values accept case/space/hyphen/underscore variants such as `Open AI` and
`Azure-OpenAI`. Legacy vendor-key shapes are not aliases and must be renamed in private overrides.
Task and pipeline keys retain their `CX_AI_TASK__` and `CX_AI_PIPELINE__` roots.

Azure Identity owns discovery of `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and `AZURE_CLIENT_SECRET`.
Document these as process environment variables; only check nonblank presence for availability,
without manually loading, validating, or passing identity values in application code.
Dotenv settings files do not populate the SDK's process environment.
The async `DefaultAzureCredential` chain and credential lifecycle remain SDK-managed.

| Vendor suffix             | Type / required or default                      | Meaning                                                                          |
|---------------------------|-------------------------------------------------|----------------------------------------------------------------------------------|
| `ENDPOINT`                | HTTPS URL, required for Azure                   | Azure resource/v1 base; OpenAI uses its SDK default without an endpoint setting  |
| `AUTH_MODE`               | `entra_id` for Azure, `api_key` for OpenAI        | Azure preference when both forms are configured; use the only available form otherwise |
| `API_KEY`                 | Secret, required only for used API-key routes    | Reject empty/example values when required; not needed for Entra authentication   |
| `CONNECT_TIMEOUT_SECONDS` | Positive number, default `10`                   | Connection-establishment timeout                                                 |
| `REQUEST_TIMEOUT_SECONDS` | Positive number, default `600`                  | Timeout per request attempt, further bounded by the remaining task deadline      |
| `MAX_RETRIES`             | Integer, default `2`, range `0..3`              | Additional transient-failure attempts, not additional repair rounds              |
| `MAX_CONCURRENCY`         | Positive integer, default `6`                   | Aggregate in-flight calls to this vendor across jobs in the application process  |
| `ORGANIZATION`            | Optional string, omitted by default             | OpenAI organization identifier, only when applicable                             |
| `PROJECT`                 | Optional string, omitted by default             | OpenAI project identifier, only when applicable                                  |

Do not treat arbitrary "OpenAI-compatible" gateways as supported vendors or assume every deployment
implements strict schemas, reasoning parameters, or hosted search. Configuration/protocol errors
remain explicit. Credentials are acquired lazily and clients are closed during application shutdown.
Log one privacy-safe lifecycle record before and after each model attempt. Start records contain
only the task ID, canonical vendor, model/deployment, reasoning effort, web-search flag, tool limit,
and effective timeout. Finish records contain elapsed time, sanitized outcome/request ID, and
provider-reported input, cached-input, cache-write, output, reasoning, and total token counts when
available. Record hosted search as a call count because the Responses API does not provide a
separate reliable web-search token total. Never log prompts, source content, structured model
output, credentials, or raw SDK exception messages.

Require TLS certificate verification. Treat configured API keys as `SecretStr` values and redact them from
errors, logs, job snapshots, reports, cookies, and frontend responses.
SDK retries are disabled; the task executor owns every attempt and budget reservation. Only
configured, domain-filtered web search is exposed. Azure search uses indexed results, not live
external page access, and may be subscription-blocked or separately billed. Native domain filters
include subdomains, so exact local host gates remain mandatory. Use `store=false`; it is not a
zero-retention guarantee under vendor/Bing policies.

### `ai_tasks.env`

Task IDs are a fixed, versioned application registry. The configured model value is the exact API
model/deployment ID understood by the referenced vendor. Switching a vendor or model should not
require changing business logic.

```dotenv
CX_AI_TASK__DISCOVER_NEWS__VENDOR=AzureOpenAI
CX_AI_TASK__DISCOVER_NEWS__MODEL=gpt-5.6-sol
CX_AI_TASK__DISCOVER_NEWS__REASONING_EFFORT=medium
CX_AI_TASK__DISCOVER_NEWS__USE_WEB_SEARCH=true
CX_AI_TASK__DISCOVER_NEWS__MAX_TOOL_CALLS=25

CX_AI_TASK__GENERATE_TALK_POINTS__VENDOR="Azure OpenAI"
CX_AI_TASK__GENERATE_TALK_POINTS__MODEL=gpt-5.6-sol
CX_AI_TASK__GENERATE_TALK_POINTS__REASONING_EFFORT=High
CX_AI_TASK__GENERATE_TALK_POINTS__USE_WEB_SEARCH=false
CX_AI_TASK__GENERATE_TALK_POINTS__MAX_TOOL_CALLS=0

CX_AI_PIPELINE__DEFAULT_AUDIENCES=ceo,cto,cio,cfo,ciso
CX_AI_PIPELINE__TALK_POINTS_PER_AUDIENCE=3
CX_AI_PIPELINE__MAX_SEED_URLS=6
CX_AI_PIPELINE__MAX_FETCHED_PAGES=12
CX_AI_PIPELINE__JOB_TIMEOUT_SECONDS=900
```

The fixed registry contains `verify_sources`, `discover_news`, `extract_evidence`,
`consolidate_evidence`, `synthesize_strategy`, `generate_talk_points`, and `review_report`.
A task ID becomes an uppercase prefix such as `CX_AI_TASK__REVIEW_REPORT__`.

| Task suffix | Type / requirement | Meaning |
|---|---|---|
| `VENDOR` | Supported vendor name | OpenAI or AzureOpenAI, including documented spelling variants |
| `MODEL` | Required string | Exact vendor model or deployment ID; no default substitution |
| `PROMPT_VERSION` | Required registered version | Versioned application prompt and compatible output schema, not an arbitrary file or URL |
| `REASONING_EFFORT` | Optional vendor-supported value | Example values are initial recommendations; omit when unsupported, never silently translate |
| `USE_WEB_SEARCH` | Boolean | Mandatory for official `discover_news`; false by default elsewhere |
| `MAX_TOOL_CALLS` | Integer `0..25` | Positive for enabled search; all attempts also share the job's 75-tool-call ceiling |
| `MAX_INPUT_TOKENS` | Required positive integer | Budget includes instructions, evidence, and schema; reject or reduce input before the call |
| `MAX_OUTPUT_TOKENS` | Required positive integer | Provider output budget, including reasoning tokens where the provider counts them |
| `TIMEOUT_SECONDS` | Required positive number | Whole-task deadline across attempts, bounded by the job deadline |
| `TEMPERATURE` | Optional number, omitted by default | Send only when supported by this model and adapter; do not force `0` onto reasoning models |

Validate parameters and input-plus-output limits against the actual deployment's capabilities.
Map settings to the adapter's real request fields; Responses API and Chat Completions formats are
not interchangeable. Use schema-constrained output and validate it again with Pydantic.
Pass the audience as validated task input to `generate_talk_points`; do not duplicate the entire
pipeline configuration for each role.

All `CX_AI_PIPELINE__` variables below belong in `ai_tasks.env`. The inline configuration files
also document newer admission, retention, news, and tokenizer options. Defaults are bounds, not
measured latency guarantees.

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
| `MAX_REPAIR_ROUNDS` | `1` | Integer `0..1`; one shared final-review artifact-repair round at most |
| `JOB_TIMEOUT_SECONDS` | `900` | Hard end-to-end processing deadline after a queued job starts running |

Numeric limits must be positive except explicitly permitted zero values. Require fetched-page
capacity to cover the seed cap. Enforce safe implementation ceilings, not merely administrator
preferences, on download sizes, concurrency, and parsing resources.
Count retries and repairs against all applicable budgets. Before a call, reserve its allowed
budget; use provider-reported usage afterwards. Treat uncertain usage after a timeout
conservatively. Use a compatible tokenizer or documented conservative estimator, not an assumed
universal characters-per-token conversion.

## 7. Application architecture and browser persistence

The application supplies FastAPI/Jinja pages, typed configuration, external login, private route
groups, session-bound CSRF tokens, the structured insights pipeline, AI clients, browser caching,
and bounded in-memory jobs. There is no database, filesystem report store, durable queue, or
permanent server-side user/report history for this feature.

Preserve the existing server-rendered architecture. Keep new authenticated routes in
`src\cxplorer\routers\private.py`; keep service logic out of route functions. Use `require_user`
for private APIs and authenticated mutations, and redirect unauthenticated private pages to
`/login`. Reuse or extract the existing logout CSRF validation for every state-changing request.
Validate redirects with `safe_local_path`.

### Login, email, and the user cache namespace

Continue using external identity providers only; do not add internal registration or passwords.
The OAuth flow requests `openid profile email`. The identity model requires the provider's email
claim and does not fall back to `preferred_username`: a username is not necessarily an email address.

Require a usable email supplied by the validated provider identity before creating an authenticated
session. Do not substitute an arbitrary username, prompt for a contact email, or accept an email
from browser storage. On missing or unusable email, clear the pending login state and return to
the neutral login page with a specific, actionable message that the provider did not share a
usable email address. Do not claim mailbox verification merely because a claim contains an email.

Compute the user cache namespace on the server from the validated session email alone:

```python
user_hash = hashlib.sha256(user.email.encode("utf-8")).hexdigest()
```

Use the same validated email string consistently; the browser must not independently transform it
or calculate a different identity. Do not add provider, issuer, tenant, subject, salt, or a schema
version to the hash input. Do not use Python's process-randomized `hash()`.

The same email maps to the same namespace across providers and tenants. An email change selects
a different namespace; a display-name change does not. This intentionally uses the provider-asserted
email as the application's user key, rather than retaining provider-scoped ownership underneath
an email-only cache key. Do not add issuer/subject session fields solely for caching.

Authentication still performs its normal provider signature, issuer, audience, nonce, and state
checks. Removing these identifiers from the cache namespace does not relax login verification.
Every private request derives the owner hash again from the authenticated session email, never
from a client-submitted email, hash, or owner ID. Existing sessions missing a usable email must
sign in again. Keep email out of AI inputs and report-cache envelopes.

### Routes and experience

| Method and URL | Purpose |
|---|---|
| `GET /dashboard` | Existing workspace: URL entry, optional seller context, audience selection, browser-cache restoration controls |
| `POST /insights` | CSRF-protected validation and in-memory enqueue; redirect to the progress page |
| `GET /insights/{report_id}` | Owner-authorized transient progress/report, or an authenticated browser-cache restore shell |
| `GET /api/private/insights/{report_id}/status` | Owner-authorized structured status for progressive enhancement |
| `GET /api/private/insights/{report_id}/cache` | Owner-authorized signed, compressed envelope for accepted report sections |
| `GET /insights/{report_id}/download` | Owner-authorized JSON download of the accepted temporary report |
| `POST /api/private/insights/cached-list` | CSRF-protected server-rendered history fragment from bounded untrusted browser metadata |
| `POST /insights/restore` | CSRF-protected verification of a browser report; restore temporarily in memory for SSR |
| `POST /insights/{report_id}/retry` | CSRF-protected, quota-controlled retry while eligible artifacts remain in memory |
| `POST /insights/{report_id}/cancel` | CSRF-protected cancellation with visible state |
| `POST /insights/{report_id}/delete` | CSRF-protected deletion of any owned transient copy; the browser also removes its local entry |

Keep `/dashboard` as the workspace rather than introducing a second URL-entry page. Use the
existing dark navy/slate theme and shared components.

The form and report work without JavaScript while their transient server data exists. Browser
save/restore necessarily requires JavaScript and available localStorage; make this limitation
visible rather than implying server-side history. Dependency-free JavaScript handles draft
restoration, cache transport, status polling, and accessible announcements. Report rendering and
authorization remain on the server. Render any cached-history metadata as untrusted, escaped data;
opening a report still requires verification of its signed envelope.

Use semantic sections for each audience, a source list, evidence-gap notices, and print-friendly
shared styles. Do not render model-produced or browser-cached HTML, execute inline scripts, or
expose unreviewed token streams.

### Bounded, transient jobs

Use an application-lifecycle-managed, in-memory job manager with bounded asynchronous work.
Return the progress page promptly rather than holding the generation HTTP request open for the
entire pipeline. Do not introduce SQLAlchemy, migrations, PostgreSQL, Redis, Celery, or a separate
durable worker for this design.

The first deployment uses one application process. Bound active jobs per user, total
active/queued jobs, source and artifact bytes, and retained results. Reserve capacity before
accepting work and release it on completion, failure, cancellation, or expiry. Parsing must not
block the event loop and must retain its own time/memory limits.

Keep successful stage artifacts only for bounded, in-process retries. Default transient result
retention is up to one hour after completion, subject to a global memory bound; release full source
bodies as soon as they are no longer needed. Browser persistence lasts seven days independently.
Do not claim cross-restart checkpoints, durable quotas, or exactly-once external execution.
Check retry eligibility against the complete remaining stage and fresh-review reservations,
not only the individual failed task. A same-ID partial-report retry retains its original generation
timestamp and seven-day cache expiry rather than extending retention.

A reload, restart, or process failure loses unfinished jobs and any results not saved in the
browser. Multiple processes/replicas cannot share these job dictionaries: ongoing jobs would need
sticky routing and coordinated limits or a separately approved architecture. Already cached
reports can be restored to a fresh instance with the same signing configuration. Do not introduce
hidden durable storage to work around this constraint.

Track `queued`, `running` plus the current stage, `completed`, `partial`, `failed`, and `cancelled`.
Keep cancellation separate from provider failure; an already submitted model request may still
finish or incur cost. Deduplicate repeated submissions within the current process lifetime, and
use provider idempotency where supported. Do not promise duplicate-charge prevention across
restarts; never automatically regenerate just because a job or browser cache entry is missing.

### Transient data contracts

| Record          | Essential contents                                                                                                        |
|-----------------|---------------------------------------------------------------------------------------------------------------------------|
| Request/job     | Derived owner ID, approved seeds/hosts, audiences, seller context, state, deadlines, redacted configuration version       |
| Source document | Source ID, original/final URL, title, media type, retrieval/publication dates, hash, parse coverage and outcome           |
| Evidence fact   | Fact ID, source references, short exact excerpts with page/section locations, company-reported/inferred status, conflicts |
| Company dossier | Structured company fields, fact references, reporting periods, explicit unknowns                                          |
| Strategy brief  | Prioritized hypotheses, business outcomes, prerequisites, controls, evidence references                                   |
| Audience set    | Audience ID, opening, requested talk-point count, questions, objections, next-step ask                                    |
| Quality review  | Decision, affected section IDs, issue codes, evidence references, repair outcome                                          |
| Task run        | Task/prompt/schema version, vendor/model, attempts, provider request ID, timing, usage, sanitized error                   |
| Report          | Accepted structured sections, audience coverage, sources, as-of date, owner, schema version                               |

These records are bounded in-memory structures, not database tables. Every read, retry,
cancellation, deletion, cache-envelope request, and eventual export must enforce ownership.
Do not share seller context or generated reports across users. Retain only necessary supporting
excerpts in the accepted report, not complete websites, raw PDFs, prompts, or hidden chain-of-thought.

### Browser cache contract

Use the email hash in unversioned keys for both input drafts and generated reports:

```text
cxplorer:<user_hash>:draft
cxplorer:<user_hash>:report:<report_id>
```

Keep one current input draft per user, including URLs, purpose hints, and selected generation
options. Restore it after login and save edits with a small debounce. Treat every restored input
as untrusted and perform the normal server-side URL/audience/context validation on submission.

Store reports separately by report ID, with input fingerprint, generation time, expiry, and a
signed compressed payload. Internal format/schema metadata may describe the payload, but never
appears in the storage key and does not select a migration namespace. The input fingerprint covers
normalized URLs, purpose hints, audiences, seller context, discovery settings, and applicable task/prompt/schema
versions. A report for different inputs or an older generation version must not appear as a fresh
matching result. Show the original as-of date and make regeneration an explicit user action.

If an entry cannot be read from localStorage, deserialized, decoded, decompressed, or accepted by
the current data contract, it is invalid/unusable and must not be restored. Discard only the
affected corrupt/incompatible entry when storage access permits and show an accessible notice.
Do not guess the intended structure, search alternate versioned keys, or silently regenerate.
A missing key is a normal cache miss. A storage access denial is an unavailable cache for that
attempt, not permission to clear unrelated data.

Failure to contact the restore endpoint, an expired login, or a temporary server error does not
prove the cached bytes are corrupt. Preserve the entry in those cases and surface sign-in/retry
guidance. Delete an entry for corruption only when the local reader or authenticated server
reports a definite payload/format failure.

Retain both kinds of cache across sign-out and use a seven-day expiry. Draft expiry is measured
from the last edit; report expiry is measured from generation, not the most recent view or restore.
Remove expired entries on application use and cache access. localStorage has no native expiry
mechanism and cannot physically delete entries while the application is closed.

The browser cache is the only persistent report copy, not a permanent archive or cross-device
store. A new browser profile, cleared/evicted storage, or expiry can make a report unavailable.
Provide a clear-cache action scoped to CXplorer data, and keep local deletion independent of
whether an expired transient server job still exists.

### Compression, integrity, and server-rendered restoration

Compress the accepted report on the server using standard-library gzip, then base64-encode it
for string-only localStorage. Keep the small URL draft as ordinary JSON. This avoids a frontend
compression dependency or a requirement for browser CompressionStream support. Measure the
actual encoded storage footprint, including base64/envelope overhead; do not promise a fixed ratio.

Sign the compressed report envelope with HMAC-SHA-256 under a purpose-separated application
signing key. Bind its owner hash, report/schema version, generation/expiry times, input fingerprint,
and report data. Keep the secret on the server and stable across restarts; use runtime/private
configuration, not a random per-start key or a value exposed to JavaScript. Signing-key rotation
needs a verification grace period covering unexpired caches, or it will invalidate those copies.

The user hash is only a namespace, and the signature is only an integrity/owner-binding mechanism.
Neither encrypts browser data nor makes localStorage private from other users of the same browser
profile. Never put OAuth tokens, CSRF tokens, session secrets, email, or the full login profile in
these caches. Do not treat the envelope as a credential that can authenticate a user.

Restoration follows this flow without any AI calls or source fetching:

```text
Authenticated workspace/report restore shell
  -> browser reads the current user's unexpired cache entry
  -> POST the signed, compressed envelope with the current session CSRF token
  -> server bounds the request and verifies the signature before bounded decompression
  -> validate schema, expiry, and owner against SHA-256 of the authenticated session email
  -> hold the accepted report temporarily in memory and render it with Jinja
```

Reject invalid, tampered, oversized, expired, wrong-owner, or incompatible envelopes with a
visible error. Never render cached HTML or accept a client-supplied owner hash as authorization.
An authenticated restore does not need configured AI access, but it does require a compatible
cache reader and the signing configuration that can verify the envelope.

Bound compressed and decompressed sizes, cache record sizes, and total browser storage use.
Cache limits, separate from the seven-day expiry, are:

| Limit | Value | Enforcement |
|---|---|---|
| Compressed report payload | 512 KiB | Reject oversized envelopes before expensive decoding or parsing |
| Decompressed report payload | 2 MiB | Enforce a streaming decompression ceiling and bounded report schema |
| Total CXplorer browser cache | 2 MiB per origin/browser profile | Include keys, draft data, metadata, base64, and string-storage overhead across user namespaces |

These are application ceilings, not a guarantee that a browser grants that much free space.
Measure the final encoded footprint conservatively and still handle actual storage quota errors.
Remove expired entries first. If space remains insufficient, do not silently delete still-valid
reports or claim that the new result was saved: show a clear warning, keep the temporary result
available while its memory lifetime permits, and offer clear-space/retry-save and download actions.
Report unavailable storage, malformed cache data, and save failures accessibly without preventing
generation or rendering from functioning. With JavaScript disabled, explain that browser
persistence is unavailable. Cached report viewing still requires authenticated server rendering;
offline report rendering is not part of this MVP.

### Implementation locations

```text
src\cxplorer\ai\config.py          Typed vendor/task registries and validation
src\cxplorer\ai\providers.py       Explicit provider adapters and usage normalization
src\cxplorer\ai\tasks.py           Typed task execution, deadlines, schema handling
src\cxplorer\insights\prompts.py   Versioned task and audience instructions
src\cxplorer\insights\schemas.py   Request, evidence, strategy, audience, report contracts
src\cxplorer\insights\sources.py   URL policy, fetching, extraction, provenance
src\cxplorer\insights\pipeline.py  Bounded workflow and repair rules
src\cxplorer\insights\jobs.py       Bounded in-memory jobs, ownership, cancellation and expiry
src\cxplorer\insights\cache.py      Signed/compressed report envelopes and restore validation
src\cxplorer\auth\models.py        Required provider email and SHA-256(email) cache identity
src\cxplorer\routers\private.py    Authenticated pages and APIs
src\cxplorer\templates\           Server-rendered forms, progress, reports
src\cxplorer\static\css\app.css    Shared responsive and print styles
src\cxplorer\static\js\           Dependency-free browser caching and progress enhancement
```

Continue starting the web app with
`python server.py`; no separate durable worker is required. Document the single-process in-memory
job limitation and the effect of `RELOAD` on unfinished work.

## 8. Safety, provenance, and operational boundaries

### URL and document ingestion

Apply SSRF defenses at both the application and egress-network layers [2]. Restrict schemes and
ports; reject URL credentials, private/local/reserved addresses, cloud metadata destinations,
and unsupported encodings. Validate all resolved IPv4/IPv6 addresses and every redirect hop.
Disable automatic redirects and authorize each hop explicitly.

Use HTTPX first with a desktop Windows Chrome/Edge user-agent. On HTTP 403 or a recognized
browser challenge, try `cloudscraper` once with a desktop Windows Chrome profile. Run its
synchronous client in a disposable, credential-free Python worker that is killed and reaped on
timeout or cancellation. Keep the original request deadline, host concurrency/crawl delay, and
shared transfer/decompression limits; allow at most four fallback HTTP requests and one
challenge-solving round. Both clients use Certifi with certificate and hostname verification.

Every fallback connection, including challenge subrequests, is pinned to the already-validated
public address and original TLS hostname. Only the original document and same-origin Cloudflare
challenge endpoints may be requested inside that exchange. An IUAM form POST to the root or
original path must match the exact form action advertised by the preceding Cloudflare challenge;
arbitrary document POST targets are not authorized. Return redirects to other document
targets to the main fetcher for normal host authorization and robots checks. Fallback cookies are
ephemeral to one attempt, never application/session cookies. Keep evaluating robots directives
as `CXplorerInsights` regardless of the browser transport header. HTTP 401/429, authentication,
paywalls, CAPTCHAs, and robots denials do not trigger access-policy bypasses.

For initial seed transport only, recognize the public registrable apex and its exact `www`
counterpart using the bundled public suffix list. Do not expand private-suffix tenants, arbitrary
subdomains, IP literals, or unknown suffixes. After fetching and company verification, rebuild the
research allowlist from exact accepted final document hosts only. Merely supplied seed hosts,
redirect-only original hosts, and unused canonical counterparts are not verified publishers.

Always run domain-filtered official-announcement discovery. Locally authorize and fetch each
candidate, then require company relevance and a supported publication date within the last
90 days. Executive attributions require source evidence. Exclude conflicting, undated, future,
stale, or third-party candidates with visible coverage limitations; do not substitute retrieval
time or broaden the search when official coverage is sparse.

Protect against DNS rebinding by using the validated resolution for the connection, retaining
correct TLS hostname verification, and enforcing an egress policy. A one-time DNS check followed
by an unrestricted second resolution is insufficient.

Bound streaming/decompressed bytes, document counts, parsing time, memory, and PDF pages. Match
declared content types with actual supported formats. Document extraction does not execute page
scripts, embedded actions, external entities, or document-supplied network requests. The isolated
browser fallback may interpret its supported Cloudflare challenge only; it is not a general
JavaScript renderer, and unsupported challenges fail explicitly. Do not expose stored source
documents as active same-origin HTML.

### Prompt injection and output handling

All fetched text and seller-supplied context are untrusted data [3]. Keep them separate from
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
Initialize application logging at `INFO` with timestamp, level, logger name, and message fields.
Use `CustomLoggerConfig` to default `azure.*` namespaces to `WARNING`. Emit paired before/after
records for each server-owned stage, source-fetch attempt, pipeline task, and provider call.
Provider before records include vendor, model/deployment, reasoning effort, web-search state, and
tool limit; after records emphasize status, duration, provider usage, and sanitized request IDs.

## 9. Efficiency and quality acceptance

Deduplicate identical normalized source content by hash within the bounded in-memory job scope.
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
- Every AI stage returns its registered structured JSON artifact; no stage or renderer depends
  on extracting fields from a free-form Markdown answer.
- Ownership and CSRF protections cover all new routes; user URLs cannot access internal services.
- Missing or unusable provider email fails login with a neutral, actionable error. Cache identity
  is exactly SHA-256 of the session email, independent of provider/issuer/subject and display name.
- Storage keys have no version component. A changed email changes the namespace, and unreadable,
  malformed, or undecompressible entries are invalid rather than inputs to guessed migrations.
- Input drafts and accepted reports are cached under the authenticated user's namespace, survive
  sign-out, and enforce seven-day expiry without exposing authentication material.
- A valid compressed browser report restores after an application restart without model calls,
  while a forged/wrong-owner envelope is rejected and browser-cache content is never trusted HTML.
- Interrupted in-memory jobs and unsaved results are reported as unavailable, not silently
  regenerated or described as durably recoverable.
- Quota or storage failures never claim persistence or silently discard another valid report.

Use a small evaluation corpus spanning public/private companies, several industries, sparse sites,
financial PDFs, contradictory dated claims, and hostile document instructions. Compare candidate
models on the same task inputs, prompts, schemas, and human-reviewed expected evidence.
Require no material factual errors on the release evaluation set and a target human rating of at
least 4/5 for audience relevance, business clarity, and actionability. These are release gates,
not a guarantee of perfect results on unseen websites.

Use existing pytest and Ruff tooling during implementation. Cover config parsing, missing secrets,
unknown task/vendor IDs, unsupported parameters, URL/redirect/DNS policy, extraction, source IDs,
cross-user access, CSRF, retries, budgets, cancellation, transient expiry, and role coverage.
Also cover required provider email, exact email hashing, unversioned keys, same-email identity
across providers, email changes, account switching, seven-day cache expiry, corrupt entries,
compression bounds, forged cache envelopes, restoration after restart, and storage failures.
Exercise every task's JSON contract, unknown references, quotation mismatch, missing role counts,
refusal/truncation, repair-budget exhaustion, and invalidation of dependent artifacts.
Keep normal tests offline with fixtures; live model evaluation is explicit and separately budgeted.

## 10. Delivery sequence and decisions before implementation

| Phase                              | Deliverable                                                                                               | Exit condition                                                                                            |
|------------------------------------|-----------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| 0. Confirm deployment and evaluate | Actual vendor/API mappings, capability checks, small extraction/strategy/writing comparison               | Working authorized model access, documented quotas/data policy, evidence that routing meets quality needs |
| 1. Foundation                      | Typed shared/local config, required email, stable user namespace, URL draft cache, schemas and in-memory jobs | Configuration errors are explicit; account-scoped drafts restore; transient job limits are enforced |
| 2. Thin end-to-end slice           | Safe source collection through a sourced company profile, one audience, review, and SSR report            | A real request completes without manual handoffs or fabricated fallback output                            |
| 3. Full report and browser cache   | Five audience sets, shared strategy, bounded repair, signed/compressed report cache and SSR restoration   | Every selected audience meets quality gates; a cached report restores without server history or AI calls |
| 4. Release readiness               | Ownership, memory/storage bounds, deletion, restart behavior, operating visibility and quality evaluation | The acceptance criteria and approved stateless hosting/data policies are met |

The email requirement, email-only cache identity, unversioned storage keys, structured AI JSON,
absence of persistent server storage, and seven-day browser retention across sign-out are confirmed.
Before implementation, approve the complete flow and confirm actual vendor/API access and model
deployment names, single-process hosting and source-fetch egress controls, transient/cache size
bounds, signing-key management, and whether
seller-specific offerings are needed for the first release. The proposed defaults remain five
audiences, English output, optional seller context, and public company sources only.

Defer unrestricted web search, scanned-PDF OCR, browser automation for JavaScript-only sites,
CRM enrichment, personal executive profiling, email generation/sending, and slide-deck exports.
Add them only as explicit later scope, with their own access, quality, and operating requirements.

## 11. Reference basis

The model references below were consulted on 2026-09-05; the structured-output contract was
reviewed against the provider guide on 2026-09-07. Recheck deployment-specific capabilities and
commercial terms before implementing; task routing remains configuration-driven.

1. [OpenAI: GPT-5.6 family positioning](https://openai.com/index/gpt-5-6/)
2. [OWASP: SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
3. [OWASP: LLM prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
4. [OpenAI: Structured Outputs, schemas, refusals, and incomplete responses](https://developers.openai.com/api/docs/guides/structured-outputs)

Repository baseline: `README.md`, `pyproject.toml`, `.github\copilot-instructions.md`,
`src\cxplorer\config.py`, `src\cxplorer\routers\private.py`,
`src\cxplorer\auth\models.py`, and `src\cxplorer\auth\dependencies.py`.
