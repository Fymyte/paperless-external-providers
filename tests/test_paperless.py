import pytest

from paperless_sources.paperless import PaperlessClient
from paperless_sources.paperless import PaperlessError
from paperless_sources.paperless import UploadOptions

URL = "http://paperless:8000"


@pytest.fixture
def client():
    c = PaperlessClient(URL, "tok")
    yield c
    c.close()


def test_upload_sends_fields_and_returns_task_id(httpx_mock, client, tmp_path):
    httpx_mock.add_response(
        method="POST",
        url=f"{URL}/api/documents/post_document/",
        match_headers={
            "Authorization": "Token tok",
            "Accept": "application/json; version=9",
        },
        json="0b1c2d3e-task",
    )
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")

    task_id = client.upload(
        f,
        "a.pdf",
        UploadOptions(correspondent=3, tags=(1, 2)),
    )

    assert task_id == "0b1c2d3e-task"
    body = httpx_mock.get_request().read()
    assert b'name="correspondent"\r\n\r\n3' in body
    assert body.count(b'name="tags"') == 2
    assert b'name="document"; filename="a.pdf"' in body
    assert b'name="document_type"' not in body


def test_upload_http_error(httpx_mock, client, tmp_path):
    httpx_mock.add_response(status_code=403, text="Insufficient permissions")
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF")
    with pytest.raises(PaperlessError, match="403"):
        client.upload(f, "a.pdf", UploadOptions())


def test_find_by_checksum(httpx_mock, client):
    httpx_mock.add_response(
        url=f"{URL}/api/documents/?checksum__iexact=abc&page_size=1",
        json={"count": 1, "results": [{"id": 42}]},
    )
    httpx_mock.add_response(
        url=f"{URL}/api/documents/?checksum__iexact=def&page_size=1",
        json={"count": 0, "results": []},
    )
    assert client.find_by_checksum("abc") == 42
    assert client.find_by_checksum("def") is None


def test_resolve_by_name_and_id(httpx_mock, client):
    httpx_mock.add_response(
        url=f"{URL}/api/tags/?name__iexact=Payslip",
        json={"count": 1, "results": [{"id": 7, "name": "payslip"}]},
    )
    assert client.resolve("tags", "Payslip") == 7
    assert client.resolve("tags", 9) == 9


def test_resolve_unknown_name(httpx_mock, client):
    httpx_mock.add_response(json={"count": 0, "results": []})
    with pytest.raises(PaperlessError, match="expected one match"):
        client.resolve("correspondents", "ACME")


def _task(httpx_mock, **fields):
    httpx_mock.add_response(
        url=f"{URL}/api/tasks/?task_id=t1",
        json=[{"task_id": "t1", **fields}],
    )


def test_task_pending(httpx_mock, client):
    _task(httpx_mock, status="STARTED", result=None, related_document=None)
    result = client.task("t1")
    assert result is not None
    assert not result.done


def test_task_success(httpx_mock, client):
    _task(
        httpx_mock,
        status="SUCCESS",
        result="Success. New document id 12 created",
        related_document=12,
    )
    result = client.task("t1")
    assert result.done
    assert result.success
    assert result.document_id == 12
    assert not result.duplicate


def test_task_duplicate_counts_as_done(httpx_mock, client):
    # Paperless 3.x with PAPERLESS_CONSUMER_DELETE_DUPLICATES=true
    _task(
        httpx_mock,
        status="FAILURE",
        result="Not consuming: It is a duplicate of document #5",
        related_document=5,
        duplicate_documents=[{"id": 5, "title": "x", "deleted_at": None}],
    )
    result = client.task("t1")
    assert result.success
    assert result.duplicate
    assert result.document_id == 5


def test_task_failure(httpx_mock, client):
    _task(httpx_mock, status="FAILURE", result="OCR failed", related_document=None)
    result = client.task("t1")
    assert result.done
    assert not result.success
    assert result.message == "OCR failed"


def test_task_unknown(httpx_mock, client):
    httpx_mock.add_response(url=f"{URL}/api/tasks/?task_id=t1", json=[])
    assert client.task("t1") is None
