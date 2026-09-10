"""Explicit, user-safe failures at insights service boundaries."""

from collections.abc import Sequence

from cxplorer.insights.schemas import SourceOutcome


class InsightError(Exception):
    def __init__(
        self,
        code: str,
        public_message: str,
        *,
        source_outcomes: Sequence[SourceOutcome] = (),
    ) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.source_outcomes = list(source_outcomes)


class CacheError(InsightError):
    """A browser copy cannot be safely stored or restored."""
