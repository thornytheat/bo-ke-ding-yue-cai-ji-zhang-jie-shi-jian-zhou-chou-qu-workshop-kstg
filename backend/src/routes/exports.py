"""导出路由：不可变发布包。

- POST /exports/packages            发起封版（同数据重试复用，不产生新版本）
- GET  /exports/packages            当前包 + 历史包状态 / 行数 / 差异
- GET  /exports/packages/{code}     单包详情
- GET  /exports/packages/{code}/zip 整包下载（封版且校验通过才开放）
- GET  /exports/packages/{code}/files/{filename} 包内单文件下载（历史包同样可下载）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import EXPORT_PUBLISH_ROLES, EXPORT_ROLES
from src.controllers import export_controller
from src.middlewares.rbac import require_roles

router = APIRouter(prefix="/exports", tags=["exports"])


def _file_response(data: bytes, media: str, filename: str) -> Response:
    return Response(
        content=data,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/packages")
def create_package(
    request: Request,
    principal: dict = Depends(require_roles(*EXPORT_PUBLISH_ROLES)),
    db: Session = Depends(get_db),
) -> dict:
    return export_controller.create_package(db, principal, request)


@router.get("/packages")
def list_packages(
    limit: int = Query(default=50, ge=1, le=200),
    principal: dict = Depends(require_roles(*EXPORT_ROLES)),
    db: Session = Depends(get_db),
) -> dict:
    return export_controller.list_packages(db, limit)


@router.get("/packages/{code}")
def get_package(
    code: str,
    principal: dict = Depends(require_roles(*EXPORT_ROLES)),
    db: Session = Depends(get_db),
) -> dict:
    return export_controller.get_package(db, code)


@router.get("/packages/{code}/zip")
def download_zip(
    code: str,
    request: Request,
    principal: dict = Depends(require_roles(*EXPORT_ROLES)),
    db: Session = Depends(get_db),
) -> Response:
    data, filename = export_controller.download_zip(db, principal, request, code)
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/packages/{code}/files/{filename}")
def download_file(
    code: str,
    filename: str,
    request: Request,
    principal: dict = Depends(require_roles(*EXPORT_ROLES)),
    db: Session = Depends(get_db),
) -> Response:
    data, media, download_name = export_controller.download_file(db, principal, request, code, filename)
    return _file_response(data, media, download_name)
