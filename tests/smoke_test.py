"""冒烟测试：离线单元检查 + 真实 HTTP 端点探测。

    python tests/smoke_test.py

全部检查都不需要 B站 登录态，也不需要大模型 api_key——它验证的是"代码本身有没有
坏"和"服务能不能起来"。依赖外部的检查（字幕、发布）会被跳过而不是失败，因为那
两项需要真实凭据。

注意：`/api/auth/qrcode` 这一项会真的去请求 B站 的扫码接口。它只做匿名取码，
不涉及登录，但确实会打一次线上请求。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

# 中文 Windows 控制台默认走 GBK，输出中文会直接抛 UnicodeEncodeError。
# 这里显式切到 UTF-8 并用 replace 兜底，保证日志不会因为编码问题把测试结果吞掉。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

report: dict[str, list] = {"offline": [], "http": [], "errors": []}


def check(name: str, fn) -> None:
    try:
        report["offline"].append({"name": name, "ok": True, "detail": fn()})
    except Exception as exc:  # noqa: BLE001 - 测试要吞掉一切并记录
        report["offline"].append(
            {"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        )


# ---------------------------------------------------------------- 离线检查


def _imports() -> str:
    import bili_scribe
    from bili_scribe.app.main import create_app  # noqa: F401
    from bili_scribe.core import collector, composer, publisher, resolver, summarizer  # noqa: F401
    from bili_scribe.infra import bili_client, endpoints, llm, store, wbi  # noqa: F401

    return f"version={bili_scribe.__version__}，全部模块导入成功"


def _config() -> str:
    from bili_scribe.config import AppConfig

    cfg = AppConfig.load(PROJECT / "config.yaml")
    assert cfg.llm.model
    assert isinstance(cfg.collect.prefer_subtitle_lang, list)
    public = cfg.public_view()
    assert "api_key" not in public["llm"], "public_view 泄露了 api_key"
    return (
        f"model={cfg.llm.model}，confirm={cfg.publish.require_confirm}，"
        f"langs={cfg.collect.prefer_subtitle_lang}，api_key 已脱敏"
    )


def _config_without_file() -> str:
    """没有 config.yaml 时必须能退回默认值，否则首次运行直接崩。"""
    from bili_scribe.config import AppConfig

    cfg = AppConfig.load(None)
    assert cfg.llm.base_url and cfg.server.port > 0
    return f"缺省配置可用：port={cfg.server.port}, model={cfg.llm.model}"


def _config_rejects_unknown() -> str:
    import tempfile

    from bili_scribe.config import AppConfig

    try:
        AppConfig.load(PROJECT / "config.yaml.bogus")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("指定了不存在的配置文件却没有报错")

    path = Path(tempfile.mkdtemp()) / "bad.yaml"
    path.write_text("llm:\n  typo_key: 1\n", encoding="utf-8")
    try:
        AppConfig.load(path)
    except ValueError as exc:
        return f"未知配置项被拒绝：{exc}"
    raise AssertionError("未知配置项没有报错，配置写错会静默失效")


def _wbi() -> str:
    from bili_scribe.infra.wbi import MIXIN_KEY_ENC_TAB, WbiSigner

    assert len(MIXIN_KEY_ENC_TAB) == 64, "重排表不是 64 位"
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64)), "重排表不是 0..63 的排列"
    key = WbiSigner.mixin_key("a" * 32, "b" * 32)
    assert len(key) == 32, f"mixin_key 长度 {len(key)}，应为 32"

    signer = WbiSigner(
        lambda _url: {
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                }
            }
        }
    )
    signed = signer.sign({"aid": 1, "cid": 2, "keyword": "a!b'c(d)e*f"})
    assert len(signed["w_rid"]) == 32
    assert "wts" in signed
    assert signed["keyword"] == "abcdef", f"未剔除非法字符：{signed['keyword']}"
    return f"mixin_key 32 位，w_rid={signed['w_rid'][:12]}…，非法字符已剔除"


def _resolver_url() -> str:
    from bili_scribe.core.resolver import Target

    paged = Target(bvid="BV1xx411c7mD", page=3)
    assert paged.url.endswith("/?p=3"), paged.url
    assert Target(bvid="BV1xx411c7mD", page=1).url.endswith("/BV1xx411c7mD/")
    return f"URL 拼装正确：{paged.url}"


def _composer() -> str:
    from bili_scribe.config import PublishConfig
    from bili_scribe.core.collector import Bundle
    from bili_scribe.core.composer import build_comment, to_plain_text
    from bili_scribe.core.summarizer import Summary

    cfg = PublishConfig(max_comment_chars=120, disclose_ai=True)
    long_text = "## 这是标题\n\n**要点**：这段很长。" * 40
    summary = Summary(one_line="一句话", summary_md=long_text, comment_short=long_text)
    bundle = Bundle(
        bvid="BV1", aid=1, cid=1, page=1, page_count=1,
        title="t", desc="", up_name="u", up_mid=1, duration=60, tname="x",
    )
    comment = build_comment(summary, bundle, cfg)

    assert "#" not in comment and "*" not in comment, "Markdown 标记未洗净"
    assert "\n" not in comment, "评论里还有换行"
    assert comment.endswith(cfg.disclosure_text), "免责声明被裁掉了"
    assert len(comment) <= cfg.max_comment_chars, f"超出限制：{len(comment)}"
    assert to_plain_text("a\n\nb   c") == "a b c"
    return f"长度={len(comment)}/{cfg.max_comment_chars}，声明完整，标记已洗净"


def _summarizer() -> str:
    from bili_scribe.core.summarizer import _coerce_timeline, _extract_json, _split

    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}, "代码块包裹未处理"
    assert _extract_json('前言 {"a": {"b": 2}} 后记') == {"a": {"b": 2}}, "前后噪声未处理"

    timeline = _coerce_timeline(
        [{"time": "01:00", "content": "x"}, "纯字符串", {"t": "", "text": ""}]
    )
    assert timeline[0] == {"t": "01:00", "text": "x"}, timeline
    assert timeline[1]["text"] == "纯字符串", timeline

    # 分块上限是成本护栏，长素材不能突破它
    pieces = _split("字" * 100000, 6000, 400)
    assert len(pieces) <= 8, f"分块数 {len(pieces)} 超过上限 8"
    assert "".join(pieces).count("字") >= 100000, "分块有遗漏"

    # 刚超过阈值时应切成两块且保留重叠
    two = _split("字" * 7000, 6000, 400)
    assert len(two) == 2, f"边界情况切成了 {len(two)} 块"
    return f"JSON 容错 OK，10 万字切 {len(pieces)} 段（上限 8），边界切成 {len(two)} 段"


def _subtitle_merge() -> str:
    from bili_scribe.core.collector import _merge_subtitle, _parse_subtitle

    payload = {
        "body": [
            {"from": 0.0, "content": "大家好"},
            {"from": 2.0, "segs": [{"utf8": "今天"}, {"utf8": "聊一个话题"}]},
            {"from": 20.0, "content": "第二段开始了"},
        ]
    }
    items = _parse_subtitle(payload)
    assert items[1][1] == "今天聊一个话题", f"segs 解析异常：{items}"

    merged = _merge_subtitle(items, window=10.0)
    assert len(merged) == 2, f"时间窗聚合结果不对：{merged}"
    assert merged[0][1] == "大家好今天聊一个话题", merged[0]
    assert merged[1][0] == 20.0, merged[1]
    return f"解析 {len(items)} 条 → 聚合 {len(merged)} 段，时间戳 [{merged[0][0]}, {merged[1][0]}]"


def _store() -> str:
    import tempfile

    from bili_scribe.infra.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="bs-smoke-"))
    st = Store(str(tmp / "t.db"), str(tmp / "k.bin"))

    st.save_credential({"SESSDATA": "s", "bili_jct": "j"}, mid="42", uname="小村")
    got = st.get_credential()
    assert got and got["cookies"]["SESSDATA"] == "s", "凭据读回失败"
    assert b"SESSDATA" not in (tmp / "t.db").read_bytes(), "凭据在数据库里是明文"

    task_id = st.create_task("BV1xx411c7mD")
    st.update_task(
        task_id, status="ready", warnings=["w1"], candidates=[{"k": 1}],
        summary_json={"one_line": "x"},
    )
    task = st.get_task(task_id)
    assert task["warnings"] == ["w1"], task["warnings"]
    assert task["candidates"] == [{"k": 1}], task["candidates"]
    assert task["summary_json"] == {"one_line": "x"}

    pid = st.add_publish(task_id, "BV1xx411c7mD", 1, "999", "内容", "published")
    assert st.find_live_publish("BV1xx411c7mD") is not None, "去重查询失败"
    assert st.last_publish_at() is not None
    st.mark_publish_deleted(pid)
    assert st.find_live_publish("BV1xx411c7mD") is None, "撤回后去重未解除"
    return "凭据加密落盘，任务/去重/撤回往返均正常"


def _publisher_gates() -> str:
    import tempfile

    from bili_scribe.config import PublishConfig
    from bili_scribe.core.publisher import Publisher
    from bili_scribe.infra.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="bs-pub-"))
    st = Store(str(tmp / "t.db"), str(tmp / "k.bin"))

    class DummyClient:
        is_logged_in = False

        def post_form(self, *args, **kwargs):
            raise AssertionError("未登录时不应该发出发布请求")

    pub = Publisher(DummyClient(), st, PublishConfig(require_confirm=True))

    assert pub.publish("t1", "BVx", 1, "内容", confirmed=False).status == "blocked"
    not_logged = pub.publish("t1", "BVx", 1, "内容", confirmed=True)
    assert not_logged.status == "blocked" and "未登录" in not_logged.message
    assert pub.publish("t1", "BVx", None, "内容", confirmed=True).status == "failed"
    return "三道闸门按预期拦截：未确认 / 未登录 / 缺 aid"


def _qr_missing_message() -> str:
    """缺 qrcode 依赖时，必须抛出能照着做的提示，而不是裸 ImportError。

    这是真实踩过的坑：`_qr_png_data_url` 里的 import 在函数内，所以缺依赖时应用
    照常启动、点按钮才炸，而裸 ImportError 一路冒到 HTTP 层只剩一个没有正文的 500。
    """
    import sys

    from bili_scribe.app.main import QrUnavailable, _qr_available, _qr_png_data_url

    saved = sys.modules.get("qrcode")
    sys.modules["qrcode"] = None  # 让 `import qrcode` 直接抛 ImportError
    try:
        assert _qr_available() is False, "屏蔽 qrcode 后 _qr_available 仍返回 True"
        try:
            _qr_png_data_url("https://example.com/x")
        except QrUnavailable as exc:
            message = str(exc)
        else:
            raise AssertionError("缺依赖时没有抛出 QrUnavailable")
    finally:
        if saved is None:
            sys.modules.pop("qrcode", None)
        else:
            sys.modules["qrcode"] = saved

    assert "qrcode[pil]" in message, f"提示里没有给出可执行的修复命令：{message}"
    assert _qr_available() is True, "恢复后 _qr_available 没有回到 True"
    return f"抛出 QrUnavailable 并给出修复命令：{message[:46]}…"


def _sensitive_files_excluded() -> str:
    """设页保存会产生 config.yaml.bak，它可能含真实 api_key。

    这个文件必须同时被两道防线挡掉：git（不进仓库）和打包脚本（不进发布包）。
    两边都曾经漏掉它——.gitignore 只写了 config.yaml，package.py 只按 .db/.bin 排除。
    """
    ignore = (PROJECT / ".gitignore").read_text(encoding="utf-8")
    assert "config.yaml.bak" in ignore, ".gitignore 没挡住 config.yaml.bak"
    assert "config.yaml.tmp" in ignore, ".gitignore 没挡住 config.yaml.tmp"

    # 按文件路径加载，避免 `import package` 撞上同名模块
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_pkg_under_test", PROJECT / "scripts" / "package.py"
    )
    pkg = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(pkg)  # type: ignore[union-attr]

    for risky in ("config.yaml", "config.yaml.bak", "config.yaml.tmp", "key.bin", "data.db"):
        assert pkg.should_skip(Path(risky)), f"打包脚本会收进 {risky}"
    # 别误伤：示例配置必须能进包
    assert not pkg.should_skip(Path("config.example.yaml")), "config.example.yaml 被误排除了"
    return "gitignore 与打包脚本都挡住了 .bak/.tmp，且未误伤 config.example.yaml"


def _llm_thinking() -> str:
    """思考模型的输出预算陷阱。

    实测过的现象：max_tokens=4096，completion_tokens 却是 5125，其中 5120 是思考，
    正文只有一个 []。而且 HTTP 200、finish_reason=stop —— 从响应表面看不出任何异常。
    所以这段逻辑必须有用例盯着，否则回归了也没人知道。
    """
    import tempfile

    import requests

    from bili_scribe.config import THINKING_MODES, AppConfig, LlmConfig
    from bili_scribe.infra.llm import THINKING_PARAM, ChatResult, LlmClient, LlmError

    msgs = [{"role": "user", "content": "hi"}]

    def client(thinking: str) -> LlmClient:
        return LlmClient(
            LlmConfig(api_key="k", model="m", base_url="http://example.invalid/v1", thinking=thinking)
        )
    # ---------- 1. 参数发送策略 ----------
    assert THINKING_PARAM not in client("auto")._payload(msgs, None, None, False, "auto"), (
        "thinking=auto 不该发送任何相关参数（对非思考模型要保持兼容）"
    )
    assert client("off")._payload(msgs, None, None, False, "off")[THINKING_PARAM] is False
    assert client("on")._payload(msgs, None, None, False, "on")[THINKING_PARAM] is True

    # ---------- 2. 枚举校验 ----------
    assert THINKING_MODES == ("auto", "off", "on")
    bad = Path(tempfile.mkdtemp()) / "bad-thinking.yaml"
    bad.write_text("llm:\n  thinking: sometimes\n", encoding="utf-8")
    try:
        AppConfig.load(bad)
    except ValueError as exc:
        assert "thinking" in str(exc), exc
    else:
        raise AssertionError("非法的 thinking 值没有被拒绝")

    # ---------- 3. 空正文 + 有思考 token → 自动关思考重试 ----------
    calls: list[dict] = []

    def fake_post(url, payload):  # noqa: ANN001, ARG001
        calls.append(dict(payload))
        if len(calls) == 1:
            # 复现真实观测：预算被思考吃光，正文为空，但状态码是 200
            return ChatResult(content="", completion_tokens=5125, reasoning_tokens=5120)
        return ChatResult(content='{"ok":1}', completion_tokens=793, reasoning_tokens=0)

    auto = client("auto")
    auto._post = fake_post  # type: ignore[method-assign]
    notes: list[str] = []
    out = auto.chat(msgs, json_mode=True, note=notes.append)
    assert out == '{"ok":1}', out
    assert len(calls) == 2, f"应该恰好重试一次，实际调用 {len(calls)} 次"
    assert THINKING_PARAM not in calls[0], "第一次不该带思考参数（auto）"
    assert calls[1].get(THINKING_PARAM) is False, "重试时必须显式关闭思考"
    assert notes and "思考" in notes[0], f"降级没有上报给界面：{notes}"

    # ---------- 4. 已经是 off 时不重试，直接报可照做的错 ----------
    off = client("off")
    off._post = lambda url, payload: ChatResult("", 100, 50)  # type: ignore[method-assign]
    try:
        off.chat(msgs)
    except LlmError as exc:
        assert "thinking" in str(exc) or "思考" in str(exc), exc
    else:
        raise AssertionError("关掉思考后仍返回空，应该报错而不是静默返回空串")

    # ---------- 5. 超时要给建议，不能只抛 RequestsTimeout ----------
    class _TimeoutSession:
        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            raise requests.Timeout("simulated")

    slow = client("auto")
    slow.session = _TimeoutSession()  # type: ignore[assignment]
    try:
        slow.chat(msgs)
    except LlmError as exc:
        text = str(exc)
        assert "off" in text, f"超时错误里没给出 thinking=off 的建议：{text}"
        assert "timeout_seconds" in text, f"超时错误里没提示可调超时：{text}"
    else:
        raise AssertionError("超时应该转成可照做的 LlmError")

    return "参数策略 / 枚举校验 / 空正文自动降级 / 超时提示，全部符合预期"


def _roles_registry() -> str:
    """角色注册表的不变量。

    这些不变量坏了不会报错，只会静默降级——比如角色提示词里漏了字段说明，
    模型就自己编一套；或者 OUTPUT_CONTRACT 没接上，模型就真的去打印阶段 1/2/3，
    然后 JSON 解析失败。所以必须显式钉住。
    """
    from bili_scribe.core import roles

    assert len(roles.ALL_ROLES) == 7, [r.key for r in roles.ALL_ROLES]
    assert roles.STANDARD.key == "standard" and roles.STANDARD.passes == 1, (
        "standard 必须是一轮回、且不带自己的提示词，否则默认行为会改变"
    )
    assert not roles.STANDARD.system, "standard 不该有 system，它要走流水线原本的提示词"

    keys = set()
    for role in roles.ALL_ROLES:
        assert role.key and role.name and role.tagline, role.key
        assert role.key not in keys, f"角色 key 重复：{role.key}"
        keys.add(role.key)
        if role.key != "standard":
            assert role.passes == 3, f"{role.key} 应该是三轮回"
            assert role.critique and role.revise, f"{role.key} 缺批判或修订提示词"
            # 修订提示词必须把批判插进去，否则第三轮等于把第一轮重跑一遍
            assert "{critique}" in role.revise, f"{role.key} 的 revise 里没有 {{critique}} 占位符"
            assert len(role.system) > 500, f"{role.key} 的提示词太短，方法论大概没写进去"

    # OUTPUT_CONTRACT 必须显式否掉"打印思考过程"，否则原文那套会击穿 JSON 解析
    assert "不要代码块包裹" in roles.OUTPUT_CONTRACT
    assert "禁止输出任何思考过程" in roles.OUTPUT_CONTRACT
    for token in ("one_line", "summary_md", "key_points", "timeline", "caution", "comment_short"):
        assert token in roles.OUTPUT_CONTRACT, f"输出契约缺字段：{token}"

    # 底线条款必须每个角色都有
    for role in roles.ALL_ROLES:
        if role.key == "standard":
            continue
        assert "素材未覆盖" in role.system, f"{role.key} 丢了「素材未覆盖」条款"
        assert "时间戳" in role.system, f"{role.key} 丢了时间戳约束"

    assert roles.get_role("bogus").key == "standard", "未知角色必须安全回退"
    assert roles.get_role(None).key == "standard"
    described = roles.describe_roles()
    assert len(described) == 7 and all("key" in d and "name" in d for d in described)
    return f"{len(roles.ALL_ROLES)} 个角色，6 个三轮回，输出契约与底线条款齐备"


def _publish_document() -> str:
    """发布版长文与 Delta 转换。

    用户明确要求：发到评论区的不是概括版，是完整长文（加粗开头 + 分节正文 +
    要点 + 时间轴）。而长文普遍超过普通评论 1000 字上限，所以它走笔记通道，
    正文需要 Quill Delta JSON——转换错了会直接 -400 或把 Markdown 标记发出去。
    """
    import json as _json

    from bili_scribe.core import composer
    from bili_scribe.core.summarizer import Summary

    s = Summary(
        one_line="中心命题",
        summary_md="## 第一节\n内容A\n\n## 第二节\n内容B",
        key_points=["要点1", "要点2"],
        timeline=[{"t": "01:02", "text": "事件"}],
        caution="局限",
        comment_short="短版",
    )
    doc = composer.build_publish_document(s)
    assert doc.startswith("**中心命题**"), doc[:40]
    assert "## 要点" in doc and "## 时间轴" in doc
    # 这些是给操作者看的工作信息，不该发出去
    for leak in ("素材局限", "采集提示", "平台侧摘要", "原视频："):
        assert leak not in doc, f"发布版里混进了 {leak}"
    assert len(doc) > len(s.comment_short), "发布版不该比概括版还短"

    delta = composer.markdown_to_delta(doc)
    ops = _json.loads(delta)
    assert isinstance(ops, list) and ops, "Delta 为空"
    assert ops[-1].get("insert") == "\n" or str(ops[-1].get("insert", "")).endswith("\n"), (
        "Quill 约定正文以换行结尾"
    )
    # 只用验证过的属性（bold）。header/list 没验证过，赌它会 -400。
    for op in ops:
        unknown = set(op.get("attributes") or {}) - {"bold"}
        assert not unknown, f"Delta 用了未验证的属性：{unknown}"
    assert any((op.get("attributes") or {}).get("bold") for op in ops), "加粗丢了"

    plain = "".join(str(op.get("insert", "")) for op in ops)
    for token in ("第一节", "第二节", "事件", "01:02", "要点1"):
        assert token in plain, f"转换后丢了 {token}"
    assert "##" not in plain, "Markdown 的 ## 没被处理，会原样发出去"

    empty = _json.loads(composer.markdown_to_delta(""))
    assert empty == [{"insert": "\n"}], empty
    return f"文档 {len(doc)} 字符 → Delta {len(ops)} 片段，仅用 bold 属性"


check("模块导入", _imports)
check("配置装载与脱敏", _config)
check("缺省配置可用", _config_without_file)
check("配置拒绝未知键", _config_rejects_unknown)
check("WBI 签名", _wbi)
check("解析器 URL 拼装", _resolver_url)
check("评论组装与裁剪", _composer)
check("summary 容错与分块上限", _summarizer)
check("字幕解析与时间窗聚合", _subtitle_merge)
check("存储往返与凭据加密", _store)
check("发布闸门", _publisher_gates)
check("二维码缺依赖时的提示", _qr_missing_message)
check("敏感文件被排除", _sensitive_files_excluded)
check("思考模型输出预算与自动降级", _llm_thinking)
check("角色注册表", _roles_registry)
check("发布长文与 Delta 转换", _publish_document)


# ---------------------------------------------------------------- HTTP 探测


def _http_probes() -> None:
    import requests
    import uvicorn

    from bili_scribe.app.main import create_app
    from bili_scribe.config import AppConfig

    port = 8899
    cfg = AppConfig.load(PROJECT / "config.yaml" if (PROJECT / "config.yaml").exists() else None)
    cfg.server.port = port
    app = create_app(cfg)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.2)
    assert server.started, "uvicorn 未能启动"

    base = f"http://127.0.0.1:{port}"
    probes = [
        ("GET", "/", 15, 200),
        ("GET", "/api/config", 10, 200),
        ("GET", "/api/auth/status", 25, 200),
        ("GET", "/api/tasks", 10, 200),
        ("GET", "/api/publishes", 10, 200),
        ("GET", "/api/llm/check", 40, 200),
        ("GET", "/api/auth/qrcode", 40, 200),
        ("GET", "/api/task/nonexistent", 10, 404),
        ("POST", "/api/publish", 10, 422),
    ]
    for method, path, timeout, expected in probes:
        try:
            if method == "GET":
                resp = requests.get(base + path, timeout=timeout)
            else:
                resp = requests.post(base + path, json={}, timeout=timeout)
            report["http"].append(
                {
                    "path": f"{method} {path}",
                    "status": resp.status_code,
                    "expected": expected,
                    "ok": resp.status_code == expected,
                    "body": resp.text.replace("\n", " ")[:200],
                }
            )
        except Exception as exc:  # noqa: BLE001
            report["http"].append(
                {
                    "path": f"{method} {path}",
                    "status": "EXC",
                    "expected": expected,
                    "ok": False,
                    "body": f"{type(exc).__name__}: {exc}",
                }
            )

    # —— 行为回归检查 ——
    # 下面三项对应真实修过的缺陷。没有它们，同一个坑会再踩一次。

    def _config_features() -> str:
        data = requests.get(base + "/api/config", timeout=10).json()
        features = data.get("features")
        assert isinstance(features, dict), f"config 缺少 features 字段：{list(data)}"
        assert isinstance(features.get("qr_login"), bool), features
        return f"features.qr_login={features['qr_login']}"

    def _qr_missing_returns_503() -> str:
        import sys

        saved = sys.modules.get("qrcode")
        sys.modules["qrcode"] = None
        try:
            resp = requests.get(base + "/api/auth/qrcode", timeout=40)
        finally:
            if saved is None:
                sys.modules.pop("qrcode", None)
            else:
                sys.modules["qrcode"] = saved

        assert resp.status_code == 503, f"期望 503，实际 {resp.status_code}：{resp.text[:200]}"
        detail = resp.json().get("detail", "")
        assert "qrcode[pil]" in detail, f"提示里没有修复命令：{detail}"
        return f"503 + {detail[:56]}…"

    def _unhandled_returns_json() -> str:
        from bili_scribe.infra import bili_client

        original = bili_client.BiliClient.qr_generate

        def boom(self):  # noqa: ANN001, ARG001
            raise ValueError("boom-仅用于测试")

        bili_client.BiliClient.qr_generate = boom
        try:
            resp = requests.get(base + "/api/auth/qrcode", timeout=20)
        finally:
            bili_client.BiliClient.qr_generate = original

        assert resp.status_code == 500, f"期望 500，实际 {resp.status_code}"
        detail = resp.json().get("detail", "")  # 必须是 JSON，不能是空正文
        assert "boom-仅用于测试" in detail, f"响应里没有带出异常原因：{detail}"
        return f"500 + 带出原因：{detail[:56]}…"

    for name, fn in (
        ("config 暴露能力开关", _config_features),
        ("缺 qrcode 时返回 503 与修复提示", _qr_missing_returns_503),
        ("未预期异常也返回 JSON detail", _unhandled_returns_json),
    ):
        try:
            report["http"].append(
                {"path": name, "status": "OK", "expected": None, "ok": True, "body": fn()}
            )
        except Exception as exc:  # noqa: BLE001
            report["http"].append(
                {
                    "path": name,
                    "status": "FAIL",
                    "expected": None,
                    "ok": False,
                    "body": f"{type(exc).__name__}: {exc}",
                }
            )

    server.should_exit = True


# ------------------------------------------------------------ 设置接口


def _settings_api() -> None:
    """设置页的读写链路。

    全程在临时目录里跑，并在结尾断言项目的 config.yaml 一个字节都没变——
    这个测试会真的写配置文件，写错地方就会把你的真实配置覆盖掉。
    """
    import copy
    import shutil
    import tempfile

    import requests
    import uvicorn

    from bili_scribe.app.main import create_app
    from bili_scribe.config import AppConfig

    real_config = PROJECT / "config.yaml"
    real_before = real_config.read_bytes() if real_config.exists() else None

    tmp = Path(tempfile.mkdtemp(prefix="bs-settings-"))
    cfg_path = tmp / "config.yaml"
    shutil.copy2(PROJECT / "config.example.yaml", cfg_path)

    port = 8898
    cfg = AppConfig.load(cfg_path)
    cfg.source_path = str(cfg_path)
    cfg.server.host = "127.0.0.1"
    cfg.server.port = port
    app = create_app(cfg)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.2)

    base = f"http://127.0.0.1:{port}"

    def run(name: str, fn) -> None:
        try:
            report["http"].append(
                {"path": name, "status": "OK", "expected": None, "ok": True, "body": fn()}
            )
        except Exception as exc:  # noqa: BLE001
            report["http"].append(
                {
                    "path": name,
                    "status": "FAIL",
                    "expected": None,
                    "ok": False,
                    "body": f"{type(exc).__name__}: {exc}",
                }
            )

    try:
        assert server.started, "设置接口测试的 uvicorn 未能启动"

        def _get_shape() -> str:
            data = requests.get(base + "/api/settings", timeout=15).json()
            assert "values" in data and "path" in data, list(data)
            assert data["platform_comment_max"] == 1000, data["platform_comment_max"]
            assert data["restart_needed_for"] == ["server"], data["restart_needed_for"]
            assert data["gate_disabled"] is False, data["gate_disabled"]
            for section in ("server", "llm", "collect", "publish", "storage"):
                assert section in data["values"], f"缺少 {section} 段"
            # 主页面每次都打 /api/config，密钥绝不能出现在那里
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert "api_key" not in pub["llm"], "公开展示接口泄露了 api_key"
            assert "source_path" not in pub, "公开展示接口泄露了 source_path"
            return "五段齐备，/api/config 不含 api_key"

        run("GET /api/settings 结构", _get_shape)

        payload = requests.get(base + "/api/settings", timeout=15).json()["values"]
        payload["confirm_disable_gate"] = False
        payload["llm"]["model"] = "smoke-test-model"
        payload["llm"]["temperature"] = 0.7
        payload["llm"]["api_key"] = "sk-smoke-1234567890"
        payload["publish"]["min_interval_seconds"] = 42

        def _put_roundtrip() -> str:
            r = requests.put(base + "/api/settings", json=payload, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            body = r.json()
            assert body["ok"] is True, body
            assert body["written"]["backup"], "没有生成备份文件"
            assert Path(body["written"]["path"]) == cfg_path, body["written"]["path"]
            assert Path(body["written"]["backup"]).exists(), "备份文件不存在"
            # 报告的字节数必须等于磁盘上的真实大小。
            # 曾经不一致：write_text 在 Windows 上把 \n 翻成 \r\n，每行多 1 字节。
            on_disk = cfg_path.stat().st_size
            assert body["written"]["bytes"] == on_disk, (
                f"报告 {body['written']['bytes']} 字节，磁盘实际 {on_disk} 字节"
            )
            after = requests.get(base + "/api/settings", timeout=15).json()["values"]
            assert after["llm"]["model"] == "smoke-test-model", after["llm"]["model"]
            assert after["llm"]["temperature"] == 0.7
            assert after["publish"]["min_interval_seconds"] == 42
            return f"PUT 后回读一致，{on_disk} 字节，备份已生成"

        run("PUT /api/settings 往返", _put_roundtrip)

        def _no_crlf() -> str:
            raw = cfg_path.read_bytes()
            assert b"\r\n" not in raw, "回写用了 CRLF，与随包的 config.example.yaml（LF）不一致"
            assert raw.count(b"\n") > 10, "回写内容行数异常"
            return f"纯 LF，{raw.count(chr(10).encode())} 行"

        run("回写行尾为 LF（与示例一致）", _no_crlf)

        def _save_idempotent() -> str:
            # 原样再存一次。手写渲染器最容易在这里出问题：
            # 每保存一次文件就长大一点，或者某项被反复重置。
            first = cfg_path.stat().st_size
            before_text = cfg_path.read_text(encoding="utf-8")
            again = requests.get(base + "/api/settings", timeout=15).json()["values"]
            again["confirm_disable_gate"] = False
            r = requests.put(base + "/api/settings", json=again, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            after_text = cfg_path.read_text(encoding="utf-8")
            assert after_text == before_text, (
                f"重复保存产生漂移：{first} → {cfg_path.stat().st_size} 字节"
            )
            return f"重复保存字节完全不变（{first} 字节）"

        run("重复保存不漂移", _save_idempotent)

        def _hot_reload() -> str:
            # 保存必须立刻生效，不能等重启。所有组件共享同一个 cfg 引用，
            # 如果 apply_settings 是"换对象"而不是"原地改"，这里就会读到旧值。
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["llm"]["model"] == "smoke-test-model", pub["llm"]["model"]
            return "/api/config 已是新值，无需重启"

        run("设置热生效（不重启）", _hot_reload)

        def _file_reloadable() -> str:
            text = cfg_path.read_text(encoding="utf-8")
            assert "#" in text, "回写后注释丢了"
            reloaded = AppConfig.load(cfg_path)
            assert reloaded.llm.model == "smoke-test-model", reloaded.llm.model
            assert reloaded.publish.min_interval_seconds == 42
            assert reloaded.llm.api_key == "sk-smoke-1234567890"
            assert reloaded.collect.prefer_subtitle_lang, "字幕语言被清空了"
            return f"{len(text.encode('utf-8'))} 字节，注释保留且可重新装载"

        run("回写文件带注释且可重载", _file_reloadable)

        def _reject_overlap() -> str:
            bad = copy.deepcopy(payload)
            bad["llm"]["chunk_size"] = 1000
            bad["llm"]["chunk_overlap"] = 1000
            r = requests.put(base + "/api/settings", json=bad, timeout=15)
            assert r.status_code == 400, f"期望 400，实际 {r.status_code}"
            assert "chunk_overlap" in r.json()["detail"], r.json()
            return "400 · " + r.json()["detail"][:36]

        run("拒绝 overlap >= size", _reject_overlap)

        def _reject_comment_limit() -> str:
            bad = copy.deepcopy(payload)
            bad["publish"]["max_comment_chars"] = 1001
            r = requests.put(base + "/api/settings", json=bad, timeout=15)
            assert r.status_code in (400, 422), f"期望 400/422，实际 {r.status_code}"
            return f"{r.status_code} · 超过平台上限被拒绝"

        run("拒绝超平台评论上限", _reject_comment_limit)

        def _gate_needs_ack() -> str:
            bad = copy.deepcopy(payload)
            bad["publish"]["require_confirm"] = False
            bad["confirm_disable_gate"] = False
            r = requests.put(base + "/api/settings", json=bad, timeout=15)
            assert r.status_code == 400, f"期望 400，实际 {r.status_code}"
            assert "确认" in r.json()["detail"], r.json()
            still = requests.get(base + "/api/settings", timeout=15).json()
            assert still["gate_disabled"] is False, "闸门被悄悄关掉了"
            return "400 + 闸门保持开启"

        run("关闸必须显式确认（服务端也拦）", _gate_needs_ack)

        def _gate_can_be_disabled() -> str:
            bad = copy.deepcopy(payload)
            bad["publish"]["require_confirm"] = False
            bad["confirm_disable_gate"] = True
            r = requests.put(base + "/api/settings", json=bad, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            assert r.json()["gate_disabled"] is True, r.json()
            assert requests.get(base + "/api/settings", timeout=15).json()["gate_disabled"] is True
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["publish"]["require_confirm"] is False
            return "确认后关闭，状态同步到 /api/config"

        run("确认后可关闭闸门", _gate_can_be_disabled)

        def _untouched() -> str:
            now = real_config.read_bytes() if real_config.exists() else None
            assert now == real_before, "测试改动了项目里的 config.yaml"
            return "项目 config.yaml 未被触碰"

        run("未触碰项目 config.yaml", _untouched)

    finally:
        server.should_exit = True
        shutil.rmtree(tmp, ignore_errors=True)


try:
    _http_probes()
except Exception:  # noqa: BLE001
    report["errors"].append(traceback.format_exc())

try:
    _settings_api()
except Exception:  # noqa: BLE001
    report["errors"].append(traceback.format_exc())


# ------------------------------------------------------------------ 汇总

offline_failed = [c for c in report["offline"] if not c["ok"]]
http_failed = [c for c in report["http"] if not c.get("ok")]

print("=" * 66)
print("bili-scribe 冒烟测试")
print("=" * 66)
for item in report["offline"]:
    print(f"[{'OK ' if item['ok'] else 'FAIL'}] {item['name']}：{item['detail']}")
print("-" * 66)
for item in report["http"]:
    flag = "OK " if item.get("ok") else "FAIL"
    if item.get("expected") is not None:
        tail = f"{item['path']} → {item['status']}（期望 {item['expected']}）"
    else:
        tail = item["path"]
    print(f"[{flag}] {tail}：{str(item.get('body', ''))[:110]}")
print("-" * 66)
print(f"离线 {len(report['offline']) - len(offline_failed)}/{len(report['offline'])} 通过，"
      f"HTTP {len(report['http']) - len(http_failed)}/{len(report['http'])} 通过")
for err in report["errors"]:
    print(err)
print("=" * 66)

sys.exit(1 if (offline_failed or http_failed or report["errors"]) else 0)
