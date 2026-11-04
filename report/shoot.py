"""截两套皮肤的实际渲染图。启动真服务 + headless Chrome 截图。

为什么不用 DOM 桩：桩里什么都"存在"，配色对不对它答不出来。皮肤这种
纯视觉的改动，唯一可信的验收就是看渲染出来的像素。
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_cfg(skin: str, port: int) -> Path:
    src = (PROJ / "config.yaml").read_text(encoding="utf-8")
    out = []
    for line in src.splitlines():
        if line.startswith("  port:"):
            out.append(f"  port: {port}")
        elif line.strip().startswith("api_key:"):
            # 这份配置会被写进 report/ 下**受版本控制**的文件（report/shot-*.yaml）。
            # 原样复制 root config.yaml 等于把活凭证推进公开仓库——2026-11 就这么
            # 泄露过一次。截图不需要 LLM 凭证，这里一律抹掉。
            # 守住这条的是 tests/wb/test_report_config_redaction.py。
            out.append('  api_key: ""  # 截图用不到凭证，凭证只留在 root config.yaml')
        elif line.strip().startswith("disclose_ai:"):
            out.append(line)
        else:
            out.append(line)
    text = "\n".join(out) + "\n"
    if "ui:" in text:
        text = text.replace("skin: ark", "skin: " + skin)
    else:
        text += f"\nui:\n  skin: {skin}\n"
    p = PROJ / "report" / f"shot-{skin}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def shoot(skin: str) -> Path:
    port = free_port()
    cfg = make_cfg(skin, port)
    env = dict(os.environ)
    proc = subprocess.Popen(
        [str(PROJ / ".venv" / "Scripts" / "python.exe"), "-c",
         "import sys,uvicorn;from bili_scribe.config import AppConfig;"
         "from bili_scribe.app.main import create_app;"
         f"cfg=AppConfig.load(r'{cfg}');cfg.source_path=r'{cfg}';"
         "uvicorn.run(create_app(cfg),host='127.0.0.1',port=" + str(port) + ",log_level='error')"],
        cwd=str(PROJ), env=env,
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
        png = PROJ / "report" / "shots" / f"{skin}.png"
        subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1",
            f"--window-size=1440,900",
            f"--screenshot={png}",
            f"--virtual-time-budget=5000",
            f"http://127.0.0.1:{port}/",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return png
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    for skin in ("ark", "exhibition"):
        print(shoot(skin))
