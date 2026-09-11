"""Offline bounded orchestration, provenance, repair, and retry tests."""

import asyncio
import copy
import hashlib
import json
import math
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from cxplorer.ai.config import AISettings
from cxplorer.ai.providers import Generated, OutputContractDetails, ProviderError, Usage
from cxplorer.ai.tasks import Allowance, BudgetLedger, TaskExecutor, TaskFailure
from cxplorer.insights.cache import ReportCache
from cxplorer.insights.errors import InsightError
from cxplorer.insights.pipeline import InsightsPipeline
from cxplorer.insights.schemas import (
    AudienceTalkPoints,
    CompanyDossier,
    EntityResolution,
    EvidenceBatch,
    EvidenceGap,
    EvidenceQuote,
    FactCandidate,
    GroundedText,
    InsightRequest,
    NewsCandidate,
    NewsDiscovery,
    OpportunityCandidate,
    QualityReview,
    QuotedQuantity,
    ReviewIssue,
    SectionReview,
    SeedInput,
    SourceVerdict,
    SourceVerification,
    StrategyBrief,
)
from cxplorer.insights.sources import SourceDocument, TextSpan
from cxplorer.insights.urls import SourceError, normalize_url
from cxplorer.insights.validation import ArtifactError, check_quotes, check_report, dates_in_text
from tests.insights_fixtures import audience, opportunity, profile

HOME = "https://contoso.com/"
ABOUT = "https://contoso.com/about"
NEWS = "https://contoso.com/news/reviewer-investment"
REPORT_ID = "a" * 32


def settings(**pipeline_changes) -> AISettings:
    configured = AISettings()
    return configured.model_copy(
        update={
            "pipeline": configured.pipeline.model_copy(update=pipeline_changes),
            "vendors": {
                name: vendor.model_copy(update={"max_retries": 0})
                for name, vendor in configured.vendors.items()
            },
        }
    )


def request(*, about=False, audiences=None, seller_context="") -> InsightRequest:
    seeds = [SeedInput(url=HOME, purpose="homepage")]
    if about:
        seeds.append(SeedInput(url=ABOUT, purpose="about"))
    return InsightRequest(
        seeds=seeds,
        audiences=audiences or ["ceo", "cto", "cio", "cfo", "ciso"],
        seller_context=seller_context,
    )


def document(
    url=HOME, *, text=None, published_at=None, metadata=True, links=(), source_id=None
) -> SourceDocument:
    text = text or [
        "Contoso provides business software for invoice reviewers.",
        "Contoso reviewers approve invoice exceptions before an invoice proceeds.",
    ]
    if isinstance(text, str):
        text = [text]
    identifier = source_id or "src_" + hashlib.sha256(url.encode()).hexdigest()[:16]
    spans = [
        TextSpan(f"{identifier}_span_{index}", value, section="Company")
        for index, value in enumerate(text)
    ]
    publication = ()
    if published_at is not None and metadata:
        date_span = TextSpan(
            f"{identifier}_publication",
            "Published: " + published_at.isoformat(),
            section="Publication",
        )
        spans.append(date_span)
        publication = (date_span,)
    content = "\n".join(span.text for span in spans)
    return SourceDocument(
        id=identifier,
        original_url=url,
        url=url,
        title="Contoso official source",
        media_type="text/html",
        text=content,
        spans=tuple(spans),
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        retrieved_at=datetime.now(UTC),
        published_at=published_at if metadata else None,
        links=tuple(links),
        coverage={
            "complete": True,
            "omissions": [],
            "publication_date_conflict": False,
        },
        publication_evidence=publication,
    )


class FakeFetcher:
    def __init__(self, documents=None, errors=None):
        self.documents = {item.url: item for item in (documents or [document()])}
        self.errors = errors or {}
        self.calls = []

    async def fetch(self, url, *, purpose="other", allowed_hosts=None):
        self.calls.append((url, purpose, set(allowed_hosts or ())))
        await asyncio.sleep(0)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.documents:
            raise SourceError("not_found", "Use an accessible official Contoso source.")
        return replace(self.documents[url], original_url=url, purpose=purpose)


@dataclass
class Call:
    task: str
    data: dict
    domains: tuple[str, ...]
    instructions: str
    output_type: type


def rebind(value, fact_id, opportunity_id=None):
    encoded = value.model_dump_json().replace("fact_001", fact_id)
    if opportunity_id:
        encoded = encoded.replace("opp_001", opportunity_id)
    return type(value).model_validate_json(encoded)


def company_fact(data):
    return next(
        (
            fact
            for fact in data["facts"]
            if fact["category"] in {"offering", "company", "product_workflow"}
        ),
        data["facts"][0],
    )


class FakeProvider:
    def __init__(self, configured=None, *, news=(), transform=None):
        self.settings = configured or settings()
        self.news = list(news)
        self.transform = transform
        self.calls = []
        self.active = Counter()
        self.maximum_active = Counter()
        self.pause_task = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.estimates = 0

    def estimate_input_tokens(
        self, task_id, instructions, data, output_type, *, allowed_domains=()
    ):
        self.estimates += 1
        framing = {
            "instructions": instructions,
            "data": data,
            "schema": output_type.model_json_schema(),
            "domains": list(allowed_domains),
        }
        count = math.ceil(len(json.dumps(framing, separators=(",", ":"))) / 4) + 64
        return (
            max(count, self.settings.tasks[task_id].max_input_tokens)
            if task_id == "discover_news"
            else count
        )

    async def generate(self, task_id, instructions, data, output_type, *, allowed_domains=()):
        call = Call(task_id, copy.deepcopy(data), tuple(allowed_domains), instructions, output_type)
        self.calls.append(call)
        self.active[task_id] += 1
        self.maximum_active[task_id] = max(self.maximum_active[task_id], self.active[task_id])
        try:
            if self.pause_task == task_id:
                self.entered.set()
                await self.release.wait()
            await asyncio.sleep(0)
            value = self.default(call)
            if self.transform is not None:
                changed = self.transform(call, value)
                if isinstance(changed, BaseException):
                    raise changed
                if changed is not None:
                    value = changed
            return Generated(
                value,
                Usage(80, 64, 1 if task_id == "discover_news" else 0),
                "opaque-provider-internal",
            )
        finally:
            self.active[task_id] -= 1

    def default(self, call):
        data = call.data
        if call.task == "verify_sources":
            verdicts = []
            for source in data["documents"]:
                identity = next(
                    (span for span in source["spans"] if "Contoso" in span["text"]),
                    source["spans"][0],
                )
                date_span = next(
                    (span for span in source["spans"] if "Published" in span["text"]), None
                )
                published = source["publisher_publication_date"]
                if published is None and date_span is not None:
                    dates = dates_in_text(date_span["text"])
                    published = next(iter(dates)).isoformat() if dates else None
                executive = next(
                    (span for span in source["spans"] if "CEO Alex Morgan said" in span["text"]),
                    None,
                )
                verdicts.append(
                    SourceVerdict(
                        source_id=source["source_id"],
                        relation="related",
                        rationale="Official Contoso material identifies the company.",
                        evidence=[
                            EvidenceQuote(
                                span_id=identity["span_id"], quote=identity["text"][:1000]
                            )
                        ],
                        published_date=published,
                        date_evidence=[
                            EvidenceQuote(span_id=date_span["span_id"], quote=date_span["text"])
                        ]
                        if date_span
                        else [],
                        executive_name="Alex Morgan" if executive else None,
                        executive_role="CEO" if executive else None,
                        executive_evidence=[
                            EvidenceQuote(span_id=executive["span_id"], quote=executive["text"])
                        ]
                        if executive
                        else [],
                    )
                )
            return SourceVerification(
                company_name="Contoso",
                homepage_source_id=data["homepage_source_id"],
                verdicts=verdicts,
            )
        if call.task == "discover_news":
            return NewsDiscovery(announcements=self.news, evidence_gaps=[])
        if call.task == "extract_evidence":
            return EvidenceBatch(
                facts=[
                    FactCandidate(
                        category="offering" if "provides" in span["text"] else "product_workflow",
                        entity="Contoso",
                        statement=span["text"][:700],
                        attribution="company_reported",
                        quantities=[],
                        time_context=None,
                        evidence=[
                            EvidenceQuote(span_id=span["span_id"], quote=span["text"][:1000])
                        ],
                    )
                    for span in data["spans"][:10]
                    if "Contoso" in span["text"]
                ],
                evidence_gaps=[],
            )
        if call.task == "consolidate_evidence":
            fact_id = company_fact(data)["id"]
            return CompanyDossier(
                entity_resolution=EntityResolution(
                    status="matched",
                    company_name=GroundedText(
                        text="Contoso", basis="source_backed", fact_ids=[fact_id]
                    ),
                    included_source_ids=[source["id"] for source in data["sources"]],
                    excluded_sources=[],
                ),
                company_profile=rebind(profile(), fact_id),
                executive_summary=[
                    GroundedText(
                        text="Contoso provides business software.",
                        basis="source_backed",
                        fact_ids=[fact_id],
                    )
                ],
                fact_groups=[],
                contradictions=[],
                evidence_gaps=[],
            )
        if call.task == "synthesize_strategy":
            fact_id = company_fact(data)["id"]
            candidate = rebind(opportunity(), fact_id)
            return StrategyBrief(
                strategic_summary=[candidate.hypothesis],
                opportunities=[
                    OpportunityCandidate.model_validate(candidate.model_dump(exclude={"id"}))
                ],
                evidence_gaps=[],
            )
        if call.task == "generate_talk_points":
            fact_id = company_fact(data)["id"]
            if not data["opportunities"]:
                return AudienceTalkPoints(
                    audience=data["audience"],
                    coverage="insufficient_evidence",
                    opening=GroundedText(
                        text="Contoso provides business software.",
                        basis="source_backed",
                        fact_ids=[fact_id],
                    ),
                    talk_points=[],
                    discovery_questions=[],
                    objections=[],
                    next_step_ask="Request better evidence before discussing a Contoso opportunity.",
                    evidence_gaps=[
                        EvidenceGap(
                            area="opportunities",
                            detail="No grounded opportunity is available for this Contoso audience.",
                        )
                    ],
                )
            output = rebind(
                audience(data["required_count"]), fact_id, data["opportunities"][0]["id"]
            )
            return output.model_copy(
                update={
                    "audience": data["audience"],
                    "talk_points": [
                        point.model_copy(
                            update={
                                "audience_relevance": GroundedText(
                                    text=data["audience_brief"],
                                    basis="analysis",
                                    fact_ids=[fact_id],
                                )
                            }
                        )
                        for point in output.talk_points
                    ],
                }
            )
        if call.task == "review_report":
            return QualityReview(
                decision="accept",
                section_reviews=[
                    SectionReview(section_id=section, decision="accept")
                    for section in data["section_ids"]
                ],
                issues=[],
            )
        raise AssertionError(call.task)


def recent_candidate(url=NEWS, *, published=None, executive=True) -> NewsCandidate:
    return NewsCandidate(
        url=url,
        title="Contoso reviewer investment",
        published_date=(published or (datetime.now(UTC).date() - timedelta(days=10))).isoformat(),
        executive_name="Alex Morgan" if executive else None,
        executive_role="CEO" if executive else None,
        summary="Contoso announced an investment in reviewer workflows.",
    )


def reject_section(value, section, *, code="unsupported_claim", facts=()):
    return QualityReview(
        decision="repair",
        section_reviews=[
            item.model_copy(update={"decision": "repair"}) if item.section_id == section else item
            for item in value.section_reviews
        ],
        issues=[
            ReviewIssue(
                code=code,
                section_id=section,
                fact_ids=list(facts),
                description="This Contoso section needs a supported correction.",
                correction="Use only the supplied Contoso evidence and label proposals as hypotheses.",
            )
        ],
    )


def run_pipeline(provider=None, fetcher=None, selected=None, configured=None):
    configured = configured or settings()
    provider = provider or FakeProvider(configured)
    pipeline = InsightsPipeline(configured, provider, fetcher or FakeFetcher())
    result = asyncio.run(pipeline.run(REPORT_ID, selected or request(), lambda *_: None))
    return pipeline, provider, result


def test_full_pipeline_has_separate_audiences_and_fresh_structured_review():
    pipeline, provider, report = run_pipeline()
    assert report.status == "completed"
    assert [item.audience for item in report.audiences] == ["ceo", "cto", "cio", "cfo", "ciso"]
    assert all(len(item.talk_points) == 3 for item in report.audiences)
    assert [call.task for call in provider.calls] == [
        "verify_sources",
        "discover_news",
        "extract_evidence",
        "consolidate_evidence",
        "synthesize_strategy",
        *["generate_talk_points"] * 5,
        "review_report",
    ]
    reviewer = provider.calls[-1]
    assert reviewer.data["original_excerpts"]
    assert {item["section_id"] for item in reviewer.data["sections"]} == set(
        reviewer.data["section_ids"]
    )
    assert "facts" not in reviewer.data["candidate"]
    assert report.news_status == "none_found"
    assert not pipeline.can_retry(REPORT_ID)
    assert not pipeline._states[REPORT_ID].documents
    assert not pipeline._states[REPORT_ID].artifacts
    assert provider.maximum_active["generate_talk_points"] == 3
    assert "opaque-provider-internal" not in report.model_dump_json()
    assert "seller_context" not in report.model_dump_json()
    check_report(report)


def test_pipeline_logs_paired_task_and_source_steps_without_content(caplog):
    with caplog.at_level("INFO", logger="cxplorer.insights.pipeline"):
        _, provider, _ = run_pipeline()
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "cxplorer.insights.pipeline"
    ]
    task_before = [
        message for message in messages if message.startswith("Insight pipeline task before:")
    ]
    task_after = [
        message for message in messages if message.startswith("Insight pipeline task after:")
    ]
    source_before = [
        message for message in messages if message.startswith("Insight source fetch before:")
    ]
    source_after = [
        message for message in messages if message.startswith("Insight source fetch after:")
    ]
    assert len(task_before) == len(task_after) == len(provider.calls)
    assert all("status=completed" in message for message in task_after)
    assert len(source_before) == len(source_after) == 1
    assert "status=completed" in source_after[0]
    assert HOME not in caplog.text
    assert "invoice reviewers" not in caplog.text


def test_official_news_is_mandatory_and_external_candidates_are_never_fetched():
    recent = datetime.now(UTC).date() - timedelta(days=4)
    news = document(
        NEWS,
        published_at=recent,
        text=['Contoso CEO Alex Morgan said, "We are investing in reviewer workflows."'],
    )
    outside = "https://contoso-publisher.com/contoso-news"
    personal = "https://contoso.com/profile/alex-morgan"
    fetcher = FakeFetcher([document(), news])
    provider = FakeProvider(
        news=[recent_candidate(outside), recent_candidate(personal), recent_candidate()]
    )
    _, provider, report = run_pipeline(provider, fetcher)
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("contoso.com",)
    assert search.data["approved_domains"] == ["contoso.com"]
    assert (
        search.data["published_on_or_after"] == (report.as_of_date - timedelta(days=90)).isoformat()
    )
    assert search.data["max_candidates"] == 6
    assert {url for url, _, _ in fetcher.calls} == {HOME, NEWS}
    assert report.news_status == "found"
    assert report.announcements[0].published_at == recent
    assert report.announcements[0].executive_name == "Alex Morgan"
    assert report.announcements[0].executive_role == "CEO"
    assert any(
        "Alex Morgan" in citation.quote for fact in report.facts for citation in fact.evidence
    )
    assert next(item for item in report.source_outcomes if item.url == outside).status == "rejected"
    assert (
        next(item for item in report.source_outcomes if item.url == personal).status == "rejected"
    )
    assert provider.calls[-1].data["candidate"]["announcements"]


def test_any_inaccessible_supplied_about_stops_before_model_calls():
    provider = FakeProvider()
    fetcher = FakeFetcher(
        errors={ABOUT: SourceError("blocked", "Use an accessible official Contoso About page.")}
    )
    pipeline = InsightsPipeline(settings(), provider, fetcher)
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(about=True), lambda *_: None))
    assert failure.value.code == "invalid_source"
    assert any(
        outcome.url == ABOUT and outcome.status == "rejected"
        for outcome in failure.value.source_outcomes
    )
    assert provider.calls == []
    assert not pipeline.can_retry(REPORT_ID)


@pytest.mark.parametrize(
    "relation", ["unrelated", "uncertain"], ids=["unrelated-about", "ambiguous-about"]
)
def test_same_host_does_not_override_about_company_identity(relation):
    def transform(call, value):
        if call.task == "verify_sources":
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(update={"relation": relation})
                        if verdict.source_id != value.homepage_source_id
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    provider = FakeProvider(transform=transform)
    fetcher = FakeFetcher(
        [
            document(),
            document(ABOUT, text="Contoso is an unrelated legal entity with a separate homepage."),
        ]
    )
    pipeline = InsightsPipeline(settings(), provider, fetcher)
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(about=True), lambda *_: None))
    assert failure.value.code in {"unrelated_source", "uncertain_company"}
    assert [call.task for call in provider.calls] == ["verify_sources"]
    assert not pipeline.can_retry(REPORT_ID)


def test_related_source_can_use_page_identity_outside_its_relation_quote_with_warning():
    about = document(
        ABOUT,
        text=[
            "We build governed invoice review workflows for customers.",
            "Contoso provides business software for invoice reviewers.",
        ],
    )

    def transform(call, value):
        if call.task == "verify_sources":
            source = next(item for item in call.data["documents"] if item["url"] == ABOUT)
            relation_span = source["spans"][0]
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(
                            update={
                                "relation": "related",
                                "evidence": [
                                    EvidenceQuote(
                                        span_id=relation_span["span_id"],
                                        quote=relation_span["text"],
                                    )
                                ],
                            }
                        )
                        if verdict.source_id == source["source_id"]
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    _, provider, report = run_pipeline(
        FakeProvider(transform=transform),
        FakeFetcher([document(), about]),
        selected=request(about=True),
    )
    assert sum(call.task == "verify_sources" for call in provider.calls) == 1
    assert report.status == "completed"
    warning = next(gap for gap in report.evidence_gaps if gap.area == "source_identity")
    assert "selected relation quotation does not name it explicitly" in warning.detail
    assert next(item for item in report.source_outcomes if item.url == ABOUT).status == "accepted"
    check_report(report)


def test_related_same_host_source_without_any_company_identity_still_fails_closed():
    about = replace(
        document(
            ABOUT,
            text="We build governed invoice review workflows for customers.",
        ),
        title="About our services",
    )
    provider = FakeProvider()
    pipeline = InsightsPipeline(
        settings(),
        provider,
        FakeFetcher([document(), about]),
    )
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(about=True), lambda *_: None))
    assert failure.value.code == "entity_mismatch"
    assert sum(call.task == "verify_sources" for call in provider.calls) == 2
    assert not any(call.task == "extract_evidence" for call in provider.calls)


def test_related_source_still_requires_an_exact_supporting_quotation():
    def transform(call, value):
        if call.task == "verify_sources":
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(update={"evidence": []})
                        if verdict.source_id != value.homepage_source_id
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(
        settings(),
        provider,
        FakeFetcher(
            [
                document(),
                document(
                    ABOUT,
                    text="Contoso explains governed invoice review services for customers.",
                ),
            ]
        ),
    )
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(about=True), lambda *_: None))
    assert failure.value.code == "entity_mismatch"
    assert sum(call.task == "verify_sources" for call in provider.calls) == 2
    assert not any(call.task == "extract_evidence" for call in provider.calls)


def test_empty_typed_evidence_stops_without_a_guessed_profile():
    def transform(call, value):
        if call.task == "extract_evidence":
            return EvidenceBatch(
                facts=[],
                evidence_gaps=[
                    EvidenceGap(area="company", detail="No substantive Contoso evidence.")
                ],
            )
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "insufficient_evidence"
    assert not any(call.task == "consolidate_evidence" for call in provider.calls)
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    assert not pipeline.can_retry(REPORT_ID)


def test_dossier_ambiguous_identity_is_not_repaired_into_a_match():
    def transform(call, value):
        if call.task == "consolidate_evidence":
            return value.model_copy(
                update={
                    "entity_resolution": value.entity_resolution.model_copy(
                        update={"status": "ambiguous"}
                    )
                }
            )
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "insufficient_evidence"
    assert sum(call.task == "consolidate_evidence" for call in provider.calls) == 1
    assert not any(call.task == "synthesize_strategy" for call in provider.calls)


def test_dossier_company_name_is_rebound_to_identifying_evidence(caplog):
    text = [
        "Contoso provides business software for invoice reviewers.",
        "The platform routes invoice exceptions to human reviewers.",
    ]

    def transform(call, value):
        if call.task == "extract_evidence":
            span = next(
                span for span in call.data["spans"] if span["text"].startswith("The platform")
            )
            contextual = FactCandidate(
                category="product_workflow",
                entity="Contoso",
                statement=span["text"],
                attribution="company_reported",
                quantities=[],
                time_context=None,
                evidence=[
                    EvidenceQuote(
                        span_id=span["span_id"],
                        quote=span["text"],
                    )
                ],
            )
            return value.model_copy(update={"facts": [*value.facts, contextual]})
        if call.task == "consolidate_evidence":
            contextual_id = next(
                fact["id"]
                for fact in call.data["facts"]
                if all("Contoso" not in citation["quote"] for citation in fact["evidence"])
            )
            return value.model_copy(
                update={
                    "entity_resolution": value.entity_resolution.model_copy(
                        update={
                            "company_name": value.entity_resolution.company_name.model_copy(
                                update={"fact_ids": [contextual_id]}
                            )
                        }
                    )
                }
            )
        return value

    caplog.set_level("INFO", logger="cxplorer.insights.pipeline")
    _, provider, report = run_pipeline(
        FakeProvider(transform=transform),
        FakeFetcher([document(text=text)]),
    )
    company_evidence = " ".join(
        citation.quote
        for fact in report.facts
        if fact.id in report.company_name.fact_ids
        for citation in fact.evidence
    )
    assert "Contoso" in company_evidence
    assert sum(call.task == "consolidate_evidence" for call in provider.calls) == 1
    assert "Insight dossier identity citation normalized" in caplog.text
    check_report(report)


def test_dossier_company_name_still_requires_identifying_evidence():
    text = "The platform routes invoice exceptions to human reviewers."

    def transform(call, value):
        if call.task == "extract_evidence":
            span = call.data["spans"][0]
            return EvidenceBatch(
                facts=[
                    FactCandidate(
                        category="product_workflow",
                        entity="Contoso",
                        statement=span["text"],
                        attribution="company_reported",
                        quantities=[],
                        time_context=None,
                        evidence=[
                            EvidenceQuote(
                                span_id=span["span_id"],
                                quote=span["text"],
                            )
                        ],
                    )
                ],
                evidence_gaps=[],
            )
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(
        settings(),
        provider,
        FakeFetcher([document(text=text)]),
    )
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "entity_mismatch"
    assert sum(call.task == "consolidate_evidence" for call in provider.calls) == 2
    assert not any(call.task == "synthesize_strategy" for call in provider.calls)


@pytest.mark.parametrize(
    "coverage", ["limited", "insufficient_evidence"], ids=["limited", "no-evidence"]
)
def test_role_evidence_limitations_remain_partial_after_bounded_correction(coverage):
    def transform(call, value):
        if call.task == "generate_talk_points" and call.data["audience"] == "ciso":
            return value.model_copy(
                update={
                    "coverage": coverage,
                    "talk_points": value.talk_points[:1] if coverage == "limited" else [],
                    "evidence_gaps": [
                        EvidenceGap(
                            area="security",
                            detail="Public Contoso evidence does not establish a security baseline.",
                        )
                    ],
                }
            )
        return value

    pipeline, provider, report = run_pipeline(FakeProvider(transform=transform))
    assert report.status == "partial"
    assert (
        next(item.status for item in report.audience_coverage if item.audience == "ciso")
        == coverage
    )
    assert sum(call.task == "generate_talk_points" for call in provider.calls) == 6
    assert sum(call.task == "review_report" for call in provider.calls) == 1
    assert not pipeline._states[REPORT_ID].repair_used


def test_shared_opportunities_trigger_one_role_coverage_improvement():
    def transform(call, value):
        if (
            call.task == "generate_talk_points"
            and call.data["audience"] == "cfo"
            and "repair_feedback" not in call.data
        ):
            return value.model_copy(
                update={
                    "coverage": "limited",
                    "talk_points": value.talk_points[:1],
                    "evidence_gaps": [
                        EvidenceGap(
                            area="finance",
                            detail="The first response used only one supported financial lens.",
                        )
                    ],
                }
            )
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    cfo = next(item for item in report.audiences if item.audience == "cfo")
    assert cfo.coverage == "complete"
    assert len(cfo.talk_points) == 3
    calls = [
        call
        for call in provider.calls
        if call.task == "generate_talk_points" and call.data["audience"] == "cfo"
    ]
    assert len(calls) == 2
    assert calls[0].data["delivery_policy"] == {
        "mode": "opportunity_driven",
        "accepted_opportunities_support_discovery_hypotheses": True,
        "external_industry_stories_allowed": False,
        "limited_coverage_requires_correction_attempt": True,
    }
    assert "repair_feedback" in calls[1].data
    assert calls[1].data["coverage_correction"]["attempt"] == 1


def test_audience_coverage_retry_preserves_core_review_repair():
    reviews = 0

    def transform(call, value):
        nonlocal reviews
        if call.task == "generate_talk_points" and call.data["audience"] == "ciso":
            return value.model_copy(
                update={
                    "coverage": "limited",
                    "talk_points": value.talk_points[:1],
                    "evidence_gaps": [
                        EvidenceGap(
                            area="security",
                            detail="Only one distinct security lens is supported.",
                        )
                    ],
                }
            )
        if call.task == "review_report":
            reviews += 1
            if reviews == 1:
                return reject_section(
                    value,
                    "section_company",
                    facts=[call.data["evidence"][0]["id"]],
                )
        return value

    pipeline, provider, report = run_pipeline(FakeProvider(transform=transform))
    counts = Counter(call.task for call in provider.calls)
    assert counts["consolidate_evidence"] == 2
    assert counts["synthesize_strategy"] == 2
    assert counts["generate_talk_points"] == 12
    assert counts["review_report"] == 2
    assert report.company_name.text == "Contoso"
    assert report.status == "partial"
    assert pipeline._states[REPORT_ID].repair_used


def test_vague_core_review_is_corrected_locally_and_logged(caplog):
    reviews = 0

    def transform(call, value):
        nonlocal reviews
        if call.task == "review_report":
            reviews += 1
            if reviews == 1:
                return reject_section(
                    value,
                    "section_company",
                    code="insufficient_evidence",
                    facts=[call.data["evidence"][0]["id"]],
                )
        return value

    caplog.set_level("INFO", logger="cxplorer.insights.pipeline")
    pipeline, provider, report = run_pipeline(FakeProvider(transform=transform))
    assert report.status == "completed"
    assert sum(call.task == "review_report" for call in provider.calls) == 2
    assert sum(call.task == "consolidate_evidence" for call in provider.calls) == 1
    assert not pipeline._states[REPORT_ID].repair_used
    assert "Insight quality review contract correction" in caplog.text
    assert "Insight quality review result" in caplog.text
    assert "core=company=accept,evidence=accept,sources=accept,summary=accept" in caplog.text


@pytest.mark.parametrize(
    "case",
    ["old", "future", "undated", "updated"],
    ids=["old", "future", "undated", "updated-only"],
)
def test_publication_dates_are_independently_verified_not_search_or_retrieval_dates(case):
    today = datetime.now(UTC).date()
    publication = today - timedelta(days=91) if case == "old" else today + timedelta(days=1)
    if case in {"undated", "updated"}:
        publication = None
    text = ["Contoso announced a reviewer-workflow investment."]
    if case == "updated":
        text.append("Last updated: " + (today - timedelta(days=1)).isoformat())
    fetcher = FakeFetcher([document(), document(NEWS, text=text, published_at=publication)])
    _, _, report = run_pipeline(FakeProvider(news=[recent_candidate()]), fetcher)
    assert report.news_status == "none_found"
    assert not report.announcements
    assert (
        next(outcome for outcome in report.source_outcomes if outcome.url == NEWS).status
        == "rejected"
    )
    assert any(gap.area == "official_news" for gap in report.evidence_gaps)


def test_exact_quoted_publisher_date_is_accepted_without_metadata_or_executive_guess():
    publication = datetime.now(UTC).date() - timedelta(days=5)
    printed = f"{publication:%B} {publication.day}, {publication.year}"
    fetcher = FakeFetcher(
        [
            document(),
            document(
                NEWS,
                text=["Contoso announced reviewer-workflow investment.", "Published " + printed],
                metadata=False,
            ),
        ]
    )
    _, _, report = run_pipeline(FakeProvider(news=[recent_candidate()]), fetcher)
    assert report.announcements[0].published_at == publication
    assert report.announcements[0].executive_name is None
    assert report.announcements[0].executive_role is None
    assert any(printed in citation.quote for fact in report.facts for citation in fact.evidence)


def test_fabricated_executive_attribution_is_not_published():
    publication = datetime.now(UTC).date() - timedelta(days=2)

    def transform(call, value):
        if call.task == "verify_sources" and call.data["news_source_ids"]:
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(
                            update={
                                "executive_name": "Alex Morgan",
                                "executive_role": "CEO",
                                "executive_evidence": [],
                            }
                        )
                        if verdict.source_id != value.homepage_source_id
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    provider = FakeProvider(news=[recent_candidate()], transform=transform)
    pipeline = InsightsPipeline(
        settings(),
        provider,
        FakeFetcher(
            [
                document(),
                document(
                    NEWS, text="Contoso announced reviewer investment.", published_at=publication
                ),
            ]
        ),
    )
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "quote_mismatch"
    assert not any(call.task == "extract_evidence" for call in provider.calls)
    assert not pipeline.can_retry(REPORT_ID)


@pytest.mark.parametrize(
    "invalid", ["source_id", "quote"], ids=["unknown-source-id", "quote-mismatch"]
)
def test_source_verification_unknown_ids_and_mismatched_quotes_fail_closed(invalid):
    def transform(call, value):
        if call.task == "verify_sources":
            verdict = value.verdicts[0]
            update = (
                {"source_id": "src_unknown"}
                if invalid == "source_id"
                else {
                    "evidence": [
                        EvidenceQuote(
                            span_id=verdict.evidence[0].span_id,
                            quote="Contoso makes an unsupported claim.",
                        )
                    ]
                }
            )
            return value.model_copy(update={"verdicts": [verdict.model_copy(update=update)]})
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code in {"unknown_reference", "quote_mismatch"}
    assert [call.task for call in provider.calls] == ["verify_sources", "verify_sources"]
    assert "repair_feedback" in provider.calls[-1].data


def test_fuzzy_extraction_quote_is_canonicalized_with_visible_warning():
    source_text = "Contoso provides business software for invoice reviewers."

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            fuzzy = fact.evidence[0].model_copy(
                update={"quote": fact.evidence[0].quote.replace("software", "softwares")}
            )
            return value.model_copy(
                update={"facts": [fact.model_copy(update={"evidence": [fuzzy]})]}
            )
        return value

    _, provider, report = run_pipeline(
        FakeProvider(transform=transform),
        FakeFetcher([document(text=source_text)]),
    )
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    citation = next(
        citation
        for fact in report.facts
        for citation in fact.evidence
        if "business software" in citation.quote
    )
    assert citation.quote in source_text
    assert "softwares" not in citation.quote
    warning = next(gap for gap in report.evidence_gaps if gap.area == "Quotation matching warnings")
    assert "replaced them with the exact source substring" in warning.detail
    check_report(report)


def test_fuzzy_quote_matching_rejects_meaning_sensitive_changes():
    span = TextSpan(
        "span_001",
        "Contoso increased reviewer capacity during 2025.",
        section="Company",
    )
    quote = EvidenceQuote(
        span_id=span.id,
        quote="Contoso decreased reviewer capacity during 2025.",
    )
    with pytest.raises(ArtifactError) as failure:
        check_quotes([quote], {span.id: span})
    assert failure.value.code == "quote_mismatch"


def test_fuzzy_quote_matching_rejects_changed_currency_symbols():
    span = TextSpan(
        "span_001",
        "Contoso reported $500 million in revenue during 2025.",
        section="Company",
    )
    quote = EvidenceQuote(
        span_id=span.id,
        quote="Contoso reported €500 million in revenue during 2025.",
    )
    with pytest.raises(ArtifactError) as failure:
        check_quotes([quote], {span.id: span})
    assert failure.value.code == "quote_mismatch"


def test_local_validation_retry_preserves_final_review_repair():
    def transform(call, value):
        if call.task == "extract_evidence" and "repair_feedback" not in call.data:
            first = value.facts[0]
            return value.model_copy(
                update={
                    "facts": [
                        first.model_copy(
                            update={
                                "evidence": [
                                    EvidenceQuote(
                                        span_id="span_unknown", quote=first.evidence[0].quote
                                    )
                                ]
                            }
                        )
                    ]
                }
            )
        if call.task == "review_report":
            return reject_section(value, "section_audience_ceo", code="generic_talk_point")
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    counts = Counter(call.task for call in provider.calls)
    assert counts["extract_evidence"] == 2
    assert counts["generate_talk_points"] == 6
    assert counts["review_report"] == 2
    assert report.status == "partial"
    assert (
        next(item.status for item in report.audience_coverage if item.audience == "ceo")
        == "unavailable"
    )
    assert all(audience.audience != "ceo" for audience in report.audiences)


def test_local_validation_retry_leaves_core_review_repair_available(caplog):
    reviews = 0

    def transform(call, value):
        nonlocal reviews
        if (
            call.task == "extract_evidence"
            and "validation_correction" not in call.data
            and "repair_feedback" not in call.data
        ):
            first = value.facts[0]
            return value.model_copy(
                update={
                    "facts": [
                        first.model_copy(
                            update={
                                "evidence": [
                                    EvidenceQuote(
                                        span_id="span_unknown", quote=first.evidence[0].quote
                                    )
                                ]
                            }
                        )
                    ]
                }
            )
        if call.task == "review_report":
            reviews += 1
            if reviews == 1:
                fact_id = call.data["evidence"][0]["id"]
                repair_sections = {"section_company", "section_evidence"}
                return QualityReview(
                    decision="repair",
                    section_reviews=[
                        item.model_copy(update={"decision": "repair"})
                        if item.section_id in repair_sections
                        else item
                        for item in value.section_reviews
                    ],
                    issues=[
                        ReviewIssue(
                            code="unsupported_claim",
                            section_id=section,
                            fact_ids=[fact_id],
                            description="This Contoso section needs a supported correction.",
                            correction="Use only the supplied accepted Contoso evidence.",
                        )
                        for section in sorted(repair_sections)
                    ],
                )
        return value

    caplog.set_level("INFO", logger="cxplorer.insights.pipeline")
    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    counts = Counter(call.task for call in provider.calls)
    assert counts["extract_evidence"] == 3
    assert counts["consolidate_evidence"] == 2
    assert counts["synthesize_strategy"] == 2
    assert counts["generate_talk_points"] == 10
    assert counts["review_report"] == 2
    assert report.status == "completed"
    assert "Insight pipeline validation correction before" in caplog.text
    assert "available=true targets=dossier,extract_chunk_" in caplog.text
    assert "origin=quality_review" in caplog.text


def test_strategy_review_repair_invalidates_all_dependent_audiences_only():
    reviews = 0

    def transform(call, value):
        nonlocal reviews
        if call.task == "synthesize_strategy" and "repair_feedback" in call.data:
            return value.model_copy(
                update={
                    "opportunities": [
                        item.model_copy(update={"title": "Refined Contoso review assistance"})
                        for item in value.opportunities
                    ]
                }
            )
        if call.task == "review_report":
            reviews += 1
            if reviews == 1:
                section = next(
                    section
                    for section in call.data["section_ids"]
                    if section.startswith("section_opp_")
                )
                return reject_section(value, section, code="missing_prerequisite")
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    counts = Counter(call.task for call in provider.calls)
    assert counts["extract_evidence"] == 1
    assert counts["consolidate_evidence"] == 1
    assert counts["synthesize_strategy"] == 2
    assert counts["generate_talk_points"] == 10
    assert counts["review_report"] == 2
    initial = next(call for call in provider.calls if call.task == "generate_talk_points").data[
        "opportunities"
    ][0]["id"]
    assert report.opportunities[0].id != initial
    assert all(
        point.opportunity_id == report.opportunities[0].id
        for audience in report.audiences
        for point in audience.talk_points
    )
    assert report.status == "completed"


def test_evidence_repair_invalidates_the_dossier_strategy_and_roles():
    reviews = 0
    original_fact = None

    def transform(call, value):
        nonlocal reviews, original_fact
        if call.task == "extract_evidence" and "repair_feedback" in call.data:
            return value.model_copy(
                update={
                    "facts": [
                        fact.model_copy(
                            update={"statement": fact.statement + " This is company-reported."}
                        )
                        for fact in value.facts
                    ]
                }
            )
        if call.task == "review_report":
            reviews += 1
            if reviews == 1:
                original_fact = call.data["evidence"][0]["id"]
                return reject_section(value, "section_evidence", facts=[original_fact])
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    counts = Counter(call.task for call in provider.calls)
    assert counts["extract_evidence"] == 2
    assert counts["consolidate_evidence"] == 2
    assert counts["synthesize_strategy"] == 2
    assert counts["generate_talk_points"] == 10
    assert counts["review_report"] == 2
    assert original_fact not in {fact.id for fact in report.facts}
    assert report.status == "completed"


def test_a_reviewed_core_survives_when_repair_dependency_calls_do_not_fit():
    configured = settings(max_model_calls=16)

    def transform(call, value):
        if call.task == "review_report":
            section = next(
                section
                for section in call.data["section_ids"]
                if section.startswith("section_opp_")
            )
            return reject_section(value, section)
        return value

    pipeline, provider, report = run_pipeline(
        FakeProvider(configured, transform=transform), configured=configured
    )
    assert report.status == "partial"
    assert report.company_name.text == "Contoso"
    assert not report.opportunities
    assert not report.audiences
    assert all(item.status == "unavailable" for item in report.audience_coverage)
    assert len(provider.calls) <= 16
    assert not pipeline.can_retry(REPORT_ID)
    check_report(report)


def test_temporary_audience_failure_retries_only_that_role_and_fresh_review():
    failures = 0

    def transform(call, value):
        nonlocal failures
        if call.task == "generate_talk_points" and call.data["audience"] == "cfo" and failures == 0:
            failures += 1
            return ProviderError(
                "provider_unavailable", "Contoso research is temporarily unavailable.", True
            )
        return value

    async def scenario():
        provider = FakeProvider(transform=transform)
        fetcher = FakeFetcher()
        pipeline = InsightsPipeline(settings(), provider, fetcher)
        first = await pipeline.run(REPORT_ID, request(), lambda *_: None)
        assert first.status == "partial"
        assert pipeline.can_retry(REPORT_ID)
        cache = ReportCache("contoso-test-session-secret-with-at-least-32-characters")
        first_cached = cache.encode(first, "b" * 64, "c" * 64)
        spent = pipeline._states[REPORT_ID].executor.ledger.spent
        with pytest.raises(InsightError, match="same inputs"):
            await pipeline.run(
                REPORT_ID,
                request(seller_context="A changed Contoso meeting objective."),
                lambda *_: None,
            )
        second = await pipeline.run(REPORT_ID, request(), lambda *_: None)
        assert second.status == "completed"
        assert second.generated_at == first.generated_at
        second_cached = cache.encode(second, "b" * 64, "c" * 64)
        assert second_cached["generated_at"] == first_cached["generated_at"]
        assert second_cached["expires_at"] == first_cached["expires_at"]
        counts = Counter(call.task for call in provider.calls)
        assert counts["verify_sources"] == 1
        assert counts["discover_news"] == 1
        assert counts["extract_evidence"] == 1
        assert counts["consolidate_evidence"] == 1
        assert counts["synthesize_strategy"] == 1
        assert counts["generate_talk_points"] == 6
        assert counts["review_report"] == 2
        assert len(fetcher.calls) == 1
        assert pipeline._states[REPORT_ID].executor.ledger.spent.calls == spent.calls + 2
        assert not any("CFO could not" in gap.detail for gap in second.evidence_gaps)
        assert not pipeline.can_retry(REPORT_ID)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("failed_roles", "max_calls", "retryable"),
    [
        ({"cfo"}, 12, False),
        ({"cfo"}, 13, True),
        ({"cfo", "ciso"}, 13, False),
        ({"cfo", "ciso"}, 14, True),
    ],
)
def test_retry_requires_budget_for_all_pending_roles_and_a_fresh_review(
    failed_roles, max_calls, retryable
):
    remaining_failures = set(failed_roles)

    def transform(call, value):
        if call.task == "generate_talk_points" and call.data["audience"] in remaining_failures:
            remaining_failures.remove(call.data["audience"])
            return ProviderError(
                "provider_unavailable", "Contoso research is temporarily unavailable.", True
            )
        return value

    async def scenario():
        configured = settings(max_model_calls=max_calls, max_repair_rounds=0)
        provider = FakeProvider(configured, transform=transform)
        pipeline = InsightsPipeline(configured, provider, FakeFetcher())
        first = await pipeline.run(REPORT_ID, request(), lambda *_: None)
        assert first.status == "partial"
        assert len(provider.calls) == 11
        assert pipeline.can_retry(REPORT_ID) is retryable
        state = pipeline._states[REPORT_ID]
        spent = state.executor.ledger.spent
        protected = dict(state.executor.ledger.protected)
        assert pipeline.can_retry(REPORT_ID) is retryable
        assert state.executor.ledger.spent == spent
        assert state.executor.ledger.protected == protected
        if retryable:
            second = await pipeline.run(REPORT_ID, request(), lambda *_: None)
            assert second.status == "completed"
            assert len(provider.calls) == 11 + len(failed_roles) + 1
            assert second.generated_at == first.generated_at
        else:
            with pytest.raises(InsightError) as failure:
                await pipeline.run(REPORT_ID, request(), lambda *_: None)
            assert failure.value.code == "not_retryable"
            assert len(provider.calls) == 11
            assert state.executor.ledger.spent == spent

    asyncio.run(scenario())


def test_temporary_chunk_failure_preserves_independently_accepted_chunks():
    failed_once = False

    def transform(call, value):
        nonlocal failed_once
        if (
            call.task == "extract_evidence"
            and call.data["source"]["url"] == ABOUT
            and not failed_once
        ):
            failed_once = True
            return ProviderError(
                "provider_unavailable", "Contoso research is temporarily unavailable.", True
            )
        return value

    async def scenario():
        provider = FakeProvider(transform=transform)
        fetcher = FakeFetcher(
            [
                document(),
                document(
                    ABOUT, text="Contoso provides governed review workflows for business customers."
                ),
            ]
        )
        pipeline = InsightsPipeline(settings(), provider, fetcher)
        with pytest.raises(InsightError):
            await pipeline.run(REPORT_ID, request(about=True), lambda *_: None)
        assert pipeline.can_retry(REPORT_ID)
        assert any(key.startswith("extract_") for key in pipeline._states[REPORT_ID].artifacts)
        report = await pipeline.run(REPORT_ID, request(about=True), lambda *_: None)
        assert report.status == "completed"
        assert len(fetcher.calls) == 2
        extracts = [call for call in provider.calls if call.task == "extract_evidence"]
        assert sum(call.data["source"]["url"] == HOME for call in extracts) == 1
        assert sum(call.data["source"]["url"] == ABOUT for call in extracts) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "code",
    ["refusal", "incomplete_output"],
    ids=["refusal", "truncation"],
)
def test_nonrecoverable_provider_format_failures_do_not_start_a_correction(code):
    def transform(call, value):
        if call.task == "extract_evidence":
            return ProviderError(code, "The structured Contoso task failed.", usage=Usage(91, 203))
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == code
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    assert not pipeline.can_retry(REPORT_ID)
    assert pipeline._states[REPORT_ID].executor.ledger.spent.output_tokens == 2 * 64 + 203


def test_persistent_invalid_output_gets_only_one_local_contract_correction(caplog):
    def transform(call, value):
        if call.task == "extract_evidence":
            return ProviderError(
                "invalid_output",
                "The structured Contoso task failed.",
                usage=Usage(91, 203),
                contract_details=OutputContractDetails(
                    stage="model_validation",
                    error_count=1,
                    locations=("facts.0.statement",),
                    error_types=("string_too_long",),
                ),
            )
        return value

    caplog.set_level("INFO", logger="cxplorer.insights.pipeline")
    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "invalid_output"
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 2
    assert not pipeline.can_retry(REPORT_ID)
    assert pipeline._states[REPORT_ID].repair_used is False
    assert pipeline._states[REPORT_ID].executor.ledger.spent.output_tokens == 2 * 64 + 2 * 203
    assert "Insight pipeline output contract correction before" in caplog.text
    assert "status=failed code=invalid_output stage=model_validation" in caplog.text


def test_strategy_invalid_output_is_corrected_without_using_artifact_repair(caplog):
    attempts = 0

    def transform(call, value):
        nonlocal attempts
        if call.task == "synthesize_strategy":
            attempts += 1
            if attempts == 1:
                return ProviderError(
                    "invalid_output",
                    "The structured Contoso strategy failed.",
                    usage=Usage(80, 64),
                    contract_details=OutputContractDetails(
                        stage="model_validation",
                        error_count=2,
                        locations=(
                            "opportunities.0.priority",
                            "opportunities.0.success_criteria",
                        ),
                        error_types=("less_than_equal", "too_long"),
                    ),
                )
        return value

    caplog.set_level("INFO", logger="cxplorer.insights.pipeline")
    pipeline, provider, report = run_pipeline(FakeProvider(transform=transform))
    strategy_calls = [call for call in provider.calls if call.task == "synthesize_strategy"]
    assert len(strategy_calls) == 2
    assert strategy_calls[1].data["output_contract_correction"] == {"attempt": 1}
    assert strategy_calls[1].data["repair_feedback"][-1]["contract_stage"] == "model_validation"
    assert report.status == "completed"
    assert pipeline._states[REPORT_ID].repair_used is False
    assert "locations=opportunities.0.priority,opportunities.0.success_criteria" in caplog.text
    assert "Insight pipeline output contract correction after" in caplog.text


def test_search_failure_is_explicit_and_never_becomes_none_found():
    def transform(call, value):
        if call.task == "discover_news":
            return ProviderError(
                "provider_unavailable", "Official company search is unavailable.", True
            )
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "provider_unavailable"
    assert [call.task for call in provider.calls] == ["verify_sources", "discover_news"]
    assert pipeline.can_retry(REPORT_ID)
    pipeline.forget(REPORT_ID)
    assert not pipeline.can_retry(REPORT_ID)


def test_search_scope_violation_is_excluded_and_generation_continues_with_a_gap():
    def transform(call, value):
        if call.task == "discover_news":
            return ProviderError(
                "search_scope_violation",
                "The AI search returned a source outside the approved official hosts.",
                usage=Usage(80, 64, 1),
            )
        return value

    pipeline, provider, report = run_pipeline(FakeProvider(transform=transform))
    assert report.status == "completed"
    assert report.news_status == "none_found"
    assert provider.calls[-1].task == "review_report"
    warning = next(
        gap
        for gap in report.evidence_gaps
        if gap.area == "official_news" and "outside" in gap.detail
    )
    assert "complete search result was excluded" in warning.detail
    assert pipeline._states[REPORT_ID].executor.ledger.spent.calls == len(provider.calls)


def test_many_source_chunks_are_ranked_before_calls_and_preserve_downstream_budget():
    configured = settings(max_model_calls=18)
    paragraphs = [
        f"Contoso provides business software in documented workflow {index}. "
        + "Reviewers approve invoice exceptions before processing. " * 24
        for index in range(90)
    ]
    fetcher = FakeFetcher([document(text=paragraphs)])
    pipeline, provider, report = run_pipeline(
        FakeProvider(configured), fetcher, configured=configured
    )
    extracts = [call for call in provider.calls if call.task == "extract_evidence"]
    assert 1 < len(extracts) < 12
    assert len(provider.calls) <= 18
    assert sum(call.task == "generate_talk_points" for call in provider.calls) == 5
    assert provider.calls[-1].task == "review_report"
    assert any("chunks" in (source.coverage_note or "") for source in report.sources)
    assert any(gap.area == "source_coverage" for gap in report.evidence_gaps)
    assert pipeline._states[REPORT_ID].executor.ledger.spent.calls == len(provider.calls)


def test_minimum_pipeline_budget_fails_before_any_model_attempt():
    configured = settings(max_model_calls=1)
    provider = FakeProvider(configured)
    pipeline = InsightsPipeline(configured, provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "budget_exhausted"
    assert provider.calls == []
    assert not pipeline.can_retry(REPORT_ID)


def test_parallel_reservations_include_in_flight_and_protected_review():
    async def scenario():
        configured = settings()
        provider = FakeProvider(configured)
        ledger = BudgetLedger(Allowance(2, 60_000, 20_000, 0))
        executor = TaskExecutor(
            provider,
            configured.tasks,
            ledger,
            retry_limits={"azure_openai": 0, "openai": 0},
            vendor_semaphores={
                "azure_openai": asyncio.Semaphore(2),
                "openai": asyncio.Semaphore(2),
            },
        )
        executor.deadline = time.monotonic() + 30
        executor.protect({"review": "review_report"})
        results = await asyncio.gather(
            executor.execute(
                "first", "extract_evidence", "Contoso evidence.", {"spans": []}, EvidenceBatch
            ),
            executor.execute(
                "second", "extract_evidence", "Contoso evidence.", {"spans": []}, EvidenceBatch
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, EvidenceBatch) for result in results) == 1
        assert sum(isinstance(result, TaskFailure) for result in results) == 1
        assert len(provider.calls) == 1
        assert ledger.spent.calls == 1
        assert ledger.spent.output_tokens == 64
        assert not ledger.in_flight
        assert ledger.protected["review"].input_tokens == 32_000
        assert ledger.protected["review"].calls == 1

    asyncio.run(scenario())


class ExecutorProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def estimate_input_tokens(self, *_args, **_kwargs):
        return 10

    async def generate(self, *_args, **_kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def executor_for(provider, *, retries=2, calls=10, deadline=30):
    configured = settings()
    task = configured.tasks["extract_evidence"].model_copy(
        update={"max_input_tokens": 100, "max_output_tokens": 100, "timeout_seconds": 20.0}
    )
    ledger = BudgetLedger(Allowance(calls, 1000, 1000, 0))
    executor = TaskExecutor(
        provider,
        {"extract_evidence": task},
        ledger,
        retry_limits={"azure_openai": retries},
        vendor_semaphores={"azure_openai": asyncio.Semaphore(2)},
    )
    executor.deadline = time.monotonic() + deadline
    return executor


def test_finite_transient_retries_charge_lost_responses_and_known_usage_once(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("cxplorer.ai.tasks.asyncio.sleep", sleep)
    provider = ExecutorProvider(
        [
            ProviderError("timeout", "Contoso request timed out.", True, 0.0),
            ProviderError(
                "rate_limited", "Contoso request is rate limited.", True, 1.5, usage=Usage(2, 3)
            ),
            Generated(EvidenceBatch(facts=[], evidence_gaps=[]), Usage(4, 5)),
        ]
    )
    executor = executor_for(provider)
    result = asyncio.run(
        executor.execute("chunk", "extract_evidence", "Contoso", {}, EvidenceBatch)
    )
    assert isinstance(result, EvidenceBatch)
    assert provider.calls == 3
    assert delays == [0.0, 1.5]
    assert executor.ledger.spent == Allowance(3, 16, 108, 0)
    assert not executor.ledger.in_flight


@pytest.mark.parametrize(
    "mode",
    ["attempts", "budget", "deadline", "authentication"],
    ids=["retry-ceiling", "call-budget", "retry-after-deadline", "no-auth-retry"],
)
def test_executor_retry_bounds_do_not_reset_or_bypass_nontransient_errors(mode, monkeypatch):
    async def sleep(_delay):
        return None

    monkeypatch.setattr("cxplorer.ai.tasks.asyncio.sleep", sleep)
    code = "authentication_failed" if mode == "authentication" else "provider_unavailable"
    retry_after = 100.0 if mode == "deadline" else 0.0
    provider = ExecutorProvider(
        [
            ProviderError(code, "The Contoso task is unavailable.", True, retry_after)
            for _ in range(5)
        ]
    )
    executor = executor_for(provider, calls=1 if mode == "budget" else 10)
    with pytest.raises(TaskFailure):
        asyncio.run(executor.execute("chunk", "extract_evidence", "Contoso", {}, EvidenceBatch))
    assert provider.calls == (3 if mode == "attempts" else 1)
    assert executor.ledger.spent.calls == provider.calls
    assert not executor.ledger.in_flight


def test_cancellation_charges_inflight_attempt_and_cleans_source_checkpoints():
    async def scenario():
        provider = FakeProvider()
        provider.pause_task = "extract_evidence"
        pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
        running = asyncio.create_task(pipeline.run(REPORT_ID, request(), lambda *_: None))
        await asyncio.wait_for(provider.entered.wait(), 2)
        state = pipeline._states[REPORT_ID]
        assert state.executor.ledger.in_flight
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not state.executor.ledger.in_flight
        assert state.executor.ledger.spent.calls == 3
        assert state.executor.ledger.spent.output_tokens == 2 * 64 + 4000
        assert not state.documents
        assert not state.artifacts
        assert not pipeline.can_retry(REPORT_ID)
        pipeline.forget(REPORT_ID)
        assert not pipeline._states
        await pipeline.close()

    asyncio.run(scenario())


def test_forget_cancels_active_generation_and_cannot_resurrect_state():
    async def scenario():
        provider = FakeProvider()
        provider.pause_task = "extract_evidence"
        pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
        running = asyncio.create_task(pipeline.run(REPORT_ID, request(), lambda *_: None))
        await asyncio.wait_for(provider.entered.wait(), 2)
        pipeline.forget(REPORT_ID)
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not pipeline._states
        assert not pipeline.can_retry(REPORT_ID)
        await pipeline.close()

    asyncio.run(scenario())


def test_canonical_content_is_deduplicated_without_omitting_seed_preflight():
    original = document()
    duplicate = replace(original, original_url=ABOUT, url=ABOUT)
    fetcher = FakeFetcher([original, duplicate])
    _, provider, report = run_pipeline(fetcher=fetcher, selected=request(about=True))
    assert len(fetcher.calls) == 2
    assert len(report.sources) == 1
    assert len(provider.calls[0].data["source_ids"]) == 1
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    assert {outcome.url for outcome in report.source_outcomes} >= {HOME, ABOUT}


def test_search_uses_verified_final_hosts_not_redirecting_seed_hosts():
    redirected = document("https://www.contoso.com/")
    about = document(
        "https://www.contoso.com/about", text="Contoso provides governed invoice review workflows."
    )
    fetcher = FakeFetcher([redirected, about])
    fetcher.documents[HOME] = redirected
    selected = InsightRequest(
        seeds=[SeedInput(url=HOME, purpose="homepage"), SeedInput(url=about.url, purpose="about")],
        audiences=["ceo"],
    )
    _, provider, _ = run_pipeline(fetcher=fetcher, selected=selected)
    assert fetcher.calls[0][2] == {"contoso.com", "www.contoso.com"}
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("www.contoso.com",)


def test_verified_company_subdomains_remain_allowed_when_search_results_are_excluded():
    homepage = "https://www.contoso.com/"
    careers = "https://careers.contoso.com/jobs"
    newsroom = "https://news.contoso.com/reviewer-investment"
    publication = datetime.now(UTC).date() - timedelta(days=4)

    def transform(call, value):
        if call.task == "discover_news":
            return ProviderError(
                "search_scope_violation",
                "The AI search returned a source outside the approved official hosts.",
                usage=Usage(80, 64, 1),
            )
        return value

    selected = InsightRequest(
        seeds=[
            SeedInput(url=homepage, purpose="homepage"),
            SeedInput(url=careers, purpose="careers"),
            SeedInput(url=newsroom, purpose="news"),
        ],
        audiences=["ceo"],
    )
    fetcher = FakeFetcher(
        [
            document(homepage, text="Contoso provides governed business software."),
            document(careers, text="Contoso careers support its reviewer engineering teams."),
            document(
                newsroom,
                published_at=publication,
                text=['Contoso CEO Alex Morgan said, "We are investing in reviewer workflows."'],
            ),
        ]
    )
    _, provider, report = run_pipeline(
        FakeProvider(transform=transform),
        fetcher,
        selected,
    )

    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == (
        "careers.contoso.com",
        "news.contoso.com",
        "www.contoso.com",
    )
    assert report.status == "completed"
    assert report.news_status == "found"
    assert report.announcements[0].published_at == publication
    assert any(
        gap.area == "official_news" and "complete search result was excluded" in gap.detail
        for gap in report.evidence_gaps
    )


def test_tenant_seed_never_authorizes_its_parent_or_unverified_subdomains():
    tenant = normalize_url("https://contoso.github.io")
    parent = "https://github.io/news/contoso"
    other = "https://news.contoso.github.io/announcement"
    fetcher = FakeFetcher([document(tenant)])
    provider = FakeProvider(news=[recent_candidate(parent), recent_candidate(other)])
    selected = InsightRequest(seeds=[SeedInput(url=tenant, purpose="homepage")], audiences=["ceo"])
    _, provider, report = run_pipeline(provider, fetcher, selected)
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("contoso.github.io",)
    assert {url for url, _, _ in fetcher.calls} == {tenant}
    assert report.news_status == "none_found"


def test_bounded_one_hop_discovery_verifies_linked_company_identity():
    third_party = "https://contoso-publisher.com/about"
    private = "https://contoso.com/login/products"

    def transform(call, value):
        if call.task == "verify_sources" and len(call.data["source_ids"]) > 1:
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(update={"relation": "unrelated"})
                        if verdict.source_id != value.homepage_source_id
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    fetcher = FakeFetcher(
        [
            document(links=[ABOUT, third_party, private]),
            document(
                ABOUT,
                text="Contoso is a different legal entity for this linked-source test.",
                links=["https://contoso.com/products/deeper"],
            ),
        ]
    )
    _, provider, report = run_pipeline(FakeProvider(transform=transform), fetcher)
    assert [url for url, _, _ in fetcher.calls] == [HOME, ABOUT]
    assert [call.task for call in provider.calls[:3]] == [
        "verify_sources",
        "discover_news",
        "verify_sources",
    ]
    assert next(item for item in report.source_outcomes if item.url == ABOUT).status == "rejected"
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1


def test_config_changes_disable_same_job_retry_without_resetting_usage():
    def transform(call, value):
        if call.task == "discover_news":
            return ProviderError("provider_unavailable", "Official search is unavailable.", True)
        return value

    configured = settings()
    provider = FakeProvider(configured, transform=transform)
    pipeline = InsightsPipeline(configured, provider, FakeFetcher())
    with pytest.raises(InsightError):
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert pipeline.can_retry(REPORT_ID)
    spent = pipeline._states[REPORT_ID].executor.ledger.spent
    configured.tasks["extract_evidence"] = configured.tasks["extract_evidence"].model_copy(
        update={"model": "contoso-approved-deployment"}
    )
    assert not pipeline.can_retry(REPORT_ID)
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "retry_input_changed"
    assert pipeline._states[REPORT_ID].executor.ledger.spent == spent
    pipeline.forget(REPORT_ID)


def test_no_user_identity_or_unsupplied_seller_portfolio_is_sent_to_models():
    supplied = "Contoso meeting objective: assess reviewer workflow needs, without assuming a product sale."
    _, provider, report = run_pipeline(selected=request(seller_context=supplied))
    for call in provider.calls:
        assert not {"email", "owner_hash", "session", "user", "auth"} & call.data.keys()
        assert supplied not in call.instructions
        if call.task in {"synthesize_strategy", "generate_talk_points"}:
            assert call.data["seller_context"] == {"provided_by_user": supplied}
        else:
            assert "seller_context" not in call.data
    assert supplied not in report.model_dump_json()


def test_state_capacity_and_restart_recovery_are_explicit():
    def transform(call, value):
        if call.task == "discover_news":
            return ProviderError("provider_unavailable", "Official search is unavailable.", True)
        return value

    configured = settings(max_results=1)
    provider = FakeProvider(configured, transform=transform)
    pipeline = InsightsPipeline(configured, provider, FakeFetcher())
    with pytest.raises(InsightError):
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert pipeline.can_retry(REPORT_ID)
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run("b" * 32, request(), lambda *_: None))
    assert failure.value.code == "capacity_exhausted"
    restarted = InsightsPipeline(configured, provider, FakeFetcher())
    assert not restarted.can_retry(REPORT_ID)
    pipeline.forget(REPORT_ID)
    assert not pipeline.can_retry(REPORT_ID)


def test_no_supported_strategy_is_a_partial_result_not_an_invented_opportunity():
    def transform(call, value):
        if call.task == "synthesize_strategy":
            return value.model_copy(
                update={
                    "opportunities": [],
                    "evidence_gaps": [
                        EvidenceGap(
                            area="opportunities",
                            detail="Available Contoso evidence does not establish a useful workflow opportunity.",
                        )
                    ],
                }
            )
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    assert report.status == "partial"
    assert not report.opportunities
    assert len(report.audiences) == 5
    assert all(item.coverage == "insufficient_evidence" for item in report.audiences)
    assert sum(call.task == "synthesize_strategy" for call in provider.calls) == 1
    assert sum(call.task == "generate_talk_points" for call in provider.calls) == 5


def test_a_role_count_repair_can_label_limited_coverage_without_inventing_points():
    def transform(call, value):
        if call.task == "generate_talk_points" and call.data["audience"] == "ceo":
            repaired = "repair_feedback" in call.data
            return value.model_copy(
                update={
                    "talk_points": value.talk_points[:2],
                    "coverage": "limited" if repaired else "complete",
                    "evidence_gaps": [
                        EvidenceGap(
                            area="opportunities",
                            detail="Only two distinct Contoso points are supported.",
                        )
                    ]
                    if repaired
                    else [],
                }
            )
        return value

    _, provider, report = run_pipeline(FakeProvider(transform=transform))
    ceo = next(item for item in report.audiences if item.audience == "ceo")
    assert ceo.coverage == "limited"
    assert len(ceo.talk_points) == 2
    assert report.status == "partial"
    assert sum(call.task == "generate_talk_points" for call in provider.calls) == 6


def test_rejected_company_core_is_not_published_after_the_single_repair():
    def transform(call, value):
        return (
            reject_section(
                value,
                "section_company",
                facts=[call.data["evidence"][0]["id"]],
            )
            if call.task == "review_report"
            else value
        )

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "quality_not_accepted"
    assert sum(call.task == "review_report" for call in provider.calls) == 2
    assert not pipeline.can_retry(REPORT_ID)
    assert not pipeline._states[REPORT_ID].documents


def test_oversized_review_does_not_silently_drop_requested_roles_or_publish():
    class OversizedReviewProvider(FakeProvider):
        def estimate_input_tokens(
            self, task_id, instructions, data, output_type, *, allowed_domains=()
        ):
            if task_id == "review_report":
                return 32_001
            return super().estimate_input_tokens(
                task_id, instructions, data, output_type, allowed_domains=allowed_domains
            )

    provider = OversizedReviewProvider()
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "task_input_limit"
    assert sum(call.task == "generate_talk_points" for call in provider.calls) == 5
    assert not any(call.task == "review_report" for call in provider.calls)
    assert not pipeline.can_retry(REPORT_ID)


def test_at_most_three_verified_announcements_prioritize_supported_executive_statements():
    today = datetime.now(UTC).date()
    news = [
        document(
            f"https://contoso.com/news/announcement-{index}",
            text=(
                'Contoso CEO Alex Morgan said, "We are investing in governed reviewer workflows."'
                if index
                else "Contoso announced new reviewer workflows."
            ),
            published_at=today - timedelta(days=index + 1),
        )
        for index in range(4)
    ]
    provider = FakeProvider(
        news=[recent_candidate(item.url, executive=index > 0) for index, item in enumerate(news)]
    )
    _, _, report = run_pipeline(provider, FakeFetcher([document(), *news]))
    assert len(report.announcements) == 3
    assert all(item.executive_name == "Alex Morgan" for item in report.announcements)
    assert (
        next(item for item in report.source_outcomes if item.url == news[0].url).status == "skipped"
    )
    check_report(report)


@pytest.mark.parametrize(
    "mismatch",
    [
        "unit",
        "currency",
        "period",
        "time_context",
        "statement",
    ],
)
@pytest.mark.parametrize("numeric_text", ["5", "500"])
def test_persistent_numeric_or_period_mismatch_omits_only_the_affected_fact(mismatch, numeric_text):
    text = [
        "Contoso reported revenue of 500 USD million in 2025.",
        "Contoso provides business software for invoice reviewers.",
    ]

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            quantity = QuotedQuantity(
                numeric_text=numeric_text,
                unit="billion" if mismatch == "unit" else "million",
                currency="EUR" if mismatch == "currency" else "USD",
                period="2024" if mismatch == "period" else "2025",
                kind="actual",
                span_id="missing_span" if mismatch == "span" else fact.evidence[0].span_id,
            )
            invalid = fact.model_copy(
                update={
                    "quantities": [quantity],
                    "time_context": "2024" if mismatch == "time_context" else None,
                    "statement": fact.statement.replace("500", "5")
                    if mismatch == "statement"
                    else fact.statement,
                }
            )
            return value.model_copy(update={"facts": [invalid, *value.facts[1:]]})
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher([document(text=text)]))
    report = asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert report.status == "completed"
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 2
    assert all("revenue" not in fact.statement.casefold() for fact in report.facts)
    warning = next(gap for gap in report.evidence_gaps if gap.area == "Numeric and period warnings")
    assert "Excluded 1 candidate fact" in warning.detail
    assert "could not be reconciled with the exact source quotation" in warning.detail
    check_report(report)


@pytest.mark.parametrize(
    ("mismatch", "code"), [("span", "unknown_reference"), ("quote", "quote_mismatch")]
)
@pytest.mark.parametrize("numeric_text", ["5", "500"])
def test_numeric_relaxation_does_not_weaken_reference_or_quote_checks(mismatch, code, numeric_text):
    text = [
        "Contoso reported revenue of 500 USD million in 2025.",
        "Contoso provides business software for invoice reviewers.",
    ]

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            quantity = QuotedQuantity(
                numeric_text=numeric_text,
                unit="million",
                currency="USD",
                period="2025",
                kind="actual",
                span_id="missing_span" if mismatch == "span" else fact.evidence[0].span_id,
            )
            invalid = fact.model_copy(
                update={
                    "quantities": [quantity],
                    "evidence": [
                        fact.evidence[0].model_copy(
                            update={"quote": fact.evidence[0].quote.replace("500", "5")}
                        )
                    ]
                    if mismatch == "quote"
                    else fact.evidence,
                }
            )
            return value.model_copy(update={"facts": [invalid, *value.facts[1:]]})
        return value

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher([document(text=text)]))
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == code
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 2
    assert not any(call.task == "consolidate_evidence" for call in provider.calls)


@pytest.mark.parametrize(
    ("quoted_number", "statement_number"),
    [("500", "500.0"), ("1,000", "1000")],
)
def test_equivalent_numeric_formatting_does_not_trigger_repair(quoted_number, statement_number):
    text = f"Contoso reported revenue of {quoted_number} USD million in 2025."

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            return value.model_copy(
                update={
                    "facts": [
                        fact.model_copy(
                            update={
                                "statement": fact.statement.replace(quoted_number, statement_number)
                            }
                        )
                    ]
                }
            )
        return value

    _, provider, report = run_pipeline(
        FakeProvider(transform=transform), FakeFetcher([document(text=text)])
    )
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    assert not any(gap.area == "Numeric and period warnings" for gap in report.evidence_gaps)
    assert statement_number in report.facts[0].statement
    check_report(report)


@pytest.mark.parametrize("numeric_text", ["5", "500.0", "five hundred", "500"])
def test_quantity_numeric_mismatches_publish_with_warnings_without_repair(numeric_text):
    text = "Contoso reported revenue of 500 USD million in 2025."

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            quantity = QuotedQuantity(
                numeric_text=numeric_text,
                unit="million",
                currency="USD",
                period="2025",
                kind="actual",
                span_id=fact.evidence[0].span_id,
            )
            return value.model_copy(
                update={"facts": [fact.model_copy(update={"quantities": [quantity]})]}
            )
        return value

    _, provider, report = run_pipeline(
        FakeProvider(transform=transform), FakeFetcher([document(text=text)])
    )
    assert report.status == "completed"
    assert sum(call.task == "extract_evidence" for call in provider.calls) == 1
    fact = report.facts[0]
    assert fact.quantities[0].numeric_text == numeric_text
    warnings = fact.warnings
    assert bool(warnings) == (numeric_text != "500")
    assert any(gap.area == "Quantity warnings" for gap in report.evidence_gaps) == bool(warnings)
    if warnings:
        assert len(warnings) == 1
        assert warnings[0].code == "quantity_numeric_mismatch"
        assert warnings[0].quantity_index == 0
        assert warnings[0].numeric_text == numeric_text
        assert warnings[0].span_id == fact.evidence[0].span_id
        assert "Verify the quoted figure" in warnings[0].detail
    for call in provider.calls:
        if call.task in {"consolidate_evidence", "synthesize_strategy", "generate_talk_points"}:
            assert call.data["facts"][0]["warnings"] == [
                warning.model_dump() for warning in warnings
            ]
        elif call.task == "review_report":
            assert call.data["evidence"][0]["warnings"] == [
                warning.model_dump() for warning in warnings
            ]
    check_report(report)


def test_quantity_warning_survives_exclusion_of_model_written_limitations():
    text = "Contoso reported revenue of 500 USD million in 2025."

    def transform(call, value):
        if call.task == "extract_evidence":
            fact = value.facts[0]
            quantity = QuotedQuantity(
                numeric_text="5",
                unit=None,
                currency=None,
                period=None,
                kind="actual",
                span_id=fact.evidence[0].span_id,
            )
            return value.model_copy(
                update={"facts": [fact.model_copy(update={"quantities": [quantity]})]}
            )
        if call.task == "review_report":
            return reject_section(value, "section_limitations")
        return value

    _, _, report = run_pipeline(
        FakeProvider(transform=transform), FakeFetcher([document(text=text)])
    )
    assert report.facts[0].warnings
    assert any(gap.area == "Quantity warnings" for gap in report.evidence_gaps)


def test_source_checkpoint_byte_limits_fail_before_generation_and_release_text(monkeypatch):
    monkeypatch.setattr("cxplorer.insights.pipeline.MAX_CHECKPOINT_BYTES", 512)
    provider = FakeProvider()
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "checkpoint_limit"
    assert not provider.calls
    assert not pipeline._states[REPORT_ID].documents
    assert not pipeline.can_retry(REPORT_ID)


def test_unreviewed_model_gap_text_is_excluded_but_structural_coverage_remains():
    unsupported = "Contoso has no security controls."

    def transform(call, value):
        if call.task == "consolidate_evidence":
            return value.model_copy(
                update={"evidence_gaps": [EvidenceGap(area="security", detail=unsupported)]}
            )
        if call.task == "review_report":
            return reject_section(value, "section_limitations")
        return value

    _, _, report = run_pipeline(FakeProvider(transform=transform))
    assert report.status == "partial"
    assert unsupported not in report.model_dump_json()
    assert any(gap.area == "official_news" for gap in report.evidence_gaps)
    assert any(gap.area == "quality_review" for gap in report.evidence_gaps)


def test_a_structured_empty_news_result_without_a_search_tool_call_is_not_success():
    class NoSearchProvider(FakeProvider):
        async def generate(self, task_id, instructions, data, output_type, *, allowed_domains=()):
            generated = await super().generate(
                task_id, instructions, data, output_type, allowed_domains=allowed_domains
            )
            return Generated(generated.value, Usage(80, 64, 0))

    provider = NoSearchProvider()
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "search_not_performed"
    assert [call.task for call in provider.calls] == ["verify_sources", "discover_news"]
    assert not pipeline.can_retry(REPORT_ID)


@pytest.mark.parametrize(
    "resource",
    ["input", "output"],
    ids=["parallel-input-reservations", "parallel-output-reservations"],
)
def test_parallel_token_reservations_cannot_spend_the_review_allowance(resource):
    async def scenario():
        configured = settings()
        provider = FakeProvider(configured)
        ledger = BudgetLedger(Allowance(10, 100_000, 100_000, 0))
        executor = TaskExecutor(
            provider,
            configured.tasks,
            ledger,
            retry_limits={"azure_openai": 0, "openai": 0},
            vendor_semaphores={
                "azure_openai": asyncio.Semaphore(2),
                "openai": asyncio.Semaphore(2),
            },
        )
        executor.deadline = time.monotonic() + 30
        planned = executor.plan(
            "first", "extract_evidence", "Contoso", {"spans": []}, EvidenceBatch
        )
        executor.protect({"review": "review_report"})
        review = ledger.protected["review"]
        ledger.ceiling = Allowance(
            10,
            review.input_tokens + planned.input_tokens if resource == "input" else 100_000,
            review.output_tokens + planned.output_tokens if resource == "output" else 100_000,
            0,
        )
        results = await asyncio.gather(
            executor.execute("first", "extract_evidence", "Contoso", {"spans": []}, EvidenceBatch),
            executor.execute("second", "extract_evidence", "Contoso", {"spans": []}, EvidenceBatch),
            return_exceptions=True,
        )
        assert sum(isinstance(result, EvidenceBatch) for result in results) == 1
        assert sum(isinstance(result, TaskFailure) for result in results) == 1
        assert len(provider.calls) == 1
        assert ledger.protected["review"] == review
        assert ledger.total().fits(ledger.ceiling)

    asyncio.run(scenario())


def test_www_named_shared_tenant_never_implicitly_authorizes_its_parent():
    tenant = "https://www.github.io/"
    redirected = document("https://github.io/")
    fetcher = FakeFetcher([redirected])
    fetcher.documents[tenant] = redirected
    selected = InsightRequest(seeds=[SeedInput(url=tenant, purpose="homepage")], audiences=["ceo"])
    provider = FakeProvider()
    pipeline = InsightsPipeline(settings(), provider, fetcher)
    with pytest.raises(InsightError):
        asyncio.run(pipeline.run(REPORT_ID, selected, lambda *_: None))
    assert fetcher.calls[0][2] == {"www.github.io"}
    assert not provider.calls


def test_psl_www_to_apex_seed_redirect_keeps_search_scoped_to_the_final_host():
    www = "https://www.contoso.com/"
    homepage = document()
    fetcher = FakeFetcher(
        [homepage, document(ABOUT, text="Contoso provides governed invoice review workflows.")]
    )
    fetcher.documents[www] = homepage
    selected = InsightRequest(seeds=[SeedInput(url=www, purpose="homepage")], audiences=["ceo"])
    _, provider, report = run_pipeline(fetcher=fetcher, selected=selected)
    assert fetcher.calls[0][2] == {"www.contoso.com", "contoso.com"}
    assert report.status == "completed"
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("contoso.com",)


def test_unexpected_boundary_failure_is_safe_and_releases_unretryable_text():
    def transform(call, value):
        return (
            RuntimeError("private provider response internals")
            if call.task == "extract_evidence"
            else value
        )

    provider = FakeProvider(transform=transform)
    pipeline = InsightsPipeline(settings(), provider, FakeFetcher())
    with pytest.raises(InsightError) as failure:
        asyncio.run(pipeline.run(REPORT_ID, request(), lambda *_: None))
    assert failure.value.code == "pipeline_failure"
    assert "private provider response" not in failure.value.public_message
    assert not pipeline._states[REPORT_ID].documents
    assert not pipeline._states[REPORT_ID].artifacts
    assert not pipeline.can_retry(REPORT_ID)


@pytest.mark.parametrize(
    "kind",
    ["generic-time", "truncated-publication"],
    ids=["generic-time-is-not-publication", "truncated-metadata-is-not-publication"],
)
def test_metadata_hints_cannot_be_promoted_to_verified_publication_dates(kind):
    publication = datetime.now(UTC).date() - timedelta(days=3)
    news = document(NEWS, text="Contoso announced an investment in reviewer workflows.")
    metadata = TextSpan(
        "sp_metadata_date",
        f"time datetime: {publication.isoformat()}T10:30:00Z",
        section="Date metadata" if kind == "generic-time" else "Publication metadata",
    )
    news = replace(
        news,
        spans=(*news.spans, metadata),
        text=news.text + "\n" + metadata.text,
        publication_evidence=(metadata,) if kind == "truncated-publication" else (),
        coverage={
            **news.coverage,
            "publication_metadata_truncated": kind == "truncated-publication",
        },
    )

    def transform(call, value):
        if call.task == "verify_sources" and call.data["news_source_ids"]:
            return value.model_copy(
                update={
                    "verdicts": [
                        verdict.model_copy(
                            update={
                                "published_date": publication.isoformat(),
                                "date_evidence": [
                                    EvidenceQuote(span_id=metadata.id, quote=metadata.text)
                                ],
                            }
                        )
                        if verdict.source_id == news.id
                        else verdict
                        for verdict in value.verdicts
                    ]
                }
            )
        return value

    provider = FakeProvider(news=[recent_candidate()], transform=transform)
    _, _, report = run_pipeline(provider, FakeFetcher([document(), news]))
    assert report.news_status == "none_found"
    assert not report.announcements
    assert (
        next(outcome for outcome in report.source_outcomes if outcome.url == NEWS).status
        == "rejected"
    )


def test_truncated_metadata_does_not_block_an_independent_visible_publication_statement():
    publication = datetime.now(UTC).date() - timedelta(days=3)
    news = document(
        NEWS,
        text=[
            "Contoso announced new reviewer workflows.",
            "Published " + publication.isoformat(),
        ],
    )
    metadata = TextSpan(
        "sp_truncated_publication",
        "article:published_time: " + publication.isoformat() + "T10:30:00Z",
        section="Publication metadata",
    )
    news = replace(
        news,
        spans=(*news.spans, metadata),
        text=news.text + "\n" + metadata.text,
        publication_evidence=(metadata,),
        coverage={**news.coverage, "publication_metadata_truncated": True},
    )
    _, _, report = run_pipeline(
        FakeProvider(news=[recent_candidate()]), FakeFetcher([document(), news])
    )
    assert report.announcements[0].published_at == publication
    assert any(
        citation.quote == "Published " + publication.isoformat()
        for fact in report.facts
        for citation in fact.evidence
    )
    assert all(
        citation.span_id != metadata.id for fact in report.facts for citation in fact.evidence
    )
    assert any("Publication metadata was truncated" in gap.detail for gap in report.evidence_gaps)


@pytest.mark.parametrize("modified_offset", [-1, 0, 1])
def test_nonpublication_dates_do_not_conflict_with_or_support_a_valid_publication(
    modified_offset,
):
    publication = datetime.now(UTC).date() - timedelta(days=3)
    modified = publication + timedelta(days=modified_offset)
    news = document(
        NEWS, text="Contoso announced new reviewer workflows.", published_at=publication
    )
    metadata = TextSpan(
        "sp_modified_time",
        f"time datetime: {modified.isoformat()}T10:30:00Z",
        section="Date metadata",
    )
    news = replace(
        news,
        spans=(*news.spans, metadata),
        text=news.text + "\n" + metadata.text,
        publication_evidence=(*news.publication_evidence, metadata),
    )
    _, _, report = run_pipeline(
        FakeProvider(news=[recent_candidate()]), FakeFetcher([document(), news])
    )
    assert report.news_status == "found"
    assert report.announcements[0].published_at == publication
    assert all(
        citation.span_id != metadata.id for fact in report.facts for citation in fact.evidence
    )


def test_conflicting_publication_metadata_in_different_formats_is_not_arbitrarily_resolved():
    publication = datetime.now(UTC).date() - timedelta(days=3)
    different = publication - timedelta(days=1)
    news = document(
        NEWS, text="Contoso announced new reviewer workflows.", published_at=publication
    )
    metadata = TextSpan(
        "sp_conflicting_publication",
        f"datePublished: {different:%B} {different.day}, {different.year}",
        section="Publication metadata",
    )
    news = replace(
        news,
        spans=(*news.spans, metadata),
        text=news.text + "\n" + metadata.text,
        publication_evidence=(*news.publication_evidence, metadata),
    )
    _, _, report = run_pipeline(
        FakeProvider(news=[recent_candidate()]), FakeFetcher([document(), news])
    )
    assert report.news_status == "none_found"
    assert not report.announcements


def test_pdf_collection_omissions_and_non_text_pages_survive_in_the_report():
    original = document()
    pdf = replace(
        original,
        media_type="application/pdf",
        spans=tuple(replace(span, page=1) for span in original.spans),
        coverage={
            "complete": False,
            "publication_date_conflict": False,
            "truncated": True,
            "text_truncated": True,
            "omissions": ["page_limit", "non_text_pages"],
            "total_pages": 8,
            "pages_processed": 3,
            "pages_with_text": 2,
            "pages_without_text": [2],
            "omitted_pages": 5,
            "extraction": "text_only_pdf",
        },
    )
    _, _, report = run_pipeline(fetcher=FakeFetcher([pdf]))
    note = report.sources[0].coverage_note
    assert "3 of 8 pages processed" in note
    assert "5 omitted" in note
    assert "Pages without readable text: 2" in note
    assert "page_limit" in note
    assert "non_text_pages" in note
    assert any("non_text_pages" in gap.detail for gap in report.evidence_gaps)


def test_misclassified_news_candidate_does_not_remove_a_verified_about_seed():
    about = document(
        ABOUT, text="Contoso provides governed invoice review workflows for customers."
    )
    provider = FakeProvider(news=[recent_candidate(ABOUT)])
    _, provider, report = run_pipeline(
        provider, FakeFetcher([document(), about]), selected=request(about=True)
    )
    assert report.news_status == "none_found"
    assert any(
        call.task == "extract_evidence" and call.data["source"]["url"] == ABOUT
        for call in provider.calls
    )
    outcome = next(item for item in report.source_outcomes if item.url == ABOUT)
    assert outcome.status == "accepted"
    assert "retained as background" in outcome.reason


def test_psl_apex_to_www_seed_redirect_keeps_search_scoped_to_the_final_host():
    redirected = document("https://www.contoso.com/")
    fetcher = FakeFetcher([redirected])
    fetcher.documents[HOME] = redirected
    provider = FakeProvider()
    _, provider, report = run_pipeline(provider, fetcher)
    assert fetcher.calls[0][2] == {"contoso.com", "www.contoso.com"}
    assert report.status == "completed"
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("www.contoso.com",)


def test_optional_redirect_failure_is_preserved_as_an_explicit_evidence_gap():
    message = (
        "The source redirects to a host that was not explicitly approved. "
        "Provide its canonical URL as a verified seed; www/apex redirects are not automatically authorized."
    )
    fetcher = FakeFetcher(
        [document(links=[ABOUT])], errors={ABOUT: SourceError("unapproved_host", message)}
    )
    _, _, report = run_pipeline(fetcher=fetcher)
    outcome = next(item for item in report.source_outcomes if item.url == ABOUT)
    assert outcome.status == "rejected"
    assert outcome.reason == message
    assert any("not explicitly approved" in gap.detail for gap in report.evidence_gaps)


def test_unfetched_psl_counterpart_is_not_authorized_for_news_or_link_discovery():
    news_url = "https://www.contoso.com/news/reviewer-investment"
    fetcher = FakeFetcher([document(links=["https://www.contoso.com/about"])])
    provider = FakeProvider(news=[recent_candidate(news_url)])
    _, provider, report = run_pipeline(provider, fetcher)
    assert fetcher.calls[0][2] == {"contoso.com", "www.contoso.com"}
    assert [url for url, _, _ in fetcher.calls] == [HOME]
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("contoso.com",)
    assert (
        next(item for item in report.source_outcomes if item.url == news_url).status == "rejected"
    )
    assert report.news_status == "none_found"


def test_redirecting_original_seed_host_is_not_reused_for_news_or_link_discovery():
    redirected = document("https://www.contoso.com/", links=["https://contoso.com/products"])
    fetcher = FakeFetcher([redirected])
    fetcher.documents[HOME] = redirected
    provider = FakeProvider(news=[recent_candidate(NEWS)])
    _, provider, report = run_pipeline(provider, fetcher)
    assert [url for url, _, _ in fetcher.calls] == [HOME]
    search = next(call for call in provider.calls if call.task == "discover_news")
    assert search.domains == ("www.contoso.com",)
    assert next(item for item in report.source_outcomes if item.url == NEWS).status == "rejected"
    assert report.news_status == "none_found"
