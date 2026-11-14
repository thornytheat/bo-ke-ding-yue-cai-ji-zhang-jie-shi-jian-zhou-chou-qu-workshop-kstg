"""不可变发布包编排：构建 → 三方核对 → 原子封版；历史包只读可下载。

不变量（对应需求）：
1. 每次发布冻结同一解析批次下的集数 / 章节 / 异常 / 时长汇总，封版后不再随库变化；
2. 行数三方一致才封版：DB 聚合行数 == CSV 数据行数 == manifest 记录，且每个文件
   sha256 复算一致、zip 复算一致；任一不符则整包废弃（failed），磁盘临时目录清除；
3. content_fingerprint 命中既有 sealed 包 → 直接复用，不生成新版本（重试幂等）；
4. building 半包不出现在发布目录、不开放下载；sealed 包永不覆盖、永不删除；
5. 历史 sealed 包持续可下载，下载前再做一次 sha256 核对，损坏直接报错而非给错切点。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import zipfile
from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import settings
from src.constants.enums import EXPORT_PACKAGE_STATUS_LABELS, ExportPackageStatus
from src.models.export_package import ExportPackage
from src.services import export_service
from src.utils.errors import AppError
from src.utils.logger import get_logger
from src.utils.timeutil import local_iso, utcnow

logger = get_logger("export_package")

# 行数核对时 CSV 表头占一行
HEADER_OFFSET = 1
STALE_BUILDING_MINUTES = 30


def _archive_root() -> pathlib.Path:
    root = pathlib.Path(settings.export_dir)
    (root / "packages").mkdir(parents=True, exist_ok=True)
    (root / "staging").mkdir(parents=True, exist_ok=True)
    return root


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _csv_data_row_count(content: str) -> int:
    # BOM 不产生额外行；splitlines 对末尾换行不生成空串
    lines = content.splitlines()
    return max(0, len(lines) - HEADER_OFFSET)


def next_package_code(db) -> str:
    row = db.scalar(select(ExportPackage).order_by(ExportPackage.id.desc()).limit(1))
    if row is None:
        return "EXP-0001"
    try:
        current = int(row.code.split("-")[1])
    except (IndexError, ValueError):  # pragma: no cover - 编码恒为 EXP-NNNN
        current = 0
    return f"EXP-{current + 1:04d}"


def _reclaim_stale_buildings(db) -> None:
    """进程崩溃残留的 building 行：超过阈值仍未封版则判失败并清临时目录，解除重试阻塞。"""
    cutoff = utcnow().timestamp() - STALE_BUILDING_MINUTES * 60
    stale = list(
        db.scalars(select(ExportPackage).where(ExportPackage.status == ExportPackageStatus.BUILDING.value))
    )
    for package in stale:
        created = package.created_at
        if created is not None and created.timestamp() <= cutoff:
            package.status = ExportPackageStatus.FAILED.value
            package.error_text = "构建中断（超时未封版），已自动废弃，可重新发起"
            package.content_fingerprint = None
            logger.warning("回收僵死构建包 %s", package.code)
    db.flush()


def _find_sealed_by_fingerprint(db, fingerprint: str) -> ExportPackage | None:
    return db.scalar(
        select(ExportPackage).where(
            ExportPackage.content_fingerprint == fingerprint,
            ExportPackage.status == ExportPackageStatus.SEALED.value,
        )
    )


def _build_bundle(db) -> tuple[OrderedDict, OrderedDict, dict, str]:
    """同一事务内依次读三张表 + 快照，返回文件名→内容、文件名→行数、快照、批次锚点。"""
    clip_content, clip_rows = export_service.clip_list_csv(db)
    chapter_content, chapter_rows = export_service.chapter_cuts_csv(db)
    issue_content, issue_rows = export_service.issues_csv(db)
    snapshot = export_service.build_frozen_snapshot(db)
    task_code = export_service.latest_success_task_code(db)

    bundle: OrderedDict[str, str] = OrderedDict(
        [
            (export_service.CLIP_FILENAME, clip_content),
            (export_service.CHAPTER_FILENAME, chapter_content),
            (export_service.ISSUE_FILENAME, issue_content),
        ]
    )
    row_counts: OrderedDict[str, int] = OrderedDict(
        [
            (export_service.CLIP_FILENAME, clip_rows),
            (export_service.CHAPTER_FILENAME, chapter_rows),
            (export_service.ISSUE_FILENAME, issue_rows),
        ]
    )
    return bundle, row_counts, snapshot, task_code


def create_package(db, principal: dict) -> tuple[ExportPackage, bool]:
    """发起一次不可变发布：命中同指纹则复用（reused=True），否则构建并原子封版。"""
    _reclaim_stale_buildings(db)

    building = db.scalar(
        select(ExportPackage).where(ExportPackage.status == ExportPackageStatus.BUILDING.value).limit(1)
    )
    if building is not None:
        raise AppError("EXPORT_BUILDING", 409, detail={"package": building.code})

    bundle, row_counts, snapshot, task_code = _build_bundle(db)
    if snapshot["episodes"] == 0:
        raise AppError("EXPORT_EMPTY", 422, detail="没有可导出的单集")

    fingerprint = export_service.content_fingerprint(snapshot, bundle, task_code)
    existing = _find_sealed_by_fingerprint(db, fingerprint)
    if existing is not None:
        logger.info("内容指纹命中既有封版包 %s，跳过重发", existing.code)
        return existing, True

    package = ExportPackage(
        code=next_package_code(db),
        status=ExportPackageStatus.BUILDING.value,
        digest_task_code=task_code,
        episode_rows=snapshot["episodes"],
        chapter_rows=snapshot["parsed_chapters"],
        issue_rows=snapshot["issues"],
        total_duration_s=snapshot["total_duration_s"],
        snapshot=snapshot,
        content_fingerprint=fingerprint,
        created_by=principal.get("email", ""),
        created_role=principal.get("role", ""),
    )
    db.add(package)
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        # 并发下唯一指纹被另一请求抢先占用 → 已封版则复用，仍在构建则提示等待
        db.rollback()
        winner = _find_sealed_by_fingerprint(db, fingerprint)
        if winner is not None:
            return winner, True
        building_winner = db.scalar(
            select(ExportPackage)
            .where(
                ExportPackage.content_fingerprint == fingerprint,
                ExportPackage.status == ExportPackageStatus.BUILDING.value,
            )
            .limit(1)
        )
        if building_winner is not None:
            raise AppError("EXPORT_BUILDING", 409, detail={"package": building_winner.code})
        raise

    try:
        _materialize_and_seal(db, package, bundle, row_counts, snapshot, task_code)
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落 failed + 清半包
        db.rollback()
        # 清掉可能已落盘的半包（临时目录与临时 zip），发布目录不允许出现失败包
        root = _archive_root()
        stale_dir = root / "staging" / package.code
        if stale_dir.exists():
            shutil.rmtree(stale_dir, ignore_errors=True)
        stale_zip = root / "staging" / f"{package.code}.zip"
        if stale_zip.exists():
            stale_zip.unlink()
        package = db.get(ExportPackage, package.id)
        if package is not None:
            package.status = ExportPackageStatus.FAILED.value
            package.error_text = str(exc)[:500]
            package.content_fingerprint = None  # 放行干净重试
            db.commit()
        raise
    db.refresh(package)
    return package, False


def _materialize_and_seal(
    db,
    package: ExportPackage,
    bundle: OrderedDict,
    row_counts: OrderedDict,
    snapshot: dict,
    task_code: str,
) -> None:
    root = _archive_root()
    staging_dir = root / "staging" / package.code
    final_dir = root / "packages" / package.code
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    # —— 第一关：行数三方一致（构造器计数 / CSV 实际行 / 冻结快照）——
    actual_rows = {name: _csv_data_row_count(content) for name, content in bundle.items()}
    if any(actual_rows[name] != row_counts[name] for name in bundle):
        raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "row-count", "actual": actual_rows})
    expected_rows = {
        export_service.CLIP_FILENAME: snapshot["episodes"],
        export_service.CHAPTER_FILENAME: snapshot["parsed_chapters"],
        export_service.ISSUE_FILENAME: snapshot["issues"],
    }
    if any(actual_rows[name] != expected for name, expected in expected_rows.items()):
        raise AppError(
            "EXPORT_CHECKSUM_MISMATCH",
            500,
            detail={"stage": "snapshot-row-count", "csv": actual_rows, "snapshot": expected_rows},
        )

    # —— 写临时目录（半包，发布目录此刻看不到）——
    files_manifest: OrderedDict[str, dict] = OrderedDict()
    for filename, content in bundle.items():
        target = staging_dir / filename
        target.write_text(content, encoding="utf-8")
        raw = content.encode("utf-8")
        files_manifest[filename] = {
            "bytes": len(raw),
            "sha256": _sha256_bytes(raw),
            "data_rows": actual_rows[filename],
        }

    manifest = {
        "package_code": package.code,
        "sealed_at": None,  # 封版成功后补写
        "digest_task_code": task_code,
        "frozen_summary": {
            "episodes": snapshot["episodes"],
            "chapters": snapshot["chapters"],
            "parsed_chapters": snapshot["parsed_chapters"],
            "unparsed_chapters": snapshot["unparsed_chapters"],
            "issues": snapshot["issues"],
            "open_issues": snapshot["open_issues"],
            "resolved_issues": snapshot["resolved_issues"],
            "total_duration_s": snapshot["total_duration_s"],
        },
        "files": files_manifest,
    }
    (staging_dir / export_service.MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # —— 第二关：逐文件落盘后复算 sha256/字节，防止写坏 ——
    for filename, meta in files_manifest.items():
        path = staging_dir / filename
        if not path.exists():
            raise AppError("EXPORT_ARTIFACT_MISSING", 500, detail={"file": filename})
        on_disk = path.read_bytes()
        if len(on_disk) != meta["bytes"] or _sha256_bytes(on_disk) != meta["sha256"]:
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "file-rewrite", "file": filename})

    # 先定封版时间并落最终 manifest，保证 ZIP 内 manifest 与散文件完全一致
    sealed_at = utcnow()
    manifest["sealed_at"] = sealed_at.strftime("%Y-%m-%d %H:%M:%S")
    (staging_dir / export_service.MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # —— 打 zip（确定性：固定元信息 + 固定条目顺序）——
    staged_zip = root / "staging" / f"{package.code}.zip"
    if staged_zip.exists():
        staged_zip.unlink()
    ordered_names = [*bundle.keys(), export_service.MANIFEST_FILENAME]
    with zipfile.ZipFile(staged_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name in ordered_names:
            path = staging_dir / name
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    zip_bytes = staged_zip.stat().st_size
    zip_sha = _sha256_file(staged_zip)

    # 测试 zip 完整性并核对条目
    with zipfile.ZipFile(staged_zip) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "zip-crc", "file": bad})
        if sorted(archive.namelist()) != sorted(ordered_names):
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "zip-entries"})
        # ZIP 内 manifest 必须与散文件同字节
        if archive.read(export_service.MANIFEST_FILENAME) != (
            staging_dir / export_service.MANIFEST_FILENAME
        ).read_bytes():
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "zip-manifest"})

    # —— 第三关：原子进入发布目录（rename 同卷原子），发布后只读 ——
    if final_dir.exists():  # 理论上不可能，防御性保护历史包
        raise AppError("CONFLICT", 409, detail=f"发布包目录已存在：{package.code}")
    staging_dir.rename(final_dir)
    final_zip = root / "packages" / f"{package.code}.zip"
    staged_zip.rename(final_zip)
    for path in final_dir.iterdir():
        path.chmod(0o444)
    final_zip.chmod(0o444)

    # —— 第四关：发布位再复算一次 zip sha256，与落库值比对 ——
    if _sha256_file(final_zip) != zip_sha:
        raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"stage": "post-rename-zip"})

    previous_sealed = db.scalar(
        select(ExportPackage)
        .where(
            ExportPackage.status == ExportPackageStatus.SEALED.value,
            ExportPackage.id != package.id,
        )
        .order_by(ExportPackage.id.desc())
        .limit(1)
    )
    previous_snapshot = None
    if previous_sealed is not None:
        previous_snapshot = dict(previous_sealed.snapshot or {})
        previous_snapshot["package_code"] = previous_sealed.code
    diff = export_service.diff_snapshots(previous_snapshot, snapshot)

    package.status = ExportPackageStatus.SEALED.value
    package.sealed_at = sealed_at
    package.zip_path = str(final_zip.relative_to(root))
    package.zip_bytes = zip_bytes
    package.zip_sha256 = zip_sha
    package.manifest = files_manifest
    package.diff = diff
    db.commit()
    logger.info("发布包 %s 封版完成（%s 集 / %s 章 / %s 异常）", package.code,
                package.episode_rows, package.chapter_rows, package.issue_rows)


def list_packages(db, limit: int = 50) -> list[dict]:
    rows = list(
        db.scalars(select(ExportPackage).order_by(ExportPackage.id.desc()).limit(limit))
    )
    current = next((row for row in rows if row.status == ExportPackageStatus.SEALED.value), None)
    result = []
    for row in rows:
        item = package_to_dict(row)
        item["is_current"] = bool(current and row.id == current.id)
        result.append(item)
    return result


def get_package(db, code: str) -> ExportPackage:
    package = db.scalar(select(ExportPackage).where(ExportPackage.code == code))
    if package is None:
        raise AppError("NOT_FOUND", 404, detail=f"export_package={code}")
    return package


def package_to_dict(package: ExportPackage) -> dict:
    return {
        "id": package.id,
        "code": package.code,
        "status": package.status,
        "status_label": EXPORT_PACKAGE_STATUS_LABELS.get(package.status, package.status),
        "digest_task_code": package.digest_task_code,
        "episode_rows": package.episode_rows,
        "chapter_rows": package.chapter_rows,
        "issue_rows": package.issue_rows,
        "total_duration_s": package.total_duration_s,
        "zip_bytes": package.zip_bytes,
        "zip_sha256": package.zip_sha256,
        "manifest": package.manifest or {},
        "diff": package.diff or {},
        "error_text": package.error_text,
        "created_by": package.created_by,
        "sealed_at": local_iso(package.sealed_at),
        "created_at": local_iso(package.created_at),
    }


def _resolve_artifact_path(package: ExportPackage, root: pathlib.Path, filename: str) -> pathlib.Path:
    # 文件名只允许包内固定三份 CSV，防路径穿越
    allowed = {
        export_service.CLIP_FILENAME,
        export_service.CHAPTER_FILENAME,
        export_service.ISSUE_FILENAME,
        export_service.MANIFEST_FILENAME,
    }
    if filename not in allowed:
        raise AppError("NOT_FOUND", 404, detail={"file": filename})
    path = (root / "packages" / package.code / filename).resolve()
    package_dir = (root / "packages" / package.code).resolve()
    if package_dir not in path.parents or not path.exists():
        raise AppError("EXPORT_ARTIFACT_MISSING", 404, detail={"file": filename})
    return path


def read_artifact(db, code: str, filename: str) -> tuple[bytes, str, str]:
    """下载历史/当前包内单个文件：仅 sealed 开放，且下载前复算 sha256。"""
    package = get_package(db, code)
    if package.status != ExportPackageStatus.SEALED.value:
        raise AppError("EXPORT_NOT_SEALED", 409, detail={"package": code, "status": package.status})
    root = _archive_root()
    path = _resolve_artifact_path(package, root, filename)
    data = path.read_bytes()

    if filename != export_service.MANIFEST_FILENAME:
        meta = (package.manifest or {}).get(filename)
        if not meta:
            raise AppError("EXPORT_ARTIFACT_MISSING", 404, detail={"file": filename})
        if _sha256_bytes(data) != meta.get("sha256") or len(data) != meta.get("bytes"):
            raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"file": filename, "package": code})

    media = "application/json; charset=utf-8" if filename == export_service.MANIFEST_FILENAME else "text/csv; charset=utf-8"
    download_name = f"{package.code}-{filename}" if filename != export_service.MANIFEST_FILENAME else f"{package.code}-manifest.json"
    return data, media, download_name


def read_zip(db, code: str) -> tuple[bytes, str]:
    """下载整包 zip：仅 sealed 开放，下载前复算整包 sha256。"""
    package = get_package(db, code)
    if package.status != ExportPackageStatus.SEALED.value:
        raise AppError("EXPORT_NOT_SEALED", 409, detail={"package": code, "status": package.status})
    root = _archive_root()
    path = (root / "packages" / f"{package.code}.zip").resolve()
    if not path.exists():
        raise AppError("EXPORT_ARTIFACT_MISSING", 404, detail={"package": code})
    if _sha256_file(path) != package.zip_sha256 or path.stat().st_size != package.zip_bytes:
        raise AppError("EXPORT_CHECKSUM_MISMATCH", 500, detail={"package": code})
    return path.read_bytes(), f"{package.code}.zip"
