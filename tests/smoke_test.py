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


try:
    _http_probes()
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
