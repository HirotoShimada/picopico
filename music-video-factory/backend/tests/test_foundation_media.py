"""Signed ``/media`` delivery: signature/expiry checks, Range, Content-Disposition."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.security import sign_media_url

KEY = "projects/p123/render/v456.mp4"
DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture
def stored(storage) -> bytes:
    storage.put_bytes(KEY, DATA, content_type="video/mp4")
    return DATA


def _parts(url: str) -> tuple[str, dict[str, str]]:
    split = urlsplit(url)
    return split.path, {k: v[0] for k, v in parse_qs(split.query).items()}


def test_valid_signed_url(client: TestClient, storage, stored: bytes) -> None:
    url = storage.url(KEY)
    assert url.startswith("http://testserver/media/projects/p123/render/v456.mp4?")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == stored
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert "content-disposition" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert client.head(url).status_code == 200


def test_tampered_or_missing_signature_is_403(client: TestClient, storage, stored: bytes) -> None:
    path, query = _parts(storage.url(KEY))
    sig = query["sig"]
    bad_sig = sig[:-1] + ("a" if sig[-1] != "a" else "b")
    cases = [
        f"{path}?exp={query['exp']}&sig={bad_sig}",
        f"{path}?exp={int(query['exp']) + 10}&sig={sig}",
        f"/media/projects/p123/render/other.mp4?exp={query['exp']}&sig={sig}",
        f"{path}?exp={query['exp']}&sig={sig}&download=1",
        f"{path}?exp={query['exp']}&sig={sig}&filename=evil.mp4",
        f"{path}?exp={query['exp']}",
        path,
    ]
    for url in cases:
        response = client.get(url)
        assert response.status_code == 403, url
        assert "署名" in response.json()["detail"]


def test_expired_url_is_403(client: TestClient, stored: bytes) -> None:
    expired = sign_media_url(KEY, ttl=-5)
    assert client.get(expired).status_code == 403


def test_range_requests(client: TestClient, storage, stored: bytes) -> None:
    url = storage.url(KEY)

    partial = client.get(url, headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.content == stored[100:200]
    assert partial.headers["content-range"] == f"bytes 100-199/{len(stored)}"
    assert partial.headers["content-length"] == "100"

    tail = client.get(url, headers={"Range": "bytes=1000-"})
    assert tail.status_code == 206 and tail.content == stored[1000:]

    suffix = client.get(url, headers={"Range": "bytes=-24"})
    assert suffix.status_code == 206 and suffix.content == stored[-24:]

    unsatisfiable = client.get(url, headers={"Range": f"bytes={len(stored) + 10}-"})
    assert unsatisfiable.status_code == 416


def test_download_content_disposition(client: TestClient, storage, stored: bytes) -> None:
    filename = "テストアーティスト_夜明けの歌_01.mp4"
    response = client.get(storage.url(KEY, download=True, filename=filename))
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="')
    assert f"filename*=UTF-8''{quote(filename, safe='')}" in disposition
    ascii_part = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert ascii_part.isascii() and ascii_part.endswith(".mp4")

    default_name = client.get(storage.url(KEY, download=True))
    assert 'filename="v456.mp4"' in default_name.headers["content-disposition"]

    injected = client.get(storage.url(KEY, download=True, filename='a"\r\nSet-Cookie: x=1.mp4'))
    assert injected.status_code == 200
    assert "set-cookie" not in injected.headers
    assert "\r" not in injected.headers["content-disposition"] and "\n" not in injected.headers["content-disposition"]


def test_missing_file_and_bad_key_are_404(client: TestClient) -> None:
    assert client.get(sign_media_url("projects/p123/render/missing.mp4")).status_code == 404
    assert client.get(sign_media_url("projects/p123/render/bad key!.mp4")).status_code == 404


def test_media_url_is_public_in_token_mode(make_client, storage, stored: bytes) -> None:
    token_client = make_client(AUTH_MODE="token", API_TOKENS="u:token-0123456789abcdef")
    assert token_client.get("/api/projects").status_code == 401
    from app.storage import get_storage

    assert token_client.get(get_storage().url(KEY)).status_code == 200


def test_media_ttl_from_settings(set_env) -> None:
    from app.storage import get_storage

    set_env(MEDIA_URL_TTL=120)
    _, query = _parts(get_storage().url(KEY))
    # Rounded up to a 12 s window (ttl / 10) so polling clients get stable URLs.
    assert 110 <= int(query["exp"]) - int(time.time()) <= 121 + 12
    assert int(query["exp"]) % 12 == 0
