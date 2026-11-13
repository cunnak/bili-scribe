import http.server
import json
import os
import threading
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "video_workbench" / "web"
OUT = Path(tempfile.gettempdir()) / "wb_light_shots"
OUT.mkdir(parents=True, exist_ok=True)
Handler = type("QuietHandler", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda self, *a: None})
def factory(*args, **kwargs):
    return Handler(*args, directory=str(WEB), **kwargs)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), factory)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html?mock=1"
readings = []
errors = []
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1525, "height": 900})
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(url, wait_until="load")
        page.wait_for_function("() => window.WB && window.WB.router")
        page.evaluate("() => window.WB.router.go('summary', {})")
        page.wait_for_selector("#sum-project", timeout=8000)
        page.screenshot(path=str(OUT / "local_1525.png"), full_page=True)
        page.evaluate("""() => {
            const s = window.WB.pages.summaryLight;
            s.state.lightInfo = {title: "AI撞上美国“新世界”：一条超长视频标题用来验证输入框不会被挤出白框", up_name: "演示UP"};
            window.WB.router.go('light', {});
        }""")
        page.wait_for_selector("#sum-light-info", timeout=8000)
        for width, height in ((1100, 720), (1525, 900), (2306, 1225), (3840, 1600)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(180)
            data = page.evaluate("""() => {
                const input = document.querySelector('#sum-light-url');
                const field = input.closest('.sum-fld');
                const fields = document.querySelector('#sum-light-fields');
                const meta = document.querySelector('#sum-light-meta');
                const ir = input.getBoundingClientRect();
                const fr = field.getBoundingClientRect();
                const mr = meta.getBoundingClientRect();
                const fsr = fields.getBoundingClientRect();
                return {viewport: [innerWidth, innerHeight], inputRight: Math.round(ir.right), fieldRight: Math.round(fr.right), inputLeft: Math.round(ir.left), fieldLeft: Math.round(fr.left), metaTop: Math.round(mr.top), fieldsBottom: Math.round(fsr.bottom), scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth};
            }""")
            data["file"] = f"light_{width}.png"
            readings.append(data)
            page.screenshot(path=str(OUT / data["file"]), full_page=True)
        print(json.dumps({"readings": readings, "errors": errors}, ensure_ascii=False, indent=2))
        browser.close()
finally:
    httpd.shutdown()
    thread.join(timeout=5)
    httpd.server_close()
