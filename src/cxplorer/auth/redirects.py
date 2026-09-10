"""Redirect target validation."""

from urllib.parse import urlsplit, urlunsplit


def safe_local_path(candidate: str | None, *, default: str = "/dashboard") -> str:
    """Return a local absolute path, rejecting external or ambiguous targets."""
    if not candidate:
        return default

    candidate = candidate.strip()
    parsed = urlsplit(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or parsed.scheme
        or parsed.netloc
    ):
        return default

    return urlunsplit(("", "", parsed.path, parsed.query, ""))
