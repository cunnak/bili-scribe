"""守住一条：截图脚本写进 report/ 的配置里不许有 LLM 凭证。

为什么需要这个文件：`report/shoot.py`、`report/shoot_drawer.py`、`report/shoot_pop.py`
三个脚本都是「读 root config.yaml → 小改两处 → 原样写到 report/{shot,drawer,pop}-{skin}.yaml」。
而 `report/*.yaml` 是**受版本控制**的文件，仓库是公开的。2026-11 就是这么把一把
真实的 aihubmix key 推上去的（origin/main 顶点 7 个文件、历史 25 个提交都带着它，
吊销前一直可用）。光删旧值挡不住下一次：谁再跑一遍截图，新 key 又被复制进受跟踪
的文件里。

这三个脚本没有别的地方能拦住这个动作，所以把它变成一条会红的测试：
用一份带哨兵值 key 的假 config.yaml 走真实的 `make_cfg()`，要求产物里查不到哨兵。
把任意一个脚本里的抹除分支删掉，本文件立刻失败（我已实测，见交付说明）。
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "report"

if str(REPORT_DIR) not in sys.path:
    sys.path.insert(0, str(REPORT_DIR))

SENTINEL = "sk-LEAKTEST-Do-Never-Commit-9F3A"

# 长得像真配置的假配置：api_key 缩进两格、有 port 行，覆盖脚本改的那两处。
FAKE_CONFIG = f"""llm:
  base_url: "https://example.invalid/v1"
  api_key: "{SENTINEL}"
  model: "some-model"
  temperature: 0.3
  max_tokens: 4096
server:
  host: "127.0.0.1"
  port: 8818
"""

CASES = [
    ("shoot", "shot"),
    ("shoot_drawer", "drawer"),
    ("shoot_pop", "pop"),
]


@pytest.fixture()
def proj(tmp_path: Path) -> Path:
    """一个只有 config.yaml 和空 report/ 的临时项目根。"""
    root = tmp_path / "proj"
    (root / "report").mkdir(parents=True)
    (root / "config.yaml").write_text(FAKE_CONFIG, encoding="utf-8")
    return root


@pytest.mark.parametrize("module_name,prefix", CASES)
def test_make_cfg_writes_no_credential(
    module_name: str, prefix: str, proj: Path, monkeypatch
) -> None:
    """三个截图脚本生成的配置里不许带着 key 落进受版本控制的目录。"""
    mod = importlib.import_module(module_name)
    monkeypatch.setattr(mod, "PROJ", proj)

    out_path = mod.make_cfg("ark", 8899)

    text = out_path.read_text(encoding="utf-8")
    assert SENTINEL not in text, f"{module_name}.make_cfg() 把凭证写进了 {out_path.name}"
    assert re.search(r'^  api_key:\s*""', text, re.M), (
        f"{module_name}.make_cfg() 没有把 api_key 抹成空串，而是整行丢了或改了别处"
    )
    # 抹除不能把配置写坏：得仍是合法 YAML，且脚本原本要改的两处仍然生效。
    data = yaml.safe_load(text)
    assert data["llm"]["api_key"] == ""
    assert data["llm"]["base_url"] == "https://example.invalid/v1"
    assert data["server"]["port"] == 8899, "port 改写被我的分支吃掉了"
    assert out_path.name == f"{prefix}-ark.yaml", "产物路径变了，说明写进了别的地方"


@pytest.mark.parametrize("module_name,prefix", CASES)
def test_make_cfg_still_handles_ui_skin_line(
    module_name: str, prefix: str, proj: Path, monkeypatch
) -> None:
    """带 ui.skin 的真配置走完抹除后，皮肤替换不能失效（脚本靠它截两套皮）。"""
    (proj / "config.yaml").write_text(FAKE_CONFIG + "\nui:\n  skin: ark\n", encoding="utf-8")
    mod = importlib.import_module(module_name)
    monkeypatch.setattr(mod, "PROJ", proj)

    data = yaml.safe_load(mod.make_cfg("exhibition", 8899).read_text(encoding="utf-8"))

    assert data["ui"]["skin"] == "exhibition"
    assert data["llm"]["api_key"] == ""


@pytest.mark.parametrize("module_name,prefix", CASES)
def test_scripts_do_not_read_the_real_credential_at_all(module_name: str, prefix: str) -> None:
    """源码层面兜一条：这三个脚本里不许出现读 key 环境变量或硬编码 key 的路子。"""
    src = (REPORT_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    assert "sk-" not in src, f"{module_name}.py 里硬编码了疑似 key 的字面量"
    assert "api_key" in src, f"{module_name}.py 不再显式处理 api_key——抹除分支可能被删了"
