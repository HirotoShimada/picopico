"""Song analysis status/result, re-run and segment selection (ARCHITECTURE.md §5.3)."""

from __future__ import annotations

from fastapi import APIRouter, status

from app import worker
from app.db import DbSession
from app.models import AnalysisStatus
from app.pipeline import song_duration
from app.schemas import AnalysisOut, AnalysisRerunOut, Segment, SegmentOut, SegmentUpdate
from app.security import CurrentUser, get_owned_project
from app.services.errors import ServiceError

router = APIRouter(tags=["analysis"])

_DURATION_TOLERANCE = 0.05


def _segment(start: float | None, end: float | None) -> Segment | None:
    if start is None or end is None:
        return None
    return Segment(start=start, end=end)


@router.get("/projects/{project_id}/analysis", response_model=AnalysisOut)
def get_analysis(project_id: str, db: DbSession, user: CurrentUser) -> AnalysisOut:
    project = get_owned_project(db, project_id, user)
    return AnalysisOut(
        status=project.analysis_status,  # type: ignore[arg-type]
        error=project.analysis_error,
        analysis=project.analysis,
        segment=_segment(project.segment_start, project.segment_end),
    )


@router.post(
    "/projects/{project_id}/analysis/rerun",
    response_model=AnalysisRerunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def rerun_analysis(project_id: str, db: DbSession, user: CurrentUser) -> AnalysisRerunOut:
    project = get_owned_project(db, project_id, user)
    if project.audio_asset_id is None:
        raise ServiceError(
            "解析する楽曲がありません。先に楽曲をアップロードしてください。", status_code=409, code="audio_missing"
        )
    if project.analysis_status == AnalysisStatus.RUNNING:
        return AnalysisRerunOut(status=project.analysis_status)  # type: ignore[arg-type]
    project.analysis_status = AnalysisStatus.QUEUED
    project.analysis_error = None
    db.commit()
    try:
        worker.enqueue("analyze", project.id)
    except worker.QueueUnavailable:
        project.analysis_status = AnalysisStatus.FAILED
        project.analysis_error = "ジョブキューに接続できないため解析を開始できませんでした。"
        db.commit()
        raise
    return AnalysisRerunOut(status=project.analysis_status)  # type: ignore[arg-type]


@router.put("/projects/{project_id}/segment", response_model=SegmentOut)
def update_segment(project_id: str, body: SegmentUpdate, db: DbSession, user: CurrentUser) -> SegmentOut:
    project = get_owned_project(db, project_id, user)
    if project.audio_asset_id is None:
        raise ServiceError("先に楽曲をアップロードしてください。", status_code=409, code="audio_missing")
    duration = song_duration(project)
    if duration is not None and body.end > duration + _DURATION_TOLERANCE:
        raise ServiceError(
            f"区間は曲の長さ（{duration:.2f} 秒）以内で指定してください。", status_code=422, code="segment_out_of_range"
        )
    end = min(body.end, duration) if duration is not None else body.end
    project.segment_start = round(body.start, 3)
    project.segment_end = round(end, 3)
    db.commit()
    return SegmentOut(segment=Segment(start=project.segment_start, end=project.segment_end))
