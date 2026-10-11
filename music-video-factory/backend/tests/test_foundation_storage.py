from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote

import boto3
import pytest
import requests
from moto import mock_aws

from app.storage import (
    LocalStorage,
    S3Storage,
    StorageKeyError,
    StorageNotFound,
    build_content_disposition,
    get_storage,
    make_key,
    project_prefix,
)


def test_make_key_scheme() -> None:
    assert make_key("p1", "audio", "a1", ".MP3") == "projects/p1/audio/a1.mp3"
    assert make_key("p1", "zip", "z1") == "projects/p1/zip/z1"
    assert project_prefix("p1") == "projects/p1/"
    for bad_ext in (".m p4", "mp4", ".../x"):
        with pytest.raises(StorageKeyError):
            make_key("p1", "clip", "c1", bad_ext)
    with pytest.raises(StorageKeyError):
        make_key("../p1", "clip", "c1", ".mp4")


def test_local_storage_roundtrip(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path / "root")
    key = "projects/p1/clip/c1.mp4"
    assert not store.exists(key)
    assert store.put_bytes(key, b"hello world") == 11
    assert store.exists(key) and store.size(key) == 11
    assert b"".join(store.iter_bytes(key, chunk_size=4)) == b"hello world"
    assert store.local_path(key) == (tmp_path / "root" / key).resolve()

    fetched = store.fetch_to(key, tmp_path / "out" / "copy.mp4")
    assert fetched.read_bytes() == b"hello world"

    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 1000)
    assert store.put_file("projects/p1/audio/a1.wav", src) == 1000 and src.exists()
    assert store.put_file("projects/p1/audio/a2.wav", src, move=True) == 1000 and not src.exists()

    store.delete(key)
    store.delete(key)  # missing is fine
    assert not store.exists(key)
    with pytest.raises(StorageNotFound):
        store.size(key)
    with pytest.raises(StorageNotFound):
        store.fetch_to(key, tmp_path / "nope")

    assert store.delete_prefix("projects/p1/") == 2
    assert not (tmp_path / "root" / "projects" / "p1").exists()
    assert store.delete_prefix("projects/p1/") == 0
    assert not list((tmp_path / "root").rglob("*.tmp"))


@pytest.mark.parametrize(
    "key",
    ["../x", "/etc/passwd", "a/../../b", "a//b", "a\\b", "", "a/./b", "a/b/", "x\x00y", "ünicode/x", "a/../b"],
)
def test_local_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    store = LocalStorage(tmp_path / "root")
    with pytest.raises(StorageKeyError):
        store.path(key)
    with pytest.raises(StorageKeyError):
        store.put_bytes(key, b"x")


def test_local_storage_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    root = tmp_path / "root"
    (root / "projects").mkdir(parents=True)
    os.symlink(outside, root / "projects" / "link")
    store = LocalStorage(root)
    with pytest.raises(StorageKeyError):
        store.path("projects/link/secret.txt")


def test_content_disposition_header() -> None:
    header = build_content_disposition("夜明け 01.mp4")
    assert header == "attachment; filename=\"01.mp4\"; filename*=UTF-8''%E5%A4%9C%E6%98%8E%E3%81%91%2001.mp4"
    assert build_content_disposition("日本語.mp4").startswith('attachment; filename="download.mp4"')
    evil = build_content_disposition('a"b\r\nX-Evil: 1;.mp4')
    assert "\r" not in evil and "\n" not in evil
    assert evil.split("; filename*=")[0].count('"') == 2


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)


@mock_aws
def test_s3_storage_roundtrip(aws_env: None, tmp_path: Path) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="mvf-test")
    store = S3Storage("mvf-test", access_key_id="testing", secret_access_key="testing", region="us-east-1")

    key = "projects/p1/render/v1.mp4"
    src = tmp_path / "v1.mp4"
    src.write_bytes(b"\x00\x01video-bytes" * 100)
    assert not store.exists(key)
    assert store.put_file(key, src, content_type="video/mp4") == src.stat().st_size
    assert store.exists(key) and store.size(key) == src.stat().st_size
    assert store.client.head_object(Bucket="mvf-test", Key=key)["ContentType"] == "video/mp4"
    assert store.put_bytes("projects/p1/thumbnail/t1.jpg", b"jpeg", content_type="image/jpeg") == 4
    assert b"".join(store.iter_bytes(key, chunk_size=64)) == src.read_bytes()
    assert store.fetch_to(key, tmp_path / "dl" / "copy.mp4").read_bytes() == src.read_bytes()
    assert store.local_path(key) is None
    with pytest.raises(StorageNotFound):
        store.size("projects/p1/render/missing.mp4")
    with pytest.raises(StorageNotFound):
        store.fetch_to("projects/p1/render/missing.mp4", tmp_path / "missing")

    src_move = tmp_path / "move.bin"
    src_move.write_bytes(b"m")
    store.put_file("projects/p1/clip/c1.mp4", src_move, move=True)
    assert not src_move.exists()

    inline_url = store.url(key, ttl=600)
    assert "X-Amz-Signature=" in inline_url and "X-Amz-Expires=600" in inline_url
    response = requests.get(inline_url, timeout=10)
    assert response.status_code == 200 and response.content == src.read_bytes()

    filename = "アーティスト_曲_01.mp4"
    download_url = store.url(key, download=True, filename=filename)
    assert "response-content-disposition=" in download_url
    assert "filename*=UTF-8''" in unquote(unquote(download_url))
    downloaded = requests.get(download_url, timeout=10)
    assert downloaded.status_code == 200
    assert downloaded.headers.get("Content-Disposition") == build_content_disposition(filename)

    store.delete("projects/p1/thumbnail/t1.jpg")
    assert not store.exists("projects/p1/thumbnail/t1.jpg")
    store.put_bytes("projects/p2/audio/a.mp3", b"other project")
    assert store.delete_prefix("projects/p1/") == 2
    assert not store.exists(key)
    assert store.exists("projects/p2/audio/a.mp3")
    with pytest.raises(StorageKeyError):
        store.put_bytes("../escape", b"x")


@mock_aws
def test_s3_delete_prefix_paginates(aws_env: None) -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="mvf-many")
    store = S3Storage("mvf-many", region="us-east-1")
    for i in range(1005):
        store.client.put_object(Bucket="mvf-many", Key=f"projects/p9/clip/{i:04d}.mp4", Body=b"x")
    assert store.delete_prefix("projects/p9/") == 1005
    assert store.client.list_objects_v2(Bucket="mvf-many").get("KeyCount", 0) == 0


@mock_aws
def test_get_storage_selects_backend(aws_env: None, set_env) -> None:
    assert isinstance(get_storage(), LocalStorage)
    set_env(STORAGE_BACKEND="s3", S3_BUCKET="mvf-sel", S3_REGION="us-east-1")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="mvf-sel")
    store = get_storage()
    assert isinstance(store, S3Storage) and store.bucket == "mvf-sel"
    store.put_bytes("projects/p/audio/a.mp3", b"abc")
    assert store.size("projects/p/audio/a.mp3") == 3


def test_get_storage_s3_requires_bucket(set_env) -> None:
    set_env(STORAGE_BACKEND="s3", S3_BUCKET="")
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        get_storage()
