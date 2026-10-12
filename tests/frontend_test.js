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
  // 迭代打磨的内联事件：脚本里定义处理函数，属性里接线。桩跑不到内联事件，
  // 所以"接线有没有接上"只能在这里查——而它接不上就等于按钮点了没反应。
  ["静态 11 · 迭代输入框接了 oninput", html.includes('id="refine-req"') && /<textarea[^>]+id="refine-req"[^>]+oninput="updateRefineButton\(\)"/.test(html)],
  ["静态 12 · 评论框同时刷新计数与手改提醒", html.includes('oninput="updateCounter(); updateRefineHint()"')],
  ["静态 13 · 再改一版按钮已接线", /<button[^>]+id="refine-btn"[^>]+onclick="refine\(\)"/.test(html)],
  ["静态 14 · 离线横幅也会禁用「再改一版」", /function showBackendBanner[\s\S]{0,1200}refine-btn/.test(code)],
  // 「展开全部差异」同理：桩里按钮点了不会触发内联事件，接线只能在源码上查
  ["静态 15 · 展开差异开关已接线", /<button[^>]+id="diff-toggle"[^>]+onclick="toggleDiffs\(\)"/.test(html)],
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
      " toggleKey, onGateToggle, setGateBanner," +
      " uploadSrt, clearSrt, onSrtPicked, submitTask, setSrtHint," +
      " renderPreview, pollTask, refine, updateRefineButton, applyRefineState," +
      " loadVersions, renderVersionList, renderUsageLine, showVersion, viewCurrent," +
      " restoreVersion, setVersionBadge, versionCostText, refreshTaskState, updateRefineHint," +
      " toggleDiffs, versionDiffText, versionDiffExamples };"
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
    seen.push({
      path,
      method,
      body: options && options.body,
      contentType: options && options.headers && options.headers["Content-Type"],
    });
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
  // 故意用一个不可能存在的版本号：如果前端把它渲染出来了，就证明页脚显示的是
  // **后端说的**，而不是前端另抄的一份。抄一份的数字会过期，而过期的那个还摆在界面上。
  version: "9.9.9-探头",
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
      "A7 离线时版本行如实说读不到（不显示一个猜的版本号）",
      /未连上后端/.test(document.getElementById("app-version").textContent),
      document.getElementById("app-version").textContent
    );
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
    assert(
      "B6 页脚显示后端报的版本号（不是前端另抄的一份）",
      document.getElementById("app-version").textContent.includes("9.9.9-探头"),
      document.getElementById("app-version").textContent
    );
    // 反过来也要守住：前端源码里不许出现写死的版本号，否则页脚会显示一个过期的数字
    assert(
      "B7 前端没有硬编码版本号",
      !/\bbili-scribe\s+\d+\.\d+\.\d+/.test(code),
      (code.match(/bili-scribe\s+\d+\.\d+\.\d+/) || ["（无）"])[0]
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
        proxy: "http://127.0.0.1:7897",
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
    sameKeys(sent.llm, ["base_url", "api_key", "proxy", "model", "temperature", "max_tokens", "thinking", "role", "timeout_seconds", "max_input_chars", "chunk_size", "chunk_overlap"], "llm");
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

    // 代理：既要能回填，也要能拦住漏了协议的写法。
    // 放过去的话它会写进 config.yaml，而加载配置时会校验协议前缀——
    // 保存那一刻不报错、下次启动直接起不来。
    assert(
      "F16b 代理地址已回填且原样发出",
      document.getElementById("s-proxy").value === "http://127.0.0.1:7897" &&
        sent.llm.proxy === "http://127.0.0.1:7897",
      document.getElementById("s-proxy").value
    );
    document.getElementById("s-proxy").value = "127.0.0.1:7897";
    assert(
      "F16c 拦住漏了协议的代理",
      validateSettings().includes("proxy"),
      validateSettings()
    );
    document.getElementById("s-proxy").value = "";
    assert("F16d 代理留空即直连，不拦", validateSettings() === "", validateSettings());
    document.getElementById("s-proxy").value = "http://127.0.0.1:7897";

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

  // G · 自己上传 srt 字幕
  {
    const TOKEN = "tok_abcdefghijklmnopqr";
    const routes = {
      "/api/config": { status: 200, body: baseCfg },
      "/api/auth/status": { status: 200, body: {} },
      "/api/publishes": { status: 200, body: [] },
      "/api/tasks": { status: 200, body: [] },
      "/api/roles": { status: 200, body: { roles: [{ key: "standard", name: "标准" }] } },
      "POST /api/subtitle/parse": {
        status: 200,
        body: {
          token: TOKEN,
          segments: 717,
          encoding: "utf-8-sig",
          first: "00:00:01",
          last: "01:59:23",
          chars: 12345,
          bytes: 45678,
        },
      },
      "POST /api/task": { status: 200, body: { task_id: "t-1", role: "standard" } },
      "GET /api/task/t-1": { status: 200, body: { status: "resolving", progress: "解析输入" } },
    };
    routes["DELETE /api/subtitle/" + TOKEN] = { status: 200, body: { removed: 1 } };

    const stub = routeFetch(routes);
    const { document, state, uploadSrt, clearSrt, submitTask } = await boot(stub);
    const fakeFile = (name) => ({
      name,
      size: 45678,
      async arrayBuffer() { return new ArrayBuffer(45678); },
    });

    await uploadSrt(fakeFile("lecture.srt"));
    assert("G1 上传成功后拿到 token", state.subtitleToken === TOKEN, String(state.subtitleToken));
    assert(
      "G2 回执显示了段数与覆盖范围",
      /717 段/.test(document.getElementById("srt-hint").textContent) &&
        /01:59:23/.test(document.getElementById("srt-hint").textContent),
      document.getElementById("srt-hint").textContent
    );
    assert(
      "G3 上传发的是原始字节（octet-stream），不是 JSON",
      stub.seen.some(
        (s) =>
          s.method === "POST" &&
          s.path === "/api/subtitle/parse" &&
          s.contentType === "application/octet-stream"
      ),
      JSON.stringify(stub.seen.filter((s) => s.path === "/api/subtitle/parse").map((s) => s.contentType))
    );
    assert(
      "G11 选完文件后按钮上显示文件名（否则使用者会怀疑没选上）",
      document.getElementById("srt-btn").textContent === "lecture.srt",
      document.getElementById("srt-btn").textContent
    );

    await clearSrt();
    assert("G4 清除后 token 归 null", state.subtitleToken === null, String(state.subtitleToken));
    assert(
      "G5 清除会通知服务端真删掉",
      stub.seen.some((s) => s.method === "DELETE" && s.path === "/api/subtitle/" + TOKEN),
      JSON.stringify(stub.seen.map((s) => s.method + " " + s.path))
    );
    assert(
      "G12 清除后按钮回到默认文字",
      document.getElementById("srt-btn").textContent === "选择字幕文件",
      document.getElementById("srt-btn").textContent
    );

    // 再传一次，这次走完整提交：token 要进请求体，提交完要忘掉它
    await uploadSrt(fakeFile("lecture.srt"));
    document.getElementById("raw-input").value = "BV1smoke";
    await submitTask();
    const posts = stub.seen.filter((s) => s.method === "POST" && s.path === "/api/task");
    let sentBody = null;
    try {
      sentBody = JSON.parse(posts[posts.length - 1].body);
    } catch (e) {
      /* 由下面的断言暴露 */
    }
    assert("G6 提交任务时带上了字幕 token", !!sentBody && sentBody.subtitle_token === TOKEN, JSON.stringify(sentBody));
    assert("G7 提交成功后忘掉 token", state.subtitleToken === null, String(state.subtitleToken));
  }

  // G2 · 解析失败时不能留下残状态
  {
    const stub = routeFetch({
      "/api/config": { status: 200, body: baseCfg },
      "POST /api/subtitle/parse": { status: 400, body: { detail: "读不了这个文件：认不出编码" } },
    });
    const { document, state, uploadSrt } = await boot(stub);
    await uploadSrt({ name: "bad.srt", size: 12, async arrayBuffer() { return new ArrayBuffer(12); } });
    assert("G8 解析失败时不留 token", state.subtitleToken === null, String(state.subtitleToken));
    assert(
      "G9 失败原因显示出来了",
      /读不了/.test(document.getElementById("srt-hint").textContent),
      document.getElementById("srt-hint").textContent
    );
    assert("G10 失败时清除按钮不出现", document.getElementById("srt-clear").style.display === "none");
    assert(
      "G13 解析失败后按钮也回到默认文字",
      document.getElementById("srt-btn").textContent === "选择字幕文件",
      document.getElementById("srt-btn").textContent
    );
  }

  // H · 迭代打磨：能不能改、改完确认要不要重来、翻历史版会不会发错东西
  {
    const versionRow = (no, source, extra) => Object.assign({
      version_no: no,
      source: source,
      requirement: source === "initial" ? "" : "把第三节展开",
      role: "standard",
      created_at: "2026-09-30T21:00:0" + no + "+08:00",
      one_line: "第 " + no + " 版的一句话",
      revise_note: source === "refine" ? "展开了第三节，删掉了两处比喻" : "",
      // 「实际改动」＝本地比对算出来的，和上面那句模型自述是两回事。
      // 三种状态：null = 没比过（初版 / 回退版）；[] = 比过没变；非空 = 逐字段变了。
      changed_fields: (source === "initial" || source === "restore") ? null : [{
        field: "one_line", label: "一句话结论", kind: "text",
        detail: "47 → 37 字", ratio: 0.48, example: "「分」→「剖」",
      }],
      metrics: source === "restore" ? {} : { usage: { calls: no + 1, prompt_tokens: 1000 * no, completion_tokens: 100 * no, total_tokens: 1100 * no, unpriced: 0 }, elapsed: 30 + no, chunks: 2, frames: 12 },
      current: false,
    }, extra || {});

    const taskWith = (extra) => Object.assign({
      id: "t1",
      bvid: "BV1probe",
      status: "ready",
      title: "探针",
      source_label: "平台 AI 字幕",
      source_trust: "中",
      warnings: [],
      preview_md: "当前版的预览",
      publish_document: "当前版要发出去的正文",
      comment_text: "当前版要发出去的正文",
      current_version: 2,
      version_count: 2,
      can_refine: true,
      refine_blocked: "",
      live_publish: null,
      summary_json: { one_line: "当前版一句话", role: "standard", revise_note: "展开了第三节" },
    }, extra || {});

    const versionsBody = {
      current: 2,
      total: { calls: 5, prompt_tokens: 4000, completion_tokens: 400, reasoning_tokens: 0, total_tokens: 4400, unpriced: 0, untracked: 0 },
      versions: [versionRow(1, "initial"), versionRow(2, "refine", { current: true })],
    };

    const makeRoutes = (task, versions) => ({
      "/api/config": { status: 200, body: baseCfg },
      "GET /api/task/t1": { status: 200, body: task },
      "GET /api/task/t1/versions": { status: 200, body: versions || versionsBody },
      "POST /api/task/t1/refine": { status: 200, body: { task_id: "t1", status: "refining", base_version: 2, reused_prepared: true } },
      "GET /api/task/t1/versions/1": {
        status: 200,
        body: Object.assign(versionRow(1, "initial"), { preview_md: "第 1 版的预览", comment_text: "第1版正文", publish_document: "**第 1 版**" }),
      },
      "POST /api/task/t1/versions/1/restore": { status: 200, body: { ok: true, version_no: 3, restored_from: 1, can_refine: true, refine_blocked: "" } },
    });

    // H1–H5 · 提交一次迭代
    {
      const task = taskWith();
      const stub = routeFetch(makeRoutes(task));
      const { document, state, refine, pollTask, renderPreview, updateRefineButton } = await boot(stub);

      state.taskId = "t1";
      renderPreview(task);
      await new Promise((r) => setTimeout(r, 20));

      const btn = document.getElementById("refine-btn");
      assert("H1 没写要求时按钮是禁用的", btn.disabled === true, String(btn.disabled));
      document.getElementById("refine-req").value = "把第三节展开";
      updateRefineButton();
      assert("H2 写了要求就能改", btn.disabled === false, String(btn.disabled));

      // 模拟"上一版已经过目并勾了确认"——迭代结束后这个勾必须被清掉
      document.getElementById("confirm-box").checked = true;
      await refine();

      const posts = stub.seen.filter((s) => s.method === "POST" && s.path === "/api/task/t1/refine");
      let body = null;
      try { body = JSON.parse(posts[0].body); } catch (e) { /* 下面的断言会暴露 */ }
      assert(
        "H3 提交时带上「我在看第几版」",
        !!body && body.base_version === 2 && body.requirement === "把第三节展开",
        JSON.stringify(body)
      );
      assert("H4 提交后记下了这一版的底稿版本", state.awaitingRefine === 2, String(state.awaitingRefine));
      assert(
        "H5 提交后清空了输入框（否则会以为没提交上，反复点）",
        document.getElementById("refine-req").value === "",
        document.getElementById("refine-req").value
      );

      // 还没出结果时轮询读到的还是旧的 ready：不能就此收工，否则新版永远取不回来
      const stale = taskWith({ current_version: 2 });
      stub.seen.length = 0;
      const fresh = taskWith({ current_version: 3, version_count: 3, publish_document: "改后要发出去的正文", preview_md: "改后的预览", summary_json: { role: "standard", revise_note: "展开了第三节，删掉了两处比喻" } });
      let served = stale;
      const fetch2 = async (path, options) => {
        const method = (options && options.method) || "GET";
        if (method === "GET" && path === "/api/task/t1") {
          return { ok: true, status: 200, text: async () => JSON.stringify(served) };
        }
        if (method === "GET" && path === "/api/task/t1/versions") {
          return { ok: true, status: 200, text: async () => JSON.stringify({ current: served.current_version, total: versionsBody.total, versions: versionsBody.versions }) };
        }
        return { ok: false, status: 404, text: async () => JSON.stringify({ detail: "no route" }) };
      };
      const boot2 = await boot(fetch2);
      boot2.state.taskId = "t1";
      boot2.state.awaitingRefine = 2;
      boot2.state.currentVersion = 2;
      boot2.document.getElementById("confirm-box").checked = true;
      await boot2.pollTask();
      assert(
        "H6 新那版还没出来时继续等，不当作已完成",
        boot2.state.awaitingRefine === 2 && boot2.document.getElementById("confirm-box").checked === true,
        "awaitingRefine=" + boot2.state.awaitingRefine
      );

      served = fresh;
      await boot2.pollTask();
      assert(
        "H7 新版出来后确认勾选被重置（内容变了，旧确认不作数）",
        boot2.document.getElementById("confirm-box").checked === false,
        String(boot2.document.getElementById("confirm-box").checked)
      );
      assert(
        "H8 发布框换成新版正文",
        boot2.document.getElementById("comment-box").value === "改后要发出去的正文",
        boot2.document.getElementById("comment-box").value
      );
      assert(
        "H9 把模型说的改动说明摆出来",
        /展开了第三节/.test(boot2.document.getElementById("refine-msg").textContent),
        boot2.document.getElementById("refine-msg").textContent
      );
      assert("H10 收工后清掉等待标记", boot2.state.awaitingRefine === null);
    }

    // H11–H14 · 不能改的时候要说清为什么
    {
      const reasons = [
        ["已发布", "这条视频的总结已经发出去了（撤回前不能再改）。要改得先撤回。"],
        ["缓存失效", "这条视频的素材缓存已经失效（程序重启过就会这样）。重新跑一次。"],
      ];
      for (const [label, why] of reasons) {
        const stub = routeFetch(makeRoutes(taskWith({ can_refine: false, refine_blocked: why })));
        const { document, state, renderPreview } = await boot(stub);
        state.taskId = "t1";
        renderPreview(taskWith({ can_refine: false, refine_blocked: why }));
        await new Promise((r) => setTimeout(r, 20));

        assert("H11 " + label + "时按钮禁用", document.getElementById("refine-btn").disabled === true);
        assert(
          "H12 " + label + "时把原因显示出来",
          document.getElementById("refine-blocked").style.display === "block" &&
            document.getElementById("refine-blocked").innerHTML.includes(why.slice(0, 12)),
          document.getElementById("refine-blocked").innerHTML
        );
        assert("H13 " + label + "时输入框也禁用", document.getElementById("refine-req").disabled === true);
      }
    }

    // H15–H19 · 版本列表与翻看历史版
    {
      const stub = routeFetch(makeRoutes(taskWith()));
      const { document, state, renderPreview, showVersion, viewCurrent, restoreVersion } = await boot(stub);
      state.taskId = "t1";
      renderPreview(taskWith());
      await new Promise((r) => setTimeout(r, 30));

      assert(
        "H15 版本列表把两版都列出来",
        document.getElementById("version-list").innerHTML.includes("第 1 版") &&
          document.getElementById("version-list").innerHTML.includes("第 2 版"),
        document.getElementById("version-list").innerHTML.slice(0, 80)
      );
      assert(
        "H16 当前版有标记、历史版有入口",
        document.getElementById("version-list").innerHTML.includes("当前") &&
          document.getElementById("version-list").innerHTML.includes("showVersion(1)"),
        document.getElementById("version-list").innerHTML.slice(0, 160)
      );
      assert(
        "H17 累计消耗显示在最上面那行",
        /合计 4,400 tokens/.test(document.getElementById("usage-line").textContent),
        document.getElementById("usage-line").textContent
      );
      assert(
        "H18 版本号显示出来了",
        /第 2 版/.test(document.getElementById("version-badge").textContent),
        document.getElementById("version-badge").textContent
      );

      await showVersion(1);
      assert(
        "H19 翻历史版只读：发布区被收起来",
        document.getElementById("publish-block").style.display === "none" &&
          document.getElementById("refine-block").style.display === "none",
        document.getElementById("publish-block").style.display + "/" + document.getElementById("refine-block").style.display
      );
      assert("H20 历史版预览换成了那一版", document.getElementById("preview-md").textContent === "第 1 版的预览");      assert(
        "H21 历史版有回到当前的出口，也说清了发出去的永远是当前版",
        document.getElementById("history-note").innerHTML.includes("viewCurrent()") &&
          document.getElementById("history-note").innerHTML.includes("永远是当前版"),
        document.getElementById("history-note").innerHTML.slice(0, 120)
      );
      assert("H22 翻历史版时不能再改", document.getElementById("refine-btn").disabled === true);

      viewCurrent();
      assert(
        "H23 回到当前版后发布区恢复、预览回到当前版",
        document.getElementById("publish-block").style.display === "" &&
          document.getElementById("preview-md").textContent === "当前版的预览",
        document.getElementById("preview-md").textContent
      );

      // 回退：追加一版，不动已有版本
      await restoreVersion(1);
      const restorePosts = stub.seen.filter((s) => s.path === "/api/task/t1/versions/1/restore");
      assert("H24 回退走的是 restore 接口", restorePosts.length === 1, JSON.stringify(stub.seen.map((s) => s.method + " " + s.path)));
      assert(
        "H25 回退的回执说明了没有消耗",
        /没有花任何模型调用/.test(document.getElementById("refine-msg").textContent),
        document.getElementById("refine-msg").textContent
      );
    }

    // H26–H27 · 离线与"手改过正文"的提醒
    {
      const { document } = await boot(async () => { throw new TypeError("Failed to fetch"); });
      assert("H26 离线时再改一版也是禁用的", document.getElementById("refine-btn").disabled === true);
    }
    {
      const stub = routeFetch(makeRoutes(taskWith()));
      const { document, state, renderPreview, updateRefineHint } = await boot(stub);
      state.taskId = "t1";
      renderPreview(taskWith());
      await new Promise((r) => setTimeout(r, 20));
      document.getElementById("comment-box").value = "我自己手改过的正文";
      // 桩里没有内联事件（oninput 是 HTML 属性，不由脚本定义），所以直接调它的处理函数。
      // 属性本身是否接线由下面的静态断言看着。
      updateRefineHint();
      assert(
        "H27 手改过正文时提醒会被覆盖（迭代基于的是模型那一版）",
        /手改/.test(document.getElementById("refine-hint").textContent),
        document.getElementById("refine-hint").textContent
      );
    }

    // H28–H32 · 「实际改动」：本地比对的结果，和模型自述并排显示
    //
    // 这条守的是一个真实事故：模型写着"其余部分一个字都别动"（说明里也这么写），
    // 而 key_points 六条里有一条被实质改写了。自述不可靠，所以界面上必须同时有
    // 一行**算出来的**东西。三种状态要分得开，混了就等于替模型圆谎。
    {
      const { document, state, renderPreview } = await boot(routeFetch(makeRoutes(taskWith())));
      state.taskId = "t1";
      renderPreview(taskWith());
      await new Promise((r) => setTimeout(r, 30));
      const html = document.getElementById("version-list").innerHTML;

      assert(
        "H28 改动版把「实际改动」逐字段列出来（带相似度）",
        html.includes("实际改动：一句话结论（47 → 37 字，相似度 0.48）"),
        html.slice(0, 260)
      );
      assert(
        "H29 具体差异放在 title 里（鼠标停上去能看到）",
        html.includes('title="一句话结论：「分」→「剖」"'),
        (html.match(/title="[^"]*"/) || ["（没有 title）"])[0]
      );
      assert(
        "H30 没比过的不显示这一行（初版只有一条实际改动）",
        (html.split("实际改动").length - 1) === 1,
        "出现次数 " + (html.split("实际改动").length - 1)
      );
      assert(
        "H31 模型自述与本地比对并排显示，谁也不冒充谁",
        html.includes("改动说明：展开了第三节") && html.includes("实际改动："),
        html.slice(0, 320)
      );
    }
    {
      // 单独一版：逐字相同（[]）与回退（null）必须说不同的话
      const versions = {
        current: 3,
        total: versionsBody.total,
        versions: [
          versionRow(1, "initial"),
          versionRow(2, "refine", { changed_fields: [] }),          // 比过：一个字没变
          versionRow(3, "restore", { current: true }),              // 没比：回退不是"改"
        ],
      };
      const { document, state, renderPreview } = await boot(routeFetch(makeRoutes(taskWith(), versions)));
      state.taskId = "t1";
      renderPreview(taskWith());
      await new Promise((r) => setTimeout(r, 30));
      const html = document.getElementById("version-list").innerHTML;

      assert(
        "H32 逐字相同时说清是「要求没被落实」",
        html.includes("无 —— 和上一版逐字相同（要求没被落实）"),
        html.slice(0, 260)
      );
      assert(
        "H33 回退版不显示字段级 diff（它没有「改」这回事）",
        (html.split("实际改动").length - 1) === 1,
        "出现次数 " + (html.split("实际改动").length - 1)
      );
      assert(
        "H34 没有差异可展开时，那个开关根本不出现（不给假入口）",
        document.getElementById("diff-toggle-row").style.display === "none",
        document.getElementById("diff-toggle-row").style.display
      );
    }

    // H35–H38 · 「展开全部差异」开关
    //
    // 为什么要有这个开关：具体差异默认只放在 title 里（列表才好扫），但**悬停是个
    // 极容易被忽略的交互**——而"改成了什么"恰恰是判断这次改动能不能接受的唯一依据。
    // 只放 title 就等于这个功能对不悬停的人不存在。
    {
      const stub = routeFetch(makeRoutes(taskWith()));
      const { document, state, renderPreview, toggleDiffs, renderVersionList } = await boot(stub);
      state.taskId = "t1";
      renderPreview(taskWith());
      await new Promise((r) => setTimeout(r, 30));
      const html = () => document.getElementById("version-list").innerHTML;
      const btn = () => document.getElementById("diff-toggle");

      assert(
        "H35 有差异时出现开关，默认收起（细节不铺在列表里）",
        document.getElementById("diff-toggle-row").style.display !== "none" &&
          btn().textContent === "展开全部差异" &&
          !html().includes("例 · "),
        btn().textContent + " / 例出现 " + (html().split("例 · ").length - 1) + " 次"
      );

      toggleDiffs();
      assert(
        "H36 点开后把具体差异铺进列表",
        html().includes("例 · 一句话结论：「分」→「剖」"),
        html().slice(0, 300)
      );
      assert("H37 按钮文字跟着改成「收起差异」", btn().textContent === "收起差异", btn().textContent);

      toggleDiffs();
      assert(
        "H38 再点一下收回去，回到只看字段名",
        !html().includes("例 · ") && btn().textContent === "展开全部差异",
        btn().textContent
      );

      // 每迭代一版都会重新拉一次版本列表。开关状态存在 state 里，所以刷新不该把它
      // 打回去——否则每次改完一版都得重新点开，等于没有这个开关。
      toggleDiffs();
      renderVersionList();
      assert(
        "H39 刷新版本列表后仍保持展开（不然每改一版就白展开了）",
        html().includes("例 · ") && btn().textContent === "收起差异",
        btn().textContent + " / 例出现 " + (html().split("例 · ").length - 1) + " 次"
      );
    }
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
