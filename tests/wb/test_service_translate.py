"""翻译与术语表 RPC 的装配测试（P2-05）。

验的是"界面能不能信"这四件事：

1. 翻译**不覆盖原始字幕**：草稿只落 ``wb_subtitle``，``wb_transcript`` 一个字节不动；
2. ``auto`` 的目标语言在提交时就定死并回给界面，不让用户猜翻译成了什么；
3. 确认（apply）之后才写 ``subtitle.{lang}.srt`` + ``subtitle.bilingual.ass``，
   并且只认用户接受的那几段；
4. 术语表非法行不落盘，CSV 导入能吃 GBK/带表头，导出能回来。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_workbench.config import read_ext
from video_workbench.core.subtitles import normalize


class StubClient:
    def __init__(self, replies, model="stub-model"):
        self.replies = list(replies)
        self.model = model
        self.base_url = "http://stub.local/v1"
        self.task_seen: list[str] = []
        self.calls = []

    @property
    def configured(self):
        return True

    def require(self):
        return None

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        item = self.replies.pop(0) if self.replies else "[]"
        if isinstance(item, Exception):
            raise item
        return item


class FakeCtx:
    def __init__(self):
        self.logs: list[str] = []
        self.steps: list[tuple[float | None, str | None]] = []

    def progress(self, value=None, stage=None, speed=None, eta=None):
        self.steps.append((value, stage))

    def log(self, line):
        self.logs.append(line)

    def should_cancel(self):
        return False

    def check_cancel(self):
        return None


def rpc(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is True, out
    return out["data"]


def err(service, method, params=None):
    out = service.dispatch(method, params or {})
    assert out.get("ok") is False, out
    return out["error"]


def configure(service, monkeypatch, client):
    service.cfg.override_llm(base_url=client.base_url, api_key="sk-test", model="stub-model")
    service.cfg.llm.model = client.model
    monkeypatch.setattr(service, "_llm_client", lambda task="subtitle": client)
    return client


def make_project(service, tmp_path: Path, *, texts=None) -> dict:
    root = tmp_path / "media" / "UP" / "标题"
    root.mkdir(parents=True, exist_ok=True)
    project = service.repo.project_create(title="标题", up_name="UP", root_dir=str(root),
                                          stage="transcribed")
    items = normalize([
        {"start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": t}
        for i, t in enumerate(texts or ["今天讲三件事", "然后第一件事", "第二件事"])
    ])
    (root / "transcript.json").write_text(
        json.dumps({"segments": items, "language": "zh", "model": "tiny"}, ensure_ascii=False),
        encoding="utf-8")
    service.repo.transcript_upsert(project["id"], segments=items, language="zh", model="tiny",
                                   srt_path=str(root / "transcript.srt"))
    return project


def original_texts(service, project_id) -> list[str]:
    row = service.repo.transcript_get(project_id) or {}
    return [s["text"] for s in normalize(row.get("segments") or [])]


# ---------------------------------------------------------------- 提交


def test_translate_without_config_does_not_create_job(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    service.cfg.llm.base_url = ""
    service.cfg.llm.api_key = ""
    service.cfg.llm.model = ""
    seen: list = []
    monkeypatch.setattr(service.jobs, "submit", lambda *a, **k: seen.append(a) or {"id": "j"})
    error = err(service, "subtitle.translate", {"project_id": project["id"]})
    assert error["code"] == "LLM_NOT_CONFIGURED"
    assert seen == []


def test_translate_unknown_project_is_not_found(service, monkeypatch):
    configure(service, monkeypatch, StubClient([]))
    assert err(service, "subtitle.translate", {"project_id": "pj_nope"})["code"] == "NOT_FOUND"


def test_translate_resolves_auto_at_submit_time(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)          # 源是中文
    configure(service, monkeypatch, StubClient([]))
    seen: dict = {}

    def fake_submit(job_type, project_id=None, payload=None):
        seen.update({"type": job_type, "payload": payload or {}})
        return {"id": "job_t", "type": job_type, "status": "queued"}

    monkeypatch.setattr(service.jobs, "submit", fake_submit)
    data = rpc(service, "subtitle.translate", {"project_id": project["id"], "target": "auto"})
    assert data["source"] == "zh" and data["target"] == "en"
    assert seen["type"] == "subtitle.translate"
    assert seen["payload"]["target"] == "en"          # 任务里不再猜语言


def test_translate_rejects_unknown_target(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    error = err(service, "subtitle.translate", {"project_id": project["id"], "target": "fr"})
    assert error["code"] == "VALIDATION"


def test_translate_clamps_batch_size(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    seen: dict = {}
    monkeypatch.setattr(service.jobs, "submit",
                        lambda t, p=None, payload=None: seen.update(payload or {}) or {"id": "j"})
    rpc(service, "subtitle.translate", {"project_id": project["id"], "batch_size": 9999})
    assert seen["batch_size"] == 50


# ---------------------------------------------------------------- handler


def test_handler_writes_draft_only_and_leaves_transcript_untouched(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    before = original_texts(service, project["id"])
    client = configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "Three things today."},
                    {"id": "s2", "text": "First one."},
                    {"id": "s3", "text": "Second one."}]),
    ]))
    result = service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    assert result["translated"] == 3 and result["failed"] == []
    # 原始字幕没被覆盖
    assert original_texts(service, project["id"]) == before
    row = service.repo.subtitle_get(project["id"], kind="translation", language="en")
    assert row and row["status"] == "draft"
    assert [s["translated_text"] for s in row["segments"]] == [
        "Three things today.", "First one.", "Second one."]


def test_handler_all_batches_failed_raises_and_keeps_db(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    from video_workbench.infra.llm import ChatError
    configure(service, monkeypatch, StubClient([ChatError("LLM_ERROR", "超时")]))
    with pytest.raises(Exception) as exc:
        service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    assert "所有批次都失败" in str(exc.value)
    assert service.repo.subtitle_get(project["id"], kind="translation", language="en") is None


def test_handler_failed_retry_keeps_confirmed_draft(service, tmp_path, monkeypatch):
    """重试全失败时不能把已确认的草稿打回 draft。

    这条是验收脚本抓出来的真缺陷：``translated`` 曾按"有译文的段数"算，
    库里还留着上次的译文，一整批全失败也数出满篇"成功"，于是草稿被重写成
    draft——用户看到的是"重试了一下，确认状态没了"。
    """
    from video_workbench.infra.llm import ChatError
    project = make_project(service, tmp_path)
    client = configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "one."}, {"id": "s2", "text": "two."},
                    {"id": "s3", "text": "three."}]),
    ]))
    service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    service.repo.subtitle_upsert(project["id"], kind="translation", language="en",
                                 segments=service.repo.subtitle_get(
                                     project["id"], kind="translation", language="en")["segments"],
                                 status="confirmed")
    client.replies = [ChatError("LLM_BAD_RESPONSE", "模型乱写", "不是 JSON")]
    with pytest.raises(Exception) as exc:
        service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en",
                                          "segment_ids": ["s1", "s2"]})
    assert "所有批次都失败" in str(exc.value)
    row = service.repo.subtitle_get(project["id"], kind="translation", language="en")
    assert row["status"] == "confirmed", "失败的重试把确认状态冲掉了"
    assert [s["translated_text"] for s in row["segments"]] == ["one.", "two.", "three."]


def test_handler_reuses_previous_draft_for_retry(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    client = configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "one."}, {"id": "s2", "text": "two."},
                    {"id": "s3", "text": "three."}]),
    ]))
    service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    # 只重试第二条：前一条的旧译文必须保住
    client.replies = [json.dumps([{"id": "s2", "text": "two, again."}])]
    result = service._translate_job(
        FakeCtx(), {"project_id": project["id"], "target": "en", "segment_ids": ["s2"]})
    got = {s["id"]: s["translated_text"] for s in result["segments"]}
    assert got["s1"] == "one." and got["s2"] == "two, again." and got["s3"] == "three."


def test_handler_applies_glossary_to_leaked_source_terms(service, tmp_path, monkeypatch):
    # 术语表强制替换管的是"模型没译、把源词原样带出来"这一类：
    # 模型回了 "AI 芯片 is expensive"，落定要变成 "accelerator is expensive"。
    # 模型换成别的写法（AI chip）就管不了——那是 prompt 的活，不是字符串替换的活。
    project = make_project(service, tmp_path, texts=["AI 芯片很贵", "第二件事", "第三件事"])
    service.cfg.subtitle.translate.glossary = ["AI 芯片=accelerator"]
    configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "AI 芯片 is expensive"}]),
        json.dumps([{"id": "s2", "text": "second"}]),
        json.dumps([{"id": "s3", "text": "third"}]),
    ]))
    result = service._translate_job(FakeCtx(),
                                    {"project_id": project["id"], "target": "en", "batch_size": 1})
    first = next(s for s in result["segments"] if s["id"] == "s1")
    assert first["translated_text"] == "accelerator is expensive"


# ---------------------------------------------------------------- 回读与确认


def test_translation_readback(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "one"}, {"id": "s2", "text": "two"},
                    {"id": "s3", "text": "three"}]),
    ]))
    assert rpc(service, "subtitle.translation", {"project_id": project["id"]})["found"] is False
    service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    data = rpc(service, "subtitle.translation", {"project_id": project["id"], "target": "en"})
    assert data["found"] is True and data["status"] == "draft"
    assert len(data["segments"]) == 3


def test_apply_writes_two_files_and_registers_assets(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "one"}, {"id": "s2", "text": "two"},
                    {"id": "s3", "text": "three"}]),
    ]))
    service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    data = rpc(service, "subtitle.translation.apply",
               {"project_id": project["id"], "target": "en", "layout": "original_top"})
    root = Path(project["root_dir"])
    srt = root / "subtitle.en.srt"
    ass = root / "subtitle.bilingual.ass"
    assert srt.is_file() and ass.is_file()
    assert [f for f in data["files"]] == [str(srt), str(ass)]
    text = ass.read_text(encoding="utf-8")
    assert "PlayResX" in text and r"one" in text and r"\N" in text
    paths = {a["path"] for a in service.repo.asset_list(project["id"])}
    assert str(srt) in paths and str(ass) in paths
    row = service.repo.subtitle_get(project["id"], kind="translation", language="en")
    assert row["status"] == "confirmed"


def test_apply_only_accepts_selected_segments(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([
        json.dumps([{"id": "s1", "text": "one"}, {"id": "s2", "text": "two"},
                    {"id": "s3", "text": "three"}]),
    ]))
    service._translate_job(FakeCtx(), {"project_id": project["id"], "target": "en"})
    rpc(service, "subtitle.translation.apply",
        {"project_id": project["id"], "target": "en", "accepted_ids": ["s1"]})
    srt = (Path(project["root_dir"]) / "subtitle.en.srt").read_text(encoding="utf-8")
    # 未接受的段退回原文，不出现空字幕块
    assert "one" in srt and "然后第一件事" in srt and "第二件事" in srt


def test_apply_without_draft_is_not_found(service, tmp_path, monkeypatch):
    project = make_project(service, tmp_path)
    configure(service, monkeypatch, StubClient([]))
    error = err(service, "subtitle.translation.apply", {"project_id": project["id"], "target": "en"})
    assert error["code"] == "NOT_FOUND"


# ---------------------------------------------------------------- 术语表


def test_glossary_put_rejects_bad_lines(service):
    error = err(service, "glossary.put", {"entries": ["Transformer=Transformer 模型", "没有等号"]})
    assert error["code"] == "VALIDATION"
    assert service.cfg.subtitle.translate.glossary == []


def test_glossary_put_persists(service, cfg_path):
    import yaml
    data = rpc(service, "glossary.put", {"entries": ["AI=人工智能", "token=词元"]})
    assert data["count"] == 2
    # subtitle 整段是 Workbench 独有，落 sidecar；共享 yaml 里不该出现
    assert "subtitle" not in yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8"))
    saved = read_ext(cfg_path)
    assert saved["subtitle"]["translate"]["glossary"] == ["AI=人工智能", "token=词元"]


def test_glossary_get_returns_parsed_pairs(service):
    rpc(service, "glossary.put", {"entries": ["AI=人工智能"]})
    data = rpc(service, "glossary.get")
    assert data["pairs"] == [{"source": "AI", "target": "人工智能"}]
    assert data["layout"] == service.cfg.subtitle.translate.layout


def test_glossary_import_csv_skips_header_and_merges(service, tmp_path):
    csv_path = tmp_path / "g.csv"
    csv_path.write_text("source,target\nAI,人工智能\nKV Cache,KV 缓存\n", encoding="utf-8")
    rpc(service, "glossary.put", {"entries": ["旧词=保留"]})
    data = rpc(service, "glossary.import_csv", {"path": str(csv_path), "merge": True})
    joined = "\n".join(data["entries"])
    assert "AI=人工智能" in joined and "KV Cache=KV 缓存" in joined and "旧词=保留" in joined


def test_glossary_import_csv_replaces_when_merge_false(service, tmp_path):
    csv_path = tmp_path / "g.csv"
    csv_path.write_text("AI,人工智能\n", encoding="utf-8")
    rpc(service, "glossary.put", {"entries": ["旧词=应删"]})
    data = rpc(service, "glossary.import_csv", {"path": str(csv_path), "merge": False})
    assert data["entries"] == ["AI=人工智能"]


def test_glossary_import_csv_reads_gbk(service, tmp_path):
    csv_path = tmp_path / "g.csv"
    csv_path.write_bytes("原文,译文\nAI,人工智能\n".encode("gbk"))
    data = rpc(service, "glossary.import_csv", {"path": str(csv_path), "merge": False})
    assert data["entries"] == ["AI=人工智能"]


def test_glossary_import_missing_file(service, tmp_path):
    error = err(service, "glossary.import_csv", {"path": str(tmp_path / "nope.csv")})
    assert error["code"] == "NOT_FOUND"


def test_glossary_export_roundtrip(service, tmp_path):
    rpc(service, "glossary.put", {"entries": ["AI=人工智能", "token=词元"]})
    out = tmp_path / "out.csv"
    data = rpc(service, "glossary.export_csv", {"path": str(out)})
    assert data["count"] == 2 and out.is_file()
    rpc(service, "glossary.put", {"entries": []})
    back = rpc(service, "glossary.import_csv", {"path": str(out), "merge": False})
    assert sorted(back["entries"]) == ["AI=人工智能", "token=词元"]
