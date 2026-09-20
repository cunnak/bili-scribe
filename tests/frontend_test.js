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
  const mk = (id) => {
    const classes = new Set();
    let _value = "";
    return {
      id,
      style: {},
      textContent: "",
      innerHTML: "",
      // 真实 DOM 对 input.value 赋值会强制转成字符串，
      // 桩必须照做，否则"数字赋值后 .trim()"这类真实可跑的逻辑会被误判为错。
      get value() { return _value; },
      set value(v) { _value = v === null || v === undefined ? "" : String(v); },
      href: "",
      src: "",
      disabled: false,
      checked: false,
      open: false,
      className: "",
      // 需要记录状态：设置页要靠 classList.toggle 控制确认区的显隐
      classList: {
        add(c) { classes.add(c); },
        remove(c) { classes.delete(c); },
        toggle(c, force) {
          const on = force === undefined ? !classes.has(c) : !!force;
          if (on) classes.add(c); else classes.delete(c);
          return on;
        },
        has(c) { return classes.has(c); },
      },
      addEventListener() {},
    };
  };
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
    code +
      "\nreturn { state, api, detectBackend, showBackendBanner, switchView, loadSettings," +
      " fillSettingsForm, collectSettings, validateSettings, saveSettings, testLlm," +
      " toggleKey, onGateToggle, setGateBanner };"
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

/* 设置页要按路径区分 GET /api/settings 与 PUT /api/settings，
   单一返回值的桩不够用了，所以加一个路由桩。 */
function routeFetch(routes) {
  const seen = [];
  const fn = async (path, options) => {
    const method = (options && options.method) || "GET";
    seen.push({ path, method, body: options && options.body });
    const key = method + " " + path;
    const hit = routes[key] || routes[path];
    if (!hit) {
      return { ok: false, status: 404, text: async () => JSON.stringify({ detail: "no route: " + key }) };
    }
    const r = typeof hit === "function" ? hit(options) : hit;
    return {
      ok: r.status >= 200 && r.status < 300,
      status: r.status,
      text: async () => (typeof r.body === "string" ? r.body : JSON.stringify(r.body)),
    };
  };
  fn.seen = seen;
  return fn;
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

  // F · 设置页
  {
    // GET /api/settings 的真实响应形状
    const settingsValues = {
      server: { host: "127.0.0.1", port: 8818 },
      llm: {
        base_url: "https://api.deepseek.com/v1",
        api_key: "sk-test-abcdef123456",
        model: "deepseek-chat",
        temperature: 0.3,
        max_tokens: 4096,
        thinking: "off",
        timeout_seconds: 300,
        max_input_chars: 60000,
        chunk_size: 12000,
        chunk_overlap: 400,
      },
      collect: {
        prefer_subtitle_lang: ["zh-CN", "zh-Hans"],
        include_platform_summary: false,
        danmaku: { enabled: true, also_with_subtitle: false, max_items: 3000, hot_items: 200, sample_items: 300 },
      },
      publish: {
        require_confirm: true,
        min_interval_seconds: 60,
        max_comment_chars: 900,
        disclose_ai: true,
        disclosure_text: "本总结由 AI 生成",
      },
      storage: { db_path: "~/.bili-scribe/data.db", key_path: "~/.bili-scribe/key.bin" },
    };
    const settingsBody = {
      values: settingsValues,
      path: "C:\\tmp\\config.yaml",
      config_exists: true,
      gate_disabled: false,
      restart_needed_for: ["server"],
      platform_comment_max: 1000,
    };
    const routes = {
      "/api/config": { status: 200, body: baseCfg },
      "GET /api/settings": { status: 200, body: settingsBody },
      "PUT /api/settings": {
        status: 200,
        body: {
          ok: true,
          written: { path: "C:\\tmp\\config.yaml", backup: "config.yaml.bak" },
          restart_required: false,
          restart_sections: [],
          gate_disabled: false,
        },
      },
      "/api/settings/test-llm": { status: 200, body: { ok: true, detail: "连接正常", model: "deepseek-chat" } },
    };

    assert(
      "F1 两个视图各自闭合（没有裸露的 </main>）",
      (html.match(/id="view-main"/g) || []).length === 1 &&
        (html.match(/id="view-settings"/g) || []).length === 1 &&
        !/<\/main>/.test(html),
      "若仍有 </main>，设置视图会被浏览器并进主视图"
    );
    assert("F2 两个 tab 都已接线", /id="tab-main"/.test(html) && /id="tab-settings"/.test(html));

    const stub = routeFetch(routes);
    const { document, state, switchView, collectSettings, validateSettings, saveSettings, onGateToggle, setGateBanner, toggleKey } = await boot(stub);

    assert("F3 初始不加载设置", state.settingsLoaded === false);
    assert(
      "F4 首屏没有请求 /api/settings（密钥不进首屏）",
      !stub.seen.some((s) => s.path === "/api/settings" && s.method === "GET"),
      JSON.stringify(stub.seen.map((s) => s.method + " " + s.path))
    );

    switchView("settings");
    await new Promise((r) => setTimeout(r, 30));

    assert("F5 切到设置页才加载", stub.seen.some((s) => s.path === "/api/settings" && s.method === "GET"));
    assert("F6 设置视图被显示", document.getElementById("view-settings").style.display === "grid");
    assert("F7 主视图被隐藏", document.getElementById("view-main").style.display === "none");

    assert(
      "F8 base_url 已回填",
      document.getElementById("s-base-url").value === "https://api.deepseek.com/v1",
      document.getElementById("s-base-url").value
    );
    assert(
      "F9 语言数组转成逗号分隔",
      document.getElementById("s-langs").value === "zh-CN, zh-Hans",
      document.getElementById("s-langs").value
    );
    assert(
      "F10 嵌套的 danmaku 已回填",
      document.getElementById("s-dm-max").value === String(settingsValues.collect.danmaku.max_items) &&
        document.getElementById("s-dm-hot").value === String(settingsValues.collect.danmaku.hot_items),
      document.getElementById("s-dm-max").value + "/" + document.getElementById("s-dm-hot").value
    );
    assert(
      "F10b thinking 与超时已回填",
      document.getElementById("s-thinking").value === "off" &&
        document.getElementById("s-timeout").value === String(settingsValues.llm.timeout_seconds),
      document.getElementById("s-thinking").value + "/" + document.getElementById("s-timeout").value
    );
    assert(
      "F11 密钥回显但默认掩着",
      document.getElementById("s-api-key").value === "sk-test-abcdef123456" &&
        document.getElementById("s-api-key").type === "password"
    );

    toggleKey();
    assert("F12 「显示」切成明文", document.getElementById("s-api-key").type === "text");
    toggleKey();
    assert("F13 再点切回掩码", document.getElementById("s-api-key").type === "password");

    // 最关键的一条：前端发出的键名必须与后端 Pydantic 逐个对齐。
    // 对不上会被 apply_settings 静默忽略——保存"成功"，但配置没生效。
    const sent = collectSettings();
    const sameKeys = (got, want, label) => {
      const g = Object.keys(got).sort().join(",");
      const w = want.slice().sort().join(",");
      assert("F14 字段名对齐 · " + label, g === w, "前端=[" + g + "] 后端=[" + w + "]");
    };
    sameKeys(sent, ["server", "llm", "collect", "publish", "storage", "confirm_disable_gate"], "顶层");
    sameKeys(sent.server, ["host", "port"], "server");
    sameKeys(sent.llm, ["base_url", "api_key", "model", "temperature", "max_tokens", "thinking", "role", "timeout_seconds", "max_input_chars", "chunk_size", "chunk_overlap"], "llm");
    sameKeys(sent.collect, ["prefer_subtitle_lang", "include_platform_summary", "danmaku"], "collect");
    sameKeys(sent.collect.danmaku, ["enabled", "also_with_subtitle", "max_items", "hot_items", "sample_items"], "collect.danmaku");
    sameKeys(sent.publish, ["require_confirm", "min_interval_seconds", "max_comment_chars", "disclose_ai", "disclosure_text"], "publish");
    sameKeys(sent.storage, ["db_path", "key_path"], "storage");

    assert(
      "F15 数字字段发出的是数字",
      typeof sent.llm.temperature === "number" && typeof sent.server.port === "number",
      typeof sent.llm.temperature + "/" + typeof sent.server.port
    );

    assert("F16 正常值通过校验", validateSettings() === "", validateSettings());
    document.getElementById("s-chunk-overlap").value = "99999";
    assert("F17 拦住 overlap >= size", validateSettings().includes("chunk_overlap"), validateSettings());
    document.getElementById("s-chunk-overlap").value = "400";

    document.getElementById("s-max-chars").value = "5000";
    assert("F18 拦住超过平台评论上限", validateSettings().includes("1000"), validateSettings());
    document.getElementById("s-max-chars").value = "900";

    document.getElementById("s-langs").value = "  ,  ";
    assert("F19 拦住空的字幕语言列表", validateSettings().includes("字幕语言"), validateSettings());
    document.getElementById("s-langs").value = "zh-CN, zh-Hans";

    document.getElementById("s-thinking").value = "sometimes";
    assert("F19b 拦住非法的 thinking 值", validateSettings().includes("thinking"), validateSettings());
    document.getElementById("s-thinking").value = "off";

    document.getElementById("s-require-confirm").checked = false;
    onGateToggle();
    assert("F20 取消勾选后展开后果确认区", document.getElementById("gate-confirm").classList.has("show") === true);
    assert("F21 未勾选确认时拦住保存", validateSettings().includes("闸门"), validateSettings());
    document.getElementById("s-gate-ack").checked = true;
    assert("F22 勾选确认后放行", validateSettings() === "", validateSettings());
    assert("F23 关闭闸门时一并发出确认标志", collectSettings().confirm_disable_gate === true);

    document.getElementById("s-require-confirm").checked = true;
    onGateToggle();
    assert("F24 重新勾选后收起确认区", document.getElementById("gate-confirm").classList.has("show") === false);

    const before = stub.seen.length;
    await saveSettings();
    const put = stub.seen.slice(before).find((s) => s.method === "PUT" && s.path === "/api/settings");
    assert(
      "F25 保存发出了 PUT /api/settings",
      !!put,
      JSON.stringify(stub.seen.slice(before).map((s) => s.method + " " + s.path))
    );
    let parsed = null;
    try { parsed = JSON.parse(put.body); } catch (e) { /* 下面会断言出来 */ }
    assert("F26 请求体是合法 JSON", !!parsed);
    assert(
      "F27 请求体带上了模型与密钥",
      !!parsed && parsed.llm.model === "deepseek-chat" && parsed.llm.api_key === "sk-test-abcdef123456"
    );
    assert(
      "F28 保存成功的提示里带出备份文件名",
      document.getElementById("settings-msg").textContent.includes("config.yaml.bak"),
      document.getElementById("settings-msg").textContent
    );
    // 保存后会把表单静默重拉一次（各字段要回到服务端的真值）。
    // 那次重拉曾经把上面这条回执清空——用户点保存看不到任何确认。
    assert(
      "F28b 保存后有重拉表单，且回执没被擦掉",
      stub.seen.slice(before).filter((s) => s.method === "GET" && s.path === "/api/settings").length === 1 &&
        document.getElementById("settings-msg").textContent !== "",
      "重拉次数=" + stub.seen.slice(before).filter((s) => s.method === "GET" && s.path === "/api/settings").length +
        " 回执=[" + document.getElementById("settings-msg").textContent + "]"
    );

    setGateBanner(true);
    assert("F29 闸门关闭时横幅可见", document.getElementById("gate-banner").style.display === "block");
    assert("F30 横幅说明了后果", document.getElementById("gate-banner").innerHTML.includes("没有确认环节"));
    setGateBanner(false);
    assert("F31 闸门恢复后横幅消失", document.getElementById("gate-banner").style.display === "none");
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
