"""坏 JSON 抢救开关（task-5）的常驻判据。

开关之前只有一条路：设环境变量 `WB_SUMMARY_PARITY_STRICT=0`。那条路连**输出预算兜底**
一起放行（`_json_budget` 的阶梯只在该闸非严格时生效），等于「想抢救坏 JSON 就得顺手改预算」。
界面上这个新开关只放行修 JSON 的阶梯（修复 → 截断续写 → 正文/时间线分批 → 严格重问），
预算一个字不加，并且带止损：上一轮阶梯跑满仍失败，下一轮不再重跑。

**默认必须等于今天的行为**：关着的时候一次请求、坏了直接报错；请求形状与失败文案由
`tests/wb/test_summary_parity.py` 钉着。本文件钉的是开关本身——开起来做了什么、
关下去是不是真的什么都没变、界面是不是真把这个键接上了。

隔离：不联网、不起 GUI、不碰真实家目录（`homes` 夹具把两家都指到 Temp）。
"""

from __future__ import annotations

import json
import os
import shutil
import types
from pathlib import Path

from test_config_home_split import homes as homes  # fixture：新旧两家都在 Temp

from video_workbench.app.errors import WBError
from video_workbench.core import summarizer as adapter

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(os.environ.get("WB_WEB_ROOT") or ROOT / "video_workbench" / "web")

STRICT_ENV = adapter.PARITY_ENV
MAX_TOKENS = 4096
# 围栏样例：反引号用 chr(96) 拼，免得这份文件里混进 Markdown 围栏让人误读。
FENCE = chr(96) * 3 + "json" + chr(10) + '{"one_line": "好", "summary_md": "正文", "key_points": [], "timeline": []}' + chr(10) + chr(96) * 3
BAD = "这不是 JSON，也不会被修好"
MSGS = [{"role": "user", "content": "给我 JSON"}]


def _strict(monkeypatch):
    """把对账闸显式钉在严格档：本文件全部用例都在「默认档」这一侧测开关。"""
    monkeypatch.setenv(STRICT_ENV, "1")


def _cfg(rescue: bool):
    return types.SimpleNamespace(
        llm=types.SimpleNamespace(max_tokens=MAX_TOKENS, temperature=0.3, rescue_json=rescue)
    )


class _Stub:
    """按位置回吐的假客户端；`requests` 就是「真发出去几次」的唯一读数。"""

    model = "rescue-stub"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def require(self) -> None:
        return None

    def chat(self, messages, *, temperature=None, max_tokens=None, json_mode=False, note=None, **kw):
        self.requests.append(
            {"max_tokens": max_tokens, "json_mode": bool(json_mode), "temperature": temperature}
        )
        idx = len(self.requests) - 1
        if idx >= len(self.replies):
            raise AssertionError(
                f"假客户端的脚本只有 {len(self.replies)} 条，第 {idx + 1} 发没得回"
                "——说明实际请求次数比用例预期多（这正是本文件要盯的量）"
            )
        item = self.replies[idx]
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def _one_chat(llm, messages=MSGS):
    """发一次 json_mode 请求。返回 (正文或 None, 错误或 None)。"""
    try:
        return llm.chat(messages, json_mode=True), None
    except WBError as exc:
        return None, exc


# ------------------------------------------------------------------ 默认值


def test_the_switch_defaults_to_off_in_the_product_config():
    """默认值就是今天的行为：关。这条红 = 有人把抢救变成了对所有人生效的默认。"""
    from video_workbench.config import WorkbenchConfig

    cfg = WorkbenchConfig()
    assert cfg.llm.rescue_json is False, "抢救开关默认必须是 False（关）"
    assert adapter._rescue_json_on(cfg) is False
    # 没有配置对象时也必须当作关：适配层到处拿得到 None cfg，不能让 None 变成「开」。
    assert adapter._rescue_json_on(None) is False
    assert adapter._rescue_json_on(types.SimpleNamespace()) is False


# ------------------------------------------------------------------ 关着 = 一字不变


def test_off_sends_exactly_one_request_and_hands_the_body_over_untouched(monkeypatch):
    _strict(monkeypatch)
    stub = _Stub([BAD])
    llm = adapter.SummaryLLM(stub, model="m", cfg=_cfg(False))
    reply, exc = _one_chat(llm)
    assert exc is None, "严格档默认不抢救，正文必须原样交出，不在这一层报错"
    assert reply == BAD
    assert len(stub.requests) == 1, f"关着必须一发，实际 {len(stub.requests)}"
    assert llm.rescue_runs == 0 and llm.rescue_last_ok is False


def test_off_failure_names_both_the_new_switch_and_the_env_var():
    """失败文案给两条路：界面上的开关（只修 JSON）与环境变量（连预算一起放行）。"""
    text = adapter._strict_hint("模型没给出可用的总结 JSON：坏了")
    assert adapter.RESCUE_UI_PATH in text, "报错必须念出设置页那条路"
    assert text.rstrip().endswith(f"{STRICT_ENV}=0 后重试。"), "末句形状由 parity 钉死，别改语序"


# ------------------------------------------------------------------ 开起来做什么


def test_on_runs_the_ladder_and_stays_bounded(monkeypatch):
    _strict(monkeypatch)
    stub = _Stub([BAD] * 8)
    llm = adapter.SummaryLLM(stub, model="m", cfg=_cfg(True))
    reply, exc = _one_chat(llm)
    assert reply is None and exc is not None, "阶梯跑完仍坏必须报错，不许静默交出坏正文"
    assert len(stub.requests) > 1, "开着必须真的重试，不然这个开关是假的"
    # 实测 4 发。上限给到 8 是留余量：要拦的是「十几发」那种失控，
    # 不是从 4 挪到 5 的实现微调。
    assert len(stub.requests) <= 8, f"抢救请求数失控：{len(stub.requests)}"
    assert llm.rescue_runs == 1


def test_opening_the_switch_does_not_raise_the_output_budget(monkeypatch):
    """这个开关只放行修 JSON，预算是另一件事（环境变量那条路才动它）。

    写成「每一发的 max_tokens 都等于配置值」，不写死倍数——照着当前读数抄一个
    阶梯放大的常数，等于把实现细节当验收标准，实现一改就假红。
    """
    _strict(monkeypatch)
    stub = _Stub([BAD] * 8)
    llm = adapter.SummaryLLM(stub, model="m", cfg=_cfg(True))
    _one_chat(llm)
    assert len(stub.requests) > 1, "没跑出多发的用例证明不了预算闸的状态"
    seen = sorted({r["max_tokens"] for r in stub.requests})
    assert seen == [MAX_TOKENS], f"抢救多发的请求抬了预算：{seen}"


def test_a_failed_rescue_is_not_re_run_on_the_next_request(monkeypatch):
    """止损：上一轮阶梯跑满仍失败，下一轮只发主请求，不再重跑阶梯。

    这一条是「真模型调用不许无预算」的产品侧兜底——模型系统性产不出合法 JSON 时，
    每一次 json_mode 请求都跑满阶梯，一轮总结就能烧掉十几发。
    """
    _strict(monkeypatch)
    stub = _Stub([BAD] * 12)
    llm = adapter.SummaryLLM(stub, model="m", cfg=_cfg(True))
    _one_chat(llm)
    assert llm.rescue_runs == 1 and llm.rescue_last_ok is False
    before = len(stub.requests)
    reply, exc = _one_chat(llm)
    assert reply is None and exc is not None
    delta = len(stub.requests) - before
    assert delta == 1, f"止损没生效：这一轮又发了 {delta} 发（期望只有主请求 1 发）"
    assert adapter.RESCUE_UI_PATH in str(exc.message), "止损文案要告诉用户怎么退回默认档"


def test_a_successful_rescue_keeps_the_next_request_eligible(monkeypatch):
    """止损不许写成「每个实例只抢救一次」——那等于预读救回来过，生成就没得救。"""
    _strict(monkeypatch)
    stub = _Stub([BAD, FENCE, FENCE] + [BAD] * 12)
    llm = adapter.SummaryLLM(stub, model="m", cfg=_cfg(True))
    first, exc1 = _one_chat(llm)
    assert exc1 is None and first, f"第一次抢救就该救回来：{exc1}"
    assert llm.rescue_runs == 1 and llm.rescue_last_ok is True
    before = len(stub.requests)
    _one_chat(llm)
    assert len(stub.requests) - before > 1, "上一次抢救成功过，这一轮必须还能再试"


def test_the_switch_is_read_per_call_not_frozen_at_import(monkeypatch):
    """与 `_parity_strict()` 同族：每次现读配置，改了不用重开程序。"""
    _strict(monkeypatch)
    cfg = _cfg(False)
    stub = _Stub([BAD] * 10)
    llm = adapter.SummaryLLM(stub, model="m", cfg=cfg)
    _one_chat(llm)
    assert len(stub.requests) == 1, "关着必须一发"
    cfg.llm.rescue_json = True
    _one_chat(llm)
    assert len(stub.requests) > 2, "同一个实例内改配置必须立刻生效（不许 import 期就绑死）"


# ------------------------------------------------------------------ 文案三态


def test_the_failure_wording_stops_teaching_an_already_open_switch():
    on = adapter._strict_hint("前半句：坏了", _cfg(True))
    assert "请打开" not in on, "开关已经开着，别再教用户去打开它"
    assert adapter.RESCUE_UI_PATH in on
    off = adapter._strict_hint("前半句：坏了", _cfg(False))
    assert "请打开" in off and off != on, "关着与开着必须念不同的话"


def test_the_stop_loss_hint_offers_a_way_back():
    text = adapter._rescue_exhausted_hint("尾巴")
    assert adapter.RESCUE_UI_PATH in text, "要给出退回默认档的入口，不能只说失败了"
    assert "尾巴" in text, "原文尾巴要带着，用户才知道模型到底吐了什么"


# ------------------------------------------------------------------ 界面接线


def _rescue_field_object(source: str) -> str:
    """从 settings.js 的声明表里抠出 llm.rescue_json 那个字段对象（源码文本）。"""
    key = 'path: "llm.rescue_json"'
    i = source.find(key)
    if i < 0:
        return ""
    start = source.rfind("{", 0, i)
    depth, j = 0, start
    while j < len(source):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[start : j + 1]
        j += 1
    return ""


def test_the_settings_page_binds_a_switch_to_the_new_key():
    """「界面上有、配置里没有」是这个仓库反复踩的坑——反过来也一样要拦。"""
    source = (WEB_DIR / "js" / "pages" / "settings.js").read_text(encoding="utf-8")
    block = _rescue_field_object(source)
    assert block, "设置页必须真有 llm.rescue_json 这一栏"
    assert 'type: "switch"' in block, "开关必须是 switch，不是数字框"
    assert "默认关" in block, "文案必须说清默认关，否则用户以为打开是推荐做法"


def test_the_web_root_override_switches_the_file_not_the_verdict(tmp_path):
    """扫描域走 WB_WEB_ROOT：牙齿有合法入口，不必改工作区文件去验红。

    同一个判据函数，喂真树必须为真、喂抠掉那一栏的副本必须为假——
    如果判据其实是硬编码读仓库文件，第二半就会红。
    """
    fake = tmp_path / "web"
    shutil.copytree(WEB_DIR, fake)
    target = fake / "js" / "pages" / "settings.js"
    source = target.read_text(encoding="utf-8")
    assert _rescue_field_object(source), "夹具前提：真树里这一栏必须在"
    block = _rescue_field_object(source)
    cut = source.replace(block + chr(13) + chr(10), "").replace(block + chr(10), "")
    if cut == source:
        i = source.index(block)
        cut = source[:i] + source[i + len(block) :]
    target.write_text(cut, encoding="utf-8")
    assert _rescue_field_object(target.read_text(encoding="utf-8")) == "", "抠掉后判据必须转假"


def test_the_mock_default_matches_the_backend_default():
    """演示态那份 SETTINGS.llm 必须给 false：mock 里写 true 等于界面默认与后端不一致。"""
    mock = (WEB_DIR / "js" / "mock.js").read_text(encoding="utf-8")
    assert mock.count("rescue_json: false") == 1, (
        f"mock 默认值必须恰好一处，实际 {mock.count('rescue_json:')}"
    )
    # 切点必须落在真正的载荷入口上：`summary.roles` 这几个字在别处也出现
    # （SETTINGS.llm 之前那条注释里就有），拿它当切点会把 SETTINGS.llm 一起切进来，
    # 判据就退化成「注释里提到过这个键」——那是假绿。
    entry = 'case "summary.roles":'
    assert mock.count(entry) == 1, f"夹具前提：mock 里 summary.roles 入口必须唯一，实际 {mock.count(entry)}"
    roles_payload = mock.split(entry, 1)[1].split("case ", 1)[0]
    assert "rescue_json" not in roles_payload, "summary.roles 的演示载荷里不该出现这个键"


# ------------------------------------------------------------------ 配置往返


def test_the_key_round_trips_through_the_settings_file(homes, monkeypatch):
    """设置页保存走的就是这条路径：apply → 校验 → save → 下次 load 必须还是 True。"""
    from video_workbench.config import WorkbenchConfig

    _strict(monkeypatch)
    first = WorkbenchConfig.load()
    assert first.llm.rescue_json is False, "新库默认关"
    first.apply({"llm": {"rescue_json": True}})
    assert first.settings_view()["llm"]["rescue_json"] is True
    first.save()
    again = WorkbenchConfig.load()
    assert again.llm.rescue_json is True, "保存后重开必须还是开（否则界面上的开关是摆设）"
    again.apply({"llm": {"rescue_json": False}})
    again.save()
    assert WorkbenchConfig.load().llm.rescue_json is False


def test_the_key_does_not_leak_into_the_bili_facing_yaml():
    """`render_yaml` 按 bili-scribe 的 AppConfig 投影，WB 扩展键故意不写进 config.yaml。

    这条不是「顺便绿」：它钉的是新开关不会污染对方那份配置——真污染了，
    bili-scribe load 时会报未知键。
    """
    from video_workbench.config import WorkbenchConfig, render_yaml

    cfg = WorkbenchConfig()
    cfg.llm.rescue_json = True
    assert "rescue_json" not in render_yaml(cfg)

