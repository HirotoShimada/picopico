"""§5.4 lyrics endpoints: text/file input, line edits, auto-timing (heuristic / Whisper), export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models import Project

DEMO_LINE_STARTS = (8, 12, 16, 20, 24, 28, 32, 36, 48, 52)


def _url(project: Project, suffix: str = "") -> str:
    return f"/api/projects/{project.id}/lyrics{suffix}"


def test_put_text_keeps_it_verbatim(client: TestClient, ready_project: Project, demo_dir: Path) -> None:
    text = (demo_dir / "demo_lyrics_ja.txt").read_text(encoding="utf-8") + "\n\n  最後の行　全角スペース  \n"
    body = client.put(_url(ready_project), json={"text": text}).json()
    assert body["format"] == "txt" and len(body["lines"]) == 11
    assert body["lines"][0] == {
        "id": "l001",
        "text": "夜明け前の街を ひとりで歩いた",
        "start": None,
        "end": None,
        "words": None,
        "source": "txt",
        "confidence": None,
    }
    assert body["lines"][-1]["text"] == "最後の行　全角スペース"
    stored = client.get(_url(ready_project)).json()
    assert stored["text"] == text and stored["format"] == "txt" and len(stored["lines"]) == 11


def test_put_lrc_is_detected_and_timed(client: TestClient, ready_project: Project, demo_dir: Path) -> None:
    text = (demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8")
    body = client.put(_url(ready_project), json={"text": text, "format": "auto"}).json()
    assert body["format"] == "lrc"
    assert [line["start"] for line in body["lines"]] == [float(s) for s in DEMO_LINE_STARTS]


def test_upload_shift_jis_and_lrc_files(client: TestClient, ready_project: Project, demo_dir: Path) -> None:
    sjis = "サビ　叫べ 今この瞬間を\n忘れないように\n".encode("cp932")
    body = client.post(_url(ready_project, "/upload"), files={"file": ("歌詞.txt", sjis, "text/plain")}).json()
    assert body["format"] == "txt" and body["lines"][0]["text"] == "サビ　叫べ 今この瞬間を"

    lrc = (demo_dir / "demo_lyrics_ja.lrc").read_bytes()
    body = client.post(_url(ready_project, "/upload"), files={"file": ("demo.lrc", lrc, "text/plain")}).json()
    assert body["format"] == "lrc" and body["lines"][4]["start"] == 24.0


def test_upload_rejections(client: TestClient, ready_project: Project) -> None:
    bad_ext = client.post(_url(ready_project, "/upload"), files={"file": ("lyrics.docx", b"x", "application/x")})
    assert bad_ext.status_code == 400 and "対応していない" in bad_ext.json()["detail"]
    big = b"a\n" * (600 * 1024)  # 1.2 MB, below the 2 MiB request cap
    too_big = client.post(_url(ready_project, "/upload"), files={"file": ("big.txt", big, "text/plain")})
    assert too_big.status_code == 413
    binary = client.post(_url(ready_project, "/upload"), files={"file": ("x.txt", b"\x00\x01\x02", "text/plain")})
    assert binary.status_code == 400 and binary.json()["code"] == "lyrics_invalid"


def test_put_lines_rescales_or_drops_words(client: TestClient, ready_project: Project) -> None:
    line = {
        "id": "l001",
        "text": "叫べ 今",
        "start": 10.0,
        "end": 12.0,
        "source": "lrc",
        "confidence": None,
        "words": [{"text": "叫べ", "start": 10.0, "end": 11.0}, {"text": "今", "start": 11.0, "end": 12.0}],
    }
    moved = {**line, "start": 20.0, "end": 24.0, "source": "manual"}
    retexted = {**line, "id": "l002", "text": "叫べ 明日"}
    body = client.put(_url(ready_project, "/lines"), json={"lines": [moved, retexted]}).json()
    first, second = body["lines"]
    assert first["words"] == [{"text": "叫べ", "start": 20.0, "end": 22.0}, {"text": "今", "start": 22.0, "end": 24.0}]
    assert first["source"] == "manual"
    assert second["words"] is None and second["text"] == "叫べ 明日"
    assert client.get(_url(ready_project)).json()["lines"][0]["start"] == 20.0

    duplicate = client.put(_url(ready_project, "/lines"), json={"lines": [line, line]})
    assert duplicate.status_code == 422
    reversed_times = client.put(_url(ready_project, "/lines"), json={"lines": [{**line, "start": 5, "end": 4}]})
    assert reversed_times.status_code == 422


def test_heuristic_auto_time_follows_vocals(client: TestClient, ready_project: Project, demo_dir: Path) -> None:
    client.put(_url(ready_project), json={"text": (demo_dir / "demo_lyrics_ja.txt").read_text(encoding="utf-8")})
    response = client.post(_url(ready_project, "/auto-time"), json={"method": "heuristic"})
    assert response.status_code == 200
    body = response.json()
    assert body["method_used"] == "heuristic"
    starts = [line["start"] for line in body["lines"]]
    assert all(abs(s - t) < 0.3 for s, t in zip(starts, DEMO_LINE_STARTS, strict=True)), starts
    assert all(line["source"] == "heuristic" and line["words"] for line in body["lines"])
    assert client.get(_url(ready_project)).json()["lines"][0]["start"] == pytest.approx(8.0, abs=0.3)


def test_auto_time_requires_lyrics(client: TestClient, ready_project: Project) -> None:
    client.put(_url(ready_project), json={"text": ""})
    response = client.post(_url(ready_project, "/auto-time"), json={"method": "heuristic"})
    assert response.status_code == 422 and response.json()["code"] == "lyrics_missing"
    assert client.post(_url(ready_project, "/auto-time"), json={"method": "x"}).status_code == 422


def test_whisper_unavailable_is_503(client: TestClient, ready_project: Project, monkeypatch: Any) -> None:
    import app.services.lyrics_align as lyrics_align

    monkeypatch.setattr(lyrics_align, "whisper_available", lambda model_size=None: (False, "モデルがありません"))
    response = client.post(_url(ready_project, "/auto-time"), json={"method": "whisper"})
    assert response.status_code == 503
    assert response.json() == {"detail": "モデルがありません", "code": "whisper_unavailable"}


def test_whisper_in_this_environment(client: TestClient, ready_project: Project) -> None:
    """No Whisper model is cached here (huggingface.co is blocked), so the real check must answer 503."""
    from app.services.lyrics_align import whisper_available

    available, reason = whisper_available()
    if available:
        pytest.skip("a local Whisper model is installed")
    response = client.post(_url(ready_project, "/auto-time"), json={"method": "whisper"})
    assert response.status_code == 503 and response.json()["detail"] == reason


def test_whisper_alignment_path(client: TestClient, ready_project: Project, monkeypatch: Any) -> None:
    import app.services.lyrics_align as lyrics_align

    seen: dict[str, Any] = {}

    def fake_align(audio_path: str, lines: list[dict[str, Any]], **kwargs: Any) -> tuple[list[Any], list[str]]:
        seen["path"], seen["kwargs"] = audio_path, kwargs
        return [{**line, "source": "whisper", "confidence": 0.9} for line in lines], ["整列しました"]

    monkeypatch.setattr(lyrics_align, "whisper_available", lambda model_size=None: (True, None))
    monkeypatch.setattr(lyrics_align, "align_lines", fake_align)
    body = client.post(_url(ready_project, "/auto-time"), json={"method": "whisper", "start": 20, "end": 40}).json()
    assert body["method_used"] == "whisper" and "整列しました" in body["warnings"]
    assert all(line["source"] == "whisper" for line in body["lines"])
    assert Path(seen["path"]).is_file() and seen["kwargs"] == {"start": 20.0, "end": 40.0}


def test_export_lrc_and_srt(client: TestClient, ready_project: Project) -> None:
    lrc = client.get(_url(ready_project, "/export"), params={"format": "lrc"})
    assert lrc.status_code == 200 and lrc.headers["content-type"].startswith("text/plain")
    assert "attachment" in lrc.headers["content-disposition"] and ".lrc" in lrc.headers["content-disposition"]
    assert "[ti:夜明けのうた]" in lrc.text and "[00:24.00]叫べ 今この瞬間を" in lrc.text
    srt = client.get(_url(ready_project, "/export"), params={"format": "srt"})
    assert srt.status_code == 200 and "00:00:08,000 --> 00:00:12,000" in srt.text
    assert client.get(_url(ready_project, "/export"), params={"format": "doc"}).status_code == 422
