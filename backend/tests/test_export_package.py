"""不可变导出发布包集成测试（SQLite 内存库，不依赖 MySQL/Redis）。

覆盖需求口径：
1. 首发生成 sealed 包，行数三方一致、sha256 落库；
2. 同数据重试命中指纹 → 复用同一版本，不新增包、不产生半包；
3. 数据变化后再发 → 新版本，旧包仍可下载，diff 正确反映增删改；
4. building 半包不可下载；构建失败落 failed、清临时目录且可干净重试；
5. 历史包文件被篡改 → 下载前 sha256 核对拦截；
6. 发布目录不可变（只读位），zip 与单文件均可追溯下载。
"""
from __future__ import annotations

import datetime
import json
import pathlib
import stat

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.settings import settings
from src.constants.enums import (
    ChapterSource,
    ExportPackageStatus,
    IssueKind,
    IssueStatus,
    ParseStatus,
)
from src.models import Base, Chapter, ChapterIssue, Episode, ExportPackage, Feed
from src.services import export_package_service, export_service
from src.utils.errors import AppError
from src.utils.timeutil import utcnow


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "archive"))
    session = Session()
    _seed(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


PRINCIPAL = {"email": "analyst@podcastdig.io", "role": "analyst"}


def _seed(session) -> None:
    feed = Feed(
        code="techweekly", name="科技周报", show_name="科技周报", kind="rss",
        url="u", fixture_path="x.xml", cadence="weekly", host="host", active=True,
        last_status="never", sort_order=0,
    )
    session.add(feed)
    session.flush()
    day = datetime.date(2026, 11, 1)
    for idx, (code, duration) in enumerate((("EP-001", 1800), ("EP-002", 3600)), start=1):
        episode = Episode(
            code=code, feed_id=feed.id, guid=f"guid-{code}", title=f"单集{code}",
            duration_seconds=duration, published_on=day, summary="s", show_notes="notes",
            ingest_state="synced", parse_status=ParseStatus.PARSED.value, content_hash=f"h{idx}",
        )
        session.add(episode)
        session.flush()
        for ordinal, seconds in enumerate((0, 120, 600)):
            session.add(
                Chapter(
                    episode_id=episode.id, ordinal=ordinal, start_seconds=seconds,
                    title=f"第{ordinal}章", raw_text=f"{seconds}", parse_rule="mm_ss",
                    confidence=0.86, ambiguous=False, source=ChapterSource.PARSE.value,
                )
            )
        # 一条未识别章节（start_seconds=None），不计入切点 CSV
        session.add(
            Chapter(
                episode_id=episode.id, ordinal=99, start_seconds=None, title="认不出",
                raw_text="1h02m", parse_rule="unrecognized", confidence=0.0,
                ambiguous=False, source=ChapterSource.UNPARSED.value,
            )
        )
        session.flush()
        session.add(
            ChapterIssue(
                issue_key=f"{code}:gap:1-2", episode_id=episode.id, chapter_id=None,
                kind=IssueKind.GAP_WARNING.value, status=IssueStatus.OPEN.value,
                severity="warn", message="缺口", suggestion="补章",
                detected_by="validator",
            )
        )
    session.commit()


def _archive_root() -> pathlib.Path:
    return pathlib.Path(settings.export_dir)


def test_first_publish_seals_immutable_package_with_three_way_row_check(db_session):
    package, reused = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused is False
    assert package.status == ExportPackageStatus.SEALED.value
    assert package.episode_rows == 2
    # 每集 3 个可识别章节；未识别行不进切点 CSV
    assert package.chapter_rows == 6
    assert package.issue_rows == 2

    # manifest 记录的三份文件行数与落库一致
    for filename, expected in (
        (export_service.CLIP_FILENAME, 2),
        (export_service.CHAPTER_FILENAME, 6),
        (export_service.ISSUE_FILENAME, 2),
    ):
        assert package.manifest[filename]["data_rows"] == expected
        assert len(package.manifest[filename]["sha256"]) == 64

    root = _archive_root()
    package_dir = root / "packages" / package.code
    # 半包目录已清理，发布目录存在且文件只读
    assert not (root / "staging" / package.code).exists()
    assert package_dir.is_dir()
    for path in package_dir.iterdir():
        assert not (path.stat().st_mode & stat.S_IWUSR)
    zip_path = root / "packages" / f"{package.code}.zip"
    assert zip_path.exists()
    assert package.zip_sha256 == export_package_service._sha256_file(zip_path)


def test_retry_with_same_data_reuses_version_and_leaves_no_half_package(db_session):
    first, reused_first = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused_first is False
    second, reused_second = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused_second is True
    assert second.id == first.id

    packages = list(db_session.scalars(select(ExportPackage)))
    assert len(packages) == 1
    # 没有任何 building/failed 残留
    assert packages[0].status == ExportPackageStatus.SEALED.value
    # staging 下不留半包（zip 临时件也被原子移走）
    staging = list((_archive_root() / "staging").glob("**/*"))
    assert all(path.is_dir() and not any(path.iterdir()) for path in staging) if staging else True


def test_changed_data_creates_new_version_while_old_stays_downloadable(db_session):
    first, _ = export_package_service.create_package(db_session, PRINCIPAL)

    # 平台改写：新增一集，产生新口径
    feed = db_session.scalar(select(Feed))
    db_session.add(
        Episode(
            code="EP-003", feed_id=feed.id, guid="guid-EP-003", title="新单集",
            duration_seconds=900, published_on=datetime.date(2026, 11, 2),
            summary="s", show_notes="n", ingest_state="synced",
            parse_status=ParseStatus.PARSED.value, content_hash="h3",
        )
    )
    db_session.commit()

    second, reused = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused is False
    assert second.code != first.code
    assert second.episode_rows == 3
    assert second.diff["summary"]["episodes"] == 1
    assert second.diff["episodes_added"] == ["EP-003"]

    # 历史包仍按旧口径可下载
    old_data, _, old_name = export_package_service.read_artifact(
        db_session, first.code, export_service.CLIP_FILENAME
    )
    assert old_name.startswith(first.code)
    assert old_data.count(b"\n") == 2 + 1  # 2 数据行 + 表头
    new_data, _, _ = export_package_service.read_artifact(
        db_session, second.code, export_service.CLIP_FILENAME
    )
    assert new_data.count(b"\n") == 3 + 1


def test_building_package_is_not_downloadable(db_session, monkeypatch):
    # 构造一个 building 半包行，下载必须被拒
    package = ExportPackage(
        code="EXP-9001", status=ExportPackageStatus.BUILDING.value,
        content_fingerprint=None, created_by="x", created_role="analyst",
    )
    db_session.add(package)
    db_session.commit()
    with pytest.raises(AppError) as exc:
        export_package_service.read_artifact(
            db_session, "EXP-9001", export_service.CLIP_FILENAME
        )
    assert exc.value.code == "EXPORT_NOT_SEALED"


def test_failed_build_marks_failed_clears_half_package_and_allows_retry(db_session, monkeypatch):
    # 第一次封版强制失败，第二次恢复正常：验证 failed 落库、半包清理、可干净重试
    state = {"fail": True}

    def guarded(*args, **kwargs):
        if state["fail"]:
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "post-rename-zip"})
        return real_materialize(*args, **kwargs)

    real_materialize = export_package_service._materialize_and_seal
    monkeypatch.setattr(export_package_service, "_materialize_and_seal", guarded)

    with pytest.raises(AppError) as exc:
        export_package_service.create_package(db_session, PRINCIPAL)
    assert exc.value.code == "EXPORT_CHECKSUM_MISMATCH"

    failed = db_session.scalar(select(ExportPackage))
    assert failed.status == ExportPackageStatus.FAILED.value
    assert failed.content_fingerprint is None  # 放行干净重试
    # 半包不进发布目录
    root = _archive_root()
    assert not (root / "packages" / failed.code).exists()

    state["fail"] = False
    retry, reused = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused is False
    assert retry.status == ExportPackageStatus.SEALED.value
    # failed 行保留可追溯，外加一个新 sealed
    statuses = sorted(row.status for row in db_session.scalars(select(ExportPackage)))
    assert statuses == [ExportPackageStatus.FAILED.value, ExportPackageStatus.SEALED.value]


def test_tampered_historical_file_is_blocked_by_checksum(db_session):
    package, _ = export_package_service.create_package(db_session, PRINCIPAL)
    target = _archive_root() / "packages" / package.code / export_service.CLIP_FILENAME
    target.chmod(stat.S_IWUSR | stat.S_IRUSR)
    target.write_text(target.read_text(encoding="utf-8") + "\nTAMPERED,row\n", encoding="utf-8")
    with pytest.raises(AppError) as exc:
        export_package_service.read_artifact(db_session, package.code, export_service.CLIP_FILENAME)
    assert exc.value.code == "EXPORT_CHECKSUM_MISMATCH"


def test_zip_download_verifies_and_manifest_is_readable(db_session):
    import zipfile

    package, _ = export_package_service.create_package(db_session, PRINCIPAL)
    data, filename = export_package_service.read_zip(db_session, package.code)
    assert filename == f"{package.code}.zip"
    assert len(data) == package.zip_bytes

    manifest_bytes, media, _ = export_package_service.read_artifact(
        db_session, package.code, export_service.MANIFEST_FILENAME
    )
    assert media.startswith("application/json")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    assert manifest["package_code"] == package.code
    assert manifest["frozen_summary"]["episodes"] == 2
    assert manifest["sealed_at"] is not None

    # ZIP 内 manifest 与散文件逐字节一致，且同样带封版时间
    root = _archive_root()
    with zipfile.ZipFile(root / "packages" / f"{package.code}.zip") as archive:
        zipped_manifest = archive.read(export_service.MANIFEST_FILENAME)
        assert zipped_manifest == manifest_bytes
        assert json.loads(zipped_manifest.decode("utf-8"))["sealed_at"] is not None
        assert archive.namelist() == [
            export_service.CLIP_FILENAME,
            export_service.CHAPTER_FILENAME,
            export_service.ISSUE_FILENAME,
            export_service.MANIFEST_FILENAME,
        ]


def test_path_traversal_filename_rejected(db_session):
    package, _ = export_package_service.create_package(db_session, PRINCIPAL)
    with pytest.raises(AppError) as exc:
        export_package_service.read_artifact(db_session, package.code, "../../etc/passwd")
    assert exc.value.status_code == 404


def test_list_marks_current_package(db_session):
    first, _ = export_package_service.create_package(db_session, PRINCIPAL)
    feed = db_session.scalar(select(Feed))
    db_session.add(
        Episode(
            code="EP-009", feed_id=feed.id, guid="guid-EP-009", title="又一集",
            duration_seconds=600, published_on=datetime.date(2026, 11, 3),
            summary="s", show_notes="n", ingest_state="synced",
            parse_status=ParseStatus.PARSED.value, content_hash="h9",
        )
    )
    db_session.commit()
    second, _ = export_package_service.create_package(db_session, PRINCIPAL)

    items = export_package_service.list_packages(db_session)
    by_code = {item["code"]: item for item in items}
    assert by_code[second.code]["is_current"] is True
    assert by_code[first.code]["is_current"] is False


def test_stale_building_is_reclaimed_so_retry_is_not_blocked(db_session):
    from src.utils.timeutil import utcnow

    stale = ExportPackage(
        code="EXP-7001",
        status=ExportPackageStatus.BUILDING.value,
        content_fingerprint="stale-fp",
        created_by="x",
        created_role="analyst",
    )
    stale.created_at = utcnow() - datetime.timedelta(
        minutes=export_package_service.STALE_BUILDING_MINUTES + 5
    )
    db_session.add(stale)
    db_session.commit()

    package, reused = export_package_service.create_package(db_session, PRINCIPAL)
    assert reused is False
    assert package.status == ExportPackageStatus.SEALED.value

    db_session.expire_all()
    reclaimed = db_session.scalar(
        select(ExportPackage).where(ExportPackage.code == "EXP-7001")
    )
    assert reclaimed.status == ExportPackageStatus.FAILED.value
    assert reclaimed.content_fingerprint is None
    assert "超时未封版" in reclaimed.error_text


def test_fingerprint_is_deterministic_for_identical_bundle(db_session):
    bundle, row_counts, snapshot, task_code = export_package_service._build_bundle(db_session)
    fp1 = export_service.content_fingerprint(snapshot, bundle, task_code)
    bundle2, row_counts2, snapshot2, task_code2 = export_package_service._build_bundle(db_session)
    fp2 = export_service.content_fingerprint(snapshot2, bundle2, task_code2)
    assert fp1 == fp2
    assert row_counts == row_counts2
    # 冻结汇总里含时长汇总
    assert snapshot["total_duration_s"] == 1800 + 3600
    assert snapshot["parsed_chapters"] == 6
    assert snapshot["unparsed_chapters"] == 2
    assert utcnow() is not None  # 保留时间工具引用
