import http.server
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path.cwd()
WEB = ROOT / "video_workbench" / "web"
OUT = ROOT / "build" / "accept"
OUT.mkdir(parents=True, exist_ok=True)

Handler = type("Q", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda self, *a: None})


def factory(*a, **k):
    return Handler(*a, **k, directory=str(WEB))


with http.server.ThreadingHTTPServer(("127.0.0.1", 0), factory) as httpd:
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/index.html?mock=1"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="load")
            page.wait_for_function(
                "() => window.WB && window.WB.router && window.WB.router.current()"
            )
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / "tristate-01-shell.png"))
            page.locator(".statusbar").first.screenshot(path=str(OUT / "tristate-02-statusbar.png"))

            page.evaluate("""() => {
              var host = document.createElement("div");
              host.style.cssText = "position:fixed;left:24px;top:70px;z-index:9999;background:#fff;padding:16px;display:flex;gap:16px";
              host.id = "tri-demo";
              var a = document.createElement("div");
              a.style.width = "280px";
              a.appendChild(window.WB.ui.loading("正在读取素材库…"));
              var c = document.createElement("div");
              c.style.width = "280px";
              c.appendChild(window.WB.ui.errorFrom({message:"网络断了，连接不上后端",retryable:true}, function(){}, "读取失败"));
              var d = document.createElement("div");
              d.style.width = "280px";
              d.appendChild(window.WB.ui.errorFrom({message:"接口地址没填，去 设置 - 模型 填好",retryable:false}, function(){}, "读不到任务"));
              host.appendChild(a); host.appendChild(c); host.appendChild(d);
              document.body.appendChild(host);
            }""")
            page.wait_for_timeout(400)
            page.locator("#tri-demo").screenshot(path=str(OUT / "tristate-03-states.png"))
            page.evaluate(
                "() => { var n = document.getElementById('tri-demo'); if (n) n.remove(); }"
            )

            page.evaluate("""() => {
              window.WB.rpc = (function (real) {
                return function (m, p, o) {
                  if (m === "job.list") return Promise.resolve([]);
                  return real.call(window.WB, m, p, o);
                };
              })(window.WB.rpc);
              document.getElementById("status-jobs").click();
            }""")
            page.wait_for_selector(".modal__panel")
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT / "tristate-04-jobs-empty.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            page.evaluate("""() => {
              document.getElementById("status-jobs").focus();
              window.WB.ui.modal({
                title: "焦点陷阱自检",
                body: window.WB.ui.h("p", { text: "Tab 在弹窗内循环，关掉之后焦点回到打开它的按钮。" }),
                footer: [
                  window.WB.ui.h("button", { class: "btn", type: "button", text: "取消" }),
                  window.WB.ui.h("button", { class: "btn btn--primary", type: "button", text: "确定" })
                ]
              });
            }""")
            page.wait_for_selector(".modal__panel")
            page.keyboard.press("Tab")
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / "tristate-05-modal-focus.png"))
            b.close()
    finally:
        httpd.shutdown()
        t.join(timeout=5)
        httpd.server_close()

for p in sorted(OUT.glob("tristate-0*.png")):
    print(p.name, p.stat().st_size)
