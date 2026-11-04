"""截设置抽屉的实际渲染（两套皮肤）。

抽屉默认关着，用和 shoot_pop.py 一样的手法：复制一份页面到 static 里、
末尾追加"打开抽屉"，截完删掉。

抽屉里表单控件最多（输入框、下拉、复选框、说明文字），是硬编码颜色最容易
漏的地方，所以必须单独看一眼。
"""
import subprocess
import socket
import sys
import time
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
STATIC = PROJ / "bili_scribe" / "app" / "static"
TMP_NAME = "_drawer_probe.html"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(skin: str, port: int) -> Path:
    src = (PROJ / "config.yaml").read_text(encoding="utf-8")
    # 同 shoot.py / shoot_pop.py：这份配置落进受版本控制的 report/drawer-*.yaml，
    # 原样复制 root config.yaml 会把活凭证推进公开仓库。截图不需要凭证。
    # 守住这条的是 tests/wb/test_report_config_redaction.py。
    out = [('  api_key: ""  # 截图用不到凭证，凭证只留在 root config.yaml'
            if l.strip().startswith("api_key:")
            else f"  port: {port}" if l.startswith("  port:") else l)
           for l in src.splitlines()]
    text = "\n".join(out) + "\n"
    if "ui:" in text:
        text = text.replace("skin: ark", "skin: " + skin)
    else:
        text += f"\nui:\n  skin: {skin}\n"
    p = PROJ / "report" / f"drawer-{skin}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def shoot(skin: str) -> Path:
    port = free_port()
    cfg = make_cfg(skin, port)

    src = (STATIC / "index.html").read_text(encoding="utf-8")
    # 抽屉是 `transform:translateX(101%)` + `.22s` 过渡滑进来的。
    # headless 的 --virtual-time-budget 不会推进 CSS 过渡，截图会停在半路
    # （第一次就是这么截到"抽屉只露一条缝"）。所以先把过渡关掉，再打开。
    extra = (
        "\n<style>*{transition:none!important;animation:none!important}</style>\n"
        "<script>\n"
        "setTimeout(function(){\n"
        "  try { openDrawer(); } catch (e) { console.error(e); }\n"
        "}, 1500);\n"
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

        png = PROJ / "report" / "shots" / f"drawer-{skin}.png"
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1440,900",
            f"--screenshot={png}",
            "--virtual-time-budget=7000",
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
