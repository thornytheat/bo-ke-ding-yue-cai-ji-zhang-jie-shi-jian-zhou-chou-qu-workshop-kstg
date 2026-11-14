"""剪辑清单导出：三张 CSV 的纯函数构造 + 发布包冻结快照与内容指纹。

本模块只负责「同一时刻读库 → 确定性字符串/快照」，不落盘、不建包。
落盘、原子封版、版本复用在 export_package_service 里完成，保证：
同一份冻结数据无论重试几次，三份 CSV 字节逐字一致、指纹一致。
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import OrderedDict

from sqlalchemy import case, func, select

from src.constants.enums import ISSUE_KIND_LABELS, IssueStatus, TaskMode, TaskStatus
from src.constants.limits import EXPORT_BOM
from src.models.chapter import Chapter
from src.models.chapter_issue import ChapterIssue
from src.models.digest_task import DigestTask
from src.models.episode import Episode
from src.models.feed import Feed
from src.utils.timecode import to_short_timecode, to_timecode

CLIP_COLUMNS = [
    "episode_code",
    "feed_code",
    "show_name",
    "title",
    "published_on",
    "duration_s",
    "duration_tc",
    "chapter_count",
    "clip_in_s",
    "clip_out_s",
    "clip_length_s",
    "clip_in_tc",
    "clip_out_tc",
    "chapter_outline",
]

CHAPTER_COLUMNS = [
    "episode_code",
    "ordinal",
    "start_s",
    "end_s",
    "length_s",
    "start_tc",
    "end_tc",
    "title",
    "source",
    "parse_rule",
    "confidence",
    "ambiguous",
]

ISSUE_COLUMNS = [
    "issue_key",
    "episode_code",
    "kind",
    "kind_label",
    "status",
    "window_start_s",
    "window_end_s",
    "message",
    "suggestion",
    "resolved_by",
    "resolution_note",
]

# 发布包内固定文件名（外包拿到的切点可按文件名 + 包 code 追溯）
CLIP_FILENAME = "clip-list.csv"
CHAPTER_FILENAME = "chapter-cuts.csv"
ISSUE_FILENAME = "issues.csv"
MANIFEST_FILENAME = "manifest.json"


def _episode_rows(db) -> list[tuple[Episode, Feed, list[Chapter]]]:
    rows = db.execute(
        select(Episode, Feed).join(Feed, Feed.id == Episode.feed_id).order_by(Episode.code)
    ).all()
    result: list[tuple[Episode, Feed, list[Chapter]]] = []
    for episode, feed in rows:
        chapters = list(
            db.scalars(
                select(Chapter)
                .where(Chapter.episode_id == episode.id, Chapter.start_seconds.is_not(None))
                .order_by(Chapter.start_seconds)
            )
        )
        result.append((episode, feed, chapters))
    return result


def clip_list_csv(db) -> tuple[str, int]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CLIP_COLUMNS)
    count = 0
    for episode, feed, chapters in _episode_rows(db):
        clip_in = chapters[0].start_seconds if chapters else 0
        clip_out = episode.duration_seconds
        outline = " · ".join(chapter.title for chapter in chapters[:4])
        if len(chapters) > 4:
            outline += " …"
        writer.writerow(
            [
                episode.code,
                feed.code,
                feed.show_name,
                episode.title,
                episode.published_on.isoformat() if episode.published_on else "",
                episode.duration_seconds,
                to_timecode(episode.duration_seconds),
                len(chapters),
                clip_in,
                clip_out,
                max(0, clip_out - clip_in),
                to_timecode(clip_in),
                to_timecode(clip_out),
                outline,
            ]
        )
        count += 1
    return EXPORT_BOM + buffer.getvalue(), count


def chapter_cuts_csv(db) -> tuple[str, int]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CHAPTER_COLUMNS)
    count = 0
    for episode, _feed, chapters in _episode_rows(db):
        for index, chapter in enumerate(chapters):
            start = chapter.start_seconds or 0
            end = chapters[index + 1].start_seconds if index + 1 < len(chapters) else episode.duration_seconds
            writer.writerow(
                [
                    episode.code,
                    chapter.ordinal,
                    start,
                    end,
                    max(0, (end or 0) - start),
                    to_short_timecode(start),
                    to_short_timecode(end),
                    chapter.title,
                    chapter.source,
                    chapter.parse_rule,
                    f"{chapter.confidence:.2f}",
                    "yes" if chapter.ambiguous else "no",
                ]
            )
            count += 1
    return EXPORT_BOM + buffer.getvalue(), count


def issues_csv(db, only_open: bool = False) -> tuple[str, int]:
    stmt = select(ChapterIssue, Episode).join(Episode, Episode.id == ChapterIssue.episode_id)
    if only_open:
        stmt = stmt.where(ChapterIssue.status == IssueStatus.OPEN.value)
    rows = db.execute(stmt.order_by(ChapterIssue.kind, ChapterIssue.id)).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ISSUE_COLUMNS)
    for issue, episode in rows:
        writer.writerow(
            [
                issue.issue_key,
                episode.code,
                issue.kind,
                ISSUE_KIND_LABELS.get(issue.kind, issue.kind),
                issue.status,
                issue.window_start if issue.window_start is not None else "",
                issue.window_end if issue.window_end is not None else "",
                issue.message,
                issue.suggestion,
                issue.resolved_by,
                issue.resolution_note,
            ]
        )
    return EXPORT_BOM + buffer.getvalue(), len(rows)


def latest_success_task_code(db) -> str:
    """封版口径锚点：最近一次成功的解析（parse/both）批次；没有则返回空串。"""
    task = db.scalar(
        select(DigestTask)
        .where(
            DigestTask.status == TaskStatus.SUCCESS.value,
            DigestTask.mode.in_([TaskMode.PARSE.value, TaskMode.BOTH.value]),
        )
        .order_by(DigestTask.id.desc())
        .limit(1)
    )
    return task.code if task else ""


def build_frozen_snapshot(db) -> dict:
    """冻结发布时刻的集数 / 章节 / 异常 / 时长汇总，并保留逐集口径用于差异比对。

    三份 CSV 与本快照在同一请求、同一 DB 会话里依次读取，发布包服务在单次
    构建内完成，避免「解析跑到一半、新旧口径混杂」被冻结进同一个包。
    """
    unparsed_flag = case((Chapter.start_seconds.is_(None), 1), else_=0)
    chapter_aggs = {
        row[0]: {"chapter_rows": int(row[1] or 0), "unparsed_rows": int(row[2] or 0)}
        for row in db.execute(
            select(
                Chapter.episode_id,
                func.count(Chapter.id),
                func.coalesce(func.sum(unparsed_flag), 0),
            ).group_by(Chapter.episode_id)
        ).all()
    }

    issue_rows = db.execute(
        select(ChapterIssue.episode_id, ChapterIssue.status, func.count())
        .group_by(ChapterIssue.episode_id, ChapterIssue.status)
    ).all()
    issue_aggs: dict[int, dict[str, int]] = {}
    for episode_id, status, count in issue_rows:
        bucket = issue_aggs.setdefault(episode_id, {"open": 0, "resolved": 0})
        bucket["open" if status == IssueStatus.OPEN.value else "resolved"] += int(count)

    feed_codes = {feed.id: feed.code for feed in db.scalars(select(Feed))}
    episodes = list(db.scalars(select(Episode).order_by(Episode.code)))
    per_episode: "OrderedDict[str, dict]" = OrderedDict()
    total_duration = 0
    for episode in episodes:
        chapters = chapter_aggs.get(episode.id, {"chapter_rows": 0, "unparsed_rows": 0})
        issues = issue_aggs.get(episode.id, {"open": 0, "resolved": 0})
        per_episode[episode.code] = {
            "feed_code": feed_codes.get(episode.feed_id, ""),
            "title": episode.title,
            "duration_s": int(episode.duration_seconds or 0),
            "parse_status": episode.parse_status,
            "chapter_rows": chapters["chapter_rows"],
            "unparsed_rows": chapters["unparsed_rows"],
            "open_issues": issues["open"],
            "resolved_issues": issues["resolved"],
        }
        total_duration += int(episode.duration_seconds or 0)

    issue_total = db.scalar(select(func.count()).select_from(ChapterIssue)) or 0
    issue_open = (
        db.scalar(
            select(func.count())
            .select_from(ChapterIssue)
            .where(ChapterIssue.status == IssueStatus.OPEN.value)
        )
        or 0
    )
    chapter_total = db.scalar(select(func.count()).select_from(Chapter)) or 0
    unparsed_total = (
        db.scalar(select(func.count()).select_from(Chapter).where(Chapter.start_seconds.is_(None)))
        or 0
    )

    return {
        "episodes": len(per_episode),
        "chapters": int(chapter_total),
        "parsed_chapters": int(chapter_total) - int(unparsed_total),
        "unparsed_chapters": int(unparsed_total),
        "issues": int(issue_total),
        "open_issues": int(issue_open),
        "resolved_issues": int(issue_total) - int(issue_open),
        "total_duration_s": int(total_duration),
        "per_episode": per_episode,
    }


def content_fingerprint(snapshot: dict, csv_bundle: "OrderedDict[str, str]", task_code: str) -> str:
    """对冻结口径 + 三份 CSV 内容取哈希。数据不变 → 指纹不变 → 重试命中同一版本。"""
    hasher = hashlib.sha256()
    hasher.update(task_code.encode("utf-8"))
    hasher.update(b"\x1e")
    hasher.update(
        repr(
            [
                snapshot["episodes"],
                snapshot["chapters"],
                snapshot["unparsed_chapters"],
                snapshot["issues"],
                snapshot["open_issues"],
                snapshot["total_duration_s"],
            ]
        ).encode("utf-8")
    )
    for filename, content in csv_bundle.items():
        hasher.update(b"\x1f")
        hasher.update(filename.encode("utf-8"))
        hasher.update(b"\x1d")
        hasher.update(content.encode("utf-8"))
    return hasher.hexdigest()


def diff_snapshots(previous: dict | None, current: dict) -> dict:
    """相邻两个 sealed 包的冻结口径差异（顶部汇总 + 逐集增删改）。"""
    if not previous:
        return {
            "against": None,
            "summary": {
                "episodes": current["episodes"],
                "chapters": current["chapters"],
                "issues": current["issues"],
                "total_duration_s": current["total_duration_s"],
            },
            "episodes_added": list(current["per_episode"].keys()),
            "episodes_removed": [],
            "episodes_changed": [],
        }

    prev_map = previous.get("per_episode", {})
    cur_map = current["per_episode"]
    added = [code for code in cur_map if code not in prev_map]
    removed = [code for code in prev_map if code not in cur_map]
    changed: list[dict] = []
    watch_fields = ("duration_s", "chapter_rows", "unparsed_rows", "open_issues", "resolved_issues", "parse_status")
    for code, cur in cur_map.items():
        prev = prev_map.get(code)
        if not prev:
            continue
        fields = {}
        for field in watch_fields:
            if prev.get(field) != cur.get(field):
                fields[field] = {"from": prev.get(field), "to": cur.get(field)}
        if fields:
            changed.append({"episode_code": code, "title": cur["title"], "fields": fields})

    return {
        "against": previous.get("package_code"),
        "summary": {
            "episodes": current["episodes"] - previous["episodes"],
            "chapters": current["chapters"] - previous["chapters"],
            "issues": current["issues"] - previous["issues"],
            "total_duration_s": current["total_duration_s"] - previous["total_duration_s"],
        },
        "episodes_added": added,
        "episodes_removed": removed,
        "episodes_changed": changed,
    }
