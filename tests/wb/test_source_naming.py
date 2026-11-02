"""源文件命名与资产身份回归测试。

对应两个真实 bug：

1. 源文件一律叫 ``source.mp4``，用户看不出哪个文件是哪一集。
2. 一旦把它改成按视频标题命名，``wb_asset`` 的去重键（含 path）就会在重下时插出第二行
   ``kind="source"``；读取侧按 ``created_at`` 升序、``setdefault`` 取**最早**那行，
   于是旧路径遮蔽新文件——转录/剪辑/导出拿到一个不存在的文件，而且不报错。

所以这里的断言重心不在"名字对不对"，而在**改名之后身份还站不站得住**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.core.downloader import DownloadResult
from video_workbench.core.layout import (
    SIDECAR,
    allocate,
    build_sidecar,
    finalize_source,
    find_source,
    read_sidecar,
    source_stem,
    write_sidecar,
)
from video_workbench.core.resolver import normalize_cover
from video_workbench.workers import download_worker as W


MIN_BYTES = W.MIN_REUSE_BYTES + 1


class FakeCtx:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def progress(self, value=None, stage=None, speed=None, eta=None):
        pass

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return False

    def check_cancel(self):
        pass


def make_downloader(*, title_suffix: str = ".mp4"):
    """替身下载器：永远往 home 里写一个 ``source.<ext>``（真实后端的暂存名）。"""

    def run(**kwargs):
        home = Path(kwargs["home"])
        target = home / f"source{title_suffix}"
        target.write_bytes(b"x" * MIN_BYTES)
        kwargs["on_event"]({"type": "progress", "percent": 100.0})
        return DownloadResult(path=target, height=1080, format_id="137+140", quality_requested="1080P")

    return run


def payload(bvid="BV1xx411c7mD", title="第一讲 · 引论", cid=1001, **extra):
    body = {
        "item": {"bvid": bvid, "cid": cid, "page": 1, "title": title, "part_title": "", "aid": 42},
        "up_name": "示例UP",
        "collection_title": "方法论合集",
        "quality": "1080P",
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------- 命名规则


@pytest.mark.parametrize(
    ("title", "part", "expected"),
    [
        ("普通标题", "", "普通标题"),
        # 非法字符是**删掉**再压空白，不是逐个换成下划线：`<>:"/\|?*` 全不在合法文件名里
        ('含非法字符 <>:"/\\|?* 的标题', "", "含非法字符 的标题"),
        ("  首尾空白  ", "", "首尾空白"),
        ("首尾点...", "", "首尾点"),
        ("CON", "", "_CON"),           # Win32 保留设备名，不能直接当文件名
        ("", "", "untitled"),
        ("", "只有分P名", "只有分P名"),   # 没主标题时别留一个孤零零的 " - "
        ("主标题", "第二讲", "主标题 - 第二讲"),   # 与列表里的 display_title 同规则
        ("同一个标题", "同一个标题", "同一个标题"),  # 分P名与主标题相同不重复拼
    ],
)
def test_source_stem_rules(title, part, expected):
    assert source_stem(title, part) == expected


def test_source_stem_is_truncated_not_empty():
    long = "长" * 400
    assert len(source_stem(long)) <= 120
    assert source_stem(long).startswith("长")


def test_source_stem_collision_is_possible_and_handled_by_sidecar():
    """清洗会把不同标题压成同一个主干——这是设计取舍，不靠文件名区分同一集。

    真正判"是不是同一集"的是侧车里的 bvid+cid（``allocate`` 的复用判定），
    文件名只负责人看。这里把碰撞钉成测试，是为了让后面想"用文件名当唯一键"的人
    先看到它不成立。
    """
    assert source_stem("A/B") == source_stem("A:B") == "AB"


# ---------------------------------------------------------------- 改名


def test_finalize_source_renames_in_place(tmp_path):
    staged = tmp_path / "source.mp4"
    staged.write_bytes(b"x" * MIN_BYTES)
    out = finalize_source(tmp_path, staged, stem="视频标题")
    assert out == tmp_path / "视频标题.mp4"
    assert out.is_file() and not staged.exists()


def test_finalize_source_overwrites_same_name(tmp_path):
    """同目录同 stem 一定是同一集（allocate 用 bvid+cid 判过），UP 改标题后重下要覆盖。"""
    old = tmp_path / "视频标题.mp4"
    old.write_bytes(b"old")
    staged = tmp_path / "source.mp4"
    staged.write_bytes(b"new")
    out = finalize_source(tmp_path, staged, stem="视频标题")
    assert out == old and old.read_bytes() == b"new"


def test_finalize_source_leaves_foreign_dir_alone(tmp_path):
    home, elsewhere = tmp_path / "a", tmp_path / "b"
    home.mkdir(); elsewhere.mkdir()
    foreign = elsewhere / "source.mp4"
    foreign.write_bytes(b"x")
    assert finalize_source(home, foreign, stem="标题") == foreign
    assert foreign.exists()


def test_finalize_source_survives_failure(tmp_path, monkeypatch):
    """改名失败不判下载失败：DB 记的是真实存在的暂存名路径。"""
    staged = tmp_path / "source.mp4"
    staged.write_bytes(b"x")
    monkeypatch.setattr(Path, "replace", lambda self, target: (_ for _ in ()).throw(OSError("锁住了")))
    assert finalize_source(tmp_path, staged, stem="标题") == staged


# ---------------------------------------------------------------- 读侧兜底


def test_find_source_prefers_authoritative_path(tmp_path):
    real = tmp_path / "视频标题.mp4"
    real.write_bytes(b"x")
    (tmp_path / "source.mp4").write_bytes(b"x")   # 目录里同时有遗留文件时不猜
    assert find_source(tmp_path, prefer=str(real), stem="视频标题") == real


def test_find_source_falls_back_to_legacy_name(tmp_path):
    legacy = tmp_path / "source.mp4"
    legacy.write_bytes(b"x")
    assert find_source(tmp_path, prefer="", stem="对不上的标题") == legacy


def test_find_source_accepts_a_single_unnamed_file(tmp_path):
    only = tmp_path / "用户自己改的名.mkv"
    only.write_bytes(b"x")
    assert find_source(tmp_path, stem="对不上") == only


def test_find_source_refuses_to_guess_between_two(tmp_path):
    (tmp_path / "A.mp4").write_bytes(b"x")
    (tmp_path / "B.mp4").write_bytes(b"x")
    assert find_source(tmp_path, stem="C") is None


def test_find_source_ignores_non_media(tmp_path):
    (tmp_path / "audio.wav").write_bytes(b"x")
    (tmp_path / SIDECAR).write_text("{}", encoding="utf-8")
    assert find_source(tmp_path) is None


# ---------------------------------------------------------------- 资产身份


def test_singleton_upsert_rewrites_instead_of_inserting(service):
    repo = service.repo
    project = repo.project_create(title="样例", root_dir="/tmp/x")
    first = repo.asset_upsert_singleton(project["id"], "source", "/tmp/x/source.mp4", bytes=10)
    repo.transcript_upsert(project["id"], segments=[], language="zh", asset_id=first["id"])

    second = repo.asset_upsert_singleton(project["id"], "source", "/tmp/x/新标题.mp4", bytes=20)

    rows = [a for a in repo.asset_list(project["id"]) if a["kind"] == "source"]
    assert len(rows) == 1, f"改名后不该留下第二行原始视频：{[r['path'] for r in rows]}"
    assert rows[0]["path"] == "/tmp/x/新标题.mp4"
    assert rows[0]["bytes"] == 20
    assert second["id"] == first["id"], "同一集应复用同一行，不换 id"
    # 转录的来源不能被抹掉：这正是"改写不删行"的理由（wb_transcript.asset_id 是 SET NULL 外键）
    assert repo.transcript_get(project["id"])["asset_id"] == first["id"]


def test_singleton_upsert_keeps_other_kinds_untouched(service):
    repo = service.repo
    project = repo.project_create(title="样例", root_dir="/tmp/x")
    repo.asset_upsert(project["id"], "subtitle", "/tmp/x/a.srt", language="zh")
    repo.asset_upsert(project["id"], "subtitle", "/tmp/x/b.srt", language="en")
    repo.asset_upsert_singleton(project["id"], "source", "/tmp/x/v.mp4")
    kinds = [a["kind"] for a in repo.asset_list(project["id"])]
    assert kinds.count("subtitle") == 2, "多值 kind 不该被单例逻辑合并"
    assert kinds.count("source") == 1


# ---------------------------------------------------------------- 封面地址


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 实测：view 接口给的 pic 就是明文 http，而页面 CSP 的 img-src 不含 http:
        ("http://i2.hdslb.com/bfs/archive/ac31f3.jpg", "https://i2.hdslb.com/bfs/archive/ac31f3.jpg"),
        ("http://i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"),
        ("//i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"),
        ("https://i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"),
        ("http://t12.biliimg.com/x.jpg", "https://t12.biliimg.com/x.jpg"),
        ("http://user:pass@i0.hdslb.com:8080/x.jpg", "https://user:pass@i0.hdslb.com:8080/x.jpg"),
        ("", ""),
        (None, ""),
        ("not a url", ""),
        # 非白名单的明文地址：CSP 注定拦，留着只是界面上一个破图 → 丢掉走占位
        ("http://evil.example.com/x.jpg", ""),
        # 形似白名单实为蹭名的域，不能升 https
        ("http://hdslb.com.evil.net/x.jpg", ""),
    ],
)
def test_normalize_cover(raw, expected):
    assert normalize_cover(raw) == expected


# ---------------------------------------------------------------- 端到端


def test_download_names_file_after_title_and_records_one_asset(service, tmp_path):
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    result = handler(FakeCtx(), payload(title="【简单意识形态学】论FX战士"))

    path = Path(result["path"])
    assert path.name == "【简单意识形态学】论FX战士.mp4", "源文件要能一眼看出是哪一集"
    assert path.is_file()
    rows = [a for a in service.repo.asset_list(result["project_id"]) if a["kind"] == "source"]
    assert len(rows) == 1 and rows[0]["path"] == str(path)


def test_title_with_hash_survives_the_round_trip(service, tmp_path):
    """标题里带 `#` 是常态；文件名与 DB 路径必须一致（前端 mediaUrl 另有转义测试）。"""
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    result = handler(FakeCtx(), payload(title="第 3 章 #12 讲"))
    path = Path(result["path"])
    assert "#" in path.name
    assert path.is_file()
    assert Path(service.repo.asset_list(result["project_id"])[0]["path"]).is_file()


def test_up_renamed_title_does_not_split_the_asset(service, tmp_path):
    """UP 改标题后重下同集：库里必须只剩一行原始视频，且指向一个真实存在的文件。

    注意这里**不**断言旧目录被清掉——目录名本来就跟着标题走（改动之前也一样），
    标题一变就是新目录，旧目录是改动前后都存在的遗留问题，不在本次修复范围内。
    本次要钉死的是：库里那行不会一分为二、不会遮蔽成新路径读不到。
    """
    media = tmp_path / "media"
    handler = W.make_download_handler(repo=service.repo, media_root=media, downloader=make_downloader())

    first = handler(FakeCtx(), payload(title="旧标题"))
    assert Path(first["path"]).is_file()

    second = handler(FakeCtx(), payload(title="新标题"))
    assert second["project_id"] == first["project_id"], "同一集不该建第二个项目"

    new_path = Path(second["path"])
    assert new_path.name == "新标题.mp4"
    rows = [a for a in service.repo.asset_list(first["project_id"]) if a["kind"] == "source"]
    assert len(rows) == 1, f"改名后残留了旧资产行：{[r['path'] for r in rows]}"
    assert rows[0]["path"] == str(new_path)
    assert Path(rows[0]["path"]).is_file(), "库里那行必须指向一个真实存在的文件"
    # 项目记录的目录也要跟着走，否则转录/剪辑仍然按老目录找文件
    assert Path(service.repo.project_get(first["project_id"])["root_dir"]) == new_path.parent


def test_repeated_download_stays_single_row(service, tmp_path):
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    first = handler(FakeCtx(), payload())
    for _ in range(3):
        handler(FakeCtx(), payload())
    rows = [a for a in service.repo.asset_list(first["project_id"]) if a["kind"] == "source"]
    assert len(rows) == 1


def test_legacy_project_dir_is_reused_without_rename(service, tmp_path):
    """历史项目（只有 source.mp4）：重下同集要认出是同一集，并且**不**把老文件改名。"""
    layout = allocate(
        tmp_path / "media",
        up_name="示例UP", title="第一讲 · 引论", collection_title="方法论合集",
        bvid="BV1xx411c7mD", cid=1001,
    ).ensure()
    legacy = layout.root / "source.mp4"
    legacy.write_bytes(b"x" * MIN_BYTES)
    write_sidecar(layout, build_sidecar(
        bvid="BV1xx411c7mD", aid=42, cid=1001, page=1, title="第一讲 · 引论", up="示例UP",
        collection="方法论合集", quality_requested="1080P", quality_actual="1080P",
        url="https://www.bilibili.com/video/BV1xx411c7mD", downloaded_at="2026-10-01T00:00:00+08:00",
        tool_version="0.1.0",
    ))

    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    result = handler(FakeCtx(), payload())

    assert result["reused"] is True, "旧目录里的 source.mp4 应被认出来，不该整集重下"
    assert Path(result["path"]) == legacy
    assert legacy.exists() and legacy.name == "source.mp4", "读侧兼容不等于顺手改名"


def test_transcriber_reads_source_from_db_not_a_hardcoded_name(service, tmp_path):
    """转录侧：文件叫什么都无所谓，只要库里的路径对得上。"""
    from video_workbench.core import transcriber as T

    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    result = handler(FakeCtx(), payload(title="一个很长的标题 带空格 和#号"))
    project = service.repo.project_get(result["project_id"])
    assert T._source_video(project, service.repo) == Path(result["path"])


def test_sidecar_name_is_still_the_format_anchor(service, tmp_path):
    """侧车名保持 source.wb.json：它是"还不知道媒体文件名时"唯一能定位目录身份的锚点。"""
    handler = W.make_download_handler(repo=service.repo, media_root=tmp_path / "media", downloader=make_downloader())
    result = handler(FakeCtx(), payload(title="标题"))
    root = Path(result["path"]).parent
    assert (root / SIDECAR).is_file()
    assert read_sidecar(root / SIDECAR)["title"] == "标题"
