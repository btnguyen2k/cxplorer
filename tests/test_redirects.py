"""Authentication redirect validation tests."""

import pytest

from cxplorer.auth.redirects import safe_local_path


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, "/dashboard"),
        ("", "/dashboard"),
        ("/dashboard", "/dashboard"),
        ("/projects?view=recent#ignored", "/projects?view=recent"),
        ("https://example.com", "/dashboard"),
        ("//example.com/path", "/dashboard"),
        (r"/\example.com", "/dashboard"),
        ("dashboard", "/dashboard"),
    ],
)
def test_safe_local_path(candidate: str | None, expected: str) -> None:
    assert safe_local_path(candidate) == expected
