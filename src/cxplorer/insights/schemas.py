"""Typed AI artifacts and the self-contained, server-rendered report contract."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Audience = Literal["ceo", "cto", "cio", "cfo", "ciso"]
AUDIENCE_LABELS: dict[str, str] = {
    "ceo": "CEO",
    "cto": "CTO",
    "cio": "CIO",
    "cfo": "CFO",
    "ciso": "CISO",
}
SourcePurpose = Literal[
    "homepage",
    "about",
    "products",
    "investors",
    "newsroom",
    "trust",
    "industry",
    "careers",
    "other",
    "news",
]
ShortText = Annotated[str, Field(min_length=1, max_length=600)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")]
FactReferences = Annotated[list[Identifier], Field(max_length=16)]


class Artifact(BaseModel):
    """Closed data objects; provider JSON never controls application execution."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SeedInput(Artifact):
    url: str = Field(min_length=1, max_length=2048)
    purpose: SourcePurpose

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        from cxplorer.insights.urls import normalize_url

        return normalize_url(value)


class InsightRequest(Artifact):
    seeds: list[SeedInput] = Field(min_length=1, max_length=6)
    audiences: list[Audience] = Field(
        default_factory=lambda: ["ceo", "cto", "cio", "cfo", "ciso"],
        min_length=1,
        max_length=5,
    )
    seller_context: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if self.seeds[0].purpose != "homepage":
            raise ValueError("Provide the official company homepage first.")
        if any(seed.purpose == "homepage" for seed in self.seeds[1:]):
            raise ValueError("Provide only one official company homepage.")
        if len({seed.url for seed in self.seeds}) != len(self.seeds):
            raise ValueError("Each company source URL must be different.")
        if len(set(self.audiences)) != len(self.audiences):
            raise ValueError("Select each executive audience only once.")
        return self


class GroundedText(Artifact):
    text: str | None = Field(max_length=1200)
    basis: Literal["source_backed", "analysis", "hypothesis", "unknown"]
    fact_ids: FactReferences

    @model_validator(mode="after")
    def consistent_basis(self) -> Self:
        if self.basis == "unknown":
            if self.text is not None or self.fact_ids:
                raise ValueError("Unknown claims must have null text and no fact references.")
        elif not self.text or not self.text.strip() or not self.fact_ids:
            raise ValueError("Company claims and hypotheses require text and supporting fact IDs.")
        if len(self.fact_ids) != len(set(self.fact_ids)):
            raise ValueError("Fact references must be unique.")
        return self


class EvidenceGap(Artifact):
    area: str = Field(min_length=1, max_length=100)
    detail: ShortText


class EvidenceQuote(Artifact):
    span_id: Identifier
    quote: str = Field(min_length=1, max_length=1000)


class SourceVerdict(Artifact):
    source_id: Identifier
    relation: Literal["related", "unrelated", "uncertain"]
    rationale: ShortText
    evidence: list[EvidenceQuote] = Field(max_length=6)
    published_date: str | None = Field(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_evidence: list[EvidenceQuote] = Field(max_length=3)
    executive_name: str | None = Field(max_length=160)
    executive_role: str | None = Field(max_length=160)
    executive_evidence: list[EvidenceQuote] = Field(max_length=3)

    @field_validator("published_date")
    @classmethod
    def iso_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)
        return value


class SourceVerification(Artifact):
    company_name: str = Field(min_length=1, max_length=160)
    homepage_source_id: Identifier
    verdicts: list[SourceVerdict] = Field(min_length=1, max_length=12)


class NewsCandidate(Artifact):
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=240)
    published_date: str | None = Field(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
    executive_name: str | None = Field(max_length=160)
    executive_role: str | None = Field(max_length=160)
    summary: ShortText


class NewsDiscovery(Artifact):
    announcements: list[NewsCandidate] = Field(max_length=6)
    evidence_gaps: list[EvidenceGap] = Field(max_length=8)


class QuotedQuantity(Artifact):
    numeric_text: str = Field(min_length=1, max_length=100)
    unit: str | None = Field(max_length=100)
    currency: str | None = Field(max_length=30)
    period: str | None = Field(max_length=120)
    kind: Literal["actual", "forecast", "target", "unknown"]
    span_id: Identifier


class FactContent(Artifact):
    category: Literal[
        "company",
        "offering",
        "product_workflow",
        "business_model",
        "industry",
        "customers",
        "geography",
        "initiative",
        "strategy",
        "financial",
        "technology",
        "leadership",
        "security",
        "news",
    ]
    entity: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=1200)
    attribution: Literal["company_reported"]
    quantities: list[QuotedQuantity] = Field(max_length=8)
    time_context: str | None = Field(max_length=160)


class FactCandidate(FactContent):
    evidence: list[EvidenceQuote] = Field(min_length=1, max_length=6)


class EvidenceBatch(Artifact):
    facts: list[FactCandidate] = Field(max_length=20)
    evidence_gaps: list[EvidenceGap] = Field(max_length=12)


class Citation(EvidenceQuote):
    source_id: Identifier
    page: int | None = Field(ge=1, le=200)
    section: str | None = Field(max_length=300)


class QuantityWarning(Artifact):
    code: Literal["quantity_numeric_mismatch"] = "quantity_numeric_mismatch"
    quantity_index: int = Field(ge=0, le=7)
    span_id: Identifier
    numeric_text: str = Field(min_length=1, max_length=100)
    detail: ShortText


class EvidenceFact(FactContent):
    id: Identifier
    evidence: list[Citation] = Field(min_length=1, max_length=6)
    warnings: list[QuantityWarning] = Field(default_factory=list, max_length=8)


class SourceExclusion(Artifact):
    source_id: Identifier
    reason: ShortText


class EntityResolution(Artifact):
    status: Literal["matched", "ambiguous"]
    company_name: GroundedText
    included_source_ids: list[Identifier] = Field(min_length=1, max_length=12)
    excluded_sources: list[SourceExclusion] = Field(max_length=12)


class CompanyProfile(Artifact):
    business_description: GroundedText
    industries: list[GroundedText] = Field(max_length=8)
    offerings: list[GroundedText] = Field(max_length=12)
    customer_segments: list[GroundedText] = Field(max_length=8)
    geographies: list[GroundedText] = Field(max_length=8)
    recent_initiatives: list[GroundedText] = Field(max_length=10)
    strategic_priorities: list[GroundedText] = Field(max_length=10)
    financials: list[GroundedText] = Field(max_length=8)
    technology: list[GroundedText] = Field(max_length=8)
    leadership: list[GroundedText] = Field(max_length=8)
    security: list[GroundedText] = Field(max_length=8)


class FactGroup(Artifact):
    fact_ids: FactReferences
    relation: Literal["duplicates", "supporting", "time_series"]


class Contradiction(Artifact):
    fact_ids: list[Identifier] = Field(min_length=2, max_length=8)
    subject: str = Field(min_length=1, max_length=160)
    explanation: ShortText


class CompanyDossier(Artifact):
    entity_resolution: EntityResolution
    company_profile: CompanyProfile
    executive_summary: list[GroundedText] = Field(max_length=5)
    fact_groups: list[FactGroup] = Field(max_length=32)
    contradictions: list[Contradiction] = Field(max_length=12)
    evidence_gaps: list[EvidenceGap] = Field(max_length=20)


class Prerequisites(Artifact):
    data: list[ShortText] = Field(max_length=4)
    integration: list[ShortText] = Field(max_length=4)
    business_owner: list[ShortText] = Field(max_length=4)
    adoption: list[ShortText] = Field(max_length=4)
    security: list[ShortText] = Field(max_length=4)
    governance: list[ShortText] = Field(max_length=4)


class SuccessCriterion(Artifact):
    metric: str = Field(min_length=1, max_length=300)
    baseline_fact_ids: FactReferences
    target: str | None = Field(max_length=200)
    measurement_plan: ShortText


class OpportunityCandidate(Artifact):
    priority: int = Field(ge=1, le=6)
    title: str = Field(min_length=1, max_length=180)
    approach: Literal[
        "ai_assisted",
        "ai_augmented",
        "bounded_autonomy",
        "non_ai",
        "discovery_only",
    ]
    company_signal: GroundedText
    hypothesis: GroundedText
    workflow: ShortText
    business_outcome: GroundedText
    prerequisites: Prerequisites
    success_criteria: list[SuccessCriterion] = Field(min_length=1, max_length=3)
    human_controls: list[ShortText] = Field(min_length=1, max_length=4)
    non_ai_alternative: str | None = Field(max_length=600)
    next_step: ShortText


class Opportunity(OpportunityCandidate):
    id: Identifier


class StrategyBrief(Artifact):
    strategic_summary: list[GroundedText] = Field(max_length=5)
    opportunities: list[OpportunityCandidate] = Field(max_length=6)
    evidence_gaps: list[EvidenceGap] = Field(max_length=16)


class TalkPoint(Artifact):
    opportunity_id: Identifier
    company_signal: GroundedText
    seller_line: GroundedText
    audience_relevance: GroundedText
    discovery_question: ShortText
    success_measure: ShortText
    next_step: ShortText


class Objection(Artifact):
    objection: ShortText
    response: GroundedText


class AudienceTalkPoints(Artifact):
    audience: Audience
    coverage: Literal["complete", "limited", "insufficient_evidence"]
    opening: GroundedText
    talk_points: list[TalkPoint] = Field(max_length=5)
    discovery_questions: list[ShortText] = Field(max_length=4)
    objections: list[Objection] = Field(max_length=3)
    next_step_ask: ShortText
    evidence_gaps: list[EvidenceGap] = Field(max_length=8)


IssueCode = Literal[
    "unsupported_claim",
    "incorrect_attribution",
    "unknown_reference",
    "quote_mismatch",
    "entity_mismatch",
    "numeric_or_period_mismatch",
    "missing_audience",
    "wrong_point_count",
    "generic_talk_point",
    "missing_prerequisite",
    "contradiction_unresolved",
    "insufficient_evidence",
]


class ReviewIssue(Artifact):
    code: IssueCode
    section_id: Identifier
    fact_ids: FactReferences
    description: ShortText
    correction: ShortText


class SectionReview(Artifact):
    section_id: Identifier
    decision: Literal["accept", "repair", "reject"]


class QualityReview(Artifact):
    decision: Literal["accept", "repair", "insufficient_evidence"]
    section_reviews: list[SectionReview] = Field(min_length=1, max_length=100)
    issues: list[ReviewIssue] = Field(max_length=40)


class SourceRecord(Artifact):
    id: Identifier
    original_url: str = Field(min_length=1, max_length=2048)
    url: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    media_type: Literal["text/html", "application/pdf"]
    retrieved_at: datetime
    published_at: date | None
    content_hash: str = Field(min_length=32, max_length=64)
    purpose: str = Field(min_length=1, max_length=30)
    is_news: bool
    coverage_note: str | None = Field(max_length=600)

    @field_validator("url", "original_url")
    @classmethod
    def safe_report_link(cls, value: str) -> str:
        from cxplorer.insights.urls import normalize_url

        return normalize_url(value)


class SourceOutcome(Artifact):
    url: str = Field(max_length=2048)
    status: Literal["accepted", "rejected", "skipped"]
    reason: ShortText


class Announcement(Artifact):
    source_id: Identifier
    title: str = Field(min_length=1, max_length=300)
    published_at: date
    executive_name: str | None = Field(max_length=160)
    executive_role: str | None = Field(max_length=160)


class AudienceCoverage(Artifact):
    audience: Audience
    status: Literal["complete", "limited", "insufficient_evidence", "unavailable"]


class AcceptedReport(Artifact):
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    generated_at: datetime
    as_of_date: date
    status: Literal["completed", "partial"]
    requested_audiences: list[Audience] = Field(min_length=1, max_length=5)
    audience_coverage: list[AudienceCoverage] = Field(min_length=1, max_length=5)
    company_name: GroundedText
    company: CompanyProfile
    executive_summary: list[GroundedText] = Field(max_length=5)
    opportunities: list[Opportunity] = Field(max_length=6)
    audiences: list[AudienceTalkPoints] = Field(max_length=5)
    evidence_gaps: list[EvidenceGap] = Field(max_length=80)
    contradictions: list[Contradiction] = Field(max_length=12)
    facts: list[EvidenceFact] = Field(max_length=240)
    sources: list[SourceRecord] = Field(min_length=1, max_length=12)
    source_outcomes: list[SourceOutcome] = Field(max_length=40)
    announcements: list[Announcement] = Field(max_length=3)
    news_status: Literal["found", "none_found"]


class CachedReport(Artifact):
    owner_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime
    report: AcceptedReport
