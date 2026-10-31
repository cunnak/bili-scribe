"""截皮肤选择浮层的实际渲染。

浮层默认是关着的，纯 `--screenshot` 截不到。做法：把 index.html 复制一份到
static 目录下、在末尾追加一句"打开浮层"，截完删掉。为什么必须放 static 里：
页面用相对路径取 `?skin=`、没有外部资源，但 boot() 会去打 /api/config ——
换个目录放，同源策略还在，只是路径要对得上。放 static 最省事。

临时文件用完即删，不留垃圾。
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
STATIC = PROJ / "bili_scribe" / "app" / "static"
TMP_NAME = "_pop_probe.html"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(skin: str, port: int) -> Path:
    src = (PROJ / "config.yaml").read_text(encoding="utf-8")
    out = []
    for line in src.splitlines():
        out.append(f"  port: {port}" if line.startswith("  port:") else line)
    text = "\n".join(out) + "\n"
    if "ui:" in text:
        text = text.replace("skin: ark", "skin: " + skin)
    else:
        text += f"\nui:\n  skin: {skin}\n"
    p = PROJ / "report" / f"pop-{skin}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def shoot(skin: str) -> Path:
    port = free_port()
    cfg = make_cfg(skin, port)

    # 复制一份页面，末尾追加打开浮层的语句。boot() 是异步的，等它跑完再开。
    src = (STATIC / "index.html").read_text(encoding="utf-8")
    extra = (
        "\n<script>\n"
        "setTimeout(function(){\n"
        "  try { openSkinPop(); } catch (e) { console.error(e); }\n"
        "}, 1200);\n"
        "</script>\n"
    )
    (STATIC / TMP_NAME).write_text(src + extra, encoding="utf-8")

    proc = subprocess.Popen(
        [str(PROJ / ".venv" / "Scripts" / "python.exe"), "-c",
         "import sys,uvicorn;from bili_scribe.config import AppConfig;"
         "from bili_scribe.app.main import create_app;"
         f"cfg=AppConfig.load(r'{cfg}');cfg.source_path=r'{cfg}';"
         "uvicorn.run(create_app(cfg),host='127.0.0.1',port=" + str(port) + ",log_level='error')"],
        cwd=str(PROJ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1)
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise SystemExit("server did not come up")

        png = PROJ / "report" / "shots" / f"pop-{skin}.png"
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1440,900",
            f"--screenshot={png}",
            "--virtual-time-budget=6000",
            f"http://127.0.0.1:{port}/static/{TMP_NAME}?skin={skin}",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return png
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        (STATIC / TMP_NAME).unlink(missing_ok=True)


if __name__ == "__main__":
    for s in ("ark", "exhibition"):
        print(shoot(s))
