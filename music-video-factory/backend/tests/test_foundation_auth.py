from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.security import (
    LOCAL_USER_ID,
    hash_token,
    parse_api_tokens,
    seed_users,
    sign_media_url,
    verify_media_signature,
)

ALICE = "alice-token-0123456789abcdef"
BOB = "bob-token-0123456789abcdef"


@pytest.fixture
def token_client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE}, bob:{BOB}")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_missing_or_invalid_token_is_401(token_client: TestClient) -> None:
    missing = token_client.get("/api/projects")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert "認証" in missing.json()["detail"]

    for headers in (_auth("wrong-token-0123456789"), {"Authorization": f"Basic {ALICE}"}, {"Authorization": "Bearer"}):
        assert token_client.get("/api/projects", headers=headers).status_code == 401

    assert token_client.get("/api/dashboard").status_code == 401
    assert token_client.post("/api/projects", json={"title": "t", "artist_name": "a"}).status_code == 401


def test_health_needs_no_token(token_client: TestClient) -> None:
    assert token_client.get("/api/health").status_code == 200


def test_ownership_isolation(token_client: TestClient, demo_dir: Path) -> None:
    created = token_client.post(
        "/api/projects", json={"title": "Aliceの曲", "artist_name": "Alice"}, headers=_auth(ALICE)
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    base = f"/api/projects/{project_id}"

    assert token_client.get(base, headers=_auth(ALICE)).status_code == 200
    assert [p["id"] for p in token_client.get("/api/projects", headers=_auth(ALICE)).json()] == [project_id]

    bob = _auth(BOB)
    assert token_client.get("/api/projects", headers=bob).json() == []
    assert token_client.get(base, headers=bob).status_code == 404
    assert token_client.patch(base, json={"title": "奪取"}, headers=bob).status_code == 404
    assert token_client.put(f"{base}/rights", json={"items": {"audio_rights": True}}, headers=bob).status_code == 404
    assert token_client.get(f"{base}/clips", headers=bob).status_code == 404
    with (demo_dir / "demo_song.mp3").open("rb") as fh:
        upload = token_client.post(f"{base}/audio", files={"file": ("x.mp3", fh, "audio/mpeg")}, headers=bob)
    assert upload.status_code == 404
    with (demo_dir / "clip_03.mp4").open("rb") as fh:
        clip_upload = token_client.post(
            f"{base}/clips/upload", files=[("files", ("c.mp4", fh, "video/mp4"))], headers=_auth(ALICE)
        )
    assert clip_upload.status_code == 201
    clip_id = clip_upload.json()[0]["id"]
    assert token_client.delete(f"/api/clips/{clip_id}", headers=bob).status_code == 404
    assert token_client.delete(base, headers=bob).status_code == 404
    assert token_client.get("/api/dashboard", headers=bob).json()["projects"] == 0

    # Alice still owns everything untouched.
    project = token_client.get(base, headers=_auth(ALICE)).json()
    assert project["title"] == "Aliceの曲" and project["counts"]["clips"] == 1
    assert token_client.delete(f"/api/clips/{clip_id}", headers=_auth(ALICE)).status_code == 204
    assert token_client.delete(base, headers=_auth(ALICE)).status_code == 204


def test_users_seeded_with_hashed_tokens(token_client: TestClient, db: Session) -> None:
    users = {u.name: u for u in db.scalars(select(User)).all()}
    assert set(users) == {"alice", "bob"}
    assert users["alice"].token_hash == hashlib.sha256(ALICE.encode()).hexdigest()
    assert all(ALICE not in (u.token_hash or "") and BOB not in (u.token_hash or "") for u in users.values())


def test_token_rotation_keeps_user(db: Session, set_env: Callable[..., object]) -> None:
    set_env(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE}")
    (alice,) = seed_users(db)
    set_env(API_TOKENS="alice:rotated-token-0123456789abc")
    (rotated,) = seed_users(db)
    assert rotated.id == alice.id
    assert rotated.token_hash == hash_token("rotated-token-0123456789abc")
    # Idempotent.
    assert seed_users(db)[0].id == alice.id
    assert len(db.scalars(select(User)).all()) == 1


def test_parse_api_tokens_skips_malformed() -> None:
    assert parse_api_tokens(" a:tok1 , broken, :x, b: tok2 ,") == [("a", "tok1"), ("b", "tok2")]
    assert parse_api_tokens(None) == []


def test_dev_mode_uses_single_local_user(client: TestClient, db: Session) -> None:
    assert client.post("/api/projects", json={"title": "t", "artist_name": "a"}).status_code == 201
    # A bearer token is irrelevant in dev mode.
    assert client.get("/api/projects", headers=_auth("anything")).status_code == 200
    users = db.scalars(select(User)).all()
    assert [(u.id, u.name) for u in users] == [(LOCAL_USER_ID, "local")]


def test_media_signature_unit() -> None:
    url = sign_media_url("projects/p/audio/a.mp3", ttl=60)
    assert url.startswith("http://testserver/media/projects/p/audio/a.mp3?exp=")
    query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
    exp, sig = int(query["exp"]), query["sig"]
    assert verify_media_signature("projects/p/audio/a.mp3", exp, sig)
    assert not verify_media_signature("projects/p/audio/b.mp3", exp, sig)
    assert not verify_media_signature("projects/p/audio/a.mp3", exp + 1, sig)
    assert not verify_media_signature("projects/p/audio/a.mp3", exp, sig, download=True)
    assert not verify_media_signature("projects/p/audio/a.mp3", exp, sig[:-1] + ("0" if sig[-1] != "0" else "1"))
    assert not verify_media_signature("projects/p/audio/a.mp3", exp, sig, now=time.time() + 120)
    assert not verify_media_signature("projects/p/audio/a.mp3", "not-int", sig)
    assert not verify_media_signature("projects/p/audio/a.mp3", None, None)


def test_media_signature_is_stable_within_a_window(monkeypatch) -> None:
    """Polling clients must get identical URLs (browser cache hits) for a while."""
    import app.security as security

    now = 1_800_000_001.0  # now + ttl is 1 s past a 300 s boundary → rounded up to the next one
    monkeypatch.setattr(security.time, "time", lambda: now)
    first = sign_media_url("projects/p/render/v.mp4", ttl=3600)
    monkeypatch.setattr(security.time, "time", lambda: now + 250)
    assert sign_media_url("projects/p/render/v.mp4", ttl=3600) == first
    query = dict(part.split("=", 1) for part in first.split("?", 1)[1].split("&"))
    assert int(query["exp"]) == 1_800_003_900
    # Always valid for at least the TTL.
    monkeypatch.setattr(security.time, "time", lambda: now + 1)
    later = sign_media_url("projects/p/render/v.mp4", ttl=3600)
    later_exp = int(dict(part.split("=", 1) for part in later.split("?", 1)[1].split("&"))["exp"])
    assert now + 1 + 3600 <= later_exp <= now + 1 + 3600 + 300


def test_removing_a_user_from_api_tokens_revokes_the_token(make_client: Callable[..., TestClient], db: Session) -> None:
    both = make_client(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE},bob:{BOB}")
    project = both.post("/api/projects", json={"title": "t", "artist_name": "a"}, headers=_auth(BOB)).json()
    assert both.get("/api/projects", headers=_auth(BOB)).status_code == 200

    alice_only = make_client(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE}")
    assert alice_only.get("/api/projects", headers=_auth(BOB)).status_code == 401
    assert alice_only.get("/api/projects", headers=_auth(ALICE)).status_code == 200

    restored = make_client(AUTH_MODE="token", API_TOKENS=f"alice:{ALICE},bob:{BOB}")
    listed = restored.get("/api/projects", headers=_auth(BOB)).json()
    assert [p["id"] for p in listed] == [project["id"]]  # same user again: projects kept
