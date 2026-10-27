/* 源码版（HTTP）端到端测试
 *
 * `frontend_test.js` 里所有用例走的都是桥接路径（假 api）。这个反过来：
 * 自己起一个真实服务，用 jsdom 打开它发出来的页面，**不给任何 pywebview 桥**，
 * 只给一个指向该服务的 fetch，看它能不能自己站起来。
 *
 * 它回答的是前端测试答不了的那个问题：`python run.py` + 浏览器这条路还通不通。
 * 这条路的代码（`HTTP_ROUTES` 那张表、`callHttp`、轮询）和桌面版不共用一行，
 * 桌面版测全绿也证明不了它没坏。
 *
 * 用法：`node tests/probe_http.js`
 *   - 自己找空闲端口起服务，不碰你正在用的 8818
 *   - 跑完自己收掉（成功、失败、异常都收）
 */

const { spawn } = require("child_process");
const net = require("net");
const path = require("path");
const fs = require("fs");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const PY = path.join(ROOT, ".venv", "Scripts", "python.exe");

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

/* 让系统给一个空闲端口。先 bind 到 0 拿到号，再关掉——中间那一瞬别人可能抢走，
   但在这台机器上跑测试的概率极低，而且真抢走了下面等服务的循环会超时并报清楚。 */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
  });
}

async function waitServer(base, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(base + "/api/config");
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
    await sleep(200);
  }
  return false;
}

/* ---------- 真正的断言 ---------- */

async function run(base) {
  const res = await fetch(base + "/");
  if (!res.ok) throw new Error("服务没起来：" + res.status);
  const html = await res.text();

  const errors = [];
  const dom = new JSDOM(html, {
    url: base + "/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
    beforeParse(window) {
      // jsdom 自己没有 fetch。把请求交回 Node 的 fetch，相对路径按页面地址补全，
      // 这样页面里的 `fetch("/api/config")` 就真的打到了那个服务上。
      window.fetch = (input, init) => {
        const u = typeof input === "string" ? new URL(input, base).href : input;
        return fetch(u, init);
      };
      window.addEventListener("error", (e) => errors.push(String(e.message || e)));
    },
  });

  const win = dom.window;
  const doc = win.document;
  const bad = [];

  // boot() 是异步的（要等几次网络往返）。`S.online` 只说明配置取到了，
  // 后面还有取角色、刷新登录态、翻历史任务——**底栏版本号是最后才填的**，
  // 所以等它出现才算真跑完。
  let online = false;
  for (let i = 0; i < 250; i++) {
    await sleep(100);
    try { online = !!win.eval("S.online"); } catch (e) { /* S 还没建好 */ }
    const ver = doc.querySelector("#ft-ver");
    if (online && ver && /^v\d/.test(ver.textContent)) break;
  }
  await sleep(300);   // 让最后那次 list_tasks 的渲染落定

  const q = (s) => doc.querySelector(s);
  const txt = (s) => (q(s) ? q(s).textContent.trim() : null);
  const transport = (() => { try { return win.eval("TRANSPORT"); } catch (e) { return null; } })();

  const state = {
    online,
    transport,
    bridge_visible: q("#bridge").classList.contains("on"),
    ft_mode: txt("#ft-mode"),
    ft_ver: txt("#ft-ver"),
    chip_auth: txt("#chip-auth"),
    chip_model: txt("#chip-model"),
    history_rows: doc.querySelectorAll("#hist-list > *").length,
    errors,
  };
  console.log(JSON.stringify(state, null, 2));

  if (!online) bad.push("没有进入在线状态（S.online 仍为 false）");
  if (transport !== "http") bad.push("传输层没走 HTTP 而是 " + transport);
  if (state.bridge_visible) bad.push("显示了断桥横幅（HTTP 下不该出现）：" + txt("#bridge"));
  if (!state.ft_mode || !state.ft_mode.includes("127.0.0.1")) bad.push("底栏没标出 HTTP 形态：" + state.ft_mode);
  if (!state.ft_ver || !/^v\d/.test(state.ft_ver)) bad.push("版本号没取到：" + state.ft_ver);
  if (!state.chip_model) bad.push("模型名没取到（get_config 没通）");
  if (errors.length) bad.push("页面脚本报错：" + errors.join(" | "));

  /* 登录态这条必须和后端对账，不能只看"chip 里有字"。
     `refreshAuth()` 把异常咽掉、退成"未登录"，所以 auth_status 这条路由拼错时
     界面不会报错，只会安安静静显示成"你没登录"——这种静默降级最难发现，
     也正是这张路由表最容易犯的错。 */
  const realAuth = await (await fetch(base + "/api/auth/status")).json();
  if (realAuth && realAuth.logged_in) {
    if (!state.chip_auth || !/UID/.test(state.chip_auth)) {
      bad.push("后端说已登录（" + (realAuth.identity && realAuth.identity.uname) +
        "），界面却显示「" + state.chip_auth + "」——auth_status 没取到");
    }
  }

  // 光看首屏还不够：轮询逻辑只在任务跑起来之后才走。这条查出"手动 get_task 之后
  // 会不会自己接着问"，否则源码版界面会停在提交那一刻的进度上不再动。
  try {
    const viaNode = await (await fetch(base + "/api/tasks?limit=1")).json();
    if (Array.isArray(viaNode) && viaNode.length) {
      const t = viaNode[0];
      win.eval(`S.taskId = ${JSON.stringify(t.id)}; S.task = null;`);
      win.eval("ensurePolling()");
      await sleep(2200);   // 一轮间隔 1.2s，等它确实发了一轮
      const calls = win.eval("pollTimer === null ? 'fired' : 'armed'");
      // 任务已是终态时应停表；未终态才继续。两种都算正确，只要求"真的跑过一轮"。
      if (calls !== "fired" && calls !== "armed") bad.push("轮询没有启动：pollTimer=" + calls);
      const got = win.eval("!!S.task");
      if (!got) bad.push("轮询跑了一轮却没有把任务塞进状态（get_task 的结果没被用上）");
    }
  } catch (e) {
    bad.push("轮询验证出错：" + e.message);
  }

  /* 桌面版专有的那几个方法，在 HTTP 下必须"说得清"，不能崩也不能静默。
     `open_data_dir` 是其中最容易被忘掉的一个——它想把服务器上的资源管理器拉起来，
     在浏览器里没有等价物。这条确认它给出的是能照做的提示，而不是一串异常。 */
  try {
    win.eval("call('open_data_dir')")
      .then(() => bad.push("open_data_dir 在 HTTP 下竟然成功了——它不该能跨进程开文件夹"))
      .catch((e) => {
        if (!/目录|文件夹/.test(e.message)) {
          bad.push("open_data_dir 的报错没告诉人目录在哪：" + e.message);
        }
      });
    await sleep(150);
  } catch (e) {
    bad.push("open_data_dir 验证出错：" + e.message);
  }

  /* 上传字幕这条路在两种形态下走的是完全不同的代码：桌面版调 Python 的
     原生对话框，源码版建一个 `<input type=file>` 读字节再 POST 到
     `/api/subtitle/parse`（收的是裸字节，不是 multipart）。
     而它同时是 `callHttp` 里唯一一个绕开 `HTTP_ROUTES` 表的特例——
     正则扫"每个 call() 名字后端都得有"时看不见它，坏掉不会有人报。 */
  try {
    const srt = "1\n00:00:01,000 --> 00:00:03,000\n你好世界\n\n2\n00:00:04,000 --> 00:00:06,500\n这是第二句\n";
    const r = await fetch(base + "/api/subtitle/parse", {
      method: "POST",
      headers: { "content-type": "application/octet-stream" },
      body: Buffer.from(srt, "utf8"),
    });
    if (!r.ok) {
      bad.push("字幕解析接口不在：" + r.status);
    } else {
      const d = await r.json();
      if (d.segments !== 2) bad.push("字幕解析段数不对：" + d.segments);
      if (!d.token) bad.push("字幕解析没给 token");
      // 拿到 token 就要还回去，否则临存区会攒下一堆没人认领的文件。
      await fetch(base + "/api/subtitle/" + encodeURIComponent(d.token), { method: "DELETE" });
    }
  } catch (e) {
    bad.push("字幕解析验证出错：" + e.message);
  }

  return bad;
}

/* ---------- 起服务、测、收尾 ---------- */

(async () => {
  if (!fs.existsSync(PY)) {
    console.log("[SKIP] 找不到 " + PY + "，跳过（这个测试需要 venv）");
    process.exit(0);
  }

  const port = await freePort();
  const base = "http://127.0.0.1:" + port;
  console.log("起服务：" + base);

  const proc = spawn(PY, ["run.py", "--no-browser", "--port", String(port)], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  let log = "";
  proc.stdout.on("data", (d) => { log += d.toString("utf8"); });
  proc.stderr.on("data", (d) => { log += d.toString("utf8"); });

  const cleanup = () => {
    try { proc.kill(); } catch (e) { /* 已经没了 */ }
  };
  process.on("exit", cleanup);

  let bad = [];
  try {
    if (!(await waitServer(base, 40000))) {
      throw new Error("服务 40 秒内没起来。日志：\n" + log.slice(-1500));
    }
    bad = await run(base);
  } catch (e) {
    cleanup();
    console.error("[FAIL] " + e.message);
    process.exit(1);
  }

  cleanup();
  if (bad.length) {
    console.log("\n[FAIL]\n - " + bad.join("\n - "));
    process.exit(1);
  }
  console.log("\n[OK] 源码版页面在没有 pywebview 桥的情况下自己起来了，数据全部来自 HTTP，轮询也会自己接上。");
  process.exit(0);
})();
