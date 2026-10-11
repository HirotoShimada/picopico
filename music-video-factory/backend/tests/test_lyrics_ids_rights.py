"""Re-saved lyrics keep their line ids (per-video overrides stay on the same lyric) and changed content
un-confirms the matching rights checklist item (audio_rights / lyrics_rights)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Batch, Project, Video
from app.pipeline import AUDIO_RIGHTS_RESET_MESSAGE, LYRICS_RIGHTS_RESET_MESSAGE, carry_over_lyric_lines


def _line(line_id: str, text: str, start: float | None = None, end: float | None = None, **extra: Any) -> dict:
    return {"id": line_id, "text": text, "start": start, "end": end, "words": None, "source": "txt",
            "confidence": None, **extra}  # fmt: skip


def _parsed(*texts: str) -> list[dict]:
    """Lines as the TXT parser numbers them (l001, l002, … in text order, untimed)."""
    return [_line(f"l{i:03d}", text) for i, text in enumerate(texts, start=1)]


OLD = [
    _line("l001", "夜明け前の街を", 8.0, 12.0, source="heuristic", confidence=0.3,
          words=[{"text": "夜明け前の", "start": 8.0, "end": 10.0}, {"text": "街を", "start": 10.0, "end": 12.0}]),
    _line("l002", "消えそうな光を", 12.0, 16.0, source="manual"),
    _line("l003", "叫べ 今この瞬間を", 16.0, 20.0, source="heuristic"),
]  # fmt: skip


def test_inserted_line_gets_a_fresh_id_and_others_keep_id_and_timing() -> None:
    lines = carry_over_lyric_lines(OLD, _parsed("新しい最初の行", "夜明け前の街を", "消えそうな光を", "叫べ 今この瞬間を"))
    assert [line["id"] for line in lines] == ["l004", "l001", "l002", "l003"]
    assert lines[0]["start"] is None and lines[0]["source"] == "txt"
    for new, old in zip(lines[1:], OLD, strict=True):
        assert new == old  # timing, karaoke words, source and confidence carried over


def test_removed_and_edited_lines() -> None:
    lines = carry_over_lyric_lines(OLD, _parsed("夜明け前の街を", "叫べ　今この瞬間を！"))
    assert [line["id"] for line in lines] == ["l001", "l003"]  # l002 removed; the edited line keeps its id
    assert lines[1]["text"] == "叫べ　今この瞬間を！" and lines[1]["start"] is None  # changed text: timing not kept
    # An unrelated replacement line is a new line.
    unrelated = carry_over_lyric_lines(OLD, _parsed("夜明け前の街を", "まったく別の歌詞です"))
    assert [line["id"] for line in unrelated] == ["l001", "l004"]


def test_repeated_chorus_lines_align_in_order() -> None:
    old = [_line(f"l{i:03d}", text, float(i)) for i, text in enumerate(["A", "サビ", "B", "サビ"], start=1)]
    lines = carry_over_lyric_lines(old, _parsed("A", "サビ", "追加", "B", "サビ"))
    assert [line["id"] for line in lines] == ["l001", "l002", "l005", "l003", "l004"]
    assert [line["start"] for line in lines] == [1.0, 2.0, None, 3.0, 4.0]


def test_fresh_ids_skip_ids_still_referenced_by_videos() -> None:
    lines = carry_over_lyric_lines(OLD[:1], _parsed("夜明け前の街を", "新しい行"), reserved_ids={"l007", "x"})
    assert [line["id"] for line in lines] == ["l001", "l008"]
    assert [line["id"] for line in carry_over_lyric_lines([], _parsed("a", "b"))] == ["l001", "l002"]


def test_timed_input_is_authoritative() -> None:
    lrc = [_line("l001", "夜明け前の街を", 9.0, 11.0, source="lrc")]
    (line,) = carry_over_lyric_lines(OLD, lrc)
    assert line["id"] == "l001" and (line["start"], line["end"], line["source"], line["words"]) == (9.0, 11.0, "lrc", None)


# --- API ------------------------------------------------------------------------------------------------------


def _url(project: Project, suffix: str = "") -> str:
    return f"/api/projects/{project.id}/lyrics{suffix}"


def _video_with_offsets(db: Session, project: Project, offsets: dict[str, dict[str, float]]) -> Video:
    batch = Batch(project_id=project.id, status="completed", settings={})
    db.add(batch)
    db.flush()
    video = Video(project_id=project.id, batch_id=batch.id, status="done", plan={"start": 0.0, "duration": 15.0,
                  "lyric_offsets": offsets})  # fmt: skip
    db.add(video)
    db.commit()
    return video


def test_video_overrides_follow_their_lyric_after_inserting_a_line(
    client: TestClient, db: Session, ready_project: Project, demo_dir: Path
) -> None:
    second = ready_project.lyric_lines[1]
    assert second["id"] == "l002"
    video = _video_with_offsets(db, ready_project, {"l002": {"start": 12.5, "end": 15.5}})
    lrc = (demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8")
    inserted = lrc.replace("[00:08.00]", "[00:04.00]新しい最初の行\n[00:08.00]", 1)

    body = client.put(_url(ready_project), json={"text": inserted}).json()
    assert [line["id"] for line in body["lines"][:3]] == ["l011", "l001", "l002"]

    detail = client.get(f"/api/videos/{video.id}").json()
    by_id = {line["id"]: line for line in detail["lyric_lines"]}
    assert by_id["l002"]["text"] == second["text"]
    assert (by_id["l002"]["start"], by_id["l002"]["end"]) == (12.5, 15.5)  # still on the same lyric
    assert by_id["l011"]["text"] == "新しい最初の行" and by_id["l011"]["start"] == 4.0
    assert by_id["l001"]["start"] == 8.0  # the line before it is not overridden


def test_resaving_identical_lyrics_keeps_ids_timing_and_rights(client: TestClient, ready_project: Project) -> None:
    before = client.get(f"/api/projects/{ready_project.id}").json()
    txt = "\n".join(line["text"] for line in ready_project.lyric_lines) + "\n"
    body = client.put(_url(ready_project), json={"text": txt}).json()
    assert body["format"] == "txt" and body["warnings"] == []
    assert body["lines"] == before["lyric_lines"]  # same ids, LRC timing kept for the untimed TXT lines
    after = client.get(f"/api/projects/{ready_project.id}").json()
    assert after["rights"]["lyrics_rights"] is True and after["rights_confirmed_at"] == before["rights_confirmed_at"]


def test_changing_lyric_text_resets_lyrics_rights(client: TestClient, ready_project: Project, demo_dir: Path) -> None:
    lrc = (demo_dir / "demo_lyrics_ja.lrc").read_text(encoding="utf-8")
    retimed = client.put(_url(ready_project), json={"text": lrc.replace("[00:12.00]", "[00:12.50]")}).json()
    # timing only: rights kept (the demo LRC's own parse notice about the bridge may still appear)
    assert LYRICS_RIGHTS_RESET_MESSAGE not in retimed["warnings"] and retimed["lines"][1]["start"] == 12.5

    changed = client.put(_url(ready_project), json={"text": lrc.replace("ひとりで歩いた", "ふたりで歩いた")}).json()
    assert changed["warnings"][0] == LYRICS_RIGHTS_RESET_MESSAGE
    project = client.get(f"/api/projects/{ready_project.id}").json()
    assert project["rights"]["lyrics_rights"] is False and project["rights_confirmed_at"] is None
    assert project["rights"]["audio_rights"] is True  # only the item the change concerns
    guard = client.post(f"/api/projects/{ready_project.id}/batches", json={"provider": "mock"})
    assert guard.status_code == 422 and guard.json()["code"] == "rights_unconfirmed"
    # Already unconfirmed: no repeated warning.
    again = client.put(_url(ready_project), json={"text": lrc}).json()
    assert LYRICS_RIGHTS_RESET_MESSAGE not in again["warnings"]


def test_upload_with_new_text_resets_lyrics_rights(client: TestClient, ready_project: Project) -> None:
    data = "まったく新しい歌詞\n二行目\n".encode()
    body = client.post(_url(ready_project, "/upload"), files={"file": ("new.txt", data, "text/plain")}).json()
    assert LYRICS_RIGHTS_RESET_MESSAGE in body["warnings"]
    assert client.get(f"/api/projects/{ready_project.id}").json()["rights"]["lyrics_rights"] is False


def test_line_edits_reset_lyrics_rights_only_for_text_changes(client: TestClient, ready_project: Project) -> None:
    lines = [dict(line) for line in ready_project.lyric_lines]
    lines[0] = {**lines[0], "start": 8.2, "end": 11.8}
    timing = client.put(_url(ready_project, "/lines"), json={"lines": lines}).json()
    assert timing["warnings"] == [] and timing["lines"][0]["start"] == 8.2
    assert client.get(f"/api/projects/{ready_project.id}").json()["rights"]["lyrics_rights"] is True

    lines[0] = {**lines[0], "text": "書き換えた歌詞"}
    edited = client.put(_url(ready_project, "/lines"), json={"lines": lines}).json()
    assert edited["warnings"] == [LYRICS_RIGHTS_RESET_MESSAGE]
    project = client.get(f"/api/projects/{ready_project.id}").json()
    assert project["rights"]["lyrics_rights"] is False and project["rights_confirmed_at"] is None


def test_replacing_the_song_resets_audio_rights(
    client: TestClient, ready_project: Project, demo_dir: Path, enqueued: list
) -> None:
    with (demo_dir / "demo_song.wav").open("rb") as fh:
        response = client.post(
            f"/api/projects/{ready_project.id}/audio", files={"file": ("new_song.wav", fh, "audio/wav")}
        )
    assert response.status_code == 201
    body = response.json()
    assert body["warnings"] == [AUDIO_RIGHTS_RESET_MESSAGE]
    assert body["rights"]["audio_rights"] is False and body["rights_confirmed_at"] is None
    assert body["rights"]["lyrics_rights"] is True
    assert client.get(f"/api/projects/{ready_project.id}").json()["warnings"] == []  # a notice of that request only


def test_song_upload_without_confirmed_rights_has_no_warning(
    client: TestClient, demo_dir: Path, enqueued: list
) -> None:
    project_id = client.post("/api/projects", json={"title": "曲", "artist_name": "A"}).json()["id"]
    with (demo_dir / "demo_song.mp3").open("rb") as fh:
        body = client.post(f"/api/projects/{project_id}/audio", files={"file": ("s.mp3", fh, "audio/mpeg")}).json()
    assert body["warnings"] == [] and body["rights"]["audio_rights"] is False


def test_heading_lines_stored_before_they_were_excluded_do_not_reset_lyrics_rights(
    client: TestClient, db: Session, ready_project: Project
) -> None:
    """Older versions stored heading-only lines (``[サビ]``) as lyric lines. Re-saving the very same text
    now drops them from the lines; that is no change of the sung lyrics, so ``lyrics_rights`` stays."""
    sung = [line["text"] for line in ready_project.lyric_lines[:4]]
    text = "\n".join(["[Aメロ]", sung[0], sung[1], "サビ：", sung[2], sung[3]]) + "\n"
    ready_project.lyrics_text, ready_project.lyrics_format = text, "txt"
    ready_project.lyric_lines = [  # as the TXT parser stored them before headings were excluded
        _line(f"l{i:03d}", line_text) for i, line_text in enumerate(text.splitlines(), start=1)
    ]
    db.commit()

    body = client.put(_url(ready_project), json={"text": text}).json()
    assert [line["text"] for line in body["lines"]] == sung
    assert LYRICS_RIGHTS_RESET_MESSAGE not in body["warnings"]
    assert any("見出し" in warning for warning in body["warnings"])  # the parser still says what it left out
    project = client.get(f"/api/projects/{ready_project.id}").json()
    assert project["rights"]["lyrics_rights"] is True and project["rights_confirmed_at"] is not None

    # Deleting such a stale heading line in the timing table is no text change either.
    db.expire_all()
    stored = [_line(f"l{i:03d}", line_text) for i, line_text in enumerate(text.splitlines(), start=1)]
    ready_project.lyric_lines = stored
    db.commit()
    kept = [line for line in stored if line["text"] in sung]
    edited = client.put(_url(ready_project, "/lines"), json={"lines": kept}).json()
    assert [line["text"] for line in edited["lines"]] == sung and edited["warnings"] == []
    assert client.get(f"/api/projects/{ready_project.id}").json()["rights"]["lyrics_rights"] is True
