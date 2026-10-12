"""启动入口。

    python run.py                     用 config.yaml 启动，自动打开浏览器
    python run.py --config my.yaml    指定配置文件
    python run.py --check             只做环境与配置自检，不起服务

刻意只监听 127.0.0.1。这个进程的数据库里存着你的 B站 登录凭据，
把它暴露到局域网或公网等于把账号交出去。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from bili_scribe import __version__
from bili_scribe.config import AppConfig


def _port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _is_our_instance(host: str, port: int, timeout: float = 2.0) -> bool:
    """端口上跑的是不是本程序。

    只看端口通不通不够——随便什么服务占了 8818，connect 都会成功。这里拉一次
    /api/config 认一下响应结构，避免把别人的服务当成自己。
    """
    url = f"http://{host}:{port}/api/config"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    return isinstance(payload, dict) and {"llm", "publish"} <= set(payload)


def _open_when_ready(host: str, port: int, url: str, timeout: float = 25.0) -> None:
    """等服务真的能响应了再开浏览器。

    固定 sleep 靠不住：机器慢、或首次导入依赖较久时，uvicorn 还没绑上端口，
    浏览器就会打开一个打不开的地址——看起来像"前端没起来"，其实只是时序问题。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_our_instance(host, port, timeout=1.0):
            break
        time.sleep(0.3)
    webbrowser.open(url)


def _banner(cfg: AppConfig) -> None:
    print("=" * 62)
    print(f"  bili-scribe {__version__} —— B站视频内容总结（本地自用）")
    print("=" * 62)
    print(f"  监听地址   http://{cfg.server.host}:{cfg.server.port}")
    print(f"  数据文件   {cfg.db_path}")
    print(f"  大模型     {cfg.llm.model} @ {cfg.llm.base_url}")
    if not cfg.llm.api_key:
        print("  [!] 未配置 llm.api_key，总结步骤会失败。请在 config.yaml 里填写。")
    if not cfg.publish.require_confirm:
        print("  [!] require_confirm = false：总结生成后会直接发到评论区，没有确认环节。")
    print("  仅监听本机。请勿改成 0.0.0.0 —— 这个进程持有你的登录凭据。")
    print("=" * 62)
    print()


def _check(cfg: AppConfig) -> int:
    """自检：把"跑起来才发现"的问题提前暴露。"""
    ok = True

    if not cfg.llm.api_key:
        print("[x] llm.api_key 为空")
        ok = False
    else:
        print(f"[v] llm.api_key 已配置（{cfg.llm.model}）")

    db = Path(cfg.db_path)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        probe = db.parent / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"[v] 数据目录可写：{db.parent}")
    except OSError as exc:
        print(f"[x] 数据目录不可写：{db.parent}（{exc}）")
        ok = False

    try:
        import qrcode  # noqa: F401

        print("[v] qrcode 可用")
    except ImportError:
        print("[x] 缺少 qrcode，无法生成登录二维码：pip install 'qrcode[pil]'")
        ok = False

    try:
        from cryptography.fernet import Fernet  # noqa: F401

        print("[v] cryptography 可用，凭据将加密存储")
    except ImportError:
        print("[!] 缺少 cryptography，凭据会以明文存放：pip install cryptography")

    try:
        from bili_scribe.app.main import create_app  # noqa: F401

        print("[v] 应用可正常导入")
    except Exception as exc:
        print(f"[x] 应用导入失败：{exc!r}")
        ok = False

    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="bili-scribe 启动器")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径（默认 config.yaml；不存在则用内置默认值）",
    )
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    parser.add_argument("--check", action="store_true", help="只做自检，不启动服务")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    config_path = Path(args.config)
    try:
        cfg = AppConfig.load(config_path if config_path.exists() else None)
    except (ValueError, FileNotFoundError) as exc:
        print(f"配置装载失败：{exc}")
        return 2

    # 记下配置文件的实际路径，设置页面要靠它知道该回写到哪。
    # 用具名路径固化下来，避免工作目录变化后写到别处。
    cfg.source_path = str(config_path.resolve())

    if not config_path.exists():
        print(f"提示：未找到 {config_path}，当前使用内置默认值。")
        print("      在网页的设置页面点保存，就会把这个文件创建出来。\n")

    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    if args.check:
        return _check(cfg)

    url = f"http://{cfg.server.host}:{cfg.server.port}/"

    # 已经有实例在跑 → 不重复启动，直接把页面打开。
    # 没有这一步的话，双击两次快捷方式会撞上 "address already in use" 然后崩，
    # 现象和"程序坏了"一模一样，很难自己判断出来。
    if _is_our_instance(cfg.server.host, cfg.server.port):
        print(f"检测到 bili-scribe 已经在运行，直接打开页面：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    if _port_in_use(cfg.server.host, cfg.server.port):
        print(f"[x] 端口 {cfg.server.port} 被其他程序占用，无法启动。")
        print(f"    可以换一个端口：python run.py --port {cfg.server.port + 1}")
        return 3

    _banner(cfg)

    # create_app 需要注入配置，所以直接构造实例交给 uvicorn，
    # 不能用 factory=True —— 那样 uvicorn 会无参调用工厂函数。
    from bili_scribe.app.main import create_app

    app = create_app(cfg)

    if not args.no_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(cfg.server.host, cfg.server.port, url),
            daemon=True,
        ).start()

    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
