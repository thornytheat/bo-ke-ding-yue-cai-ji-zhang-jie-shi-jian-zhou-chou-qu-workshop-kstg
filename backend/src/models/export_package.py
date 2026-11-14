"""不可变导出发布包（ExportPackage）。

一次发布 = 同一解析批次下冻结的剪辑清单 / 章节切点 / 异常清单三张表。
构建流程：building（半包，目录不可见、不可下载）→ 写临时目录 → 逐文件核对
（行数、字节、sha256）→ 一致才置 sealed 并原子改名到发布目录。

- content_fingerprint 对「冻结集数/章节/异常/时长汇总 + 三份 CSV 内容」取哈希：
  数据没变就命中既有 sealed 包，重试不重复生成版本。
- 失败行 content_fingerprint 置 NULL（唯一索引只约束非空值），允许干净重试；
  临时目录在失败时整体删除，磁盘上不留半包。
- sealed 包永不覆盖、永不删除，历史包持续可下载。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ExportPackage(Base):
    __tablename__ = "export_packages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(12), index=True, default="building")

    # 封版时冻结的解析批次口径（最近一次成功的 parse/both 批次）
    digest_task_code: Mapped[str] = mapped_column(String(24), default="", index=True)

    # 各表行数（与 manifest / CSV 实际行数三方一致才允许封版）
    episode_rows: Mapped[int] = mapped_column(Integer, default=0)
    chapter_rows: Mapped[int] = mapped_column(Integer, default=0)
    issue_rows: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_s: Mapped[int] = mapped_column(Integer, default=0)

    # 冻结快照 + 与上一个 sealed 包的差异 + 封版清单（文件名/字节/sha256）
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)

    # 整包校验值与落盘位置（sealed 后不可变）
    zip_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    zip_path: Mapped[str] = mapped_column(String(255), default="")
    zip_bytes: Mapped[int] = mapped_column(Integer, default=0)

    # 同数据重试命中同指纹 → 复用既有包，不产生新版本；失败行置 NULL 允许重试
    content_fingerprint: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    error_text: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(160), default="")
    created_role: Mapped[str] = mapped_column(String(20), default="")
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
