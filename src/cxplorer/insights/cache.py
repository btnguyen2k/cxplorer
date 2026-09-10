"""Signed, compressed browser reports with no persistent server storage."""

import base64
import binascii
import gzip
import hashlib
import hmac
import re
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from cxplorer.insights.errors import CacheError
from cxplorer.insights.schemas import AcceptedReport, CachedReport
from cxplorer.insights.validation import ArtifactError, check_report

CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_COMPRESSED_BYTES = 512 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_BROWSER_CACHE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BLOB_CHARS = ((MAX_COMPRESSED_BYTES + 2) // 3) * 4 + 65


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReportCache:
    def __init__(
        self,
        secret: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._key = hmac.digest(secret.encode("utf-8"), b"cxplorer-signed-browser-report", "sha256")
        self._clock = clock

    def encode(
        self, report: AcceptedReport, owner_hash: str, input_fingerprint: str
    ) -> dict[str, str]:
        if report.generated_at.tzinfo is None:
            raise CacheError("invalid_report", "The report has an invalid generation date.")
        expires_at = report.generated_at + timedelta(seconds=CACHE_TTL_SECONDS)
        if expires_at <= self._clock():
            raise CacheError("expired_cache", "This saved report has expired.")
        envelope = CachedReport(
            owner_hash=owner_hash,
            input_fingerprint=input_fingerprint,
            expires_at=expires_at,
            report=report,
        )
        try:
            check_report(report)
        except ArtifactError as error:
            raise CacheError(
                "invalid_report", "The report has inconsistent evidence references."
            ) from error
        raw = envelope.model_dump_json().encode("utf-8")
        if len(raw) > MAX_REPORT_BYTES:
            raise CacheError(
                "cache_too_large",
                "This report is too large for browser storage. Download a copy before leaving.",
            )
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise CacheError(
                "cache_too_large",
                "This report is too large for browser storage. Download a copy before leaving.",
            )
        encoded = base64.urlsafe_b64encode(compressed)
        signature = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        return {
            "report_id": report.report_id,
            "company_name": (report.company_name.text or "Company insights")[:240],
            "generated_at": report.generated_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "input_fingerprint": input_fingerprint,
            "blob": f"{encoded.decode('ascii')}.{signature}",
        }

    def decode(self, blob: str, owner_hash: str) -> CachedReport:
        invalid = "The saved browser report is invalid or corrupt and cannot be restored."
        if not isinstance(blob, str) or len(blob) > MAX_CACHE_BLOB_CHARS:
            raise CacheError("invalid_cache", invalid)
        encoded_text, separator, signature = blob.partition(".")
        if (
            separator != "."
            or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded_text)
        ):
            raise CacheError("invalid_cache", invalid)
        encoded = encoded_text.encode("ascii")
        expected = hmac.new(self._key, encoded, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise CacheError("invalid_cache", invalid)
        try:
            compressed = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except binascii.Error as error:
            raise CacheError("invalid_cache", invalid) from error
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise CacheError("invalid_cache", invalid)
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            raw = decoder.decompress(compressed, MAX_REPORT_BYTES + 1)
        except zlib.error as error:
            raise CacheError("invalid_cache", invalid) from error
        if (
            len(raw) > MAX_REPORT_BYTES
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise CacheError("invalid_cache", invalid)
        try:
            envelope = CachedReport.model_validate_json(raw)
            check_report(envelope.report)
        except (ValidationError, ArtifactError) as error:
            raise CacheError("invalid_cache", invalid) from error
        if not hmac.compare_digest(envelope.owner_hash, owner_hash):
            raise CacheError("wrong_owner", "This saved report belongs to a different account.")
        generated_at = envelope.report.generated_at
        expires_at = envelope.expires_at
        if generated_at.tzinfo is None or expires_at.tzinfo is None:
            raise CacheError("invalid_cache", invalid)
        now = self._clock()
        if expires_at != generated_at + timedelta(
            seconds=CACHE_TTL_SECONDS
        ) or generated_at > now + timedelta(minutes=1):
            raise CacheError("invalid_cache", invalid)
        if expires_at <= now:
            raise CacheError("expired_cache", "This saved report has reached its seven-day expiry.")
        return envelope
