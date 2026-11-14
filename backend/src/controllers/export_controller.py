"""导出 controller：不可变发布包的发起、列表、详情与封版后下载（含审计）。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, export_package_service


def list_packages(db: Session, limit: int) -> dict:
    return {"items": export_package_service.list_packages(db, limit)}


def get_package(db: Session, code: str) -> dict:
    package = export_package_service.get_package(db, code)
    return {"package": export_package_service.package_to_dict(package)}


def create_package(db: Session, principal: dict, request: Request) -> dict:
    package, reused = export_package_service.create_package(db, principal)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="export.package.publish",
        entity="export_package",
        entity_id=package.code,
        detail={
            "reused": reused,
            "status": package.status,
            "episodes": package.episode_rows,
            "chapters": package.chapter_rows,
            "issues": package.issue_rows,
            "digest_task_code": package.digest_task_code,
            "zip_sha256": package.zip_sha256,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    db.refresh(package)
    return {"package": export_package_service.package_to_dict(package), "reused": reused}


def download_file(db: Session, principal: dict, request: Request, code: str, filename: str):
    data, media, download_name = export_package_service.read_artifact(db, code, filename)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="export.package.download",
        entity="export_package",
        entity_id=code,
        detail={"file": filename, "bytes": len(data)},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return data, media, download_name


def download_zip(db: Session, principal: dict, request: Request, code: str):
    data, download_name = export_package_service.read_zip(db, code)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="export.package.download_zip",
        entity="export_package",
        entity_id=code,
        detail={"file": download_name, "bytes": len(data)},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return data, download_name
