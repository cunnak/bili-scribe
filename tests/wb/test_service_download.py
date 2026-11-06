"""下载相关 RPC 的装配测试（P1-02 / P1-07 的接口面）。

验的是**接线对不对**：参数模型放不放行、任务交给谁、重试会不会重下一遍。
真下载不在这里跑（那要网络和 ffmpeg），但"点了按钮到底发生了什么"必须能测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_workbench.core.resolver import Item, Resolution


class FakeResolver:
    def __init__(self, resolution: Resolution):
        self.resolution = resolution
        self.calls: list[str] = []

    def resolve(self, url: str) -> Resolution:
        self.calls.append(url)
        return self.resolution


def err_code(service, method, params=None):
    """dispatch 不抛异常而是回错误信封——界面靠它渲染文案，所以测信封。"""
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]["code"]


def single():
    return Resolution(
        kind="single",
        title="线性代数的本质",
        up_name="3Blue1Brown",
        items=[Item(bvid="BV1xx411c7mD", cid=123, page_no=1, title="线性代数的本质")],
    )


def collection(count: int = 3):
    return Resolution(
        kind="collection",
        title="合集：概率论",
        up_name="某UP",
        collection_title="概率论",
        items=[
            Item(bvid=f"BV{i}", page_no=i, title="概率论", part_title=f"第{i}讲")
            for i in range(1, count + 1)
        ],
    )


@pytest.fixture
def spy(service, monkeypatch):
    """把 submit 拦下来：既不让真下载起来，又能看清单。"""
    calls: list[tuple[str, dict]] = []

    def fake_submit(job_type, project_id=None, payload=None):
        calls.append((job_type, payload or {}))
        return {"id": f"job_{len(calls)}", "type": job_type, "status": "queued", "payload": payload}

    monkeypatch.setattr(service.jobs, "submit", fake_submit)
    return calls


def test_resolve_returns_payload_without_creating_job(service, spy, monkeypatch):
    monkeypatch.setattr(service, "_resolver", FakeResolver(single()))
    data = service.dispatch("download.resolve", {"url": "BV1xx411c7mD"})["data"]
    assert data["kind"] == "single"
    assert data["count"] == 1
    assert data["items"][0]["bvid"] == "BV1xx411c7mD"
    assert spy == [], "解析不该建任务"


def test_resolve_rejects_blank_url(service):
    assert err_code(service, "download.resolve", {"url": "   "}) == "VALIDATION"


def test_start_single_builds_batch_job(service, spy, monkeypatch):
    monkeypatch.setattr(service, "_resolver", FakeResolver(single()))
    out = service.dispatch("download.start", {"url": "BV1xx411c7mD", "quality": "720p"})["data"]
    assert out["count"] == 1
    job_type, payload = spy[0]
    assert job_type == "download.batch"
    assert payload["quality"] == "720P", "画质要归一化成大写"
    assert payload["items"][0]["cid"] == 123
    assert payload["up_name"] == "3Blue1Brown"


def test_start_collection_marks_all_items(service, spy, monkeypatch):
    monkeypatch.setattr(service, "_resolver", FakeResolver(collection(5)))
    out = service.dispatch("download.start", {"url": "https://space.bilibili.com/x"})["data"]
    assert out["count"] == 5
    payload = spy[0][1]
    assert payload["collection_title"] == "概率论"
    assert payload["page_count"] == 5
    assert [i["page"] for i in payload["items"]] == [1, 2, 3, 4, 5]


def test_start_accepts_explicit_items_without_resolving(service, spy, monkeypatch):
    """前端勾选后把 items 传回来时，不该再打一次 B站接口。"""
    fake = FakeResolver(single())
    monkeypatch.setattr(service, "_resolver", fake)
    chosen = [{"bvid": "BV1", "title": "a", "page": 2}, {"bvid": "BV2", "title": "b", "page": 3}]
    out = service.dispatch("download.start", {"items": chosen})["data"]
    assert out["count"] == 2
    assert fake.calls == []
    assert spy[0][1]["items"][1]["page"] == 3


def test_empty_resolution_is_an_error(service, spy, monkeypatch):
    monkeypatch.setattr(service, "_resolver", FakeResolver(Resolution(kind="empty", title="")))
    assert err_code(service, "download.start", {"url": "BV1"}) == "BILI_ERROR"
    assert spy == []


def test_retry_failed_only_resubmits_failures(service, spy, monkeypatch):
    monkeypatch.setattr(service, "_resolver", FakeResolver(collection(3)))
    job = {
        "id": "job_parent",
        "type": "download.batch",
        "status": "succeeded",
        "payload": {"quality": "1080P", "up_name": "某UP", "collection_title": "概率论"},
        "result": {
            "total": 3,
            "items": [
                {"bvid": "BV1", "title": "概率论", "page": 1, "status": "succeeded"},
                {"bvid": "BV2", "title": "概率论", "page": 2, "status": "failed"},
                {"bvid": "BV3", "title": "概率论", "page": 3, "status": "failed"},
            ],
        },
    }
    monkeypatch.setattr(service.jobs, "get", lambda _id: job)
    out = service.dispatch("download.retry_failed", {"id": "job_parent"})["data"]
    assert out["count"] == 2
    payload = spy[0][1]
    assert [i["page"] for i in payload["items"]] == [2, 3]
    assert payload["quality"] == "1080P"


def test_retry_without_failures_is_conflict(service, spy, monkeypatch):
    job = {"id": "j", "payload": {}, "result": {"items": [{"status": "succeeded"}]}}
    monkeypatch.setattr(service.jobs, "get", lambda _id: job)
    assert err_code(service, "download.retry_failed", {"id": "j"}) == "CONFLICT"


def test_import_local_via_rpc(service, tmp_path):
    video = tmp_path / "课程" / "第1讲.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 4096)
    data = service.dispatch("import.local", {"paths": [str(video)], "recursive": False})["data"]
    assert data["total"] == 1
    assert service.repo.project_count("") == 1


def test_import_local_probes_duration_and_thumb(service, tmp_path, monkeypatch):
    """本地导入必须接上探测与抽帧。

    实际缺陷：import_local 调 import_videos 时既不传 prober 也不传 thumbs，
    于是本地导入的项目 duration_sec 恒为 0、没有缩略图——素材库显示 00:00，
    编辑器的时间轴和波形按 0 时长算，播放头拖不动。P2 编辑器真机验收就是这么红的。
    """
    video = tmp_path / "课程" / "第2讲.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 4096)

    class Info:
        duration_ms = 125_000
        width, height, fps, vcodec, acodec = 1920, 1080, 30.0, "h264", "aac"

    monkeypatch.setattr(service, "_probe", staticmethod(lambda _p: Info()))

    def fake_thumb(_src, root):
        out = Path(root) / "frames" / "thumb.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"jpg")
        return out

    monkeypatch.setattr(service, "_thumb", staticmethod(fake_thumb))

    data = service.dispatch("import.local", {"paths": [str(video)], "recursive": False})["data"]
    pid = data["imported"][0]["project_id"]
    source = next(a for a in service.repo.asset_list(pid) if a["kind"] == "source")
    assert source["duration_ms"] == 125_000
    row = service._decorate(service.repo.project_get(pid))
    assert row["duration_sec"] == 125.0, "列表/时间轴要能看到时长"
    assert row["has_thumb"] is True


def test_import_local_needs_paths(service):
    assert err_code(service, "import.local", {"paths": []}) == "VALIDATION"


def test_unknown_method_still_errors(service):
    assert err_code(service, "download.nope", {}) in ("NOT_FOUND", "UNSUPPORTED")
