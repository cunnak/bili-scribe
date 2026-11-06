"""P6-05 命令行的测试：只管"契约"，不碰真网真模型。

三条底线：

1. `--json` 的 stdout 必须是**一行**合法 JSON——脚本要 `jq` 吃得下；
2. 退出码要能区分"跑挂了（1）"和"你参数/环境不对（2）"；
3. 子命令走的都是 RPC（`service.router`），不接受命令行自己另开一条捷径。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from video_workbench import cli as cli_mod


@pytest.fixture()
def run(cfg_path, tmp_path):
    """跑一次命令行，返回 (退出码, stdout, stdout 解析后的对象或 None)。"""

    def _run(*argv: str):
        out = io.StringIO()
        code = None
        old = sys.stdout
        try:
            with redirect_stdout(out):
                code = cli_mod.main(["--config", str(cfg_path), *argv])
        finally:
            sys.stdout = old
        text = out.getvalue()
        payload = None
        stripped = text.strip()
        if stripped.startswith("{"):
            payload = json.loads(stripped)
        return code, text, payload

    return _run


def test_info_json_is_one_line_and_reports_paths(run, cfg_path):
    code, text, payload = run("--json", "info")
    assert code == 0
    assert len([line for line in text.splitlines() if line.strip()]) == 1, (
        "stdout 不止一行，脚本没法直接 parse"
    )
    assert payload["ok"] is True
    assert payload["cmd"] == "info"
    info = payload["data"]["info"]
    assert info["name"] == "VideoWorkbench"
    assert info["config_path"] == str(cfg_path)
    assert "deps" in info and "ffmpeg" in info["deps"]


def test_info_human_mode_prints_paths(run):
    code, text, _ = run("info")
    assert code == 0
    assert "VideoWorkbench" in text
    assert "配置：" in text and "数据库：" in text


def test_jobs_json_returns_list(run):
    code, _, payload = run("--json", "jobs")
    assert code == 0
    assert isinstance(payload["data"], list)


def test_unknown_project_exits_2_with_not_found(run):
    code, _, payload = run("--json", "summarize", "pj_不存在")
    assert code == 2, "前条件不对应当是 2，不是 1"
    assert payload["ok"] is False
    assert payload["error"]["code"] in ("NOT_FOUND", "VALIDATION")


def test_missing_llm_is_bad_input_not_crash(run, service):
    """没配大模型是"环境没准备好"，脚本据此应当停下来改配置，而不是以为批处理跑挂了。"""
    project = service.repo.project_create(
        title="CLI 无模型用例",
        source_kind="local",
        root_dir=str(Path(str(service.cfg.media_root)) / "cli"),
        stage="transcribed",
    )
    service.repo.transcript_upsert(
        project["id"],
        segments=[{"id": "s1", "start_ms": 0, "end_ms": 2000, "text": "你好"}],
        language="zh",
        model="tiny",
    )
    code, _, payload = run("--json", "summarize", project["id"])
    assert code == 2
    assert payload["error"]["code"] == "LLM_NOT_CONFIGURED"


def test_download_requires_url(run):
    with pytest.raises(SystemExit) as exc:
        run("--json", "download")
    assert exc.value.code == 2


def test_merge_requires_two_files(run, tmp_path):
    one = tmp_path / "a.mp4"
    one.write_bytes(b"x")
    code, _, payload = run("--json", "merge", str(one))
    assert code == 2
    assert payload["error"]["code"] == "VALIDATION"


def test_transcribe_without_projects_is_rejected(run):
    with pytest.raises(SystemExit) as exc:
        run("--json", "transcribe")
    assert exc.value.code == 2


def test_run_with_project_id_and_skips_returns_project(run, service):
    """`run --project-id --skip-*` 是脚本里最常用的形态：只做编排，不碰真模型。"""
    project = service.repo.project_create(
        title="CLI 编排用例",
        source_kind="local",
        root_dir=str(Path(str(service.cfg.media_root)) / "cli2"),
        stage="transcribed",
    )
    code, _, payload = run(
        "--json", "run", "--project-id", project["id"], "--skip-transcribe", "--skip-summary"
    )
    assert code == 0
    assert payload["data"]["project_id"] == project["id"]
    assert payload["data"]["steps"] == []
    assert payload["data"]["exports"] == []


def test_exit_code_mapping_table():
    """退出码是契约，被人改坏必须立刻红。"""
    assert cli_mod._exit_for({"code": "OK"}) == 1  # 未知码当失败
    assert cli_mod._exit_for({"code": "NOT_FOUND"}) == 2
    assert cli_mod._exit_for({"code": "VALIDATION"}) == 2
    assert cli_mod._exit_for({"code": "PROCESS_FAILED"}) == 1
    assert cli_mod._exit_for({"code": "LLM_ERROR"}) == 1
    assert cli_mod._exit_for({"code": "CANCELED"}) == 3


def test_help_lists_five_verbs_and_run():
    parser = cli_mod.build_parser()
    text = parser.format_help()
    for verb in ("download", "transcribe", "summarize", "clip", "merge", "run"):
        assert verb in text, f"帮助里没有 {verb}"


def test_globals_can_follow_the_subcommand():
    """`cli info --json` 是人的直觉写法：argparse 只认前置，所以入口要替人吞掉顺序。

    这条红了说明 `hoist_globals()` 被改坏——症状很讨厌：参数看着都对，命令直接 usage 退出 2。
    """
    assert cli_mod.hoist_globals(["info", "--json"]) == ["--json", "info"]
    assert cli_mod.hoist_globals(["jobs", "--quiet", "--timeout", "5"]) == [
        "--quiet",
        "--timeout",
        "5",
        "jobs",
    ]
    # 已经在前面的不能重复搬
    assert cli_mod.hoist_globals(["--json", "info"]) == ["--json", "info"]
    # 子命令自己的参数不能被当成全局参数搬走
    assert cli_mod.hoist_globals(["merge", "a.mp4", "-o", "x.mp4", "--mode", "precise"]) == [
        "merge",
        "a.mp4",
        "-o",
        "x.mp4",
        "--mode",
        "precise",
    ]
    # --config=... 这种带等号的写法要整体搬
    assert cli_mod.hoist_globals(["info", "--config=/tmp/x.yaml"]) == [
        "--config=/tmp/x.yaml",
        "info",
    ]


def test_json_works_after_subcommand(run):
    """落盘级验证：`info --json` 的 stdout 仍然只有一行 JSON。"""
    code, text, payload = run("info", "--json")
    assert code == 0
    assert payload and payload["ok"] is True
    assert len([line for line in text.splitlines() if line.strip()]) == 1
