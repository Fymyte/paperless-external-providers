import datetime
import json

import httpx
import pytest

from paperless_sources.providers import ProviderAuthError
from paperless_sources.providers import ProviderError
from paperless_sources.providers import RemoteDocument
from paperless_sources.providers import SourceProvider
from paperless_sources.providers.edocperso import API_URL
from paperless_sources.providers.edocperso import EDocPersoProvider

LOGIN = f"{API_URL}/login"
FOLDERS = f"{API_URL}/folders"
DOCUMENTS = f"{API_URL}/documents"
DOWNLOAD = f"{API_URL}/documents/download"

# Shape of the November 2026 capture: a flat list linked by parentId
FOLDER_LIST = {
    "folders": [
        {
            "id": "emp",
            "parentId": None,
            "children": ["acme"],
            "name": "Mes employeurs",
            "className": "MyEmployers",
        },
        {
            "id": "acme",
            "parentId": "emp",
            "children": [],
            "name": "ACME",
            "className": None,
        },
        {
            "id": "test",
            "parentId": None,
            "children": [],
            "name": "test",
            "className": None,
        },
    ],
}


def _item(doc_id, **fields):
    return {
        "id": doc_id,
        "title": f"{doc_id}-title",
        "dateAdded": "2025-06-30T08:00:00.000Z",
        "updatedAt": None,
        "state": "Ready",
        "typeCode": "PAYE",
        "fileExtension": ".pdf",
        "isGDPRBlocked": False,
        **fields,
    }


def _login_ok(httpx_mock, token="TOKEN1"):
    httpx_mock.add_response(
        method="POST",
        url=LOGIN,
        match_json={"email": "me@example.com", "password": "pw"},
        json={"userId": "u", "token": token, "refreshToken": "r"},
    )


def _folders_ok(httpx_mock, token="TOKEN1"):
    httpx_mock.add_response(
        method="GET",
        url=FOLDERS,
        match_headers={"Authorization": f"Bearer {token}"},
        json=FOLDER_LIST,
    )


def _documents(httpx_mock, folder_id, items, *, total=None, offset=0):
    httpx_mock.add_response(
        method="POST",
        url=DOCUMENTS,
        match_json={"paging": {"limit": 1000, "offset": offset}, "folderId": folder_id},
        json={
            "paging": {
                "limit": 1000,
                "offset": offset,
                "total": total if total is not None else len(items),
            },
            "items": items,
        },
    )


@pytest.fixture
def http():
    with httpx.Client() as client:
        yield client


def _provider(http, **config):
    return EDocPersoProvider(
        {"login": "me@example.com", "password": "pw", **config},
        http,
    )


def test_satisfies_protocol(http):
    assert isinstance(_provider(http), SourceProvider)


def test_list_builds_paths_from_parent_ids(httpx_mock, http):
    _login_ok(httpx_mock)
    _folders_ok(httpx_mock)
    _documents(httpx_mock, "emp", [])
    _documents(httpx_mock, "acme", [_item("d1", title="Bulletins 06_2025")])
    _documents(
        httpx_mock,
        "test",
        [_item("d2", title="notes.txt", fileExtension=".txt", typeCode="AUDO")],
    )

    with _provider(http) as provider:
        docs = list(provider.list_documents())

    assert docs == [
        RemoteDocument(
            remote_id="d1",
            filename="Bulletins 06_2025.pdf",
            path=("Mes employeurs", "ACME"),
            modified=datetime.datetime(2025, 6, 30, 8, 0, tzinfo=datetime.UTC),
        ),
        RemoteDocument(
            remote_id="d2",
            filename="notes.txt",
            path=("test",),
            modified=datetime.datetime(2025, 6, 30, 8, 0, tzinfo=datetime.UTC),
        ),
    ]


def test_folder_filter_only_queries_selected_folders(httpx_mock, http):
    _login_ok(httpx_mock)
    _folders_ok(httpx_mock)
    # pytest-httpx fails the test on any request without a matching response,
    # so this also proves "test" is never queried
    _documents(httpx_mock, "emp", [])
    _documents(httpx_mock, "acme", [_item("d1")])

    with _provider(http, folders=["mes EMPLOYEURS"]) as provider:
        assert [d.remote_id for d in provider.list_documents()] == ["d1"]


def test_type_codes_and_state_filters(httpx_mock, http):
    _login_ok(httpx_mock)
    _folders_ok(httpx_mock)
    _documents(
        httpx_mock,
        "test",
        [
            _item("pay"),
            _item("other", typeCode="AUDO"),
            _item("pending", state="Processing"),
            _item("blocked", isGDPRBlocked=True),
        ],
    )

    with _provider(http, folders=["test"], type_codes=["PAYE"]) as provider:
        assert [d.remote_id for d in provider.list_documents()] == ["pay"]


def test_documents_are_paged(httpx_mock, http):
    _login_ok(httpx_mock)
    _folders_ok(httpx_mock)
    _documents(httpx_mock, "test", [_item(f"a{i}") for i in range(1000)], total=1001)
    _documents(httpx_mock, "test", [_item("last")], total=1001, offset=1000)

    with _provider(http, folders=["test"]) as provider:
        docs = list(provider.list_documents())

    assert len(docs) == 1001
    assert docs[-1].remote_id == "last"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_login_rejected(httpx_mock, http, status):
    httpx_mock.add_response(method="POST", url=LOGIN, status_code=status)
    with pytest.raises(ProviderAuthError):
        _provider(http).__enter__()


def test_expired_token_logs_in_again_once(httpx_mock, http):
    _login_ok(httpx_mock, token="OLD")
    httpx_mock.add_response(
        method="GET",
        url=FOLDERS,
        match_headers={"Authorization": "Bearer OLD"},
        status_code=401,
    )
    _login_ok(httpx_mock, token="NEW")
    _folders_ok(httpx_mock, token="NEW")
    _documents(httpx_mock, "test", [])

    with _provider(http, folders=["test"]) as provider:
        assert list(provider.list_documents()) == []


def test_second_401_is_an_auth_error(httpx_mock, http):
    _login_ok(httpx_mock)
    httpx_mock.add_response(
        method="GET", url=FOLDERS, status_code=401, is_reusable=True,
    )
    httpx_mock.add_response(method="POST", url=LOGIN, json={"token": "T2"})

    with _provider(http) as provider, pytest.raises(ProviderAuthError):
        list(provider.list_documents())


def test_download_uses_attachment_filename(httpx_mock, http, tmp_path):
    _login_ok(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=DOWNLOAD,
        match_json={"documentIds": ["d1"], "folderIds": []},
        match_headers={"Accept": "application/octet-stream"},
        headers={
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="Bulletins 06_2025.pdf"',
        },
        content=b"%PDF-1.7 payslip",
    )
    doc = RemoteDocument(remote_id="d1", filename="listed-name.pdf")

    with _provider(http) as provider:
        path = provider.download(doc, tmp_path)

    assert path == tmp_path / "Bulletins 06_2025.pdf"
    assert path.read_bytes() == b"%PDF-1.7 payslip"


def test_download_unsafe_filename_stays_in_dest(httpx_mock, http, tmp_path):
    _login_ok(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=DOWNLOAD,
        headers={"content-disposition": 'attachment; filename="../../etc/x.pdf"'},
        content=b"%PDF",
    )
    with _provider(http) as provider:
        path = provider.download(
            RemoteDocument(remote_id="d1", filename="a.pdf"), tmp_path,
        )
    assert path == tmp_path / "x.pdf"


def test_download_without_header_uses_listed_name(httpx_mock, http, tmp_path):
    _login_ok(httpx_mock)
    httpx_mock.add_response(method="POST", url=DOWNLOAD, content=b"%PDF")
    with _provider(http) as provider:
        path = provider.download(
            RemoteDocument(remote_id="d1", filename="a.pdf"), tmp_path,
        )
    assert path == tmp_path / "a.pdf"


def test_download_json_error_is_not_saved(httpx_mock, http, tmp_path):
    _login_ok(httpx_mock)
    httpx_mock.add_response(
        method="POST",
        url=DOWNLOAD,
        headers={"content-type": "application/json"},
        content=json.dumps({"error": "not found"}).encode(),
    )
    with _provider(http) as provider, pytest.raises(ProviderError):
        provider.download(RemoteDocument(remote_id="d1", filename="a.pdf"), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_parent_loop_does_not_hang(httpx_mock, http):
    _login_ok(httpx_mock)
    httpx_mock.add_response(
        method="GET",
        url=FOLDERS,
        json={
            "folders": [
                {"id": "a", "parentId": "b", "name": "A"},
                {"id": "b", "parentId": "a", "name": "B"},
            ],
        },
    )
    _documents(httpx_mock, "a", [])
    _documents(httpx_mock, "b", [])
    with _provider(http) as provider:
        assert list(provider.list_documents()) == []


def test_use_without_login_fails(http):
    with pytest.raises(ProviderError, match="context manager"):
        list(_provider(http).list_documents())


def test_type_codes_must_be_a_list(http):
    with pytest.raises(ValueError, match="type_codes"):
        _provider(http, type_codes="PAYE")
