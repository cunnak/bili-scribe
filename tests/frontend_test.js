/* ============================================================================
   前端测试（桌面版）

   跑法：  node tests/frontend_test.js            全部
           node tests/frontend_test.js 状态机      只跑名字含「状态机」的

   设计取舍，都是为了「失败时能说清原因」：
   1. **jsdom 真跑页面，不抠字符串。** 上一代测试用 `html.match()` 在源码里找
      片段，结果是 HTML 改一次、142 项全绿或全红，与人看到的行为无关。
   2. **每个用例一个全新 jsdom。** 约 80ms 的代价，换来用例之间零共享状态。
   3. **桥必须在脚本执行前注入。** 页面末尾是
      `if (window.pywebview && window.pywebview.api) boot()`，而 `new JSDOM()`
      是同步跑完 `<script>` 才返回的。构造后再写 `window.pywebview` 已经晚了，
      页面既没 boot 也没注册监听器——所以用 `beforeParse` 钩子。
   4. **页面脚本是顶层 `const S`，它进的是全局词法环境，不是 `window` 属性。**
      所以 `win.S` 永远是 undefined，要读状态得走 `win.eval("S")`。
      顶层 `function` 声明会挂到 window 上，那是能直接调的。
   5. 断言一律看**用户能看见的东西**（DOM 文本、按钮 disabled）或**前端到底向
      后端要了什么**（假后端记的调用）。不去读内部变量，除非没有别的观测点。
   ============================================================================ */

const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const only = process.argv[2] && !process.argv[2].startsWith("-") ? process.argv[2] : null;
const htmlPath = path.join(__dirname, "..", "bili_scribe", "app", "static", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

/* ============================================================ 断言收集 */

const results = [];

/* 分组在**定义时**捕获，不是在执行时读。
   用例是 async 的，交错执行时读全局 currentGroup 会把所有结果记到最后
   一个分组名下，失败详情会指错地方。 */
function group(name) {
  return name;
}

function assert(groupName, name, cond, detail) {
  results.push({ group: groupName, name, ok: !!cond, detail: cond ? "" : detail || "" });
}

function eq(groupName, name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  assert(groupName, name, ok,
    "期望 " + JSON.stringify(expected) + "，实际 " + JSON.stringify(actual));
}

/* ============================================================ 假后端 */

/* 一个够用的假桥。形状严格对齐 `bili_scribe/desktop.py` 的 `DesktopApi`
   —— 每个方法都返回 `{ok:true,data}` 或 `{ok:false,error,status}`，
   因为前端的 `call()` 就是这么解包的。

   `state` 里的东西可以在用例中途改，用来模拟「后端状态变了」。
   `calls` 记录每次调用的方法名与参数，用来断言「前端要了什么」。 */
function makeBackend(overrides) {
  const calls = [];
  const state = {
    config: {
      version: "0.6.0",
      llm: { model: "qwen-test", base_url: "https://example.invalid/v1", configured: true, role: "wengao" },
      storage: { db_path: "/home/u/.bili-scribe/bili-scribe.db" },
      publish: { require_confirm: true },
      features: { qr_login: true },
    },
    auth: { logged_in: false, identity: null },
    roles: {
      roles: [
        { key: "standard", name: "标准", tagline: "", note: "", passes: 1 },
        { key: "wengao", name: "文稿", tagline: "", note: "", passes: 3 },
      ],
      current: "wengao",
    },
    task: null,
    versions: { current: null, total: null, versions: [] },
    publishes: [],
    history: [],
    notes: { entries: 0, bytes: 0, dir: "/home/u/.bili-scribe/notes", session_hits: 0, session_misses: 0 },
    settings: {
      values: { llm: { model: "qwen-test", base_url: "https://example.invalid/v1", api_key: "sk-x", role: "wengao" } },
      path: "/home/u/.bili-scribe/config.yaml",
      config_exists: true,
      gate_disabled: false,
      restart_needed_for: ["server"],
      platform_comment_max: 1000,
    },
  };

  const ok = (v) => ({ ok: true, data: v });

  /* 故意用最简单、最没想象力的写法。所有方法都只是「返回一份数据」，
     真正的观测点在外面（DOM 与 calls）。 */
  const base = {
    mark_ready: () => ok(null),
    get_config: () => ok(state.config),
    check_llm: () => ok({ ok: true, detail: "可用", model: "qwen-test" }),
    get_roles: () => ok(state.roles),
    /* 切角色。真后端只改 llm.role 这一项并写盘，这里跟着改内存里的两份副本：
       config.llm.role（顶栏 chip 读它）与 settings.values.llm.role（抽屉隐藏字段读它）。
       两边都改才能测出"保存设置时不会把角色写回旧值"。 */
    set_role: (p) => {
      const key = (p && p.role) || "";
      const hit = state.roles.roles.find((r) => r.key === key);
      if (!hit) return { ok: false, error: "未知的总结角色：" + key };
      state.roles.current = key;
      state.config.llm.role = key;
      state.settings.values.llm.role = key;
      return ok({ role: hit.key, name: hit.name, passes: hit.passes });
    },

    get_settings: () => ok(state.settings),
    save_settings: (p) => ok({ written: p, restart_required: false }),
    test_llm: () => ok({ ok: true, detail: "可用", model: "qwen-test", saved: false }),

    auth_status: () => ok(state.auth),
    auth_qrcode: () => ok({ key: "k1", url: "https://qr.example/x", image: "data:image/png;base64,AAA" }),
    auth_poll: () => ok({ status: 2 }),
    auth_cookie: () => ok({ logged_in: true }),
    auth_logout: () => ok({ logged_in: false }),

    pick_subtitle: () => ok({ cancelled: true }),
    discard_subtitle: () => ok({ discarded: true }),

    create_task: (p) => {
      state.task = sampleTask({ id: "t1", status: "resolving", progress: "解析输入" });
      return ok({ task_id: "t1", role: "wengao" });
    },
    get_task: (id) => ok(state.task || sampleTask({ id })),
    list_versions: () => ok(state.versions),
    get_version: (id, n) => ok(versionDetail(n)),
    restore_version: (id, n) => ok({ restored: n }),
    refine_task: (id) => ok({ task_id: id, status: "refining", base_version: 1, reused_prepared: true }),
    list_tasks: () => ok([]),
    list_history: () => ok(state.history),
    publish: () => ok({ status: "published", publish_id: 9, rpid: "123" }),
    withdraw: (pid) => ok({ publish_id: pid, withdrawn: true }),
    list_publishes: () => ok(state.publishes),
    notes_stats: () => ok(state.notes),
    clear_notes: () => ok({ removed: 3 }),
    open_data_dir: () => ok({ opened: true }),
  };

  /* 自动记录每一次调用。手写 track() 的版本会在"用例用 overrides 换掉了某个
     方法"时漏记——那个缺陷曾经让两条断言凭空失败，而失败原因指向被测代码。 */
  const api = {};
  const impl = Object.assign({}, base, overrides || {});
  for (const name of Object.keys(impl)) {
    api[name] = (...args) => {
      calls.push({ name, args });
      return impl[name](...args);
    };
  }

  return { api, calls, state, ok, base };
}

/* 一条完整的任务。字段对齐 `service.get_task()` 的注入结果：
   `summary_json`（不是 summary）、`publish_document`、`can_refine`、
   `refine_blocked`、`refining`、`version_count`、`current_version`、`live_publish`。 */
let TASK_SEQ = 0;
function sampleTask(patch) {
  TASK_SEQ++;
  const t = {
    id: "tk" + TASK_SEQ,
    bvid: "BV1xx411c7mD",
    aid: 1,
    title: "一个用于测试的视频标题",
    up_name: "某UP",
    duration: 600,
    status: "ready",
    progress: "",
    raw_input: "BV1xx411c7mD",
    source_kind: "subtitle",
    source_label: "字幕（人工）",
    source_trust: "高",
    warnings: [],
    candidates: [],
    summary_json: {
      one_line: "一句话结论。",
      summary_md: "## 第一节\n\n正文第一段。\n\n## 第二节\n\n正文第二段。",
      key_points: ["要点一", "要点二"],
      caution: "",
      comment_short: "短评。",
      role: "wengao",
    },
    preview_md: "# 预览",
    platform_summary: "",
    comment_text: "",
    error: null,
    created_at: "2026-10-27 10:00",
    updated_at: "2026-10-27 10:05",
    publish_document: "这是要发到评论区的完整文本。",
    can_refine: true,
    refine_blocked: "",
    refining: false,
    version_count: 1,
    current_version: 1,
    live_publish: null,
  };
  return Object.assign(t, patch || {});
}

function versionDetail(n) {
  return {
    version_no: n,
    source: n === 1 ? "initial" : "refine",
    requirement: "",
    role: "wengao",
    created_at: "2026-10-27 10:00",
    one_line: "第 " + n + " 版的一句话",
    revise_note: "",
    changed_fields: null,
    summary_json: { one_line: "第 " + n + " 版的一句话", summary_md: "第 " + n + " 版正文" },
    preview_md: "",
    comment_text: "",
    publish_document: "第 " + n + " 版的发布文本",
    metrics: {},
    current: true,
  };
}

/* ============================================================ 载入页面 */

async function loadPage(opts) {
  const o = opts || {};
  const be = o.backend || makeBackend();

  const vc = new VirtualConsole();
  const errors = [];
  vc.on("jsdomError", (e) => errors.push(String(e && e.message ? e.message : e)));
  vc.on("error", (...a) => errors.push(a.map(String).join(" ")));

  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    url: "http://localhost/index.html",
    pretendToBeVisual: true,
    virtualConsole: vc,
    /* 关键：桥必须在页面脚本执行**之前**挂好，否则页面末尾那句
       `if (window.pywebview && window.pywebview.api) boot()` 走 else 分支。 */
    beforeParse(window) {
      window.pywebview = { api: be.api };
    },
  });
  const win = dom.window;

  await settle(win, o.settleMs);

  return {
    dom,
    win,
    doc: win.document,
    backend: be,
    errors,
    /* 顶层的 `const S` 不在 window 上，只能通过 eval 读。
       返回的是同一个对象引用，改它等于改页面状态。 */
    S: () => win.eval("S"),
    $: (id) => win.document.getElementById(id),
    text: (id) => {
      const el = win.document.getElementById(id);
      return el ? el.textContent : null;
    },
    close: () => {
      try {
        const s = win.eval("S");
        if (s && s.qrTimer) win.clearInterval(s.qrTimer);
      } catch (e) { /* 已经关了 */ }
      try { win.close(); } catch (e) { /* 同上 */ }
    },
  };
}

/* boot() 里有多层 await。用 10ms 的宏任务循环推进，直到页面进入
   「配置已拿到」或超时。比固定 sleep 既快又稳。 */
function settle(win, maxMs) {
  const limit = Date.now() + (maxMs || 3000);
  return new Promise((resolve) => {
    function tick() {
      let online = false;
      try { online = !!win.eval("typeof S !== 'undefined' && S.online"); } catch (e) { /* 脚本还没跑 */ }
      if (online || Date.now() > limit) return resolve();
      setTimeout(tick, 10);
    }
    tick();
  });
}

/* 派发真实事件。只改 value 不派 input 事件，监听器不会被触发。 */
function typeInto(p, el, value) {
  el.value = value;
  el.dispatchEvent(new p.win.Event("input", { bubbles: true }));
}

function click(p, el) {
  el.dispatchEvent(new p.win.MouseEvent("click", { bubbles: true }));
}

function wait(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/* ============================================================ 用例表 */

const CASES = [];
function test(name, fn) {
  CASES.push({ name, fn });
}

/* 提交一条任务。多个用例都要这几步。 */
async function submitTask(p, raw) {
  typeInto(p, p.$("raw-input"), raw || "BV1xx411c7mD");
  click(p, p.$("run-btn"));
  await wait(60);
}

/* 让页面手上有一条「已完成」的任务，用于测版本/发布/打磨。
   走真实路径：提交 → 后端推送 ready。

   `state.versions` 必须跟着设：`loadVersions()` 会用 `d.current` **覆盖**
   `S.currentVersion`，这是有意的——版本列表才是版号的权威来源。假后端如果
   在这里留 `current: null`，页面就会认为当前没有版号，打磨也就不会带底稿。 */
async function readyTask(p, patch) {
  await submitTask(p);
  const t = sampleTask(Object.assign({ id: "t1", status: "ready" }, patch || {}));
  if (p.backend.state) {
    p.backend.state.task = t;
    p.backend.state.versions = {
      current: t.current_version,
      total: null,
      versions: [{ version_no: t.current_version, source: "initial", one_line: "第一版", metrics: {}, changed_fields: null }],
    };
  }
  p.win.__onTaskUpdate(t);
  await wait(60);
}

/* ============================================================ 一、启动 */

const G1 = group("启动");

test("页面加载后从后端拿到配置与角色，界面进入在线状态", async () => {
  const p = await loadPage();
  try {
    assert(G1, "在线状态已置位", p.S().online === true, "boot() 没有跑完，S.online = " + p.S().online);
    assert(G1, "模型名进了顶栏", /qwen-test/.test(p.text("chip-model")), "chip-model = " + p.text("chip-model"));
    assert(G1, "角色表已载入", p.S().roles.length === 2, JSON.stringify(p.S().roles));
    assert(G1, "断桥横幅没有出现", !p.$("bridge").classList.contains("on"), "横幅说明 boot 失败了");
    eq(G1, "没有 jsdom 级异常", p.errors, []);
  } finally { p.close(); }
});

test("没有桥的时候，页面明说连不上接口，而不是静默失效", async () => {
  /* 传一个空 api：mark_ready 不存在 → call() 抛错 → boot 走失败分支。 */
  const p = await loadPage({ backend: { api: {} }, settleMs: 500 });
  try {
    const banner = p.$("bridge");
    assert(G1, "断桥横幅出现", banner.classList.contains("on"), "bridge 没有 .on");
    assert(G1, "横幅文案告诉人怎么办", /重启|退出|重新/.test(banner.textContent), banner.textContent);
  } finally { p.close(); }
});

test("顶栏时钟按 YYYY-MM-DD HH:MM 显示", async () => {
  const p = await loadPage();
  try {
    const t = p.text("ft-clock");
    assert(G1, "时钟格式正确", /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(t), "实际：" + t);
  } finally { p.close(); }
});

test("刻度尺建出 64 根刻度", async () => {
  const p = await loadPage();
  try {
    eq(G1, "刻度数", p.$("ruler").children.length, 64);
  } finally { p.close(); }
});

test("底栏只显示数据库文件名，不泄露完整路径", async () => {
  const p = await loadPage();
  try {
    const t = p.text("ft-note");
    assert(G1, "显示文件名", /bili-scribe\.db/.test(t), t);
    assert(G1, "不含目录分隔符", !/[\\/]/.test(t), "泄露了路径：" + t);
  } finally { p.close(); }
});

test("底栏版本号来自后端，后端给什么就显示什么", async () => {
  /* 夹具故意用一个不存在的版本号：前端要是自己抄了一份（比如写死成 0.4.2），
     对它自己那一份和这里都对不上，这条就会红。 */
  const p = await loadPage();
  try {
    eq(G1, "显示后端报的版本号", p.text("ft-ver"), "v0.6.0");
  } finally { p.close(); }
});

test("启动时自动恢复最近一条任务，而不是从空白开始", async () => {
  const be = makeBackend({
    list_tasks: () => ({ ok: true, data: [{ id: "recent1", title: "最近的", status: "ready" }] }),
    get_task: () => ({ ok: true, data: sampleTask({ id: "recent1", title: "最近的视频" }) }),
  });
  const p = await loadPage({ backend: be });
  try {
    assert(G1, "任务标题已显示", /最近的视频/.test(p.text("t-title")), p.text("t-title"));
    const lt = p.backend.calls.filter((c) => c.name === "list_tasks");
    eq(G1, "只问 1 条", lt.length ? lt[0].args : null, [1]);
  } finally { p.close(); }
});

/* ============================================================ 二、提交 */

const G2 = group("提交任务");

test("输入为空时提交按钮禁用，输入后解禁", async () => {
  const p = await loadPage();
  try {
    const btn = p.$("run-btn");
    assert(G2, "空输入 → 禁用", btn.disabled === true, "没有禁用");

    typeInto(p, p.$("raw-input"), "https://www.bilibili.com/video/BV1xx411c7mD");
    assert(G2, "有输入 → 解禁", btn.disabled === false, "仍然禁用");
  } finally { p.close(); }
});

test("点开始总结，把输入原样交给后端", async () => {
  const p = await loadPage();
  try {
    const raw = "https://www.bilibili.com/video/BV1xx411c7mD?p=2";
    await submitTask(p, raw);

    const c = p.backend.calls.find((x) => x.name === "create_task");
    assert(G2, "确实调了 create_task", !!c, "没调用");
    eq(G2, "输入原样传过去（不截断 query）", c ? c.args[0].raw_input : null, raw);
  } finally { p.close(); }
});

test("回车键等同于点按钮", async () => {
  const p = await loadPage();
  try {
    const inp = p.$("raw-input");
    typeInto(p, inp, "BV1xx411c7mD");
    inp.dispatchEvent(new p.win.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await wait(80);
    assert(G2, "回车也调了 create_task",
      !!p.backend.calls.find((x) => x.name === "create_task"), "回车无效");
  } finally { p.close(); }
});

test("任务在跑的时候不能再提交一次", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    p.win.__onTaskUpdate(sampleTask({ id: "t1", status: "resolving", progress: "解析输入" }));
    /* isRunning 是顶层 function，挂在 window 上，可以直接调。 */
    assert(G2, "isRunning 为真", p.win.isRunning() === true, "状态没推上来");
    assert(G2, "按钮被禁用", p.$("run-btn").disabled === true, "还能再点");
  } finally { p.close(); }
});

test("后端报错时把原因显示出来，而不是静默失败", async () => {
  const be = makeBackend({
    create_task: () => ({ ok: false, error: "链接里没有 BV 号", status: 400 }),
  });
  const p = await loadPage({ backend: be });
  try {
    await submitTask(p, "随便一段话");
    await wait(60);
    const msg = p.text("task-msg") || "";
    assert(G2, "错误文案可见", /BV 号/.test(msg), "task-msg = " + JSON.stringify(msg));
  } finally { p.close(); }
});

/* ============================================================ 三、状态机 */

const G3 = group("状态机");

test("推送的进度驱动分段刻度条，而不是整条进度条", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    p.win.__onTaskUpdate(sampleTask({ id: "t1", status: "fetching_subtitle", progress: "取字幕" }));
    /* STAGES 有 12 项，renderSegments() 懒建 12 根 <i>。 */
    eq(G3, "分段刻度是 12 根（对齐 STAGES）", p.$("seg").children.length, 12);
    assert(G3, "有百分比读数", /%/.test(p.text("seg-pct") || ""), p.text("seg-pct"));
    assert(G3, "已走过的段被点亮",
      p.$("seg").children[0].className === "on",
      "第 0 段 class = " + p.$("seg").children[0].className);
    assert(G3, "当前段被标出来",
      Array.from(p.$("seg").children).some((el) => el.className === "cur"),
      "没有任何一段是 cur");
  } finally { p.close(); }
});

test("准备就绪后界面显示正文，并按 01/02 分块", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    const body = p.$("body-inner").innerHTML;
    assert(G3, "渲染了一句话", /一句话结论/.test(body), "没找到一句话");
    assert(G3, "渲染了正文小节", /第一节/.test(body), "没找到正文");
    assert(G3, "有一句话的编号", /01/.test(body), "没有 01");
    assert(G3, "有主体的编号", /02/.test(body), "没有 02");
    assert(G3, "进度读数到 100%", p.text("seg-pct") === "100%", p.text("seg-pct"));
  } finally { p.close(); }
});

test("「上次改到一半程序退出了」必须明说，不能装作正常", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    /* status 说还在改，但 refining=false —— 这是进程被杀留下的状态。 */
    p.win.__onTaskUpdate(sampleTask({
      id: "t1", status: "refining", refining: false, can_refine: true, current_version: 1,
    }));
    const msg = (p.text("task-msg") || "") + " " + (p.text("refine-hint") || "");
    assert(G3, "给出了中断提示", /退出|中断|没改完/.test(msg), "提示文案：" + JSON.stringify(msg));
  } finally { p.close(); }
});

test("迭代失败时上一版还在，要说成失败而不是成功", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    p.win.__onTaskUpdate(sampleTask({
      id: "t1", status: "ready", refining: false, error: "模型超时", current_version: 1,
    }));
    const msg = p.text("task-msg") || "";
    assert(G3, "把失败说出来", /超时|失败/.test(msg), "task-msg = " + JSON.stringify(msg));
  } finally { p.close(); }
});

test("版号没涨的时候继续等，不会把半成品当成新版", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    p.win.__onTaskUpdate(sampleTask({ id: "t1", status: "ready", current_version: 1 }));
    /* 模拟「刚提交了打磨，等第 2 版」，然后再推一条仍是第 1 版的 ready。 */
    p.S().awaitingRefine = 2;
    p.win.__onTaskUpdate(sampleTask({ id: "t1", status: "ready", current_version: 1 }));
    eq(G3, "仍在等第 2 版", p.S().awaitingRefine, 2);
  } finally { p.close(); }
});

test("不是当前任务的推送被忽略", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    const before = p.text("t-title");
    p.win.__onTaskUpdate(sampleTask({ id: "别的任务", title: "不该出现的标题" }));
    eq(G3, "标题没被改掉", p.text("t-title"), before);
  } finally { p.close(); }
});

/* ============================================================ 四、版本 */

const G4 = group("版本与打磨");

test("版本列表把当前版标出来", async () => {
  const be = makeBackend({
    list_versions: () => ({
      ok: true, data: {
        current: 2,
        total: { calls: 6, prompt_tokens: 1000, completion_tokens: 200, total_tokens: 1200 },
        versions: [
          { version_no: 1, one_line: "第一版", role: "wengao", created_at: "2026-10-27 09:00", metrics: {}, changed_fields: null },
          { version_no: 2, one_line: "第二版", role: "wengao", created_at: "2026-10-27 10:00", metrics: {}, changed_fields: [] },
        ],
      },
    }),
  });
  const p = await loadPage({ backend: be });
  try {
    await readyTask(p, { current_version: 2, version_count: 2 });
    await wait(80);
    const list = p.$("v-list").innerHTML;
    assert(G4, "列出第一版", /第一版/.test(list), list.slice(0, 160));
    assert(G4, "列出第二版", /第二版/.test(list), list.slice(0, 160));
    assert(G4, "当前版有 on 标记", /class="v on"/.test(list), "没有当前版标记");
    eq(G4, "版本计数两位补零", p.text("v-count"), "02");
  } finally { p.close(); }
});

test("「没比过」和「一个字没变」必须区别对待", async () => {
  /* changed_fields 三态：null=没比过（不显示）、[]=比过但没变（要明说）、
     非空=逐字段列。混起来会把"没比过"读成"没改动"。 */
  const be = makeBackend({
    list_versions: () => ({
      ok: true, data: {
        current: 2, total: null,
        versions: [
          { version_no: 1, source: "initial", one_line: "第一版", metrics: {}, changed_fields: null },
          { version_no: 2, source: "refine", one_line: "第二版", metrics: {}, changed_fields: [] },
        ],
      },
    }),
  });
  const p = await loadPage({ backend: be });
  try {
    await readyTask(p, { current_version: 2, version_count: 2 });
    await wait(80);
    const list = p.$("v-list").innerHTML;
    assert(G4, "「没变」被明说出来", /逐字相同|没被落实|一个字没变/.test(list), list);
    assert(G4, "「没变」用警示样式而非普通样式", /class="w"/.test(list), "没有 .w 标记");
  } finally { p.close(); }
});

test("打磨按钮在没填要求时禁用", async () => {
  const p = await loadPage();
  try {
    await readyTask(p, { can_refine: true });
    assert(G4, "空要求 → 禁用", p.$("refine-btn").disabled === true, "没有禁用");
    typeInto(p, p.$("refine-req"), "把第三节压成两段");
    assert(G4, "有要求 → 解禁", p.$("refine-btn").disabled === false, "仍然禁用");
  } finally { p.close(); }
});

test("点打磨把要求原样交给后端，并带上当前版号", async () => {
  const p = await loadPage();
  try {
    await readyTask(p, { can_refine: true, current_version: 1 });
    typeInto(p, p.$("refine-req"), "把第三节压成两段");
    click(p, p.$("refine-btn"));
    await wait(90);

    const c = p.backend.calls.find((x) => x.name === "refine_task");
    assert(G4, "调了 refine_task", !!c, "没调用");
    eq(G4, "要求原样传过去", c ? c.args[1].requirement : null, "把第三节压成两段");
    eq(G4, "带上底稿版号", c ? c.args[1].base_version : null, 1);
  } finally { p.close(); }
});

test("正在改的时候不让人再点一次", async () => {
  const p = await loadPage();
  try {
    await submitTask(p);
    p.win.__onTaskUpdate(sampleTask({ id: "t1", status: "refining", refining: true, can_refine: false }));
    assert(G4, "打磨按钮禁用", p.$("refine-btn").disabled === true, "还能点");
    assert(G4, "打磨输入框禁用", p.$("refine-req").disabled === true, "还能填");
  } finally { p.close(); }
});

test("后台不能改的时候，说清为什么不能改", async () => {
  const p = await loadPage();
  try {
    await readyTask(p, { can_refine: false, refine_blocked: "这条视频的总结已经发布过了" });
    const hint = p.text("refine-hint") || "";
    assert(G4, "把阻塞原因说出来", /发布过/.test(hint), "refine-hint = " + JSON.stringify(hint));
    assert(G4, "打磨按钮禁用", p.$("refine-btn").disabled === true, "还能点");
  } finally { p.close(); }
});

/* ============================================================ 五、发布 */

const G5 = group("发布");

test("评论超过 1000 字时改走笔记通道，而不是禁掉发布", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    typeInto(p, p.$("comment-box"), "字".repeat(1200));
    /* 勾了确认才该启用——这是防止「内容变了还沿用上一版授权」的闸门。 */
    p.$("confirm-box").checked = true;
    p.$("confirm-box").dispatchEvent(new p.win.Event("change", { bubbles: true }));

    const counter = p.text("counter") || "";
    assert(G5, "计数说明改走笔记", /笔记/.test(p.text("via-hint") || ""), "via-hint = " + p.text("via-hint"));
    assert(G5, "计数本身显示字数", /1200/.test(counter), "counter = " + counter);
    assert(G5, "发布按钮仍可用（不是禁用）", p.$("publish-btn").disabled === false, "被禁用了");
  } finally { p.close(); }
});

test("没勾确认就不给发", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    typeInto(p, p.$("comment-box"), "一段正常的短评");
    assert(G5, "有内容但没勾 → 禁用", p.$("publish-btn").disabled === true, "竟然能发");

    p.$("confirm-box").checked = true;
    p.$("confirm-box").dispatchEvent(new p.win.Event("change", { bubbles: true }));
    assert(G5, "勾了 → 解禁", p.$("publish-btn").disabled === false, "还是禁用");
  } finally { p.close(); }
});

test("换了一版就要求重新勾确认，不能沿用上一版的授权", async () => {
  const p = await loadPage();
  try {
    await readyTask(p, { current_version: 1 });
    p.$("confirm-box").checked = true;

    /* 推一条新版：current_version 从 1 变 2。 */
    p.win.__onTaskUpdate(sampleTask({
      id: "t1", status: "ready", current_version: 2, version_count: 2,
      publish_document: "这是第 2 版的发布文本。",
      summary_json: { one_line: "换了", summary_md: "新正文", key_points: [], role: "wengao" },
    }));
    await wait(60);

    assert(G5, "确认框被清掉", p.$("confirm-box").checked === false, "还勾着上一版的确认");
    assert(G5, "评论框换成了新版内容", /第 2 版/.test(p.$("comment-box").value), p.$("comment-box").value);
  } finally { p.close(); }
});

test("点发布把任务、正文、确认位一起交给后端", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    typeInto(p, p.$("comment-box"), "我要发这段。");
    p.$("confirm-box").checked = true;
    p.$("confirm-box").dispatchEvent(new p.win.Event("change", { bubbles: true }));
    click(p, p.$("publish-btn"));
    await wait(90);

    const c = p.backend.calls.find((x) => x.name === "publish");
    assert(G5, "调了 publish", !!c, "没调用");
    eq(G5, "带上 task_id", c ? c.args[0].task_id : null, "t1");
    eq(G5, "带上正文", c ? c.args[0].text : null, "我要发这段。");
    eq(G5, "带上确认位", c ? c.args[0].confirm : null, true);
  } finally { p.close(); }
});

/* ============================================================ 六、设置 */

const G6 = group("设置");

test("打开抽屉会去读设置，并填进表单", async () => {
  const p = await loadPage();
  try {
    p.win.openDrawer();
    await wait(80);
    assert(G6, "抽屉已打开", p.$("drawer").classList.contains("on"), "抽屉没开");
    assert(G6, "读了 get_settings",
      !!p.backend.calls.find((c) => c.name === "get_settings"), "没调用");

    const out = p.win.collectSettings();
    assert(G6, "收集出对象", out && typeof out === "object", String(out));
    eq(G6, "model 值被填进表单", out.llm && out.llm.model, "qwen-test");
  } finally { p.close(); }
});

test("关掉发布前确认时，自动带上「我确认要关」的标志", async () => {
  const p = await loadPage();
  try {
    p.win.openDrawer();
    await wait(80);
    p.$("s-gate").checked = false;
    p.$("s-gate").dispatchEvent(new p.win.Event("change", { bubbles: true }));

    const real = p.win.collectSettings();
    eq(G6, "带上了 confirm_disable_gate", real.confirm_disable_gate, true);
    assert(G6, "确认勾选框露出来了", !p.$("gate-ack").classList.contains("hidden"), "还藏着");
  } finally { p.close(); }
});

test("保存设置把收集到的对象原样交给后端", async () => {
  const p = await loadPage();
  try {
    p.win.openDrawer();
    await wait(80);
    p.win.saveSettings();
    await wait(90);

    const c = p.backend.calls.find((x) => x.name === "save_settings");
    assert(G6, "调了 save_settings", !!c, "没调用");
    eq(G6, "带上 llm.model", c ? c.args[0].llm.model : null, "qwen-test");
  } finally { p.close(); }
});

test("测试连通性发的是 llm 那一段本身，不再多包一层", async () => {  // 这条来自一个真实 bug：原来是 `call("test_llm", {llm: <那一段>})`，多包了一层。
  // 后端的 `LlmPatch` 收的就是那一段本身，于是校验报"缺 base_url 与 model"，
  // 而错误信息指着字段名、不指着多出来的壳，看着像配置写错了。
  // 当时前端测试一条都没碰过 testLlm，所以 87 项全绿也没发现。
  const p = await loadPage();
  try {
    p.win.openDrawer();
    await wait(80);
    await p.win.testLlm();
    await wait(120);

    const c = p.backend.calls.find((x) => x.name === "test_llm");
    assert(G6, "调了 test_llm", !!c, "没调用");
    const arg = c ? c.args[0] : null;
    assert(G6, "顶层就是 base_url", !!(arg && arg.base_url), "没传 base_url：" + JSON.stringify(arg));
    assert(G6, "没有多包一层 llm", !(arg && arg.llm), "还带着 llm 这层壳");
    eq(G6, "模型名跟着一起发了", arg ? arg.model : null, "qwen-test");
    eq(G6, "结果显示在抽屉里", p.$("d-msg").className.includes("ok"), true);
  } finally { p.close(); }
});

/* ============================================================ 六·五、角色选择 */

const G65 = group("角色选择");

test("顶栏有角色 chip，显示的是当前角色名", async () => {
  const p = await loadPage();
  try {
    const chip = p.$("chip-role");
    assert(G65, "chip 存在", !!chip, "找不到 #chip-role");
    // 顶栏里跟「设置」并排，顺序在它前面。
    eq(G65, "chip 的父节点是顶栏", chip.parentElement.className, "top");
    assert(G65, "显示当前角色名（文稿）",
      chip.textContent.includes("文稿"), "实际：" + chip.textContent);
  } finally { p.close(); }
});

test("点 chip 弹出角色清单，全部角色都在里面", async () => {
  const p = await loadPage();
  try {
    p.$("chip-role").click();
    await wait(40);
    assert(G65, "浮层打开了", p.$("role-pop").classList.contains("on"), "没打开");

    const items = p.$("role-list").querySelectorAll("[data-role]");
    eq(G65, "条目数与角色数一致", items.length, p.backend.state.roles.roles.length);
    eq(G65, "第一条是 standard", items[0].dataset.role, "standard");
    // 当前角色那一项要有标记，否则用户看不出自己在哪一档。
    eq(G65, "当前项被标出来", p.$("role-list").querySelectorAll(".role.on").length, 1);
    eq(G65, "标的是 wengao",
      p.$("role-list").querySelector(".role.on").dataset.role, "wengao");
  } finally { p.close(); }
});

test("选一个角色：调 set_role、chip 跟着变、浮层关掉", async () => {
  const p = await loadPage();
  try {
    p.$("chip-role").click();
    await wait(40);
    p.doc.querySelector('#role-list [data-role="standard"]').click();
    await wait(90);

    const c = p.backend.calls.find((x) => x.name === "set_role");
    assert(G65, "调了 set_role", !!c, "没调用");
    eq(G65, "传的是对象而不是裸字符串", c ? typeof c.args[0] : "", "object");
    eq(G65, "带上了 role", c ? c.args[0].role : null, "standard");

    assert(G65, "chip 显示新角色（标准）",
      p.$("chip-role").textContent.includes("标准"), "实际：" + p.$("chip-role").textContent);
    assert(G65, "浮层关掉了", !p.$("role-pop").classList.contains("on"), "还开着");
    assert(G65, "给了提示", p.$("task-msg").className.includes("ok"), "没有成功提示");
  } finally { p.close(); }
});

test("选当前那个角色：不白跑一趟后端", async () => {
  const p = await loadPage();
  try {
    p.$("chip-role").click();
    await wait(40);
    p.doc.querySelector('#role-list [data-role="wengao"]').click();  // 当前就是 wengao
    await wait(70);
    eq(G65, "没有发 set_role",
      p.backend.calls.filter((x) => x.name === "set_role").length, 0);
    assert(G65, "浮层还是关掉了", !p.$("role-pop").classList.contains("on"), "还开着");
  } finally { p.close(); }
});

test("换了角色之后保存设置，不会把角色重置回旧值", async () => {
  /* 这条守的是一个很容易踩的坑：角色选择器搬去顶栏之后，设置抽屉里只剩一个
     隐藏的 data-path="llm.role"。如果那个隐藏字段没跟着更新，collectSettings()
     交上去的就是旧值——而 LlmPatch.role 有默认值 "standard"，一旦字段缺席，
     保存任何别的设置都会把角色悄悄重置。 */
  const p = await loadPage();
  try {
    p.$("chip-role").click();
    await wait(40);
    p.doc.querySelector('#role-list [data-role="standard"]').click();
    await wait(90);

    eq(G65, "隐藏字段跟上了", p.$("s-role").value, "standard");

    p.win.openDrawer();
    await wait(80);
    p.win.saveSettings();
    await wait(90);

    const c = p.backend.calls.find((x) => x.name === "save_settings");
    assert(G65, "调了 save_settings", !!c, "没调用");
    eq(G65, "llm.role 是新选的那个", c ? c.args[0].llm.role : null, "standard");
  } finally { p.close(); }
});

test("抽屉里已经没有角色下拉框了", async () => {
  const p = await loadPage();
  try {
    // 搬家要搬干净：留一个看得见的下拉框在抽屉里，会出现两个地方都能改角色的局面。
    eq(G65, "抽屉里没有 select#s-role",
      p.doc.querySelectorAll('select#s-role').length, 0);
    eq(G65, "只留一个隐藏字段",
      p.doc.querySelectorAll('[data-path="llm.role"]').length, 1);
    eq(G65, "它是 hidden 的",
      p.$("s-role").type, "hidden");
  } finally { p.close(); }
});

test("Esc 与点外面都能关掉角色浮层", async () => {
  const p = await loadPage();
  try {
    p.$("chip-role").click();
    await wait(40);
    assert(G65, "先确认开着", p.$("role-pop").classList.contains("on"), "没打开");

    p.doc.dispatchEvent(new p.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wait(30);
    assert(G65, "Esc 关掉了", !p.$("role-pop").classList.contains("on"), "还开着");

    p.$("chip-role").click();
    await wait(40);
    p.doc.body.click();
    await wait(30);
    assert(G65, "点外面也关掉了", !p.$("role-pop").classList.contains("on"), "还开着");
  } finally { p.close(); }
});

/* ============================================================ 七、历史总结 */

const G66 = group("历史总结");

/* 造几条历史索引。字段必须跟后端 list_task_index 的输出一致——
   测试里少一个字段，前端就不会踩到那条分支，等于没测。 */
function histRow(over) {
  return Object.assign({
    id: "t1", title: "第一个视频", bvid: "BV1xx411c7mD", up_name: "某UP",
    status: "ready", created_at: "2026-10-01T10:00:00+08:00",
    updated_at: "2026-10-01T10:05:00+08:00",
    one_line: "一句话结论甲", role: "wengao",
    version_count: 2, current_version: 2, has_summary: true, error: null,
  }, over || {});
}

test("顶栏有历史 chip，条数来自后端", async () => {
  const be = makeBackend();
  be.state.history = [histRow(), histRow({ id: "t2", title: "第二个视频" })];
  const p = await loadPage({ backend: be });
  try {
    const chip = p.$("chip-history");
    assert(G66, "chip 存在", !!chip, "找不到 #chip-history");
    eq(G66, "父节点是顶栏", chip.parentElement.className, "top");
    // 启动时就该取好：等点开再取会先显示「历史」再跳成「历史 · 2」。
    assert(G66, "显示条数", chip.textContent.includes("2"), "实际：" + chip.textContent);
  } finally { p.close(); }
});

test("点开历史：列出全部记录，标题与一句话结论都在", async () => {
  const be = makeBackend();
  be.state.history = [
    histRow(),
    histRow({ id: "t2", title: "第二个视频", one_line: "结论乙", status: "failed", error: "字幕拿不到" }),
  ];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    assert(G66, "浮层打开了", p.$("hist-pop").classList.contains("on"), "没打开");

    const rows = p.$("hist-list2").querySelectorAll("[data-hid]");
    eq(G66, "条数对得上", rows.length, 2);
    assert(G66, "标题渲染了", rows[0].textContent.includes("第一个视频"), "实际：" + rows[0].textContent);
    assert(G66, "一句话结论渲染了", rows[0].textContent.includes("一句话结论甲"), "没有结论");
    // 失败的任务更要留在列表里——用户常常正是回去看「上次为什么没成」。
    assert(G66, "失败的那条也在", rows[1].textContent.includes("第二个视频"), "失败了就被藏了");
    assert(G66, "失败有标记", rows[1].querySelector(".bad") !== null, "没标失败");
  } finally { p.close(); }
});

test("搜索能按标题、UP、BV 号、结论过滤", async () => {
  const be = makeBackend();
  be.state.history = [
    histRow({ id: "a", title: "聊经济", up_name: "甲UP", bvid: "BV1aa", one_line: "通胀那段" }),
    histRow({ id: "b", title: "聊历史", up_name: "乙UP", bvid: "BV1bb", one_line: "朝代更替" }),
  ];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);

    const hit = (kw) => {
      p.$("hist-q").value = kw;
      p.$("hist-q").dispatchEvent(new p.win.Event("input", { bubbles: true }));
      return p.$("hist-list2").querySelectorAll("[data-hid]").length;
    };

    eq(G66, "搜标题", hit("历史"), 1);
    eq(G66, "搜 UP 名", hit("甲UP"), 1);
    eq(G66, "搜 BV 号", hit("BV1bb"), 1);
    eq(G66, "搜一句话结论", hit("通胀"), 1);
    eq(G66, "搜不中给 0 条", hit("完全不存在的词"), 0);
    eq(G66, "清空恢复全部", hit(""), 2);
  } finally { p.close(); }
});

test("选中一条，右侧给出摘要和两个动作", async () => {
  const be = makeBackend();
  be.state.history = [histRow({ id: "a", title: "选中我", one_line: "这条的结论" })];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    p.doc.querySelector('#hist-list2 [data-hid="a"]').click();
    await wait(40);

    const pv = p.$("hist-preview");
    assert(G66, "预览显示了标题", pv.textContent.includes("选中我"), "实际：" + pv.textContent);
    assert(G66, "预览显示了结论", pv.textContent.includes("这条的结论"), "没有结论");
    assert(G66, "有「在工作区打开」", !!pv.querySelector("[data-open]"), "没有打开按钮");
    assert(G66, "有「重新总结」", !!pv.querySelector("[data-reuse]"), "没有复用按钮");

    // 点行会重绘整个列表，被点的按钮随即脱离文档。全局那个「点外面就关」的
    // 监听器如果只按 closest() 判断，会把它当成外点，面板刚点开就关掉。
    // 这一条曾经在真机上出现而 161 项测试全绿——因为它只发生在「点第二行」时。
    assert(G66, "点完行面板还开着",
      p.$("hist-pop").classList.contains("on"), "面板被自己的重绘关掉了");
  } finally { p.close(); }
});

test("点列表里任意一行都不会把面板关掉", async () => {
  const be = makeBackend();
  be.state.history = [
    histRow({ id: "a", title: "第一" }), histRow({ id: "b", title: "第二" }),
    histRow({ id: "c", title: "第三" }),
  ];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    // 逐行点一遍。第一行是默认选中的，点它不会改状态；真正触发重绘的是第二、三行。
    for (const id of ["a", "b", "c"]) {
      p.doc.querySelector('#hist-list2 [data-hid="' + id + '"]').click();
      await wait(40);
      assert(G66, "点 " + id + " 之后还开着",
        p.$("hist-pop").classList.contains("on"), "点 " + id + " 把面板关了");
      eq(G66, "选中的是 " + id, p.S().histPick, id);
      eq(G66, "预览跟着换了 " + id,
        p.$("hist-preview").textContent.includes({ a: "第一", b: "第二", c: "第三" }[id]), true);
    }
  } finally { p.close(); }
});

test("「在工作区打开」会拉全文并渲染，浮层关掉", async () => {
  const be = makeBackend({
    // 走 overrides 而不是事后改 be.api：事后赋值会绕过调用记录，
    // 于是「有没有按 id 拉全文」这条断言永远看不到调用，看起来像被测代码没调用。
    // 另外 `ok()` 是 makeBackend 内部的局部函数，用例里拿不到，这里直接写字面量。
    get_task: (id) => ({ ok: true, data: sampleTask({ id: id, title: "要打开的" }) }),
  });
  be.state.history = [histRow({ id: "a", title: "要打开的" })];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    p.doc.querySelector('#hist-list2 [data-hid="a"]').click();
    await wait(40);
    p.$("hist-preview").querySelector("[data-open]").click();
    await wait(120);

    const c = p.backend.calls.find((x) => x.name === "get_task" && x.args[0] === "a");
    assert(G66, "按 id 拉了全文", !!c, "没调用 get_task");
    eq(G66, "浮层关了", p.$("hist-pop").classList.contains("on"), false);
    // 打开之后当前任务要跟着换，否则「迭代打磨」会打到上一条任务上。
    eq(G66, "当前任务切过去了", p.S().taskId, "a");
  } finally { p.close(); }
});

test("「重新总结」把 BV 号填进输入框，不发请求", async () => {
  const be = makeBackend();
  be.state.history = [histRow({ id: "a", bvid: "BV1reuse" })];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    p.doc.querySelector('#hist-list2 [data-hid="a"]').click();
    await wait(40);
    p.$("hist-preview").querySelector("[data-reuse]").click();
    await wait(60);

    eq(G66, "输入框填上了 BV 号", p.$("raw-input").value, "BV1reuse");
    eq(G66, "浮层关了", p.$("hist-pop").classList.contains("on"), false);
    eq(G66, "没有偷偷发任务", p.backend.calls.filter((c) => c.name === "create_task").length, 0);
  } finally { p.close(); }
});

test("Esc 关历史浮层，搜索框里的 Esc 先清词", async () => {
  const be = makeBackend();
  be.state.history = [histRow()];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-history").click();
    await wait(80);
    p.$("hist-q").value = "关键词";
    p.$("hist-q").dispatchEvent(new p.win.Event("input", { bubbles: true }));

    // 第一次 Esc：只清词，面板留着。否则「我只是想清掉刚打的字」变成
    // 「面板没了，还得重新打开」。
    p.$("hist-q").dispatchEvent(new p.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wait(30);
    eq(G66, "Esc 先清空搜索词", p.$("hist-q").value, "");
    assert(G66, "面板还开着", p.$("hist-pop").classList.contains("on"), "被关掉了");

    p.doc.dispatchEvent(new p.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await wait(30);
    assert(G66, "再按一次才关", !p.$("hist-pop").classList.contains("on"), "还开着");
  } finally { p.close(); }
});

test("两个浮层不会同时开着", async () => {
  const be = makeBackend();
  be.state.history = [histRow()];
  const p = await loadPage({ backend: be });
  try {
    p.$("chip-role").click();
    await wait(40);
    p.$("chip-history").click();
    await wait(80);
    assert(G66, "历史开了", p.$("hist-pop").classList.contains("on"), "没开");
    assert(G66, "角色浮层被关掉", !p.$("role-pop").classList.contains("on"), "两个都开着");

    p.$("chip-role").click();
    await wait(80);
    assert(G66, "反过来也一样", !p.$("hist-pop").classList.contains("on"), "两个都开着");
  } finally { p.close(); }
});

test("历史读取失败不会影响总结本身", async () => {
  const be = makeBackend({ list_history: () => ({ ok: false, error: "库文件损坏" }) });
  const p = await loadPage({ backend: be });
  try {
    // 读不到历史是「少了个功能」，不是「程序坏了」：输入框还能用。
    eq(G66, "历史为空", p.S().history.length, 0);
    eq(G66, "记住了失败原因", p.S().histError, "库文件损坏");
    assert(G66, "chip 还在", !!p.$("chip-history"), "chip 没了");
    eq(G66, "输入框仍可用", p.$("raw-input").disabled, false);

    // 但点开要说出原因，不能一声不响给个空列表——那看着像记录丢了。
    p.$("chip-history").click();
    await wait(80);
    assert(G66, "点开时说明失败原因",
      p.$("hist-list2").textContent.includes("库文件损坏"),
      "实际：" + p.$("hist-list2").textContent);
  } finally { p.close(); }
});

/* ============================================================ 八、登录 */

const G7 = group("登录");

test("未登录时点账号位，走二维码流程", async () => {
  const p = await loadPage();
  try {
    p.win.openLogin();
    await wait(100);

    assert(G7, "弹了登录框", p.$("modal").classList.contains("on"), "没弹");
    const q = p.backend.calls.find((c) => c.name === "auth_qrcode");
    assert(G7, "要了二维码", !!q, "没调用 auth_qrcode");
    assert(G7, "记下了 key", p.S().qrKey === "k1", "qrKey = " + p.S().qrKey);
    assert(G7, "二维码轮询起了定时器", !!p.S().qrTimer, "没有定时器");
  } finally { p.close(); }
});

test("Cookie 不完整时明确拒绝，不发请求", async () => {
  const p = await loadPage();
  try {
    p.win.openLogin();
    await wait(100);
    /* 二维码框里还有 Cookie 入口；只填一半就该被拦。 */
    const ck1 = p.$("ck-1");
    const ck2 = p.$("ck-2");
    if (!ck1 || !ck2) return;                       // 这个版本没有 Cookie 入口就跳过
    const before = p.backend.calls.filter((c) => c.name === "auth_cookie").length;

    ck1.value = "只有 sessdata";
    ck2.value = "";
    p.win.submitCookie();
    await wait(90);

    const after = p.backend.calls.filter((c) => c.name === "auth_cookie").length;
    eq(G7, "没有发出无效请求", after, before);
  } finally { p.close(); }
});

/* ============================================================ 八、字幕 */

const G8 = group("字幕");

test("选字幕后行内显示文件信息，再点一次清掉", async () => {
  const be = makeBackend({
    pick_subtitle: () => ({
      ok: true, data: {
        cancelled: false, name: "我的字幕.srt", token: "tok1",
        segments: 128, chars: 2048, encoding: "utf-8", bytes: 4096,
        first: "00:00:01,000", last: "00:12:30,000",
      },
    }),
  });
  const p = await loadPage({ backend: be });
  try {
    click(p, p.$("srt-btn"));
    await wait(90);

    const line = p.text("srt-line") || "";
    assert(G8, "显示文件名", /我的字幕\.srt/.test(line), "srt-line = " + JSON.stringify(line));
    assert(G8, "显示段数与字数", /128 段/.test(line) && /2048 字/.test(line), line);
    eq(G8, "记下 token", p.S().subtitleToken, "tok1");

    /* 再点一次是"清掉"，不该再弹对话框。 */
    click(p, p.$("srt-btn"));
    await wait(90);
    eq(G8, "token 已清", p.S().subtitleToken, null);
    assert(G8, "给了清除提示", /清除|平台字幕/.test(p.text("srt-line") || ""), p.text("srt-line"));
  } finally { p.close(); }
});

/* ============================================================ 九、结构 */

const G9 = group("结构与契约");

test("页面里没有 <input type=file>：桌面版走原生对话框", async () => {
  const p = await loadPage();
  try {
    eq(G9, "没有 file input", p.doc.querySelectorAll('input[type="file"]').length, 0);
  } finally { p.close(); }
});

test("每个 call() 用到的名字，后端都必须提供", async () => {
  const names = new Set();
  const re = /\bcall\(\s*"([a-z_]+)"/g;
  let m;
  while ((m = re.exec(html))) names.add(m[1]);

  const be = makeBackend();
  const missing = [];
  for (const n of names) {
    if (typeof be.api[n] !== "function") missing.push(n);
  }
  eq(G9, "没有缺失的接口", missing, []);
  assert(G9, "至少调用了 15 个接口", names.size >= 15, "只找到 " + names.size + " 个");
});

test("HTTP 路由表里的每条路径，后端都真的注册了", async () => {
  // 桌面版走桥，源码版走这张表。名字拼错在桌面版永远暴露不出来（那条路根本不经过它），
  // 而源码版会安静地 404 或者更糟——退成默认值（比如 auth_status 坏掉就显示"未登录"）。
  // 这条把表和后端路由表对起来，改一边忘另一边时当场红。
  const mainPy = fs.readFileSync(
    path.join(__dirname, "..", "bili_scribe", "app", "main.py"), "utf8");

  // 后端注册的路由，把 {task_id} 这类占位符换成 `*` 以便比对。
  const declared = new Set();
  const routeRe = /@app\.(get|post|put|delete)\("([^"]+)"\)/g;
  let rm;
  while ((rm = routeRe.exec(mainPy))) {
    declared.add(rm[1].toUpperCase() + " " + rm[2].replace(/\{[^}]+\}/g, "*"));
  }
  assert(G9, "读到了后端路由", declared.size >= 20, "只读到 " + declared.size + " 条");

  /* 不解析源码文本，**直接跑表里的 `p(...)` 拿真路径**。
     用正则猜 '" + encodeURIComponent(x) + "' 拼出来长什么样，改一次写法就得改一次正则，
     而且猜错了会假绿。这里传几个哑参数（同样是数字 1）调用，把拿到的路径归一化——
     有几个参数不重要，多传的会被 JS 忽略。 */
  const p = await loadPage();
  try {
    const rows = p.win.eval(`Object.keys(HTTP_ROUTES).map(function (k) {
      var r = HTTP_ROUTES[k];
      var path;
      try { path = r.p(1, 1, 1); } catch (e) { path = "(抛错: " + e.message + ")"; }
      return { name: k, method: r.m, path: path };
    })`);

    assert(G9, "表里读到足够多的接口", rows.length >= 20, "只读到 " + rows.length + " 条");

    // 归一化：去掉查询串、把哑参数那几段换成 `*`。
    const norm = (s) => String(s)
      .split("?")[0]
      .replace(/\/(1|undefined|null|NaN)(?=\/|$)/g, "/*");

    const missing = [];
    for (const e of rows) {
      if (String(e.path).startsWith("(抛错")) {
        missing.push(e.name + " 的 p() 跑不起来：" + e.path);
        continue;
      }
      const key = e.method + " " + norm(e.path);
      if (!declared.has(key)) missing.push(e.name + " → " + key);
    }
    eq(G9, "路由表里没有对不上的路径", missing, []);

    // 反向也要查：后端有、表里没有，说明源码版少了一条能力。
    // `/` 是页面本身，`/api/subtitle/parse` 走裸字节特例（不在表里，但源码版用得到）。
    const covered = new Set(rows.map((e) => e.method + " " + norm(e.path)));
    const skip = new Set(["GET /", "POST /api/subtitle/parse"]);
    const onlyBackend = [...declared].filter((k) => !covered.has(k) && !skip.has(k)).sort();
    eq(G9, "后端没有漏在表外的接口", onlyBackend, []);
  } finally { p.close(); }
});

test("页面里没有内联事件处理器", async () => {
  const hits = html.match(/\son[a-z]+\s*=\s*"/g) || [];
  eq(G9, "内联事件处理器数量", hits.length, 0);
});

test("颜色对比度：正文至少 4.5:1，弱化文字至少 3:1", async () => {
  const p = await loadPage();
  try {
    const cs = p.win.getComputedStyle(p.doc.documentElement);
    const rgb = (hex) => {
      const h = hex.replace("#", "");
      return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
    };
    const lum = (c) => {
      const a = c.map((v) => {
        const s = v / 255;
        return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
    };
    const ratio = (fg, bg) => {
      const l1 = lum(rgb(fg)), l2 = lum(rgb(bg));
      return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    };

    const text = cs.getPropertyValue("--text").trim();
    const muted = cs.getPropertyValue("--muted").trim();
    const bg = cs.getPropertyValue("--bg").trim();
    assert(G9, "取到了三个颜色变量", !!text && !!muted && !!bg, [text, muted, bg].join(","));
    if (!text || !muted || !bg) return;

    const r1 = ratio(text, bg);
    assert(G9, "正文对比度 ≥ 4.5", r1 >= 4.5, r1.toFixed(2) + ":1（" + text + " on " + bg + "）");
    const r2 = ratio(muted, bg);
    assert(G9, "弱化文字对比度 ≥ 3", r2 >= 3, r2.toFixed(2) + ":1（" + muted + " on " + bg + "）");

    // --dim 是第三档，用在底栏、说明文字上。曾经是 #4A5250，对 bg 只有 2.39:1，
    // 在深色底上基本看不见（用户报的「这一行字可见度太低」就是它）。
    // 原来这条测试只查 --text 和 --muted，所以一直没拦住。
    const dim = cs.getPropertyValue("--dim").trim();
    const panel = cs.getPropertyValue("--panel").trim();
    assert(G9, "取到了 --dim 与 --panel", !!dim && !!panel, [dim, panel].join(","));
    if (dim && panel) {
      const r3 = ratio(dim, bg);
      assert(G9, "最弱一档文字对比度 ≥ 3（bg 上）", r3 >= 3,
        r3.toFixed(2) + ":1（" + dim + " on " + bg + "）");
      const r4 = ratio(dim, panel);
      assert(G9, "最弱一档文字对比度 ≥ 3（panel 上）", r4 >= 3,
        r4.toFixed(2) + ":1（" + dim + " on " + panel + "）");
    }

    // 光把 --dim 调亮还不够：如果某个该读的类仍然直接用 var(--dim)，
    // 它就永远停在最低那一档。这里逐条确认哪些类不许用它。
    const mustNotUseDim = [".c", ".vd", ".ft .t", ".v .act"];
    const offenders = [];
    for (const sheet of Array.from(p.doc.styleSheets)) {
      for (const rule of Array.from(sheet.cssRules || [])) {
        const sel = rule.selectorText || "";
        const decl = rule.style && rule.style.getPropertyValue("color");
        if (!decl || !/var\(\s*--dim\s*\)/.test(decl)) continue;
        for (const banned of mustNotUseDim) {
          if (sel.split(",").map((s) => s.trim()).includes(banned)) {
            offenders.push(banned + " → " + decl.trim());
          }
        }
      }
    }
    eq(G9, "该读的类没有用最弱那一档颜色", offenders, []);
  } finally { p.close(); }
});

test("超限提示不只靠颜色，有文字说明", async () => {
  const p = await loadPage();
  try {
    await readyTask(p);
    typeInto(p, p.$("comment-box"), "字".repeat(1200));
    const via = p.text("via-hint") || "";
    assert(G9, "有文字说明走笔记通道", /笔记|超/.test(via), "via-hint = " + JSON.stringify(via));
  } finally { p.close(); }
});

/* ============================================================ 执行 */

(async function main() {
  const t0 = Date.now();
  let lastGroup = "";

  for (const c of CASES) {
    if (only && !c.name.includes(only)) continue;
    const before = results.length;
    try {
      await c.fn();
    } catch (e) {
      const g = results.length > before ? results[results.length - 1].group : "(未分组)";
      results.push({
        group: g, name: c.name, ok: false,
        detail: (e && e.stack) || String(e),
      });
    }
    if (results.length === before) {
      results.push({ group: "(未分组)", name: c.name, ok: false, detail: "用例没有产生任何断言" });
    }
  }

  let pass = 0, fail = 0;
  const failures = [];

  for (const r of results) {
    if (r.group !== lastGroup) {
      console.log("\n── " + r.group + " " + "─".repeat(Math.max(0, 50 - r.group.length)));
      lastGroup = r.group;
    }
    if (r.ok) {
      pass++;
      console.log("  ✓ " + r.name);
    } else {
      fail++;
      failures.push(r);
      console.log("  ✗ " + r.name);
    }
  }

  if (failures.length) {
    console.log("\n失败详情：");
    for (const f of failures) {
      console.log("\n  [" + f.group + "] " + f.name);
      console.log("    " + String(f.detail).split("\n").join("\n    "));
    }
  }

  console.log("\n" + "═".repeat(58));
  console.log("前端测试：" + pass + " 通过 / " + fail + " 失败 / 共 " + results.length
    + " 项 · " + (Date.now() - t0) + "ms");
  console.log("═".repeat(58));
  process.exit(fail ? 1 : 0);
})();

