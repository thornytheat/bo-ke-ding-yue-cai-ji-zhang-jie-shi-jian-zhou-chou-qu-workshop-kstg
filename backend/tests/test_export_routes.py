"""发布包路由 HTTP 级测试：JWT 鉴权 / RBAC / JSON 契约 / 下载响应头。

用 SQLite 内存库覆盖 get_db 依赖；限流中间件在无 Redis 时自动降级，不阻塞。
"""
from __future__ import annotations

import datetime
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.app import create_app
from src.config.database import get_db
from src.config.settings import settings
from src.constants.enums import ChapterSource, ParseStatus, Role
from src.models import Base, Chapter, Episode, Feed, User
from src.services import export_service
from src.utils.security import create_access_token


@asynccontextmanager
async def _null_lifespan(app):  # noqa: ARG001
    yield


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "archive"))

    with Session() as seed:
        seed.add(User(email="viewer@podcastdig.io", password_hash="x", display_name="剪辑师", role=Role.VIEWER.value))
        seed.add(User(email="admin@podcastdig.io", password_hash="x", display_name="主管", role=Role.ADMIN.value))
        feed = Feed(
            code="rss1", name="n", show_name="节目", kind="rss", url="u", fixture_path="f.xml",
            cadence="weekly", host="h", active=True, last_status="never", sort_order=0,
        )
        seed.add(feed)
        seed.flush()
        ep = Episode(
            code="EP-001", feed_id=feed.id, guid="g1", title="一集", duration_seconds=1800,
            published_on=datetime.date(2026, 11, 1), summary="", show_notes="notes",
            ingest_state="synced", parse_status=ParseStatus.PARSED.value, content_hash="hh",
        )
        seed.add(ep)
        seed.flush()
        for ordinal, seconds in enumerate((0, 300)):
            seed.add(
                Chapter(
                    episode_id=ep.id, ordinal=ordinal, start_seconds=seconds, title=f"章{ordinal}",
                    raw_text=str(seconds), parse_rule="mm_ss", confidence=0.86, ambiguous=False,
                    source=ChapterSource.PARSE.value,
                )
            )
        seed.commit()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    # 跳过启动期真实建表/seed
    app.router.lifespan_context = _null_lifespan
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


def _auth(role: Role) -> dict:
    email = {"admin": "admin@podcastdig.io", "viewer": "viewer@podcastdig.io"}[role.value]
    token, _ = create_access_token(email, role.value)
    return {"Authorization": f"Bearer {token}"}


def test_requires_authentication(client):
    response = client.post("/api/exports/packages")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_MISSING"


def test_viewer_can_read_and_download_but_cannot_publish(client):
    viewer = _auth(Role.VIEWER)

    # viewer 发起封版 → 403（仅运营主管/内容运营可发布新版本）
    forbidden = client.post("/api/exports/packages", headers=viewer)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"

    # admin 先封一个包
    admin = _auth(Role.ADMIN)
    code = client.post("/api/exports/packages", headers=admin).json()["package"]["code"]

    # viewer 仍可查看列表与下载历史/当前包
    assert client.get("/api/exports/packages", headers=viewer).status_code == 200
    file_resp = client.get(
        f"/api/exports/packages/{code}/files/{export_service.CLIP_FILENAME}", headers=viewer
    )
    assert file_resp.status_code == 200
    assert client.get(f"/api/exports/packages/{code}/zip", headers=viewer).status_code == 200


def test_publish_list_and_download_flow(client):
    headers = _auth(Role.ADMIN)

    # 发起封版
    response = client.post("/api/exports/packages", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reused"] is False
    package = body["package"]
    assert package["status"] == "sealed"
    assert package["episode_rows"] == 1
    assert package["chapter_rows"] == 2
    code = package["code"]

    # 列表：当前包标记 + 行数
    listed = client.get("/api/exports/packages", headers=headers).json()["items"]
    assert listed[0]["code"] == code
    assert listed[0]["is_current"] is True
    assert listed[0]["episode_rows"] == 1

    # 重试：复用，不新增版本
    again = client.post("/api/exports/packages", headers=headers).json()
    assert again["reused"] is True
    assert again["package"]["code"] == code
    assert len(client.get("/api/exports/packages", headers=headers).json()["items"]) == 1

    # 单文件下载：带 Content-Disposition，前缀含包 code（可追溯）
    file_resp = client.get(
        f"/api/exports/packages/{code}/files/{export_service.CLIP_FILENAME}", headers=headers
    )
    assert file_resp.status_code == 200
    assert file_resp.headers["content-type"].startswith("text/csv")
    assert code in file_resp.headers["content-disposition"]
    assert file_resp.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM

    # 整包 zip 下载
    zip_resp = client.get(f"/api/exports/packages/{code}/zip", headers=headers)
    assert zip_resp.status_code == 200
    assert zip_resp.headers["content-type"] == "application/zip"
    assert zip_resp.headers["content-disposition"].endswith(f'{code}.zip"')

    # 不存在的包 → 404
    assert client.get("/api/exports/packages/EXP-9999", headers=headers).status_code == 404


def test_detail_and_manifest_endpoints(client):
    headers = _auth(Role.ADMIN)
    code = client.post("/api/exports/packages", headers=headers).json()["package"]["code"]

    detail = client.get(f"/api/exports/packages/{code}", headers=headers).json()["package"]
    assert detail["manifest"][export_service.ISSUE_FILENAME]["data_rows"] == 0

    manifest = client.get(
        f"/api/exports/packages/{code}/files/{export_service.MANIFEST_FILENAME}", headers=headers
    )
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/json")
    assert manifest.json()["package_code"] == code


def test_concurrent_publish_returns_conflict_when_building(client):
    headers = _auth(Role.ADMIN)
    from src.constants.enums import ExportPackageStatus
    from src.models.export_package import ExportPackage

    # 预置一个 building 半包行，守门逻辑应拒绝新的发起（409）
    db = next(client.app.dependency_overrides[get_db]())
    db.add(
        ExportPackage(
            code="EXP-0500", status=ExportPackageStatus.BUILDING.value,
            content_fingerprint=None, created_by="x", created_role="admin",
        )
    )
    db.commit()
    db.close()

    response = client.post("/api/exports/packages", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "EXPORT_BUILDING"
