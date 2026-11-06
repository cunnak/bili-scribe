"""错误码表、日志脱敏、诊断包。

这一组测试守的是"用户能不能看懂 + 能不能把问题带出来"：

* 每个错误码都得有一句中文人话，拼错一个码就让 ``WBError`` 当场炸；
* 日志里出现凭据就是事故，所以脱敏要按"宁可多抹"验；
* 诊断包必须真的能打开、真的不含 api key、真的带上了数据库版本。
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from video_workbench.app.errors import ERROR_CODES, RETRYABLE_CODES, WBError
from video_workbench.infra import log as log_mod

ROOT = Path(__file__).resolve().parents[2]
CODE_RE = re.compile(r'(?:WBError|ChatError)\(\s*"([A-Z_]+)"')


# ------------------------------------------------------------ 错误码表


def test_every_code_has_readable_message() -> None:
    assert ERROR_CODES, "错误码表不能为空"
    for code, text in ERROR_CODES.items():
        assert re.fullmatch(r"[A-Z][A-Z_]+", code), f"{code} 不像错误码"
        assert text.strip(), f"{code} 没有中文文案"
        assert not text.strip().endswith("。"), f"{code} 的文案不用句号（界面自己加标点）"


def test_retryable_codes_are_registered() -> None:
    unknown = RETRYABLE_CODES - set(ERROR_CODES)
    assert not unknown, f"可重试集合里有没注册的码：{sorted(unknown)}"


def test_unknown_code_explodes_immediately() -> None:
    with pytest.raises(ValueError):
        WBError("NO_SUCH_CODE", "x")


def test_payload_shape_and_retryable_default() -> None:
    err = WBError("LLM_TIMEOUT", "模型超时了")
    payload = err.to_payload()
    assert set(payload) == {"code", "message", "detail", "retryable"}
    assert payload["retryable"] is True  # LLM_TIMEOUT 在可重试集合里
    assert WBError("VALIDATION", "参数不合法").to_payload()["retryable"] is False
    assert WBError("VALIDATION", "x", retryable=True).to_payload()["retryable"] is True
    assert str(err) == "[LLM_TIMEOUT] 模型超时了"


def test_source_codes_are_all_registered() -> None:
    """扫源码里所有字面量错误码。新增码忘了进表 = 用户看到"内部错误"。"""
    missing: dict[str, list[str]] = {}
    for path in (ROOT / "video_workbench").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for code in CODE_RE.findall(text):
            if code not in ERROR_CODES:
                missing.setdefault(code, []).append(str(path.relative_to(ROOT)))
    assert not missing, f"源码里用了没注册的错误码：{json.dumps(missing, ensure_ascii=False)}"


# ------------------------------------------------------------ 脱敏


@pytest.mark.parametrize(
    "raw, expected_hint",
    [
        # 只剩字段名："api_key: ***" 比"sk-abc***"更好——谁也不需要看见密钥前缀
        ("api_key: sk-abcdef1234567890abcd", "api_key"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "Authorization"),
        ("Cookie: SESSDATA=abc123def456; bili_jct=zzz999", "Cookie"),
        ("https://x/y?SESSDATA=abcdefgh&bili_jct=ijklmnop", "SESSDATA"),
        ('{"token": "abcd1234efgh5678"}', "token"),
    ],
)
def test_redact_masks_credentials(raw: str, expected_hint: str) -> None:
    out = log_mod.redact(raw)
    assert expected_hint in out  # 保留字段名，方便人看懂是什么被抹了
    for secret in (
        "abcdef1234567890abcd",
        "eyJhbGciOiJIUzI1NiJ9",
        "abc123def456",
        "zzz999",
        "abcdefgh",
        "ijklmnop",
        "abcd1234efgh5678",
    ):
        assert secret not in out, f"{secret} 没被抹掉：{out}"


def test_redact_keeps_ordinary_text() -> None:
    text = "转码失败：ffmpeg exited 1，时间是 2026-10-30T21:00:00"
    assert log_mod.redact(text) == text


def test_redact_obj_walks_nested_config() -> None:
    cfg = {
        "llm": {
            "api_key": "sk-live-1234567890",
            "model": "deepseek-chat",
            "proxy": "http://127.0.0.1:7897",
        },
        "download": {"cookies_from": "bili_scribe"},
        "list": [{"token": "abc123456"}],
    }
    out = log_mod.redact_obj(cfg)
    assert out["llm"]["api_key"] == "***"
    assert out["llm"]["model"] == "deepseek-chat"
    assert out["list"][0]["token"] == "***"


# ------------------------------------------------------------ 日志落盘


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    first = log_mod.setup_logging(tmp_path, console=False)
    second = log_mod.setup_logging(tmp_path, console=False)
    assert first["file"] == second["file"]
    logger = log_mod.get_logger("test")
    logger.info("hello %s", "world")
    for handler in log_mod.get_logger().handlers:
        handler.flush()
    rows = log_mod.tail_lines(first["file"], 50)
    assert any("hello world" in line for line in rows)
    # 重复 setup 不该叠加 handler（否则一行日志写 N 遍）
    assert len([h for h in log_mod.get_logger().handlers if getattr(h, "_wb_owned", False)]) <= 2


def test_tail_lines_on_missing_file(tmp_path: Path) -> None:
    assert log_mod.tail_lines(tmp_path / "nope.log", 10) == []


def test_prune_removes_old_logs(tmp_path: Path) -> None:
    old = tmp_path / "wb-20200101.log"
    old.write_text("old", encoding="utf-8")
    fresh = tmp_path / "wb-29990101.log"
    fresh.write_text("fresh", encoding="utf-8")
    import os

    os.utime(old, (1_000_000, 1_000_000))
    assert log_mod.prune(tmp_path, keep_days=30) == 1
    assert fresh.exists() and not old.exists()


def test_log_dir_follows_cache_root(tmp_path: Path) -> None:
    cache = tmp_path / "VideoWorkbench" / "cache"
    assert log_mod.log_dir_for(cache) == tmp_path / "VideoWorkbench" / "logs"


def test_log_dir_expands_unresolved_env_vars(monkeypatch, tmp_path: Path) -> None:
    """原始配置串（`%APPDATA%/...`）传进来时也要展开。

    曾经的真实缺陷：`service` 把 `cfg.storage.cache_root`（未展开的原串）交给
    `log_dir_for`，于是在当前工作目录下建出字面叫 `%APPDATA%` 的文件夹，
    日志全写进仓库里。这条断言把"必须展开环境变量"钉住。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    got = log_mod.log_dir_for("%APPDATA%/VideoWorkbench/cache")
    assert got == tmp_path / "Roaming" / "VideoWorkbench" / "logs"
    assert "%APPDATA%" not in str(got)


# ------------------------------------------------------------ 诊断包


def test_write_bundle_redacts_config_and_includes_logs(tmp_path: Path) -> None:
    log_file = tmp_path / "wb-20261030.log"
    log_file.write_text(
        "\n".join(["line one", "Authorization: Bearer abcdef123456", "line three"]),
        encoding="utf-8",
    )
    out = tmp_path / "diag.zip"
    bundle = log_mod.write_bundle(
        out,
        title="测试诊断包",
        sections=[("环境", "python: 3.14"), ("数据库", "schema_version: 4")],
        config_yaml="llm:\n  api_key: sk-realkey1234567890\n  model: deepseek-chat\n",
        log_file=log_file,
        log_lines=20,
    )
    assert Path(bundle["path"]).is_file() and bundle["bytes"] > 0
    assert set(bundle["entries"]) == {"report.txt", "config.redacted.yaml", "log.txt"}

    with zipfile.ZipFile(out) as archive:
        report = archive.read("report.txt").decode("utf-8")
        config = archive.read("config.redacted.yaml").decode("utf-8")
        logs = archive.read("log.txt").decode("utf-8")
    assert "测试诊断包" in report and "schema_version: 4" in report
    assert "sk-realkey1234567890" not in config and "deepseek-chat" in config
    assert "abcdef123456" not in logs and "line three" in logs


def test_write_bundle_without_logs_still_works(tmp_path: Path) -> None:
    bundle = log_mod.write_bundle(tmp_path / "d.zip", title="t", sections=[("a", "b")])
    assert "log.txt" not in bundle["entries"]
    with zipfile.ZipFile(tmp_path / "d.zip") as archive:
        assert "report.txt" in archive.namelist()


# ------------------------------------------------------------ 服务层：诊断包与日志尾巴


def _call(service, method: str, params: dict | None = None) -> dict:
    reply = service.dispatch(method, params or {})
    assert reply["ok"], f"{method} 失败：{reply}"
    return reply["data"]


def test_app_info_reports_log_location(service) -> None:
    info = _call(service, "app.info")
    assert info["log_dir"] and info["log_file"]
    assert Path(info["log_dir"]).is_dir()
    assert Path(info["log_file"]).suffix == ".log"


def test_diagnostics_bundle_from_service(service) -> None:
    data = _call(service, "app.diagnostics", {"log_lines": 50})
    target = Path(data["path"])
    assert target.is_file() and target.suffix == ".zip"
    assert "report.txt" in data["entries"]
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        report = archive.read("report.txt").decode("utf-8")
        config = (
            archive.read("config.redacted.yaml").decode("utf-8")
            if "config.redacted.yaml" in names
            else ""
        )
    assert "report.txt" in names
    # 正文里该有的关键事实
    assert "schema_version" in report
    assert "错误码表" in report
    assert "VALIDATION" in report
    # 配置脱敏：测试配置里没有 key 就不该凭空出现 api_key: sk- 之类
    assert "sk-" not in config.replace("sk-***", "")


def test_diagnostics_rejects_outside_path(service, tmp_path: Path) -> None:
    outside = "C:/Windows/Temp/wb-diag.zip"
    reply = service.dispatch("app.diagnostics", {"path": outside})
    assert not reply["ok"]
    assert reply["error"]["code"] == "VALIDATION"


def test_diagnostics_accepts_path_inside_media_root(service, tmp_path: Path) -> None:
    inside = Path(service.cfg.media_root) / "diag-manual.zip"
    data = _call(service, "app.diagnostics", {"path": str(inside)})
    assert Path(data["path"]) == inside
    assert inside.is_file()


def test_log_tail_redacts_and_lists_files(service) -> None:
    service.logger.info("Authorization: Bearer %s", "abcdef1234567890")
    for handler in log_mod.get_logger().handlers:
        handler.flush()
    data = _call(service, "app.log_tail", {"lines": 100})
    assert "abcdef1234567890" not in data["text"]
    assert data["lines"] >= 1
    assert any(row["name"].startswith("wb-") for row in data["files"])


def test_job_log_goes_to_file(service) -> None:
    """任务日志必须同时落盘：界面抽屉关掉之后还得能查。"""
    job = _call(service, "job.demo.submit", {"steps": 4})
    job_id = job.get("job", job)["id"]
    import time

    for _ in range(200):
        row = _call(service, "job.get", {"id": job_id})
        row = row.get("job", row)
        if row["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(0.02)
    for handler in log_mod.get_logger().handlers:
        handler.flush()
    text = _call(service, "app.log_tail", {"lines": 400})["text"]
    assert job_id in text, f"日志里没有 {job_id}：{text[-400:]}"
