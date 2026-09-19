/* 前端回归测试。

    node tests/frontend_test.js

这个文件存在的理由是一个真实事故：用户打开的是被当成静态文件呈现的页面，
点"获取登录二维码"没有任何反应，而且界面上看不到任何线索——因为所有接口都不存在，
失败信息又被压成了一句话。

所以这里用最小 DOM 桩**真实执行** index.html 里的脚本，盯住两件事：
1. 页面能不能识别出"我没有连上后端"，并说清楚该怎么办；
2. 各类错误响应能不能变成人能看懂的文字。

不依赖浏览器：改的是纯 JS 逻辑，没有动样式与布局。
*/

const fs = require("fs");
const path = require("path");

const htmlPath = process.argv[2] || path.join(__dirname, "..", "bili_scribe", "app", "static", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) {
  console.log("FAIL: index.html 里找不到 <script> 块");
  process.exit(1);
}
const code = scriptMatch[1];

const results = [];
function assert(name, cond, detail) {
  results.push({ name, ok: !!cond, detail: detail === undefined ? "" : String(detail) });
}

/* ------------------------------------------------------------- 静态检查 */

try {
  new Function(code);
  assert("静态 1 · JS 语法可编译", true);
} catch (e) {
  assert("静态 1 · JS 语法可编译", false, e.message);
  report();
  process.exit(1);
}

const structural = [
  ["静态 2 · 存在离线横幅容器", html.includes('id="backend-banner"')],
  ["静态 3 · 定义了 showBackendBanner", /function showBackendBanner\s*\(/.test(code)],
  ["静态 4 · 定义了 detectBackend", /async function detectBackend\s*\(/.test(code)],
  ["静态 5 · init 先探测后端", /await detectBackend\(\)/.test(code)],
  ["静态 6 · api() 捕获网络异常并给出启动指引", /连不上后端/.test(code) && /python run\.py/.test(code)],
  ["静态 7 · api() 保留原始响应正文", /响应无正文/.test(code)],
  ["静态 8 · startQr 有离线守卫", /async function startQr[\s\S]{0,220}state\.online/.test(code)],
  ["静态 9 · 二维码不可用时禁用入口", /扫码登录不可用/.test(code)],
  ["静态 10 · 旧的 loadConfig 已移除", !/function loadConfig\s*\(/.test(code)],
];
for (const [name, ok] of structural) assert(name, ok);

/* ------------------------------------------------------------- 运行桩 */

function makeDocument() {
  const cache = Object.create(null);
  const mk = (id) => ({
    id,
    style: {},
    textContent: "",
    innerHTML: "",
    value: "",
    href: "",
    src: "",
    disabled: false,
    checked: false,
    open: false,
    className: "",
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
  });
  return {
    cache,
    getElementById(id) {
      if (!cache[id]) cache[id] = mk(id);
      return cache[id];
    },
    // 与 getElementById 共用缓存，保证 querySelector("details") 和
    // getElementById("__sel_details") 是同一个桩对象。
    querySelector(sel) {
      return this.getElementById("__sel_" + sel);
    },
  };
}

async function boot(fetchStub) {
  const document = makeDocument();
  const expose = new Function(
    "document",
    "fetch",
    "setInterval",
    "clearInterval",
    "confirm",
    "alert",
    "window",
    "console",
    code + "\nreturn { state, api, detectBackend, showBackendBanner };"
  );
  const api = expose(
    document,
    fetchStub,
    () => 0,
    () => {},
    () => true,
    () => {},
    {},
    { log() {}, warn() {}, error() {} }
  );
  await new Promise((r) => setTimeout(r, 80)); // 等 init 的异步链跑完
  return { document, ...api };
}

const okResponse = (payload) => async () => ({
  ok: true,
  status: 200,
  text: async () => JSON.stringify(payload),
});

const errResponse = (status, body) => async () => ({
  ok: false,
  status,
  text: async () => body,
});

const baseCfg = {
  llm: { model: "deepseek-chat", configured: false },
  publish: { require_confirm: true, max_comment_chars: 900 },
  features: { qr_login: true },
};

async function main() {
  // A · 完全没有后端 —— 就是出事故的那个场景
  {
    const { document } = await boot(async () => {
      throw new TypeError("Failed to fetch");
    });
    const banner = document.getElementById("backend-banner");
    assert("A1 离线时横幅可见", banner.style.display === "block", banner.style.display);
    assert("A2 横幅说明了原因", banner.innerHTML.includes("没有连上后端"));
    assert(
      "A3 横幅给出了启动命令与地址",
      banner.innerHTML.includes("python run.py") && banner.innerHTML.includes("127.0.0.1:8818")
    );
    assert("A4 二维码按钮被禁用", document.getElementById("qr-btn").disabled === true);
    assert("A5 总结按钮被禁用", document.getElementById("run-btn").disabled === true);
    assert(
      "A6 顶部状态显示未连接后端",
      document.getElementById("auth-text").textContent === "未连接后端",
      document.getElementById("auth-text").textContent
    );
  }

  // B · 后端正常
  {
    const { document, state } = await boot(okResponse(baseCfg));
    assert(
      "B1 在线时横幅不显示",
      document.getElementById("backend-banner").style.display !== "block",
      document.getElementById("backend-banner").style.display || "(未设置)"
    );
    assert("B2 识别为在线", state.online === true);
    assert("B3 二维码按钮可用", document.getElementById("qr-btn").disabled === false);
    assert(
      "B4 模型信息已填充",
      document.getElementById("model-chip").textContent.includes("deepseek-chat"),
      document.getElementById("model-chip").textContent
    );
    assert(
      "B5 未配置 key 时有标注",
      document.getElementById("model-chip").textContent.includes("未配置 key")
    );
  }

  // C · 后端在但缺 qrcode 依赖 —— 应当降级到 Cookie 通道，而不是报错
  {
    const cfg = JSON.parse(JSON.stringify(baseCfg));
    cfg.features.qr_login = false;
    const { document, state } = await boot(okResponse(cfg));
    assert("C1 仍识别为在线", state.online === true);
    assert("C2 二维码按钮被禁用", document.getElementById("qr-btn").disabled === true);
    assert(
      "C3 按钮文案改为不可用",
      document.getElementById("qr-btn").textContent === "扫码登录不可用",
      document.getElementById("qr-btn").textContent
    );
    assert(
      "C4 提示里给出安装命令",
      document.getElementById("auth-msg").textContent.includes("qrcode[pil]"),
      document.getElementById("auth-msg").textContent.slice(0, 56)
    );
    assert(
      "C5 自动展开 Cookie 兜底面板",
      document.getElementById("__sel_details").open === true
    );
  }

  // D · 错误正文为空时可读性
  {
    const { api } = await boot(errResponse(500, ""));
    let message = "";
    try {
      await api("/api/auth/qrcode");
    } catch (e) {
      message = e.message;
    }
    assert("D1 空正文错误带上状态码", message.includes("HTTP 500"), message);
    assert("D2 空正文错误说明无响应正文", message.includes("响应无正文"), message);
  }

  // E · 后端给了可操作提示时应当原样透出
  {
    const { api } = await boot(
      errResponse(503, JSON.stringify({ detail: '缺少二维码依赖，请执行：pip install "qrcode[pil]"' }))
    );
    let message = "";
    try {
      await api("/api/auth/qrcode");
    } catch (e) {
      message = e.message;
    }
    assert("E1 透出后端给的可操作提示", message.includes("qrcode[pil]"), message);
  }

  report();
}

function report() {
  let bad = 0;
  for (const r of results) {
    if (!r.ok) bad++;
    console.log((r.ok ? "[OK ] " : "[FAIL] ") + r.name + (r.detail ? "  → " + r.detail : ""));
  }
  console.log("-".repeat(58));
  console.log(`前端测试 ${results.length - bad}/${results.length} 通过`);
  process.exit(bad ? 1 : 0);
}

main();
