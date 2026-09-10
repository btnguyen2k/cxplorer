"""Deterministic evidence, coverage, and publication gates."""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Protocol

from pydantic import BaseModel

from cxplorer.insights.errors import InsightError
from cxplorer.insights.schemas import (
    AcceptedReport,
    AudienceTalkPoints,
    Citation,
    CompanyDossier,
    EvidenceBatch,
    EvidenceFact,
    EvidenceQuote,
    FactCandidate,
    GroundedText,
    Opportunity,
    QualityReview,
    QuantityWarning,
    SourceVerification,
    StrategyBrief,
)


class Span(Protocol):
    id: str
    text: str
    page: int | None
    section: str | None


class Document(Protocol):
    id: str
    title: str
    text: str
    spans: Iterable[Span]


class ArtifactError(InsightError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.feedback = {"code": code, "message": message}


@dataclass(frozen=True, slots=True)
class FactAcceptance:
    facts: tuple[EvidenceFact, ...]
    omitted_numeric_or_period: int = 0
    recovered_quotes: int = 0


@dataclass(frozen=True, slots=True)
class _QuoteToken:
    value: str
    start: int
    end: int


def stable_id(prefix: str, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def normalized_entity(value: str) -> str:
    words = re.findall(r"\w+", value.casefold())
    suffixes = {"inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation", "plc"}
    while words and words[-1] in suffixes:
        words.pop()
    return "".join(words)


def entity_in_text(entity: str, text: str) -> bool:
    words = re.findall(r"\w+", entity.casefold())
    suffixes = {"inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation", "plc"}
    while words and words[-1] in suffixes:
        words.pop()
    if not words:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(word) for word in words) + r"\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _numeric_values(text: str) -> set[str]:
    values = set()
    for token in re.findall(r"\b\d+(?:[.,]\d+)*%?", text):
        suffix = "%" if token.endswith("%") else ""
        number = token.removesuffix("%")
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", number):
            number = number.replace(",", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", number):
            integer, separator, fraction = number.partition(".")
            integer = integer.lstrip("0") or "0"
            fraction = fraction.rstrip("0")
            number = integer + (separator + fraction if fraction else "")
        values.add(number + suffix)
    return values


_QUOTE_TOKEN = re.compile(
    r"\d+(?:[.,]\d+)*%?|[^\W\d_]+(?:['\N{RIGHT SINGLE QUOTATION MARK}][^\W\d_]+)?",
    re.UNICODE,
)
_QUOTE_MEANING_GUARDS = {
    "cannot",
    "can't",
    "decrease",
    "decreased",
    "decreases",
    "decreasing",
    "increase",
    "increased",
    "increases",
    "increasing",
    "isn't",
    "less",
    "more",
    "never",
    "no",
    "none",
    "not",
    "without",
    "won't",
}
_QUOTE_MEANING_SYMBOLS = frozenset("$€£¥₹₩₽₫<>≤≥=≠±+@\N{MINUS SIGN}")


def _normalized_quote_token(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKC", value)
        .casefold()
        .replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    )
    numeric = _numeric_values(normalized)
    return next(iter(numeric)) if len(numeric) == 1 else normalized


def _quote_tokens(text: str) -> list[_QuoteToken]:
    return [
        _QuoteToken(_normalized_quote_token(match.group()), match.start(), match.end())
        for match in _QUOTE_TOKEN.finditer(text)
    ]


def _quote_meaning_markers(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text)
    markers = [character for character in normalized if character in _QUOTE_MEANING_SYMBOLS]
    if re.search(r"(?<!\w)-\s*(?:[$€£¥₹₩₽₫]\s*)?\d", normalized):
        markers.append("negative_number")
    if re.search(r"\(\s*(?:[$€£¥₹₩₽₫]\s*)?\d[\d.,]*\s*\)", normalized):
        markers.append("accounting_negative")
    return tuple(sorted(markers))


def _canonical_quote(quote: str, source_text: str) -> str | None:
    if quote in source_text:
        return quote
    requested = _quote_tokens(quote)
    source = _quote_tokens(source_text)
    if not requested or len(requested) > len(source):
        return None
    requested_values = [token.value for token in requested]
    width = len(requested)
    exact_matches = []
    for index in range(len(source) - width + 1):
        if [token.value for token in source[index : index + width]] != requested_values:
            continue
        candidate = source_text[source[index].start : source[index + width - 1].end]
        if _numeric_values(quote) == _numeric_values(candidate) and _quote_meaning_markers(
            quote
        ) == _quote_meaning_markers(candidate):
            exact_matches.append(candidate)
    if exact_matches:
        return exact_matches[0]
    if width < 5:
        return None

    normalized_quote = "".join(requested_values)
    maximum_changes = 1 if width < 12 else 2
    candidates: list[tuple[float, str]] = []
    for index in range(len(source) - width + 1):
        window = source[index : index + width]
        window_values = [token.value for token in window]
        differences = [
            (left, right)
            for left, right in zip(requested_values, window_values, strict=True)
            if left != right
        ]
        if not differences or len(differences) > maximum_changes:
            continue
        if any(
            left in _QUOTE_MEANING_GUARDS
            or right in _QUOTE_MEANING_GUARDS
            or SequenceMatcher(None, left, right, autojunk=False).ratio() < 0.9
            for left, right in differences
        ):
            continue
        candidate = source_text[window[0].start : window[-1].end]
        if _numeric_values(quote) != _numeric_values(candidate) or _quote_meaning_markers(
            quote
        ) != _quote_meaning_markers(candidate):
            continue
        token_score = SequenceMatcher(None, requested_values, window_values, autojunk=False).ratio()
        character_score = SequenceMatcher(
            None,
            normalized_quote,
            "".join(window_values),
            autojunk=False,
        ).ratio()
        if token_score >= 0.85 and character_score >= 0.95:
            candidates.append(((token_score + character_score) / 2, candidate))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    if (
        len(candidates) > 1
        and candidates[0][1] != candidates[1][1]
        and candidates[0][0] - candidates[1][0] < 0.01
    ):
        return None
    return candidates[0][1]


def check_quotes(
    quotes: Iterable[EvidenceQuote],
    spans: Mapping[str, Span],
    *,
    canonicalize: bool = True,
) -> int:
    recovered = 0
    for quote in quotes:
        span = spans.get(quote.span_id)
        if span is None:
            raise ArtifactError("unknown_reference", "A quoted source span is not in this input.")
        canonical = _canonical_quote(quote.quote, span.text) if quote.quote.strip() else None
        if canonical is None:
            raise ArtifactError("quote_mismatch", "A quotation does not match its source text.")
        if canonical != quote.quote:
            recovered += 1
            if canonicalize:
                quote.quote = canonical
    return recovered


def check_source_verification(
    result: SourceVerification,
    documents: Iterable[Document],
    homepage_id: str,
    *,
    company_name: str | None = None,
) -> int:
    documents = list(documents)
    docs = {document.id: document for document in documents}
    ids = [verdict.source_id for verdict in result.verdicts]
    if (
        homepage_id not in docs
        or len(docs) != len(documents)
        or result.homepage_source_id != homepage_id
        or set(ids) != set(docs)
        or len(ids) != len(docs)
    ):
        raise ArtifactError(
            "unknown_reference", "Source verification must cover each supplied source."
        )
    homepage = docs[homepage_id]
    if not entity_in_text(result.company_name, f"{homepage.title} {homepage.text}"):
        raise ArtifactError("entity_mismatch", "The company name is not supported by the homepage.")
    if company_name and normalized_entity(result.company_name) != normalized_entity(company_name):
        raise ArtifactError("entity_mismatch", "Source verification changed the target company.")
    recovered_quotes = 0
    for verdict in result.verdicts:
        spans = {span.id: span for span in docs[verdict.source_id].spans}
        recovered_quotes += check_quotes(verdict.evidence, spans)
        recovered_quotes += check_quotes(verdict.date_evidence, spans)
        recovered_quotes += check_quotes(verdict.executive_evidence, spans)
        if verdict.relation == "related" and not verdict.evidence:
            raise ArtifactError(
                "entity_mismatch",
                "Related sources need an exact supporting quotation from the source.",
            )
        if verdict.relation == "related" and not entity_in_text(
            result.company_name,
            f"{docs[verdict.source_id].title} {docs[verdict.source_id].text}",
        ):
            raise ArtifactError(
                "entity_mismatch",
                "A related source must identify the homepage company in its retained title or text.",
            )
        if verdict.executive_role and not verdict.executive_name:
            raise ArtifactError(
                "incorrect_attribution", "An executive role needs an identified, quoted executive."
            )
        if verdict.executive_name and (
            not verdict.executive_evidence
            or not entity_in_text(
                verdict.executive_name, " ".join(q.quote for q in verdict.executive_evidence)
            )
        ):
            raise ArtifactError(
                "quote_mismatch", "Executive attribution needs a supporting quotation."
            )
        if verdict.executive_role and not entity_in_text(
            verdict.executive_role, " ".join(q.quote for q in verdict.executive_evidence)
        ):
            raise ArtifactError(
                "quote_mismatch", "The executive's role needs a supporting quotation."
            )
    return recovered_quotes


def check_quantities(fact: FactCandidate | EvidenceFact) -> list[QuantityWarning]:
    warnings = []
    for index, quantity in enumerate(fact.quantities):
        quoted_spans = [quote.quote for quote in fact.evidence if quote.span_id == quantity.span_id]
        if not quoted_spans:
            raise ArtifactError(
                "unknown_reference",
                "A quantity references a span outside its fact's supporting quotations.",
            )
        pattern = (
            r"(?<![\d.,])" + re.escape(quantity.numeric_text) + r"(?![\d.,])"
            if re.search(r"\d", quantity.numeric_text)
            else r"(?<!\w)" + re.escape(quantity.numeric_text) + r"(?!\w)"
        )
        quantity_quotes = [quote for quote in quoted_spans if re.search(pattern, quote)]
        if not quantity_quotes:
            warnings.append(
                QuantityWarning(
                    quantity_index=index,
                    span_id=quantity.span_id,
                    numeric_text=quantity.numeric_text,
                    detail=(
                        f"Quantity '{quantity.numeric_text}' does not exactly match its supporting "
                        "source quotation. Verify the quoted figure before using this value."
                    ),
                )
            )
        for component in (quantity.unit, quantity.currency, quantity.period):
            if component and not any(
                component.casefold() in quotation.casefold()
                for quotation in quantity_quotes or quoted_spans
            ):
                raise ArtifactError(
                    "numeric_or_period_mismatch",
                    "A quantity's unit, currency, or period is not in its supporting quotation.",
                )
    return warnings


def accept_facts(
    batch: EvidenceBatch,
    document: Document,
    selected_spans: Iterable[Span],
    company_name: str,
    *,
    tolerate_numeric_or_period: bool = False,
    canonicalize_quotes: bool = True,
) -> FactAcceptance:
    spans = {span.id: span for span in selected_spans}
    accepted: list[EvidenceFact] = []
    omitted_numeric_or_period = 0
    recovered_quotes = 0
    for fact in batch.facts:
        try:
            if normalized_entity(fact.entity) != normalized_entity(company_name):
                raise ArtifactError(
                    "entity_mismatch", "A fact is attributed to a different company."
                )
            fact_recovered_quotes = check_quotes(
                fact.evidence, spans, canonicalize=canonicalize_quotes
            )
            quoted_text = " ".join(quote.quote for quote in fact.evidence)
            if not _numeric_values(fact.statement) <= _numeric_values(quoted_text):
                raise ArtifactError(
                    "numeric_or_period_mismatch",
                    "A fact contains a number absent from its supporting quotations.",
                )
            if fact.time_context and not _numeric_values(fact.time_context) <= _numeric_values(
                quoted_text
            ):
                raise ArtifactError(
                    "numeric_or_period_mismatch",
                    "A fact's time context contains a period absent from its quotations.",
                )
            warnings = check_quantities(fact)
            evidence = [
                Citation(
                    source_id=document.id,
                    span_id=quote.span_id,
                    quote=quote.quote,
                    page=spans[quote.span_id].page,
                    section=spans[quote.span_id].section,
                )
                for quote in fact.evidence
            ]
            contents = fact.model_dump(exclude={"evidence"})
            identifier = stable_id(
                "fact",
                {"source": document.id, **contents, "evidence": [q.model_dump() for q in evidence]},
            )
            accepted.append(
                EvidenceFact(id=identifier, evidence=evidence, warnings=warnings, **contents)
            )
            recovered_quotes += fact_recovered_quotes
        except ArtifactError as error:
            if not tolerate_numeric_or_period or error.code != "numeric_or_period_mismatch":
                raise
            omitted_numeric_or_period += 1
    return FactAcceptance(tuple(accepted), omitted_numeric_or_period, recovered_quotes)


def model_nodes(value: object) -> Iterable[BaseModel]:
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from model_nodes(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from model_nodes(item)


def referenced_facts(value: object) -> set[str]:
    result: set[str] = set()
    for node in model_nodes(value):
        for name in ("fact_ids", "baseline_fact_ids"):
            references = getattr(node, name, None)
            if isinstance(references, list):
                result.update(reference for reference in references if isinstance(reference, str))
    return result


def check_grounding(artifact: BaseModel, facts: Mapping[str, EvidenceFact]) -> None:
    if not referenced_facts(artifact) <= facts.keys():
        raise ArtifactError(
            "unknown_reference", "The artifact references a fact outside this report."
        )
    for claim in model_nodes(artifact):
        if not isinstance(claim, GroundedText) or claim.basis != "source_backed" or not claim.text:
            continue
        evidence = " ".join(
            quote.quote for fact_id in claim.fact_ids for quote in facts[fact_id].evidence
        )
        if not _numeric_values(claim.text) <= _numeric_values(evidence):
            raise ArtifactError(
                "numeric_or_period_mismatch",
                "A source-backed statement contains a number absent from its cited excerpts.",
            )


def check_dossier(
    dossier: CompanyDossier,
    facts: Mapping[str, EvidenceFact],
    source_ids: set[str],
    company_name: str,
) -> bool:
    check_grounding(dossier, facts)
    resolution = dossier.entity_resolution
    if resolution.status != "matched":
        raise ArtifactError(
            "insufficient_evidence",
            "Company identity remains ambiguous. Supply clearer official company sources.",
        )
    if not set(resolution.included_source_ids) <= source_ids or any(
        excluded.source_id not in source_ids for excluded in resolution.excluded_sources
    ):
        raise ArtifactError(
            "unknown_reference", "The dossier references an unknown company source."
        )
    included = set(resolution.included_source_ids)
    excluded = [item.source_id for item in resolution.excluded_sources]
    if (
        len(included) != len(resolution.included_source_ids)
        or len(set(excluded)) != len(excluded)
        or included.intersection(excluded)
    ):
        raise ArtifactError(
            "entity_mismatch", "Included and excluded company sources must be distinct."
        )
    if any(
        quote.source_id not in included
        for fact_id in referenced_facts(dossier)
        for quote in facts[fact_id].evidence
    ):
        raise ArtifactError("entity_mismatch", "The dossier cites a source it has not included.")
    if (
        resolution.company_name.text is None
        or resolution.company_name.basis != "source_backed"
        or normalized_entity(resolution.company_name.text) != normalized_entity(company_name)
    ):
        raise ArtifactError("entity_mismatch", "The dossier changed the verified company identity.")
    identity_rebound = False
    if not entity_in_text(
        company_name,
        " ".join(
            quote.quote
            for fact_id in resolution.company_name.fact_ids
            for quote in facts[fact_id].evidence
        ),
    ):
        identifying_facts = sorted(
            (
                fact
                for fact in facts.values()
                if fact.evidence
                and all(quote.source_id in included for quote in fact.evidence)
                and any(entity_in_text(company_name, quote.quote) for quote in fact.evidence)
            ),
            key=lambda fact: (
                fact.category != "company",
                fact.category not in {"company", "offering", "product_workflow"},
                fact.id,
            ),
        )
        if not identifying_facts:
            raise ArtifactError(
                "entity_mismatch", "The company name must cite identifying company evidence."
            )
        resolution.company_name = resolution.company_name.model_copy(
            update={"fact_ids": [identifying_facts[0].id]}
        )
        identity_rebound = True
    if (
        dossier.company_profile.business_description.basis != "source_backed"
        or dossier.company_profile.business_description.text is None
        or not dossier.executive_summary
    ):
        raise ArtifactError(
            "insufficient_evidence",
            "There is not enough evidence for a company profile and summary. Supply better sources.",
        )
    return identity_rebound


def check_strategy(strategy: StrategyBrief, facts: Mapping[str, EvidenceFact]) -> None:
    check_grounding(strategy, facts)
    priorities = [item.priority for item in strategy.opportunities]
    if len(priorities) != len(set(priorities)):
        raise ArtifactError(
            "generic_talk_point", "Opportunities must have distinct priority ranks."
        )
    for opportunity in strategy.opportunities:
        if opportunity.company_signal.basis != "source_backed":
            raise ArtifactError(
                "unsupported_claim", "An opportunity needs a source-backed company signal."
            )
        if opportunity.hypothesis.basis not in {"hypothesis", "analysis"}:
            raise ArtifactError(
                "unsupported_claim", "A proposed opportunity must not be asserted as fact."
            )
        if opportunity.business_outcome.basis not in {"hypothesis", "analysis"}:
            raise ArtifactError(
                "unsupported_claim", "A proposed business outcome is not an existing result."
            )
        for criterion in opportunity.success_criteria:
            if criterion.target is not None:
                raise ArtifactError(
                    "numeric_or_period_mismatch",
                    "Do not invent success targets. Leave targets null until a user agrees them.",
                )
            if any(
                not any(quantity.kind == "actual" for quantity in facts[fact_id].quantities)
                for fact_id in criterion.baseline_fact_ids
            ):
                raise ArtifactError(
                    "numeric_or_period_mismatch",
                    "Baseline references must identify actual, quoted measurements.",
                )
        if not any(
            getattr(opportunity.prerequisites, name)
            for name in type(opportunity.prerequisites).model_fields
        ):
            raise ArtifactError(
                "missing_prerequisite", "An opportunity needs concrete prerequisites."
            )


def check_audience(
    output: AudienceTalkPoints,
    audience: str,
    count: int,
    facts: Mapping[str, EvidenceFact],
    opportunities: Mapping[str, Opportunity],
) -> None:
    check_grounding(output, facts)
    if output.audience != audience:
        raise ArtifactError(
            "missing_audience", "The response is for a different executive audience."
        )
    actual = len(output.talk_points)
    if (
        (output.coverage == "complete" and actual != count)
        or (output.coverage == "limited" and not 0 < actual < count)
        or (output.coverage == "insufficient_evidence" and actual != 0)
        or actual > count
        or (output.coverage != "complete" and not output.evidence_gaps)
    ):
        raise ArtifactError("wrong_point_count", "Audience coverage and talk-point count disagree.")
    lines = [
        " ".join((point.seller_line.text or "").casefold().split()) for point in output.talk_points
    ]
    if len(set(lines)) != len(lines):
        raise ArtifactError("generic_talk_point", "An audience contains duplicate talk points.")
    if any(point.opportunity_id not in opportunities for point in output.talk_points):
        raise ArtifactError("unknown_reference", "A talk point refers to an unknown opportunity.")


def check_review(review: QualityReview, section_ids: set[str], fact_ids: set[str]) -> None:
    covered = [item.section_id for item in review.section_reviews]
    if set(covered) != section_ids or len(covered) != len(section_ids):
        raise ArtifactError("unknown_reference", "Review every supplied section exactly once.")
    if any(
        issue.section_id not in section_ids or not set(issue.fact_ids) <= fact_ids
        for issue in review.issues
    ):
        raise ArtifactError(
            "unknown_reference", "A review issue references an unknown section or fact."
        )
    dispositions = {item.section_id: item.decision for item in review.section_reviews}
    issue_sections = {issue.section_id for issue in review.issues}
    server_owned = {"section_sources"} & section_ids
    if any(dispositions[section] != "accept" for section in server_owned) or any(
        issue.section_id in server_owned for issue in review.issues
    ):
        raise ArtifactError(
            "unsupported_claim",
            "Server-owned source metadata already passed deterministic checks and must be accepted.",
        )
    evidence_core = {
        "section_company",
        "section_summary",
        "section_evidence",
        "section_contradictions",
    } & section_ids
    if any(
        issue.section_id in evidence_core and issue.code == "insufficient_evidence"
        for issue in review.issues
    ):
        raise ArtifactError(
            "unsupported_claim",
            "Core evidence cannot be rejected as vaguely insufficient after deterministic "
            "minimum-evidence checks; identify the unsupported claim and its fact IDs.",
        )
    if any(
        dispositions[section] != "accept"
        and not any(issue.section_id == section and issue.fact_ids for issue in review.issues)
        for section in evidence_core
    ):
        raise ArtifactError(
            "unsupported_claim",
            "A rejected core evidence section must identify the affected fact IDs.",
        )
    if any(
        decision != "accept" and section not in issue_sections
        for section, decision in dispositions.items()
    ):
        raise ArtifactError(
            "unsupported_claim", "Rejected sections must have specific review issues."
        )
    if any(dispositions[section] == "accept" for section in issue_sections):
        raise ArtifactError(
            "unsupported_claim", "An accepted section cannot have a blocking issue."
        )
    if review.decision == "accept" and (
        review.issues or any(value != "accept" for value in dispositions.values())
    ):
        raise ArtifactError(
            "unsupported_claim", "An accepted review cannot contain blocking issues."
        )


def check_report(report: AcceptedReport) -> None:
    facts = {fact.id: fact for fact in report.facts}
    sources = {source.id for source in report.sources}
    opportunities = {opportunity.id: opportunity for opportunity in report.opportunities}
    if (
        len(facts) != len(report.facts)
        or len(sources) != len(report.sources)
        or len(opportunities) != len(report.opportunities)
    ):
        raise ArtifactError("unknown_reference", "The report contains duplicate identifiers.")
    check_grounding(report, facts)
    requested = set(report.requested_audiences)
    coverage = {item.audience: item.status for item in report.audience_coverage}
    audiences = {item.audience: item for item in report.audiences}
    if (
        len(requested) != len(report.requested_audiences)
        or len(coverage) != len(report.audience_coverage)
        or coverage.keys() != requested
        or len(audiences) != len(report.audiences)
        or not audiences.keys() <= requested
    ):
        raise ArtifactError(
            "missing_audience", "The report must account for each requested audience."
        )
    for audience, status in coverage.items():
        output = audiences.get(audience)
        if (output is None and status != "unavailable") or (
            output is not None and output.coverage != status
        ):
            raise ArtifactError(
                "missing_audience", "Audience coverage does not match retained content."
            )
        if output is not None and (
            (output.coverage == "complete" and not output.talk_points)
            or (output.coverage == "limited" and not output.talk_points)
            or (output.coverage == "insufficient_evidence" and output.talk_points)
            or (output.coverage != "complete" and not output.evidence_gaps)
        ):
            raise ArtifactError("wrong_point_count", "Retained audience coverage is inconsistent.")
    if report.status == "completed" and any(status != "complete" for status in coverage.values()):
        raise ArtifactError(
            "missing_audience", "A completed report must cover all requested audiences."
        )
    if (
        not facts
        or report.company_name.basis != "source_backed"
        or not report.company_name.text
        or report.company.business_description.basis != "source_backed"
        or not report.company.business_description.text
        or not report.executive_summary
    ):
        raise ArtifactError("insufficient_evidence", "The report has no accepted company core.")
    for fact in report.facts:
        if any(quote.source_id not in sources for quote in fact.evidence):
            raise ArtifactError(
                "unknown_reference", "A retained citation has no source in this report."
            )
        if normalized_entity(fact.entity) != normalized_entity(report.company_name.text):
            raise ArtifactError(
                "entity_mismatch", "The report mixes evidence from another company."
            )
        expected_warnings = check_quantities(fact)
        if [warning.model_dump(exclude={"detail"}) for warning in fact.warnings] != [
            warning.model_dump(exclude={"detail"}) for warning in expected_warnings
        ]:
            raise ArtifactError(
                "numeric_or_period_mismatch",
                "Quantity warnings must match the retained numeric metadata and quotations.",
            )
    if any(
        point.opportunity_id not in opportunities
        for audience in report.audiences
        for point in audience.talk_points
    ):
        raise ArtifactError("unknown_reference", "A retained audience has a missing opportunity.")
    if any(announcement.source_id not in sources for announcement in report.announcements):
        raise ArtifactError("unknown_reference", "An announcement has no source in this report.")
    if len({item.source_id for item in report.announcements}) != len(report.announcements) or (
        report.news_status == "found"
    ) != bool(report.announcements):
        raise ArtifactError(
            "unknown_reference", "Announcement coverage must match accepted articles."
        )
    records = {source.id: source for source in report.sources}
    for announcement in report.announcements:
        source = records[announcement.source_id]
        if (
            not report.as_of_date - timedelta(days=90)
            <= announcement.published_at
            <= report.as_of_date
            or source.published_at != announcement.published_at
            or not source.is_news
        ):
            raise ArtifactError(
                "numeric_or_period_mismatch",
                "Announcements require verified official publication dates in the last 90 days.",
            )
        excerpts = " ".join(
            quote.quote
            for fact in report.facts
            for quote in fact.evidence
            if quote.source_id == announcement.source_id
        )
        for attribution in (announcement.executive_name, announcement.executive_role):
            if attribution and not entity_in_text(attribution, excerpts):
                raise ArtifactError(
                    "incorrect_attribution", "An announcement's executive attribution is not cited."
                )
        if announcement.executive_role and not announcement.executive_name:
            raise ArtifactError(
                "incorrect_attribution", "An executive role has no identified executive."
            )


def dates_in_text(text: str) -> set[date]:
    values: set[date] = set()
    for value in re.findall(r"\b(\d{4}-\d{2}-\d{2})(?:\b|T(?=\d{2}:))", text):
        try:
            values.add(date.fromisoformat(value))
        except ValueError:
            continue
    months = (
        "January|February|March|April|May|June|July|August|September|October|November|December|"
        "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )
    normalized = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    pattern = rf"\b(?:({months})\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+({months})\s+\d{{4}})\b"
    for match in re.finditer(pattern, normalized, re.IGNORECASE):
        value = re.sub(r"\bsept\b", "Sep", match.group(0).replace(",", ""), flags=re.IGNORECASE)
        for format_string in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
            try:
                values.add(datetime.strptime(value, format_string).date())
                break
            except ValueError:
                continue
    return values
