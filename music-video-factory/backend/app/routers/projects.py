"""Project CRUD and the rights checklist (ARCHITECTURE.md §5.2, §9)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import DbSession
from app.models import ACTIVE_BATCH_STATUSES, Batch, Project, utcnow
from app.schemas import ProjectCreate, ProjectOut, ProjectSummary, ProjectUpdate, RightsUpdate
from app.security import CurrentUser, get_owned_project
from app.serializers import all_rights_confirmed, to_project_out, to_project_summaries
from app.storage import get_storage, project_prefix

logger = logging.getLogger(__name__)

router = APIRouter(tags=["projects"])


def has_active_batch(db: Session, project_id: str) -> bool:
    """True while a batch of the project is queued/planning/generating/rendering."""
    stmt = select(Batch.id).where(Batch.project_id == project_id, Batch.status.in_(ACTIVE_BATCH_STATUSES)).limit(1)
    return db.scalar(stmt) is not None


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(db: DbSession, user: CurrentUser) -> list[ProjectSummary]:
    projects = db.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.updated_at.desc())).all()
    return to_project_summaries(db, projects)


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = Project(owner_id=user.id, **body.model_dump())
    db.add(project)
    db.commit()
    return to_project_out(db, project)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: DbSession, user: CurrentUser) -> ProjectOut:
    return to_project_out(db, get_owned_project(db, project_id, user))


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, body: ProjectUpdate, db: DbSession, user: CurrentUser) -> ProjectOut:
    project = get_owned_project(db, project_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    return to_project_out(db, project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: DbSession, user: CurrentUser) -> Response:
    project = get_owned_project(db, project_id, user)
    if has_active_batch(db, project.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="生成中のバッチがあるため削除できません。先にバッチをキャンセルしてください。",
        )
    prefix = project_prefix(project.id)
    db.delete(project)
    db.commit()
    try:
        removed = get_storage().delete_prefix(prefix)
        logger.info("deleted project %s (%d stored files)", project_id, removed)
    except Exception:
        logger.exception("project %s deleted but its files under %s could not be removed", project_id, prefix)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/projects/{project_id}/rights", response_model=ProjectOut)
def update_rights(project_id: str, body: RightsUpdate, db: DbSession, user: CurrentUser) -> ProjectOut:
    """Merge checklist answers; ``rights_confirmed_at`` is set once every required item is true."""
    project = get_owned_project(db, project_id, user)
    rights = {**(project.rights or {}), **body.items}
    project.rights = rights
    if all_rights_confirmed(rights):
        if project.rights_confirmed_at is None:
            project.rights_confirmed_at = utcnow()
    else:
        project.rights_confirmed_at = None
    db.commit()
    return to_project_out(db, project)
