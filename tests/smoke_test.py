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
import os
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

    # 发布记录必须能按视频查。只回「全局最近 N 条」再让调用方自己筛，老视频的
    # 记录会被后来别的视频挤出窗口——界面上就是「发布记录一片空白，撤回按钮没了」。
    other_task = st.create_task("BV1other0001")
    st.add_publish(other_task, "BV1other0001", 1, "888", "别的视频的内容", "published")
    mine = st.list_publishes(limit=12, bvid="BV1xx411c7mD")
    assert [r["id"] for r in mine] == [pid], f"按 bvid 过滤失败：{mine}"
    assert all(r["bvid"] == "BV1xx411c7mD" for r in mine), "结果里混进了别的视频"
    assert len(st.list_publishes(limit=12)) == 2, "不带 bvid 时应当返回全局记录"

    st.mark_publish_deleted(pid)
    assert st.find_live_publish("BV1xx411c7mD") is None, "撤回后去重未解除"
    return "凭据加密落盘，任务/去重/撤回往返均正常，发布记录可按视频过滤"


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


def _llm_proxy() -> str:
    """代理只能来自 llm.proxy，绝不来自环境变量。

    这是一个真实事故的回归用例：服务从一个设了 HTTP_PROXY 的 shell 里启动，
    requests 默认 trust_env=True 就把所有模型请求塞进了那个代理，
    界面报 "ProxyError / Tunnel connection failed: 502"——看起来像模型服务挂了，
    其实请求压根没出去。所以必须钉死两件事：
      1) 客户端不读环境变量；2) 代理地址写错在加载配置时就被拒绝。
    """
    import os
    import tempfile

    import requests

    from bili_scribe.config import PROXY_SCHEMES, AppConfig, LlmConfig
    from bili_scribe.infra.llm import LlmClient, LlmError

    msgs = [{"role": "user", "content": "hi"}]
    url = "https://api.example.com/v1/chat/completions"

    def make(proxy: str = "") -> LlmClient:
        return LlmClient(
            LlmConfig(
                api_key="k",
                model="m",
                base_url="https://api.example.com/v1",
                proxy=proxy,
            )
        )

    # ---------- 1. 不配代理时：trust_env 关闭，proxies 为空 ----------
    plain = make()
    assert plain.session.trust_env is False, "trust_env 没关：会读环境变量里的代理"
    assert plain.proxies is None, plain.proxies
    assert "直连" in plain.describe(), plain.describe()

    # ---------- 2. 配了代理时：显式带上 ----------
    proxied = make("http://127.0.0.1:7897")
    assert proxied.proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }, proxied.proxies
    assert "127.0.0.1:7897" in proxied.describe(), proxied.describe()

    # ---------- 3. 环境变量不能漏进来（这条是核心） ----------
    # 用 requests 自己的解析函数取证：它就是真正决定"这次请求走哪个代理"的那一步。
    saved = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    try:
        for k in saved:
            os.environ[k] = "http://127.0.0.1:55159"
        plain2 = make()
        merged = plain2.session.merge_environment_settings(url, dict(plain2.proxies or {}), None, None, None)
        assert not merged.get("proxies"), (
            f"环境变量里的代理漏进了请求：{merged.get('proxies')}——"
            "这正是那个 502 事故的成因"
        )
        # 对照组：trust_env=True 时确实会读到，说明上面那条不是因为用例写错了
        leaky = requests.Session()
        leaky.trust_env = True
        control = leaky.merge_environment_settings(url, {}, None, None, None)
        assert control.get("proxies"), "对照组都没读到代理，说明这个取证方法无效"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---------- 4. 代理地址写错：加载配置时就拒绝（不能等到下次启动才发现） ----------
    assert "http" in PROXY_SCHEMES and "socks5" in PROXY_SCHEMES, PROXY_SCHEMES
    for bad_value in ("127.0.0.1:7897", "ftp://x", "随便写的"):
        cfg_file = Path(tempfile.mkdtemp()) / "bad-proxy.yaml"
        cfg_file.write_text(f"llm:\n  proxy: {bad_value!r}\n", encoding="utf-8")
        try:
            AppConfig.load(cfg_file)
        except ValueError as exc:
            assert "proxy" in str(exc), exc
        else:
            raise AssertionError(f"非法代理值没有被拒绝：{bad_value!r}")

    # ---------- 5. 代理失败要说明"这不是模型服务的问题" ----------
    class _ProxyFailSession:
        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            raise requests.exceptions.ProxyError("Tunnel connection failed: 502 Bad Gateway")

    failing = make("http://127.0.0.1:7897")
    failing.session = _ProxyFailSession()  # type: ignore[assignment]
    try:
        failing.chat(msgs)
    except LlmError as exc:
        text = str(exc)
        assert "代理" in text, f"代理失败没被识别出来：{text}"
        assert "llm.proxy" in text, f"没告诉用户去改哪一项：{text}"
        assert "7897" in text, f"没把当前代理地址带出来：{text}"
    else:
        raise AssertionError("代理失败应该转成可照做的 LlmError")

    # ---------- 6. ConnectTimeout 不能被当成"读超时"处理 ----------
    # ConnectTimeout 是 Timeout 的子类。catch 顺序写错的话，这个场景会得到
    # "把 thinking 改成 off"这种和连接无关的建议，反而把人带偏——所以单独立一条。
    class _ConnectFailSession:
        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            raise requests.exceptions.ConnectTimeout("Connection to api.example.com timed out")

    unreachable = make()
    unreachable.session = _ConnectFailSession()  # type: ignore[assignment]
    try:
        unreachable.chat(msgs)
    except LlmError as exc:
        text = str(exc)
        assert "thinking" not in text, f"连接超时不该给思考相关的建议：{text}"
        assert "proxy" in text, f"没提示可能是 DNS 问题、可以走代理：{text}"
        assert "连不上" in text or "超时" in text, text
    else:
        raise AssertionError("连接超时应该转成 LlmError")

    return "trust_env 已关 / 环境变量代理漏不进 / 非法值在加载时被拒 / 代理与连接超时提示可照做"


def _frames_module() -> str:
    """全片抽帧的纯函数与不变量。

    这里出错是**静默的**：挑错帧、裁错格子、或者把"没有时间戳"说成有时间戳，
    都不会抛异常，只会让总结里的画面和时间对不上。所以逐个钉死。
    """
    import tempfile

    from bili_scribe.config import AppConfig, FramesConfig
    from bili_scribe.core.frames import (
        Frame,
        FrameSet,
        as_content_blocks,
        cache_size,
        cell_box,
        clear_cache,
        pick_cells,
        purge_stale,
    )

    # ---------- 1. 挑格子：必须铺满整条视频 ----------
    # 真实数据：18 张拼图 × 10×10 = 1800 格，接口给了 1726 个时间戳
    picks = pick_cells(total_cells=1800, index_len=1726, want=12)
    assert len(picks) == 12, picks
    assert picks[0] == 0, f"必须从第一格开始，实际 {picks[0]}"
    assert picks[-1] == 1725, f"必须在最后一个有时间戳的格子上结束，实际 {picks[-1]}"
    steps = [b - a for a, b in zip(picks, picks[1:])]
    assert max(steps) - min(steps) <= 1, f"间隔不均：{steps}"
    assert picks == sorted(picks) and len(set(picks)) == 12, "必须升序且不重复"

    # 没有时间戳时退化到按格子等距（短一点的视频会出现这种）
    no_ts = pick_cells(total_cells=400, index_len=0, want=6)
    assert no_ts[0] == 0 and no_ts[-1] == 399, no_ts
    assert len(no_ts) == 6, no_ts

    # 格子比想要的数量还少时，只能给这么多，不能崩
    few = pick_cells(total_cells=3, index_len=3, want=12)
    assert len(few) == 3 and few == [0, 1, 2], few
    # 极短视频：取 1 帧
    assert pick_cells(total_cells=1, index_len=1, want=12) == [0]
    assert pick_cells(total_cells=0, index_len=0, want=12) == []
    assert pick_cells(total_cells=100, index_len=100, want=0) == []

    # ---------- 2. 裁格子的坐标：行优先，别裁错位置 ----------
    # 10×10 网格、单格 480×270。第 0 格在左上，第 9 格在第 0 行最右，
    # 第 10 格换到第 1 行开头，第 99 格是这张拼图的右下角。
    assert cell_box(0, 10, 10, 480, 270) == (0, 0, 480, 270), cell_box(0, 10, 10, 480, 270)
    assert cell_box(9, 10, 10, 480, 270) == (4320, 0, 4800, 270), cell_box(9, 10, 10, 480, 270)
    assert cell_box(10, 10, 10, 480, 270) == (0, 270, 480, 540), cell_box(10, 10, 10, 480, 270)
    assert cell_box(99, 10, 10, 480, 270) == (4320, 2430, 4800, 2700), cell_box(99, 10, 10, 480, 270)
    # 跨到第二张拼图时，格内下标要回绕（这是最容易写错的一处）
    assert cell_box(100, 10, 10, 480, 270) == (0, 0, 480, 270), cell_box(100, 10, 10, 480, 270)

    # ---------- 3. 拼给模型的内容块：必须成对，且是 data URL ----------
    fs = FrameSet(
        frames=[
            Frame(order=1, timestamp=0, data_url="data:image/jpeg;base64,AAA", size=3),
            Frame(order=2, timestamp=754, data_url="data:image/jpeg;base64,BBB", size=3),
        ],
        total_cells=1800,
        sprites=18,
        has_timestamps=True,
    )
    blocks = as_content_blocks(fs)
    assert blocks and blocks[0]["type"] == "text", blocks[:1]
    assert "00:00:00" in blocks[0]["text"] and "00:12:34" in blocks[0]["text"], blocks[0]["text"]
    kinds = [b["type"] for b in blocks[1:]]
    assert kinds == ["text", "image_url", "text", "image_url"], kinds
    assert all(
        b["image_url"]["url"].startswith("data:image/jpeg;base64,")
        for b in blocks if b["type"] == "image_url"
    )
    # 必须明确"不要逐帧描述"——实测不然会烧掉大量输出 token
    assert "不要逐帧描述" in blocks[0]["text"], blocks[0]["text"]
    assert as_content_blocks(FrameSet()) == [], "空帧集不该产出任何内容块"

    # ---------- 4. 没有时间戳时必须如实说，不能编时刻 ----------
    blind = FrameSet(
        frames=[Frame(order=1, timestamp=None, data_url="data:image/jpeg;base64,AAA", size=3)],
        total_cells=400,
        sprites=4,
        has_timestamps=False,
    )
    note = blind.timeline_note()
    assert "没有" in note or "未提供" in note, note
    assert "不要" in note and "编造" in note, note
    assert "00:00" not in note, f"没有时间戳却出现了时刻：{note}"
    assert blind.frames[0].label == "第 1 帧", blind.frames[0].label
    assert as_content_blocks(blind)[0]["text"].count("00:") == 0

    # ---------- 5. 配置校验：写错的值必须当场挡掉 ----------
    def cfg_with(frames_yaml: str):
        p = Path(tempfile.mkdtemp()) / "c.yaml"
        p.write_text(f"collect:\n  frames:\n{frames_yaml}", encoding="utf-8")
        return AppConfig.load(p)

    for bad, why in (
        ("    max_frames: -1\n", "负的张数"),
        ("    quality: 0\n", "质量为 0"),
        ("    quality: 101\n", "质量超过 100"),
    ):
        try:
            cfg_with(bad)
        except ValueError as exc:
            assert "frames" in str(exc), f"{why} 的报错信息里没提到 frames：{exc}"
        else:
            raise AssertionError(f"{why} 没有被拒绝")

    ok = cfg_with("    enabled: false\n    max_frames: 24\n    quality: 70\n").collect.frames
    assert ok.enabled is False and ok.max_frames == 24 and ok.quality == 70, ok
    # 默认值：默认开、上限 12（这是"默认开 + 加上限"那个决定）
    dflt = FramesConfig()
    assert dflt.enabled is True and dflt.max_frames == 12, dflt

    # ---------- 6. 缓存目录挂在 data home 下，且能清、能算大小 ----------
    # 目录名是 `<aid>-<随机>`：一次采集一个独占目录（见 _new_cache_dir 里的说明）
    tmp_db = str(Path(tempfile.mkdtemp()) / "t.db")
    assert cache_size(tmp_db) == 0, "一开始应当是空的"
    d = Path(tmp_db).parent / "frames" / "123-1a2b3c4d"
    d.mkdir(parents=True)
    (d / "1.jpg").write_bytes(b"x" * 100)
    assert cache_size(tmp_db) == 100, cache_size(tmp_db)
    assert clear_cache(tmp_db, 123) == 1, "应当删掉 1 个文件"
    assert clear_cache(tmp_db, 999) == 0, "不存在的视频应当返回 0，而不是报错"
    assert cache_size(tmp_db) == 0, "清完之后不该还有占用"

    # ---------- 7. purge_stale：只清过期的，不动正在用的 ----------
    # 这是"采集自己收尾"之外的兜底：进程被杀／崩溃时根本没机会走到清理，
    # 那些残骸必须在下一次启动时被收走，否则别人的画面会永久留在磁盘上。
    d2 = Path(tmp_db).parent / "frames" / "456-deadbeef"
    d2.mkdir(parents=True)
    (d2 / "2.jpg").write_bytes(b"y" * 10)
    assert purge_stale(tmp_db, max_age_seconds=3600) == 0, "刚写下的缓存被当成残留清掉了"
    assert (d2 / "2.jpg").exists(), "新鲜的缓存在 purge 里被误删"
    old = time.time() - 3 * 3600
    os.utime(d2, (old, old))
    assert purge_stale(tmp_db, max_age_seconds=3600) == 1, "过期的残留没被清掉"
    assert not d2.exists(), "过期目录还留在磁盘上"
    assert cache_size(tmp_db) == 0, "purge 之后应当归零"

    # 名字解析不出 aid 的目录不是我们建的，一律不碰（max_age 给 0，按时间早该清了）
    decoy = Path(tmp_db).parent / "frames" / "别人的目录"
    decoy.mkdir(parents=True)
    (decoy / "x.jpg").write_bytes(b"z" * 7)
    assert purge_stale(tmp_db, max_age_seconds=0) == 0, "purge 动了不属于自己的目录"
    assert decoy.exists(), "purge 删掉了不属于自己的目录"

    return (
        "挑帧铺满全片 / 裁格行优先且跨图回绕正确 / 内容块成对且为 data URL / "
        "无时间戳如实说明 / 配置非法值被拒 / 缓存可算可清 / purge 只清过期残留且不碰别人的目录"
    )


def _frames_own_their_dir() -> str:
    """画面文件的生死：采集自己收尾；同一个视频的两次采集互不干扰。

    这条守的是一个**真踩过的竞争**，它长得像偶发网络问题：

        主任务采集完、写库说 ready（但它的 finally 还没跑到）
        迭代紧接着开始采集（同一个 aid）
        主任务的 finally 这时才删掉 `frames/<aid>` → 迭代写到一半 FileNotFoundError

    复现率约 1/6（脚本见项目根的 `refine-debug-race.py`）。根因是**两次采集共用了
    按 aid 命名的同一个目录**，清理者无从知道自己删的是不是别人正在写的东西。
    所以这里钉两件事：采完（无论成败）不留残骸；同一 aid 并发采集互不干扰。
    """
    import io as _io
    import tempfile
    import threading
    import time

    from PIL import Image

    from bili_scribe.config import FramesConfig
    from bili_scribe.core import frames as fm
    from bili_scribe.infra.bili_client import BiliError

    tmp_db = str(Path(tempfile.mkdtemp()) / "t.db")

    def sprite(seed: int) -> bytes:
        buf = _io.BytesIO()
        Image.new("RGB", (96, 54), (30 * seed % 255, 60, 90)).save(buf, "JPEG", quality=70)
        return buf.getvalue()

    class FakeClient:
        """假 B站：两张 2×2 拼图，单格 48×27。可注入下载延迟与失败。"""

        def __init__(self, delay: float = 0.0, fail_on: int = 0):
            self.delay = delay
            self.fail_on = fail_on  # 第几次下载时抛错（从 1 起算；0＝不抛）
            self.calls = 0

        def get_raw(self, url, params=None):  # noqa: ANN001, ARG002
            return {
                "code": 0,
                "data": {
                    "image": ["http://x/1.jpg", "http://x/2.jpg"],
                    "index": [0, 10, 20, 30, 40, 50, 60, 70],
                    "img_x_len": 2,
                    "img_y_len": 2,
                    "img_x_size": 48,
                    "img_y_size": 27,
                },
            }

        def get_bytes(self, url):  # noqa: ANN001, ARG002
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            if self.fail_on and self.calls == self.fail_on:
                raise BiliError(-404, "假的下载失败")
            return sprite(self.calls)

    cfg = FramesConfig(enabled=True, max_frames=4, quality=70)

    # ---------- 1. 成功：采完磁盘上不该留下任何帧文件 ----------
    out = fm.collect_frames(FakeClient(), cfg, 555, tmp_db)
    assert len(out.frames) == 4, f"应当取到 4 帧，实际 {len(out.frames)}"
    assert fm.cache_size(tmp_db) == 0, f"采集完还有残留：{fm.cache_size(tmp_db)} 字节"
    assert not (Path(tmp_db).parent / "frames").exists(), "空目录壳也该一并收掉"

    # ---------- 2. 失败：中途下载失败同样不能留残骸 ----------
    try:
        fm.collect_frames(FakeClient(fail_on=2), cfg, 555, tmp_db)
    except fm.FrameError as exc:
        assert "假的下载失败" in str(exc), exc
    else:
        raise AssertionError("下载失败应当抛 FrameError（调用方据此降级）")
    assert fm.cache_size(tmp_db) == 0, f"失败一次就留下了残骸：{fm.cache_size(tmp_db)} 字节"

    # ---------- 3. 并发：同一视频两次采集，谁也不许动谁的目录 ----------
    # 慢的那次先把目录建起来、卡在下载里；快的那次从开始到收尾整个发生在它中间。
    # 按 aid 共用目录的老实现，这里必然失败——快的那次收尾会把慢的那次正在写的目录删掉。
    errors: list[str] = []
    slow, fast = FakeClient(delay=0.4), FakeClient()

    def run(client, label: str) -> None:
        try:
            got = fm.collect_frames(client, cfg, 777, tmp_db)
            assert len(got.frames) == 4, got
        except Exception as exc:  # noqa: BLE001 - 这里要的是"有没有人被打断"
            errors.append(f"{label}：{type(exc).__name__}: {exc}")

    t_slow = threading.Thread(target=run, args=(slow, "慢的那次"), name="frames-slow")
    t_slow.start()
    time.sleep(0.15)  # 让慢的那次先建好目录、进入下载等待
    t_fast = threading.Thread(target=run, args=(fast, "快的那次"), name="frames-fast")
    t_fast.start()
    t_fast.join(30)
    t_slow.join(30)
    assert not errors, f"并发采集互相踩了：{errors}"
    assert fm.cache_size(tmp_db) == 0, "并发跑完还有残留"
    assert not (Path(tmp_db).parent / "frames").exists(), "并发跑完还留着空壳目录"

    return (
        "采完即清（成功、失败都清，连空壳目录一起收）；"
        "同一视频并发采集互不干扰（每次采集独占目录）"
    )


def _frames_wired() -> str:
    """画面必须真的进到**发出去的那次请求**里，且只进那一次。

    这是"写了接线"和"接线对了"的区别。summarize 里拼好了 blocks 却没传下去、
    或者传给了错误的那一轮，都不会报错——总结照常生成，只是没有画面，
    而且从产出上完全看不出来。所以这里直接抓请求体断言，不信任何中间变量。
    """
    import types

    from bili_scribe.config import AppConfig
    from bili_scribe.core import summarizer
    from bili_scribe.core.collector import Bundle
    from bili_scribe.core.frames import Frame, FrameSet

    bundle = Bundle(
        bvid="BV1smoke",
        aid=1,
        cid=2,
        page=1,
        page_count=1,
        title="测试稿件",
        desc="",
        up_name="某人",
        up_mid=9,
        duration=600,
        tname="知识",
        source_kind="subtitle_ai",
        source_label="平台 AI 字幕",
        source_trust="中",
        source_detail="717 段",
        body="这是一段用来测试的素材正文，长度不足以触发分块。" * 3,
    )
    frameset = FrameSet(
        frames=[
            Frame(order=1, timestamp=0, data_url="data:image/jpeg;base64,AAA", size=3),
            Frame(order=2, timestamp=300, data_url="data:image/jpeg;base64,BBB", size=3),
        ],
        total_cells=200,
        sprites=2,
        has_timestamps=True,
    )

    seen: list[list[dict[str, Any]]] = []
    reply = (
        '{"one_line":"一句话结论","summary_md":"正文","key_points":["要点"],'
        '"timeline":[{"t":"00:00","text":"开场"}],"caution":"","comment_short":"短评"}'
    )

    class FakeLlm:
        cfg = types.SimpleNamespace(model="fake-model")

        def chat(self, messages, **_kwargs):
            seen.append(messages)
            return reply

    cfg = AppConfig()
    stages: list[str] = []
    out = summarizer.summarize(
        bundle,
        FakeLlm(),
        cfg,
        progress=lambda stage, _msg: stages.append(stage),
        role_key="standard",
        frameset=frameset,
    )

    assert len(seen) == 1, f"standard 角色应当只调用一次，实际 {len(seen)} 次"
    content = seen[0][1]["content"]
    assert isinstance(content, list), "带帧时 user content 必须是内容块数组，实际还是字符串"
    kinds = [c["type"] for c in content]
    assert kinds[0] == "text", f"第一个块必须是正文：{kinds[:2]}"
    assert kinds.count("image_url") == 2, f"应当有 2 张图，实际 {kinds.count('image_url')}：{kinds}"
    images = [c for c in content if c["type"] == "image_url"]
    assert all(
        c["image_url"]["url"].startswith("data:image/jpeg;base64,") for c in images
    ), images
    assert out.frames == 2, f"summary 要记下这次附了几帧，实际 {out.frames}"
    assert "reducing" in stages, stages

    # 不带帧时行为必须与加这个功能之前**完全一致**（content 还是字符串）
    seen.clear()
    out2 = summarizer.summarize(bundle, FakeLlm(), cfg, role_key="standard")
    assert isinstance(seen[0][1]["content"], str), "无帧路径被改成了数组，属于行为回归"
    assert out2.frames == 0, out2.frames

    # 三轮回：帧只能进归约那一次，批判与修订都不能带——
    # 否则同一批图会被反复计费，而它们的作用在归约时已经用完。
    seen.clear()
    out3 = summarizer.summarize(bundle, FakeLlm(), cfg, role_key="xueshu", frameset=frameset)
    assert len(seen) == 3, f"三轮回应当是 3 次调用，实际 {len(seen)} 次"
    assert isinstance(seen[0][1]["content"], list), "归约那一次必须带帧"
    assert isinstance(seen[1][1]["content"], str), "自我批判那一次不该带帧"
    assert isinstance(seen[2][1]["content"], str), "修订那一次不该带帧"
    assert out3.frames == 2, out3.frames

    # 空帧集（采集拿不到图）不能把请求变成空数组，必须安全退回纯文本
    seen.clear()
    out4 = summarizer.summarize(
        bundle, FakeLlm(), cfg, role_key="standard", frameset=FrameSet()
    )
    assert isinstance(seen[0][1]["content"], str), "空帧集不该把 content 变成数组"
    assert out4.frames == 0, out4.frames

    return "帧只进归约那一次（standard 1 次 / 三轮回 3 次中仅第 1 次）/ 无帧路径行为不变 / 空帧集安全退回"


def _user_subtitle() -> str:
    """使用者上传的 srt：解析、接入、对齐校验。

    这条链上有两处**静默失败**最值得盯：

    1. **编码探测**。gb18030 几乎能解出任意字节序列、不抛异常，所以"第一个解码
       成功的就是对的"这个假设是错的——它会把一个 UTF-8 文件解成乱码还"成功"返回。
       所以探测必须再验一次"解出来的东西像不像字幕"。
    2. **`has_timestamps` 漏改**。新增 `subtitle_user` 这一种 source_kind 后，
       summarizer 里的白名单不跟着改**不会报错**，只是 timeline 静默变成空数组。
    """
    import types

    from bili_scribe.config import AppConfig, CollectConfig
    from bili_scribe.core import collector, srt, summarizer
    from bili_scribe.infra.endpoints import ARCHIVE_TAGS, PLAYER_V2, VIDEO_VIEW

    text = (
        "1\n00:00:01,000 --> 00:00:04,500\n第一句字幕\n\n"
        "2\n00:00:05,200 --> 00:00:08,100\n第二句字幕\n\n"
    )
    expect = [(1.0, "第一句字幕"), (5.2, "第二句字幕")]

    # ---------- 1. 常见编码都要能读出同样的结果，且回显的编码名要准确 ----------
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "big5"):
        items, got_enc = srt.parse_bytes(text.encode(enc))
        assert items == expect, f"{enc} 解出来不对：{items}（识别为 {got_enc}）"
        assert got_enc == enc, f"文件存的是 {enc}，回显却写成 {got_enc}"

    # 编码回显是"出乱码时的唯一线索"，所以不能含糊：有没有 BOM 必须被区分开。
    # utf-8-sig 的解码器对**无 BOM** 的 UTF-8 输入同样有效，会让它跟 utf-8 同分；
    # 若它参与候选打分并靠顺序胜出，纯 UTF-8 文件就会被误报成 utf-8-sig。
    _, no_bom = srt.parse_bytes(text.encode("utf-8"))
    assert no_bom == "utf-8", f"无 BOM 的 UTF-8 被报成了 {no_bom}"
    _, with_bom = srt.parse_bytes(text.encode("utf-8-sig"))
    assert with_bom == "utf-8-sig", f"带 BOM 的 UTF-8 被报成了 {with_bom}"

    # ---------- 2. 时间戳的写法变体 ----------
    for stamp in (
        "00:00:01,000 --> 00:00:04,500",
        "00:00:01.000 --> 00:00:04.500",  # 用句点分隔毫秒
        "00:01,000 --> 00:04,500",  # 省略小时
    ):
        got = srt.parse_text(f"{stamp}\n文本\n")
        assert len(got) == 1, f"{stamp} 没解析出条目：{got}"
        assert got[0][0] == 1.0, f"{stamp} 起始秒应为 1.0，实际 {got[0][0]}"
    # 毫秒只有一位时是 500ms 不是 5ms——它的起始秒是 1.5，所以要单独断言
    assert srt.parse_text("00:00:01,5 --> 00:00:04,5\nx\n")[0][0] == 1.5

    # ---------- 3. 样式清洗与多行合并 ----------
    got = srt.parse_text(
        "1\n00:00:01,000 --> 00:00:02,000\n<i>斜体</i>{\\an8}与{\\pos(1,2)}标记\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n中文折\n行不该补空格\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nhello\nworld\n\n"
    )
    assert got[0][1] == "斜体与标记", got[0]
    assert got[1][1] == "中文折行不该补空格", got[1]  # 中文的换行是显示折行，直拼
    assert got[2][1] == "hello world", got[2]  # 英文两侧是单词字符，得补空格
    assert srt.clean_markup("a &amp; b") == "a & b"
    # 先剥标签再解实体：否则本来要显示成字面量的 &lt;i&gt; 会被当成真标签删掉
    assert srt.clean_markup("&lt;i&gt;") == "<i>"

    # ---------- 4. 乱序检测 ----------
    unordered = srt.parse_text("00:00:09,000 --> 00:00:10,000\n后\n\n00:00:01,000 --> 00:00:02,000\n前\n")
    assert srt.is_ordered(unordered) is False
    assert srt.is_ordered(expect) is True

    # 时间重叠是字幕的常态（双行对白同时显示），两条都要保留，不该被合并或丢弃
    overlap = srt.parse_text(
        "00:00:01,000 --> 00:00:05,000\n甲说的话\n\n"
        "00:00:03,000 --> 00:00:06,000\n乙说的话\n\n"
    )
    assert len(overlap) == 2, overlap
    assert overlap[0][1] == "甲说的话" and overlap[1][1] == "乙说的话", overlap

    # ---------- 5. 与视频时长的对齐校验（只警告，不阻止） ----------
    far = srt.check_alignment([(0.0, "a"), (9999.0, "b")], 600)
    assert far and "不是这条视频" in far[0], far
    part = srt.check_alignment([(0.0, "a"), (100.0, "b")], 3600)
    assert part and "只覆盖" in part[0], part
    late = srt.check_alignment([(900.0, "a"), (1000.0, "b")], 1000)
    assert late and "才开始" in late[0], late
    # 正常范围不该出声，否则警告会被当噪音
    assert srt.check_alignment([(0.0, "a"), (580.0, "b")], 600) == []

    # ---------- 6. 错误路径 ----------
    for bad, why in ((b"", "空文件"), (b"just plain text\nno stamps here\n", "没有时间行")):
        try:
            srt.parse_bytes(bad)
        except srt.SrtError:
            pass
        else:
            raise AssertionError(f"{why} 应当抛 SrtError")

    # ---------- 7. 接进采集：上传件优先，且平台字幕仍被如实报告 ----------
    class FakeClient:
        is_logged_in = True

        def get_data(self, url, params=None, **_kw):
            if url == VIDEO_VIEW:
                return {
                    "aid": 11,
                    "bvid": "BV1smoke",
                    "title": "测试视频",
                    "desc": "",
                    "owner": {"name": "某人", "mid": 2},
                    "duration": 600,
                    "tname": "知识",
                    "pages": [{"cid": 33, "duration": 600}],
                }
            if url == ARCHIVE_TAGS:
                return [{"tag_name": "标签"}]
            if url == PLAYER_V2:
                return {
                    "subtitle": {
                        "subtitles": [
                            {
                                # 用 ai- 前缀：无字幕视频上常见的就是平台 AI 字幕，
                                # 顺便把 is_ai 那条分支也覆盖到
                                "lan": "ai-zh",
                                "lan_doc": "中文（自动生成）",
                                "subtitle_url": "https://example.invalid/sub.json",
                            }
                        ]
                    }
                }
            return {}

        def get_json_url(self, url):
            # 回落到平台字幕时会真的下载它，所以要给一份能解析的 json3
            return {"body": [{"from": 0.0, "content": "平台 AI 字幕内容"}]}

        def get_bytes(self, url, params=None):
            raise collector.BiliError(-404, "测试里不提供弹幕")

    bundle = collector.collect(
        FakeClient(), "BV1smoke", 1, CollectConfig(), user_srt=text.encode("utf-8")
    )
    assert bundle.source_kind == "subtitle_user", bundle.source_kind
    assert bundle.source_label == "你上传的字幕", bundle.source_label
    # 时间戳走 collector 的 _stamp：不足一小时时只输出 MM:SS，不补前导的 00:
    assert "[00:01] 第一句字幕" in bundle.body, bundle.body
    assert "[00:05] 第二句字幕" in bundle.body, bundle.body
    assert bundle.duration == 600, bundle.duration

    kinds = {c.kind: c for c in bundle.checks}
    assert kinds["subtitle_user"].available is True, kinds["subtitle_user"]
    assert "已解析 2 段" in kinds["subtitle_user"].detail, kinds["subtitle_user"].detail
    # 平台确实有字幕这件事必须如实报告——否则使用者会以为上传被静默忽略了
    assert kinds["subtitle"].available is True, kinds["subtitle"]
    assert "未采用" in kinds["subtitle"].detail, kinds["subtitle"].detail
    # 已经用上上传件，就不该再去抓弹幕
    assert kinds["danmaku"].available is False, kinds["danmaku"]
    assert "跳过" in kinds["danmaku"].detail, kinds["danmaku"].detail

    # ---------- 8. 时间轴必须能为上传件生成（最容易漏的那处耦合） ----------
    seen: list = []

    class FakeLlm:
        cfg = types.SimpleNamespace(model="fake-model")

        def chat(self, messages, **_kw):
            seen.append(messages)
            return (
                '{"one_line":"一句话","summary_md":"正文","key_points":[],'
                '"timeline":[{"t":"00:00:01","text":"开场"}],"caution":"","comment_short":"短评"}'
            )

    out = summarizer.summarize(bundle, FakeLlm(), AppConfig(), role_key="standard")
    prompt = seen[0][1]["content"]
    assert "不要生成 timeline 内容" not in prompt, (
        "上传件被当成了「没有时间戳」——时间轴会静默变空，而且不报错"
    )
    assert out.timeline, out.timeline

    # ---------- 9. 上传件解析失败时，必须降级回平台字幕而不是让任务失败 ----------
    fallback = collector.collect(
        FakeClient(), "BV1smoke", 1, CollectConfig(), user_srt=b"this is not a srt file"
    )
    assert fallback.source_kind == "subtitle_ai", fallback.source_kind
    bad_check = {c.kind: c for c in fallback.checks}["subtitle_user"]
    assert bad_check.available is False, bad_check
    assert "回落到平台字幕" in bad_check.detail, bad_check.detail
    assert any("用不了" in w for w in fallback.warnings), fallback.warnings

    return (
        "四种编码可读 / 时间戳变体（含省略小时与一位毫秒）/ 样式与多行合并正确 / "
        "乱序可检测 / 对齐校验三判据且正常不报警 / 上传件优先且平台字幕如实报告 / "
        "时间轴能为上传件生成 / 解析失败降级回平台字幕"
    )


def _uploads_lifecycle() -> str:
    """上传件的临存：存/取/删、token 安全、兜底清理。

    这里最要紧的不是"能不能存下来"，而是 **token 会不会被当成路径拼进去**——
    token 来自外部请求，`../` 这类输入如果没挡住，就等于开了一个能读写任意路径的洞。
    """
    import tempfile

    from bili_scribe.infra import uploads

    tmp = Path(tempfile.mkdtemp(prefix="bs-up-"))
    db = str(tmp / "t.db")

    token = uploads.new_token()
    assert len(token) >= 16, token
    assert uploads.exists(db, token) is False, "还没存就报存在"
    assert uploads.load(db, token) is None

    raw = "1\n00:00:01,000 --> 00:00:02,000\n内容\n".encode("utf-8")
    saved = uploads.save(db, token, raw)
    assert saved.exists(), saved
    assert uploads.exists(db, token) is True
    assert uploads.load(db, token) == raw, "取回的字节和存进去的不一致"
    assert uploads.total_size(db) == len(raw), uploads.total_size(db)

    # token 是外部输入，必须挡住路径穿越与畸形值
    for bad in ("../../etc/passwd", "..\\..\\win.ini", "a/b", "", "short", "x" * 100, "有中文"):
        assert uploads.load(db, bad) is None, f"非法 token 被当成路径了：{bad!r}"
        assert uploads.exists(db, bad) is False, bad
        assert uploads.discard(db, bad) == 0, bad

    # 用完即删，且重复删不该报错
    assert uploads.discard(db, token) == 1, "没有删掉"
    assert uploads.exists(db, token) is False, "删完还在"
    assert uploads.discard(db, token) == 0, "重复删应当返回 0"
    assert uploads.total_size(db) == 0, uploads.total_size(db)

    # 兜底清理：只收过期的（"传了文件却没提交任务"留下的那些）
    t2 = uploads.new_token()
    saved2 = uploads.save(db, t2, raw)
    assert uploads.purge_stale(db, max_age_seconds=3600) == 0, "刚存的被当残留清了"
    assert uploads.exists(db, t2) is True
    old = time.time() - 3 * 3600
    os.utime(saved2, (old, old))
    assert uploads.purge_stale(db, max_age_seconds=3600) == 1, "过期上传件没被清掉"
    assert uploads.total_size(db) == 0, uploads.total_size(db)

    # 大小上限要存在，且是个合理的数——它挡的是"选错文件"（比如误传了一个视频）
    assert 1024 * 1024 <= uploads.MAX_BYTES <= 50 * 1024 * 1024, uploads.MAX_BYTES

    return "token 拒绝路径穿越 / 存取一致 / 用完即删 / 只清过期残留 / 有大小上限"


def _roles_registry() -> str:
    """角色注册表的不变量。

    这些不变量坏了不会报错，只会静默降级——比如角色提示词里漏了字段说明，
    模型就自己编一套；或者 OUTPUT_CONTRACT 没接上，模型就真的去打印阶段 1/2/3，
    然后 JSON 解析失败。所以必须显式钉住。
    """
    from bili_scribe.core import roles

    assert len(roles.ALL_ROLES) == 8, [r.key for r in roles.ALL_ROLES]
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
    assert len(described) == 8 and all("key" in d and "name" in d for d in described)
    return f"{len(roles.ALL_ROLES)} 个角色，7 个三轮回，输出契约与底线条款齐备"


def _role_shensi() -> str:
    """「超强思考模式总结」：四问骨架 + 文体纪律 + 归纳可核对。

    这个角色的价值**全在提示词里**——没有代码实现它。所以提示词被改坏
    （四问少了一节、套话禁令被删掉、"归纳必须指出依据"那一句丢了）不会有任何报错，
    只会静默地变成另一个东西。规格是使用者直接给的，所以逐条钉住。

    三条最容易被"顺手优化掉"的：
    - 四个二级标题被改成不显式的叙述（那就不再是这个角色了）；
    - 禁令从"具体形态"退化成"要真诚、要去掉废话"（这种话对着模型说等于没说）；
    - 「归纳隐含问题」丢掉"必须指出素材依据"，于是变成替作者立论。
    """
    from bili_scribe.core import roles

    role = roles.get_role("shensi")
    assert role.key == "shensi" and role.name == "超强思考模式总结", role
    assert role.passes == 3, "这个角色要三轮回：文体问题靠一轮自己看不见"

    for section in (
        "## 作者提出的问题",
        "## 内容的实质",
        "## 他给出的解法",
        "## 他发现了什么",
    ):
        assert section in role.system, f"四问骨架缺了：{section}"

    for ban in ("接下来我们看看", "深刻地揭示了", "不仅仅是", "综上所述", "我们应该"):
        assert ban in role.system, f"套话禁令被删了：{ban}"
    assert "能删掉一个词就删掉" in role.system, "奥威尔那条（能删就删）没了"
    assert "玻璃窗" in role.system, "文体判据（好文章像玻璃窗）没了"

    # 「尽量替它归纳隐含问题」必须带着可核对的两条，缺一条就退化成替作者立论
    assert "归纳" in role.system and ("支撑" in role.system or "依据" in role.system), (
        "归纳隐含问题却没说必须指出素材依据"
    )

    assert "不要按你自己的清单去挑内容毛病" in role.critique, (
        "批判轮又变成通用清单了——它会顺手把四问结构也改掉（这个坑踩过）"
    )
    assert "逐字保留" in role.critique, "没有「逐字保留处」，修订会把好句子一起磨平"
    assert "指不出依据" in role.critique, "批判轮没有专门盯「替作者立论」"

    assert "{critique}" in role.revise, "修订提示词没有插批判"
    assert "不要用另一句套话补上" in role.revise, "修订时可以拿套话补套话"
    assert "长度不是目标" in role.revise, "没写「长度不是目标」，容易被注水"

    # key_points 与引用：活体验证第一版就栽在这里——模型把原话碎片（还带省略号截断、
    # 夹着英文白字）直接当成了要点，而批判轮因为被限定在"只盯文体与立论"而看不见。
    # 所以这两条必须写在提示词里，且批判轮必须点名检查。
    assert "提炼过的判断，不是素材里的原话片段" in role.system and "省略号截断" in role.system, (
        "要点必须被明确要求成提炼后的判断，否则模型会剪贴原话"
    )
    assert "同音字错误" in role.system, "没写 AI 字幕同音字不要直接搬"
    assert "要点与引用" in role.critique, "批判轮没有点名检查要点与引用——它只看文体，会漏掉剪贴"

    return "四问骨架齐、禁令具体、归纳必须给出素材依据、批判轮专盯文体与立论"


def _subtitle_parse_offload() -> str:
    """上传解析不能阻塞事件循环。

    `/api/subtitle/parse` 是个 async 路由，而 srt 解析是同步的 CPU 操作。
    实测一份 5 MB（约 5 万条）的 srt 解析要 0.5-0.8 秒——直接在事件循环里跑，
    这段时间内**其他所有请求都会卡住**（包括正在轮询的任务进度）。常见文件
    只有十几毫秒，但那不构成「可以偶尔卡一下」的理由。

    这条用例是**防回归守卫**：断言接口把解析与落盘都交给了线程池。
    我把话说明白——它测的是**实现方式**，不是行为。原因是「事件循环有没有
    被阻塞」在单进程测试里很难稳定观测（要精确构造并发与时序，容易 flaky），
    而源码守卫能挡住大部分退化场景：有人为了简洁把它改回直接调用。

    ⚠️ 2026-10-27 更新断言：业务逻辑搬进 `service.py` 之后，线程池包的是
    **整个 `service.parse_subtitle`**（解析 + 落盘一次切换），而不是分别包两步。
    这比原来更保险——中间那次 await 会让出事件循环，而现在是零个 await。
    所以断言从「匹配 srt_mod.parse_bytes」改成「匹配 service.parse_subtitle」，
    并补一条：它必须是**同步**函数。如果哪天有人把它写成 `async def`，
    `run_in_threadpool` 会立刻返回一个协程、真正的解析跑在事件循环线程上，
    阻塞照旧——而那一步从源码上看是看不出来的，所以必须显式钉住。
    """
    import inspect as _inspect
    import re as _re
    import time

    from bili_scribe.app.service import Service
    from bili_scribe.core import srt as srt_mod

    src = (PROJECT / "bili_scribe" / "app" / "main.py").read_text(encoding="utf-8")
    m = _re.search(r"async def api_parse_subtitle.*?(?=\n    @app\.)", src, _re.S)
    assert m, "找不到 api_parse_subtitle —— 接口改名了？这条守卫需要跟着更新"
    body = m.group(0)

    assert "run_in_threadpool(service.parse_subtitle" in body, (
        "解析没有整体交给线程池：它会阻塞事件循环，让其他请求（含任务进度轮询）一起卡住"
    )
    assert "parse_bytes(" not in body and "uploads.save(" not in body, (
        "路由里直接调了解析或落盘——它们必须留在被线程池包住的那一侧"
    )
    assert not _inspect.iscoroutinefunction(Service.parse_subtitle), (
        "Service.parse_subtitle 不能是 async：run_in_threadpool 会把协程原样返回，"
        "真正的同步解析于是在事件循环线程上跑，阻塞照旧且看不出来"
    )

    # 复杂度守卫：解析应当是线性的。造 4 万条，上界给得很宽——
    # 这条防的是「有人引入 O(n^2) 写法」，不是卡性能指标。
    def stamp(sec: int) -> str:
        h, rem = divmod(sec, 3600)
        mnt, s = divmod(rem, 60)
        return f"{h:02d}:{mnt:02d}:{s:02d},000"

    bulk = "\n".join(
        f"{i}\n{stamp(i * 2)} --> {stamp(i * 2 + 1)}\n第 {i} 条测试字幕内容\n" for i in range(1, 40001)
    ).encode("utf-8")
    t0 = time.perf_counter()
    items, enc = srt_mod.parse_bytes(bulk)
    dt = time.perf_counter() - t0
    assert len(items) == 40000, f"只解析出 {len(items)} 条"
    assert dt < 10, f"解析 4 万条用了 {dt:.1f} 秒，复杂度可能退化了"
    return f"接口经线程池，未阻塞事件循环；4 万条解析 {dt * 1000:.0f} ms（{enc}）"


def _upload_entry_visible() -> str:
    """上传入口必须是看得见的按钮。

    起因是一次真实反馈：「页面上找不到选择字幕文件的选项」。查下来元素在、
    JS 也对，问题出在样式——原生 `input[type=file]` 当时用 var(--line) #2c313a
    作边框、var(--bg) #16181d 作背景，**对比度约 1.4:1**（常用可辨识阈值是 3:1），
    在深色页面里几乎融进背景，看起来就像没有这个功能。

    这类缺陷 DOM 桩永远抓不到：桩里 getElementById 对任何 id 都凭空返回对象，
    「元素在不在」它一律答「在」。所以这条改成读 HTML 源码 + 算对比度——
    把一个视觉可辨识性要求变成可执行的约束。
    """
    import re

    html = (PROJECT / "bili_scribe" / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def rgb(token: str) -> tuple[int, int, int]:
        token = token.strip()
        var = re.fullmatch(r"var\((--[\w-]+)\)", token)
        if var:
            found = re.search(
                rf"{re.escape(var.group(1))}\s*:\s*#([0-9a-fA-F]{{6}})", html
            )
            assert found, f"CSS 变量 {var.group(1)} 没有定义，样式会静默失效"
            token = "#" + found.group(1)
        short = re.fullmatch(r"#([0-9a-fA-F]{3})", token)
        if short:  # #fff 是合法写法，展开成 #ffffff
            token = "#" + "".join(c * 2 for c in short.group(1))
        m = re.fullmatch(r"#([0-9a-fA-F]{6})", token)
        assert m, f"认不出的颜色写法：{token!r}"
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def lum(token: str) -> float:
        def ch(v: int) -> float:
            s = v / 255
            return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

        r, g, b = rgb(token)
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

    def contrast(a: str, b: str) -> float:
        la, lb = lum(a), lum(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    # 1) 入口必须是真按钮，不是原生控件
    btn = re.search(r"<button[^>]*id=\"srt-btn\"[^>]*>", html)
    assert btn, "找不到 id=srt-btn 的按钮 —— 上传入口没了"
    assert "onclick" not in btn.group(0), (
        "按钮不该用内联 onclick 接线（脚本在末尾统一绑定），"
        "两处都接会让一次点击触发两次"
    )

    # 2) 桌面端走的是原生文件对话框，所以页面上不该再有 <input type=file>：
    #    留着它等于留着一个点了没用、或者走另一套代码的假入口。
    assert not re.search(r"<input[^>]*type=\"file\"", html), (
        "页面里还有原生 file input —— 桌面端选文件走的是 pick_subtitle 的"
        "系统对话框，两套入口并存会有一套是坏的"
    )
    assert "pick_subtitle" in html, "按钮没有接到 pick_subtitle，点了不会有反应"

    # 3) 按钮的字必须与页面底色拉开差距，否则「在但看不见」会重演
    rule = re.search(r"\.btn2\s*\{([^}]*)\}", html)
    assert rule, "缺 .btn2 的样式规则"
    body = rule.group(1)
    fg = re.search(r"color:\s*([^;]+);", body)
    assert fg, f".btn2 必须显式写 color：{body.strip()}"

    c_page = contrast(fg.group(1), "var(--bg)")
    assert c_page >= 3, (
        f"字幕入口与页面底色的对比度只有 {c_page:.2f}:1，低于 3:1 —— 会看不见。"
        f"（上次就是这个数太低导致的反馈）"
    )

    # 4) 页面不能自己再维护一份「在跑」状态：多一份就多一处会不一致
    assert "isRunning()" in html and "function isRunning" in html, (
        "提交按钮没有用 isRunning() 从任务状态推「在跑」，"
        "说明又另外存了一份状态"
    )

    return f"入口为按钮形态且接 pick_subtitle；按钮文字对比度 {c_page:.2f}:1"


def _refine_reuses_prepared() -> str:
    """迭代打磨必须**真的跳过预读**。

    这是阶段 1 的全部意义：同一份素材改很多版，预读只付一次钱（4 小时视频
    省下 32,663 tokens，占一次总结的 38%）。

    **怎么证明「跳过了」**：用假 LLM 记下每次收到的 system 提示词。预读用的是
    MAP_SYSTEM，归约用的是另一套，两者不会混。所以只要断言「这一轮里一条
    MAP_SYSTEM 都没有」，就证明预读真的没跑——这比看耗时可靠得多，
    耗时会被网络波动掩盖。

    顺带钉住两个容易漏的点：
    - **笔记本身超限时不能再预读**：那是二次压缩，信息会在无人察觉的情况下再掉一层；
    - **修订必须同时带着素材笔记全文**：只给上一版的话，几轮「基于上一版改」会像
      传话游戏一样逐渐偏离素材，而产物是要发到别人评论区的。
    """
    import types

    from bili_scribe.config import AppConfig
    from bili_scribe.core import summarizer
    from bili_scribe.core.collector import Bundle

    cfg = AppConfig()
    cfg.llm.max_input_chars = 200
    cfg.llm.chunk_size = 200
    cfg.llm.chunk_overlap = 20

    bundle = Bundle(
        bvid="BV1refine",
        aid=7,
        cid=8,
        page=1,
        page_count=1,
        title="迭代测试",
        desc="",
        up_name="某人",
        up_mid=9,
        duration=3600,
        tname="知识",
        source_kind="subtitle_ai",
        source_label="平台 AI 字幕",
        source_trust="中",
        source_detail="测试",
        body="素材正文。" * 200,  # 1000 字符 > 上限 200
    )

    seen: list[list[dict]] = []

    class FakeLlm:
        cfg = types.SimpleNamespace(model="fake-model")

        def chat(self, messages, **_kw):
            seen.append(messages)
            return (
                '{"one_line":"一句话","summary_md":"正文","key_points":[],'
                '"timeline":[],"caution":"","comment_short":"短评",'
                '"revise_note":"改了解释顺序"}'
            )

    llm = FakeLlm()
    marker = summarizer.MAP_SYSTEM[:20]

    # ---------- 1. 首次生成：长素材应当发生分段预读 ----------
    material = summarizer.prepare_material(bundle, llm, cfg)
    pre_calls = len(seen)
    assert pre_calls >= 2, f"长素材应当分段预读，实际只调用了 {pre_calls} 次"
    assert material.chunks >= 2, f"分块数不对：{material.chunks}"
    assert all(marker in m[0]["content"] for m in seen), "预读阶段发的应当是 MAP_SYSTEM"
    seen.clear()

    # ---------- 2. 复用笔记 → 不许再预读 ----------
    out1 = summarizer.summarize(bundle, llm, cfg, role_key="standard", prepared=material)
    assert len(seen) == 1, f"复用了笔记就不该再有预读调用，实际 {len(seen)} 次"
    assert marker not in seen[0][0]["content"], "又发了一次 MAP_SYSTEM —— 预读没被跳过"
    assert out1.chunks == material.chunks, (
        f"分块数要如实带过来：{out1.chunks} != {material.chunks}"
    )
    assert out1.revise_note == "", "初次生成不该有 revise_note"

    # ---------- 3. 迭代打磨 ----------
    seen.clear()
    out2 = summarizer.summarize(
        bundle,
        llm,
        cfg,
        role_key="standard",
        prepared=material,
        refinement=summarizer.Refinement(
            requirement="把第三部分展开，别用比喻", previous='{"summary_md":"上一版正文"}'
        ),
    )
    assert len(seen) == 1, f"迭代也应当只调一次，实际 {len(seen)} 次"
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert marker not in system, "迭代时又预读了一遍"
    assert "把第三部分展开" in user, "本次修改要求没进提示词"
    assert "上一版正文" in user, "上一版没进提示词"
    assert material.body[:30] in user, (
        "迭代时丢了素材笔记 —— 模型会开始脱离素材改，而这一点从产出上看不出来"
    )
    assert out2.revise_note == "改了解释顺序", out2.revise_note

    # ---------- 4. 笔记自己超限，也不能被当成新素材再预读 ----------
    seen.clear()
    long_notes = summarizer.PreparedMaterial(body="笔记正文。" * 400, chunks=9)
    assert len(long_notes.body) > cfg.llm.max_input_chars, "这条的前提是笔记比上限长"
    out3 = summarizer.summarize(bundle, llm, cfg, role_key="standard", prepared=long_notes)
    assert len(seen) == 1, (
        f"笔记超限时被当成新素材又预读了一遍（{len(seen)} 次调用）"
        "—— 那是二次压缩，信息会再掉一层"
    )
    assert out3.chunks == 9, out3.chunks

    # ---------- 5. 上一版不能带着它自己的 revise_note ----------
    # 这条是**活体验收抓到的**：连续改两版，两版的 revise_note 一字不差——
    # 模型看到上一版 JSON 里那个字段，直接照抄了。而使用者恰恰要靠这句话
    # 判断「这一版到底改了什么」，照抄等于这个字段白设计了。
    view = summarizer.build_previous_view(
        {"summary_md": "上一版正文", "key_points": ["甲"], "revise_note": "上一版的说明"}
    )
    assert "revise_note" not in view, f"上一版的说明被带进提示词了：{view}"
    assert "上一版的说明" not in view, "上一版说明的内容漏进去了"
    assert "上一版正文" in view, "summary_md 必须留着——模型要看上一版长什么样"

    return (
        f"预读 {pre_calls} 段只付一次；首次与迭代都不再预读；"
        f"修订同时带上素材与上一版（且不带上一版的自我说明）；笔记超限也不二次预读"
    )


def _version_history() -> str:
    """版本史：落库、编号、旧记录回填、回退。

    这个表存在的理由是**不可重建**：素材笔记丢了重新预读就能算回来，而一段被人
    逐条改过的文字，重跑模型不会给出同一个结果。所以这里要盯住的不是"能不能写
    进去"，而是"能不能忠实"——编号不许重、旧记录不能凭空消失、回退要留下痕迹。
    """
    import tempfile
    import threading

    from bili_scribe.infra.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="bs-ver-"))
    st = Store(str(tmp / "t.db"), str(tmp / "k.bin"))

    # ---------- 1. 升级前的老记录：有总结、没有版本行 ----------
    old = st.create_task("BV1old")
    st.update_task(
        old, status="ready", summary_json={"one_line": "老总结", "role": "cordial"},
        preview_md="旧预览", comment_text="旧正文",
    )
    assert st.list_versions(old) == [], "还没写版本，列表应当是空的"
    assert st.ensure_initial_version(old) is True, "有总结的老记录应当被补成第 1 版"
    assert st.ensure_initial_version(old) is False, "回填必须幂等，不能补出第二条第 1 版"
    v1 = st.list_versions(old)[0]
    assert v1["version_no"] == 1 and v1["source"] == "initial", v1
    assert v1["summary_json"]["one_line"] == "老总结", v1["summary_json"]
    assert v1["role"] == "cordial", "角色要从旧 summary_json 里带过来"
    assert v1["metrics"] is None, "老版本没有记账，必须是 None 而不是 0"

    # 老记录上再改一版：这里走的是"读时已补写过"的那条路，所以不该再补一次
    out = st.record_version(
        old, source="refine", requirement="展开第三段",
        summary_json={"one_line": "新总结", "role": "cordial", "revise_note": "展开了第三段"},
        preview_md="新预览", comment_text="新正文", role="cordial",
        metrics={"usage": {"calls": 2, "total_tokens": 300}, "elapsed": 12.5},
        status="ready", progress="改好了",
    )
    assert out["version_no"] == 2 and out["backfilled_previous"] is False, out
    assert len(st.list_versions(old)) == 2, "老版本不该被新版本挤掉"

    # 另一条老记录：从没被读过就直接改一版 —— 补写必须发生在这一次写入里，
    # 否则第 1 版会凭空消失，而"改过的东西都留着"就成了一句空话。
    legacy = st.create_task("BV1legacy")
    st.update_task(legacy, summary_json={"one_line": "从没被读过的老总结"},
                   preview_md="旧预览", comment_text="旧正文")
    out2 = st.record_version(legacy, source="refine", requirement="改一下",
                             summary_json={"one_line": "第二条新总结"})
    assert out2["version_no"] == 2 and out2["backfilled_previous"] is True, out2
    kept = st.list_versions(legacy)
    assert [v["version_no"] for v in kept] == [1, 2], kept
    assert kept[0]["summary_json"]["one_line"] == "从没被读过的老总结", kept[0]["summary_json"]

    assert st.get_task(old)["summary_json"]["one_line"] == "新总结", "当前版要跟着更新"
    assert st.get_task(old)["preview_md"] == "新预览"
    v2 = st.get_version(old, 2)
    assert v2["requirement"] == "展开第三段" and v2["metrics"]["usage"]["calls"] == 2
    assert st.get_version(old, 99) is None, "不存在的一版要返回 None 而不是报错"

    # 老记录里本来就可能是一次"改版"的产物（升级前改过版的任务）。
    # 它的内容自己会说话——summary_json 里带着 revise_note。标成「初次生成」
    # 就是标签和内容对不上，而这种小谎最难被发现，因为它看起来很正常。
    revised = st.create_task("BV1revised")
    st.update_task(revised, summary_json={"one_line": "改过的总结",
                                          "revise_note": "按上一条要求改了开头"})
    st.ensure_initial_version(revised)
    v = st.list_versions(revised)[0]
    assert v["source"] == "refine", f"带改动说明的老记录该标成 refine：{v['source']}"
    assert v["requirement"], "要求虽然没留存，但要如实说明，不能留白"

    # ---------- 2. 回退：把第 1 版抄成新的当前版，不动已有版本 ----------
    st.record_version(
        old, source="restore", requirement="以第 1 版为底稿",
        summary_json=v1["summary_json"], preview_md=v1["preview_md"],
        comment_text=v1["comment_text"], role=v1["role"], metrics=None,
        status="ready", progress="回来了",
    )
    versions = st.list_versions(old)
    assert [v["version_no"] for v in versions] == [1, 2, 3], [v["version_no"] for v in versions]
    assert versions[2]["source"] == "restore" and versions[2]["metrics"] is None
    assert st.get_task(old)["summary_json"] == v1["summary_json"], "回退后当前版应是第 1 版的内容"
    # 编号必须单调、不重号——否则"当前是第几版"就没有意义了
    assert len({v["version_no"] for v in versions}) == 3

    # ---------- 3. 并发追加同一任务：编号不能撞车 ----------
    # 两个几乎同时到达的迭代请求会各写一版，而"当前是第几版"全靠编号区分。
    # 编号如果在 Python 里先读后写，这两条会抢到同一个号。
    race = st.create_task("BV1race")
    st.update_task(race, summary_json={"one_line": "x"})
    errors: list[str] = []

    def spawn(index: int) -> None:
        try:
            st.record_version(
                race, source="refine", requirement=f"并发 {index}",
                summary_json={"one_line": f"第 {index} 次"},
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=spawn, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写版本出错：{errors}"
    nums = sorted(v["version_no"] for v in st.list_versions(race))
    # 补写的第 1 版 + 4 条并发 = 1..5，号不重不漏
    assert nums == [1, 2, 3, 4, 5], f"并发下版本号应当不重不漏，实际 {nums}"

    # ---------- 4. 没有总结的任务：不产生任何版本 ----------
    empty = st.create_task("BV1empty")
    assert st.ensure_initial_version(empty) is False, "没有总结就不该凭空补出版本"
    try:
        st.record_version("不存在", source="initial", summary_json={})
        raise AssertionError("给不存在的任务写版本应当报错")
    except KeyError:
        pass
    try:
        st.record_version(empty, source="乱来", summary_json={})
        raise AssertionError("未知来源应当被拒绝")
    except ValueError:
        pass

    return (
        "老记录补写第 1 版且幂等；追加与回退都在同一事务里；"
        "并发 4 条编号 1..5 不重不漏；无总结不产生版本；未知来源被拒"
    )


def _llm_usage() -> str:
    """token 记账：每次 HTTP 往返都要算进账，且不同实例的账互不污染。

    "这一版花了多少"是迭代打磨唯一能判断值不值的依据，算错就等于这个功能没做。
    两件事最容易错：① 把"端点没返回 usage"记成 0（看起来像不要钱）；② 共用实例
    导致并发任务的账串在一起。所以这里把两种情况都钉住。
    """
    from bili_scribe.config import LlmConfig
    from bili_scribe.infra.llm import LlmClient

    cfg = LlmConfig(base_url="https://example.invalid/v1", api_key="k", model="m")
    client = LlmClient(cfg)

    class FakeResp:
        status_code = 200
        text = ""

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    seen: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None):  # noqa: ANN001, ARG001
        seen.append(json)
        if len(seen) == 1:
            return FakeResp({
                "choices": [{"message": {"content": "好"}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            })
        # 有些兼容端点压根不返回 usage
        return FakeResp({"choices": [{"message": {"content": "好"}}]})

    client.session.post = fake_post
    client.chat([{"role": "user", "content": "a"}])
    client.chat([{"role": "user", "content": "b"}])

    u = client.usage.to_dict()
    assert u["calls"] == 2, u
    assert u["prompt_tokens"] == 1000 and u["completion_tokens"] == 50, u
    assert u["total_tokens"] == 1050 and u["reasoning_tokens"] == 30, u
    assert u["unpriced"] == 1, f"端点没返回用量那一次必须被单独记下来：{u}"

    # 另一个实例的账必须是干净的（并发任务各记各的）
    other = LlmClient(cfg)
    assert other.usage.to_dict()["calls"] == 0, "新实例的账不该接着上一条任务的算"

    # 汇总：把版本里存的账加起来，老版本（没记账）如实算"未记录"
    from bili_scribe.app.main import _sum_usage

    total = _sum_usage([
        {"source": "initial", "metrics": {"usage": {"calls": 7, "prompt_tokens": 100,
                                                    "completion_tokens": 20,
                                                    "total_tokens": 120, "unpriced": 0}}},
        {"source": "refine", "metrics": {"usage": {"calls": 1, "prompt_tokens": 10,
                                                   "completion_tokens": 5,
                                                   "total_tokens": 15, "unpriced": 0}}},
        {"source": "restore", "metrics": None},
        {"source": "refine", "metrics": None},
    ])
    assert total["calls"] == 8 and total["total_tokens"] == 135, total
    assert total["untracked"] == 1, f"只有没记账的那一版才算未记录：{total}"
    return (
        f"两次调用记 {u['calls']} 次、输入 {u['prompt_tokens']} / 输出 {u['completion_tokens']} "
        f"tokens，{u['unpriced']} 次无用量被单列；实例之间互不污染；汇总跳过回退版"
    )


def _llm_transient_retry() -> str:
    """瞬时断链要重试，真故障不该重试。

    这是这条线路上的真实故障：走代理请求 aihubmix 时，对端在 TLS 握手或读响应
    的中途直接把连接掐掉，报
        SSLError: SSL: UNEXPECTED_EOF_WHILE_READING EOF occurred in violation of protocol
    或 ProxyError('Unable to connect to proxy', RemoteEndClosed(...))
    抖动时长只有 0.1–0.3 秒，但一次就足以让一整版素材白跑几万 token。

    边界的另一半同样重要：**只有"下一轮可能不一样"的错误才值得重试**。
      · 超时——已经等满一整轮 timeout_seconds，再等两轮只是把 5 分钟变成 15 分钟；
      · HTTP 4xx/5xx——响应体里已经写明原因，盲目重试是白烧额度；
      · 代理端口没开（连接被拒）——重试一百次也一样，该做的是让人去改配置。
    所以这里把"重试"和"不重试"两侧都钉住，而不是只测能救回来的那一半。
    """
    import requests

    from bili_scribe.config import LlmConfig
    from bili_scribe.infra import llm as llm_mod
    from bili_scribe.infra.llm import LlmClient, LlmError

    # 把退避压到近乎 0：这条用例要测的是"重试了几次"，不是"等了多久"。
    saved_backoff = llm_mod.RETRY_BACKOFF
    llm_mod.RETRY_BACKOFF = (0.0, 0.0)
    msgs = [{"role": "user", "content": "hi"}]

    def make() -> LlmClient:
        return LlmClient(
            LlmConfig(
                api_key="k",
                model="m",
                base_url="https://api.example.com/v1",
                proxy="http://127.0.0.1:7897",
            )
        )

    def ssl_eof() -> Exception:
        # 现场捞回来的原文，不是编的
        return requests.exceptions.SSLError(
            "SSL: UNEXPECTED_EOF_WHILE_READING EOF occurred in violation of protocol"
        )

    class Ok:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "好"}}]}

    try:
        # ---------- 1. 抖两下、第三次通了：这一版不该报废 ----------
        calls = {"n": 0}

        def flaky(*args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            calls["n"] += 1
            if calls["n"] < 3:
                raise ssl_eof()
            return Ok()

        c = make()
        c.session.post = flaky
        notes: list[str] = []
        assert c.chat(msgs, note=notes.append) == "好", "重试后应当拿到正文"
        assert calls["n"] == 3, f"应当打 3 次，实际 {calls['n']} 次"
        assert len(notes) == 2, f"每次重试都该出现在进度里，实际 {notes}"
        assert "重试" in notes[0], notes

        # ---------- 2. 一直断：报错，并且说清已经重试过 ----------
        c = make()
        c.session.post = lambda *a, **k: (_ for _ in ()).throw(ssl_eof())  # noqa: ARG005
        try:
            c.chat(msgs)
        except LlmError as exc:
            text = str(exc)
            assert "已自动重试" in text, f"得让人知道不是没努力过：{text}"
            assert "127.0.0.1:7897" in text, f"得带上当前代理，才改得动：{text}"
        else:
            raise AssertionError("一直断还不报错，界面会永远停在'正在改'")

        # ---------- 3. 代理端口没开：一次就够，别假装是抖动 ----------
        refused = {"n": 0}

        def reject(*args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            refused["n"] += 1
            raise requests.exceptions.ProxyError(
                "Cannot connect to proxy.", OSError("Connection refused")
            )

        c = make()
        c.session.post = reject
        try:
            c.chat(msgs)
        except LlmError as exc:
            assert "llm.proxy" in str(exc), f"该让人去改配置：{exc}"
        else:
            raise AssertionError("代理连不上还不报错")
        assert refused["n"] == 1, f"连接被拒重试是白费力气，实际打了 {refused['n']} 次"

        # ---------- 4. 超时不重试 ----------
        slow = {"n": 0}

        def timeout(*args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            slow["n"] += 1
            raise requests.exceptions.ReadTimeout("read timed out")

        c = make()
        c.session.post = timeout
        try:
            c.chat(msgs)
        except LlmError:
            pass
        else:
            raise AssertionError("超时还不报错")
        assert slow["n"] == 1, f"超时已经很慢了，不该再等两轮，实际打了 {slow['n']} 次"

        # ---------- 5. HTTP 500 不重试（响应体已经说明原因） ----------
        class Boom:
            status_code = 500
            text = "boom"

            def json(self):
                return {}

        broken = {"n": 0}

        def post_500(*args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            broken["n"] += 1
            return Boom()

        c = make()
        c.session.post = post_500
        try:
            c.chat(msgs)
        except LlmError as exc:
            assert "500" in str(exc), str(exc)[:120]
        else:
            raise AssertionError("HTTP 500 还不报错")
        assert broken["n"] == 1, f"状态码错误重试也不会变，实际打了 {broken['n']} 次"
    finally:
        llm_mod.RETRY_BACKOFF = saved_backoff

    return (
        "TLS 抖动重试 3 次后成功且进度可见；一直断报错并说明已重试；"
        "连接被拒 / 超时 / HTTP 500 一律不重试"
    )


def _refine_writes_version() -> str:
    """迭代打磨真的落库成新一版，失败则不落。

    跑的是真实的 `_run_refine`（后台线程里跑的就是它），只把模型换成假的、
    把画面帧关掉，所以能验到"版本表被写了什么"，而不只是 summarizer 的返回值。
    """
    import json as _json
    import tempfile
    import types

    from bili_scribe.app.main import AppState, RefineContext, _run_refine
    from bili_scribe.config import AppConfig
    from bili_scribe.core.collector import Bundle
    from bili_scribe.core.summarizer import PreparedMaterial
    from bili_scribe.infra.llm import ChatResult, LlmError, Usage

    tmp = Path(tempfile.mkdtemp(prefix="bs-refversion-"))
    cfg = AppConfig()
    # db_path / key_path 是 AppConfig 上的只读属性，真正要改的是 storage 段
    cfg.storage.db_path = str(tmp / "t.db")
    cfg.storage.key_path = str(tmp / "k.bin")
    # 关掉画面帧：这一轮要验的是版本落库，而取帧要真连 B站。
    cfg.collect.frames.enabled = False

    state = AppState(cfg)

    bundle = Bundle(
        bvid="BV1ver", aid=7, cid=8, page=1, page_count=1,
        title="迭代落库", desc="", up_name="某人", up_mid=9, duration=600,
        tname="知识", source_kind="subtitle_ai", source_label="平台 AI 字幕",
        source_trust="中", source_detail="测试", body="笔记正文",
    )

    class FakeLlm:
        """跑真实记账路径的假模型：账是通过 Usage/ChatResult 真加进去的。"""

        def __init__(self, fail: bool = False):
            self.cfg = types.SimpleNamespace(model="fake-model")
            self.usage = Usage()
            self.fail = fail
            self.systems: list[str] = []

        def chat(self, messages, **_kw):
            self.systems.append(messages[0]["content"])
            if self.fail:
                self.usage.add(ChatResult(content="", prompt_tokens=5, completion_tokens=50,
                                          total_tokens=55))
                raise LlmError("假模型：这次改不动")
            self.usage.add(ChatResult(content="", prompt_tokens=800, completion_tokens=100,
                                      total_tokens=900))
            return _json.dumps({
                "one_line": "改后的一句话",
                "summary_md": "改后的正文",
                "key_points": ["甲"],
                "timeline": [],
                "caution": "",
                "comment_short": "改后的短评",
                "revise_note": "把第三节展开了",
            }, ensure_ascii=False)

    def seed(task_id: str) -> None:
        """把任务摆到"已经生成过初版、且素材笔记还在内存里"的状态。"""
        state.store.record_version(
            task_id, source="initial",
            summary_json={"one_line": "初版一句话", "summary_md": "初版正文",
                          "comment_short": "初版短评", "role": "standard"},
            preview_md="初版预览", comment_text="初版短评", role="standard",
            metrics={"usage": {"calls": 3, "prompt_tokens": 500, "completion_tokens": 60,
                               "total_tokens": 560, "unpriced": 0}, "elapsed": 40.0},
            status="ready", progress="总结已生成",
        )
        state.remember_prepared(
            task_id,
            RefineContext(bundle=bundle, material=PreparedMaterial(body="笔记正文", chunks=3)),
        )

    def previous_of(task_id: str) -> str:
        from bili_scribe.core import summarizer

        return summarizer.build_previous_view(state.store.get_task(task_id)["summary_json"])

    # ---------- 1. 成功：追加第 2 版，并把账记在这一版上 ----------
    tid = state.store.create_task("BV1ver")
    seed(tid)
    llm = FakeLlm()
    state.new_llm = lambda: llm          # 只替换模型，其余全是真的
    assert state.begin_refine(tid) is True

    _run_refine(state, tid, state.get_prepared(tid), "把第三节展开", "standard", previous_of(tid))

    versions = state.store.list_versions(tid)
    assert [v["version_no"] for v in versions] == [1, 2], [v["version_no"] for v in versions]
    new = versions[1]
    assert new["source"] == "refine" and new["requirement"] == "把第三节展开", new
    assert new["summary_json"]["revise_note"] == "把第三节展开了", new["summary_json"]
    # 落库的是这一版**真正会发出去的正文**（组装后的），不是模型给的原始片段
    assert "改后的短评" in new["comment_text"], new["comment_text"]
    assert new["comment_text"] != versions[0]["comment_text"], "新版的正文要和旧版不同"
    assert new["preview_md"] and "改后" in new["preview_md"], new["preview_md"]
    usage = new["metrics"]["usage"]
    assert usage["calls"] == 1 and usage["total_tokens"] == 900, usage
    assert new["metrics"]["chunks"] == 3, "分块数要如实带过来"
    assert new["metrics"].get("elapsed") is not None, "耗时也要记——它是迭代变快的直接证据"
    task = state.store.get_task(tid)
    assert task["status"] == "ready" and "改好" in task["progress"], task["progress"]
    assert task["summary_json"]["one_line"] == "改后的一句话", "任务当前版要指向新那一版"
    assert task["error"] is None, "成功之后不该留着上一次的错误"
    assert len(llm.systems) == 1, f"迭代只该调用一次模型，实际 {len(llm.systems)} 次"
    assert state.is_refining(tid) is False, "跑完必须放开迭代名额，否则再也不能改"

    # ---------- 2. 失败：不落版本，且名额要放开 ----------
    before = len(state.store.list_versions(tid))
    state.new_llm = lambda: FakeLlm(fail=True)   # 换成"这次改不动"的模型
    assert state.begin_refine(tid) is True
    _run_refine(state, tid, state.get_prepared(tid), "再改一次", "standard", previous_of(tid))

    assert len(state.store.list_versions(tid)) == before, "没改成的那一版不该出现在版本史里"
    task = state.store.get_task(tid)
    assert task["status"] == "ready", f"失败后要退回 ready，不能作废已有成果：{task['status']}"
    assert "没改成" in task["progress"] and task["error"], task
    assert task["summary_json"]["one_line"] == "改后的一句话", "上一版的内容必须原样留着"
    assert state.is_refining(tid) is False, "失败也必须放开名额，否则这条视频永远改不动"

    return (
        f"成功追加第 {new['version_no']} 版并记账（{usage['calls']} 次调用 / "
        f"{usage['total_tokens']} tokens / 分块 {new['metrics']['chunks']}）；"
        "失败不落版本、退回 ready、名额照旧放开"
    )


def _refine_survives_three_passes() -> str:
    """三轮回角色下，使用者的修改要求必须活着穿过批判与修订这两轮。

    这是活体验收抓到的真问题：配置里的角色是三轮回（生成 → 自我批判 → 修订），
    而要求"只改 one_line、其余一个字都别动"提交下去，产出却按批判清单改了七处，
    `revise_note` 也变成了"根据诊断清单修订如下：1…7"——使用者的要求被自己的
    质量流程覆盖掉了。

    为什么单测抓不到：假模型不会"自行发挥"，只有看**真实的老版本 + 真实的要求**
    跑一遍真实角色才会暴露。这里的补救是让批判与修订这两轮也能看见那条要求，
    并用假模型钉住"它确实被传下去了"这个事实——否则同一个坑会再踩一次。
    """
    import json as _json
    import types

    from bili_scribe.config import AppConfig
    from bili_scribe.core import summarizer
    from bili_scribe.core.collector import Bundle
    from bili_scribe.core.roles import get_role

    cfg = AppConfig()
    bundle = Bundle(
        bvid="BV1passes", aid=1, cid=2, page=1, page_count=1,
        title="三轮回", desc="", up_name="某人", up_mid=3, duration=600,
        tname="知识", source_kind="subtitle_ai", source_label="平台 AI 字幕",
        source_trust="中", source_detail="测试", body="素材正文",
    )

    role = next(
        r for r in (get_role(k) for k in summarizer.roles.ROLE_KEYS)
        if r.passes >= 3 and r.critique and r.revise
    )
    seen: list[list[dict]] = []

    class FakeLlm:
        cfg = types.SimpleNamespace(model="fake-model")

        def chat(self, messages, **_kw):
            seen.append(messages)
            return (
                '{"one_line":"一句话","summary_md":"正文","key_points":[],'
                '"timeline":[],"caution":"","comment_short":"短评",'
                '"revise_note":"把开头压短了"}'
            )

    requirement = "只把开头那句压到 25 字以内，其余一个字都别动"
    out = summarizer.summarize(
        bundle, FakeLlm(), cfg, role_key=role.key,
        prepared=summarizer.PreparedMaterial(body="素材正文", chunks=1),
        refinement=summarizer.Refinement(
            requirement=requirement, previous='{"summary_md":"上一版"}'
        ),
    )
    assert len(seen) == 3, f"三轮回角色应当调用三次（生成/批判/修订），实际 {len(seen)}"

    gen_user = seen[0][1]["content"]
    crit_user = seen[1][1]["content"]
    rev_user = seen[2][1]["content"]
    assert requirement in gen_user, "生成那一轮没看到要求"
    assert requirement in crit_user, (
        "批判那一轮不知道使用者的要求 —— 它会按角色自己的清单挑毛病，"
        "然后修订照单全收，把使用者已经认可的部分一起改掉"
    )
    assert requirement in rev_user, "修订那一轮不知道使用者的要求"
    for label, text in (("批判", crit_user), ("修订", rev_user)):
        assert "只改他提到的部分" in text, f"{label}那一轮没看到「只改提到的部分」这条边界"
        assert "素材保真" in text, f"{label}那一轮丢了素材保真这条常驻底线"
    assert "revise_note" in rev_user, "修订那轮的改动说明没被要求写成「相对上一版改了什么」"

    # 不是迭代的时候，不能把这段约束贴上去——否则一次普通生成会被要求"只改提到的部分"，
    # 而那时压根没有"上一版"和"要求"。
    seen.clear()
    summarizer.summarize(
        bundle, FakeLlm(), cfg, role_key=role.key,
        prepared=summarizer.PreparedMaterial(body="素材正文", chunks=1),
    )
    assert len(seen) == 3, len(seen)
    for index, messages in enumerate(seen):
        assert "本次修改要求" not in messages[1]["content"], (
            f"普通生成的第 {index + 1} 轮被贴上了迭代约束"
        )
    assert out.revise_note == "把开头压短了", out.revise_note

    return (
        f"角色「{role.name}」三轮回：要求同时进入生成/批判/修订三轮，"
        "「只改提到的部分」与素材保真带到了后两轮；普通生成不受影响"
    )


def _error_detail_points_at_the_frame() -> str:
    """报错必须能指出「哪一行」。守的是一个具体事故。

    迭代曾经在 1.5 秒内失败，库里只留下一句
    `FileNotFoundError(2, 'No such file or directory')`——**连文件名都没有**，
    因为 `{exc!r}` 只打参数、不打 `.filename`（文件名在 `str(exc)` 里）。
    结果是：报错进了库、界面上也显示了，却没有任何线索指向哪一行，
    只能靠手工复现去找——那一次花了半小时。所以现在错误里带出最后两帧的
    「文件:行号 函数名」。

    这条用例同时钉住"文件名不能丢"（当初丢的就是它）和"没有异常栈也不能崩"。

    ⚠️ 造这个异常的方式要紧：**必须真的去 open 一个不存在的文件**。
    手工构造 `FileNotFoundError(2, msg, "路径")` 会把文件名塞进 args，
    于是 `repr()` 里也有它——那样就复现不出"文件名消失"这件事，
    用例会在旧实现上照样通过（第一版就是这么写错的）。
    真的 open 失败时，文件名只存在于 `.filename` 与 `str(exc)` 里，
    `repr(exc)` 只剩 `FileNotFoundError(2, 'No such file or directory')`。
    """
    import tempfile

    from bili_scribe.app.main import _error_detail

    missing = str(Path(tempfile.mkdtemp()) / "不存在的目录" / "0.jpg")

    def inner():
        with open(missing, "wb"):
            pass

    def outer():
        inner()

    try:
        outer()
    except FileNotFoundError as exc:
        text = _error_detail(exc)
    else:
        raise AssertionError("没有抛出异常")

    assert "0.jpg" in text, f"错误里丢了文件名（当初就是这么丢的）：{text}"
    assert "inner" in text and "outer" in text, f"没有指出出错的代码位置：{text}"
    assert "smoke_test.py" in text, f"没有指出出错的文件：{text}"

    # 没有异常栈的异常（比如手工构造的那个）不能让它自己崩掉
    bare = RuntimeError("光秃秃的错误")
    assert _error_detail(bare) == "RuntimeError: 光秃秃的错误", _error_detail(bare)

    return f"带文件名与两帧位置；无栈时不崩：{text[:58]}…"


def _field_diff() -> str:
    """字段级 diff：**不问模型"你改了什么"，直接把相邻两版逐字段比。**

    为什么要这个东西：`revise_note` 是模型自己写的，实测会不准——一次要求写着
    "其余部分一个字都别动"、说明也写着"其他字段均保持原样未动"，而 key_points
    六条里有一条被实质改写了，只有比对才看得出来。

    两条边界要钉住，它们决定了这个功能是信号还是噪音：
    - **只报内容字段**：元信息（chunks / model / frames / role / revise_note）变了
      不算内容改动，混进来会让每次都显示"改了一堆"，等于没信号；
    - **列表项数没变、内容变了也要抓到**（就是上面那个真实例子）。只比项数会漏掉它。
    """
    from bili_scribe.core import summarizer as s

    base = {
        "one_line": "甲", "summary_md": "aaa bbb", "key_points": ["一", "二"],
        "timeline": [], "caution": "c", "comment_short": "s",
        "chunks": 1, "model": "m", "role": "standard", "frames": 12, "revise_note": "旧说明",
    }

    assert s.diff_fields(base, dict(base)) == [], "完全一样却报出了改动"
    assert s.diff_fields(base, dict(base, chunks=5, model="x", frames=0, revise_note="新")) == [], (
        "元信息变动被当成了内容改动——那会让每一次比对都显示「改了一堆」，信号就没了"
    )

    one = s.diff_fields(base, dict(base, one_line="甲甲"))
    assert len(one) == 1 and one[0]["field"] == "one_line", one
    assert one[0]["kind"] == "text" and 0 < one[0]["ratio"] < 1, one[0]
    assert one[0]["example"], "文本改动应当给出一处差异，否则界面上只能看到一个数字"

    swapped = s.diff_fields(base, dict(base, key_points=["一", "二改"]))
    assert [i["field"] for i in swapped] == ["key_points"], swapped
    assert "2 项 → 2 项" in swapped[0]["detail"] and "1 项内容不同" in swapped[0]["detail"], (
        f"项数没变、内容换了，必须点出来：{swapped[0]['detail']}"
    )
    assert "二改" in swapped[0]["example"], swapped[0]

    longer = s.diff_fields(base, dict(base, key_points=["一", "二", "三"]))
    assert "2 项 → 3 项" in longer[0]["detail"], longer[0]["detail"]

    # 顺序固定：界面上不能一会儿一个样
    both = s.diff_fields(base, dict(base, one_line="甲甲", caution="c改"))
    assert [i["field"] for i in both] == ["one_line", "caution"], both

    # 字段缺失/类型不对（旧记录、模型返回乱类型）不能崩
    assert s.diff_fields({}, dict(base)) is not None
    assert s.diff_fields({"key_points": "不是列表"}, {"key_points": ["一"]}) is not None

    return (
        f"只报内容字段（元信息变动不报）；文本给相似度+一处差异；"
        f"列表项数没变也能抓：{swapped[0]['detail']}"
    )


def _prepared_cache_eviction() -> str:
    """预读缓存的淘汰顺序：满了先扔**最久没用**的那条。

    这条守的是一个很容易写反、而且写反了很难看出来的地方。`remember_prepared`
    先 pop 再插，是为了把这条挪到队尾；漏掉那句 pop，它就成了"先进先出"，
    于是表现是——**你刚刚改过的那条视频，过一会儿说缓存没了**——
    没人会往淘汰顺序上想，只会觉得"又出 bug 了"。

    上限本身（200）是量出来的（见 refine-cache-budget.py），这里只把它调小到 2
    来验顺序，免得为了测淘汰去塞 200 条。
    """
    import tempfile

    from bili_scribe.app.main import AppState
    from bili_scribe.config import AppConfig

    tmp = Path(tempfile.mkdtemp())
    cfg = AppConfig()
    cfg.storage.db_path = str(tmp / "t.db")
    cfg.storage.key_path = str(tmp / "k.bin")
    state = AppState(cfg)

    class Fake:
        def __init__(self, name: str):
            self.name = name

    saved = state.MAX_PREPARED
    state.MAX_PREPARED = 2
    try:
        state.remember_prepared("a", Fake("a"))
        state.remember_prepared("b", Fake("b"))
        assert state.get_prepared("a") is not None

        state.remember_prepared("c", Fake("c"))          # 超上限，该挤掉最久没用的 a
        assert state.get_prepared("a") is None, "最久没用的那条没被淘汰"
        assert state.get_prepared("b") is not None and state.get_prepared("c") is not None

        state.remember_prepared("b", Fake("b2"))         # 重存 b → 它排到队尾
        state.remember_prepared("d", Fake("d"))          # 于是这次该挤掉 c
        assert state.get_prepared("c") is None, "重存没让它排到队尾（LRU 写成了先进先出）"
        assert state.get_prepared("b").name == "b2", "重存应当覆盖旧的那条，而不是并排留着"
        assert len(state.prepared) == 2, f"条数应当被压在 2，实际 {len(state.prepared)}"

        # get 不能顺手删掉它：打磨是反复的，取走就没了的话第二轮就没得改了
        state.get_prepared("b")
        assert state.get_prepared("b") is not None, "get_prepared 把条目取走了"
    finally:
        state.MAX_PREPARED = saved

    return "满了先淘汰最久没用的；重存会挪到队尾（不是先进先出）；读不删除"


def _note_cache() -> str:
    """预读笔记的磁盘缓存：地址算得对不对，坏了会不会连累任务。

    这个东西的全部价值在**地址**上。地址漏了某个会影响产出输入的变量，就会
    「素材没变、配置变了，却拿到旧笔记」——产物静默变形，而且没人会怀疑到缓存
    头上。反过来，地址里塞多了不影响笔记的变量，本该命中的场合会全部落空，
    等于白做。两头都要钉住。

    真实账本说明了它存在的理由：`BV1hBnC6ZEug` 被重开成两个独立任务，两次各付
    约 13 万 prompt tokens 的完整预读，而第二遍本可以一分不花。
    """
    import tempfile

    from bili_scribe.infra.notecache import CACHE_VERSION, NoteCache

    tmp = Path(tempfile.mkdtemp())
    cache = NoteCache(tmp / "notes")
    body = "第一段笔记" * 200
    recipe = {"max_input_chars": 24000, "chunk_size": 6000, "chunk_overlap": 400}
    prompt = "你是素材预读员。"

    # ---------- 1. 地址：该变的必须变 ----------
    key = NoteCache.make_key(body, "人工字幕", recipe, prompt)
    assert key == NoteCache.make_key(body, "人工字幕", dict(recipe), prompt), "同样的输入算出了两个地址"
    assert key != NoteCache.make_key(body, "弹幕", recipe, prompt), (
        "素材类型没进地址——同一段文字标成弹幕还是字幕，会拿到同一份笔记"
    )
    assert key != NoteCache.make_key(body, "人工字幕", dict(recipe, chunk_size=8000), prompt), (
        "分块大小没进地址——分段边界变了，笔记却还是旧的"
    )
    assert key != NoteCache.make_key(body, "人工字幕", recipe, prompt + "补充一句要求"), (
        "预读提示词没进地址——指令变了还吃旧笔记，产物会静默按旧指令生成"
    )
    assert key != NoteCache.make_key(body + "多一个字", "人工字幕", recipe, prompt), "素材内容没进地址"

    # ---------- 2. 地址：不该变的必须不变 ----------
    # 模型的加入是最容易犯的错：换了模型确实会得到不同笔记，但那是「换模型后
    # 重新预读」本身该付的钱。把它写进 key 只会让复用率归零。
    assert NoteCache.make_key(body, "人工字幕", recipe, prompt) == key, "地址不稳定"
    assert "qwen" not in key and "model" not in key, "地址里混进了模型信息"

    # ---------- 3. 往返 ----------
    assert cache.get(key) is None, "空缓存不该命中"
    assert cache.misses == 1 and cache.hits == 0, (cache.hits, cache.misses)
    cache.put(key, "笔记正文", 7)
    got = cache.get(key)
    assert got == {"body": "笔记正文", "chunks": 7}, got
    assert cache.hits == 1, f"命中没被记上：{cache.hits}"
    assert (tmp / "notes" / f"{key}.json").exists(), "笔记没落盘"

    # ---------- 4. 坏条目只算未命中，不能让任务挂 ----------
    (tmp / "notes" / f"{key}.json").write_text("{ 不是 json", encoding="utf-8")
    assert cache.get(key) is None, "坏掉的条目被当成命中了"
    (tmp / "notes" / f"{key}.json").write_text('{"v": 1, "body": "x", "chunks": 2}', encoding="utf-8")
    assert cache.get(key) is None, "格式版本对不上却命中了——旧笔记会污染新产物"
    (tmp / "notes" / f"{key}.json").write_text(
        json.dumps({"v": CACHE_VERSION, "body": "", "chunks": 2}), encoding="utf-8"
    )
    assert cache.get(key) is None, "空正文被当成有效笔记"

    # ---------- 5. 淘汰：先扔最久没用的，读一次就算「用过」 ----------
    cache2 = NoteCache(tmp / "evict")
    for name in ("a", "b", "c"):
        cache2.put(name, "正文" + name, 2)
    # 手工把 mtime 拉开：a 最旧，c 最新
    base = time.time() - 1000
    for offset, name in enumerate(("a", "b", "c")):
        path = tmp / "evict" / f"{name}.json"
        os.utime(path, (base + offset * 100, base + offset * 100))
    assert cache2.get("a") is not None, "读一次应当让它变成最近用过的"
    import bili_scribe.infra.notecache as nc

    saved = nc.MAX_ENTRIES
    nc.MAX_ENTRIES = 2
    try:
        cache2.put("d", "正文d", 2)
    finally:
        nc.MAX_ENTRIES = saved
    assert cache2.get("b") is None, "该扔的是最久没用的 b（a 刚被读过）"
    assert cache2.get("a") is not None and cache2.get("d") is not None

    # ---------- 6. 运维：统计与清空 ----------
    stats = cache2.stats()
    assert stats["entries"] == 2, stats
    assert stats["bytes"] > 0 and stats["dir"].endswith("evict"), stats
    assert cache2.clear() == 2, "清空应当返回删掉的条数"
    assert cache2.stats()["entries"] == 0 and cache2.clear() == 0, "清空空目录应当返回 0 而不是报错"
    assert list((tmp / "evict").glob("*.json")) == [], "清完还有残留"

    return "地址覆盖素材/类型/分块参数/提示词、不含模型；坏条目只算未命中；淘汰按最近使用"


def _note_cache_really_saves_tokens() -> str:
    """端到端：同一份素材跑两次，第二次不该再调预读。

    这条比上面那组更接近它存在的理由。上面测地址算得对，这条测**省下来的钱
    确实进了口袋**：第一次要 N 次预读调用，第二次一次都不该有。
    """
    import tempfile

    from bili_scribe.config import AppConfig
    from bili_scribe.core import collector, summarizer
    from bili_scribe.infra.notecache import NoteCache

    tmp = Path(tempfile.mkdtemp())
    cfg = AppConfig()
    # 把阈值压小，让素材「显得很长」，从而真的踩到预读分支
    cfg.llm.max_input_chars = 600
    cfg.llm.chunk_size = 300
    cfg.llm.chunk_overlap = 20

    bundle = collector.Bundle(
        bvid="BVprobe", aid=1, cid=2, page=1, page_count=1,
        title="探针", desc="", up_name="UP", up_mid=3, duration=600, tname="知识",
        source_label="人工字幕", source_kind="subtitle", source_trust="human",
        body="这是一段素材。" * 200,
    )

    class CountingLlm:
        """只认预读提示词；别的一律报错——这条不该走到主生成。"""

        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return "笔记" + str(self.calls) + "：" + "摘" * 50

    cache = NoteCache(tmp / "notes")

    first_llm = CountingLlm()
    first = summarizer.prepare_material(bundle, first_llm, cfg, cache=cache)
    assert first_llm.calls > 1, f"素材没被切成多段，这条测不到东西：{first_llm.calls}"
    assert first.chunks == first_llm.calls, (first.chunks, first_llm.calls)
    assert first.body, "预读没产出正文"

    second_llm = CountingLlm()
    second = summarizer.prepare_material(bundle, second_llm, cfg, cache=cache)
    assert second_llm.calls == 0, f"第二次仍然调了 {second_llm.calls} 次预读，笔记没被复用"
    assert second.body == first.body, "命中缓存拿到的笔记和原来不一致"
    assert second.chunks == first.chunks, (second.chunks, first.chunks)

    # 短素材不预读，也就不该写缓存（写进去只是占地方）
    cfg.llm.max_input_chars = 100000
    short = collector.Bundle(
        bvid="BVshort", aid=1, cid=2, page=1, page_count=1,
        title="短", desc="", up_name="UP", up_mid=3, duration=10, tname="知识",
        source_label="人工字幕", source_kind="subtitle", source_trust="human",
        body="很短。",
    )
    short_llm = CountingLlm()
    got = summarizer.prepare_material(short, short_llm, cfg, cache=cache)
    assert short_llm.calls == 0 and got.chunks == 1, (short_llm.calls, got.chunks)
    assert len(list((tmp / "notes").glob("*.json"))) == 1, "短素材也写了缓存"

    return f"第一次 {first_llm.calls} 次预读 → 第二次 0 次，产物一致"


def _shell_settings() -> str:
    """桌面窗口的 WebView2 开关：右键菜单 + 浏览器快捷键都要打开。

    这是一条**真实用户报障换来的用例**：反馈「粘贴链接无法右键选择粘贴」。
    查下去是 pywebview 把两样都关了，都在
    `webview/platforms/edgechromium.py:287-288`，而且都是 `= _state['debug']`：

        settings.AreBrowserAcceleratorKeysEnabled = _state['debug']
        settings.AreDefaultContextMenusEnabled = _state['debug']

    第一版只补了第二行（右键菜单），用户回来说"还是粘不进去"——因为
    `AreBrowserAcceleratorKeysEnabled` 是 Ctrl+V 的总开关，漏了它连键盘快捷键
    都不响应。**这两条必须一起测，只测一条的话，漏掉另一条同样能全绿。**

    这里用假对象记赋值，不启动 GUI：能这样测才可能进常态回归。
    """

    from bili_scribe.desktop import _apply_shell_settings

    class Recorder:
        def __init__(self) -> None:
            object.__setattr__(self, "log", {})

        def __setattr__(self, key: str, value: object) -> None:
            object.__getattribute__(self, "log")[key] = value

    rec = Recorder()
    done = _apply_shell_settings(rec)
    log = rec.log

    # 右键菜单：没有它，右键一片空白，用户以为输入框坏了
    assert log.get("AreDefaultContextMenusEnabled") is True, "右键菜单没开"
    # Ctrl+V / Ctrl+C / Ctrl+A 的总开关：漏了这条，粘不进去
    assert log.get("AreBrowserAcceleratorKeysEnabled") is True, (
        "浏览器快捷键没开 —— Ctrl+V 会完全没反应，用户报的『粘不进去』就是这个"
    )
    # 状态栏留着会在左下角浮出 javascript:void(0) 之类的地址条，不是应用该有的
    assert log.get("IsStatusBarEnabled") is False, "不该打开状态栏"

    # 打开右键菜单不能顺带放出开发者工具：那是两回事，放出来就等于给窗口
    # 装了个"查看网页源码/改样式"的后门，对最终用户没有意义。
    assert "AreDevToolsEnabled" not in log, "不该碰开发者工具开关"
    assert "IsScriptEnabled" not in log, "不该碰脚本开关"
    assert "AreDefaultScriptDialogsEnabled" not in log, "不该碰脚本对话框开关"

    # 返回的项名要如实反映设了什么：调用方靠它打日志，日志是打包后唯一的证据
    assert set(done) == {"AreDefaultContextMenusEnabled", "AreBrowserAcceleratorKeysEnabled"}, done

    # ---- 读回：赋值成功 ≠ 值还在 -------------------------------------
    # 真实事故：日志写了「已打开」，右键还是没菜单。因为 pywebview 的
    # `on_webview_ready` 随后又用 `_state['debug']`(=False) 写了一遍，
    # 谁先谁后看时序。所以必须能读回**当前实际值**，否则日志本身就是假的。
    from bili_scribe.desktop import _read_shell_settings

    class Sticky:
        def __init__(self) -> None:
            self.AreDefaultContextMenusEnabled = True
            self.AreBrowserAcceleratorKeysEnabled = True
            self.IsStatusBarEnabled = False

    assert _read_shell_settings(Sticky()) == {
        "AreDefaultContextMenusEnabled": True,
        "AreBrowserAcceleratorKeysEnabled": True,
    }, "读回没拿到实际值"

    class Swallowed:
        """写进去被吞掉、读出来永远是 False —— 模拟被后写的一方盖回去。"""

        def __init__(self) -> None:
            self._flag = False

        @property
        def AreDefaultContextMenusEnabled(self) -> bool:
            return self._flag

        @AreDefaultContextMenusEnabled.setter
        def AreDefaultContextMenusEnabled(self, value: object) -> None:
            pass

        @property
        def AreBrowserAcceleratorKeysEnabled(self) -> bool:
            return self._flag

        @AreBrowserAcceleratorKeysEnabled.setter
        def AreBrowserAcceleratorKeysEnabled(self, value: object) -> None:
            pass

        @property
        def IsStatusBarEnabled(self) -> bool:
            return self._flag

        @IsStatusBarEnabled.setter
        def IsStatusBarEnabled(self, value: object) -> None:
            pass

    swallowed = Swallowed()
    _apply_shell_settings(swallowed)
    state = _read_shell_settings(swallowed)
    assert not any(state.values()), (
        "被盖回去的情况没被识别出来 —— 调用方会以为开好了，日志会撒谎"
    )

    # ---- 接管 on_webview_ready：exe 里唯一真正生效的写法 ----------------
    # 上面两条只是「设了、读回 True」。实测 0.7.3 前三轮：读回真、右键假。
    # 真正让 exe 出菜单的是把开关写进 pywebview 自己的 `sender`（= 真在渲染
    # 页面的那个控件）。这条用例守的是「补丁装上了、且能重复调用不出错」，
    # 防止以后重构把这行悄悄删掉——删了它测试照样全绿，但用户右键照样没菜单。
    try:
        from webview.platforms import edgechromium

        from bili_scribe.desktop import _patch_webview_settings_hook

        _patch_webview_settings_hook()
        _patch_webview_settings_hook()  # 幂等：第二次应直接返回
        hooked = edgechromium.EdgeChrome.on_webview_ready
        assert getattr(hooked, "_bili_patched", False), "on_webview_ready 没被接管"
        assert hooked.__name__ == "patched", "接管后应指向包装函数"
    except ImportError:  # 非 Windows / 没装 pywebview 的环境跳过
        pass

    return f"设 {len(done)} 项：{' / '.join(done)}；读回能识别「写了但没留住」；已接管 sender"


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
check("代理只来自配置、不来自环境变量", _llm_proxy)
check("瞬时断链重试、真故障不重试", _llm_transient_retry)
check("全片抽帧", _frames_module)
check("画面文件的生死", _frames_own_their_dir)
check("画面接进了总结请求", _frames_wired)
check("使用者上传的 srt", _user_subtitle)
check("上传件的临存与清理", _uploads_lifecycle)
check("上传解析不阻塞事件循环", _subtitle_parse_offload)
check("上传入口可辨识", _upload_entry_visible)
check("迭代复用预读", _refine_reuses_prepared)
check("字段级 diff", _field_diff)
check("预读缓存的淘汰顺序", _prepared_cache_eviction)
check("预读笔记的磁盘缓存", _note_cache)
check("同一份素材跑两次只付一次预读", _note_cache_really_saves_tokens)
check("版本史落库与回退", _version_history)
check("token 记账与汇总", _llm_usage)
check("迭代落库成新一版", _refine_writes_version)
check("迭代要求穿过三轮回", _refine_survives_three_passes)
check("角色注册表", _roles_registry)
check("超强思考模式总结的四问与文体纪律", _role_shensi)
check("发布长文与 Delta 转换", _publish_document)
check("桌面窗口的右键与快捷键开关", _shell_settings)


def _desktop_role_bridge() -> str:
    """桌面桥要暴露 set_role，且行为与 HTTP 那条路一致。

    网页版和桌面版共用同一份 `index.html`，前端都写 `call("set_role", {...})`：
    网页版打到 `POST /api/role`，桌面版落到 `DesktopApi.set_role`。**只做一边的话，
    另一个形态点了没反应，而且从界面上完全看不出来**——这正是"两套入口必须一起测"
    的典型场合（和右键菜单那两个开关同一类问题）。

    顺带钉住两条：
    - 未知角色返回 400 且**不**改动配置文件（半路失败不能留下半个状态）
    - 空请求体走 Pydantic 校验返回 422，而不是炸成 500
    """
    import shutil
    import tempfile

    from bili_scribe.app.service import Service
    from bili_scribe.config import AppConfig
    from bili_scribe.desktop import DesktopApi

    tmp = Path(tempfile.mkdtemp(prefix="bs-desk-role-"))
    try:
        cfg_path = tmp / "config.yaml"
        shutil.copy2(PROJECT / "config.example.yaml", cfg_path)
        cfg = AppConfig.load(cfg_path)
        cfg.source_path = str(cfg_path)
        cfg.storage.db_path = str(tmp / "t.db")
        cfg.storage.key_path = str(tmp / "k.bin")
        api = DesktopApi(Service(cfg))

        assert hasattr(api, "set_role"), (
            "DesktopApi 没有 set_role —— 桌面版点角色没反应，网页版却正常"
        )
        # 名字以下划线开头的话 pywebview 不会把它暴露给 JS
        assert not "set_role".startswith("_"), "pywebview 不会暴露下划线开头的方法"

        ok = api.set_role({"role": "xueshu"})
        assert ok["ok"] is True, ok
        assert ok["data"]["role"] == "xueshu", ok["data"]
        assert ok["data"]["passes"] == 3, ok["data"]
        # 必须真的落盘：只改内存的话重启又变回去，用户会以为选择器是摆设
        assert AppConfig.load(cfg_path).llm.role == "xueshu", "桌面版没把角色写进配置文件"

        bad = api.set_role({"role": "bogus"})
        assert bad["ok"] is False, bad
        assert bad.get("status") == 400, bad
        assert "bogus" in bad["error"], bad["error"]
        assert AppConfig.load(cfg_path).llm.role == "xueshu", "被拒绝的请求把角色改坏了"

        empty = api.set_role({})
        assert empty["ok"] is False, empty
        assert empty.get("status") == 422, empty
        assert "role" in empty["error"], f"422 的提示要点出是哪个字段：{empty['error']}"

        return "桌面桥 set_role 可用：写盘 / 400 不改状态 / 422 点出字段"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _desktop_config_falls_back_when_exe_dir_readonly() -> str:
    """exe 旁边写不进去时，配置必须退到用户目录。

    `_default_config()` 原本的规则是"取第一个存在的，都不存在则取第一个候选"。
    便携版没问题，但**装机版会踩坑**：把 exe 拖进 `Program Files\\`、或者从压缩包
    里直接双击（解压在 `%TEMP%` 下的临时目录），exe 旁边就是不可写的。程序照旧
    选中那个位置，用户认真填完 api_key 点保存，拿到的是

        配置写入失败：[Errno 13] Permission denied: '...\\config.yaml.tmp'

    看着像程序坏了，其实只是落脚点选错了——而这恰好是"只发一个 exe 给对方"
    最常见的用法，所以必须钉住。

    测法是替换 `_config_candidates()`：不依赖这台机器的实际权限（以管理员身份
    跑测试时 `Program Files` 也能写，靠真实权限判会时灵时不灵），直接给一个
    必然不可写的目录——它是**文件占位**，`is_dir()` 为假。
    """
    import tempfile

    from bili_scribe import desktop

    tmp = Path(tempfile.mkdtemp(prefix="bs-desk-cfg-"))
    saved = desktop._config_candidates
    saved_frozen = desktop._frozen
    saved_default_home = desktop.DEFAULT_HOME
    try:
        # 占位文件冒充"只读目录"：路径存在，但不是目录，往里写必然失败
        readonly = tmp / "ProgramFiles-as-file"
        readonly.write_bytes(b"")
        writable = tmp / "home"
        writable.mkdir()

        desktop._config_candidates = lambda: [
            readonly / "config.yaml",
            writable / "config.yaml",
        ]
        got = desktop._default_config()
        assert got == writable / "config.yaml", (
            f"exe 旁边写不进去时没退到用户目录，而是选了 {got} —— "
            "对方填完 key 会撞上 PermissionError"
        )

        # 反过来：exe 旁边可写时仍要优先它，便携版的心智模型不能丢
        desktop._config_candidates = lambda: [
            writable / "config.yaml",
            writable / "sub" / "config.yaml",
        ]
        got2 = desktop._default_config()
        assert got2 == writable / "config.yaml", (
            f"可写时没优先 exe 旁边：{got2} —— 便携版拷走 exe 就该能带走配置"
        )

        # 已有的文件优先于"可写性"：用户明确放在 exe 旁边那份不能被忽略。
        # 注意这份要真的能存在——把父路径做成文件的话，路径本身就不可能存在，
        # 测到的就不是"已存在优先"而是"路径非法"了。
        existing_dir = tmp / "exe-dir"
        existing_dir.mkdir()
        existing = existing_dir / "config.yaml"
        existing.write_text("llm:\n  model: demo\n", encoding="utf-8")
        desktop._config_candidates = lambda: [existing, writable / "config.yaml"]
        got3 = desktop._default_config()
        assert got3 == existing, (
            f"已存在的配置没被优先采用：{got3} —— 会读不到用户自己写的那份"
        )

        return "配置落脚点：只读退用户目录 / 可写优先 exe 旁 / 已存在优先"
    finally:
        desktop._config_candidates = saved
        desktop._frozen = saved_frozen
        desktop.DEFAULT_HOME = saved_default_home
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def _skin_switch() -> str:
    """换肤必须是"真的换了一套"，而且三个副本必须一致。

    这件事的失败方式很阴：换肤代码本身没 bug，是**同一个事实被抄在了三个地方**——
    CSS 变量块（`[data-skin="exhibition"]`）、前端清单（`SKINS`）、后端白名单
    （`SKIN_KEYS`）、窗口底色（`SKIN_BG`）。少改一处，症状分别是：

    - 后端白名单漏了 → 点皮肤报 400，看着像"这个皮肤不存在"
    - CSS 漏一个变量 → 那处颜色**静默**退回上一层的值，肉眼看只是"有点不对"
    - 窗口底色漏了 → 启动瞬间闪一下另一种底色
    - 前端清单漏了 → 皮肤在 CSS 里能用，但界面上点不到

    四种都不会报错，所以只能靠这条测试钉住。检查项：

    1. 两个变量块都存在，且 exhibition 覆盖的变量在 `:root` 里**都有定义**
       （反过来就是拼错变量名——CSS 里拼错不报错，只是那行失效）
    2. 每套皮肤的文字色 `--text` / `--text-2` / `--acid-tx` 对它自己的 `--bg`
       都 ≥4.5:1。这是换肤最容易翻车的地方：荧光黄绿在近白底上只有 1.54:1，
       直接把 `--acid` 当文字用，换过去就是一整屏读不出来的字
    3. 后端 `SKIN_KEYS`、窗口 `SKIN_BG`、前端 `SKINS` 三份清单的键集合完全相同
    4. 桌面桥暴露 `set_skin`（否则桌面版点皮肤没反应，网页版正常）
    5. `<head>` 里那段预置脚本在 `<body>` **之前**，否则浅色皮肤会先画一帧深色
    """
    import re

    from bili_scribe import desktop
    from bili_scribe.config import SKIN_KEYS
    from bili_scribe.desktop import DesktopApi

    html = (PROJECT / "bili_scribe" / "app" / "static" / "index.html").read_text(encoding="utf-8")

    def var_block(selector: str) -> dict[str, str]:
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
        assert m, f"找不到 {selector} 变量块 —— 这套皮肤不存在"
        out: dict[str, str] = {}
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1)):
            out[name] = value.strip()
        return out

    root = var_block(":root")
    light = var_block('[data-skin="exhibition"]')
    assert light, "浅色皮肤变量块是空的"

    # 1) 覆盖的每个变量都得在 :root 里有定义，否则就是拼错了名字
    stray = sorted(set(light) - set(root))
    assert not stray, (
        f"浅色皮肤里这些变量在 :root 中不存在：{stray} —— "
        "CSS 变量拼错不会报错，只是那处样式静默失效"
    )
    # 换肤必须翻的几档，漏一个就是"只换了一半"
    must_flip = [
        "--void", "--bg", "--panel", "--panel-2",
        "--acid", "--acid-d", "--ember", "--paper",
        "--text", "--muted", "--dim", "--ink-rgb",
    ]
    missing = [v for v in must_flip if v not in light]
    assert not missing, f"浅色皮肤没覆盖这些必须反相的变量：{missing}"

    # 2) 每套皮肤自己的文字对比度
    def rgb(token: str, table: dict[str, str]) -> tuple[int, int, int]:
        token = token.strip()
        m = re.fullmatch(r"var\((--[\w-]+)\)", token)
        if m:
            token = table.get(m.group(1), "")
            assert token, f"变量 {m.group(1)} 在这套皮肤里取不到值"
            m2 = re.fullmatch(r"#([0-9a-fA-F]{6})", token.strip())
            assert m2, f"变量 {m.group(1)} 的值 {token!r} 不是六位色值，算不了对比度"
        short = re.fullmatch(r"#([0-9a-fA-F]{3})", token)
        if short:
            token = "#" + "".join(c * 2 for c in short.group(1))
        m3 = re.fullmatch(r"#([0-9a-fA-F]{6})", token)
        assert m3, f"认不出的颜色写法：{token!r}"
        h = m3.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def lum(token: str, table: dict[str, str]) -> float:
        def ch(v: int) -> float:
            s = v / 255
            return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

        r, g, b = rgb(token, table)
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)

    def contrast(fg: str, bg: str, table: dict[str, str]) -> float:
        a, b = lum(fg, table), lum(bg, table)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    report_lines = []
    for label, table in (("ark", root), ("exhibition", {**root, **light})):
        bg = table["--bg"]
        for name in ("--text", "--text-2", "--acid-tx"):
            c = contrast(table[name], bg, table)
            assert c >= 4.5, (
                f"{label} 皮肤的 {name}（{table[name]}）压在 --bg（{bg}）上只有 "
                f"{c:.2f}:1，正文读不清。换肤最常见的翻车就是把实心色块用的荧光色"
                f"直接拿去当文字色——它在浅底上只有 1.5:1"
            )
        report_lines.append(f"{label} 正文 {contrast(table['--text'], bg, table):.1f}:1")

    # 3) 三份清单必须是同一个集合
    # 只从 SKINS 数组里取，不全文找 `key:`——那样会把无关对象也算进来，
    # 以后随手加个 {key: "..."} 的配置项就会让这条测试假红。
    skins_src = re.search(r"const SKINS\s*=\s*\[(.*?)\n\];", html, re.S)
    assert skins_src, "前端 SKINS 数组读不出来"
    front = set(re.findall(r'\bkey:\s*"([\w-]+)"', skins_src.group(1)))
    assert front, "前端 SKINS 清单是空的"
    assert front == set(SKIN_KEYS), (
        f"前端皮肤清单 {sorted(front)} 与后端 SKIN_KEYS {sorted(SKIN_KEYS)} 不一致 —— "
        "后端多一个会点了报 400，前端多一个会点了没反应"
    )
    assert set(desktop.SKIN_BG) == set(SKIN_KEYS), (
        f"窗口底色 SKIN_BG {sorted(desktop.SKIN_BG)} 与 SKIN_KEYS "
        f"{sorted(SKIN_KEYS)} 不一致 —— 缺的那套皮肤启动时会闪一下别的底色"
    )
    # 底色还得和 CSS 里那套皮肤的 --bg 逐字相同，否则就是上面那个"闪一下"
    for key in SKIN_KEYS:
        table = root if key == "ark" else {**root, **light}
        assert desktop.SKIN_BG[key].upper() == table["--bg"].upper(), (
            f"{key} 的窗口底色 {desktop.SKIN_BG[key]} 与 CSS 的 --bg "
            f"{table['--bg']} 不同 —— 窗口出现到页面画完之间会闪一下"
        )

    # 4) 桌面桥
    assert hasattr(DesktopApi, "set_skin"), (
        "DesktopApi 没有 set_skin —— 桌面版点皮肤没反应，网页版却正常"
    )

    # 5) 预置脚本必须在 <body> 之前：晚一步就要先画一帧默认皮肤
    head_end = html.find("</head>")
    assert head_end > 0, "找不到 </head>"
    # 从 head 结束处往后找 body：CSS/JS 的注释里会出现 "<body>" 字样，
    # 全局 find 会命中注释而不是真标签（这条测试第一版就是这么假红的）。
    body_start = html.find("<body", head_end)
    assert body_start > head_end, "找不到 </head> 之后的 <body>"
    head = html[:head_end]
    assert 'data-skin' in head, (
        "`<head>` 里没有设置 data-skin 的同步脚本 —— 浅色皮肤会先按深色画一帧再跳"
    )
    assert "skin=" in head and "URLSearchParams" in head, (
        "`<head>` 的预置脚本没有读 skin —— 桌面版拿不到配置里存的皮肤，"
        "只能等一次接口往返，那一瞬就是启动闪动"
    )
    # 6) 桌面版必须走 hash 而不是查询串。
    #    `file://...index.html?skin=ark` 里的 `?skin=ark` 会被 WebView2 当成文件名
    #    的一部分，窗口里直接是 ERR_FILE_NOT_FOUND —— 而那张报错页颜色数上万，
    #    probe_exe 的"颜色数够多"判据会心安理得地放它过去。这个坑真踩过。
    assert "location.hash" in head, (
        "`<head>` 没有读 location.hash —— 桌面版加载的是 file:// URI，"
        "查询串会被当成文件名，窗口里只会显示 ERR_FILE_NOT_FOUND"
    )
    assert "#skin=" in (PROJECT / "bili_scribe" / "desktop.py").read_text(encoding="utf-8"), (
        "desktop.py 没有用 `#skin=` 传皮肤 —— 用 `?skin=` 会让 file:// 加载失败，"
        "窗口里是一张白底报错页"
    )

    return "两套皮肤变量齐备；" + " / ".join(report_lines) + "；三份清单一致；预置脚本在 head 内"


check("桌面桥暴露 set_role", _desktop_role_bridge)
check("换肤：变量齐备、对比度达标、三份清单一致", _skin_switch)
check("exe 旁不可写时配置退到用户目录", _desktop_config_falls_back_when_exe_dir_readonly)
check("报错指出出错的那一行", _error_detail_points_at_the_frame)


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
    # 探针必须直连 localhost。如果让 requests 读环境变量里的代理，请求会被送去
    # 让那个代理转发到 127.0.0.1——而代理通常连不回来，测试就随启动它的 shell
    # 变化。被测对象是应用，不是 shell 的环境。（这和 LlmClient 关掉 trust_env
    # 是同一个原因，见 infra/llm.py。）
    probe = requests.Session()
    probe.trust_env = False
    probes = [
        ("GET", "/", 15, 200),
        ("GET", "/api/config", 10, 200),
        ("GET", "/api/auth/status", 25, 200),
        ("GET", "/api/tasks", 10, 200),
        ("GET", "/api/publishes", 10, 200),
        # ⚠️ 这一项是整套探测里**唯一真的会调用外部模型服务**的（走 llm.proxy）。
        # 所以超时给得比别处宽：40 秒曾在一次网络波动里误报成失败，看起来像
        # "代码坏了"，实际只是 aihubmix 那一刻慢。实测正常是 1.5-6.5 秒。
        ("GET", "/api/llm/check", 120, 200),
        ("GET", "/api/auth/qrcode", 40, 200),
        ("GET", "/api/task/nonexistent", 10, 404),
        ("POST", "/api/publish", 10, 422),
        # 上传接口：一个 JSON 空对象不是 srt，必须是 400（而不是 500）
        ("POST", "/api/subtitle/parse", 10, 400),
        # 拿一个合法但没存过的 token 去清除：应当 200 + removed=0，而不是报错
        ("DELETE", "/api/subtitle/not-a-real-token", 10, 200),
    ]
    for method, path, timeout, expected in probes:
        try:
            if method == "GET":
                resp = probe.get(base + path, timeout=timeout)
            elif method == "DELETE":
                resp = probe.delete(base + path, timeout=timeout)
            else:
                resp = probe.post(base + path, json={}, timeout=timeout)
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
        # 版本号必须由后端给出，界面才能显示"我现在跑的是哪一版"。
        # 断言它等于包版本——**这样它就不可能和 __version__ 漂开**，
        # 而不是只断言"字段存在"（存在但写死一个旧数字是最坏的情况）。
        import bili_scribe

        assert data.get("version") == bili_scribe.__version__, (
            f"config 里的版本 {data.get('version')!r} 与包版本 {bili_scribe.__version__!r} 不一致"
        )
        return f"features.qr_login={features['qr_login']}，version={data['version']}"

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

        # —— 角色切换（顶栏那个独立选择器） ——
        #
        # 它走的是 POST /api/role，而不是复用 PUT /api/settings：改一个下拉框不该
        # 让明文 api_key 再在请求体里走一遍。所以这条链路要单独验，包括"真的写盘"
        # ——只改内存的话，重启后会悄悄变回旧角色，用户会以为选择器坏了。

        def _role_switch() -> str:
            before = requests.get(base + "/api/config", timeout=10).json()["llm"]["role"]
            r = requests.post(base + "/api/role", json={"role": "xueshu"}, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            body = r.json()
            assert body["role"] == "xueshu", body
            assert body["name"], f"没返回可读名字：{body}"
            assert body["passes"] == 3, f"xueshu 是三轮回，实际 passes={body['passes']}"

            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["llm"]["role"] == "xueshu", pub["llm"]["role"]
            # 关键：必须落到磁盘上。AppConfig.load 是全新解析一遍文件。
            reloaded = AppConfig.load(cfg_path)
            assert reloaded.llm.role == "xueshu", (
                f"角色没有写进配置文件，重新装载得到 {reloaded.llm.role!r}"
            )
            return f"{before} → xueshu（{body['passes']} 轮），已写盘"

        run("POST /api/role 切换并写盘", _role_switch)

        def _role_unknown() -> str:
            r = requests.post(base + "/api/role", json={"role": "bogus"}, timeout=15)
            assert r.status_code == 400, f"期望 400，实际 {r.status_code}"
            detail = r.json()["detail"]
            assert "bogus" in detail, f"错误信息没点出是哪个值：{detail}"
            # 报错时要给出可选清单，否则用户只知道"错了"、不知道能填什么。
            for key in ("wengao", "standard"):
                assert key in detail, f"错误信息里缺可选项 {key}：{detail}"
            # 被拒绝的请求不能把已有角色改坏。
            still = requests.get(base + "/api/config", timeout=10).json()
            assert still["llm"]["role"] == "xueshu", still["llm"]["role"]
            return "400 · " + detail[:44] + "…"

        run("POST /api/role 拒绝未知角色并列出可选项", _role_unknown)

        def _role_empty() -> str:
            r = requests.post(base + "/api/role", json={"role": ""}, timeout=15)
            assert r.status_code == 422, f"期望 422，实际 {r.status_code}"
            r2 = requests.post(base + "/api/role", json={}, timeout=15)
            assert r2.status_code == 422, f"缺字段也应当 422，实际 {r2.status_code}"
            return "422 · 空角色与缺字段都被挡在字段校验"

        run("POST /api/role 空值/缺字段返回 422", _role_empty)

        def _role_survives_settings_save() -> str:
            # 用户在顶栏换了角色，随后进设置页改别的项。保存提交的是整份设置，
            # 只要前端把 llm.role 带上了，角色就不该被重置。
            cur = requests.get(base + "/api/settings", timeout=15).json()["values"]
            assert cur["llm"]["role"] == "xueshu", (
                f"/api/settings 读到的角色是 {cur['llm']['role']!r}，"
                "与刚设的 xueshu 不一致 —— 设置页会把它写回旧值"
            )
            cur["confirm_disable_gate"] = False
            cur["llm"]["timeout_seconds"] = 111
            r = requests.put(base + "/api/settings", json=cur, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["llm"]["role"] == "xueshu", pub["llm"]["role"]
            assert pub["llm"]["timeout_seconds"] == 111, pub["llm"]["timeout_seconds"]
            return "改别的设置不影响角色"

        run("改别的设置不会把角色重置", _role_survives_settings_save)

        def _role_in_config_public() -> str:
            # 顶栏 chip 靠 /api/config 里的 llm.role 显示当前角色。
            # 它必须在这个"公开"视图里，同时又不能把 api_key 带出来。
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert "role" in pub["llm"], f"公开视图里没有 role：{sorted(pub['llm'])}"
            assert "api_key" not in pub["llm"], "公开视图泄露了 api_key"
            return f"llm.role={pub['llm']['role']} 可见，api_key 不可见"

        run("公开视图带 role 且不带 api_key", _role_in_config_public)

        # —— 皮肤切换 ——
        #
        # 跟「总结角色」同一条路子，所以这里要验的也是同一件事：**真的写盘了**。
        # 只改内存的话，界面当场换了、下次启动又变回去，会被当成玄学 bug。
        # 另外两条是这条链路特有的：皮肤名写错必须 400 并列出可选项（默默退回
        # 默认值会让界面变成一片白板而配置看着正常），以及保存整份设置时不能
        # 把刚选的皮肤带成旧值。

        def _skin_switch() -> str:
            before = requests.get(base + "/api/config", timeout=10).json()["ui"]["skin"]
            r = requests.post(base + "/api/skin", json={"skin": "exhibition"}, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            body = r.json()
            assert body["ok"] is True, body
            assert body["skin"] == "exhibition", body
            assert body["path"], f"没返回写到了哪个文件：{body}"

            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["ui"]["skin"] == "exhibition", pub["ui"]
            reloaded = AppConfig.load(cfg_path)
            assert reloaded.ui.skin == "exhibition", (
                f"皮肤没有写进配置文件，重新装载得到 {reloaded.ui.skin!r} —— "
                "界面当场换了但重启会变回去"
            )
            return f"{before} → exhibition，已写盘"

        run("POST /api/skin 切换并写盘", _skin_switch)

        def _skin_unknown() -> str:
            r = requests.post(base + "/api/skin", json={"skin": "exibition"}, timeout=15)
            assert r.status_code == 400, f"期望 400，实际 {r.status_code}"
            detail = r.json()["detail"]
            assert "exibition" in detail, f"错误信息没点出是哪个值：{detail}"
            # 拼错一个字母是最常见的情形，报错必须给出可选清单。
            for key in ("ark", "exhibition"):
                assert key in detail, f"错误信息里缺可选项 {key}：{detail}"
            still = requests.get(base + "/api/config", timeout=10).json()
            assert still["ui"]["skin"] == "exhibition", still["ui"]["skin"]
            return "400 · " + detail[:44] + "…"

        run("POST /api/skin 拒绝未知皮肤并列出可选项", _skin_unknown)

        def _skin_survives_settings_save() -> str:
            cur = requests.get(base + "/api/settings", timeout=15).json()["values"]
            assert cur.get("ui", {}).get("skin") == "exhibition", (
                f"/api/settings 读到的皮肤是 {cur.get('ui')!r}，与刚设的不一致 —— "
                "设置页会把它写回旧值"
            )
            cur["confirm_disable_gate"] = False
            cur["llm"]["timeout_seconds"] = 222
            r = requests.put(base + "/api/settings", json=cur, timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            pub = requests.get(base + "/api/config", timeout=10).json()
            assert pub["ui"]["skin"] == "exhibition", pub["ui"]["skin"]
            assert pub["llm"]["timeout_seconds"] == 222, pub["llm"]["timeout_seconds"]
            return "改别的设置不影响皮肤"

        run("改别的设置不会把皮肤重置", _skin_survives_settings_save)

        # —— 历史总结索引 ——
        #
        # 这个接口存在的唯一理由是**不要正文**。拿 list_tasks 当历史列表用，
        # 一百条就是近 1 MB 的载荷，而其中 99% 的正文用户根本不会点开。
        # 所以这里要钉住的不是「能返回」，而是「返回的东西里没有正文」。

        def _history_shape() -> str:
            r = requests.get(base + "/api/history?limit=5", timeout=15)
            assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
            rows = r.json()
            assert isinstance(rows, list), type(rows)
            need = {"id", "title", "bvid", "up_name", "status", "created_at",
                    "updated_at", "one_line", "role", "version_count",
                    "current_version", "has_summary", "error"}
            if rows:
                got = set(rows[0])
                assert need <= got, f"缺字段：{sorted(need - got)}"
                # 正文与其它重字段都不该出现。summary_json 一旦混进来，
                # 这个接口就退化成 list_tasks 了。
                for bad in ("summary_json", "preview_md", "comment_text", "platform_summary"):
                    assert bad not in got, f"索引里混进了 {bad}"
            return f"{len(rows)} 条，字段齐全且不含正文"

        run("GET /api/history 只给索引，不给正文", _history_shape)

        def _history_cheaper() -> str:
            # 用真实库量一次：同样的条数下，索引接口的载荷必须更小。
            # 这条会随数据增长而变化，所以只断言比例，不断言绝对字节。
            idx = requests.get(base + "/api/history?limit=50", timeout=20).content
            full = requests.get(base + "/api/tasks?limit=50", timeout=20).content
            assert len(idx) < len(full), (
                f"索引 {len(idx)}B 竟然不比全文 {len(full)}B 小，说明没起到作用"
            )
            return f"索引 {len(idx) // 1024}KB vs 全文 {len(full) // 1024}KB"

        run("历史索引的载荷小于任务全文", _history_cheaper)

        def _history_version_counts() -> str:
            # 版本数用子查询而不是 JOIN：JOIN 会让「还没有任何版本」的任务
            # 整条从列表里消失，而那恰恰是最需要被看见的（刚跑完、初版还没落库）。
            rows = requests.get(base + "/api/history?limit=300", timeout=20).json()
            if not rows:
                return "库是空的，跳过"
            bad = [r for r in rows if r["version_count"] < 0]
            assert not bad, f"版本数是负数：{bad[:2]}"
            nover = [r for r in rows if r["version_count"] == 0]
            assert len(rows) > len(nover), "所有任务都没有版本，这条断言失去意义"
            for r in rows:
                if r["version_count"]:
                    assert r["current_version"] is not None, f"{r['id']} 有版本却算不出当前版"
                    assert r["current_version"] >= 1, r["current_version"]
                else:
                    assert r["current_version"] is None, f"{r['id']} 没有版本却给了当前版"
            return f"{len(rows)} 条里 {len(nover)} 条还没有版本，仍留在列表里"

        run("版本数用子查询算，没有版本的任务不会消失", _history_version_counts)

        def _history_failed_kept() -> str:
            # 失败的任务必须留在历史里。用户回去翻历史的动机里，
            # 「上次为什么没成」占很大一部分，藏起来等于逼人查日志。
            rows = requests.get(base + "/api/history?limit=300", timeout=20).json()
            failed = [r for r in rows if r["status"] == "failed"]
            if not failed:
                return "库里没有失败记录，跳过"
            assert all(r["id"] for r in failed), "失败记录缺 id"
            return f"{len(failed)} 条失败记录仍在列表里"

        run("失败的任务留在历史里", _history_failed_kept)

        def _history_limit_clamped() -> str:
            # limit 要夹住：不夹的话 ?limit=100000 就是一次全库扫描。
            # 服务层把上限设在 300。
            big = requests.get(base + "/api/history?limit=100000", timeout=20)
            assert big.status_code == 200, big.status_code
            n_big = len(big.json())
            assert n_big <= 300, f"limit 没夹住，返回了 {n_big} 条"
            # limit=0 走 max(1, ...) 那条路，应当返回至少 1 条而不是空列表。
            zero = requests.get(base + "/api/history?limit=0", timeout=15)
            assert zero.status_code == 200, f"limit=0 不该报错：{zero.status_code}"
            assert len(zero.json()) >= 1, "limit=0 被当成 0 条了，应当是至少 1 条"
            return f"limit=100000 → {n_big} 条（上限 300）；limit=0 → {len(zero.json())} 条"

        run("limit 被夹在合理区间", _history_limit_clamped)

        def _history_store_layer() -> str:
            # 直接打 store：确认 SQL 层就没把 summary_json 读出来，
            # 而不是靠上层 pop 掉——pop 掉的话磁盘 IO 和解析开销照付。
            store = app.state.service.state.store

            rows = store.list_task_index(limit=5)
            if not rows:
                return "库是空的，跳过"
            for r in rows:
                assert "summary_json" not in r, "store 层把正文读出来了"
            # 对照：list_tasks 是带正文的，两条路径必须真的不同。
            full = store.list_tasks(limit=1)
            if full:
                assert "summary_json" in full[0], (
                    "list_tasks 居然不带正文了——那「索引载荷更小」那条断言就失去意义"
                )
            return f"索引 {len(rows)} 条无正文，list_tasks 仍带正文"

        run("store 层就没读正文", _history_store_layer)




        def _untouched() -> str:
            now = real_config.read_bytes() if real_config.exists() else None
            assert now == real_before, "测试改动了项目里的 config.yaml"
            return "项目 config.yaml 未被触碰"

        run("未触碰项目 config.yaml", _untouched)

    finally:
        server.should_exit = True
        shutil.rmtree(tmp, ignore_errors=True)


# 这一整套 HTTP 探测都打本机 127.0.0.1。如果让 requests 去读环境变量里的代理，
# 请求会被送去让代理转发回 localhost——而代理通常连不回来，于是测试结果随
# 启动它的那个 shell 变化（实测踩过：一个指向别处的 HTTP_PROXY 让 /api/llm/check
# 在 40 秒后超时，看起来像应用坏了）。被测对象是应用，不是 shell 的环境。
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

try:
    _http_probes()
except Exception:  # noqa: BLE001
    report["errors"].append(traceback.format_exc())

try:
    _settings_api()
except Exception:  # noqa: BLE001
    report["errors"].append(traceback.format_exc())


# ------------------------------------------------------- 迭代打磨接口


def _refine_api() -> None:
    """迭代打磨的接口闸门与失败路径（临时目录 + 临时端口，不碰真实数据）。

    这一组要验的是**串起来的行为**：缓存失效时说清"重跑一次就行"、已发布时先把
    话说在前面、底稿版本不符就拒绝、以及"这一版没改成时上一版必须完好如初"。
    这些判断分散在 store、闸门函数、路由三处，只有真起一个服务点一遍，
    才能确认它们接得上——单测各自通过而拼起来不通，是最常见的一类空转。

    全程不碰网络：画面帧关掉、模型用默认配置（没有 api_key），所以迭代会以
    "尚未配置 api_key"失败——而失败路径本身也正是这里要验的东西。
    """
    import tempfile

    import requests
    import uvicorn

    from bili_scribe.app.main import RefineContext, create_app
    from bili_scribe.config import AppConfig
    from bili_scribe.core.collector import Bundle
    from bili_scribe.core.summarizer import PreparedMaterial

    tmp = Path(tempfile.mkdtemp(prefix="bs-refineapi-"))
    cfg = AppConfig()
    cfg.storage.db_path = str(tmp / "t.db")
    cfg.storage.key_path = str(tmp / "k.bin")
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 8897
    cfg.collect.frames.enabled = False

    app = create_app(cfg)
    state = app.state.ctx
    task_id = state.store.create_task("BV1probe")
    # bvid 是任务跑完解析之后才写进库的，这里手工补上——否则"这条视频发过没有"
    # 就无从查起（发布记录是按 bvid 建的索引）。
    state.store.update_task(task_id, bvid="BV1probe", aid=1, cid=2, title="探针")
    state.store.record_version(
        task_id, source="initial",
        summary_json={"one_line": "初版一句话", "summary_md": "初版正文",
                      "comment_short": "初版短评", "role": "standard"},
        preview_md="初版预览", comment_text="初版短评", role="standard",
        metrics={"usage": {"calls": 3, "prompt_tokens": 500, "completion_tokens": 60,
                           "total_tokens": 560, "unpriced": 0}, "elapsed": 40.0},
        status="ready", progress="总结已生成",
    )

    def seed_cache() -> None:
        """把"素材笔记还在内存里"这个状态造出来——真跑过一次总结才会有。"""
        state.remember_prepared(
            task_id,
            RefineContext(
                bundle=Bundle(
                    bvid="BV1probe", aid=1, cid=2, page=1, page_count=1,
                    title="探针", desc="", up_name="某人", up_mid=3, duration=60,
                    tname="知识", source_kind="subtitle_ai", source_label="平台 AI 字幕",
                    source_trust="中", source_detail="测试", body="笔记正文",
                ),
                material=PreparedMaterial(body="笔记正文", chunks=2),
            ),
        )

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8897, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(60):
        if server.started:
            break
        time.sleep(0.2)
    assert server.started, "迭代探针的 uvicorn 未能启动"

    base = "http://127.0.0.1:8897"
    probe = requests.Session()
    probe.trust_env = False

    def task() -> dict:
        resp = probe.get(f"{base}/api/task/{task_id}", timeout=10)
        assert resp.status_code == 200, resp.text[:200]
        return resp.json()

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

    def cache_expired_reason() -> str:
        data = task()
        assert data["can_refine"] is False, "缓存不在时不该说能改"
        assert "重新跑一次" in data["refine_blocked"], data["refine_blocked"]
        assert data["current_version"] == 1 and data["version_count"] == 1, data
        assert data["live_publish"] is None
        return f"can_refine=False · {data['refine_blocked'][:46]}…"

    def cache_expired_refine_is_409() -> str:
        resp = probe.post(f"{base}/api/task/{task_id}/refine",
                          json={"requirement": "改一下"}, timeout=10)
        assert resp.status_code == 409, f"期望 409，实际 {resp.status_code}：{resp.text[:160]}"
        detail = resp.json().get("detail", "")
        assert "重新跑一次" in detail, f"没告诉人该怎么办：{detail}"
        return f"409 · {detail[:46]}…"

    def versions_payload() -> str:
        data = probe.get(f"{base}/api/task/{task_id}/versions", timeout=10).json()
        assert data["current"] == 1 and len(data["versions"]) == 1, data
        assert data["total"]["calls"] == 3 and data["total"]["total_tokens"] == 560, data["total"]
        row = data["versions"][0]
        assert row["source"] == "initial" and row["current"] is True, row
        assert "summary_json" not in row, "列表不该背全文，否则几版之后接口会越来越重"
        assert row["metrics"]["usage"]["calls"] == 3, row["metrics"]
        return f"1 版 · 累计 {data['total']['calls']} 次调用 / {data['total']['total_tokens']} tokens"

    def version_detail() -> str:
        data = probe.get(f"{base}/api/task/{task_id}/versions/1", timeout=10).json()
        assert data["publish_document"].startswith("**初版一句话**"), data["publish_document"][:60]
        assert data["summary_json"]["summary_md"] == "初版正文", data["summary_json"]
        resp = probe.get(f"{base}/api/task/{task_id}/versions/9", timeout=10)
        assert resp.status_code == 404, resp.status_code
        return "第 1 版带全文与发布长文；第 9 版 404"

    def restore_current_is_rejected() -> str:
        resp = probe.post(f"{base}/api/task/{task_id}/versions/1/restore", timeout=10)
        # 回退到当前版只会往版本史里塞一条一模一样的记录，所以直接拒绝
        assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}：{resp.text[:160]}"
        assert "就是当前版" in resp.json().get("detail", ""), resp.text[:160]
        return "400 · 第 1 版就是当前版"

    def changed_fields_diff() -> str:
        """版本列表里的「实际改动」：三种状态必须分得开。

        它直接往版本史里追加记录（探针本来就是这么造数据的），所以**必须排在那些
        「假设只有 1 版」的探测之后**——否则它们会莫名其妙地失败。

        null / [] / 非空 是三件不同的事：没比过 / 比过没变 / 逐字段变了。
        界面上把「没比过」读成「没改动」，就等于替模型圆了谎。
        """
        def data_versions() -> dict:
            resp = probe.get(f"{base}/api/task/{task_id}/versions", timeout=10)
            assert resp.status_code == 200, resp.text[:200]
            return resp.json()

        assert data_versions()["versions"][0]["changed_fields"] is None, (
            "第 1 版没有上一版可比，应当是不适用（null），不是空数组"
        )

        state.store.record_version(
            task_id, source="refine", requirement="把 one_line 改得更平实",
            summary_json={"one_line": "改后一句话", "summary_md": "初版正文",
                          "comment_short": "初版短评", "role": "standard",
                          "revise_note": "只改了 one_line，其余未动"},
            preview_md="改后预览", comment_text="初版短评", role="standard",
            status="ready", progress="已改好",
        )
        rows = data_versions()["versions"]
        assert len(rows) == 2, rows
        fields = {f["field"]: f for f in rows[1]["changed_fields"]}
        assert list(fields) == ["one_line"], f"实际只动了 one_line，却报成 {list(fields)}"
        assert 0 < fields["one_line"]["ratio"] < 1 and fields["one_line"]["example"], fields

        # 再追加一版逐字相同的：必须是空数组（比过了、没变），而不是 null（没比过）
        state.store.record_version(
            task_id, source="refine", requirement="什么都不改",
            summary_json={"one_line": "改后一句话", "summary_md": "初版正文",
                          "comment_short": "初版短评", "role": "standard"},
            preview_md="改后预览", comment_text="初版短评", role="standard",
            status="ready", progress="已改好",
        )
        assert data_versions()["versions"][2]["changed_fields"] == [], (
            "逐字相同应当报空数组——'要求没被落实'本身就是有用信息"
        )

        # 回退版不参与比对（它没有"改"这回事）
        state.store.record_version(
            task_id, source="restore", requirement="以第 1 版为底稿",
            summary_json={"one_line": "初版一句话", "summary_md": "初版正文",
                          "comment_short": "初版短评", "role": "standard"},
            preview_md="初版预览", comment_text="初版短评", role="standard",
            status="ready", progress="已回到第 1 版",
        )
        assert data_versions()["versions"][3]["changed_fields"] is None, (
            "回退版没有字段级 diff——它该说的是「内容回到第 N 版」"
        )
        # 单版详情与列表口径一致（同一件事在两个地方不能各说各的）
        detail = probe.get(f"{base}/api/task/{task_id}/versions/2", timeout=10).json()
        assert [f["field"] for f in detail["changed_fields"]] == ["one_line"], detail["changed_fields"]
        return "第 1 版/回退版 = 不适用；改动版逐字段列出；未改动 = 空数组"

    def cache_back_opens_the_gate() -> str:
        seed_cache()
        data = task()
        assert data["can_refine"] is True and data["refine_blocked"] == "", data
        return "缓存回来后 can_refine=True"

    def stale_base_version_is_rejected() -> str:
        resp = probe.post(f"{base}/api/task/{task_id}/refine",
                          json={"requirement": "改一下", "base_version": 99}, timeout=10)
        assert resp.status_code == 409, f"期望 409，实际 {resp.status_code}：{resp.text[:160]}"
        detail = resp.json().get("detail", "")
        assert "第 99 版" in detail and "第 1 版" in detail, detail
        return f"409 · {detail[:46]}…"

    def failed_refine_keeps_previous() -> str:
        resp = probe.post(f"{base}/api/task/{task_id}/refine",
                          json={"requirement": "把第三节展开", "base_version": 1}, timeout=10)
        assert resp.status_code == 200, f"提交应当被接受：{resp.status_code} {resp.text[:160]}"
        assert resp.json()["base_version"] == 1, resp.json()

        deadline = time.time() + 20
        data = task()
        while data["status"] == "refining" and time.time() < deadline:
            time.sleep(0.3)
            data = task()
        # 没有 api_key，所以这一版必然改不成——而"改不成"必须表现为
        # "上一版还在、还能继续改"，不是"这条任务废了"。
        assert data["status"] == "ready", f"失败后要退回 ready，实际 {data['status']}"
        assert "没改成" in data["progress"], data["progress"]
        assert data["version_count"] == 1, "没改成的版本不该进版本史"
        assert data["can_refine"] is True, "失败后还得能继续改"
        assert data["summary_json"]["one_line"] == "初版一句话", "上一版内容必须原样还在"
        return f"提交 200 → 状态回 ready · {data['progress'][:40]}"

    def published_closes_refine() -> str:
        state.store.add_publish(task_id, "BV1probe", 1, "999", "发出去的内容", "published")
        data = task()
        assert data["can_refine"] is False, "已经发出去的评论不能编辑，打磨入口必须关掉"
        assert "撤回" in data["refine_blocked"], data["refine_blocked"]
        assert data["live_publish"] and data["live_publish"]["rpid"] == "999", data["live_publish"]
        resp = probe.post(f"{base}/api/task/{task_id}/refine",
                          json={"requirement": "再改"}, timeout=10)
        assert resp.status_code == 409, resp.status_code
        assert "撤回" in resp.json().get("detail", ""), resp.text[:160]
        return f"已发布 → can_refine=False · {data['refine_blocked'][:34]}…"

    for name, fn in (
        ("缓存失效时说明该怎么办", cache_expired_reason),
        ("缓存失效时迭代返回 409", cache_expired_refine_is_409),
        ("版本列表与累计消耗", versions_payload),
        ("单版详情与不存在的版本", version_detail),
        ("回退到当前版被拒", restore_current_is_rejected),
        ("缓存回来入口重开", cache_back_opens_the_gate),
        ("底稿版本不符时拒绝", stale_base_version_is_rejected),
        ("这一版没改成时上一版完好", failed_refine_keeps_previous),
        ("已发布则关闭打磨入口", published_closes_refine),
        # ⚠️ 这一条会往版本史里追加记录，必须排最后——前面几条都假设"只有 1 版"
        ("版本列表的实际改动", changed_fields_diff),
    ):
        run(name, fn)

    server.should_exit = True


try:
    _refine_api()
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
