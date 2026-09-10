"""Authenticated cache transport and private route boundaries."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from cxplorer.insights.schemas import CachedReport, EvidenceGap
from tests.conftest import TEST_CSRF_TOKEN
from tests.insights_fixtures import report, report_with_quantity_warning

REPORT_ID = "a" * 32
OWNER = hashlib.sha256(b"ada@example.com").hexdigest()


def install_report(client: TestClient, owner: str = OWNER) -> None:
    client.app.state.insights_jobs.restore(
        CachedReport(
            owner_hash=owner,
            input_fingerprint="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            report=report(REPORT_ID),
        )
    )


@pytest.mark.parametrize(
    "path",
    [
        f"/api/private/insights/{REPORT_ID}/status",
        f"/api/private/insights/{REPORT_ID}/cache",
        f"/insights/{REPORT_ID}/download",
    ],
)
def test_private_data_routes_require_login(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_report_page_redirects_to_login(client: TestClient) -> None:
    response = client.get(f"/insights/{REPORT_ID}", follow_redirects=False)
    assert response.status_code == 303
    assert "/login?" in response.headers["location"]


def test_owned_cache_payload_and_download_contain_no_authentication(client: TestClient) -> None:
    client.post("/_test/sign-in")
    install_report(client)
    cached = client.get(f"/api/private/insights/{REPORT_ID}/cache")
    assert cached.status_code == 200
    assert cached.headers["cache-control"] == "no-store"
    assert cached.json()["report_id"] == REPORT_ID
    assert "blob" in cached.json()
    assert "ada@example.com" not in cached.text
    assert TEST_CSRF_TOKEN not in cached.text
    downloaded = client.get(f"/insights/{REPORT_ID}/download")
    assert downloaded.status_code == 200
    assert downloaded.json()["company_name"]["text"] == "Contoso"
    assert "attachment" in downloaded.headers["content-disposition"]
    assert downloaded.headers["cache-control"] == "no-store"


def test_cross_owner_data_is_not_returned(client: TestClient) -> None:
    client.post("/_test/sign-in")
    install_report(client, "e" * 64)
    for path in (
        f"/api/private/insights/{REPORT_ID}/status",
        f"/api/private/insights/{REPORT_ID}/cache",
        f"/insights/{REPORT_ID}/download",
    ):
        assert client.get(path).status_code == 404


def test_restore_requires_current_csrf_and_survives_memory_loss(client: TestClient) -> None:
    client.post("/_test/sign-in")
    cached = client.app.state.report_cache.encode(report(), OWNER, "b" * 64)
    assert client.post("/insights/restore", data={"cache_blob": cached["blob"]}).status_code == 403
    response = client.post(
        "/insights/restore",
        data={
            "csrf_token": TEST_CSRF_TOKEN,
            "cache_blob": cached["blob"],
            "report_id": REPORT_ID,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/insights/{REPORT_ID}")
    assert client.get(f"/api/private/insights/{REPORT_ID}/cache").status_code == 200


@pytest.mark.parametrize("numeric_text", ["5", "<img src=x onerror=alert(1)>"])
def test_restored_quantity_warnings_are_visible_escaped_and_included_in_download(
    client: TestClient, numeric_text: str
) -> None:
    client.post("/_test/sign-in")
    value = report_with_quantity_warning(numeric_text=numeric_text)
    cached = client.app.state.report_cache.encode(value, OWNER, "b" * 64)
    restored = client.post(
        "/insights/restore",
        data={
            "csrf_token": TEST_CSRF_TOKEN,
            "cache_blob": cached["blob"],
            "report_id": REPORT_ID,
        },
        follow_redirects=False,
    )
    assert restored.status_code == 303
    page = client.get(f"/insights/{REPORT_ID}")
    assert page.status_code == 200
    assert "Quantity warnings: verify before use" not in page.text
    assert "Verify the quoted figure before using this value." in page.text
    assert '<div class="coverage-note" role="note" aria-label="Quantity warnings">' in page.text
    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "Contoso reported revenue of 500 USD million in 2025." in page.text
    downloaded = client.get(f"/insights/{REPORT_ID}/download")
    fact = downloaded.json()["facts"][0]
    assert fact["warnings"][0]["numeric_text"] == numeric_text
    assert fact["warnings"][0]["code"] == "quantity_numeric_mismatch"
    assert client.get(f"/api/private/insights/{REPORT_ID}/cache").status_code == 200


def test_reports_without_quantity_mismatches_have_no_warning_notice(client: TestClient) -> None:
    client.post("/_test/sign-in")
    install_report(client)
    page = client.get(f"/insights/{REPORT_ID}")
    assert page.status_code == 200
    assert "Quantity warnings: verify before use" not in page.text


def test_omitted_numeric_or_period_facts_show_a_report_warning(client: TestClient) -> None:
    client.post("/_test/sign-in")
    value = report(REPORT_ID).model_copy(
        update={
            "evidence_gaps": [
                EvidenceGap(
                    area="Numeric and period warnings",
                    detail=(
                        "Excluded 1 candidate fact because its reporting period could not be "
                        "reconciled with the exact source quotation."
                    ),
                )
            ]
        }
    )
    client.app.state.insights_jobs.restore(
        CachedReport(
            owner_hash=OWNER,
            input_fingerprint="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            report=value,
        )
    )
    page = client.get(f"/insights/{REPORT_ID}")
    assert page.status_code == 200
    assert "Numeric and reporting-period warnings" not in page.text
    assert "Numeric and period warnings" in page.text
    assert "Excluded 1 candidate fact" in page.text
    downloaded = client.get(f"/insights/{REPORT_ID}/download")
    assert downloaded.json()["evidence_gaps"][0]["area"] == "Numeric and period warnings"


def test_source_identity_warnings_are_visible_and_included_in_download(
    client: TestClient,
) -> None:
    client.post("/_test/sign-in")
    note = (
        "Identity warning: the retained page title or text identifies the homepage company, "
        "but the selected relation quotation does not name it explicitly."
    )
    value = report(REPORT_ID)
    value = value.model_copy(
        update={
            "evidence_gaps": [
                EvidenceGap(area="source_identity", detail=f"Contoso official source: {note}")
            ],
            "sources": [
                source.model_copy(update={"coverage_note": note}) for source in value.sources
            ],
        }
    )
    client.app.state.insights_jobs.restore(
        CachedReport(
            owner_hash=OWNER,
            input_fingerprint="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            report=value,
        )
    )
    page = client.get(f"/insights/{REPORT_ID}")
    assert page.status_code == 200
    assert "Source identity warning" not in page.text
    assert "source_identity" in page.text
    assert "selected relation quotation does not name it explicitly" in page.text
    downloaded = client.get(f"/insights/{REPORT_ID}/download").json()
    assert downloaded["evidence_gaps"][0]["area"] == "source_identity"
    assert downloaded["sources"][0]["coverage_note"] == note


def test_quotation_matching_warnings_are_visible_and_included_in_download(
    client: TestClient,
) -> None:
    client.post("/_test/sign-in")
    detail = "A model-provided quotation was replaced with the exact retained source substring."
    value = report(REPORT_ID).model_copy(
        update={"evidence_gaps": [EvidenceGap(area="Quotation matching warnings", detail=detail)]}
    )
    client.app.state.insights_jobs.restore(
        CachedReport(
            owner_hash=OWNER,
            input_fingerprint="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
            report=value,
        )
    )
    page = client.get(f"/insights/{REPORT_ID}")
    assert page.status_code == 200
    assert "Quotation matching warnings" in page.text
    assert detail in page.text
    downloaded = client.get(f"/insights/{REPORT_ID}/download").json()
    assert downloaded["evidence_gaps"][0] == {
        "area": "Quotation matching warnings",
        "detail": detail,
    }


def test_streamed_oversized_upload_is_rejected_before_parsing(client: TestClient) -> None:
    client.post("/_test/sign-in")
    response = client.post(
        "/insights/restore",
        content=iter([b"x" * 600000, b"x" * 600000]),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("action", ["cancel", "retry", "delete"])
def test_mutations_require_csrf(client: TestClient, action: str) -> None:
    client.post("/_test/sign-in")
    assert client.post(f"/insights/{REPORT_ID}/{action}", data={}).status_code == 403
