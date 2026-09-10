"""Browser cache integrity, expiry, and bounded decompression."""

import base64
import gzip
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from cxplorer.insights.cache import MAX_REPORT_BYTES, ReportCache
from cxplorer.insights.errors import CacheError
from cxplorer.insights.schemas import CachedReport
from tests.insights_fixtures import report, report_with_quantity_warning

SECRET = "contoso-test-session-secret-long-enough-for-signing"
OWNER = hashlib.sha256(b"seller@contoso.example").hexdigest()
FINGERPRINT = "c" * 64
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def signed_raw(compressed: bytes) -> str:
    encoded = base64.urlsafe_b64encode(compressed)
    key = hmac.digest(SECRET.encode(), b"cxplorer-signed-browser-report", "sha256")
    return encoded.decode() + "." + hmac.new(key, encoded, hashlib.sha256).hexdigest()


def test_report_restores_after_a_new_server_instance_without_identity_data() -> None:
    payload = ReportCache(SECRET, clock=lambda: NOW).encode(
        report(generated_at=NOW), OWNER, FINGERPRINT
    )
    restored = ReportCache(SECRET, clock=lambda: NOW + timedelta(days=1)).decode(
        payload["blob"], OWNER
    )
    assert restored.report.company_name.text == "Contoso"
    assert restored.expires_at == NOW + timedelta(days=7)
    assert "email" not in restored.model_dump_json()
    assert "csrf" not in restored.model_dump_json()
    assert len(payload["blob"]) < len(restored.model_dump_json())


def test_quantity_warnings_survive_a_signed_cache_round_trip() -> None:
    value = report_with_quantity_warning(generated_at=NOW)
    payload = ReportCache(SECRET, clock=lambda: NOW).encode(value, OWNER, FINGERPRINT)
    restored = ReportCache(SECRET, clock=lambda: NOW + timedelta(days=1)).decode(
        payload["blob"], OWNER
    )
    assert restored.report.facts[0].warnings == value.facts[0].warnings
    assert restored.report.facts[0].quantities[0].numeric_text == "5"


@pytest.mark.parametrize("removed", [True, False], ids=["missing-warning", "changed-warning"])
def test_numeric_mismatch_cannot_be_cached_with_missing_or_inconsistent_warnings(removed):
    value = report_with_quantity_warning(generated_at=NOW)
    fact = value.facts[0]
    warnings = [] if removed else [fact.warnings[0].model_copy(update={"numeric_text": "500"})]
    value = value.model_copy(update={"facts": [fact.model_copy(update={"warnings": warnings})]})
    with pytest.raises(CacheError) as error:
        ReportCache(SECRET, clock=lambda: NOW).encode(value, OWNER, FINGERPRINT)
    assert error.value.code == "invalid_report"


def test_older_reports_without_a_warning_field_still_restore():
    envelope = CachedReport(
        owner_hash=OWNER,
        input_fingerprint=FINGERPRINT,
        expires_at=NOW + timedelta(days=7),
        report=report(generated_at=NOW),
    ).model_dump(mode="json")
    for fact in envelope["report"]["facts"]:
        fact.pop("warnings")
    blob = signed_raw(gzip.compress(json.dumps(envelope).encode()))
    restored = ReportCache(SECRET, clock=lambda: NOW).decode(blob, OWNER)
    assert restored.report.facts[0].warnings == []


def test_signed_cache_cannot_drop_a_required_quantity_warning():
    envelope = CachedReport(
        owner_hash=OWNER,
        input_fingerprint=FINGERPRINT,
        expires_at=NOW + timedelta(days=7),
        report=report_with_quantity_warning(generated_at=NOW),
    ).model_dump(mode="json")
    envelope["report"]["facts"][0].pop("warnings")
    blob = signed_raw(gzip.compress(json.dumps(envelope).encode()))
    with pytest.raises(CacheError) as error:
        ReportCache(SECRET, clock=lambda: NOW).decode(blob, OWNER)
    assert error.value.code == "invalid_cache"


def test_warning_copy_changes_do_not_invalidate_structurally_consistent_cached_reports():
    value = report_with_quantity_warning(generated_at=NOW)
    fact = value.facts[0]
    warning = fact.warnings[0].model_copy(
        update={"detail": "Verify this numeric value against the cited source before use."}
    )
    value = value.model_copy(update={"facts": [fact.model_copy(update={"warnings": [warning]})]})
    codec = ReportCache(SECRET, clock=lambda: NOW)
    payload = codec.encode(value, OWNER, FINGERPRINT)
    assert codec.decode(payload["blob"], OWNER).report.facts[0].warnings == [warning]


def test_same_email_namespace_does_not_authorize_another_owner() -> None:
    codec = ReportCache(SECRET, clock=lambda: NOW)
    blob = codec.encode(report(generated_at=NOW), OWNER, FINGERPRINT)["blob"]
    with pytest.raises(CacheError, match="different account"):
        codec.decode(blob, "e" * 64)


@pytest.mark.parametrize("age", [timedelta(days=7), timedelta(days=8)])
def test_seven_day_expiry_does_not_slide_on_restore(age: timedelta) -> None:
    blob = ReportCache(SECRET, clock=lambda: NOW).encode(
        report(generated_at=NOW), OWNER, FINGERPRINT
    )["blob"]
    with pytest.raises(CacheError, match="expiry"):
        ReportCache(SECRET, clock=lambda: NOW + age).decode(blob, OWNER)


@pytest.mark.parametrize(
    "blob",
    ["", "not-a-report", "abc.invalid", "a" * 800000],
    ids=["empty", "not-report", "invalid-signature", "oversized"],
)
def test_corrupt_or_oversized_cache_is_rejected(blob: str) -> None:
    with pytest.raises(CacheError):
        ReportCache(SECRET, clock=lambda: NOW).decode(blob, OWNER)


def test_tampering_and_signing_key_change_are_rejected() -> None:
    codec = ReportCache(SECRET, clock=lambda: NOW)
    blob = codec.encode(report(generated_at=NOW), OWNER, FINGERPRINT)["blob"]
    with pytest.raises(CacheError):
        codec.decode(("A" if blob[0] != "A" else "B") + blob[1:], OWNER)
    with pytest.raises(CacheError):
        ReportCache("different-contoso-secret", clock=lambda: NOW).decode(blob, OWNER)


@pytest.mark.parametrize(
    "compressed",
    [
        b"not gzip",
        gzip.compress(b"not JSON"),
        gzip.compress(json.dumps({"owner_hash": OWNER}).encode()),
        gzip.compress(b"x" * (MAX_REPORT_BYTES + 1)),
        gzip.compress(b"{}") + gzip.compress(b"{}"),
    ],
)
def test_authenticated_bad_payloads_still_fail_bounded_decoding(compressed: bytes) -> None:
    with pytest.raises(CacheError):
        ReportCache(SECRET, clock=lambda: NOW).decode(signed_raw(compressed), OWNER)
