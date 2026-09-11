"""Bounded, ephemeral company research; Python owns all scheduling and publication."""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel

from cxplorer.ai.config import AISettings
from cxplorer.ai.providers import AIProviderClient
from cxplorer.ai.tasks import Allowance, BudgetLedger, TaskExecutor, TaskFailure
from cxplorer.insights.errors import InsightError
from cxplorer.insights.prompts import AUDIENCE_BRIEFS, instructions_for
from cxplorer.insights.schemas import (
    AcceptedReport,
    Announcement,
    AudienceCoverage,
    AudienceTalkPoints,
    Citation,
    CompanyDossier,
    EvidenceBatch,
    EvidenceFact,
    EvidenceGap,
    InsightRequest,
    NewsDiscovery,
    Opportunity,
    QualityReview,
    SourceOutcome,
    SourceRecord,
    SourceVerdict,
    SourceVerification,
    StrategyBrief,
)
from cxplorer.insights.sources import SourceDocument, SourceFetcher, TextSpan
from cxplorer.insights.urls import (
    SourceError,
    approved_hosts,
    canonical_seed_hosts,
    is_approved_url,
    normalize_url,
)
from cxplorer.insights.validation import (
    ArtifactError,
    accept_facts,
    check_audience,
    check_dossier,
    check_report,
    check_review,
    check_source_verification,
    check_strategy,
    dates_in_text,
    entity_in_text,
    referenced_facts,
    stable_id,
)

logger = logging.getLogger(__name__)

Progress = Callable[[str, str], None]
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_ALL_CHECKPOINT_BYTES = 32 * 1024 * 1024
MAX_DOCUMENT_TEXT_BYTES = 128 * 1024
MAX_SOURCE_TEXT_BYTES = 1536 * 1024
MAX_JOB_TOOL_CALLS = 75
MAX_NEWS_CANDIDATES = 6
MAX_RETAINED_LINKS = 32
CORE_SECTIONS = frozenset(
    {"section_company", "section_summary", "section_evidence", "section_sources"}
)
_SOCIAL_HOSTS = frozenset(
    {"linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com"}
)
_PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:log-?in|sign-?in|sign-?up|oauth|auth|account|accounts|profile|profiles|personal)(?:/|$)",
    re.IGNORECASE,
)
_DISCOVERY_PATHS = (
    ("investors", re.compile(r"(?:investor|annual[-_ ]?report|financial[-_ ]?report)", re.I)),
    ("about", re.compile(r"(?:about|who-we-are|our-company|company/overview)", re.I)),
    ("products", re.compile(r"(?:products?|services?|solutions?)", re.I)),
    ("trust", re.compile(r"(?:trust|security|compliance|responsible-ai)", re.I)),
)
_SUBSTANCE = re.compile(
    r"\b(?:provides?|products?|services?|solutions?|customers?|workflow|review|strategy|investment|"
    r"revenue|financial|security|governance|chief|ceo|cto|cio|cfo|ciso|announced|said|published)\b",
    re.IGNORECASE,
)


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _public_company_link(url: str, hosts: set[str]) -> bool:
    if not is_approved_url(url, hosts):
        return False
    parts = urlsplit(url)
    host = parts.hostname or ""
    if any(host == denied or host.endswith("." + denied) for denied in _SOCIAL_HOSTS):
        return False
    return not _PRIVATE_PATH.search(unquote(parts.path))


def _span_data(span: TextSpan) -> dict:
    return {"span_id": span.id, "text": span.text, "page": span.page, "section": span.section}


@dataclass
class CollectedSource:
    record: SourceRecord
    spans: tuple[TextSpan, ...]
    publication_span_ids: frozenset[str]
    links: tuple[str, ...]
    original_span_count: int
    publication_conflict: bool
    publisher_date: date | None = None
    publication_metadata_truncated: bool = False
    verified: bool = False
    omissions: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def title(self) -> str:
        return self.record.title

    @property
    def text(self) -> str:
        return "\n".join(span.text for span in self.spans)


@dataclass(frozen=True)
class Chunk:
    key: str
    source_id: str
    spans: tuple[TextSpan, ...]


@dataclass
class ArtifactCheckpoint:
    task_id: str
    input_hash: str
    value: BaseModel


@dataclass
class CallSpec:
    key: str
    task_id: str
    data: dict
    output_type: type[BaseModel]
    validate: Callable[[Any], None]
    domains: tuple[str, ...] = ()
    retry_validate: Callable[[Any], None] | None = None


class StageInvalid(ArtifactError):
    def __init__(self, key: str, error: ArtifactError, value: BaseModel) -> None:
        super().__init__(error.code, error.public_message)
        self.key = key
        self.value = value


@dataclass
class Checkpoint:
    input_hash: str
    config_hash: str
    audiences: tuple[str, ...]
    as_of_date: date
    executor: TaskExecutor
    generated_at: datetime | None = None
    artifacts: dict[str, ArtifactCheckpoint] = field(default_factory=dict)
    documents: dict[str, CollectedSource] = field(default_factory=dict)
    url_documents: dict[str, str] = field(default_factory=dict)
    attempted_urls: set[str] = field(default_factory=set)
    outcomes: dict[str, SourceOutcome] = field(default_factory=dict)
    gaps: list[EvidenceGap] = field(default_factory=list)
    seed_ids: list[str] = field(default_factory=list)
    homepage_id: str = ""
    company_name: str = ""
    official_hosts: set[str] = field(default_factory=set)
    seeds_done: bool = False
    news_done: bool = False
    discovery_done: bool = False
    news_source_ids: list[str] = field(default_factory=list)
    discovery_source_ids: list[str] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    news_facts: dict[str, EvidenceFact] = field(default_factory=dict)
    chunks: list[Chunk] | None = None
    facts: dict[str, EvidenceFact] = field(default_factory=dict)
    fact_owners: dict[str, str] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    failures: dict[str, TaskFailure] = field(default_factory=dict)
    terminal_tasks: set[str] = field(default_factory=set)
    estimates: dict[str, Allowance] = field(default_factory=dict)
    task_ids: dict[str, str] = field(default_factory=dict)
    repair_used: bool = False
    repair_origin: str = ""
    repair_feedback: dict[str, list[dict]] = field(default_factory=dict)
    fallback: AcceptedReport | None = None
    elapsed: float = 0.0
    retryable: bool = False
    finished: bool = False
    forgotten: bool = False
    active_task: asyncio.Task | None = None

    def clear_payload(self) -> None:
        self.artifacts.clear()
        self.documents.clear()
        self.url_documents.clear()
        self.attempted_urls.clear()
        self.outcomes.clear()
        self.gaps.clear()
        self.seed_ids.clear()
        self.official_hosts.clear()
        self.news_source_ids.clear()
        self.discovery_source_ids.clear()
        self.announcements.clear()
        self.news_facts.clear()
        self.chunks = None
        self.facts.clear()
        self.fact_owners.clear()
        self.opportunities.clear()
        self.failures.clear()
        self.terminal_tasks.clear()
        self.estimates.clear()
        self.task_ids.clear()
        self.repair_feedback.clear()
        self.fallback = None
        self.executor.ledger.protected.clear()


class InsightsPipeline:
    def __init__(
        self, ai_settings: AISettings, provider: AIProviderClient, fetcher: SourceFetcher
    ) -> None:
        self.settings = ai_settings
        self.provider = provider
        self.fetcher = fetcher
        self._states: dict[str, Checkpoint] = {}
        self._vendor_semaphores = {
            name: asyncio.Semaphore(vendor.max_concurrency)
            for name, vendor in ai_settings.vendors.items()
        }
        self._closed = False
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        search = self.settings.tasks["discover_news"]
        if (
            not search.use_web_search
            or not 1 <= search.max_tool_calls <= 25
            or any(
                task.use_web_search
                for name, task in self.settings.tasks.items()
                if name != "discover_news"
            )
        ):
            raise InsightError(
                "invalid_configuration",
                "Official announcement search is required; other insight tasks must not browse.",
            )
        if self.settings.tasks["review_report"].max_input_tokens > 32_000:
            raise InsightError(
                "invalid_configuration", "The final review input must remain within 32,000 tokens."
            )

    def _new_state(self, request: InsightRequest) -> Checkpoint:
        limits = self.settings.pipeline
        ledger = BudgetLedger(
            Allowance(
                min(40, limits.max_model_calls),
                min(300_000, limits.max_total_input_tokens),
                min(120_000, limits.max_total_output_tokens),
                MAX_JOB_TOOL_CALLS,
            )
        )
        return Checkpoint(
            input_hash=_fingerprint(request.model_dump(mode="json")),
            config_hash=self.settings.fingerprint,
            audiences=tuple(request.audiences),
            as_of_date=datetime.now(UTC).date(),
            executor=TaskExecutor(
                self.provider,
                self.settings.tasks,
                ledger,
                retry_limits={
                    key: value.max_retries for key, value in self.settings.vendors.items()
                },
                vendor_semaphores=self._vendor_semaphores,
            ),
        )

    def forget(self, report_id: str) -> None:
        state = self._states.pop(report_id, None)
        if state is not None:
            state.forgotten = True
            state.retryable = False
            if state.active_task is not None and not state.active_task.done():
                state.active_task.cancel()
            state.clear_payload()

    def can_retry(self, report_id: str) -> bool:
        state = self._states.get(report_id)
        if (
            self._closed
            or state is None
            or state.forgotten
            or state.finished
            or state.active_task is not None
            or not state.retryable
            or state.config_hash != self.settings.fingerprint
            or state.elapsed >= min(900, self.settings.pipeline.job_timeout_seconds)
            or state.executor.ledger.exhausted
        ):
            return False
        return self._has_transient_failure(state)

    async def close(self) -> None:
        self._closed = True
        tasks = [
            state.active_task
            for state in self._states.values()
            if state.active_task is not None and not state.active_task.done()
        ]
        for report_id in list(self._states):
            self.forget(report_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(
        self, report_id: str, request: InsightRequest, progress: Progress
    ) -> AcceptedReport:
        if self._closed:
            raise InsightError("shutting_down", "Generation is unavailable during shutdown.")
        if re.fullmatch(r"[a-f0-9]{32}", report_id) is None:
            raise InsightError("invalid_report_id", "Start a new generation from the workspace.")
        if len(request.seeds) > self.settings.pipeline.max_seed_urls:
            raise InsightError("too_many_sources", "Use fewer official company source URLs.")
        state = self._states.get(report_id)
        if state is None:
            if len(self._states) >= min(16, self.settings.pipeline.max_results):
                raise InsightError("capacity_exhausted", "Temporary generation capacity is full.")
            state = self._new_state(request)
            self._states[report_id] = state
        else:
            if (
                state.input_hash != _fingerprint(request.model_dump(mode="json"))
                or state.config_hash != self.settings.fingerprint
            ):
                raise InsightError(
                    "retry_input_changed",
                    "A retry must use the same inputs and configuration. Start a new generation.",
                )
            if not self.can_retry(report_id):
                raise InsightError("not_retryable", "This temporary generation cannot be retried.")
        started = time.monotonic()
        state.active_task = asyncio.current_task()
        state.retryable = False
        state.executor.deadline = (
            started + min(900, self.settings.pipeline.job_timeout_seconds) - state.elapsed
        )
        try:
            self._protect_remaining(state)
            async with asyncio.timeout_at(state.executor.deadline):
                await self._collect(state, request, progress)
                await self._generate(state, request, progress)
                candidate = self._candidate(report_id, state, request)
                review = await self._review(state, request, candidate, progress)
                fallback = self._publish(state, candidate, review, allow_failure=True)
                state.fallback = fallback
                targets = self._review_targets(state, review)
                if targets:
                    logger.info(
                        "Insight quality review repair plan: available=%s targets=%s "
                        "issue_count=%d artifact_repair_origin=%s",
                        str(
                            not state.repair_used and bool(self.settings.pipeline.max_repair_rounds)
                        ).lower(),
                        ",".join(sorted(targets)),
                        sum(len(issues) for issues in targets.values()),
                        state.repair_origin or "none",
                    )
                if targets and not state.repair_used and self.settings.pipeline.max_repair_rounds:
                    progress(
                        "repair", "Correcting the affected artifacts once, then reviewing again."
                    )
                    try:
                        self._begin_repair(
                            state,
                            targets,
                            origin="quality_review",
                        )
                        self._protect_remaining(state)
                        await self._collect(state, request, progress)
                        await self._generate(state, request, progress)
                        candidate = self._candidate(report_id, state, request)
                        review = await self._review(state, request, candidate, progress)
                    except InsightError:
                        if fallback is None:
                            raise
                        state.retryable = self._has_transient_failure(state)
                        if not state.retryable:
                            state.finished = True
                            state.clear_payload()
                        return fallback
                report = self._publish(state, candidate, review)
                assert report is not None
                state.retryable = self._has_transient_failure(state)
                if report.status == "completed" or not state.retryable:
                    state.finished = True
                    state.clear_payload()
                return report
        except asyncio.CancelledError:
            state.retryable = False
            state.finished = True
            state.clear_payload()
            raise
        except TimeoutError:
            state.retryable = False
            state.finished = True
            state.clear_payload()
            raise InsightError(
                "job_timeout",
                "Generation reached its time limit and was not restarted automatically.",
            ) from None
        except InsightError as error:
            state.retryable = (
                isinstance(error, TaskFailure)
                and error.retryable
                and self._has_transient_failure(state)
            )
            fallback = state.fallback
            if fallback is not None and error.code not in {
                "unrelated_source",
                "uncertain_company",
                "retry_input_changed",
                "configuration_changed",
                "job_expired",
            }:
                if not state.retryable:
                    state.finished = True
                    state.clear_payload()
                return fallback
            outcomes = list(state.outcomes.values())
            if not state.retryable:
                state.finished = True
                state.clear_payload()
            if error.source_outcomes:
                raise
            raise InsightError(error.code, error.public_message, source_outcomes=outcomes) from None
        except Exception:
            outcomes = list(state.outcomes.values())
            state.retryable = False
            state.finished = True
            state.clear_payload()
            raise InsightError(
                "pipeline_failure",
                "Generation stopped before a valid report could be accepted. Check the sources and "
                "configuration before starting a new generation.",
                source_outcomes=outcomes,
            ) from None
        finally:
            state.elapsed += time.monotonic() - started
            state.active_task = None
            if state.forgotten:
                state.clear_payload()

    def _has_transient_failure(self, state: Checkpoint) -> bool:
        if state.executor.ledger.exhausted or not any(
            failure.retryable and key in state.estimates and key not in state.terminal_tasks
            for key, failure in state.failures.items()
        ):
            return False
        projected = replace(state.executor.ledger, protected=self._remaining_reservations(state))
        return projected.total().fits(projected.ceiling)

    def _remaining_reservations(self, state: Checkpoint) -> dict[str, Allowance]:
        plans = {
            "dossier": "consolidate_evidence",
            "strategy": "synthesize_strategy",
            **{f"audience_{audience}": "generate_talk_points" for audience in state.audiences},
            "review": "review_report",
        }
        if not state.seeds_done:
            plans["verify_seeds"] = "verify_sources"
        if not state.news_done:
            plans["discover_news"] = "discover_news"
            plans["verify_news"] = "verify_sources"
        if not state.discovery_done and state.discovery_source_ids:
            plans["verify_discovery"] = "verify_sources"
        if not state.facts and state.chunks is None:
            plans["extract_minimum"] = "extract_evidence"
        reservations = {
            key: state.executor.allowance(task)
            for key, task in plans.items()
            if (key == "review" or key not in state.artifacts)
            and key not in state.terminal_tasks
            and key not in state.executor.ledger.in_flight
        }
        for chunk in state.chunks or []:
            if (
                chunk.key not in state.artifacts
                and chunk.key not in state.executor.ledger.in_flight
            ):
                reservations[chunk.key] = state.estimates.get(
                    chunk.key, state.executor.allowance("extract_evidence")
                )
        if not state.repair_used and self.settings.pipeline.max_repair_rounds:
            reservations["repair_reserve"] = state.executor.allowance(
                "extract_evidence"
            ) + state.executor.allowance("review_report")
        return reservations

    def _protect_remaining(self, state: Checkpoint) -> None:
        reservations = self._remaining_reservations(state)
        state.executor.ledger.protected.clear()
        state.executor.protect_allowances(reservations)

    def _gap(self, state: Checkpoint, area: str, detail: str) -> None:
        gap = EvidenceGap(area=area[:100], detail=detail[:600])
        if gap not in state.gaps and len(state.gaps) < 60:
            state.gaps.append(gap)

    def _outcome(self, state: Checkpoint, url: str, status: str, reason: str) -> None:
        if url in state.outcomes or len(state.outcomes) < 40:
            try:
                public_url = normalize_url(url)
            except SourceError:
                public_url = ""
            state.outcomes[url] = SourceOutcome(url=public_url, status=status, reason=reason[:600])

    def _record_relation_warnings(
        self, state: Checkpoint, verification: SourceVerification
    ) -> None:
        changed = False
        note = (
            "Identity warning: the retained page title or text identifies the homepage company, "
            "but the selected relation quotation does not name it explicitly."
        )
        for verdict in verification.verdicts:
            if verdict.relation != "related" or entity_in_text(
                verification.company_name, " ".join(quote.quote for quote in verdict.evidence)
            ):
                continue
            source = state.documents[verdict.source_id]
            previous = source.record.coverage_note or ""
            if note not in previous:
                source.record = source.record.model_copy(
                    update={"coverage_note": f"{previous} {note}".strip()[:600]}
                )
                changed = True
            self._gap(state, "source_identity", f"{source.title}: {note}")
        if changed:
            self._check_memory(state)

    def _record_quote_match_warning(self, state: Checkpoint) -> None:
        self._gap(
            state,
            "Quotation matching warnings",
            "One or more model-provided quotations differed slightly from retained source text. "
            "CXplorer accepted only high-confidence matches, replaced them with the exact source "
            "substring, and flagged the citations for review.",
        )

    def _checkpoint_size(self, state: Checkpoint) -> int:
        source_bytes = sum(
            sum(len(span.text.encode("utf-8")) + len(span.id) + 400 for span in source.spans)
            + sum(len(link.encode("utf-8")) for link in source.links)
            + len(source.record.model_dump_json().encode("utf-8"))
            for source in state.documents.values()
        )
        artifact_bytes = sum(
            len(artifact.value.model_dump_json().encode("utf-8"))
            for artifact in state.artifacts.values()
        )
        original_spans = {
            (source.id, span.id): span.text
            for source in state.documents.values()
            for span in source.spans
        }
        fragment_bytes = sum(
            len(span.text.encode("utf-8"))
            for chunk in state.chunks or []
            for span in chunk.spans
            if original_spans.get((chunk.source_id, span.id)) != span.text
        )
        return (
            source_bytes
            + artifact_bytes
            + fragment_bytes
            + _json_size(state.repair_feedback)
            + sum(len(fact.model_dump_json().encode("utf-8")) for fact in state.news_facts.values())
            + sum(len(fact.model_dump_json().encode("utf-8")) for fact in state.facts.values())
            + sum(
                len(item.model_dump_json().encode("utf-8")) for item in state.opportunities.values()
            )
            + (len(state.fallback.model_dump_json().encode("utf-8")) if state.fallback else 0)
        )

    def _check_memory(self, state: Checkpoint) -> None:
        if (
            self._checkpoint_size(state) > MAX_CHECKPOINT_BYTES
            or sum(self._checkpoint_size(item) for item in self._states.values())
            > MAX_ALL_CHECKPOINT_BYTES
        ):
            raise InsightError(
                "checkpoint_limit",
                "The bounded in-memory research capacity is full. Use fewer or narrower sources.",
            )

    async def _call(self, state: Checkpoint, spec: CallSpec) -> BaseModel:
        if state.forgotten:
            raise InsightError("job_expired", "This temporary generation is no longer available.")
        if state.config_hash != self.settings.fingerprint:
            raise InsightError(
                "configuration_changed",
                "Generation configuration changed. Start a new job rather than mixing model routes.",
            )
        started = time.monotonic()
        logger.info(
            "Insight pipeline task before: task=%s artifact=%s",
            spec.task_id,
            spec.key,
        )
        input_hash = _fingerprint(spec.data)
        previous = state.artifacts.get(spec.key)
        if previous is not None and previous.input_hash == input_hash:
            state.executor.unprotect(spec.key)
            logger.info(
                "Insight pipeline task after: task=%s artifact=%s status=reused "
                "duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                max(time.monotonic() - started, 0.0),
            )
            return previous.value
        if previous is not None:
            self._invalidate(state, spec.key)
        data = dict(spec.data)
        if spec.key in state.repair_feedback:
            data["repair_feedback"] = state.repair_feedback[spec.key]
        instructions = instructions_for(spec.task_id)
        state.task_ids[spec.key] = spec.task_id

        async def execute(call_data: dict) -> BaseModel:
            state.estimates[spec.key] = state.executor.plan(
                spec.key,
                spec.task_id,
                instructions,
                call_data,
                spec.output_type,
                allowed_domains=spec.domains,
            )
            return await state.executor.execute(
                spec.key,
                spec.task_id,
                instructions,
                call_data,
                spec.output_type,
                allowed_domains=spec.domains,
            )

        async def execute_with_output_correction() -> BaseModel:
            try:
                return await execute(data)
            except TaskFailure as error:
                if error.code != "invalid_output":
                    raise
                details = error.contract_details
                stage = details.stage if details is not None else "unknown"
                locations = details.locations if details is not None else ()
                error_types = details.error_types if details is not None else ()
                error_count = details.error_count if details is not None else 0
                logger.warning(
                    "Insight pipeline output contract correction before: task=%s artifact=%s "
                    "stage=%s validation_errors=%d locations=%s types=%s",
                    spec.task_id,
                    spec.key,
                    stage,
                    error_count,
                    ",".join(locations) or "none",
                    ",".join(error_types) or "none",
                )
                feedback = {
                    "code": "invalid_output",
                    "message": (
                        "The previous response failed the registered output contract. Return "
                        "exactly one artifact and obey every field type, enum, nullability rule, "
                        "list/text/numeric bound, uniqueness rule, and cross-field invariant."
                    ),
                    "contract_stage": stage,
                    "validation_locations": list(locations),
                    "validation_error_types": list(error_types),
                }
                retry_data = {
                    **data,
                    "repair_feedback": [
                        *data.get("repair_feedback", []),
                        feedback,
                    ],
                    "output_contract_correction": {"attempt": 1},
                }
                try:
                    value = await execute(retry_data)
                except TaskFailure as retry_error:
                    retry_details = retry_error.contract_details
                    logger.warning(
                        "Insight pipeline output contract correction after: task=%s artifact=%s "
                        "status=failed code=%s stage=%s",
                        spec.task_id,
                        spec.key,
                        retry_error.code,
                        retry_details.stage if retry_details is not None else "unknown",
                    )
                    raise
                logger.info(
                    "Insight pipeline output contract correction after: task=%s artifact=%s "
                    "status=completed",
                    spec.task_id,
                    spec.key,
                )
                return value

        try:
            value = await execute_with_output_correction()
        except asyncio.CancelledError:
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=cancelled "
                "duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                max(time.monotonic() - started, 0.0),
            )
            raise
        except TaskFailure as error:
            state.failures[spec.key] = error
            if not error.retryable:
                state.terminal_tasks.add(spec.key)
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=failed code=%s "
                "retryable=%s duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                error.code,
                str(error.retryable).lower(),
                max(time.monotonic() - started, 0.0),
            )
            raise
        try:
            spec.validate(value)
        except ArtifactError as error:
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=rejected code=%s "
                "duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                error.code,
                max(time.monotonic() - started, 0.0),
            )
            raise StageInvalid(spec.key, error, value) from None
        if state.forgotten:
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=failed "
                "code=job_expired duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                max(time.monotonic() - started, 0.0),
            )
            raise InsightError("job_expired", "This temporary generation is no longer available.")
        if state.config_hash != self.settings.fingerprint:
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=failed "
                "code=configuration_changed duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                max(time.monotonic() - started, 0.0),
            )
            raise InsightError(
                "configuration_changed",
                "Generation configuration changed before the result was accepted.",
            )
        state.artifacts[spec.key] = ArtifactCheckpoint(spec.task_id, input_hash, value)
        state.failures.pop(spec.key, None)
        state.terminal_tasks.discard(spec.key)
        try:
            self._check_memory(state)
        except InsightError as error:
            logger.warning(
                "Insight pipeline task after: task=%s artifact=%s status=failed code=%s "
                "duration_seconds=%.3f",
                spec.task_id,
                spec.key,
                error.code,
                max(time.monotonic() - started, 0.0),
            )
            raise
        logger.info(
            "Insight pipeline task after: task=%s artifact=%s status=completed "
            "duration_seconds=%.3f",
            spec.task_id,
            spec.key,
            max(time.monotonic() - started, 0.0),
        )
        return value

    async def _batch(
        self, state: Checkpoint, specs: list[CallSpec], concurrency: int
    ) -> list[BaseModel | BaseException]:
        semaphore = asyncio.Semaphore(concurrency)

        async def invoke(spec: CallSpec) -> BaseModel:
            async with semaphore:
                return await self._call(state, spec)

        tasks = [asyncio.create_task(invoke(spec)) for spec in specs]
        try:
            return await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _validated_batch(
        self, state: Checkpoint, specs: list[CallSpec], concurrency: int
    ) -> list[BaseModel | BaseException]:
        results = await self._batch(state, specs, concurrency)
        positions = [
            index
            for index, result in enumerate(results)
            if isinstance(result, StageInvalid)
            and result.code != "insufficient_evidence"
            and self.settings.pipeline.max_repair_rounds
        ]
        retries = []
        for index in positions:
            spec = specs[index]
            invalid = results[index]
            assert isinstance(invalid, StageInvalid)
            feedback = [
                *spec.data.get("repair_feedback", []),
                *state.repair_feedback.get(spec.key, []),
                invalid.feedback,
            ]
            logger.info(
                "Insight pipeline validation correction before: task=%s artifact=%s code=%s",
                spec.task_id,
                spec.key,
                invalid.code,
            )
            retries.append(
                CallSpec(
                    f"{spec.key}_validation_retry",
                    spec.task_id,
                    {
                        **spec.data,
                        "repair_feedback": feedback,
                        "validation_correction": {
                            "attempt": 1,
                            "code": invalid.code,
                        },
                    },
                    spec.output_type,
                    spec.retry_validate or spec.validate,
                    domains=spec.domains,
                )
            )
        if retries:
            repaired = await self._batch(state, retries, concurrency)
            for index, retry_spec, result in zip(positions, retries, repaired, strict=True):
                spec = specs[index]
                if isinstance(result, BaseModel):
                    self._promote_temporary_artifact(
                        state,
                        retry_spec.key,
                        spec,
                        result,
                    )
                    logger.info(
                        "Insight pipeline validation correction after: task=%s artifact=%s "
                        "status=completed",
                        spec.task_id,
                        spec.key,
                    )
                else:
                    self._discard_temporary_task(state, retry_spec.key)
                    code = result.code if isinstance(result, InsightError) else "unexpected_error"
                    logger.warning(
                        "Insight pipeline validation correction after: task=%s artifact=%s "
                        "status=failed code=%s",
                        spec.task_id,
                        spec.key,
                        code,
                    )
                    if isinstance(result, TaskFailure):
                        mapped = TaskFailure(
                            result.code,
                            result.public_message,
                            retryable=result.retryable,
                            task_key=spec.key,
                            contract_details=result.contract_details,
                        )
                        state.failures[spec.key] = mapped
                        if not mapped.retryable:
                            state.terminal_tasks.add(spec.key)
                        result = mapped
                results[index] = result
        return results

    def _discard_temporary_task(self, state: Checkpoint, key: str) -> None:
        state.artifacts.pop(key, None)
        state.failures.pop(key, None)
        state.terminal_tasks.discard(key)
        state.estimates.pop(key, None)
        state.task_ids.pop(key, None)
        state.repair_feedback.pop(key, None)
        state.executor.unprotect(key)

    def _promote_temporary_artifact(
        self,
        state: Checkpoint,
        temporary_key: str,
        spec: CallSpec,
        value: BaseModel,
    ) -> None:
        self._discard_temporary_task(state, temporary_key)
        state.artifacts[spec.key] = ArtifactCheckpoint(
            spec.task_id,
            _fingerprint(spec.data),
            value,
        )
        state.failures.pop(spec.key, None)
        state.terminal_tasks.discard(spec.key)
        self._check_memory(state)

    async def _improve_audience_results(
        self,
        state: Checkpoint,
        specs: list[CallSpec],
        results: list[BaseModel | BaseException],
        progress: Progress,
    ) -> list[BaseModel | BaseException]:
        retries: list[CallSpec] = []
        positions: list[int] = []
        for index, (spec, result) in enumerate(zip(specs, results, strict=True)):
            previous = result.value if isinstance(result, StageInvalid) else result
            if not isinstance(previous, AudienceTalkPoints):
                continue
            if isinstance(result, StageInvalid):
                feedback = [result.feedback]
            elif state.opportunities and previous.coverage != "complete":
                feedback = [
                    {
                        "code": "audience_coverage_improvement",
                        "message": (
                            "Accepted shared opportunities support a fuller discovery-oriented "
                            "executive conversation. Add distinct analysis or hypotheses without "
                            "inventing role-specific company priorities."
                        ),
                    }
                ]
            else:
                continue
            retry_data = {
                **spec.data,
                "repair_feedback": feedback,
                "coverage_correction": {
                    "attempt": 1,
                    "previous_coverage": previous.coverage,
                    "previous_point_count": len(previous.talk_points),
                },
            }
            retries.append(
                CallSpec(
                    f"{spec.key}_coverage_retry",
                    spec.task_id,
                    retry_data,
                    spec.output_type,
                    spec.validate,
                )
            )
            positions.append(index)
        if not retries:
            return results

        progress(
            "generate_talk_points",
            f"Making one evidence-safe coverage correction for {len(retries)} audience(s).",
        )
        retried = await self._batch(
            state,
            retries,
            self.settings.pipeline.audience_concurrency,
        )
        coverage_rank = {"insufficient_evidence": 0, "limited": 1, "complete": 2}
        for index, retry_spec, retry_result in zip(positions, retries, retried, strict=True):
            spec = specs[index]
            original = results[index]
            selected: BaseModel | BaseException = retry_result
            if isinstance(original, AudienceTalkPoints):
                if isinstance(retry_result, AudienceTalkPoints):
                    original_rank = (
                        coverage_rank[original.coverage],
                        len(original.talk_points),
                    )
                    retry_rank = (
                        coverage_rank[retry_result.coverage],
                        len(retry_result.talk_points),
                    )
                    selected = retry_result if retry_rank >= original_rank else original
                elif isinstance(retry_result, (StageInvalid, TaskFailure)):
                    selected = original
            if selected is retry_result and isinstance(retry_result, AudienceTalkPoints):
                self._promote_temporary_artifact(
                    state,
                    retry_spec.key,
                    spec,
                    retry_result,
                )
            else:
                self._discard_temporary_task(state, retry_spec.key)
            if isinstance(selected, TaskFailure):
                mapped = TaskFailure(
                    selected.code,
                    selected.public_message,
                    retryable=selected.retryable,
                    task_key=spec.key,
                    contract_details=selected.contract_details,
                )
                state.failures[spec.key] = mapped
                if not mapped.retryable:
                    state.terminal_tasks.add(spec.key)
                selected = mapped
            results[index] = selected
        return results

    async def _one(self, state: Checkpoint, spec: CallSpec) -> BaseModel:
        result = (await self._validated_batch(state, [spec], 1))[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def _begin_repair(
        self,
        state: Checkpoint,
        targets: Mapping[str, list[dict]],
        *,
        origin: str,
    ) -> None:
        if state.repair_used:
            raise InsightError(
                "repair_exhausted", "The generation's single correction round was used."
            )
        codes = sorted(
            {str(issue.get("code", "unknown")) for issues in targets.values() for issue in issues}
        )
        sections = sorted(
            {
                str(issue.get("section_id", "unknown")).removeprefix("section_")
                for issues in targets.values()
                for issue in issues
            }
        )
        logger.info(
            "Insight pipeline correction round started: origin=%s artifacts=%d keys=%s "
            "codes=%s sections=%s",
            origin,
            len(targets),
            ",".join(sorted(targets)),
            ",".join(codes) or "none",
            ",".join(sections) or "none",
        )
        state.repair_used = True
        state.repair_origin = origin
        state.executor.unprotect("repair_reserve")
        for key, issues in targets.items():
            state.repair_feedback[key] = list(issues)[:40]
            self._invalidate(state, key)
        self._check_memory(state)

    def _invalidate(self, state: Checkpoint, key: str) -> None:
        invalid = {key, "review"}
        if key.startswith("extract_") or key.startswith("verify_"):
            invalid.update({"dossier", "strategy"})
            state.facts.clear()
        elif key == "dossier":
            invalid.add("strategy")
        if "strategy" in invalid:
            invalid.update(key for key in state.artifacts if key.startswith("audience_"))
            state.opportunities.clear()
        if key == "verify_news":
            state.news_done = False
            state.news_facts.clear()
            state.announcements.clear()
            invalid.update(
                chunk.key
                for chunk in state.chunks or []
                if chunk.source_id in state.news_source_ids
            )
            state.chunks = None
        for artifact_key in invalid:
            state.artifacts.pop(artifact_key, None)
            state.terminal_tasks.discard(artifact_key)

    def _span_rank(self, span: TextSpan, company_name: str = "") -> int:
        return (
            min(12, len(_SUBSTANCE.findall(span.text)))
            + (12 if company_name and entity_in_text(company_name, span.text) else 0)
            + (3 if span.section and _SUBSTANCE.search(span.section) else 0)
        )

    def _materialize(self, state: Checkpoint, document: SourceDocument) -> CollectedSource:
        used = sum(
            len(span.text.encode("utf-8"))
            for source in state.documents.values()
            for span in source.spans
        )
        remaining = min(MAX_DOCUMENT_TEXT_BYTES, MAX_SOURCE_TEXT_BYTES - used)
        if remaining <= 0:
            raise SourceError("source_memory_limit", "The bounded source-text allowance is full.")
        publication_ids = {span.id for span in document.publication_evidence}
        ranked = sorted(
            enumerate(document.spans),
            key=lambda item: (
                item[1].id in publication_ids,
                self._span_rank(item[1], state.company_name) + (8 if item[0] == 0 else 0),
                -item[0],
            ),
            reverse=True,
        )
        kept: dict[int, TextSpan] = {}
        for index, span in ranked:
            if remaining <= 0:
                break
            encoded = span.text.encode("utf-8")
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            if not text.strip():
                continue
            kept[index] = TextSpan(
                id=span.id,
                text=text,
                page=span.page,
                section=span.section[:300] if span.section else None,
            )
            remaining -= len(text.encode("utf-8"))
        if not kept:
            raise SourceError("unreadable_source", "The source has no usable company text.")
        spans = tuple(kept[index] for index in sorted(kept))
        notes = []
        if not document.coverage["complete"]:
            notes.append("The publisher's accessible text was only partially readable.")
        if document.media_type == "application/pdf" and "total_pages" in document.coverage:
            coverage = document.coverage
            missing = coverage["pages_without_text"]
            notes.append(
                f"PDF coverage: {coverage['pages_processed']} of {coverage['total_pages']} pages "
                f"processed; {coverage['omitted_pages']} omitted; {len(missing)} without readable text."
            )
            if missing:
                pages = ", ".join(str(page) for page in missing[:12])
                remainder = f"; {len(missing) - 12} more" if len(missing) > 12 else ""
                notes.append(f"Pages without readable text: {pages}{remainder}.")
        for omission in document.coverage["omissions"]:
            notes.append(f"Collection omission: {omission}.")
        if document.coverage.get("publication_metadata_truncated"):
            notes.append(
                "Publication metadata was truncated; dates require separate visible publisher evidence."
            )
        if len(document.links) > MAX_RETAINED_LINKS or document.coverage.get("links_truncated"):
            notes.append(
                f"Discovery retained {min(len(document.links), MAX_RETAINED_LINKS)} of "
                f"{len(document.links)} collected links; additional navigation was omitted."
            )
        if len(spans) != len(document.spans) or any(
            kept[index].text != document.spans[index].text for index in kept
        ):
            notes.append(
                f"Retained bounded excerpts from {len(spans)} of {len(document.spans)} parsed spans."
            )
        note = " ".join(notes)[:600] or None
        record = SourceRecord(
            id=document.id,
            original_url=document.original_url,
            url=document.url,
            title=document.title[:300] or "Official company source",
            media_type=document.media_type,
            retrieved_at=document.retrieved_at,
            published_at=document.published_at,
            content_hash=document.content_hash,
            purpose=document.purpose[:30],
            is_news=False,
            coverage_note=note,
        )
        return CollectedSource(
            record=record,
            spans=spans,
            publication_span_ids=frozenset(span.id for span in spans if span.id in publication_ids),
            links=document.links[:MAX_RETAINED_LINKS],
            original_span_count=len(document.spans),
            publication_conflict=bool(document.coverage["publication_date_conflict"]),
            publisher_date=document.published_at,
            publication_metadata_truncated=bool(
                document.coverage.get("publication_metadata_truncated", False)
            ),
            omissions=notes,
        )

    async def _fetch(
        self, state: Checkpoint, url: str, purpose: str, hosts: set[str]
    ) -> CollectedSource | None:
        started = time.monotonic()
        safe_purpose = purpose if re.fullmatch(r"[a-z][a-z0-9_]{0,31}", purpose) else "other"

        def finish(
            status: str,
            *,
            code: str = "none",
            source: CollectedSource | None = None,
        ) -> None:
            log = logger.warning if status == "rejected" else logger.info
            log(
                "Insight source fetch after: purpose=%s status=%s code=%s "
                "duration_seconds=%.3f text_bytes=%s spans=%s links=%s",
                safe_purpose,
                status,
                code,
                max(time.monotonic() - started, 0.0),
                len(source.text.encode("utf-8")) if source is not None else "unavailable",
                len(source.spans) if source is not None else "unavailable",
                len(source.links) if source is not None else "unavailable",
            )

        logger.info(
            "Insight source fetch before: purpose=%s attempted=%d limit=%d",
            safe_purpose,
            len(state.attempted_urls) + 1,
            self.settings.pipeline.max_fetched_pages,
        )
        if url in state.url_documents:
            source = state.documents[state.url_documents[url]]
            finish("reused", source=source)
            return source
        if url in state.attempted_urls:
            finish("skipped", code="already_attempted")
            return None
        if len(state.attempted_urls) >= self.settings.pipeline.max_fetched_pages:
            self._outcome(
                state, url, "skipped", "The bounded document-fetch allowance was reached."
            )
            self._gap(
                state, "collection", "Additional linked sources were omitted at the document limit."
            )
            finish("skipped", code="document_limit")
            return None
        state.attempted_urls.add(url)
        try:
            if not _public_company_link(url, hosts):
                raise SourceError(
                    "unapproved_source",
                    "Use public official company pages, not login or social profiles.",
                )
            document = await self.fetcher.fetch(url, purpose=purpose, allowed_hosts=hosts)
            if not _public_company_link(document.url, hosts):
                raise SourceError(
                    "unapproved_redirect",
                    "A source redirected outside its approved hosts. Supply the final public company URL directly.",
                )
            duplicate = next(
                (
                    source
                    for source in state.documents.values()
                    if source.record.url == document.url
                    or source.record.content_hash == document.content_hash
                ),
                None,
            )
            if duplicate is not None:
                state.url_documents[url] = duplicate.id
                state.url_documents[document.url] = duplicate.id
                self._outcome(
                    state,
                    url,
                    "accepted",
                    "Duplicate canonical content; the existing source is reused.",
                )
                finish("reused", source=duplicate)
                return duplicate
            source = self._materialize(state, document)
            if source.id in state.documents:
                raise SourceError(
                    "source_identity_conflict", "Source identifiers could not be reconciled."
                )
            state.documents[source.id] = source
            state.url_documents[url] = source.id
            state.url_documents[document.url] = source.id
            self._outcome(
                state, url, "accepted", "Accessible source text collected for company verification."
            )
            for note in source.omissions:
                self._gap(state, "source_coverage", f"{source.title}: {note}")
            try:
                self._check_memory(state)
            except InsightError as error:
                finish("rejected", code=error.code)
                raise
            finish("completed", source=source)
            return source
        except SourceError as error:
            self._outcome(state, url, "rejected", error.public_message)
            self._gap(state, "collection", f"Source omitted: {error.public_message} ({url})")
            finish("rejected", code=error.code)
            return None

    def _verification_spec(
        self, state: Checkpoint, key: str, sources: list[CollectedSource], *, supplied: bool = False
    ) -> CallSpec:
        unique = {source.id: source for source in sources}
        homepage = state.documents[state.homepage_id]
        unique[homepage.id] = homepage
        selections = {}
        for source in unique.values():
            selected = sorted(
                enumerate(source.spans),
                key=lambda item: (
                    self._span_rank(item[1], state.company_name),
                    item[1].id in source.publication_span_ids,
                    -item[0],
                ),
                reverse=True,
            )[:6]
            selections[source.id] = [
                TextSpan(span.id, span.text[:1200], span.page, span.section) for _, span in selected
            ]

        def pack() -> dict:
            return {
                "homepage_source_id": homepage.id,
                "company_name": state.company_name or None,
                "source_ids": list(unique),
                "news_source_ids": [
                    source_id for source_id in unique if source_id in state.news_source_ids
                ],
                "documents": [
                    {
                        "source_id": source.id,
                        "url": source.record.url,
                        "title": source.title,
                        "purpose": source.record.purpose,
                        "publisher_publication_date": (
                            source.publisher_date.isoformat() if source.publisher_date else None
                        ),
                        "publication_metadata_truncated": source.publication_metadata_truncated,
                        "publication_date_conflict": source.publication_conflict,
                        "spans": [_span_data(span) for span in selections[source.id]],
                    }
                    for source in unique.values()
                ],
            }

        while True:
            data = pack()
            tokens = state.executor.estimate(
                "verify_sources", instructions_for("verify_sources"), data, SourceVerification
            )
            if tokens <= self.settings.tasks["verify_sources"].max_input_tokens:
                break
            removable = [source_id for source_id, spans in selections.items() if len(spans) > 1]
            if removable:
                longest = max(
                    removable, key=lambda source_id: sum(len(s.text) for s in selections[source_id])
                )
                selections[longest].pop()
                continue
            longest = max(selections, key=lambda source_id: len(selections[source_id][0].text))
            span = selections[longest][0]
            if len(span.text) < 120:
                raise TaskFailure(
                    "task_input_limit",
                    "The source identities cannot fit the configured verification budget.",
                )
            selections[longest][0] = TextSpan(
                span.id, span.text[: len(span.text) // 2], span.page, span.section
            )
        checked = [
            CollectedSource(
                record=source.record,
                spans=tuple(selections[source.id]),
                publication_span_ids=source.publication_span_ids,
                links=(),
                original_span_count=source.original_span_count,
                publication_conflict=source.publication_conflict,
                publisher_date=source.publisher_date,
                publication_metadata_truncated=source.publication_metadata_truncated,
            )
            for source in unique.values()
        ]

        def validate(result: SourceVerification) -> None:
            ids = [verdict.source_id for verdict in result.verdicts]
            if (
                set(ids) != set(unique)
                or len(ids) != len(unique)
                or result.homepage_source_id != homepage.id
            ):
                raise ArtifactError(
                    "unknown_reference", "Source verification must cover exactly its input."
                )
            rejected = [
                verdict
                for verdict in result.verdicts
                if verdict.relation != "related"
                and (supplied or verdict.source_id in state.seed_ids)
            ]
            if rejected:
                for verdict in rejected:
                    source = unique[verdict.source_id]
                    for url, source_id in state.url_documents.items():
                        if source_id == source.id:
                            self._outcome(
                                state,
                                url,
                                "rejected",
                                f"Company identity is {verdict.relation}: {verdict.rationale}",
                            )
                raise InsightError(
                    "unrelated_source"
                    if any(v.relation == "unrelated" for v in rejected)
                    else "uncertain_company",
                    "A supplied page cannot be verified as this company's official source. "
                    "Replace unrelated or ambiguous pages with the company's own homepage/About page.",
                    source_outcomes=list(state.outcomes.values()),
                )
            recovered_quotes = check_source_verification(
                result, checked, homepage.id, company_name=state.company_name or None
            )
            if recovered_quotes:
                self._record_quote_match_warning(state)

        return CallSpec(key, "verify_sources", data, SourceVerification, validate)

    async def _collect(
        self, state: Checkpoint, request: InsightRequest, progress: Progress
    ) -> None:
        if not state.seeds_done:
            progress(
                "preflight", "Checking every supplied company's source URL for accessible text."
            )
            failures = []
            seed_transport_hosts = canonical_seed_hosts(seed.url for seed in request.seeds)
            for seed in request.seeds:
                source = await self._fetch(state, seed.url, seed.purpose, seed_transport_hosts)
                if source is None:
                    failures.append(seed.url)
                else:
                    if not state.homepage_id:
                        state.homepage_id = source.id
                    if source.id not in state.seed_ids:
                        state.seed_ids.append(source.id)
            if failures:
                raise InsightError(
                    "invalid_source",
                    "Every supplied URL must be accessible. Replace blocked, unreadable or unavailable "
                    "pages with public official company pages before generating insights.",
                    source_outcomes=list(state.outcomes.values()),
                )
            progress(
                "verify_sources", "Verifying each supplied source against the official homepage."
            )
            result = await self._one(
                state,
                self._verification_spec(
                    state,
                    "verify_seeds",
                    [state.documents[source_id] for source_id in state.seed_ids],
                    supplied=True,
                ),
            )
            assert isinstance(result, SourceVerification)
            self._record_relation_warnings(state, result)
            state.company_name = result.company_name
            company_verified_documents = [
                state.documents[source_id] for source_id in state.seed_ids
            ]
            for source in company_verified_documents:
                source.verified = True
            state.official_hosts = approved_hosts(
                source.record.url for source in company_verified_documents
            )
            for seed in request.seeds:
                self._outcome(
                    state,
                    seed.url,
                    "accepted",
                    "Verified accessible official source for the homepage company.",
                )
            state.seeds_done = True
        if not state.news_done:
            await self._collect_news(state, request, progress)
        if not state.discovery_done:
            await self._collect_links(state, progress)

    async def _collect_news(
        self, state: Checkpoint, request: InsightRequest, progress: Progress
    ) -> None:
        progress("discover_news", "Searching official company announcements from the last 90 days.")
        lower = state.as_of_date - timedelta(days=self.settings.pipeline.news_days)
        domains = tuple(sorted(state.official_hosts))
        data = {
            "company_name": state.company_name,
            "homepage_url": state.documents[state.homepage_id].record.url,
            "approved_domains": list(domains),
            "published_on_or_after": lower.isoformat(),
            "as_of_date": state.as_of_date.isoformat(),
            "max_candidates": MAX_NEWS_CANDIDATES,
            "queries": [
                f'site:{host} "{state.company_name}" official announcement executive said after:{lower.isoformat()}'
                for host in domains
            ],
        }
        result: NewsDiscovery | None = None
        try:
            generated = await self._one(
                state,
                CallSpec(
                    "discover_news", "discover_news", data, NewsDiscovery, lambda _: None, domains
                ),
            )
        except TaskFailure as error:
            if error.code != "search_scope_violation":
                raise
            state.failures.pop("discover_news", None)
            state.terminal_tasks.discard("discover_news")
            state.executor.unprotect("discover_news")
            self._gap(
                state,
                "official_news",
                "Official announcement search warning: the provider returned references outside "
                "the independently verified company hosts. The complete search result was excluded, "
                "and generation continued using only independently verified supplied sources.",
            )
            logger.warning(
                "Insight news search scope recovery: status=continued "
                "code=search_scope_violation result=excluded"
            )
            progress(
                "discover_news",
                "Out-of-scope search results were excluded; continuing with verified official sources.",
            )
        else:
            assert isinstance(generated, NewsDiscovery)
            result = generated
        for seed in request.seeds:
            if seed.purpose == "news":
                source_id = state.url_documents[seed.url]
                if source_id != state.homepage_id and source_id not in state.news_source_ids:
                    state.news_source_ids.append(source_id)
        candidates = (
            sorted(
                enumerate(result.announcements),
                key=lambda pair: (pair[1].executive_name is None, pair[0]),
            )
            if result is not None
            else []
        )
        for _, candidate in candidates[:MAX_NEWS_CANDIDATES]:
            try:
                url = normalize_url(candidate.url)
            except SourceError:
                self._outcome(
                    state,
                    candidate.url,
                    "rejected",
                    "The announcement URL is not a safe public HTTPS URL.",
                )
                self._gap(state, "official_news", "An unsafe announcement candidate was excluded.")
                continue
            if not _public_company_link(url, state.official_hosts):
                self._outcome(
                    state,
                    url,
                    "rejected",
                    "Only verified official company hosts are eligible; external publishers and profiles are excluded.",
                )
                self._gap(
                    state,
                    "official_news",
                    "An external or non-public announcement candidate was excluded.",
                )
                continue
            source = await self._fetch(state, url, "news", state.official_hosts)
            if source is not None and source.id != state.homepage_id:
                if source.id not in state.news_source_ids:
                    state.news_source_ids.append(source.id)
            else:
                self._gap(
                    state,
                    "official_news",
                    "An announcement candidate was unavailable or was only the homepage.",
                )
        if not state.news_source_ids:
            state.executor.unprotect("verify_news")
            self._gap(
                state,
                "official_news",
                "No independently verified official company announcements were found in the last 90 days.",
            )
            state.news_done = True
            return
        progress(
            "verify_news",
            "Fetching and verifying official announcement dates and executive quotations.",
        )
        verified = await self._one(
            state,
            self._verification_spec(
                state,
                "verify_news",
                [state.documents[source_id] for source_id in state.news_source_ids],
            ),
        )
        assert isinstance(verified, SourceVerification)
        self._record_relation_warnings(state, verified)
        verdicts = {verdict.source_id: verdict for verdict in verified.verdicts}
        eligible: list[tuple[CollectedSource, SourceVerdict, date]] = []
        for source_id in state.news_source_ids:
            source = state.documents[source_id]
            source.record = source.record.model_copy(update={"is_news": False})
            verdict = verdicts[source_id]
            publication = self._publication_date(source, verdict)
            if verdict.relation != "related":
                reason = "The announcement is unrelated to, or uncertain for, the homepage company."
            elif publication is None:
                reason = (
                    "The announcement has no unambiguous, explicitly supported publication date."
                )
            elif publication < lower or publication > state.as_of_date:
                reason = (
                    "The announcement is outside the last 90 days or has a future publication date."
                )
            else:
                eligible.append((source, verdict, publication))
                continue
            if self._regular_seed(state, source):
                self._outcome(
                    state,
                    source.record.original_url,
                    "accepted",
                    f"Verified company seed retained as background, not a recent announcement. {reason}",
                )
            else:
                self._outcome(state, source.record.original_url, "rejected", reason)
            self._gap(state, "official_news", f"{source.title}: {reason}")
        eligible.sort(key=lambda item: (item[1].executive_name is not None, item[2]), reverse=True)
        state.announcements.clear()
        state.news_facts.clear()
        for index, (source, verdict, publication) in enumerate(eligible):
            if index >= self.settings.pipeline.max_news_articles:
                self._outcome(
                    state,
                    source.record.original_url,
                    "accepted" if self._regular_seed(state, source) else "skipped",
                    "Omitted from the announcement section after prioritizing at most three verified "
                    "articles; independently verified non-news seeds remain available as background.",
                )
                self._gap(
                    state,
                    "official_news",
                    "Additional qualifying announcements were omitted at the article limit.",
                )
                continue
            source.verified = True
            source.record = source.record.model_copy(
                update={"published_at": publication, "is_news": True}
            )
            state.announcements.append(
                Announcement(
                    source_id=source.id,
                    title=source.title,
                    published_at=publication,
                    executive_name=verdict.executive_name,
                    executive_role=verdict.executive_role,
                )
            )
            self._outcome(
                state,
                source.record.original_url,
                "accepted",
                "Verified recent official announcement with a supported publication date.",
            )
            self._news_support(state, source, verdict, publication)
        if not state.announcements:
            self._gap(
                state,
                "official_news",
                "No qualifying official announcements remained after independent date and company verification.",
            )
        state.news_done = True

    def _publication_date(self, source: CollectedSource, verdict: SourceVerdict) -> date | None:
        if source.publication_conflict:
            return None
        publication_dates = set()
        for span in source.spans:
            if span.id in source.publication_span_ids and span.section != "Date metadata":
                publication_dates.update(dates_in_text(span.text))
        if len(publication_dates) > 1:
            return None
        proposed = date.fromisoformat(verdict.published_date) if verdict.published_date else None
        metadata = None if source.publication_metadata_truncated else source.publisher_date
        if metadata is not None:
            return metadata if proposed is None or proposed == metadata else None
        if proposed is None:
            return None
        spans = {span.id: span for span in source.spans}
        for evidence in verdict.date_evidence:
            span = spans[evidence.span_id]
            if span.section == "Date metadata":
                continue
            if source.publication_metadata_truncated and (
                span.id in source.publication_span_ids or span.section == "Publication metadata"
            ):
                continue
            if re.search(
                r"\b(?:updated|modified|retrieved|accessed|copyright)\b", evidence.quote, re.I
            ) and not re.search(r"\b(?:published|posted|released|issued)\b", evidence.quote, re.I):
                continue
            if proposed in dates_in_text(evidence.quote):
                return proposed
        return None

    def _news_support(
        self, state: Checkpoint, source: CollectedSource, verdict: SourceVerdict, publication: date
    ) -> None:
        spans = {span.id: span for span in source.spans}
        quotes = [
            (quote.span_id, quote.quote)
            for quote in (verdict.evidence[:1] + verdict.executive_evidence)
        ]
        quotes.extend(
            (quote.span_id, quote.quote)
            for quote in verdict.date_evidence
            if publication in dates_in_text(quote.quote)
            and spans[quote.span_id].section != "Date metadata"
            and not (
                source.publication_metadata_truncated
                and (
                    quote.span_id in source.publication_span_ids
                    or spans[quote.span_id].section == "Publication metadata"
                )
            )
        )
        quotes.extend(
            (span.id, span.text[:1000])
            for span in source.spans
            if not source.publication_metadata_truncated
            and span.id in source.publication_span_ids
            and span.section != "Date metadata"
            and publication in dates_in_text(span.text)
        )
        for span_id, quote in dict.fromkeys(quotes):
            span = spans[span_id]
            fact = EvidenceFact(
                id=stable_id("fact", [source.id, span_id, quote, "announcement"]),
                category="news",
                entity=state.company_name,
                statement=quote,
                attribution="company_reported",
                quantities=[],
                time_context=publication.isoformat(),
                evidence=[
                    Citation(
                        source_id=source.id,
                        span_id=span_id,
                        quote=quote,
                        page=span.page,
                        section=span.section,
                    )
                ],
            )
            state.news_facts[fact.id] = fact
            state.fact_owners[fact.id] = "verify_news"

    async def _collect_links(self, state: Checkpoint, progress: Progress) -> None:
        progress("collect_sources", "Collecting a bounded set of relevant official company links.")
        links: dict[str, str] = {}
        for source in list(state.documents.values()):
            if not source.verified:
                continue
            for link in source.links:
                for purpose, pattern in _DISCOVERY_PATHS:
                    if not pattern.search(unquote(urlsplit(link).path)):
                        continue
                    if _public_company_link(link, state.official_hosts):
                        url = normalize_url(link)
                        if url not in state.url_documents and url not in state.attempted_urls:
                            links.setdefault(url, purpose)
                    break
        available = self.settings.pipeline.max_fetched_pages - len(state.attempted_urls)
        if len(links) > available:
            self._gap(
                state,
                "collection",
                f"Omitted {len(links) - available} related links at the document limit; no unrestricted crawl was performed.",
            )
        for url, purpose in list(links.items())[:available]:
            source = await self._fetch(state, url, purpose, state.official_hosts)
            if (
                source is not None
                and source.id not in state.seed_ids
                and source.id not in state.news_source_ids
                and source.id not in state.discovery_source_ids
            ):
                state.discovery_source_ids.append(source.id)
        if not state.discovery_source_ids:
            state.executor.unprotect("verify_discovery")
            state.discovery_done = True
            return
        result = await self._one(
            state,
            self._verification_spec(
                state,
                "verify_discovery",
                [state.documents[source_id] for source_id in state.discovery_source_ids],
            ),
        )
        assert isinstance(result, SourceVerification)
        self._record_relation_warnings(state, result)
        for verdict in result.verdicts:
            if verdict.source_id == state.homepage_id:
                continue
            source = state.documents[verdict.source_id]
            source.verified = verdict.relation == "related"
            self._outcome(
                state,
                source.record.original_url,
                "accepted" if source.verified else "rejected",
                "Verified official company source."
                if source.verified
                else "The linked page could not be verified as the homepage company's official material.",
            )
            if not source.verified:
                self._gap(
                    state,
                    "company_boundary",
                    f"{source.title}: excluded as unrelated or uncertain.",
                )
        state.discovery_done = True

    def _regular_seed(self, state: Checkpoint, source: CollectedSource) -> bool:
        return source.id in state.seed_ids and source.record.purpose != "news"

    def _usable_sources(self, state: Checkpoint) -> list[CollectedSource]:
        accepted_news = {announcement.source_id for announcement in state.announcements}
        return [
            source
            for source in state.documents.values()
            if source.verified
            and (
                source.id not in state.news_source_ids
                or source.id in accepted_news
                or self._regular_seed(state, source)
            )
        ]

    def _chunk(self, source: CollectedSource, spans: Iterable[TextSpan]) -> Chunk:
        spans = tuple(spans)
        return Chunk(
            key="extract_" + stable_id("chunk", [source.id, [_span_data(span) for span in spans]]),
            source_id=source.id,
            spans=spans,
        )

    def _chunk_data(self, state: Checkpoint, chunk: Chunk) -> dict:
        source = state.documents[chunk.source_id]
        return {
            "chunk_id": chunk.key,
            "company_name": state.company_name,
            "source": {
                "source_id": source.id,
                "title": source.title,
                "url": source.record.url,
                "purpose": source.record.purpose,
                "published_at": source.record.published_at.isoformat()
                if source.record.published_at
                else None,
                "is_verified_recent_announcement": source.record.is_news,
            },
            "spans": [_span_data(span) for span in chunk.spans],
        }

    def _chunk_allowance(self, state: Checkpoint, chunk: Chunk) -> Allowance:
        return state.executor.plan(
            chunk.key,
            "extract_evidence",
            instructions_for("extract_evidence"),
            self._chunk_data(state, chunk),
            EvidenceBatch,
        )

    def _all_chunks(self, state: Checkpoint) -> dict[str, list[Chunk]]:
        grouped: dict[str, list[Chunk]] = {}
        target = min(6000, self.settings.tasks["extract_evidence"].max_input_tokens)
        for source in self._usable_sources(state):
            current: list[TextSpan] = []
            chunks: list[Chunk] = []
            pending: list[tuple[TextSpan, ...]] = []
            characters = 0
            for span in source.spans:
                if current and characters + len(span.text) + 150 > target * 2:
                    pending.append(tuple(current))
                    current = []
                    characters = 0
                current.append(span)
                characters += len(span.text) + 150
            if current:
                pending.append(tuple(current))
            while pending:
                spans = pending.pop(0)
                candidate = self._chunk(source, spans)
                estimate = state.executor.estimate(
                    "extract_evidence",
                    instructions_for("extract_evidence"),
                    self._chunk_data(state, candidate),
                    EvidenceBatch,
                )
                if estimate <= target:
                    chunks.append(candidate)
                    continue
                if len(spans) > 1:
                    boundary = len(spans) // 2
                    pending[:0] = [spans[:boundary], spans[boundary:]]
                    continue
                span = spans[0]
                if len(span.text) < 120:
                    raise TaskFailure(
                        "task_input_limit",
                        "Even a small exact source excerpt cannot fit the configured extraction budget.",
                    )
                boundary = len(span.text) // 2
                pending[:0] = [
                    (TextSpan(span.id, span.text[:boundary], span.page, span.section),),
                    (TextSpan(span.id, span.text[boundary:], span.page, span.section),),
                ]
            grouped[source.id] = sorted(
                {chunk.key: chunk for chunk in chunks}.values(),
                key=lambda chunk: max(
                    self._span_rank(span, state.company_name) for span in chunk.spans
                ),
                reverse=True,
            )
        return grouped

    def _prepare_chunks(self, state: Checkpoint) -> None:
        state.executor.unprotect("extract_minimum")
        if state.chunks is not None:
            for chunk in state.chunks:
                if chunk.key not in state.artifacts:
                    reservation = self._chunk_allowance(state, chunk)
                    if not state.executor.ledger.affordable(chunk.key, reservation):
                        raise TaskFailure(
                            "budget_exhausted",
                            "The remaining evidence tasks exceed the job budget.",
                        )
                    state.executor.ledger.protected[chunk.key] = reservation
            return
        groups = self._all_chunks(state)
        ordered_sources = sorted(
            groups,
            key=lambda source_id: (
                source_id != state.homepage_id,
                state.documents[source_id].record.purpose not in {"products", "investors", "news"},
                source_id,
            ),
        )
        chosen: list[Chunk] = []
        maximum = max((len(chunks) for chunks in groups.values()), default=0)
        for index in range(maximum):
            for source_id in ordered_sources:
                chunks = groups[source_id]
                if index >= len(chunks):
                    continue
                chunk = chunks[index]
                reservation = self._chunk_allowance(state, chunk)
                if state.executor.ledger.affordable(chunk.key, reservation):
                    state.executor.ledger.protected[chunk.key] = reservation
                    chosen.append(chunk)
        if not chosen:
            raise TaskFailure(
                "budget_exhausted",
                "No source chunks fit while preserving the required strategy, audiences and quality review.",
            )
        state.chunks = chosen
        for source_id, chunks in groups.items():
            selected = sum(chunk.source_id == source_id for chunk in chosen)
            if selected != len(chunks):
                source = state.documents[source_id]
                note = f"Processed {selected} of {len(chunks)} ranked source chunks within the job budget."
                previous = source.record.coverage_note or ""
                source.record = source.record.model_copy(
                    update={"coverage_note": (previous + " " + note).strip()[:600]}
                )
                self._gap(state, "source_coverage", f"{source.title}: {note}")

    def _select_facts(self, state: Checkpoint) -> None:
        extracted: dict[str, EvidenceFact] = {}
        owners: dict[str, str] = dict.fromkeys(state.news_facts, "verify_news")
        for chunk in state.chunks or []:
            checkpoint = state.artifacts[chunk.key]
            batch = checkpoint.value
            assert isinstance(batch, EvidenceBatch)
            acceptance = accept_facts(
                batch,
                state.documents[chunk.source_id],
                chunk.spans,
                state.company_name,
                tolerate_numeric_or_period=True,
            )
            if acceptance.recovered_quotes:
                self._record_quote_match_warning(state)
            for fact in acceptance.facts:
                extracted[fact.id] = fact
                owners[fact.id] = chunk.key
            if acceptance.omitted_numeric_or_period:
                logger.warning(
                    "Insight evidence candidates omitted: task=extract_evidence artifact=%s "
                    "code=numeric_or_period_mismatch count=%d",
                    chunk.key,
                    acceptance.omitted_numeric_or_period,
                )
                self._gap(
                    state,
                    "Numeric and period warnings",
                    f"Excluded {acceptance.omitted_numeric_or_period} candidate fact(s) from "
                    f"{state.documents[chunk.source_id].title} because a number, unit, currency, "
                    "or reporting period could not be reconciled with the exact source quotation.",
                )
            if not acceptance.facts:
                self._gap(
                    state,
                    "evidence",
                    f"No substantive company facts were extracted from a selected chunk of "
                    f"{state.documents[chunk.source_id].title}.",
                )
        if not extracted:
            raise InsightError(
                "insufficient_evidence",
                "No useful company evidence was found. Supply a substantive official About, product or investor page.",
            )
        all_facts = {**extracted, **state.news_facts}
        evidence_allowance = min(
            self.settings.tasks["generate_talk_points"].max_input_tokens // 3,
            self.settings.tasks["synthesize_strategy"].max_input_tokens // 3,
            self.settings.tasks["consolidate_evidence"].max_input_tokens // 2,
            self.settings.tasks["review_report"].max_input_tokens // 3,
        )
        base = state.executor.estimate(
            "generate_talk_points",
            instructions_for("generate_talk_points"),
            {"facts": []},
            AudienceTalkPoints,
        )

        def size(facts: list[EvidenceFact]) -> int:
            return (
                state.executor.estimate(
                    "generate_talk_points",
                    instructions_for("generate_talk_points"),
                    {"facts": [fact.model_dump(mode="json") for fact in facts]},
                    AudienceTalkPoints,
                )
                - base
            )

        selected = list(state.news_facts.values())
        if size(selected) > evidence_allowance:
            raise TaskFailure(
                "task_input_limit",
                "The required announcement evidence is too large for the configured downstream budgets.",
            )
        ranked = sorted(
            extracted.values(),
            key=lambda fact: (
                fact.category not in {"company", "offering", "product_workflow"},
                not any(quote.source_id == state.homepage_id for quote in fact.evidence),
                fact.category,
                fact.id,
            ),
        )
        # Round-robin source/category groups avoids spending the evidence allowance on one page.
        buckets: dict[tuple[str, str], list[EvidenceFact]] = {}
        for fact in ranked:
            buckets.setdefault((fact.evidence[0].source_id, fact.category), []).append(fact)
        for index in range(max((len(values) for values in buckets.values()), default=0)):
            for values in buckets.values():
                if index < len(values):
                    fact = values[index]
                    if fact.id in state.news_facts:
                        continue
                    if len(selected) < 240 and size([*selected, fact]) <= evidence_allowance:
                        selected.append(fact)
        if not any(fact.id in extracted for fact in selected):
            raise InsightError(
                "insufficient_evidence",
                "No substantive company facts fit the downstream evidence allowance.",
            )
        state.facts = {fact.id: fact for fact in selected}
        state.fact_owners = {fact_id: owners[fact_id] for fact_id in state.facts}
        if len(state.facts) != len(all_facts):
            self._gap(
                state,
                "evidence_coverage",
                f"Selected {len(state.facts)} of {len(all_facts)} extracted and announcement facts "
                "before synthesis to preserve source diversity and the final review allowance.",
            )
        self._check_memory(state)

    async def _generate(
        self, state: Checkpoint, request: InsightRequest, progress: Progress
    ) -> None:
        self._prepare_chunks(state)
        progress(
            "extract_evidence",
            f"Extracting exact quoted evidence from {len(state.chunks or [])} selected chunks.",
        )
        specs = []
        for chunk in state.chunks or []:
            source = state.documents[chunk.source_id]

            def validate(batch: EvidenceBatch, source=source, chunk=chunk) -> None:
                accept_facts(
                    batch,
                    source,
                    chunk.spans,
                    state.company_name,
                    tolerate_numeric_or_period=state.repair_used,
                    canonicalize_quotes=False,
                )

            def validate_retry(batch: EvidenceBatch, source=source, chunk=chunk) -> None:
                accept_facts(
                    batch,
                    source,
                    chunk.spans,
                    state.company_name,
                    tolerate_numeric_or_period=True,
                    canonicalize_quotes=False,
                )

            specs.append(
                CallSpec(
                    chunk.key,
                    "extract_evidence",
                    self._chunk_data(state, chunk),
                    EvidenceBatch,
                    validate,
                    retry_validate=validate_retry,
                )
            )
        results = await self._validated_batch(
            state, specs, self.settings.pipeline.extraction_concurrency
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            permanent = next(
                (
                    error
                    for error in failures
                    if not isinstance(error, TaskFailure) or not error.retryable
                ),
                None,
            )
            raise permanent or failures[0]
        self._select_facts(state)
        facts = [fact.model_dump(mode="json") for fact in state.facts.values()]
        sources = [source.record.model_dump(mode="json") for source in self._usable_sources(state)]
        source_ids = {source["id"] for source in sources}
        progress(
            "consolidate_evidence", "Reconciling the accepted facts into a shared company dossier."
        )

        def validate_dossier(value: CompanyDossier) -> None:
            identity_rebound = check_dossier(
                value,
                state.facts,
                source_ids,
                state.company_name,
            )
            if identity_rebound:
                logger.info(
                    "Insight dossier identity citation normalized: status=completed fact_count=1"
                )

        dossier = await self._one(
            state,
            CallSpec(
                "dossier",
                "consolidate_evidence",
                {
                    "company_name": state.company_name,
                    "facts": facts,
                    "sources": sources,
                    "evidence_gaps": [
                        gap.model_dump()
                        for gap in state.gaps
                        if gap.area
                        in {
                            "official_news",
                            "collection",
                            "source_coverage",
                            "company_boundary",
                            "evidence",
                            "evidence_coverage",
                            "Numeric and period warnings",
                        }
                    ],
                },
                CompanyDossier,
                validate_dossier,
            ),
        )
        assert isinstance(dossier, CompanyDossier)
        progress("synthesize_strategy", "Developing a shared, evidence-backed opportunity brief.")
        strategy = await self._one(
            state,
            CallSpec(
                "strategy",
                "synthesize_strategy",
                {
                    "company_name": state.company_name,
                    "dossier": dossier.model_dump(mode="json"),
                    "facts": facts,
                    "requested_audiences": request.audiences,
                    "seller_context": {"provided_by_user": request.seller_context or None},
                },
                StrategyBrief,
                lambda value: check_strategy(value, state.facts),
            ),
        )
        assert isinstance(strategy, StrategyBrief)
        if not strategy.opportunities:
            self._gap(
                state,
                "opportunities",
                "No supported opportunities were identified from the available company evidence.",
            )
        state.opportunities = {
            stable_id("opp", candidate.model_dump()): Opportunity(
                id=stable_id("opp", candidate.model_dump()), **candidate.model_dump()
            )
            for candidate in sorted(
                strategy.opportunities, key=lambda candidate: candidate.priority
            )
        }
        progress(
            "generate_talk_points", "Preparing separate, grounded executive-audience conversations."
        )
        audience_specs = []
        for audience in request.audiences:
            key = f"audience_{audience}"
            if key in state.terminal_tasks:
                state.executor.unprotect(key)
                continue
            data = {
                "company_name": state.company_name,
                "audience": audience,
                "audience_brief": AUDIENCE_BRIEFS[audience],
                "required_count": self.settings.pipeline.talk_points_per_audience,
                "delivery_policy": {
                    "mode": "opportunity_driven",
                    "accepted_opportunities_support_discovery_hypotheses": True,
                    "external_industry_stories_allowed": False,
                    "limited_coverage_requires_correction_attempt": True,
                },
                "dossier": dossier.model_dump(mode="json"),
                "strategic_summary": [item.model_dump() for item in strategy.strategic_summary],
                "opportunities": [
                    item.model_dump(mode="json") for item in state.opportunities.values()
                ],
                "facts": facts,
                "seller_context": {"provided_by_user": request.seller_context or None},
            }

            def validate(value: AudienceTalkPoints, audience=audience) -> None:
                check_audience(
                    value,
                    audience,
                    self.settings.pipeline.talk_points_per_audience,
                    state.facts,
                    state.opportunities,
                )

            audience_specs.append(
                CallSpec(key, "generate_talk_points", data, AudienceTalkPoints, validate)
            )
        results = await self._batch(
            state, audience_specs, self.settings.pipeline.audience_concurrency
        )
        results = await self._improve_audience_results(
            state,
            audience_specs,
            results,
            progress,
        )
        for spec, result in zip(audience_specs, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, InsightError):
                state.executor.unprotect(spec.key)
                if not isinstance(result, TaskFailure):
                    state.failures[spec.key] = TaskFailure(
                        result.code, result.public_message, task_key=spec.key
                    )
                    state.terminal_tasks.add(spec.key)
                self._gap(
                    state,
                    "audience_coverage",
                    f"{spec.data['audience'].upper()} could not be accepted: {result.public_message}",
                )
            elif isinstance(result, BaseException):
                raise result
            else:
                state.gaps = [
                    gap
                    for gap in state.gaps
                    if not (
                        gap.area == "audience_coverage"
                        and gap.detail.startswith(spec.data["audience"].upper() + " ")
                    )
                ]
        accepted = sum(f"audience_{audience}" in state.artifacts for audience in request.audiences)
        progress(
            "generate_talk_points",
            f"Accepted typed artifacts for {accepted} of {len(request.audiences)} audiences.",
        )

    def _quantity_gaps(self, state: Checkpoint) -> list[EvidenceGap]:
        count = sum(len(fact.warnings) for fact in state.facts.values())
        if not count:
            return []
        return [
            EvidenceGap(
                area="Quantity warnings",
                detail=(
                    f"{count} extracted quantity value(s) could not be matched exactly to their "
                    "source quotations. Retained values are flagged in the evidence section; "
                    "verify them against the cited text before use."
                ),
            )
        ]

    def _unique_gaps(self, gaps: Iterable[EvidenceGap]) -> list[EvidenceGap]:
        unique = {(gap.area, gap.detail): gap for gap in gaps}
        values = list(unique.values())
        if len(values) > 80:
            return [
                *values[:79],
                EvidenceGap(
                    area="coverage",
                    detail=f"{len(values) - 79} additional repeated or lower-priority limitations were omitted from the compact report.",
                ),
            ]
        return values

    def _candidate(
        self, report_id: str, state: Checkpoint, request: InsightRequest
    ) -> AcceptedReport:
        dossier = state.artifacts["dossier"].value
        strategy = state.artifacts["strategy"].value
        assert isinstance(dossier, CompanyDossier)
        assert isinstance(strategy, StrategyBrief)
        audiences = [
            state.artifacts[f"audience_{audience}"].value
            for audience in request.audiences
            if f"audience_{audience}" in state.artifacts
        ]
        assert all(isinstance(audience, AudienceTalkPoints) for audience in audiences)
        coverage = [
            AudienceCoverage(
                audience=audience,
                status=next(
                    (output.coverage for output in audiences if output.audience == audience),
                    "unavailable",
                ),
            )
            for audience in request.audiences
        ]
        model_gaps = []
        for artifact in state.artifacts.values():
            if isinstance(
                artifact.value,
                (EvidenceBatch, NewsDiscovery, CompanyDossier, StrategyBrief, AudienceTalkPoints),
            ):
                model_gaps.extend(artifact.value.evidence_gaps)
        if state.generated_at is None:
            state.generated_at = datetime.now(UTC)
        candidate = AcceptedReport(
            report_id=report_id,
            generated_at=state.generated_at,
            as_of_date=state.as_of_date,
            status="completed"
            if all(item.status == "complete" for item in coverage)
            else "partial",
            requested_audiences=request.audiences,
            audience_coverage=coverage,
            company_name=dossier.entity_resolution.company_name,
            company=dossier.company_profile,
            executive_summary=dossier.executive_summary,
            opportunities=list(state.opportunities.values()),
            audiences=audiences,
            evidence_gaps=self._unique_gaps(
                [*self._quantity_gaps(state), *state.gaps, *model_gaps]
            ),
            contradictions=dossier.contradictions,
            facts=list(state.facts.values()),
            sources=[source.record for source in self._usable_sources(state)],
            source_outcomes=list(state.outcomes.values()),
            announcements=list(state.announcements),
            news_status="found" if state.announcements else "none_found",
        )
        used = referenced_facts(candidate) | state.news_facts.keys()
        facts = [fact for fact in candidate.facts if fact.id in used]
        source_ids = {quote.source_id for fact in facts for quote in fact.evidence}
        source_ids.add(state.homepage_id)
        source_ids.update(announcement.source_id for announcement in candidate.announcements)
        candidate = candidate.model_copy(
            update={
                "facts": facts,
                "sources": [source for source in candidate.sources if source.id in source_ids],
            }
        )
        check_report(candidate)
        return candidate

    def _sections(self, candidate: AcceptedReport) -> list[dict]:
        sections = [
            {
                "section_id": "section_company",
                "owner": "dossier",
                "fields": ["company_name", "company"],
            },
            {"section_id": "section_summary", "owner": "dossier", "fields": ["executive_summary"]},
            {"section_id": "section_evidence", "owner": "evidence", "fields": ["facts"]},
            {
                "section_id": "section_sources",
                "owner": "server",
                "fields": ["sources", "source_outcomes"],
            },
            {
                "section_id": "section_limitations",
                "owner": "limitations",
                "fields": ["evidence_gaps"],
            },
            {
                "section_id": "section_announcements",
                "owner": "verify_news",
                "fields": ["announcements", "news_status"],
            },
        ]
        if candidate.contradictions:
            sections.append(
                {
                    "section_id": "section_contradictions",
                    "owner": "dossier",
                    "fields": ["contradictions"],
                }
            )
        for opportunity in candidate.opportunities:
            sections.append(
                {
                    "section_id": f"section_{opportunity.id}",
                    "owner": "strategy",
                    "opportunity_id": opportunity.id,
                    "fact_ids": sorted(referenced_facts(opportunity)),
                }
            )
        for audience in candidate.requested_audiences:
            sections.append(
                {
                    "section_id": f"section_coverage_{audience}",
                    "owner": f"audience_{audience}",
                    "audience": audience,
                    "fields": ["audience_coverage"],
                }
            )
        for audience in candidate.audiences:
            sections.append(
                {
                    "section_id": f"section_audience_{audience.audience}",
                    "owner": f"audience_{audience.audience}",
                    "audience": audience.audience,
                    "opportunity_ids": sorted(
                        {point.opportunity_id for point in audience.talk_points}
                    ),
                    "fact_ids": sorted(referenced_facts(audience)),
                }
            )
        return sections

    def _review_data(
        self, state: Checkpoint, request: InsightRequest, candidate: AcceptedReport
    ) -> dict:
        excerpts = {}
        for fact in candidate.facts:
            for citation in fact.evidence:
                source = state.documents[citation.source_id]
                span = next((span for span in source.spans if span.id == citation.span_id), None)
                if span is None or citation.quote not in span.text:
                    raise ArtifactError(
                        "quote_mismatch",
                        "A review citation no longer matches its original excerpt.",
                    )
                start = span.text.index(citation.quote)
                context = span.text[max(0, start - 120) : start + len(citation.quote) + 120]
                excerpts[(source.id, span.id, context)] = {
                    "source_id": source.id,
                    "span_id": span.id,
                    "text": context,
                    "page": span.page,
                    "section": span.section,
                }
        return {
            "candidate": candidate.model_dump(
                mode="json", exclude={"report_id", "generated_at", "facts"}
            ),
            "sections": self._sections(candidate),
            "section_ids": [section["section_id"] for section in self._sections(candidate)],
            "evidence": [fact.model_dump(mode="json") for fact in candidate.facts],
            "original_excerpts": list(excerpts.values()),
            "requested_audiences": request.audiences,
            "required_count": self.settings.pipeline.talk_points_per_audience,
            "deterministic_gates": {
                "company_identity": "matched",
                "references_and_exact_quotes": "passed",
                "official_news_dates": "passed",
                "coverage_is_server_derived": True,
            },
        }

    async def _review(
        self,
        state: Checkpoint,
        request: InsightRequest,
        candidate: AcceptedReport,
        progress: Progress,
    ) -> QualityReview:
        progress(
            "review_report", "Reviewing every report section against its original cited excerpts."
        )
        data = self._review_data(state, request, candidate)

        def validate(value: QualityReview) -> None:
            check_review(
                value,
                set(data["section_ids"]),
                {fact.id for fact in candidate.facts},
            )

        spec = CallSpec("review", "review_report", data, QualityReview, validate)
        try:
            result = await self._call(state, spec)
        except StageInvalid as error:
            if not self.settings.pipeline.max_repair_rounds:
                raise
            logger.info(
                "Insight quality review contract correction: code=%s",
                error.code,
            )
            retry_spec = CallSpec(
                "review_contract_retry",
                "review_report",
                {
                    **data,
                    "repair_feedback": [error.feedback],
                    "review_correction": {"attempt": 1},
                },
                QualityReview,
                validate,
            )
            try:
                result = await self._call(state, retry_spec)
            except TaskFailure as retry_error:
                self._discard_temporary_task(state, retry_spec.key)
                mapped = TaskFailure(
                    retry_error.code,
                    retry_error.public_message,
                    retryable=retry_error.retryable,
                    task_key=spec.key,
                    contract_details=retry_error.contract_details,
                )
                state.failures[spec.key] = mapped
                if not mapped.retryable:
                    state.terminal_tasks.add(spec.key)
                raise mapped from None
            except BaseException:
                self._discard_temporary_task(state, retry_spec.key)
                raise
            self._promote_temporary_artifact(state, retry_spec.key, spec, result)
        assert isinstance(result, QualityReview)
        decisions = {item.section_id: item.decision for item in result.section_reviews}
        required = set(CORE_SECTIONS)
        if candidate.contradictions:
            required.add("section_contradictions")
        core = ",".join(
            f"{section.removeprefix('section_')}={decisions[section]}"
            for section in sorted(required)
        )
        issue_codes = ",".join(sorted({issue.code for issue in result.issues})) or "none"
        issue_sections = (
            ",".join(
                f"{issue.section_id.removeprefix('section_')}:{issue.code}:"
                f"facts={len(issue.fact_ids)}"
                for issue in result.issues
            )
            or "none"
        )
        log = (
            logger.warning
            if any(decisions[section] != "accept" for section in required)
            else logger.info
        )
        log(
            "Insight quality review result: decision=%s accepted=%d repair=%d rejected=%d "
            "issues=%d issue_codes=%s issue_sections=%s core=%s artifact_repair_used=%s "
            "artifact_repair_origin=%s",
            result.decision,
            sum(value == "accept" for value in decisions.values()),
            sum(value == "repair" for value in decisions.values()),
            sum(value == "reject" for value in decisions.values()),
            len(result.issues),
            issue_codes,
            issue_sections,
            core,
            str(state.repair_used).lower(),
            state.repair_origin or "none",
        )
        return result

    def _review_targets(self, state: Checkpoint, review: QualityReview) -> dict[str, list[dict]]:
        targets: dict[str, list[dict]] = {}
        for issue in review.issues:
            if issue.code == "insufficient_evidence":
                continue
            owners: set[str] = set()
            section = issue.section_id
            if section in {"section_company", "section_summary", "section_contradictions"}:
                owners.add("dossier")
            elif section.startswith("section_opp_"):
                owners.add("strategy")
            elif section.startswith(("section_audience_", "section_coverage_")):
                audience = section.rsplit("_", 1)[-1]
                key = f"audience_{audience}"
                artifact = state.artifacts.get(key)
                if artifact is not None:
                    output = artifact.value
                    assert isinstance(output, AudienceTalkPoints)
                    if not (
                        output.coverage != "complete"
                        and issue.code in {"wrong_point_count", "missing_audience"}
                    ):
                        owners.add(key)
            elif section == "section_evidence":
                owners.update(
                    state.fact_owners[fact_id]
                    for fact_id in issue.fact_ids
                    if fact_id in state.fact_owners
                )
                if not owners:
                    owners.update(chunk.key for chunk in state.chunks or [])
            elif section == "section_announcements" and state.announcements:
                owners.add("verify_news")
            elif section == "section_limitations":
                owners.update(
                    key
                    for key, artifact in state.artifacts.items()
                    if isinstance(
                        artifact.value,
                        (CompanyDossier, StrategyBrief, AudienceTalkPoints, EvidenceBatch),
                    )
                    and artifact.value.evidence_gaps
                )
            for owner in owners:
                targets.setdefault(owner, []).append(issue.model_dump(mode="json"))
        return targets

    def _publish(
        self,
        state: Checkpoint,
        candidate: AcceptedReport,
        review: QualityReview,
        *,
        allow_failure: bool = False,
    ) -> AcceptedReport | None:
        decisions = {section.section_id: section.decision for section in review.section_reviews}
        required = set(CORE_SECTIONS)
        if candidate.contradictions:
            required.add("section_contradictions")
        if any(decisions[section] != "accept" for section in required):
            if allow_failure:
                return None
            logger.warning(
                "Insight quality review core rejection: sections=%s artifact_repair_used=%s "
                "artifact_repair_origin=%s",
                ",".join(
                    sorted(
                        section.removeprefix("section_")
                        for section in required
                        if decisions[section] != "accept"
                    )
                ),
                str(state.repair_used).lower(),
                state.repair_origin or "none",
            )
            raise InsightError(
                "quality_not_accepted",
                "The company profile or its evidence did not pass quality review. Supply clearer official sources.",
            )
        opportunities = [
            opportunity
            for opportunity in candidate.opportunities
            if decisions[f"section_{opportunity.id}"] == "accept"
        ]
        opportunity_ids = {opportunity.id for opportunity in opportunities}
        audiences = [
            audience
            for audience in candidate.audiences
            if decisions[f"section_audience_{audience.audience}"] == "accept"
            and decisions[f"section_coverage_{audience.audience}"] == "accept"
            and all(point.opportunity_id in opportunity_ids for point in audience.talk_points)
        ]
        coverage = [
            AudienceCoverage(
                audience=audience,
                status=next(
                    (output.coverage for output in audiences if output.audience == audience),
                    "unavailable",
                ),
            )
            for audience in candidate.requested_audiences
        ]
        gaps = list(
            candidate.evidence_gaps if decisions["section_limitations"] == "accept" else state.gaps
        )
        removed = {audience.audience for audience in candidate.audiences} - {
            audience.audience for audience in audiences
        }
        for audience in sorted(removed):
            gaps.append(
                EvidenceGap(
                    area="audience_coverage",
                    detail=f"{audience.upper()} was excluded because its content or a shared dependency was not accepted by quality review.",
                )
            )
        announcements = (
            candidate.announcements if decisions["section_announcements"] == "accept" else []
        )
        if candidate.announcements and not announcements:
            gaps.append(
                EvidenceGap(
                    area="official_news",
                    detail="Announcement presentation did not pass quality review; no unaccepted announcements are published.",
                )
            )
        if decisions["section_limitations"] != "accept":
            gaps.append(
                EvidenceGap(
                    area="quality_review",
                    detail="Unaccepted model-written limitation statements were excluded. Server-observed source and task omissions remain visible.",
                )
            )
        if len(opportunities) != len(candidate.opportunities):
            gaps.append(
                EvidenceGap(
                    area="opportunities",
                    detail="Some proposed opportunities did not pass quality review and were excluded.",
                )
            )
        partial = any(item.status != "complete" for item in coverage) or any(
            value != "accept" for value in decisions.values()
        )
        report = candidate.model_copy(
            update={
                "status": "partial" if partial else "completed",
                "opportunities": opportunities,
                "audiences": audiences,
                "audience_coverage": coverage,
                "announcements": announcements,
                "news_status": "found" if announcements else "none_found",
                "evidence_gaps": self._unique_gaps([*self._quantity_gaps(state), *gaps]),
            }
        )
        used = referenced_facts(report)
        announcement_sources = {announcement.source_id for announcement in announcements}
        used.update(
            fact.id
            for fact in candidate.facts
            if fact.id in state.news_facts
            and any(quote.source_id in announcement_sources for quote in fact.evidence)
        )
        facts = [fact for fact in candidate.facts if fact.id in used]
        source_ids = {
            quote.source_id for fact in facts for quote in fact.evidence
        } | announcement_sources
        source_ids.add(state.homepage_id)
        report = report.model_copy(
            update={
                "facts": facts,
                "sources": [source for source in candidate.sources if source.id in source_ids],
            }
        )
        check_report(report)
        return report
