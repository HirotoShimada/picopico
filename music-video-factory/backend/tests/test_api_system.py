"""§5.1 ``GET /api/system/capabilities``."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.schemas import CapabilitiesOut


def test_capabilities_shape(client: TestClient) -> None:
    response = client.get("/api/system/capabilities")
    assert response.status_code == 200
    body = response.json()
    CapabilitiesOut.model_validate(body)
    providers = {p["id"]: p for p in body["providers"]}
    assert list(providers) == ["mock", "library", "veo", "runway", "kling"]
    assert providers["mock"]["configured"] and providers["mock"]["available"] and providers["mock"]["kind"] == "mock"
    assert providers["library"]["kind"] == "library"
    for api in ("veo", "runway", "kling"):
        assert providers[api]["kind"] == "api" and providers[api]["configured"] is False and providers[api]["reason"]
        assert all(m["verified"] is False and m["price_per_second_usd"] > 0 for m in providers[api]["models"])
    assert body["claude_configured"] is False
    assert body["whisper"]["available"] in (True, False)
    if not body["whisper"]["available"]:
        assert body["whisper"]["reason"]
    assert {p["id"] for p in body["presets"]} == {
        "cinematic",
        "emotional",
        "urban_night",
        "nostalgic",
        "nature",
        "dark",
        "dreamy",
        "documentary",
        "custom",
    }
    assert [s["id"] for s in body["lyric_styles"]] == ["simple", "cinematic", "minimal", "karaoke", "fade"]
    assert {"natural", "teal_orange", "mono_noir"} <= {g["id"] for g in body["color_grades"]}
    assert {f["family"] for f in body["fonts"]} == {"Noto Sans CJK JP", "Noto Serif CJK JP"}
    assert body["durations"] == [15, 30, 60] and body["counts"] == [1, 3, 5, 10, 30]
    assert body["limits"] == {
        "max_audio_mb": 100,
        "max_video_mb": 500,
        "max_count": 50,
        "min_duration": 5,
        "max_duration": 90,
    }


def test_capabilities_never_expose_keys(make_client: Any) -> None:
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-test-secret-value-123",
        "GOOGLE_API_KEY": "google-test-secret-value-456",
        "RUNWAY_API_KEY": "runway-test-secret-value-789",
        "KLING_API_KEY": "kling-test-secret-value-000",
    }
    client = make_client(**secrets)
    response = client.get("/api/system/capabilities")
    text = response.text
    assert all(value not in text for value in secrets.values())
    body = response.json()
    providers = {p["id"]: p for p in body["providers"]}
    assert body["claude_configured"] is True
    assert providers["veo"]["configured"] and providers["runway"]["configured"] and providers["kling"]["configured"]


def test_capabilities_require_auth_in_token_mode(make_client: Any) -> None:
    client = make_client(AUTH_MODE="token", API_TOKENS="alice:alice-token-0123456789abcdef")
    assert client.get("/api/system/capabilities").status_code == 401
    ok = client.get("/api/system/capabilities", headers={"Authorization": "Bearer alice-token-0123456789abcdef"})
    assert ok.status_code == 200
