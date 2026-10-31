"""首次启动向导：五件事的环境体检。

打出来的 exe 要在**一台没装过开发环境**的 Windows 上跑起来，这五件事任何
一件缺了，用户看到的就是"点了没反应"：

============  ====================================================
WebView2      pywebview 的窗口层。缺了是白窗或 COM 异常，**不是**报错框
FFmpeg        抽帧/转码/烧字幕/合并全靠它；缺了只有"导出失败"
ASR 模型      缺了转录按钮转两下就失败（模型 1.6GB，不能随包发）
B 站登录      没登录只能手动导入本地文件，解析链接会被 403
LLM           key 没配就只有规则兜底文案，没有总结/翻译/切片提示
============  ====================================================

设计取舍：

1. **只体检、不代劳**。模型下载走 `transcribe.model.download`（有进度、可取消），
   FFmpeg 让用户自己装了指路径（许可与体积都不允许我们代发，方案书 §0）。
   向导负责"告诉你在哪一步、缺什么、点哪里"，不重复实现一遍下载器。
2. **状态三态而不是两态**：``ok`` / ``todo``（还没做，但能一键做）/
   ``missing``（缺了得用户自己装，我们给地址）。两态会把"模型没下"和
   "FFmpeg 没装"说成同一件事，用户按提示点半天也没用。
3. **不因为缺东西就不让进**。``ready`` 只用于界面提示；用户可以先跳过，
   用能用的部分（手动导入 + 已有字幕）先跑起来。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

# WebView2 Evergreen Runtime 的固定 GUID（微软官方文档里那个）
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/"
STEP_IDS = ("webview2", "ffmpeg", "model", "bili", "llm")


# ============================================================ 单项体检


def webview2_version() -> str:
    """读 WebView2 运行库版本号；没装返回空串。

    先查注册表（安装了就一定在），再退回扫目录——有些精简版系统的注册表项
    被清理过，但文件还在。
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
    except Exception:  # pragma: no cover - 非 Windows
        return ""
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    ]
    for root, key in candidates:
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "pv")
                if value:
                    return str(value)
        except OSError:
            continue
    for base in (r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
                 r"C:\Program Files\Microsoft\EdgeWebView\Application"):
        folder = Path(base)
        if folder.is_dir():
            versions = sorted((p.name for p in folder.iterdir() if p.is_dir()), reverse=True)
            if versions:
                return versions[0]
    return ""


def ffmpeg_path(cfg: Any) -> str:
    """FFmpeg 位置：配置里指了就用它，否则看 PATH。"""
    configured = str(getattr(getattr(cfg, "storage", None), "ffmpeg_dir", "") or "")
    if configured:
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = Path(configured) / name
            if candidate.is_file():
                return str(candidate)
        if Path(configured).is_file():
            return configured
    found = shutil.which("ffmpeg")
    return found or ""


def model_state(cfg: Any) -> dict[str, Any]:
    """当前选中的 ASR 模型下了没有。"""
    from video_workbench.infra import models as models_mod

    root = str(cfg.storage.models_root)
    want = str(cfg.transcribe.model)
    rows = models_mod.list_status(root)
    row = next((item for item in rows if item.get("name") == want), None)
    return {
        "name": want,
        "downloaded": bool(row and row.get("downloaded")),
        "bytes": int((row or {}).get("bytes") or 0),
        "models": [{"name": item.get("name"), "downloaded": bool(item.get("downloaded")),
                    "bytes": int(item.get("bytes") or 0)} for item in rows],
    }


def bili_state(gateway: Any) -> dict[str, Any]:
    """B 站登录态。注意我们**复用 bili-scribe 的登录**，这里只报告，不发起登录。"""
    try:
        logged = bool(gateway.is_logged_in())
    except Exception:  # noqa: BLE001 - 登录态查不到按未登录处理，别让向导崩
        logged = False
    who = ""
    if logged:
        try:
            identity = gateway.identity() or {}
            if isinstance(identity, dict):
                for key in ("name", "uname", "nickname", "username"):
                    if identity.get(key):
                        who = str(identity[key])
                        break
            else:  # 万一以后换成对象
                who = str(getattr(identity, "name", "") or "")
        except Exception:  # noqa: BLE001
            who = ""
    return {"logged_in": logged, "who": who}


def llm_state(cfg: Any) -> dict[str, Any]:
    key = str(getattr(cfg.llm, "api_key", "") or "")
    return {
        "configured": bool(key.strip()),
        "model": str(getattr(cfg.llm, "model", "") or ""),
        "base_url": str(getattr(cfg.llm, "base_url", "") or ""),
    }


# ============================================================ 汇总


def _step(step_id: str, title: str, status: str, detail: str, *, hint: str = "",
          action: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": step_id, "title": title, "status": status, "detail": detail,
            "hint": hint, "action": action}


def report(*, cfg: Any, gateway: Any, command_line: str = "") -> dict[str, Any]:
    """五项体检 + 汇总。

    ``command_line`` 由调用方给（exe 还是 `python -m`），只用于界面显示——
    用户报障时一眼能看出他跑的是哪个。
    """
    steps: list[dict[str, Any]] = []

    version = webview2_version()
    steps.append(_step(
        "webview2", "窗口运行库（WebView2）",
        "ok" if version else "missing",
        f"已装 WebView2 {version}" if version else "没找到 WebView2 运行库",
        hint="" if version else "不装它窗口是白的。装完重启本程序即可，设置不用动。",
        action=None if version else {"kind": "url", "url": WEBVIEW2_URL, "label": "去下载"},
    ))

    ffmpeg = ffmpeg_path(cfg)
    steps.append(_step(
        "ffmpeg", "FFmpeg",
        "ok" if ffmpeg else "missing",
        ffmpeg or "PATH 里没有 ffmpeg，配置里也没指目录",
        hint="" if ffmpeg else "下载解压后，在「设置 · 素材与缓存」里把目录指到 bin 文件夹。"
                              "我们不随包分发 FFmpeg（许可与体积），只调用你本机这一份。",
        action=None if ffmpeg else {"kind": "url", "url": FFMPEG_URL, "label": "去下载"},
    ))

    model = model_state(cfg)
    size_text = f"{model['bytes'] / (1024 ** 3):.1f} GB" if model["bytes"] else "约 1.6 GB"
    steps.append(_step(
        "model", "语音模型",
        "ok" if model["downloaded"] else "todo",
        f"{model['name']} 已就绪" if model["downloaded"] else f"{model['name']} 还没下（{size_text}）",
        hint="" if model["downloaded"] else "下载在后台跑，可以先去别的页面。下完不用重启。",
        action=None if model["downloaded"] else {
            "kind": "rpc", "method": "transcribe.model.download",
            "params": {"name": model["name"]}, "label": "开始下载",
        },
    ))

    bili = bili_state(gateway)
    steps.append(_step(
        "bili", "B 站登录",
        "ok" if bili["logged_in"] else "missing",
        (f"已登录{'：' + bili['who'] if bili['who'] else ''}") if bili["logged_in"] else "没有可用的登录态",
        hint="" if bili["logged_in"] else
        "我们是复用 bili-scribe 的登录态（同一台机器上登录一次就行）。"
        "没登录也能用：把本地视频拖进素材库即可。",
        action=None if bili["logged_in"] else {"kind": "note", "label": "知道了"},
    ))

    llm = llm_state(cfg)
    steps.append(_step(
        "llm", "大模型",
        "ok" if llm["configured"] else "todo",
        f"{llm['model']} · key 已配置" if llm["configured"] else "还没填 API key",
        hint="" if llm["configured"] else "不填也能用：总结/翻译/切片提示会退回规则兜底，只是质量差一截。",
        action=None if llm["configured"] else {
            "kind": "route", "route": "settings", "params": {"section": "llm"}, "label": "去填写",
        },
    ))

    ok_count = sum(1 for item in steps if item["status"] == "ok")
    missing = [item["id"] for item in steps if item["status"] == "missing"]
    # "阻塞"只算"不解决就干不了活"的那几件：窗口起不来、抽不了帧、转不出字。
    # 没登录（还能本地导入）和没配 LLM（还有规则兜底）不阻塞，只提示。
    blocking = [step_id for step_id in ("webview2", "ffmpeg", "model") if step_id in missing]
    if not model["downloaded"] and "model" not in blocking:
        blocking.append("model")
    return {
        "steps": steps,
        "ok_count": ok_count,
        "total": len(steps),
        "ready": not blocking,
        "blocking": blocking,
        "missing": missing,
        "done": bool(getattr(getattr(cfg, "ui", None), "onboarded", False)),
        "command_line": command_line,
        "platform": f"{sys.platform} / python {sys.version.split()[0]}"
                    + ("（打包版）" if getattr(sys, "frozen", False) else "（源码运行）"),
    }
