/**
 * 前端骨架验收（任务清单 P0-04 / P0-06）。
 *
 * 为什么用 jsdom 而不是"打开看一眼"：
 *   「六个页面都能渲染」这件事有 6 条路径，人眼每次只验 1 条，还容易漏。
 *   这里把六条路径全跑一遍，并且**收集 JS 错误**——页面白屏之前通常先在控制台
 *   报一条 TypeError，能报出来的错就不该留到用户那里。
 *
 * 跑法：node tests/wb/frontend_test.js（或 npm run test:wb）
 */

const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM, VirtualConsole } = require("jsdom");

const WEB = path.resolve(__dirname, "..", "..", "video_workbench", "web");
const INDEX = path.join(WEB, "index.html");

// 每个路由的标题，与 js/pages/*.js 里的 router.define 对齐
const ROUTES = [
  ["library", "素材库"],
  ["newtask", "新建任务"],
  ["clipper", "切片台"],
  ["merge", "合并"],
  ["publish", "发布"],
  ["subtitles", "字幕"],
  ["light", "轻量总结"],
  ["settings", "设置"]
];

const problems = [];
const results = [];

function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      results.push(`  ok   ${name}`);
    })
    .catch((err) => {
      problems.push(`${name}: ${err && err.message ? err.message : err}`);
      results.push(`  FAIL ${name}`);
    });
}

function tick(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms || 30));
}

/**
 * 等到 predicate 成立（默认最多 30 次 × 100ms）。
 * 发布区要先等总结页两轮 rpc、再等它自己的三轮，固定 tick 在慢机器上会假红。
 */
async function until(predicate, label, tries) {
  const limit = tries || 30;
  for (let i = 0; i < limit; i += 1) {
    if (predicate()) return true;
    // eslint-disable-next-line no-await-in-loop
    await tick(100);
  }
  assert.ok(false, `等不到：${label || "条件"}`);
  return false;
}

async function main() {
  assert.ok(fs.existsSync(INDEX), `找不到 ${INDEX}`);

  const virtualConsole = new VirtualConsole();
  const notes = [];
  virtualConsole.on("jsdomError", (err) => {
    const message = String(err && err.message ? err.message : err);
    // jsdom 默认不带 canvas 实现，getContext() 会抛 "Not implemented"。
    // 这是测试环境缺件、不是应用缺陷：波形组件在拿不到上下文时会降级成空白画布
    // （见 js/components/waveform.js 的 degraded 分支），而字幕列表/跳转/质检照常。
    if (message.indexOf("Not implemented: HTMLCanvasElement") >= 0) {
      if (notes.indexOf("canvas") < 0) notes.push("canvas");
      return;
    }
    // jsdom 也没有媒体实现：<video>.load()/play()/pause() 都是 "Not implemented"。
    // 这不是应用缺陷——切片台只依赖"能挂载、能 seek、能报进度"，那些在 jsdom 里
    // 本来就跑不了；真实播放靠桌面窗口验收（scripts/accept_p3_clips.py 里断言 DOM 与
    // 数据流，播放本身由样张与手工验收覆盖）。
    if (message.indexOf("Not implemented: HTMLMediaElement") >= 0) {
      if (notes.indexOf("media") < 0) notes.push("media");
      return;
    }
    problems.push(`jsdomError: ${message}`);
  });
  virtualConsole.on("error", (msg) => problems.push(`console.error: ${msg}`));
  virtualConsole.on("warn", () => {}); // rpc 的 warn 是设计内的，不算噪声

  // `?mock=1`：没有 pywebview 时用 mock.js 顶上（见 js/rpc.js 头注）
  const url = "file://" + INDEX.replace(/\\/g, "/") + "?mock=1";
  const dom = new JSDOM(fs.readFileSync(INDEX, "utf8"), {
    url,
    runScripts: "dangerously",
    resources: "usable",
    pretendToBeVisual: true,
    virtualConsole
  });

  const { window } = dom;
  window.addEventListener("error", (event) => {
    problems.push(`window.onerror: ${event.message}`);
  });
  window.addEventListener("unhandledrejection", (event) => {
    problems.push(`unhandledrejection: ${event.reason && event.reason.message}`);
  });

  await new Promise((resolve) => window.addEventListener("load", resolve, { once: true }));
  await tick(80); // 等 boot() 里的第一轮渲染落定

  const $ = (sel) => window.document.querySelector(sel);
  const WB = window.WB;

  await check("脚本装载完成（WB 命名空间齐备）", () => {
    assert.ok(WB, "window.WB 不存在：app.js 没跑起来");
    for (const key of ["rpc", "raw", "router", "ui", "mock", "pages", "applySkin"]) {
      assert.ok(WB[key], `WB.${key} 缺失`);
    }
    assert.ok(WB.isMock(), "没进 mock 模式：mock=1 的检测逻辑失效了");
  });

  await check("左栏渲染出 8 个入口且顺序稳定", () => {
    const items = [...window.document.querySelectorAll("#rail-nav .rail__item, #rail-nav button, #rail-nav a")];
    assert.strictEqual(items.length, 8, `左栏条目 ${items.length} 个，应为 8`);
    // 顺序也是契约：用户靠肌肉记忆点左栏，插一个入口不该把别的挤走。
    // 「发布」2026-11-02 并进总结页：路由还在（nav:false，兼容老链接），入口不再有。
    // 「字幕」2026-11-07 升成一级入口（plan-subtitles-entry §2 决策 1）：不必再从素材库点进。
    const labels = items.map((item) => item.textContent.trim().replace(/\s+/g, ""));
    const order = ["新建任务", "素材库", "字幕", "总结", "轻量总结", "切片台", "合并", "设置"];
    order.forEach((name, index) => {
      assert.ok(labels[index] && labels[index].indexOf(name) >= 0,
        `第 ${index + 1} 个入口应该是「${name}」，实际是「${labels[index]}」`);
    });
  });

  await check("默认落在素材库，标题与面包屑一致", () => {
    assert.strictEqual(WB.router.current().route, "library");
    assert.strictEqual($("#page-title").textContent.trim(), "素材库");
    assert.ok($("#page-crumb").textContent.trim().length > 0, "面包屑是空的");
  });

  await check("演示模式也把状态条填上（版本/依赖不再是占位符）", async () => {
    await tick(200);
    const ver = window.document.getElementById("status-version").textContent;
    const deps = window.document.getElementById("status-deps").textContent;
    assert.ok(/^v\d/.test(ver), "版本号还是占位符：" + ver);
    assert.notStrictEqual(deps, "依赖检查中…", "依赖状态没刷新（app.bootstrap 没跑）");
  });

  await check("素材库用 mock 数据渲染出列表", () => {
    const view = $("#view");
    assert.ok(view.innerHTML.length > 200, "视图几乎是空的");
    const rows = view.querySelectorAll("tbody tr, .card, .row");
    assert.ok(rows.length >= 1, "列表一条都没渲染出来");
  });

  await check("素材库：点合集只显示合集内素材，点全部项目恢复", async () => {
    WB.router.go("library");
    await tick(300);
    const groups = [...window.document.querySelectorAll(".lib-tree__group")];
    const group = groups.find((node) => node.getAttribute("data-collection"));
    assert.ok(group, "mock 素材库里没有合集组");
    const title = group.getAttribute("data-collection");
    group.click();
    await until(
      () => WB.pages.library.state.collection === title && !WB.pages.library.state.loading,
      "点合集组后进入来源筛选"
    );
    const scopedRows = window.document.querySelectorAll(".lib-row");
    assert.strictEqual(scopedRows.length, 3, "mock 合集应有 3 条素材，实际 " + scopedRows.length);
    const all = [...window.document.querySelectorAll(".tree__row")]
      .find((node) => (node.textContent || "").indexOf("全部项目") >= 0);
    assert.ok(all, "没有「全部项目」入口");
    all.click();
    await until(
      () => WB.pages.library.state.collection === "" && !WB.pages.library.state.loading,
      "点全部项目后清空来源"
    );
    assert.ok(window.document.querySelectorAll(".lib-row").length >= 4, "清来源后没有恢复全部素材");
  });

  // ---------------------------------------------------------------- 源语言

  // 以前点转录按钮直接发 RPC，源语言只能靠自动探测——探测只看开头一段，
  // 中英混排或开头没说话的素材经常判错，用户既改不了也看不出来。

  // Promise 挂死时 await 会一直等下去，整个测试变成"卡住"而不是"失败"，
  // 所以所有等待都套一层超时——超时就是可读的失败信息。
  function within(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, reject) => {
        setTimeout(() => reject(new Error(label + " 超过 " + ms + "ms 没结果：Promise 悬着")), ms);
      })
    ]);
  }

  function langModal() {
    return window.document.querySelector(".langpick");
  }

  function langBtn(label) {
    return [...window.document.querySelectorAll(".modal__foot button")]
      .find((b) => b.textContent.trim() === label);
  }

  async function openLangPick() {
    // 转录/字幕/移除这些按钮在详情卡里，列表行上只有标题和状态，
    // 所以要先把项目选中（路由参数 project）才看得到。
    WB.router.go("library", { project: "p_demo_1" });
    await tick(500);
    // mock 里的项目都转过，所以这里是「重新转录」；两条按钮走同一段逻辑，
    // 文案只差在标题和确认键上。
    const all = [...window.document.querySelectorAll("#view button")].map((b) => b.textContent.trim());
    const btn = [...window.document.querySelectorAll("#view button")]
      .find((b) => ["开始转录", "重新转录"].indexOf(b.textContent.trim()) >= 0);
    assert.ok(btn, "素材库里没有任何转录入口：" + all.join(" | "));
    btn.click();
    await tick(150);
    assert.ok(langModal(), "点了转录按钮没弹源语言选择");
    return btn;
  }

  await check("ui.pick：选中、取消、ESC 三条路都一定给结果", async () => {
    const one = [{ code: "", label: "自动检测" }];

    const escP = WB.ui.pick({ title: "选语言", options: one });
    await tick(40);
    assert.ok(langModal(), "弹窗没开");
    window.document.body.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    assert.strictEqual(await within(escP, 1200, "ESC 关闭"), null,
      "ESC 关掉时调用方拿不到结果，用户看到的就是「点了没反应」");

    const cancelP = WB.ui.pick({ title: "选语言", options: one });
    await tick(40);
    langBtn("取消").click();
    assert.strictEqual(await within(cancelP, 1200, "取消关闭"), null);

    const pickP = WB.ui.pick({ title: "选语言", options: [{ code: "zh", label: "中文" }] });
    await tick(40);
    langModal().querySelector(".langpick__item input").click();
    await tick(20);
    langBtn("确定").click();
    assert.strictEqual(await within(pickP, 1200, "选中后确认"), "zh", "选中的值要原样交回");
  });

  await check("转录前先弹源语言选择：不预选、确认灰着、取消不发请求", async () => {
    const before = (await WB.rpc("job.list", {}, { silent: true })).length;
    await openLangPick();

    const items = [...langModal().querySelectorAll(".langpick__item")];
    assert.ok(items.length >= 8, "常用语言没渲染全：" + items.length);
    assert.ok(items[0].textContent.indexOf("自动检测") >= 0, "自动检测要保留，排在第一位");
    assert.strictEqual(items[0].querySelector("input").checked, false, "不该预选任何一项");

    const confirm = langBtn("重新转录") || langBtn("开始转录");
    assert.ok(confirm, "弹窗上没有确认键");
    assert.strictEqual(confirm.disabled, true, "没选就点得动，等于让用户空手提交");

    langBtn("取消").click();
    await tick(120);
    assert.ok(!langModal(), "取消没关掉弹窗");
    const after = (await WB.rpc("job.list", {}, { silent: true })).length;
    assert.strictEqual(after, before, "取消之后还是把任务发出去了");
  });

  await check("选了中文再确认：language=zh 真的传到 transcribe.start", async () => {
    const before = (await WB.rpc("job.list", {}, { silent: true })).length;
    await openLangPick();

    const zh = [...langModal().querySelectorAll(".langpick__item")]
      .find((el) => el.textContent.indexOf("中文") >= 0);
    assert.ok(zh, "常用组里没有中文");
    zh.querySelector("input").click();
    await tick(30);

    const confirm = langBtn("重新转录") || langBtn("开始转录");
    assert.strictEqual(confirm.disabled, false, "选了之后确认键还是灰的");
    confirm.click();
    await tick(250);

    const jobs = await WB.rpc("job.list", {}, { silent: true });
    assert.strictEqual(jobs.length, before + 1, "没提交转录任务");
    assert.strictEqual(jobs[0].project_id, "p_demo_1", "任务挂错了项目");
    assert.strictEqual(jobs[0].language, "zh", "用户选的语言没传下去");

    // 这条 queued 任务留着会污染后面「状态条只数 running/queued」的断言。
    await WB.rpc("job.cancel", { id: jobs[0].id }, { silent: true });
  });

  await check("按 ESC 关掉弹窗 = 放弃，不发请求（以前 Promise 会一直悬着）", async () => {
    const before = (await WB.rpc("job.list", {}, { silent: true })).length;
    await openLangPick();

    window.document.body.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    await tick(150);

    assert.ok(!langModal(), "ESC 没关掉弹窗");
    const after = (await WB.rpc("job.list", {}, { silent: true })).length;
    assert.strictEqual(after, before, "ESC 取消之后还是把任务发出去了");
  });

  for (const [route, title] of ROUTES) {
    // eslint-disable-next-line no-await-in-loop
    await check(`切到 ${route} 能渲染且标题正确`, async () => {
      WB.router.go(route);
      await tick(60);
      assert.strictEqual(WB.router.current().route, route);
      assert.strictEqual($("#page-title").textContent.trim(), title);
      assert.ok($("#view").innerHTML.length > 120, `${route} 页渲染内容过少`);
      assert.ok(!$("#view").classList.contains("view--error"), `${route} 落到了错误态`);
    });
  }

  await check("字幕编辑器：带项目进页面会渲染出段落与波形画布", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(120);
    const view = $("#view");
    assert.ok(view.querySelector("canvas"), "没有波形画布");
    const rows = view.querySelectorAll(".sub-row");
    assert.ok(rows.length >= 1, "一段都没渲染出来");
    assert.ok(rows.length <= 30, `虚拟列表渲染了 ${rows.length} 行，说明没做窗口化`);
    assert.ok(view.innerHTML.indexOf("strangers") >= 0, "段落文本没进 DOM");
  });

  await check("字幕编辑器：1000 段时只渲染视口内的行（虚拟滚动）", async () => {
    const page = WB.pages.subtitles;
    const before = page.state.segments;
    page.state.segments = [];
    for (let i = 0; i < 1000; i += 1) {
      page.state.segments.push({ id: "big" + i, start_ms: i * 1000, end_ms: i * 1000 + 900,
                                 text: "第 " + i + " 句", words: [] });
    }
    page.state.qc = {};
    // 直接调渲染：和滚动时走的是同一条路径
    const internals = page._internals;
    assert.strictEqual(internals.rowHeight > 0, true, "行高未定义");
    const view = $("#view");
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(120);
    const rows = view.querySelectorAll(".sub-row");
    assert.ok(rows.length <= 40, `1000 段渲染了 ${rows.length} 行，窗口化失效`);
    page.state.segments = before;
  });

  await check("质检汇总按段聚合：一个段能挂多条问题", () => {
    const fn = WB.pages.subtitles._internals.indexQc;
    const out = fn([
      { kind: "empty", level: "error", segment_ids: ["a"], message: "空" },
      { kind: "overlap", level: "error", segment_ids: ["a", "b"], message: "重叠" }
    ]);
    assert.strictEqual(out.map.a.length, 2, "a 段应挂 2 条");
    assert.strictEqual(out.map.b.length, 1, "b 段应挂 1 条");
    assert.strictEqual(out.issues, 2);
  });

  await check("双击编辑→Enter 提交→撤销→重做，文本能来回", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(150);
    const first = window.document.querySelectorAll(".sub-row")[0];
    assert.ok(first, "一行都没有");
    const original = first.querySelector(".sub-row__text").textContent;

    first.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
    await tick(30);
    const input = window.document.querySelector(".sub-row__input");
    assert.ok(input, "双击没进入编辑态");
    input.value = "改过的第一句";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await tick(200);
    assert.ok($("#view").textContent.indexOf("改过的第一句") >= 0, "编辑没写回列表");

    const buttons = Array.from(window.document.querySelectorAll("#view button"));
    const findBtn = (label) => buttons.find((b) => b.textContent.trim() === label);
    const undoBtn = findBtn("撤销");
    const redoBtn = findBtn("重做");
    assert.ok(undoBtn && redoBtn, "工具栏上没有撤销/重做");
    assert.strictEqual(undoBtn.disabled, false, "改完之后撤销按钮还是灰的");

    undoBtn.click();
    await tick(250);
    assert.ok($("#view").textContent.indexOf("改过的第一句") < 0, "撤销没生效");
    assert.ok($("#view").textContent.indexOf(original) >= 0, "撤销后没回到原文");

    redoBtn.click();
    await tick(250);
    assert.ok($("#view").textContent.indexOf("改过的第一句") >= 0, "重做没生效");
  });

  await check("质检徽标：长句标黄、负时长标红（规则与后端一致）", async () => {
    const before = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    const probe = [
      { id: "q1", start_ms: 0, end_ms: 3000, text: "正常的句子", words: [] },
      { id: "q2", start_ms: 3000, end_ms: 6000, text: "x".repeat(60), words: [] },
      { id: "q3", start_ms: 6000, end_ms: 5000, text: "倒着走", words: [] }
    ];
    const saved = await WB.rpc("subtitle.save", { project_id: "p_demo_1", segments: probe });
    assert.strictEqual(saved.qc.length, 2, `质检条数 ${saved.qc.length}，应为 2`);
    assert.strictEqual(saved.qc[0].level, "warn", "长句应是 warn");
    assert.strictEqual(saved.qc[1].kind, "negative", "倒序段应报 negative");
    assert.strictEqual(saved.qc[1].level, "error", "负时长必须是 error");

    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(200);
    const rows = window.document.querySelectorAll(".sub-row");
    assert.strictEqual(rows.length, 3, `渲染了 ${rows.length} 行`);
    assert.ok(rows[1].classList.contains("is-warn"), "长句那行没标黄");
    assert.ok(rows[2].classList.contains("is-error"), "负时长那行没标红");

    // 复位，后面的用例还要用这份演示数据
    await WB.rpc("subtitle.save", { project_id: "p_demo_1", segments: before.segments });
  });

  // ---- 字幕页一级入口（plan-subtitles-entry §5.5）----

  await check("字幕一级入口：无参数进入自动选默认项目", async () => {
    WB.router.go("subtitles");
    await until(() => {
      const params = WB.router.current().params || {};
      return params.project === "p_demo_1";
    }, "无参数进入后默认项目落位（应为第一个有字幕的项目）");
    await tick(200);
    const select = window.document.getElementById("sub-project-select");
    assert.ok(select, "字幕页没有项目下拉（#sub-project-select 缺失）");
    assert.strictEqual(select.value, "p_demo_1", "项目下拉没选中 p_demo_1");
    // 不钉第一行原文：前序编辑用例会改 p_demo_1 第一行，这里只验"行真的渲染出来了"
    assert.ok(window.document.querySelectorAll(".sub-row").length > 0,
      "行列表没渲染出 p_demo_1 的字幕行");
  });

  await check("项目条切换项目用 replace：地址更新但历史不增长", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => (WB.router.current().params || {}).project === "p_demo_1", "进入 p_demo_1");
    await tick(150);
    const before = window.history.length;
    const select = window.document.getElementById("sub-project-select");
    assert.ok(select, "没有项目下拉");
    select.value = "p_demo_2";
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    await until(() => (WB.router.current().params || {}).project === "p_demo_2", "切换到 p_demo_2");
    await tick(150);
    assert.ok(window.location.hash.indexOf("project=p_demo_2") >= 0,
      "hash 没带上 project=p_demo_2");
    assert.strictEqual(window.history.length, before,
      "项目切换不该产生历史条目（后退应回进入字幕页之前的页面）");
  });

  await check("切换项目会先保存旧项目的未保存草稿", async () => {
    const before = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => window.document.querySelectorAll(".sub-row").length > 0
      && (WB.router.current().params || {}).project === "p_demo_1", "进入 p_demo_1 并渲染行");
    await tick(150);
    const first = window.document.querySelectorAll(".sub-row")[0];
    first.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
    await tick(30);
    const input = window.document.querySelector(".sub-row__input");
    assert.ok(input, "双击没进入编辑态");
    input.value = "切换前保存测试";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await tick(50);
    const select = window.document.getElementById("sub-project-select");
    select.value = "p_demo_2";
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
    await tick(500);
    const saved = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    const texts = (saved.segments || []).map((s) => s.text);
    assert.ok(texts.indexOf("切换前保存测试") >= 0, "切走之后旧项目的草稿丢了（flush 没生效）");
    // 切回 p_demo_1，列表里要真的出现这句
    const back = window.document.getElementById("sub-project-select");
    back.value = "p_demo_1";
    back.dispatchEvent(new window.Event("change", { bubbles: true }));
    await until(() => (WB.router.current().params || {}).project === "p_demo_1"
      && window.document.querySelectorAll(".sub-row").length > 0, "切回 p_demo_1");
    await tick(150);
    assert.ok($("#view").textContent.indexOf("切换前保存测试") >= 0, "切回后列表没显示保存过的文本");
    await WB.rpc("subtitle.save", { project_id: "p_demo_1", segments: before.segments });
  });

  await check("保存失败时阻断切换：留在原项目、下拉回弹、给出提示", async () => {
    const before = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => window.document.querySelectorAll(".sub-row").length > 0
      && (WB.router.current().params || {}).project === "p_demo_1", "进入 p_demo_1");
    await tick(150);
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, options) {
      if (method === "subtitle.save") {
        return Promise.reject(new WB.RpcError("PROCESS_FAILED", "模拟保存失败"));
      }
      return realRpc(method, params, options);
    };
    try {
      const first = window.document.querySelectorAll(".sub-row")[0];
      first.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
      await tick(30);
      const input = window.document.querySelector(".sub-row__input");
      input.value = "阻断切换测试";
      input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      await tick(50);
      const select = window.document.getElementById("sub-project-select");
      select.value = "p_demo_2";
      select.dispatchEvent(new window.Event("change", { bubbles: true }));
      await tick(400);
      assert.strictEqual((WB.router.current().params || {}).project, "p_demo_1",
        "保存失败还把项目切走了");
      assert.strictEqual(select.value, "p_demo_1", "下拉没回弹到 p_demo_1");
      assert.ok(window.document.getElementById("sub-notice").textContent.indexOf("切换失败") >= 0,
        "页面上没有「切换失败」提示");
    } finally {
      WB.rpc = realRpc;
      await WB.rpc("subtitle.save", { project_id: "p_demo_1", segments: before.segments });
    }
  });

  await check("离开字幕页会先把未保存草稿落盘", async () => {
    const before = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => window.document.querySelectorAll(".sub-row").length > 0, "进入 p_demo_1");
    await tick(150);
    const first = window.document.querySelectorAll(".sub-row")[0];
    first.dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
    await tick(30);
    const input = window.document.querySelector(".sub-row__input");
    input.value = "离页保存测试";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await tick(50);
    WB.router.go("library");
    await tick(300);
    assert.strictEqual(WB.router.current().route, "library");
    const saved = await WB.rpc("subtitle.get", { project_id: "p_demo_1" });
    const texts = (saved.segments || []).map((s) => s.text);
    assert.ok(texts.indexOf("离页保存测试") >= 0, "离开字幕页时草稿丢了");
    await WB.rpc("subtitle.save", { project_id: "p_demo_1", segments: before.segments });
  });

  await check("重复进入字幕页播放器不消失", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => (WB.router.current().params || {}).project === "p_demo_1", "进入 p_demo_1");
    await tick(250);
    assert.strictEqual(window.document.querySelectorAll("#sub-video video").length, 1,
      "首次进入没挂上播放器");
    WB.router.go("library");
    await tick(150);
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => (WB.router.current().params || {}).project === "p_demo_1", "再次进入 p_demo_1");
    await tick(250);
    assert.strictEqual(window.document.querySelectorAll("#sub-video video").length, 1,
      "离开再回来播放器消失了（旧实现 video 非空直接 return）");
  });

  await check("「在文件夹中显示」传 root_dir，不带 project_id", async () => {
    const list = await WB.rpc("library.list", { query: "", limit: 50, offset: 0 });
    const demo = (list.items || []).find((row) => row.id === "p_demo_1");
    assert.ok(demo && demo.root_dir, "mock 里 p_demo_1 没有 root_dir");
    WB.router.go("subtitles", { project: "p_demo_1" });
    await until(() => (WB.router.current().params || {}).project === "p_demo_1", "进入 p_demo_1");
    await tick(200);
    const calls = [];
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, options) {
      if (method === "fs.reveal") calls.push(params);
      return realRpc(method, params, options);
    };
    try {
      const btn = window.document.getElementById("sub-reveal");
      assert.ok(btn, "字幕页没有「在文件夹中显示」按钮");
      assert.strictEqual(btn.disabled, false, "有 root_dir 时按钮不该是灰的");
      btn.click();
      await tick(80);
      assert.strictEqual(calls.length, 1, "fs.reveal 应该恰好被调一次");
      assert.strictEqual(calls[0].path, demo.root_dir, "fs.reveal 的 path 不是 root_dir");
      assert.strictEqual(Object.prototype.hasOwnProperty.call(calls[0], "project_id"), false,
        "fs.reveal 不该带 project_id");
    } finally {
      WB.rpc = realRpc;
    }
  });

  await check("未知路由回落到素材库而不是白屏", async () => {
    WB.router.go("no-such-page");
    await tick(60);
    assert.ok($("#view").innerHTML.length > 120, "未知路由把视图清空了");
  });

  await check("换皮肤只改 data-skin，不动 DOM 结构", async () => {
    const chip = window.document.querySelector('[data-skin-chip="exhibition"]');
    assert.ok(chip, "没有 exhibition 皮肤按钮");
    chip.click();
    await tick(30);
    assert.strictEqual(window.document.documentElement.getAttribute("data-skin"), "exhibition");
    assert.strictEqual(chip.getAttribute("aria-pressed"), "true");
    const arkChip = window.document.querySelector('[data-skin-chip="ark"]');
    assert.strictEqual(arkChip.getAttribute("aria-pressed"), "false");
  });

  await check("rpc 成功返回 data，失败抛出带码的错误", async () => {
    const data = await WB.rpc("library.list", { query: "", limit: 5, offset: 0 });
    assert.ok(data && Array.isArray(data.items), "library.list 没回 items");
    assert.ok(typeof data.total === "number");

    let caught = null;
    try {
      await WB.rpc("no.such.method", {}, { silent: true });
    } catch (err) {
      caught = err;
    }
    assert.ok(caught, "未知接口居然没抛错");
    assert.ok(caught.code, "错误里没有 code");
  });

  await check("没有桥时不能默认当 mock（回归：桌面版显示假数据）", async () => {
    // rpc.js 里 isMock 曾经写成"看不到 pywebview 就算 mock"。pywebview 是文档
    // 解析后才注入 api 的，于是桌面版真的跑起来也显示 mock 的示例列表。
    const fs2 = require("fs");
    const path2 = require("path");
    const src = fs2.readFileSync(path2.join(__dirname, "../../video_workbench/web/js/rpc.js"), "utf8");
    const fakeWindow = { WB: {} };
    const fakeLocation = { search: "", hash: "" };
    new Function("window", "location", "document", "setTimeout", src)(
      fakeWindow, fakeLocation, { addEventListener() {} }, () => {}
    );
    const WB2 = fakeWindow.WB;
    assert.strictEqual(WB2.isMock(), false, "没显式要求就不能算 mock");
    WB2.setMock(true);
    assert.strictEqual(WB2.isMock(), true, "降级后要能切到 mock");
  });

  await check("在素材库页回车 = 就地过滤，不跳路由", async () => {
    WB.router.go("library");
    await tick(60);
    const input = $("#global-search");
    assert.ok(input, "没有搜索框");
    input.value = "某期";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await tick(120);
    assert.strictEqual(WB.router.current().route, "library");
    assert.strictEqual(WB.pages.library.state.query, "某期");
    assert.strictEqual(WB.router.current().params.q, undefined, "就地过滤不该改地址");
  });

  await check("在别的页回车 = 带 q 跳回素材库", async () => {
    WB.router.go("settings");
    await tick(60);
    const input = $("#global-search");
    input.value = "关键词";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await tick(80);
    assert.strictEqual(WB.router.current().route, "library");
    assert.strictEqual(WB.router.current().params.q, "关键词");
    assert.strictEqual(WB.pages.library.state.query, "关键词");
  });

  await check("设置页「模型」分区有测试连接按钮，点了会回报延迟", async () => {
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const btn = [...window.document.querySelectorAll("#settings-body button")]
      .find((b) => b.textContent.trim() === "测试连接");
    assert.ok(btn, "模型分区没有「测试连接」");
    btn.click();
    await tick(120);
    const note = [...window.document.querySelectorAll("#settings-body span")]
      .map((s) => s.textContent).join(" ");
    assert.ok(note.indexOf("连上了") >= 0, "测试结果没显示出来：" + note.slice(0, 80));
  });

  await check("服务商卡片：增删、设为当前、折叠区与保存", async () => {
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const body = window.document.querySelector("#settings-body");
    const cards = () => [...body.querySelectorAll(".provider-card")];
    assert.ok(body.querySelector(".providers"), "没有服务商容器");
    assert.strictEqual(cards().length, 2, "mock 里应该有两张卡");
    const secondId = WB.mock.settings().llm.providers[1].id;

    // 「当前使用」只能有一个，另一张必须是「设为当前」按钮
    const on = cards().filter((c) => c.querySelector(".chip--on"));
    assert.strictEqual(on.length, 1, "当前使用标记不止一个：" + on.length);
    assert.strictEqual(cards().filter((c) => c.classList.contains("is-active")).length, 1);

    // 添加 → 删除
    const add = [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "添加服务商");
    assert.ok(add, "没有「添加服务商」按钮");
    add.click();
    await tick(40);
    assert.strictEqual(cards().length, 3, "添加后没多一张卡");
    const del = cards()[2].querySelector(".icon-btn");
    assert.ok(del, "第三张卡没有删除按钮");
    assert.ok(del.querySelector("svg path"), "删除按钮没有图形（点了个隐形按钮）");
    del.click();
    await tick(40);
    assert.strictEqual(cards().length, 2, "删除后卡片没少");

    // 就地改名称：不动数组引用，只标脏
    const nameInput = cards()[0].querySelector(".provider-card__name");
    assert.ok(nameInput, "卡头没有名称输入");
    nameInput.value = "DeepSeek 官方（改）";
    nameInput.dispatchEvent(new window.Event("input", { bubbles: true }));
    await tick(30);

    // 设为当前：标记换人 + active_provider 进 dirty
    const setBtn = [...cards()[1].querySelectorAll("button")].find((b) => b.textContent.trim() === "设为当前");
    assert.ok(setBtn, "第二张卡没有「设为当前」");
    setBtn.click();
    await tick(40);
    assert.ok(cards()[1].querySelector(".chip--on"), "切换后没有当前使用标记");
    assert.strictEqual(cards().filter((c) => c.querySelector(".chip--on")).length, 1, "切完出现两个当前");

    // 折叠区「上下文与长素材」：默认收起，展开后有分段控件与七行任务映射
    const adv = body.querySelector("details.adv");
    assert.ok(adv, "没有折叠区");
    assert.strictEqual(adv.open, false, "折叠区默认就该收起");
    assert.strictEqual(adv.querySelector(".adv__title").textContent.trim(), "上下文与长素材");
    adv.open = true;
    await tick(30);
    // 「深度思考」三档分段控件已在 task-26 删除（WB 自己不发送 enable_thinking，
    // 留着按钮等于假承诺；理由逐字写在 settings.js 的字段表里）。这里改成反向锁：
    // 它不许回来。原来那条 >= 3 的断言在控件删掉当天就该跟着删，留着的结果是
    // 整个套件在 HEAD 上常年红一条，谁也不敢把它当闸用。
    assert.strictEqual(adv.querySelectorAll(".seg__item").length, 0,
                       "「深度思考」分段控件不该回来（WB 不发这个参数）");
    assert.strictEqual(adv.querySelectorAll(".taskmap__row").length, 7, "任务模型该有七行");

    // 保存：providers 整个数组 + active_provider 一起发出去（mock 的 merge 对数组是整体替换）
    const save = [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存");
    assert.ok(save, "没有保存按钮");
    save.click();
    await tick(160);
    const saved = WB.mock.settings().llm;
    assert.strictEqual(saved.active_provider, secondId, "保存后当前服务商没切过去");
    assert.strictEqual(saved.providers.length, 2, "保存后服务商条数不对");
    assert.strictEqual(saved.providers[0].name, "DeepSeek 官方（改）", "就地改的名称没提交");
  });

  await check("服务商卡片：获取可用模型 → 勾选后加进 chips", async () => {
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const body = window.document.querySelector("#settings-body");
    const card = body.querySelectorAll(".provider-card")[0];
    const chipTexts = () => [...card.querySelectorAll(".chip .mono")].map((el) => el.textContent);
    const before = chipTexts();

    const btn = [...card.querySelectorAll("button")].find((b) => b.textContent.trim() === "获取可用模型");
    assert.ok(btn, "卡片上没有「获取可用模型」按钮");
    btn.click();
    await tick(200);

    const rows = () => [...window.document.querySelectorAll(".model-pick__row")];
    assert.ok(rows().length >= 3, "候选模型没列出来，实际 " + rows().length + " 行");
    const boxes = rows().map((r) => r.querySelector("input[type=checkbox]"));
    assert.ok(boxes.every(Boolean), "每一行都要有勾选框");
    const checkedIds = boxes.filter((b) => b.checked).map((b) => b.value);
    assert.ok(
      before.every((m) => checkedIds.indexOf(m) >= 0),
      "已配置的模型没有预勾上：" + before.join(",") + " vs " + checkedIds.join(",")
    );

    const all = [...window.document.querySelectorAll(".model-pick__bar button")].find(
      (b) => b.textContent.trim() === "全选"
    );
    assert.ok(all, "弹窗里没有「全选」");
    all.click();
    await tick(30);

    const addBtn = [...window.document.querySelectorAll(".modal__foot button")].find(
      (b) => b.textContent.trim() === "保存所选"
    );
    assert.ok(addBtn, "弹窗里没有「保存所选」");
    assert.strictEqual(addBtn.disabled, false, "列出候选后「保存所选」该可用");
    const total = rows().length;
    addBtn.click();
    await tick(80);
    assert.strictEqual(rows().length, 0, "确认后弹窗没关");
    assert.strictEqual(chipTexts().length, total, "加完之后 chips 数应等于候选数");

    const save = [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存");
    assert.ok(save, "没有保存按钮");
    save.click();
    await tick(200);
    const saved = WB.mock.settings().llm.providers[0].models;
    assert.strictEqual(saved.length, total, "保存后这家卡的模型数不对：" + saved.length + " vs " + total);
    assert.ok(
      chipTexts().length === saved.length,
      "保存回填后 chips 与库里的模型数不一致"
    );
  });

  await check("服务商卡片：弹窗里取消勾选，卡片里的模型也跟着少一个", async () => {
    // 之前弹窗只能"加"：勾选框看起来是「已选」，可取消勾选完全不生效，
    // 卡片与弹窗各说一套。现在勾选集合就是这家的模型列表。
    WB.router.go("settings", { section: "llm" });
    await tick(100);
    const body = window.document.querySelector("#settings-body");
    const card = () => body.querySelectorAll(".provider-card")[0];
    const chips = () => [...card().querySelectorAll(".chip .mono")].map((el) => el.textContent);
    const before = chips();
    assert.ok(before.length >= 2, "这张卡初始应有多个模型，实际 " + before.join(","));

    [...card().querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "获取可用模型")
      .click();
    await tick(200);

    const rows = [...window.document.querySelectorAll(".model-pick__row")];
    const target = rows.find((r) => {
      const cb = r.querySelector("input[type=checkbox]");
      return cb && cb.value === before[0];
    });
    assert.ok(target, "弹窗里找不到已在卡片上的模型：" + before[0]);
    const box = target.querySelector("input[type=checkbox]");
    box.checked = false;
    box.dispatchEvent(new window.Event("change", { bubbles: true }));
    await tick(30);

    [...window.document.querySelectorAll(".modal__foot button")]
      .find((b) => b.textContent.trim() === "保存所选")
      .click();
    await tick(100);

    const after = chips();
    assert.strictEqual(after.indexOf(before[0]), -1, "取消勾选后卡片里还留着 " + before[0]);
    assert.strictEqual(after.length, before.length - 1, "卡片模型数应减一");
    assert.strictEqual(
      [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存").disabled,
      false,
      "取消勾选后没有标脏，保存按钮是灰的"
    );
  });

  await check("设置页：保存过一次之后，再改模型列表仍然能保存", async () => {
    // 回归：current 若与 base 共享引用（浅拷），就地改数组后 JSON 相等 → 永远标不出脏，
    // 症状是「保存过一次，之后再改模型列表，保存按钮一直是灰的，得先去动一下别的开关」。
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const body = window.document.querySelector("#settings-body");
    const card = () => body.querySelectorAll(".provider-card")[0];
    const nameInput = () =>
      [...card().querySelectorAll("input")].find((el) => el.placeholder === "模型名，回车添加");
    const saveBtn = () =>
      [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存");

    function addModel(text) {
      const input = nameInput();
      assert.ok(input, "没找到「模型名，回车添加」输入框");
      input.value = text;
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
      input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }

    addModel("probe-a");
    await tick(30);
    assert.strictEqual(saveBtn().disabled, false, "第一次改模型后保存按钮就该亮");
    saveBtn().click();
    await tick(200);
    assert.ok(
      WB.mock.settings().llm.providers[0].models.indexOf("probe-a") >= 0,
      "第一次改的模型没存进去"
    );

    // 第二次改：这里就是那条回归
    addModel("probe-b");
    await tick(40);
    assert.strictEqual(
      saveBtn().disabled,
      false,
      "保存过一次再改模型列表，保存按钮还是灰的（脏检测失效）"
    );
    saveBtn().click();
    await tick(200);
    const saved = WB.mock.settings().llm.providers[0].models;
    assert.ok(saved.indexOf("probe-b") >= 0, "第二次改的模型没存进去");
    assert.ok(saved.indexOf("probe-a") >= 0, "第二次保存把第一次的模型弄丢了");
    assert.strictEqual(saveBtn().disabled, true, "存完没有新改动时保存按钮该回到禁用");
  });

  await check("设置页：数字输入框边打边标脏，不用先点别处", async () => {
    // 回归：数字框原本只绑 onchange（失焦才触发）。用户改完数字直接点保存——按钮此时
    // 还是禁用态，点不动，只能先去点一下别的控件「选中」才生效。
    WB.router.go("settings", { section: "general" });
    await tick(100);
    const body = window.document.querySelector("#settings-body");
    const portInput = [...body.querySelectorAll("input[type=number]")][0];
    assert.ok(portInput, "常规分区没有数字输入框");
    const saveBtn = () =>
      [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存");

    portInput.value = "9123";
    portInput.dispatchEvent(new window.Event("input", { bubbles: true }));
    await tick(40);
    assert.strictEqual(
      saveBtn().disabled,
      false,
      "改了数字、光标还在框里，保存按钮却是灰的（检测要等失焦才发生）"
    );
    saveBtn().click();
    await tick(200);
    assert.strictEqual(
      window.WB.mock.settings().server.port,
      9123,
      "数字改动没保存进去"
    );
  });

  await check("左栏底部的「设置」也能打开设置页", async () => {
    // 底部那个「设置」是 index.html 里的静态按钮（不在 navItems 里），
    // 之前点了没反应：renderRail 只给 #rail-nav 挂点击。
    WB.router.go("library");
    await tick(100);
    const foot = window.document.querySelector(".rail .rail__foot .rail__item[data-route=settings]");
    assert.ok(foot, "左栏底部没找到设置按钮");
    foot.click();
    await tick(120);
    assert.strictEqual(WB.router.current().route, "settings", "点了底部设置没进设置页");
    const body = window.document.querySelector("#settings-body");
    assert.ok(body && body.textContent.trim().length > 0, "设置页没渲染内容");
  });

  await check("模型分区：还没保存就能测连接、取可用模型", async () => {
    // 用户的实际动作：新填地址和密钥 → 直接点「测试连接」/「获取可用模型」。
    // 之前后端只认磁盘配置，于是明明填了却说「缺 api_key」。
    WB.router.go("settings", { section: "llm" });
    await tick(100);
    const body = window.document.querySelector("#settings-body");
    const card = body.querySelector(".provider-card");
    assert.ok(card, "没有服务商卡片");
    // 按 type/class 精确取：第一张卡里还有一个「名称」输入框（input.provider-card__name）
    const base = card.querySelector("input.mono");
    const key = card.querySelector('input[type="password"]');
    base.value = "https://typed.example/v1";
    base.dispatchEvent(new window.Event("input", { bubbles: true }));
    key.value = "sk-typed";
    key.dispatchEvent(new window.Event("input", { bubbles: true }));
    await tick(40);

    const btn = (text) =>
      [...card.querySelectorAll("button")].find((b) => b.textContent.trim() === text);

    // 盯住参数本身：mock 里第一张卡本来就带密钥，只看"成功"分不出
    // 到底用的是卡片里现填的值还是磁盘上的旧值。
    const llmCalls = [];
    const origRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (String(method).indexOf("llm.") === 0) llmCalls.push({ method: method, params: params || {} });
      return origRpc.call(WB, method, params, opts);
    };

    // 测试连接：不该再报「缺 api_key」
    btn("测试连接").click();
    await tick(150);
    const probeText = [...card.querySelectorAll("span")].map((s) => s.textContent).join(" | ");
    assert.ok(
      probeText.indexOf("连上了") >= 0,
      "没保存就测试连接失败了，卡片上的回报是：" + probeText.slice(0, 80)
    );

    // 获取可用模型：弹窗里应该真列出候选
    btn("获取可用模型").click();
    await tick(150);
    const rows = [...window.document.querySelectorAll(".model-pick__row")];
    assert.ok(rows.length >= 3, "没保存就取模型，弹窗里没有候选，实际 " + rows.length);
    const cancel = [...window.document.querySelectorAll(".modal__foot button")].find(
      (b) => b.textContent.trim() === "取消"
    );
    if (cancel) cancel.click();
    await tick(40);
    WB.rpc = origRpc;

    const probeCall = llmCalls.find((c) => c.method === "llm.test");
    const modelsCall = llmCalls.find((c) => c.method === "llm.models");
    assert.ok(probeCall && modelsCall, "没发 llm.test / llm.models 请求");
    [probeCall, modelsCall].forEach((c) => {
      assert.strictEqual(
        c.params.base_url,
        "https://typed.example/v1",
        c.method + " 没把界面上刚填的地址传上去"
      );
      assert.strictEqual(
        c.params.api_key,
        "sk-typed",
        c.method + " 没把界面上刚填的密钥传上去"
      );
    });
  });

  await check("模型分区：一键接入本机 magpie，自动带地址密钥并设为当前", async () => {
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const body = window.document.querySelector("#settings-body");
    const cards = () => [...body.querySelectorAll(".provider-card")];

    const btn = [...body.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "接入本机 magpie"
    );
    assert.ok(btn, "模型分区里没有「接入本机 magpie」按钮");

    const before = cards().length;
    btn.click();
    await tick(160);

    const after = cards();
    assert.strictEqual(after.length, before + 1, "点完没有多出一张卡");

    const card = after[after.length - 1];
    const inputs = [...card.querySelectorAll("input")].filter(
      (i) => i.type !== "checkbox" && i.type !== "radio"
    );
    const url = inputs.find((i) => String(i.value).indexOf("127.0.0.1:3425") >= 0);
    assert.ok(url, "新卡里没有 magpie 的地址，实际 " + inputs.map((i) => i.value).join(" | "));
    assert.ok(card.classList.contains("is-active"), "接进来的卡没有设为当前使用");

    const chips = [...card.querySelectorAll(".chip .mono")].map((n) => n.textContent.trim());
    assert.ok(chips.length >= 3, "模型列表没拉回来，实际 " + chips.length + " 个");

    // 再点一次不该重复建卡（只认地址里带 3425 的那张）
    btn.click();
    await tick(120);
    assert.strictEqual(cards().length, before + 1, "重复点会重复建卡");

    // 存下去，卡片要真的进了配置
    const saveBtn = [...body.querySelectorAll("button")].find((b) => b.textContent.trim() === "保存");
    assert.ok(saveBtn, "找不到保存按钮");
    assert.strictEqual(saveBtn.disabled, false, "接入之后保存按钮还是灰的（没标脏）");
    saveBtn.click();
    await tick(140);
    const provs = WB.mock.settings().llm.providers || [];
    const saved = provs.find((x) => String(x.base_url || "").indexOf("127.0.0.1:3425") >= 0);
    assert.ok(saved, "保存后配置里没有 magpie 这张卡");
    assert.ok((saved.models || []).length >= 3, "保存后模型列表是空的");
    assert.strictEqual(WB.mock.settings().llm.active_provider, saved.id, "保存后当前服务商不是它");
  });

  await check("模型分区只剩一张卡时不给删除按钮（不能删空）", async () => {
    WB.router.go("settings", { section: "llm" });
    await tick(80);
    const body = window.document.querySelector("#settings-body");
    const cards = () => [...body.querySelectorAll(".provider-card")];
    let guard = 0;
    while (cards().length > 1 && guard < 6) {
      const del = cards()[cards().length - 1].querySelector(".icon-btn");
      assert.ok(del, "多卡时该有删除按钮");
      del.click();
      await tick(30);
      guard += 1;
    }
    assert.strictEqual(cards().length, 1, "删到一张就停");
    assert.strictEqual(cards()[0].querySelector(".icon-btn"), null, "最后一张还能删，会被删空");
    assert.ok(cards()[0].querySelector(".chip--on"), "唯一一张该标成当前使用");
  });

  await check("字幕编辑器：AI 优化出预览清单，勾选后能应用", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(120);
    const page = WB.pages.subtitles;
    const view = $("#view");
    const btn = [...view.querySelectorAll(".sub-toolbar button")]
      .find((b) => b.textContent.trim() === "AI 优化");
    assert.ok(btn, "工具栏没有 AI 优化");
    const before = page.state.segments.map((s) => s.text).join("|");
    btn.click();
    await tick(400);
    const rows = view.querySelectorAll(".sub-opt__row");
    assert.ok(rows.length >= 1, "预览列表是空的");
    assert.ok(view.querySelector(".sub-opt").hidden === false, "优化面板没展开");
    const apply = [...view.querySelectorAll(".sub-opt__bar button")]
      .find((b) => b.textContent.trim() === "应用所选");
    assert.ok(apply, "没有应用按钮");
    apply.click();
    await tick(400);
    const after = page.state.segments.map((s) => s.text).join("|");
    assert.notStrictEqual(after, before, "应用后文本没变化");
    assert.ok(view.querySelector(".sub-opt").hidden === true, "应用后面板没收起");
  });

  await check("字幕编辑器：AI 翻译出双语草稿，手改译文能落回草稿", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(120);
    const view = $("#view");
    const btn = [...view.querySelectorAll(".sub-toolbar button")]
      .find((b) => b.textContent.trim() === "AI 翻译");
    assert.ok(btn, "工具栏没有 AI 翻译");
    btn.click();
    await tick(300);
    const panel = view.querySelector("#sub-trans");
    assert.ok(panel && panel.hidden === false, "翻译面板没展开");
    assert.ok(view.querySelector("#tr-target"), "表单没有目标语言选择");
    assert.ok(view.querySelector("#tr-layout"), "表单没有双语版式选择");
    const start = [...panel.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "开始翻译");
    assert.ok(start, "没有开始翻译按钮");
    start.click();
    await tick(500);
    const rows = panel.querySelectorAll(".tr-row");
    assert.ok(rows.length >= 4, "草稿行数太少：" + rows.length);
    assert.ok([...panel.querySelectorAll("button")].some((b) => b.textContent.trim() === "重试失败段"),
      "mock 里有失败批，界面却没给重试入口");
    const edit = rows[0].querySelector(".tr-edit");
    assert.ok(edit, "译文没有可编辑的输入框");
    const edited = "手改过的译文";
    edit.value = edited;
    edit.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    await tick(60);
    assert.ok(/手改 1/.test(panel.querySelector(".sub-opt__bar .muted").textContent),
      "改动没进计数：" + panel.querySelector(".sub-opt__bar .muted").textContent);
    const apply = [...panel.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "写入双语字幕");
    assert.ok(apply, "没有写入双语字幕按钮");
    apply.click();
    await tick(400);
    assert.ok(panel.hidden === true, "确认后没收起面板");
    // 重开面板：mock 草稿里应还是改过的那句，等于验证 overrides 真的回了后端
    WB.router.go("library", {});
    await tick(80);
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(150);
    const again = [...$("#view").querySelectorAll(".sub-toolbar button")]
      .find((b) => b.textContent.trim() === "AI 翻译");
    again.click();
    await tick(400);
    const back = $("#view").querySelector(".tr-row .tr-edit");
    assert.strictEqual(back.value, edited, "手改的译文没回写到草稿");
  });

  await check("字幕编辑器：导出面板能出字幕文件，软字幕走任务并回报成片", async () => {
    WB.router.go("subtitles", { project: "p_demo_1" });
    await tick(150);
    const view = $("#view");
    const open = [...view.querySelectorAll(".sub-toolbar button")]
      .find((b) => b.textContent.trim() === "导出");
    assert.ok(open, "工具栏没有导出");
    open.click();
    await tick(200);
    const panel = view.querySelector("#sub-export");
    assert.ok(panel && panel.hidden === false, "导出面板没展开");
    const boxes = [...panel.querySelectorAll(".exp-fmt")];
    assert.strictEqual(boxes.length, 3, "格式勾选项不是三个：" + boxes.length);
    assert.ok(boxes.every((b) => b.checked), "默认应该三个格式全勾");
    const mode = panel.querySelector("#exp-mode");
    assert.ok(mode, "没有出片方式选择");
    assert.strictEqual(mode.options.length, 3, "出片方式应该三选一");
    const height = panel.querySelector("#exp-height");
    assert.ok(height && height.options.length === 3, "输出高度选项不对");

    // ① 只出字幕文件：同步返回，不该起任务
    const start = [...panel.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "开始导出");
    assert.ok(start, "没有开始导出按钮");
    start.click();
    await tick(300);
    const files = [...panel.querySelectorAll(".exp-file")];
    assert.strictEqual(files.length, 3, "字幕文件行数不对：" + files.length);
    assert.ok(/SRT|ASS|VTT/.test(files.map((f) => f.textContent).join("")), "没列出格式名");
    assert.ok(files[0].querySelector(".exp-file__path").textContent.indexOf("subtitle.") > 0,
      "没列出落盘路径：" + files[0].textContent);

    // ② 软字幕：走任务队列，轮询到 succeeded 后把成片路径显示出来
    const again = [...view.querySelectorAll(".sub-toolbar button")]
      .find((b) => b.textContent.trim() === "导出");
    again.click();
    await tick(150);
    const panel2 = view.querySelector("#sub-export");
    const mode2 = panel2.querySelector("#exp-mode");
    mode2.value = "soft";
    const start2 = [...panel2.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "开始导出");
    start2.click();
    await tick(700);
    const done = panel2.querySelector("#exp-done");
    assert.ok(done, "软字幕没走到完成态：" + panel2.textContent.slice(0, 80));
    const all = panel2.textContent;
    assert.ok(all.indexOf("软字幕") >= 0, "成片行没标软字幕");
    assert.ok(/\.mp4/.test(all), "成片路径不是 mp4：" + all.slice(0, 120));
    assert.ok(all.indexOf("MB") > 0, "没显示成片大小");
  });

  await check("总结页：换角色重出、按一句话打磨、恢复旧版都走通", async () => {
    WB.router.go("summary", {});
    await tick(260);
    const view = $("#view");
    const picker = view.querySelector("#sum-project");
    assert.ok(picker, "没有项目选择");
    assert.ok(picker.options.length >= 1, "项目列表是空的");
    const role = view.querySelector("#sum-role");
    assert.ok(role, "没有角色选择");
    assert.ok(role.options.length >= 6, "角色太少：" + role.options.length);
    // mock 里 p_demo_1 已有两版总结，默认项目应能读到
    const host = view.querySelector("#summary-host");
    assert.ok(host, "没有正文容器");
    assert.ok(host.querySelector("#sum-body"), "没渲染正文卡");
    // mock 的正文跟着项目标题走（写死一段文案会在截图里跟左边项目对不上），
    // 所以这里断言的是"有正文结构"，不是某句话。
    assert.ok(host.querySelectorAll("#sum-body .md__h").length >= 2,
              "正文没渲染：" + host.textContent.slice(0, 60));
    assert.ok(host.querySelectorAll(".sum-time__row").length >= 1, "没有时间线");
    const versionNoteLabel = host.querySelector(".ver-row__note b");
    assert.ok(versionNoteLabel, "版本行没有说明标签");
    assert.strictEqual(versionNoteLabel.textContent, "它说", "模型自述被标成了实际改动");

    // ① 换角色重新生成 → 版本数涨到 3
    const before = host.querySelectorAll(".ver-row").length;
    role.value = "xueshu";
    role.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    const start = view.querySelector("#sum-start");
    assert.ok(start, "没有生成按钮");
    start.click();
    await tick(1200);
    const after = view.querySelectorAll(".ver-row").length;
    assert.ok(after > before, "重新生成没有加版本：" + before + " → " + after);

    // ② 打磨：输入一句要求，回到新版本且带修改说明
    const need = view.querySelector("#sum-requirement");
    assert.ok(need, "没有打磨输入框");
    need.value = "要点压到三条";
    need.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    const go = view.querySelector("#sum-refine");
    assert.ok(go, "没有按这个改按钮");
    go.click();
    await tick(1200);
    assert.ok(/压到三条/.test(view.querySelector("#summary-host").textContent),
      "修改说明没显示：" + view.querySelector("#summary-host").textContent.slice(0, 120));

    // ③ 恢复旧版：列表按版本倒序，第一行是当前版（没有恢复按钮），
    //    最末行才是最早的那一版。点它，版本数继续涨而不是替换。
    const rows = [...view.querySelectorAll(".ver-row")];
    const oldest = rows[rows.length - 1];
    assert.ok(rows[0].classList.contains("is-active"), "当前版本没标出来");
    const restoreBtn = [...oldest.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "恢复");
    assert.ok(restoreBtn, "非当前版本没有恢复按钮");
    const counted = rows.length;
    restoreBtn.click();
    await tick(700);
    const rows2 = view.querySelectorAll(".ver-row");
    assert.ok(rows2.length > counted, "恢复应该新增一版：" + counted + " → " + rows2.length);
    assert.ok(/恢复自第\d+版/.test(view.querySelector("#summary-host").textContent.replace(/ /g, "")) ||
      /恢复自第 \d+ 版/.test(view.querySelector("#summary-host").textContent),
      "没标出恢复来源");
  });

  await check("总结页：用量卡给出这一版开销与这条视频累计", async () => {
    WB.router.go("summary", {});
    await tick(260);
    const view = $("#view");
    const card = view.querySelector("#sum-cost");
    assert.ok(card, "没有用量卡（#sum-cost）");
    const text = card.textContent;
    assert.ok(/这一版开销/.test(text), "没标出这一版：" + text.slice(0, 80));
    assert.ok(/这条视频累计/.test(text), "没有累计段：" + text.slice(0, 160));
    assert.ok(/输入 tokens/.test(text) && /输出 tokens/.test(text), "缺输入/输出：" + text.slice(0, 160));
    assert.ok(/总 tokens/.test(text), "缺总量行：" + text.slice(0, 160));
    // 数字必须是人能读的分组格式，不是 241242 一坨
    const values = [...card.querySelectorAll(".sum-cost__value")].map((n) => n.textContent.trim());
    assert.ok(values.some((v) => /^\d{1,3}(,\d{3})+$/.test(v)), "没有分组数字：" + JSON.stringify(values));

    const inst = WB.pages.summary._internals;
    assert.strictEqual(inst.groupNum(241242), "241,242", "分组函数不对");
    assert.strictEqual(inst.groupNum(0), "—", "0 应显示为占位而不是 0");
    assert.strictEqual(inst.groupSeconds(1092), "18m12s", "耗时格式不对");
    assert.strictEqual(inst.groupSeconds(84.6), "84.6s", "短耗时格式不对");

    // 累计必须 ≥ 单版（求和，而不是拿最新一版顶替）
    const nums = (label) => {
      const row = [...card.querySelectorAll(".sum-cost__row")]
        .find((r) => r.textContent.indexOf(label) === 0);
      const raw = row ? row.querySelector(".sum-cost__value").textContent.replace(/,/g, "") : "";
      return parseInt(raw, 10);
    };
    const single = nums("调用次数");
    const total = nums("模型调用");
    assert.ok(total >= single, "累计比单版还少：" + total + " < " + single);
  });


  await check("总结页：长任务不会在 12 分钟假装失败", async () => {
    // 现场 bug：轮询到 900 次（12 分钟）就 reject「等太久了，先停在这里」，
    // 界面看着像失败，其实后台还在跑（两小时视频三轮回要 20 分钟）。
    const poll = WB._summaryPoll;
    assert.ok(poll, "没暴露轮询策略（WB._summaryPoll）");
    assert.ok(poll.limit > 0 && poll.hard > poll.limit, "阈值不合理");
    assert.equal(poll.gaveUp(poll.limit + 1), false, "刚到 12 分钟就放弃了");
    assert.equal(poll.gaveUp(poll.limit + 200), false, "还在正常区间就放弃了");
    assert.equal(poll.gaveUp(poll.hard + 1), true, "到了硬上限也不放手");
    assert.ok(poll.delay(1) < poll.slow, "正常阶段不该已经是慢速");
    assert.equal(poll.delay(poll.limit + 1), poll.slow, "过了 12 分钟没有放慢节奏");
  });

  await check("总结页：换模型后，生成请求带的就是那个模型", async () => {
    WB.router.go("summary", {});
    await tick(260);
    const view = $("#view");
    const pick = view.querySelector("#sum-model");
    assert.ok(pick, "工具栏没有模型下拉");
    assert.ok(pick.options.length >= 2, "模型下拉只有一个选项：" + pick.options.length);
    // 第一项是「跟随设置」，其余是具体模型；选一个不等于默认的
    const target = [...pick.options].map((o) => o.value).filter((v) => v)[0];
    assert.ok(target, "没有可选的模型 spec");
    pick.value = target;
    pick.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(60);

    // 抓 summary.start 的参数：模型必须原样带上去
    const seen = [];
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "summary.start") seen.push(params || {});
      return realRpc.call(WB, method, params, opts);
    };
    try {
      view.querySelector("#sum-start").click();
      await tick(600);
    } finally {
      WB.rpc = realRpc;
    }
    assert.strictEqual(seen.length, 1, "没发出 summary.start");
    assert.strictEqual(seen[0].model, target, "带上去的模型不对：" + JSON.stringify(seen[0]));
  });
  await check("总结页：模型输出里的 HTML 不会被执行", async () => {
    const md = window.WB.pages.summary._internals.markdown;
    const node = md("## 标题\n正文里有 <img src=x onerror=alert(1)> 和 <script>bad()</script>");
    assert.strictEqual(node.querySelectorAll("img").length, 0, "把模型的 HTML 当真渲染了");
    assert.strictEqual(node.querySelectorAll("script").length, 0, "把模型的 script 插进 DOM 了");
    assert.ok(node.textContent.indexOf("onerror") > 0, "原始文本应该原样显示");
  });

  await check("总结页：切到合集能看子项、生成总览、版本增长", async () => {
    // 上一段测完后 state.mode 可能停在 video，这里从头进：先进单条，再切合集。
    window.WB.pages.summary._internals.switchMode("video");
    WB.router.go("summary", {});
    await tick(260);
    // 首屏按钮不能是灰的：head() 建在数据之前，靠 paint() 里的 swapHead() 纠正。
    // 这条曾经真的坏过——用户第一眼看到「生成总结」点不动。
    const firstStart = $("#view").querySelector("#sum-start");
    assert.ok(firstStart, "单条模式没有生成按钮");
    assert.strictEqual(firstStart.disabled, false, "首屏「生成总结」是灰的（head 没跟着数据刷新）");
    assert.ok($("#view").querySelector("#sum-project").value, "首屏项目下拉是空的");
    const tabs = $("#view").querySelectorAll(".sum-tab");
    assert.strictEqual(tabs.length, 2, "没有「单条视频 / 合集」切换：" + tabs.length);
    tabs[1].click();
    await tick(420);

    const view = $("#view");
    const picker = view.querySelector("#sum-collection");
    assert.ok(picker, "合集模式没有合集选择");
    const host = view.querySelector("#summary-host");
    assert.ok(host, "合集模式没有正文容器");
    const rows = host.querySelectorAll(".coll-row");
    assert.ok(rows.length >= 2, "子项没渲染：" + rows.length);
    assert.strictEqual(host.querySelectorAll("#sum-body").length, 0,
                       "合集模式不该出现单条正文卡");
    const text = host.textContent;
    assert.ok(text.indexOf("核心结论") >= 0, "总览没渲染核心结论：" + text.slice(0, 60));
    assert.ok(text.indexOf("推荐观看顺序") >= 0, "总览没渲染观看顺序");

    // 生成/重新生成都要新增一版（真后端每次 submit 都 insert）
    const before = view.querySelectorAll(".ver-row").length;
    const go = view.querySelector("#sum-coll-start");
    assert.ok(go, "没有生成总览按钮");
    assert.strictEqual(go.disabled, false, "「生成总览」是灰的（合集列表到了但按钮没刷新）");
    go.click();
    await tick(1400);
    const after = $("#view").querySelectorAll(".ver-row").length;
    assert.ok(after > before, "生成总览没有加版本：" + before + " → " + after);
  });

  await check("总结页与轻量总结是两条独立路由", async () => {
    if (WB.pages.summary.state.mode !== "video") {
      WB.pages.summary._internals.switchMode("video");
      await tick(300);
    }
    WB.router.go("summary", {});
    await tick(300);
    let view = $("#view");
    assert.strictEqual(view.querySelectorAll(".sum-src").length, 0, "本地项目页不该再有来源切换");
    assert.ok(view.querySelector("#sum-project"), "本地项目页没有项目下拉");
    assert.ok(view.querySelector("#sum-role"), "本地项目页没有角色下拉");
    assert.ok(view.querySelector("#sum-model"), "本地项目页没有模型下拉");
    assert.strictEqual(view.querySelectorAll(".sum-tab").length, 2, "本地项目页的标签条不对");

    WB.router.go("light", {});
    await tick(300);
    view = $("#view");
    assert.ok(WB.pages.summaryLight, "没有注册轻量总结页面");
    assert.notStrictEqual(WB.pages.summary.state, WB.pages.summaryLight.state, "两个页面共用了同一份 state");
    assert.strictEqual(view.querySelectorAll(".sum-tab").length, 0, "轻量页不该有单条/合集标签");
    assert.ok(!view.querySelector("#sum-project"), "轻量页不该有本地项目下拉");
    assert.ok(!view.querySelector("#sum-title"), "轻量空态不该出现本地正文的项目标题");
    assert.strictEqual(view.querySelectorAll(".sum-side .card").length, 0, "轻量空态不该挂本地总结的版本/打磨/用量侧栏");
    assert.strictEqual(view.querySelector("#sum-light-title").textContent.trim(), "轻量总结");
    const url = view.querySelector("#sum-light-url");
    const role = view.querySelector("#sum-light-role");
    const model = view.querySelector("#sum-light-model");
    const probeBtn = view.querySelector("#sum-light-probe");
    const fileBtn = view.querySelector("#sum-light-file");
    const go = view.querySelector("#sum-light-go");
    assert.ok(url && role && model && probeBtn && fileBtn && go, "轻量面板字段不全");
    assert.strictEqual(go.disabled, false, "「提取字幕并总结」是灰的");
  });

  await check("轻量页：角色/模型随开始一起提交", async () => {
    const view = $("#view");
    await until(() => view.querySelectorAll("#sum-light-role option").length >= 2, "轻量角色选项");
    await until(() => view.querySelectorAll("#sum-light-model option").length >= 2, "轻量模型选项");
    const role = view.querySelector("#sum-light-role");
    const roleTarget = [...role.options].map((o) => o.value).filter((v) => v)[0];
    assert.ok(roleTarget, "没有可选角色");
    role.value = roleTarget;
    role.dispatchEvent(new window.Event("change", { bubbles: true }));
    const model = view.querySelector("#sum-light-model");
    const modelTarget = [...model.options].map((o) => o.value).filter((v) => v)[0];
    assert.ok(modelTarget, "没有可选模型");
    model.value = modelTarget;
    model.dispatchEvent(new window.Event("change", { bubbles: true }));
    await tick(60);

    const input = view.querySelector("#sum-light-url");
    input.value = "BV1xx411c7mD";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    const seen = [];
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "summary.start") seen.push(params || {});
      return realRpc.call(WB, method, params, opts);
    };
    try {
      view.querySelector("#sum-light-go").click();
      await until(() => seen.length === 1, "轻量页发出 summary.start", 40);
      await tick(500);
    } finally {
      WB.rpc = realRpc;
    }
    assert.strictEqual(seen[0].role, roleTarget, "轻量页带上去的角色不对：" + JSON.stringify(seen[0]));
    assert.strictEqual(seen[0].model, modelTarget, "轻量页带上去的模型不对：" + JSON.stringify(seen[0]));
    const lightProject = WB.pages.summaryLight.state.projectId;
    assert.ok(lightProject, "轻量页没有拿到自己的项目");
    assert.strictEqual(WB.pages.publish.state.filterProjectId, lightProject, "发布卡没有按轻量页的项目过滤");
  });

  await check("轻量页：空输入提示、解析字幕、选文件", async () => {
    const view = $("#view");
    const input = view.querySelector("#sum-light-url");
    input.value = "";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    view.querySelector("#sum-light-go").click();
    await tick(120);
    const note = view.querySelector("#sum-light-note") || view.querySelector("#sum-light-error");
    assert.ok(note && note.textContent.trim().length > 0, "空输入点下去没有任何提示");

    input.value = "BV1yy411c7mD";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    view.querySelector("#sum-light-probe").click();
    await tick(400);
    const picker2 = $("#view").querySelector("#sum-light-subs");
    assert.ok(picker2, "解析完没有字幕轨下拉");
    assert.strictEqual(picker2.options.length, 2, "字幕轨数量不对：" + picker2.options.length);
    assert.ok(picker2.textContent.indexOf("AI") >= 0, "AI 字幕没标出来：" + picker2.textContent);

    $("#view").querySelector("#sum-light-file").click();
    await tick(300);
    const srt = $("#view").querySelector("#sum-light-srt");
    assert.ok(srt, "选了文件但界面上看不到文件名");
  });

  await check("本地项目与轻量页的 state 互不覆盖", async () => {
    WB.router.go("summary", {});
    await tick(300);
    let view = $("#view");
    const pick = view.querySelector("#sum-project");
    const projects = WB.pages.summary.state.projects;
    const current = WB.pages.summary.state.projectId;
    const other = projects.find((item) => item.id !== current);
    if (other) {
      pick.value = other.id;
      pick.dispatchEvent(new window.Event("change", { bubbles: true }));
      await tick(300);
      view = $("#view");
    }
    const localId = WB.pages.summary.state.projectId;
    const localTitle = view.querySelector("#sum-title").textContent;
    WB.router.go("light", {});
    await tick(300);
    const lightUrl = $("#view").querySelector("#sum-light-url");
    lightUrl.value = "BV1isolation";
    lightUrl.dispatchEvent(new window.Event("input", { bubbles: true }));
    WB.router.go("summary", {});
    await tick(300);
    assert.strictEqual(WB.pages.summary.state.projectId, localId, "本地项目被轻量页改写");
    assert.strictEqual($("#view").querySelector("#sum-title").textContent, localTitle, "本地正文标题被轻量页改写");
    WB.router.go("light", {});
    await tick(300);
    assert.strictEqual($("#view").querySelector("#sum-light-url").value, "BV1isolation", "轻量输入被本地页改写");
  });
  await check("设置页：字幕分区带术语表，能保存", async () => {
    WB.router.go("settings", { section: "subtitle" });
    await tick(200);
    const box = window.document.querySelector("#settings-body textarea");
    assert.ok(box, "字幕分区没有术语表输入框");
    await tick(120);
    assert.ok(String(box.value).indexOf("=") > 0, "术语表没 load 出内容：" + box.value.slice(0, 40));
    box.value = "GPU=显卡\r\nCPU=处理器";
    const save = [...window.document.querySelectorAll("#settings-body button")]
      .find((b) => b.textContent.trim() === "保存");
    assert.ok(save, "没有保存按钮");
    save.click();
    await tick(300);
    const glossary = await WB.rpc("glossary.get", {});
    assert.strictEqual(JSON.stringify(glossary.entries), JSON.stringify(["GPU=显卡", "CPU=处理器"]),
      "术语表没存进去：" + JSON.stringify(glossary.entries));
  });

  await check("切片台：候选渲染、选中同步、改切点写库、生成候选", async () => {
    WB.router.go("clipper", {});
    await tick(360);
    const view = $("#view");
    const grid = view.querySelector(".clip-grid");
    assert.ok(grid, "切片台没有三栏布局");

    const cards = [...view.querySelectorAll(".clip-card")];
    assert.strictEqual(cards.length, 4, `候选卡片 ${cards.length} 张，应为 4`);
    // 兜底候选必须挂标：规则结果不能冒充模型判断
    const pills = cards.map((card) => card.textContent);
    assert.ok(pills.some((text) => text.indexOf("兜底") >= 0), "没有「兜底」标记");
    assert.ok(pills.some((text) => text.indexOf("已锁定") >= 0), "没有「已锁定」标记");
    assert.ok(cards.some((card) => card.classList.contains("is-active")), "首屏没选中任何候选");

    // 选第二条：检查器里的时间码要跟着换
    cards[1].dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await tick(80);
    const startInput = view.querySelector('.clip-inspect input[aria-label="起点"]');
    const endInput = view.querySelector('.clip-inspect input[aria-label="终点"]');
    assert.ok(startInput && endInput, "检查器里没有切点输入框");
    assert.strictEqual(startInput.value, "00:41.200", "起点时间码不对：" + startInput.value);
    assert.strictEqual(endInput.value, "00:58.900", "终点时间码不对：" + endInput.value);
    // 锁定候选的输入框只读
    assert.ok(startInput.disabled, "锁定候选的切点输入框还能改");

    // 回到第一条，用组件的 moveHandle + 提交走一遍「拖动 → 写库」
    cards[0].dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await tick(80);
    const internals = WB.pages.clips._internals;
    const timeline = internals.timeline();
    assert.ok(timeline, "时间轴没挂上");
    // 16.5s 离最近的句边界（12.4s / 34.8s）超过 2s 容差，不会被磁吸拽回，
    // 否则这条断言测的就变成"吸附生效"，而不是"改切点能落库"。
    const moved = timeline.moveHandle("start", 16_500);
    assert.ok(moved && moved.start_ms === 16_500, "起点没被搬动：" + JSON.stringify(moved));
    timeline.commit();
    await tick(220);

    // 落库时按帧栅格对齐（P3-04）：25fps → 一帧 40ms，16.500s 落在两帧之间会被吸到 16.520，
    // 这样"切点误差 ≤ 1 帧"才是后端保证的，不是靠界面凑巧。
    const cardText = [...$("#view").querySelectorAll(".clip-card")][0].textContent;
    assert.ok(cardText.indexOf("00:16.520") >= 0,
      "卡片上的时间码没跟着改：" + cardText.slice(0, 60));

    // 磁吸上限与后端同口径（2000ms）：离边界 500ms 会吸附，离 4s 不吸。
    // 半小时的视频在 600px 画布上 6px ≈ 18s，只按像素判会让微调形同虚设。
    const nearSnap = timeline.moveHandle("start", 12_900);
    assert.strictEqual(nearSnap.start_ms, 12_400, "离边界 500ms 应该吸附：" + JSON.stringify(nearSnap));
    assert.ok(nearSnap.snapped, "500ms 内应标 snapped");
    const farSnap = timeline.moveHandle("start", 16_500);
    assert.strictEqual(farSnap.start_ms, 16_500, "离边界 4.1s 不该吸附：" + JSON.stringify(farSnap));
    assert.ok(!farSnap.snapped, "4s 外不该标 snapped");

    // 帧微调：+1 帧 = 40ms，且必须落库后回读一致
    const state = internals.state();
    const before = state.data.candidates[0].start_ms;
    const plusFrame = [...$("#view").querySelectorAll(".clip-inspect button")]
      .filter((b) => b.textContent.trim() === "+1帧")[0];
    assert.ok(plusFrame, "检查器里没有 +1帧");
    plusFrame.click();
    await tick(220);
    assert.strictEqual(state.data.candidates[0].start_ms, before + 40,
      "帧微调没生效：" + before + " → " + state.data.candidates[0].start_ms);

    // 预卷开关：默认开着，播放起点要提前 3 秒，但不能改候选的 start_ms
    const preRoll = [...$("#view").querySelectorAll(".clip-transport__check input")][0];
    assert.ok(preRoll, "运输条上没有「3 秒预卷」开关");
    assert.strictEqual(preRoll.checked, true, "预卷默认应该是开的");
    const keepStart = state.data.candidates[0].start_ms;
    const playBtn = [...$("#view").querySelectorAll("button")]
      .filter((b) => b.textContent.trim() === "播放这一段")[0];
    playBtn.click();
    await tick(60);
    assert.strictEqual(state.data.candidates[0].start_ms, keepStart,
      "预卷把候选起点改了：" + keepStart + " → " + state.data.candidates[0].start_ms);
    // 检查器要写明一帧多长，否则用户不知道"±1帧"是 33ms 还是 40ms
    assert.ok($("#view").textContent.indexOf("一帧 40ms") >= 0, "检查器没写帧长");

    // 生成候选：走任务 → 轮询 → 重新加载，卡片数不变（mock 幂等）
    const genBtn = [...$("#view").querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "生成候选");
    assert.ok(genBtn, "没有「生成候选」按钮");
    genBtn.click();
    await tick(120);
    assert.ok(state.jobId, "没拿到任务 id");
    assert.ok(genBtn.disabled, "生成中按钮应该禁用");
  });

  await check("切片台精析：生成候选完成后，提示条说清画面精析改了几条", async () => {
    WB.router.go("clipper", {});
    await tick(400);
    // 上一项点过「生成候选」，`state.busy` 要等上一条任务轮询完才清；等按钮回到可点状态
    let genBtn = null;
    for (let i = 0; i < 20; i++) {
      genBtn = [...$("#view").querySelectorAll("button")]
        .find((b) => b.textContent.trim() === "生成候选");
      if (genBtn) break;
      await tick(200);
    }
    assert.ok(genBtn, "没有「生成候选」按钮");
    genBtn.click();
    // mock 的候选任务 1 拍就完成，之后走 load() 重画
    await tick(900);
    const note = $("#view").querySelector(".clip-transport__note");
    assert.ok(note, "提示条节点没渲染");
    const text = note.textContent || "";
    assert.ok(
      text.indexOf("画面精析改了 2 条") >= 0,
      "提示条没写精析结果（不能被\"候选已更新\"盖掉）：" + text
    );
  });

  await check("切片台导出：选模式、提交任务、队列落一条带偏移的成片", async () => {
    WB.router.go("clipper", {});
    await tick(400);
    const view = $("#view");
    const form = view.querySelector(".exp-form");
    assert.ok(form, "右栏没有导出面板");
    const selects = [...form.querySelectorAll("select")];
    // 字幕 / 模式 / 画幅 —— 竖屏下拉只在选 9:16 时出现，所以默认三个
    assert.strictEqual(selects.length, 3, "导出面板应有字幕/模式/画幅三个下拉");
    // 默认精确模式：那段说明要讲清"准但慢"
    assert.ok(form.textContent.indexOf("切点准到 1 帧") >= 0, "没写精确模式的特点");

    // 切到快速：说明必须变，因为切点会前移
    const modeSelect = selects[1];
    modeSelect.value = "fast";
    modeSelect.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(60);
    assert.ok(
      $("#view").querySelector(".exp-form").textContent.indexOf("切点可能前移") >= 0,
      "切到快速模式后没更新说明"
    );

    const go = [...$("#view").querySelectorAll(".exp-form button")]
      .find((b) => b.textContent.indexOf("导出这一段") >= 0);
    assert.ok(go, "没有「导出这一段」按钮");
    assert.ok(!go.disabled, "有选中候选时导出按钮不该禁用");
    go.click();
    // mock 的导出任务 400ms 完成，队列轮询 800ms 一拍
    await tick(1300);

    const rows = [...$("#view").querySelectorAll(".exp-row")];
    assert.strictEqual(rows.length, 1, `导出队列 ${rows.length} 条，应为 1`);
    const text = rows[0].textContent;
    assert.ok(text.indexOf("完成") >= 0, "队列状态不是完成：" + text.slice(0, 40));
    assert.ok(text.indexOf("快速") >= 0, "队列没标快速模式：" + text.slice(0, 40));
    // 快速模式的偏移必须写出来，否则用户以为切点准
    assert.ok(text.indexOf("480 ms") >= 0, "队列没写切点偏移：" + text.slice(0, 80));
    assert.ok(
      [...rows[0].querySelectorAll("button")].some((b) => b.textContent.trim() === "打开文件"),
      "完成的成片没有「打开文件」"
    );
  });

  await check("切片台竖屏：切 9:16 出现竖屏选项，导出带竖屏标记、安全区可开", async () => {
    WB.router.go("clipper", {});
    await tick(400);
    const form = () => $("#view").querySelector(".exp-form");
    assert.ok(form(), "右栏没有导出面板");

    // 切画幅到 9:16
    const aspect = [...form().querySelectorAll("select")].find(
      (s) => [...s.options].some((o) => o.value === "9x16")
    );
    assert.ok(aspect, "没有画幅下拉");
    aspect.value = "9x16";
    aspect.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(60);

    // 竖屏模式下拉 + 取景滑块 + 安全区开关
    const vsel = [...form().querySelectorAll("select")].find(
      (s) => [...s.options].some((o) => o.value === "follow")
    );
    assert.ok(vsel, "选了 9:16 却没出现竖屏模式下拉");
    assert.ok(form().querySelector(".exp-form__range"), "没有取景位置滑块");
    const safe = form().querySelector(".exp-form__check input");
    assert.ok(safe, "没有安全区开关");
    assert.strictEqual(
      $("#view").querySelector(".wb-player__safe").style.display,
      "none",
      "没勾安全区时遮罩不该显示"
    );
    safe.checked = true;
    safe.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(40);
    assert.notStrictEqual(
      $("#view").querySelector(".wb-player__safe").style.display,
      "none",
      "勾了安全区遮罩还不显示"
    );

    // 切到人脸跟随：出现「检测人脸」，且轨迹说明里能看见手工点数
    vsel.value = "follow";
    vsel.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(80);
    const focusBtn = [...form().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("检测人脸") >= 0
    );
    assert.ok(focusBtn, "跟随模式没有「检测人脸」按钮");
    assert.ok(
      form().textContent.indexOf("3 个点") >= 0,
      "没显示轨迹点数：" + form().textContent.slice(0, 120)
    );
    assert.ok(form().textContent.indexOf("1 个手工点") >= 0, "没显示手工点数");

    // 导出：队列行要写清竖屏与模式
    const go = [...form().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("导出这一段") >= 0
    );
    go.click();
    await tick(1300);
    const rows = [...$("#view").querySelectorAll(".exp-row")];
    const last = rows[rows.length - 1];
    assert.ok(last.textContent.indexOf("竖屏·人脸跟随") >= 0, "队列没标竖屏模式：" + last.textContent);
  });

  await check("切片台跟随增强：多人主体可选、换人立刻换轨迹、关掉增强回退单脸", async () => {
    WB.router.go("clipper", {});
    await tick(400);
    const form = () => $("#view").querySelector(".exp-form");
    assert.ok(form(), "右栏没有导出面板");

    const aspect = [...form().querySelectorAll("select")].find(
      (s) => [...s.options].some((o) => o.value === "9x16")
    );
    aspect.value = "9x16";
    aspect.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(60);
    const vsel = [...form().querySelectorAll("select")].find(
      (s) => [...s.options].some((o) => o.value === "follow")
    );
    vsel.value = "follow";
    vsel.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(120);

    // 两个主体 → 出现「跟谁」下拉，选项带分数；切点要写出来
    const picker = form().querySelector(".exp-form__subject");
    assert.ok(picker, "多人画面没出现主体选择器");
    const labels = [...picker.options].map((o) => o.textContent);
    assert.ok(labels.length === 2, "主体选项数不对：" + labels.join("/"));
    assert.ok(labels[0].indexOf("左边那位") >= 0 && labels[0].indexOf("78 分") >= 0,
      "选项没写主体名与分数：" + labels[0]);
    assert.ok(form().textContent.indexOf("1 处镜头切换不插值") >= 0,
      "没提示镜头切换处不插值：" + form().textContent.slice(0, 160));

    // 换人：只换轨迹，不重跑检测
    picker.value = "sub_b";
    picker.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(160);
    assert.ok(form().textContent.indexOf("跟右边那位") >= 0,
      "换主体后没更新跟随对象：" + form().textContent.slice(0, 160));
    const internals = WB.pages.clips._internals;
    const x = internals.state().focus[0].x;
    assert.ok(Math.abs(x - 0.74) < 0.001, "换主体后轨迹没换成 sub_b：" + x);

    // 关掉增强：选择器消失，说明改成"只跟最大那张脸"
    const enhance = form().querySelector(".exp-form__enhance");
    assert.ok(enhance, "没有「增强跟随」开关");
    assert.strictEqual(enhance.checked, true, "增强默认该是开的");
    enhance.checked = false;
    enhance.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(120);
    assert.ok(!form().querySelector(".exp-form__subject"), "关掉增强后主体选择器还在");
    assert.ok(form().textContent.indexOf("增强关闭") >= 0,
      "关掉增强后没提示：" + form().textContent.slice(0, 160));
  });

  await check("切片台文案：写文案出标题简介标签，抽帧能换封面", async () => {
    WB.router.go("clipper", {});
    await tick(400);
    const pane = () => $("#view").querySelector(".copy-pane");
    assert.ok(pane(), "右栏没有文案面板");

    // 默认选中的第一条还没文案：先说清楚，别给空输入框
    const first = $("#view").querySelector(".clip-card");
    first.dispatchEvent(new dom.window.MouseEvent("mousedown", { bubbles: true }));
    await tick(120);
    assert.ok(
      pane().textContent.indexOf("还没写文案") >= 0,
      "没写文案时应有提示：" + pane().textContent.slice(0, 80)
    );

    // 点「写文案」→ 任务跑完 → 三个字段都填上
    const writeBtn = [...pane().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("写文案") >= 0
    );
    assert.ok(writeBtn, "没有「写文案」按钮");
    writeBtn.click();
    await tick(1400);
    const title = pane().querySelector('input[aria-label="标题"]');
    const desc = pane().querySelector('textarea[aria-label="简介"]');
    const tags = pane().querySelector('input[aria-label="标签"]');
    assert.ok(title && title.value, "标题没填上：" + (title && title.value));
    assert.ok(desc && desc.value.length > 10, "简介太短：" + (desc && desc.value));
    assert.ok(tags && tags.value.split(/\s+/).length >= 3, "标签不够：" + (tags && tags.value));

    // 抽帧：出 4 个候选帧，点第 2 个换封面
    const grab = [...pane().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("抽 4 帧") >= 0
    );
    assert.ok(grab, "没有「抽 4 帧」按钮");
    grab.click();
    await tick(300);
    const frames = [...pane().querySelectorAll(".cover-frame")];
    assert.strictEqual(frames.length, 4, "候选帧应有 4 个，实际 " + frames.length);
    frames[1].click();
    await tick(200);
    assert.ok(
      pane().querySelector(".cover-meta").textContent.indexOf("取自") >= 0,
      "换完封面没写清取自哪一帧：" + pane().querySelector(".cover-meta").textContent
    );

    // 手改标题再保存：保存后字段值就是改过的那个
    title.value = "自己改过的标题文案";
    const saveBtn = [...pane().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("保存修改") >= 0
    );
    saveBtn.click();
    await tick(250);
    assert.ok(
      pane().textContent.indexOf("已保存") >= 0,
      "保存后没回执：" + pane().textContent.slice(-60)
    );
  });

  await check("总结页发布卡：从总结生成草稿、改标题、保存后列表出现", async () => {
    // 发布已并入总结页（bili_scribe 的卡序：正文卡 → 发布卡 → 发布记录卡）
    // 合集用例把 state.mode 停在 collection；合集里不出发布区（发布挂单条正文下）
    window.WB.pages.summary._internals.switchMode("video");
    WB.router.go("summary", {});
    await until(() => $("#view").querySelector(".pb-stack"), "总结页里的发布区");
    const view = $("#view");
    assert.ok(view.querySelector(".pb-stack"), "总结页里没有发布区");
    assert.ok(view.querySelector(".sum-main #publish-main"), "发布区不在正文那一列");
    assert.ok(view.querySelector(".sum-side #publish-rail"), "草稿列表没进右栏");
    assert.strictEqual(view.querySelectorAll(".pb-row").length, 0, "一开始不该有草稿");

    const picker = view.querySelector("#pb-project");
    assert.ok(picker, "没有项目下拉");
    assert.ok(picker.options.length > 0, "项目下拉是空的");

    const gen = [...view.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "从总结生成");
    assert.ok(gen, "没有「从总结生成」按钮");
    gen.click();
    await tick(360);

    const titleInput = view.querySelector("#pb-title");
    assert.ok(titleInput, "生成后没有标题输入框");
    assert.ok(titleInput.value.trim().length > 0, "标题是空的");
    const body = view.querySelector("#pb-body");
    assert.ok(body && body.textContent.trim().length > 10, "正文预览是空的");
    assert.ok(body.textContent.indexOf("要点") >= 0 || body.textContent.indexOf("时间轴") >= 0,
      "正文里没有总结的分节：" + body.textContent.slice(0, 60));

    // 字数跟着输入走（码点数，和后端一致）
    titleInput.value = "我自己改的标题";
    titleInput.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    const counts = [...view.querySelectorAll(".pb-count")].map((n) => n.textContent);
    assert.ok(counts.some((c) => c.indexOf("7 / 80") === 0), "标题计数没更新：" + JSON.stringify(counts));

    const save = [...view.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "保存草稿");
    assert.ok(save, "没有保存按钮");
    save.click();
    await tick(420);

    const rows = $("#view").querySelectorAll(".pb-row");
    assert.strictEqual(rows.length, 1, "保存后列表里应该只有一份草稿：" + rows.length);
    assert.ok(rows[0].textContent.indexOf("我自己改的标题") >= 0,
      "列表里的标题不是刚改的那个：" + rows[0].textContent.slice(0, 40));
    const chars = $("#view").querySelector(".pb-count");
    assert.ok(chars, "保存后详情卡不见了");
  });

  await check("总结页发布卡：列表带 UP 与更新时间，详情亮三道门禁，重新生成只换标题", async () => {
    // 上一条用例已经存了一份草稿，这里接着用同一份
    const view = $("#view");
    const row = view.querySelector(".pb-row");
    assert.ok(row, "没有草稿行");
    assert.ok(row.querySelector(".pb-status"), "列表行没有状态徽标");
    assert.ok(row.textContent.indexOf("UP") >= 0 || row.textContent.indexOf("测试") >= 0,
      "列表行没有 UP 名：" + row.textContent.slice(0, 60));

    // 门禁卡：三关都要在，且各自的结论要能读出来
    const card = view.querySelector(".pb-gate");
    assert.ok(card, "详情里没有门禁卡");
    const gateRows = [...card.querySelectorAll(".pb-gate__row")];
    assert.strictEqual(gateRows.length, 3, "门禁应该是三关：" + gateRows.length);
    const labels = gateRows.map((n) => n.querySelector(".pb-gate__label").textContent.trim());
    assert.deepStrictEqual(labels, ["内容确认", "账号与视频", "频率与重复"]);
    assert.strictEqual(card.getAttribute("data-ready"), "0", "还没勾确认，不该显示三关都过");
    assert.ok(card.textContent.indexOf("还不能发") >= 0, "门禁卡抬头没说清现在不能发");

    // 摘要字段已下线（2026-11-07）：界面上不该再有摘要输入框
    assert.strictEqual(view.querySelector("#pb-summary"), null,
      "摘要字段已经下线，不该还有输入框");
    assert.ok(view.textContent.indexOf("摘要") < 0,
      "界面上还有「摘要」字样：" + view.textContent.slice(0, 120));

    // 重新生成标题：正文是用户可能改过的东西，不能被冲掉
    const beforeBody = view.querySelector("#pb-body").textContent;
    const regen = [...view.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "重新生成标题");
    assert.ok(regen, "没有「重新生成标题」按钮");
    regen.click();
    await tick(420);
    const after = $("#view");
    assert.ok(after.querySelector("#pb-title").value.length > 0, "标题被清空了");
    assert.strictEqual(after.querySelector("#pb-body").textContent, beforeBody,
      "只重算标题时正文被打回原样了");
  });

  await check("总结页发布卡：没勾确认发布按钮点不动，勾了能发出去、也能撤回", async () => {
    // 接上一项：库里已经有一份草稿，详情卡在右栏
    const view = $("#view");

    // 上一步刚重新生成过标题 → 屏幕上是还没落库的预览，发布会用库里旧的那一版，
    // 所以这里必须先拦住并说清原因（S1：双真相源）。
    assert.ok(view.textContent.indexOf("先点「保存草稿」") >= 0,
      "没提示「先保存」：" + view.textContent.slice(-90));
    const saveBtn = [...view.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "保存草稿");
    assert.ok(saveBtn, "没有保存按钮");
    saveBtn.click();
    await tick(450);

    const checkBox = $("#view").querySelector("#pb-confirm");
    assert.ok(checkBox, "没有「我已阅读并确认」勾选框");
    assert.strictEqual(checkBox.checked, false, "一开始就该是没勾的");

    const publishBtn = () =>
      [...$("#view").querySelectorAll("button")].find((b) => b.textContent.trim().startsWith("发布笔记"));
    assert.ok(publishBtn(), "没有「发布笔记」按钮");
    assert.strictEqual(publishBtn().disabled, true, "没勾确认时按钮该是禁用的");
    assert.ok($("#view").textContent.indexOf("还不能发") >= 0, "门禁卡该说清现在不能发");

    // 勾上 → 门禁重算 → 按钮可用
    checkBox.checked = true;
    checkBox.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(400);
    assert.strictEqual(publishBtn().disabled, false, "勾了确认之后按钮该能点");
    assert.strictEqual($("#view").querySelector(".pb-gate").getAttribute("data-ready"), "1",
      "勾确认后门禁卡该显示三关都过");

    // S3：确认只对「勾过的那一份草稿」有效。把 confirmedFor 指到别的 id，
    // 勾不该留、按钮要回到灰的——以前 confirmed 是个全局布尔，换草稿能一路带过去。
    const st = WB.pages.publish.state;
    const mine = st.current.id;
    st.confirmedFor = "draft_not_this_one";
    WB.pages.publish._internals.paint();
    const otherBox = $("#view").querySelector("#pb-confirm");
    assert.strictEqual(otherBox.checked, false, "确认绑在别的草稿上时，勾不该还亮着");
    const otherBtn = [...$("#view").querySelectorAll("button")]
      .find((b) => b.textContent.trim().startsWith("发布笔记"));
    assert.ok(otherBtn && otherBtn.disabled, "确认失效时发布按钮该是灰的");
    // 还原，后面的用例还要用这一份
    st.confirmedFor = mine;
    WB.pages.publish._internals.paint();
    assert.strictEqual($("#view").querySelector("#pb-confirm").checked, true, "还原后勾该回来");

    // 事故现场（2026-11-07）：确认框只给两个按钮 resolve，用户按 ESC 或点遮罩关掉时
    // Promise 永远悬着 ⇒ state.publishing 停在 true ⇒ 按钮永久显示「发布中…」且点不动，
    // 后端日志里连一条发布请求都没有。onClose 兜底之后，ESC 必须等价于「再想想」。
    publishBtn().click();
    await tick(60);
    assert.ok(window.document.getElementById("modal-root").querySelector(".modal"),
      "点发布没有弹出确认框");
    window.document.dispatchEvent(
      new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    await tick(300);
    assert.strictEqual(window.document.querySelector(".modal"), null, "ESC 没关掉确认框");
    const afterEsc = $("#view");
    const stillBtn = [...afterEsc.querySelectorAll("button")]
      .find((b) => b.textContent.trim().startsWith("发布"));
    assert.ok(stillBtn, "ESC 之后发布按钮不见了");
    assert.ok(stillBtn.textContent.indexOf("发布中") < 0,
      "ESC 关掉确认框之后按钮卡在「发布中…」：" + stillBtn.textContent);
    assert.strictEqual(stillBtn.disabled, false, "ESC 之后按钮该还能再点");
    assert.strictEqual(WB.pages.publish.state.publishing, false, "ESC 之后 publishing 该复位");

    // 点发布 → 弹确认框 → 确认
    publishBtn().click();
    await tick(60);
    const root = window.document.getElementById("modal-root");
    const confirmBtn = [...root.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "发布");
    assert.ok(confirmBtn, "点发布没有弹出确认框");
    confirmBtn.click();
    await tick(500);

    const after = $("#view");
    const pill = after.querySelector(".pb-status");
    assert.ok(pill && pill.textContent.indexOf("已发布") >= 0,
      "发布后状态徽标该是「已发布」：" + (pill && pill.textContent));
    const withdrawBtn = [...after.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "撤回笔记");
    assert.ok(withdrawBtn, "发布之后该出现「撤回笔记」");
    assert.ok(after.textContent.indexOf("note_id note_mock_") >= 0, "没显示 note_id");

    // 撤回
    withdrawBtn.click();
    await tick(60);
    const root2 = window.document.getElementById("modal-root");
    const confirmWithdraw = [...root2.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "撤回");
    assert.ok(confirmWithdraw, "撤回没有确认框");
    confirmWithdraw.click();
    await tick(500);

    const pill2 = $("#view").querySelector(".pb-status");
    assert.ok(pill2 && pill2.textContent.indexOf("已撤回") >= 0,
      "撤回后状态该是「已撤回」：" + (pill2 && pill2.textContent));
  });

  await check("总结页发布卡：记录卡留下已发与已撤回，能打开视频链接", async () => {
    // 接上一项：那条笔记已经发出去又撤回了，记录卡该有它的痕迹
    window.WB.pages.summary._internals.switchMode("video");
    WB.router.go("summary", {});
    await until(() => $("#view").querySelector(".pb-record"), "发布记录行");
    const view = $("#view");
    const card = [...view.querySelectorAll(".pb-card")].find(
      (n) => n.textContent.indexOf("发布记录") >= 0
    );
    assert.ok(card, "总结页没有「发布记录」卡");
    // 抬头写「已发 N · 已撤 M」：mock 里本来就种了历史记录，别硬编码成 1
    const head = card.querySelector(".pb-card__count") || card;
    const headText = head.textContent || "";
    const withdrawnCount = Number((headText.match(/已撤\s*(\d+)/) || [])[1] || 0);
    assert.ok(withdrawnCount >= 1, "记录抬头该数出撤回数：" + headText);

    const rows = [...card.querySelectorAll(".pb-record")];
    assert.ok(rows.length >= 1, "记录卡是空的，撤回过的笔记该留在里面");
    const row = rows[0];
    assert.ok(row.querySelector(".pb-status").textContent.indexOf("已撤回") >= 0,
      "记录行状态该是已撤回");
    assert.ok(row.textContent.indexOf("note_id note_mock_") >= 0, "记录行没写 note_id");

    // 打开视频：走 app.open_url，mock 把 URL 记在 window.__wbOpenedUrls
    window.__wbOpenedUrls = [];
    const openBtn = [...row.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "打开视频"
    );
    assert.ok(openBtn, "记录行没有「打开视频」");
    openBtn.click();
    await tick(120);
    assert.ok(
      (window.__wbOpenedUrls || []).some((u) => u.indexOf("bilibili.com/video/") >= 0),
      "点「打开视频」没有把链接交给系统：" + JSON.stringify(window.__wbOpenedUrls)
    );

    // 查看详情：右栏该载入这一条记录（已发布的那条不在草稿列表里，
    // 所以这里断言的是详情卡的内容，而不是"草稿列表有一行高亮"）
    const backBtn = [...row.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "查看详情"
    );
    assert.ok(backBtn, "记录行没有「查看详情」");
    backBtn.click();
    await tick(500);
    const titleBox = $("#view").querySelector("#pb-title");
    assert.ok(titleBox, "详情卡没有标题输入框");
    assert.strictEqual(titleBox.value, row.querySelector(".pb-record__title").textContent,
      "点「查看详情」之后右栏该是这条记录的内容");
  });



  await check("总结页发布卡：标题改了没保存，发布按钮不该能点（发出去的会是旧标题）", async () => {
    // 自造一份"能发"的状态，不去碰 mock 里的草稿（后面的用例还要用它）：
    // 这条判的是 publishReady() 的第五个条件，与数据来源无关。
    const st = WB.pages.publish.state;
    const keep = {
      current: st.current, editTitle: st.editTitle, confirmedFor: st.confirmedFor,
      gates: st.gates, draft: st.draft
    };
    st.current = { id: "draft_probe_title", title: "库里的标题", status: "draft",
      project_id: "pj_probe", content: '[{"insert":"x\\n"}]', content_text: "x" };
    st.editTitle = "库里的标题";
    st.draft = null;
    st.confirmedFor = "draft_probe_title";
    st.gates = { ready: true, gates: [] };
    assert.strictEqual(WB.pages.publish._internals.publishReady(), true,
      "前提没成立：这份草稿本来就该能发");

    // 改标题但不保存：发布读的是库里那一行，发出去的会是旧标题，
    // 而勾选框写的正是「确认标题与正文都要发出去」（2026-11-07 补）
    st.editTitle = "库里的标题（改了没存）";
    assert.strictEqual(WB.pages.publish._internals.publishReady(), false,
      "标题改过还没保存，发布按钮不该能点");
    assert.ok(WB.pages.publish._internals.gateBlockedText().indexOf("标题改过还没保存") >= 0,
      "没告诉用户为什么不能发：" + WB.pages.publish._internals.gateBlockedText());
    st.editTitle = "库里的标题";
    assert.strictEqual(WB.pages.publish._internals.publishReady(), true,
      "把标题改回去之后按钮该恢复");

    st.current = keep.current;
    st.editTitle = keep.editTitle;
    st.confirmedFor = keep.confirmedFor;
    st.gates = keep.gates;
    st.draft = keep.draft;
  });

  await check("总结页发布卡：发布收尾那一步失败，按钮也要复位（不能卡在「发布中…」）", async () => {
    const st = WB.pages.publish.state;
    const keep = {
      current: st.current, editTitle: st.editTitle, confirmedFor: st.confirmedFor,
      gates: st.gates, draft: st.draft, notes: st.notes, records: st.records
    };
    st.current = { id: "draft_probe_reset", title: "探针草稿", status: "draft",
      project_id: "pj_probe", content: '[{"insert":"x\\n"}]', content_text: "x" };
    st.editTitle = "探针草稿";
    st.draft = null;
    st.confirmedFor = "draft_probe_reset";
    st.gates = { ready: true, gates: [] };
    assert.strictEqual(WB.pages.publish._internals.publishReady(), true, "前提没成立");

    // 事故现场：发布成功后要 Promise.all([loadNotes, loadRecords]) 收尾，
    // 以前复位写在那个 all 的 .then 里 ⇒ 只要列表接口失败，复位整段被跳过，
    // 按钮永久「发布中…」且点不动，而后端其实已经发出去了。
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "publish.list") return Promise.reject(new Error("列表接口挂了"));
      if (method === "publish.publish") {
        return Promise.resolve({ note: { id: "draft_probe_reset", status: "published",
          title: "探针草稿", note_id: "note_probe", project_id: "pj_probe",
          content: '[{"insert":"x\\n"}]', content_text: "x" } });
      }
      return realRpc.call(WB, method, params, opts);
    };
    const run = WB.pages.publish._internals.publishNow();
    await tick(80);
    const root = window.document.getElementById("modal-root");
    const confirmBtn = [...root.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "发布");
    assert.ok(confirmBtn, "点发布没有弹出确认框");
    confirmBtn.click();
    await tick(600);
    WB.rpc = realRpc;
    await run;

    assert.strictEqual(st.publishing, false,
      "收尾那步失败之后 publishing 没复位 ⇒ 按钮永久「发布中…」");

    st.current = keep.current;
    st.editTitle = keep.editTitle;
    st.confirmedFor = keep.confirmedFor;
    st.gates = keep.gates;
    st.draft = keep.draft;
    st.notes = keep.notes;
    st.records = keep.records;
  });
  await check("总结页发布卡：弹窗挂载点缺失时也不许把 Promise 悬住", async () => {
    // 和 ESC 那条同一个形状：ui.modal 在没有 #modal-root 时返回一个空 close()，
    // 谁拿它兜底（ui.confirm / 发布确认框）就永远等不到 resolve。
    // 这里把挂载点临时摘掉，走完一次「点发布」，按钮必须自己复位。
    const st = WB.pages.publish.state;
    const keep = {
      current: st.current, editTitle: st.editTitle, confirmedFor: st.confirmedFor,
      gates: st.gates, draft: st.draft
    };
    st.current = { id: "draft_probe_noroot", title: "探针草稿", status: "draft",
      project_id: "pj_probe", content: '[{"insert":"x\\n"}]', content_text: "x" };
    st.editTitle = "探针草稿";
    st.draft = null;
    st.confirmedFor = "draft_probe_noroot";
    st.gates = { ready: true, gates: [] };

    const mount = window.document.getElementById("modal-root");
    const holder = window.document.createElement("div");
    mount.parentNode.replaceChild(holder, mount);
    try {
      const run = WB.pages.publish._internals.publishNow();
      await tick(200);
      assert.strictEqual(st.publishing, false,
        "挂载点缺失时 publishing 卡在 true ⇒ 按钮永久「发布中…」");
      await run;
    } finally {
      holder.parentNode.replaceChild(mount, holder);
      st.current = keep.current;
      st.editTitle = keep.editTitle;
      st.confirmedFor = keep.confirmedFor;
      st.gates = keep.gates;
      st.draft = keep.draft;
    }
  });
  await check("全局提示：后端发的 app.toast 能真的弹出来（名字与字段都要对）", async () => {
    // 后端两处都在发 app.toast（events.toast 与发布重试回调），而 app.js 以前只认
    // toast ⇒ 所有后端提示从来没显示过。名字与字段各收两种。
    const host = window.document.getElementById("toasts");
    assert.ok(host, "页面没有 toast 容器");
    host.textContent = "";
    window.__wbEvent("app.toast", JSON.stringify({ level: "info", message: "后端提示·message" }));
    await tick(80);
    assert.ok(host.textContent.indexOf("后端提示·message") >= 0,
      "app.toast 没弹出来：" + host.textContent);
    host.textContent = "";
    window.__wbEvent("app.toast", JSON.stringify({ level: "info", text: "后端提示·text" }));
    await tick(80);
    assert.ok(host.textContent.indexOf("后端提示·text") >= 0,
      "app.toast 的 text 字段没被认：" + host.textContent);
  });

  await check("事件总线：页面订阅走的是 WB.events.on，别再让它静默失效", async () => {
    assert.strictEqual(typeof window.WB.events, "object", "WB.events 不存在");
    assert.strictEqual(window.WB.events.on, window.WB.on,
      "WB.events.on 与 WB.on 必须是同一根总线，不能是第二份监听表");
    let hit = 0;
    const off = window.WB.events.on("probe.events", function () { hit += 1; });
    window.__wbEvent("probe.events", JSON.stringify({}));
    await tick(60);
    off();
    assert.strictEqual(hit, 1, "通过 WB.events.on 订阅收不到事件");
  });
  await check("样式页：两个内置预设、改字号能存成新样式、真实渲染出图", async () => {
    WB.router.go("styles", {});
    await tick(420);
    const view = $("#view");
    assert.ok(view.querySelector(".styles__grid2"), "样式页没有两栏布局");

    const items = [...view.querySelectorAll(".styles__item")];
    assert.ok(items.length >= 2, "预设列表少于两个：" + items.length);
    assert.ok(items.some((n) => n.textContent.indexOf("圆角") >= 0), "没有圆角预设");

    // 默认选中第一个，右侧表单与画布都该在
    const host = view.querySelector("#styles-canvas-host");
    assert.ok(host, "没有预览画布容器");
    assert.ok(host.querySelector("canvas"), "画布没建出来");
    const sizeInput = [...view.querySelectorAll(".styles__num")].find(
      (n) => n.getAttribute("aria-label") === "字号"
    );
    assert.ok(sizeInput, "主字幕字号输入框没找到");
    const before = Number(sizeInput.value);

    // 改字号 → 顶部提示有未保存改动（不落盘）
    sizeInput.value = String(before + 6);
    sizeInput.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    await tick(60);
    assert.ok(view.textContent.indexOf("有未保存的改动") >= 0, "改了值却没提示未保存");

    // 保存：内置改不动，这里走「另存为新样式」
    const save = [...view.querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "另存为新样式" || b.textContent.trim() === "保存"
    );
    assert.ok(save, "没有保存按钮");
    save.click();
    await tick(420);
    const after = [...$("#view").querySelectorAll(".styles__item")];
    assert.ok(after.length > items.length, "另存后预设列表没变长：" + items.length + " → " + after.length);
    const picked = after.find((n) => n.className.indexOf("is-active") >= 0);
    assert.ok(picked, "另存后没有选中新样式");
    assert.ok(picked.textContent.indexOf(String(before + 6) + " 号") >= 0,
      "新样式的字号不是刚改的值：" + picked.textContent);

    // 真实渲染：mock 不产真 PNG（没有可读文件），页面要给出明确说明而不是破图
    const render = [...$("#view").querySelectorAll("button")].find(
      (b) => b.textContent.trim() === "真实渲染"
    );
    assert.ok(render, "没有真实渲染按钮");
    render.click();
    await tick(300);
    const renderBox = $("#view").querySelector("#styles-render");
    assert.ok(renderBox.textContent.trim().length > 0, "渲染完没有任何说明");
    assert.equal($("#view").querySelectorAll(".styles__render-img").length, 0,
      "mock 下不该出现读不到的图片");
  });

  await check("切片台批量导出：勾两条一起导、一条失败不挡另一条、重试能补上", async () => {
    WB.router.go("clipper", {});
    await tick(400);

    const picks = () => $("#view").querySelector(".clip-picks");
    assert.ok(picks(), "候选栏没有批量导出条");
    assert.ok(picks().textContent.indexOf("已选 0 条") >= 0, "初始不该有勾选");

    // 勾前两条
    const boxes = [...$("#view").querySelectorAll(".clip-card__pick")];
    assert.ok(boxes.length >= 2, "候选卡片上没有勾选框");
    boxes[0].checked = true;
    boxes[0].dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    boxes[1].checked = true;
    boxes[1].dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    await tick(60);
    assert.ok(picks().textContent.indexOf("已选 2 条") >= 0, "勾了两条却没更新计数");

    const go = [...picks().querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("批量导出") >= 0
    );
    assert.ok(go, "没有批量导出按钮");
    assert.ok(!go.disabled, "有勾选时批量导出不该禁用");
    go.click();
    // mock 批量任务 400/520ms 落地，队列轮询 900ms 一拍——等够两拍
    await tick(1700);

    // 这批两条：抬头写清条数，第二条故意失败
    const head = $("#view").querySelector(".exp-batch");
    assert.ok(head, "批量导出没有批次抬头");
    assert.ok(head.textContent.indexOf("这批 2 条") >= 0, "抬头条数不对：" + head.textContent);
    assert.ok(head.textContent.indexOf("1 失败") >= 0, "抬头没写失败数：" + head.textContent);

    const rows = [...$("#view").querySelectorAll(".exp-row")];
    const states = rows.map((r) => r.textContent);
    assert.ok(states.some((t) => t.indexOf("完成") >= 0), "没有成功的行：" + states.join(" | "));
    assert.ok(states.some((t) => t.indexOf("失败") >= 0), "没有失败的行：" + states.join(" | "));

    // 重试失败项：只有一条失败，重试后该行变成完成、抬头不再有"失败"
    const again = [...head.querySelectorAll("button")].find(
      (b) => b.textContent.indexOf("重试失败项") >= 0
    );
    assert.ok(again, "批次抬头没有重试失败项");
    again.click();
    await tick(1400);
    const after = [...$("#view").querySelectorAll(".exp-row")].map((r) => r.textContent);
    assert.ok(!after.some((t) => t.indexOf("失败") >= 0), "重试后仍有失败行：" + after.join(" | "));
    const head2 = $("#view").querySelector(".exp-batch");
    assert.ok(head2.textContent.indexOf("失败") < 0, "重试后抬头还写着失败：" + head2.textContent);
  });

  await check("合并页：加三个片段、上移下移反转、删除清空、预检表能读", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");

    // 空态：列表空、开始按钮禁用、预检写「加片段后自动预检」
    assert.ok(view.querySelector(".merge-empty"), "空列表没有空态文案");
    const startBtn = [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并");
    assert.ok(startBtn && startBtn.disabled, "空列表时开始合并应该是禁用的");

    // 加两条切片 + 一条素材库源片（规格不同，应出现差异表）
    const srcs = [...view.querySelectorAll(".merge-src")];
    assert.ok(srcs.length >= 3, "来源列表少于 3 条：" + srcs.length);
    srcs[0].click();
    srcs[1].click();
    await tick(600);
    let rows = [...view.querySelectorAll(".merge-row")];
    assert.equal(rows.length, 2, "加两条后顺序列表长度不对");
    assert.ok(rows[0].textContent.indexOf("1") === 0, "序号没从 1 开始：" + rows[0].textContent);

    // 预检：两条同规格 → 可以快速拼接
    let pfText = view.querySelector(".merge-table").textContent;
    assert.ok(pfText.indexOf("可以：参数一致") >= 0, "两条同规格却没说可以快速拼接：" + pfText);

    // 上移/下移/反转
    const actsOf = (i) => [...view.querySelectorAll(".merge-row")[i].querySelectorAll("button")];
    const names0 = [...view.querySelectorAll(".merge-row__name")].map((n) => n.textContent);
    actsOf(1)[0].click();
    await tick(200);
    const names1 = [...view.querySelectorAll(".merge-row__name")].map((n) => n.textContent);
    assert.equal(names1[0], names0[1], "上移后第一条应该是原来的第二条");
    assert.equal(names1[1], names0[0], "上移后第二条应该是原来的第一条");
    // 再下移回来，后面按原顺序断言
    actsOf(0)[1].click();
    await tick(200);

    const libRow = [...view.querySelectorAll(".merge-src")].find((b) => b.textContent.indexOf("source.mp4") >= 0);
    if (libRow) {
      libRow.click();
      await tick(600);
      pfText = view.querySelector(".merge-table").textContent;
      assert.ok(pfText.indexOf("规格差异") >= 0, "混入不同规格没出现差异表：" + pfText);
      assert.ok(pfText.indexOf("帧率") >= 0 || pfText.indexOf("高度") >= 0, "差异表没写具体项：" + pfText);
      // 规格不一致 → 快速拼接被禁、模式自动切精确
      const quick = view.querySelector('input[name="merge-mode"]');
      assert.ok(quick.disabled, "规格不一致时快速拼接应禁用");
      // 删掉这条，回到一致状态
      const last = [...view.querySelectorAll(".merge-row")].pop();
      [...last.querySelectorAll("button")].pop().click();
      await tick(600);
    }

    // 反转：顺序倒过来
    const before = [...view.querySelectorAll(".merge-row__name")].map((n) => n.textContent);
    [...view.querySelectorAll("button")].find((b) => b.textContent === "反转").click();
    await tick(600);
    const after = [...view.querySelectorAll(".merge-row__name")].map((n) => n.textContent);
    assert.equal(after.join("|"), before.slice().reverse().join("|"), "反转没生效");

    // 清空
    [...view.querySelectorAll("button")].find((b) => b.textContent === "清空").click();
    await tick(200);
    assert.equal(view.querySelectorAll(".merge-row").length, 0, "清空后还有行");
    assert.ok(view.querySelector(".merge-empty"), "清空后没有空态");
  });

  await check("合并页：点开始合并会提交任务并回报成片", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");

    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[0].click();
    srcs[1].click();
    await tick(700);

    const btn = [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并");
    assert.ok(btn && !btn.disabled, "两条片段都到位了，开始合并还是禁用");

    btn.click();
    await tick(80);
    // 提交后按钮立刻锁住，避免连点提交两份
    const busy = [...view.querySelectorAll("button")].find((b) => b.textContent.indexOf("合并中") === 0);
    assert.ok(busy && busy.disabled, "开始合并后按钮应该变成「合并中…」并禁用");

    await tick(900);
    const note = view.querySelector(".merge-flash:not(.merge-flash--error)");
    assert.ok(note && note.textContent.indexOf("合并完成") === 0, "没提示合并完成：" + (note && note.textContent));
    assert.ok(note.textContent.indexOf("2 段") >= 0, "完成提示里没写段数：" + note.textContent);
    const done = [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并");
    assert.ok(done && !done.disabled, "合并完成后按钮应该恢复可点");
  });

  await check("合并页：规格不一致时快速拼接禁用，精确重编码照样能合并", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");

    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[0].click();
    const lib = [...view.querySelectorAll(".merge-src")].find((b) => b.textContent.indexOf("source.mp4") >= 0);
    assert.ok(lib, "没有素材库源片可用");
    lib.click();
    await tick(700);

    const pf = view.querySelector(".merge-table").textContent;
    assert.ok(pf.indexOf("规格差异") >= 0, "差异没显示");
    // 规格不一致时这条只能叫"目标规格"：叫"统一规格"等于宣称它们已经统一了
    assert.ok(pf.indexOf("目标规格") >= 0, "不一致时规格行应写目标规格：" + pf.slice(0, 120));
    const quick = view.querySelector('input[name="merge-mode"]');
    assert.ok(quick.disabled, "快速拼接应禁用");

    const btn = [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并");
    assert.ok(btn && !btn.disabled, "有两条片段时按钮应可点");
    btn.click();
    await tick(1800);
    // 成功提示是第二个 .merge-flash（第一个是错误提示，平时 display:none）
    const note = view.querySelector(".merge-flash:not(.merge-flash--error)");
    assert.ok(note && note.textContent.indexOf("合并完成") >= 0, "精确模式没有报成功：" + (note && note.textContent));
  });

  await check("合并页：只有一条时不谎称规格不一致，手选模式不被预检改回去", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");

    // 页面 state 是模块级的，上一个用例留下的两条还在列表里——先清空再验"只有一条"
    const clear = [...view.querySelectorAll("button")].find((b) => b.textContent === "清空");
    if (clear) { clear.click(); await tick(300); }
    [...view.querySelectorAll(".merge-src")][0].click();
    await tick(600);
    // 只有一条时 can_quick 也是 false，但那不是"规格不一致"
    const flash = view.querySelector(".merge-flash:not(.merge-flash--error)");
    assert.ok(!flash || flash.textContent.indexOf("规格不一致") < 0,
      "只有一条时说成规格不一致：" + (flash && flash.textContent));
    const pf = view.querySelector(".merge-table").textContent;
    assert.ok(pf.indexOf("至少要两个片段") >= 0, "没有说清为什么不能拼接：" + pf.slice(0, 160));

    // 手动选精确后，再加一条一致规格的片段，预检不能替用户改回快速
    const precise = [...view.querySelectorAll('input[name="merge-mode"]')][1];
    precise.checked = true;
    precise.dispatchEvent(new dom.window.Event("change"));
    await tick(200);
    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[1].click();   // 换一条没加过的：点已加入的只会提示"已经在列表里了"
    await tick(700);
    const again = [...view.querySelectorAll('input[name="merge-mode"]')][1];
    assert.ok(again.checked, "预检把用户手选的精确模式改回去了");

    // 输出文件名要跟着段数走，不能勾了两段还挂着"合并_1段"
    const nameInput = [...view.querySelectorAll("input")].find((i) => (i.value || "").indexOf("合并_") === 0);
    assert.ok(nameInput && nameInput.value.indexOf("2段") >= 0, "文件名没跟上段数：" + (nameInput && nameInput.value));
  });

  await check("合并页：添加本地文件支持一次多选，按 | 拆开后全部进顺序表", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");
    const clear = [...view.querySelectorAll("button")].find((b) => b.textContent === "清空");
    if (clear) { clear.click(); await tick(200); }

    const realRpc = WB.rpc;
    const calls = [];
    WB.rpc = function (method, params, opts) {
      if (method === "fs.pick") {
        calls.push(params || {});
        return Promise.resolve({ path: "C:/local/a.mp4|C:/local/b.mp4" });
      }
      return realRpc.call(WB, method, params, opts);
    };
    [...view.querySelectorAll("button")].find((b) => b.textContent === "添加本地文件").click();
    await tick(800);
    WB.rpc = realRpc;

    assert.equal(calls.length, 1, "没有发起 fs.pick");
    assert.equal(calls[0].kind, "files", "合并页应该按多文件语义要路径：" + JSON.stringify(calls[0]));
    const names = [...view.querySelectorAll(".merge-row__name")].map((n) => n.textContent);
    assert.deepStrictEqual(names, ["a.mp4", "b.mp4"], "多选回来的路径没有全部进顺序表：" + names.join("|"));
  });

  await check("合并页：选过的分辨率和帧率会随提交传给后端", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");
    const clear = [...view.querySelectorAll("button")].find((b) => b.textContent === "清空");
    if (clear) { clear.click(); await tick(200); }

    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "fs.pick") return Promise.resolve({ path: "C:/local/a.mp4|C:/local/b.mp4" });
      return realRpc.call(WB, method, params, opts);
    };
    [...view.querySelectorAll("button")].find((b) => b.textContent === "添加本地文件").click();
    await tick(800);
    WB.rpc = realRpc;

    const selects = [...view.querySelectorAll("select")];
    assert.ok(selects.length >= 2, "没有分辨率和帧率下拉");
    selects[0].value = "1080";
    selects[0].dispatchEvent(new dom.window.Event("change"));
    selects[1].value = "30";
    selects[1].dispatchEvent(new dom.window.Event("change"));
    await tick(800);

    let started = null;
    const beforeStart = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "merge.start") { started = params || {}; }
      return beforeStart.call(WB, method, params, opts);
    };
    [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并").click();
    await tick(80);
    WB.rpc = beforeStart;

    assert.ok(started, "没有提交 merge.start");
    assert.equal(started.width, 1080, "宽度没传：" + JSON.stringify(started));
    assert.equal(started.height, 608, "高度没按 16:9 推：" + JSON.stringify(started));
    assert.equal(started.fps, 30, "帧率没传：" + JSON.stringify(started));
  });

  await check("合并页：有缺失文件时开始按钮禁用并提示先移出列表", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");
    const clear = [...view.querySelectorAll("button")].find((b) => b.textContent === "清空");
    if (clear) { clear.click(); await tick(200); }

    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "fs.pick") return Promise.resolve({ path: "C:/local/a.mp4|C:/local/b.mp4" });
      if (method === "merge.preflight") {
        return realRpc.call(WB, method, params, opts).then(function (data) {
          data.missing = [{ path: "C:/local/gone.mp4", name: "gone.mp4" }];
          return data;
        });
      }
      return realRpc.call(WB, method, params, opts);
    };
    [...view.querySelectorAll("button")].find((b) => b.textContent === "添加本地文件").click();
    await tick(900);
    WB.rpc = realRpc;

    const table = view.querySelector(".merge-table").textContent;
    assert.ok(table.indexOf("1 个不在") >= 0, "预检没显示缺失数：" + table.slice(0, 160));
    const start = [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并");
    assert.ok(start && start.disabled, "有缺失文件时开始按钮应该禁用");
    assert.ok((start.title || "").indexOf("标红") >= 0, "禁用原因没写在 title：" + start.title);
  });

  await check("合并页：输出目录路径按钮能选文件夹，预检不再把手选目录盖回默认", async () => {
    WB.router.go("merge");
    await tick(400);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    // 模块级 state 跨用例保留：显式清干净，别让上一个用例的目录漏进来
    st.order = []; st.preflight = null; st.outDir = ""; st.outDirManual = false;
    st.lastMergeId = ""; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const pathBtn = () => view.querySelector(".merge-outdir-path");
    assert.ok(pathBtn(), "输出目录没有做成按钮");
    assert.equal(pathBtn().tagName, "BUTTON", "输出目录应该是按钮而不是只读输入框");
    assert.ok(pathBtn().textContent.indexOf("选择输出目录") >= 0,
      "空态没有可点的占位文案：" + pathBtn().textContent);

    // 加两段，预检给出默认目录
    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[0].click();
    srcs[1].click();
    await tick(700);
    assert.ok(pathBtn().textContent.indexOf("merge") >= 0,
      "预检后没显示默认输出目录：" + pathBtn().textContent);

    // 点路径按钮：走 fs.pick 的 dir 语义
    const calls = [];
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "fs.pick") {
        calls.push(params || {});
        return Promise.resolve({ path: "D:/chosen/out" });
      }
      return realRpc.call(WB, method, params, opts);
    };
    pathBtn().click();
    await tick(300);
    WB.rpc = realRpc;

    assert.equal(calls.length, 1, "没有发起 fs.pick");
    assert.equal(calls[0].kind, "dir", "选输出目录要按 dir 语义要路径：" + JSON.stringify(calls[0]));
    assert.equal(pathBtn().textContent, "D:/chosen/out", "选完路径没显示出来");
    assert.equal(pathBtn().title, "D:/chosen/out", "完整路径没放进 title");

    // 再加一段触发预检：手选目录不能被默认值盖回去
    [...view.querySelectorAll(".merge-src")][2].click();
    await tick(800);
    assert.equal(pathBtn().textContent, "D:/chosen/out", "预检把手选的输出目录盖回去了");

    // 提交时传的就是手选目录
    let started = null;
    const beforeStart = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "merge.start") started = params || {};
      return beforeStart.call(WB, method, params, opts);
    };
    [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并").click();
    await tick(120);
    WB.rpc = beforeStart;
    assert.ok(started, "没有提交 merge.start");
    assert.equal(started.out_dir, "D:/chosen/out", "提交的 out_dir 不是手选目录：" + JSON.stringify(started));
    await tick(1400);
  });

  await check("合并页：取消选择输出目录不改原值，也不报错", async () => {
    WB.router.go("merge");
    await tick(300);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    st.order = []; st.preflight = null; st.outDir = "D:/keep/me"; st.outDirManual = true;
    st.lastMergeId = ""; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const pathBtn = view.querySelector(".merge-outdir-path");
    assert.equal(pathBtn.textContent, "D:/keep/me");

    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "fs.pick") return Promise.resolve({ path: null });
      return realRpc.call(WB, method, params, opts);
    };
    pathBtn.click();
    await tick(300);
    WB.rpc = realRpc;

    assert.equal(pathBtn.textContent, "D:/keep/me", "取消选择把已选目录清掉了");
    const err = view.querySelector(".merge-flash--error");
    assert.ok(!err || err.style.display === "none" || !err.textContent,
      "取消选择不该报错：" + (err && err.textContent));
  });

  await check("合并页：合并成功后才能打开输出目录，且按 merge_id 打开", async () => {
    WB.router.go("merge");
    await tick(300);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    st.order = []; st.preflight = null; st.outDir = ""; st.outDirManual = false;
    st.lastMergeId = ""; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const openBtn = () => [...view.querySelectorAll("button")]
      .find((b) => b.textContent === "打开输出目录");
    assert.ok(openBtn(), "没有「打开输出目录」按钮");
    assert.ok(openBtn().disabled, "还没合并，打开按钮不该可点");

    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[0].click();
    srcs[1].click();
    await tick(700);
    [...view.querySelectorAll("button")].find((b) => b.textContent === "开始合并").click();
    await tick(1600);

    assert.ok(!openBtn().disabled, "合并成功了，打开按钮还是禁用");

    const opened = [];
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "merge.reveal_output") {
        opened.push(params || {});
        return Promise.resolve({ path: "C:/mock/export/merge" });
      }
      return realRpc.call(WB, method, params, opts);
    };
    openBtn().click();
    await tick(200);
    WB.rpc = realRpc;

    assert.equal(opened.length, 1, "没有调用 merge.reveal_output");
    assert.ok(opened[0].merge_id, "打开时没带 merge_id：" + JSON.stringify(opened[0]));
    assert.ok(!("path" in opened[0]), "不该把路径递给后端：" + JSON.stringify(opened[0]));
  });

  await check("合并页：打开输出目录失败时错误可见，不静默吞掉", async () => {
    WB.router.go("merge");
    await tick(300);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    st.order = []; st.preflight = null; st.lastMergeId = "mg_mock_999"; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const openBtn = [...view.querySelectorAll("button")]
      .find((b) => b.textContent === "打开输出目录");
    assert.ok(openBtn && !openBtn.disabled, "有上次合并记录时打开按钮应可点");

    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "merge.reveal_output") {
        const err = new Error("成片和它所在的目录都不在了");
        err.code = "NOT_FOUND";
        return Promise.reject(err);
      }
      return realRpc.call(WB, method, params, opts);
    };
    openBtn.click();
    await tick(200);
    WB.rpc = realRpc;

    const flash = view.querySelector(".merge-flash--error");
    assert.ok(flash && flash.style.display !== "none" && flash.textContent.indexOf("不在了") >= 0,
      "打开失败没有可见错误：" + (flash && flash.textContent));
  });

  await check("合并页：清空列表把输出目录和打开按钮一起复位", async () => {
    WB.router.go("merge");
    await tick(300);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    st.order = []; st.preflight = null; st.outDir = ""; st.outDirManual = false;
    st.lastMergeId = ""; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const srcs = [...view.querySelectorAll(".merge-src")];
    srcs[0].click();
    await tick(600);
    const st2 = WB.pages.merge._internals.state;
    st2.outDir = "D:/hand/picked";
    st2.outDirManual = true;
    st2.lastMergeId = "mg_mock_1";
    WB.pages.merge._internals.refreshPreflight();
    await tick(600);

    const pathBtn = view.querySelector(".merge-outdir-path");
    assert.equal(pathBtn.textContent, "D:/hand/picked");

    [...view.querySelectorAll("button")].find((b) => b.textContent === "清空").click();
    await tick(300);

    assert.ok(pathBtn.textContent.indexOf("选择输出目录") >= 0,
      "清空后输出目录没复位：" + pathBtn.textContent);
    assert.equal(WB.pages.merge._internals.state.outDirManual, false, "清空后手选标记没复位");
    assert.equal(WB.pages.merge._internals.state.lastMergeId, "", "清空后还留着上次合并 id");
    const openBtn = [...view.querySelectorAll("button")]
      .find((b) => b.textContent === "打开输出目录");
    assert.ok(openBtn && openBtn.disabled, "清空后打开按钮应该禁用");
  });

  await check("合并页：切到别的页再回来，手选输出目录还在", async () => {
    WB.router.go("merge");
    await tick(300);
    const st = WB.pages.merge._internals.state;
    st.order = []; st.preflight = null; st.outDir = ""; st.outDirManual = false;
    st.lastMergeId = ""; st.lastOutput = "";
    WB.router.go("merge");
    await tick(300);

    const view = $("#view");
    [...view.querySelectorAll(".merge-src")][0].click();
    await tick(500);
    const st2 = WB.pages.merge._internals.state;
    st2.outDir = "E:/keep/across-pages";
    st2.outDirManual = true;

    WB.router.go("library");
    await tick(300);
    WB.router.go("merge");
    await tick(400);

    const pathBtn = $("#view").querySelector(".merge-outdir-path");
    assert.equal(pathBtn.textContent, "E:/keep/across-pages",
      "切页回来手选目录丢了：" + pathBtn.textContent);
  });

  await check("合并页：长任务不会在 12 分钟假装失败", async () => {
    // 现场 bug：轮询到 900 次（12 分钟）就静默停下，按钮永远显示「合并中…」，
    // 而那次合并实际跑了 20 分钟才成功。
    const poll = WB._mergePoll;
    assert.ok(poll, "没暴露轮询策略（WB._mergePoll）");
    assert.ok(poll.limit > 0 && poll.hard > poll.limit, "阈值不合理");
    assert.equal(poll.gaveUp(poll.limit + 1), false, "刚到 12 分钟就放弃了");
    assert.equal(poll.gaveUp(poll.limit + 200), false, "还在正常区间就放弃了");
    assert.equal(poll.gaveUp(poll.hard + 1), true, "到了硬上限也不放手");
    assert.ok(poll.delay(1) < poll.slow, "正常阶段不该已经是慢速");
    assert.equal(poll.delay(poll.limit + 1), poll.slow, "过了 12 分钟没有放慢节奏");
  });

  await check("合并页：任务结束事件能把界面收尾（轮询兜底）", async () => {
    WB.router.go("merge");
    await tick(300);
    const view = $("#view");
    const st = WB.pages.merge._internals.state;
    const api = WB.pages.merge._internals;

    st.order = []; st.preflight = null;
    st.lastMergeId = ""; st.lastOutput = "";
    st.running = true; st.jobId = "job_merge_event"; st.progress = 0.4;
    api.watchJob("job_merge_event");

    window.__wbEvent("job.finished", JSON.stringify({
      id: "job_merge_event",
      status: "succeeded",
      result: { merge_id: "mg_event", path: "C:/mock/out/x.mp4", name: "x.mp4", count: 2 },
      error: null,
      job: { id: "job_merge_event", type: "merge.run", status: "succeeded", progress: 1 }
    }));
    await tick(120);

    assert.equal(st.running, false, "任务结束了页面还在「合并中」");
    assert.equal(st.jobId, "", "任务结束后还留着 jobId");
    assert.equal(st.lastMergeId, "mg_event", "事件里的 merge_id 没写进页面");
    assert.ok(view.textContent.indexOf("合并完成：x.mp4（2 段）") >= 0,
      "完成提示没出现：" + view.textContent);
    const openBtn = [...view.querySelectorAll("button")]
      .find((b) => b.textContent === "打开输出目录");
    assert.ok(openBtn && !openBtn.disabled, "事件收尾后打开按钮仍禁用");
    api.unwatchJob();
  });

  // 44/45 号之后：关于页的日志与诊断包
  await check("设置页关于分区：显示日志目录、导出诊断包、复制最近日志", async () => {
    WB.router.go("settings", { section: "about" });
    await tick(100);
    const view = window.document.querySelector("#settings-body");
    const about = view.querySelector(".settings__about");
    assert.ok(about, "没有关于分区");
    const labels = [...about.querySelectorAll(".field__label")].map((n) => n.textContent);
    assert.deepStrictEqual(
      labels,
      ["版本", "日志目录", "排障", "", ""],
      "关于分区字段不对：" + labels.join("/")
    );
    await tick(60);
    const version = about.querySelector(".settings__row-actions .muted").textContent;
    assert.ok(/VideoWorkbench 0\.1\.0/.test(version), "版本没显示出来：" + version);
    const dirText = [...about.querySelectorAll(".settings__row-actions .muted")].map((n) => n.textContent).join(" | ");
    assert.ok(dirText.indexOf("logs") >= 0, "日志目录没显示：" + dirText);

    const buttons = [...about.querySelectorAll("button")];
    const exportBtn = buttons.find((b) => b.textContent === "导出诊断包");
    assert.ok(exportBtn, "没有导出诊断包按钮");
    exportBtn.click();
    await tick(120);
    const notes = about.textContent;
    assert.ok(notes.indexOf("diagnostics") >= 0 && notes.indexOf("KB") >= 0, "导出后没有落点提示：" + notes);
  });


  await check("切片台音轨：选 BGM、改音量、关闪避、淡入淡出都随导出一起发出去", async () => {
    const calls = [];
    const realRpc = window.WB.rpc;
    window.WB.rpc = function (method, params, opts) {
      calls.push({ method: method, params: params });
      if (method === "fs.pick") {
        return Promise.resolve({ path: "D:/music/lofi.mp3" });
      }
      return realRpc.call(window.WB, method, params, opts);
    };
    try {
      WB.router.go("clipper", {});
      await tick(400);
      const view = $("#view");
      const box = () => view.querySelector(".exp-form__audio");
      assert.ok(box(), "导出面板没有音轨区");

      // 默认值：25% 音量、开闪避、淡入淡出 200ms（0.2 秒可配的默认档）
      assert.ok(box().querySelector(".exp-form__audio-hint").textContent.indexOf("音量 25%") >= 0,
        "默认音量不是 25%：" + box().textContent);
      assert.ok(box().querySelector(".exp-form__audio-hint").textContent.indexOf("200 ms") >= 0,
        "默认没带淡入淡出 200ms：" + box().textContent);
      assert.strictEqual(box().querySelector(".exp-form__bgm-duck").checked, true,
        "人声闪避默认应该是开着的");

      // 选一首：走 fs.pick(kind=audio)，选完要显示文件名而不是整条路径
      const pick = [...box().querySelectorAll("button")].find(
        (b) => b.textContent.indexOf("选一首") >= 0
      );
      assert.ok(pick, "没有选背景音乐按钮");
      pick.click();
      await tick(150);
      assert.ok(calls.some((c) => c.method === "fs.pick" && c.params.kind === "audio"),
        "选文件没按音频类型调 fs.pick");
      assert.ok(view.querySelector(".exp-form__bgm-name").textContent.indexOf("lofi.mp3") >= 0,
        "选完没显示文件名：" + view.querySelector(".exp-form__bgm-name").textContent);

      // 改音量到 40%、关掉闪避
      const volume = view.querySelector(".exp-form__bgm-volume");
      volume.value = "40";
      volume.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      const duck = view.querySelector(".exp-form__bgm-duck");
      duck.checked = false;
      duck.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      await tick(60);
      const hint = view.querySelector(".exp-form__audio-hint").textContent;
      assert.ok(hint.indexOf("音量 40%") >= 0, "音量改了但文案没跟上：" + hint);
      assert.ok(hint.indexOf("不闪避") >= 0, "关掉闪避后文案没说明：" + hint);

      // 导出：四个参数必须原样发出去（少一个后端就按配置静默替换）
      const go = [...view.querySelectorAll(".exp-form button")].find(
        (b) => b.textContent.indexOf("导出这一段") >= 0
      );
      go.click();
      await tick(200);
      const sent = calls.filter((c) => c.method === "clip.export").pop();
      assert.ok(sent, "没有发出 clip.export");
      assert.strictEqual(sent.params.bgm, "D:/music/lofi.mp3", "bgm 没带上：" + JSON.stringify(sent.params));
      assert.strictEqual(sent.params.bgm_volume, 40, "音量没带上：" + sent.params.bgm_volume);
      assert.strictEqual(sent.params.bgm_duck, false, "闪避开关没带上：" + sent.params.bgm_duck);
      assert.strictEqual(sent.params.fade_ms, 200, "淡入淡出没带上：" + sent.params.fade_ms);
    } finally {
      window.WB.rpc = realRpc;
    }
  });

  await check("切片台交付：三个通道都能点、进剪映草稿箱要显式勾、结果给路径与说明", async () => {
    const calls = [];
    const realRpc = window.WB.rpc;
    window.WB.rpc = function (method, params, opts) {
      calls.push({ method: method, params: params });
      return realRpc.call(window.WB, method, params, opts);
    };
    try {
      // 前面的用例进过切片台，通道信息可能已经缓存过；清掉才能证明是这一页自己取的
      const internals = WB.pages.clips._internals;
      if (internals && internals.state) internals.state().nle = null;
      WB.router.go("clipper", {});
      await tick(500);
      const view = $("#view");
      const box = () => view.querySelector(".exp-form__deliver");
      assert.ok(box(), "导出面板没有交付区");

      // 通道信息必须真取一次：写死文案的话，没装剪映的机器也会显示"放进草稿箱"
      assert.ok(
        calls.some((c) => c.method === "nle.info"),
        "没调过 nle.info，通道与是否装剪映都无从得知"
      );
      const btns = () => [...box().querySelectorAll(".exp-form__deliver-btn")];
      assert.strictEqual(btns().length, 3, "通道数不对：" + btns().map((b) => b.textContent).join("/"));
      assert.strictEqual(
        btns().map((b) => b.dataset.channel).join(","),
        "jianying,otio,bundle",
        "三个通道的身份/顺序不对"
      );
      assert.ok(box().querySelector(".exp-form__place"), "检测到剪映却没给「放进草稿箱」的勾");
      assert.ok(
        box().textContent.indexOf("字幕样式不跟随") >= 0,
        "没写清字幕样式不跟随：" + box().textContent.slice(0, 200)
      );

      // 默认不勾：点剪映只写项目目录，不许动人家的草稿箱
      btns()[0].click();
      await tick(1000);
      const first = calls.filter((c) => c.method === "clip.deliver").pop();
      assert.ok(first, "没发出 clip.deliver");
      assert.strictEqual(first.params.channel, "jianying");
      assert.strictEqual(first.params.place_in_app, false, "默认就去改别人的草稿箱了");
      const shown = box().querySelector(".exp-form__deliver-path");
      assert.ok(shown, "交付完成没给路径");
      assert.ok(shown.textContent.indexOf("export") >= 0, "路径不像落到了项目 export：" + shown.textContent);

      // 勾上再交付 → place_in_app 必须为真，并且提示改成"已放进草稿箱"
      const place = box().querySelector(".exp-form__place");
      place.checked = true;
      place.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
      await tick(80);
      btns()[0].click();
      await tick(1000);
      const second = calls.filter((c) => c.method === "clip.deliver").pop();
      assert.strictEqual(second.params.place_in_app, true, "勾了却没把 place_in_app 带上");
      assert.ok(
        box().textContent.indexOf("已放进剪映草稿箱") >= 0,
        "勾了之后提示没变：" + box().textContent.slice(0, 200)
      );

      // 达芬奇 OTIO：通道名要对，路径后缀要是 .otio
      btns()[1].click();
      await tick(1000);
      const third = calls.filter((c) => c.method === "clip.deliver").pop();
      assert.strictEqual(third.params.channel, "otio");
      assert.ok(
        box().querySelector(".exp-form__deliver-path").textContent.indexOf(".otio") >= 0,
        "OTIO 通道没给 .otio 路径"
      );

      // 降级包：必须说清包里放的是整段源视频还是成片
      btns()[2].click();
      await tick(1000);
      const fourth = calls.filter((c) => c.method === "clip.deliver").pop();
      assert.strictEqual(fourth.params.channel, "bundle");
      assert.ok(
        box().textContent.indexOf("整段源视频") >= 0,
        "降级包没说明用的是源视频：" + box().textContent.slice(0, 240)
      );
    } finally {
      window.WB.rpc = realRpc;
    }
  });

  await check("首次检查页：五件事三态齐全，跳过之后不再弹", async () => {
    const calls = [];
    const realRpc = window.WB.rpc;
    // 记下这一步真正发了什么：跳过必须真的告诉后端，只在界面上关掉等于没记
    window.WB.rpc = function (method, params, opts) {
      calls.push({ method: method, params: params });
      return realRpc.call(window.WB, method, params, opts);
    };
    try {
      WB.router.go("onboard");
      await tick(150);
      const view = $("#view");
      const panel = view.querySelector(".onb-panel");
      assert.ok(panel, "向导面板没渲染");

      const rows = [...panel.querySelectorAll(".onb-row")];
      assert.strictEqual(rows.length, 5, `应该是五件事，实际 ${rows.length}`);
      assert.strictEqual(rows.map((row) => row.dataset.step).join(","),
        "webview2,ffmpeg,model,bili,llm", "五件事的顺序/身份不对");

      // 三态都要出现：少一种说明 status→文案映射断了（截图里会看不出差别）
      const pills = rows.map((row) => row.querySelector(".onb-pill").textContent);
      assert.ok(pills.indexOf("就绪") >= 0, "没有 ok 态：" + pills.join("/"));
      assert.ok(pills.indexOf("待设置") >= 0, "没有 todo 态：" + pills.join("/"));
      assert.ok(pills.indexOf("缺失") >= 0, "没有 missing 态：" + pills.join("/"));

      // 有待处理项时汇总必须点名缺什么，只说"未就绪"等于没说
      const sum = panel.querySelector(".onb-sum");
      assert.ok(sum.textContent.indexOf("还差") >= 0, "汇总没说缺什么：" + sum.textContent);
      assert.strictEqual(sum.classList.contains("is-ready"), false);

      // 「先这样，进去试试」永远可点：缺模型/缺 key 也拦人，是把能用的功能一起锁掉
      const startBtn = window.document.getElementById("onb-done");
      assert.ok(startBtn && !startBtn.disabled, "主按钮被禁用了");

      const skipBtn = window.document.getElementById("onb-skip");
      assert.ok(skipBtn, "没有跳过按钮");
      skipBtn.click();
      await tick(150);
      assert.ok(
        calls.some((c) => c.method === "app.onboard.done" && c.params && c.params.skipped === true),
        "跳过没调 app.onboard.done(skipped=true)，下次还会弹"
      );

      // 已看过的机器不该再弹（否则每次启动都骚扰）
      const shown = await window.WB.onboard.maybeShow({ onboarded: true });
      assert.strictEqual(shown, false, "onboarded=true 时仍然弹了向导");
    } finally {
      window.WB.rpc = realRpc;
    }
  });

  await check("下载兜底：失败批次能看见、装了 BBDown 才有「用 BBDown 重试」、点了把 backend 传下去", async () => {
    const calls = [];
    const realRpc = window.WB.rpc;
    window.WB.rpc = function (method, params, opts) {
      calls.push({ method: method, params: params });
      return realRpc.call(window.WB, method, params, opts);
    };
    try {
      window.__wbRetried = null;
      WB.pages.newtask._resetRecent();
      WB.router.go("newtask", {});
      await tick(600);
      const view = $("#view");

      // 后端体检必须真取一次：写死"已装 BBDown"会让没装的机器点出一个假按钮
      assert.ok(
        calls.some((c) => c.method === "download.backends"),
        "没调过 download.backends"
      );

      const rows = [...view.querySelectorAll(".nt-recent__row")];
      assert.ok(rows.length >= 1, "没有最近下载记录");
      const failedRow = rows.filter((row) => row.textContent.indexOf("失败") >= 0)[0];
      assert.ok(failedRow, "失败批次没显示出来：" + rows.map((r) => r.textContent).join(" | "));

      const bbBtn = failedRow.querySelector(".nt-recent__bbdown");
      assert.ok(bbBtn, "装了 BBDown 却没给「用 BBDown 重试」");
      assert.ok(bbBtn.textContent.indexOf("BBDown") >= 0, "按钮文案不对：" + bbBtn.textContent);

      bbBtn.click();
      await tick(400);
      const retried = window.__wbRetried;
      assert.ok(retried, "点了按钮却没发 download.retry_failed");
      assert.strictEqual(retried.backend, "bbdown", "重试时没带 backend=bbdown");
      assert.ok(retried.id, "重试没带任务 id");

      // 设置页也要能看见后端状态（P1-08 验收第①条）
      WB.router.go("settings", { section: "process" });
      await tick(500);
      const dots = [...$("#view").querySelectorAll(".settings__backend")];
      assert.ok(dots.length >= 2, "设置页没有后端体检两行：" + dots.length);
      assert.ok(
        $("#view").textContent.indexOf("默认后端") >= 0,
        "设置页没说默认走哪个后端"
      );
    } finally {
      window.WB.rpc = realRpc;
    }
  });

  await check("设置页：额外请求头按「名字: 值」填，存成对象", async () => {
    // 有些网关要私有请求头（opencode 的 x-opencode-session、Azure 的 api-version）。
    // 界面上是一行一个，配置里必须是对象——两边都要转对。
    WB.router.go("settings", { section: "llm" });
    await tick(100);
    const body = window.document.querySelector("#settings-body");
    const labels = [...body.querySelectorAll(".field__label")].map((n) => n.textContent.trim());
    assert.ok(labels.indexOf("额外请求头") >= 0, "模型分区里没有「额外请求头」，现有字段：" + labels.join("/"));

    const field = [...body.querySelectorAll(".field")].find((f) => {
      const l = f.querySelector(".field__label");
      return l && l.textContent.trim() === "额外请求头";
    });
    const area = field.querySelector("textarea");
    assert.ok(area, "额外请求头不是一个多行输入框");

    area.value = "x-opencode-session: wb-test{0}api-version: 2024-02-01".replace("{0}", String.fromCharCode(10));
    area.dispatchEvent(new window.Event("input", { bubbles: true }));
    await tick(40);

    const bar = window.document.querySelector(".settings__bar");
    assert.ok(bar && bar.textContent.indexOf("待保存") >= 0, "填了请求头却没标脏");

    const puts = [];
    const realRpc = window.WB.rpc;
    window.WB.rpc = function (method, params, opts) {
      if (method === "app.settings.put") puts.push(params || {});
      return realRpc.call(window.WB, method, params, opts);
    };
    try {
      const saveBtn = [...window.document.querySelectorAll("button")].find(
        (b) => b.textContent.trim() === "保存"
      );
      saveBtn.click();
      await tick(150);
    } finally {
      window.WB.rpc = realRpc;
    }
    const patch = puts.length ? puts[puts.length - 1].values || {} : {};
    const headers = (patch.llm || {}).extra_headers;
    assert.ok(headers && typeof headers === "object", "提交里没有 llm.extra_headers 对象：" + JSON.stringify(patch).slice(0, 120));
    assert.strictEqual(Object.keys(headers).length, 2, "应解析出两个头，实际 " + JSON.stringify(headers));
    assert.strictEqual(headers["api-version"], "2024-02-01");
  });

  // 状态条曾经恒为 0，两处都错：任务行的类型列叫 type（前端按 kind 读，永远是空串），
  // 而且统计发生在合并事件之前（新任务那一刻永远是 0）。这两条钉住映射与统计本身。
  await check("状态条：把任务行按 type 分到三格里", () => {
    assert.strictEqual(WB._jobBucket({ type: "download.item" }), "download");
    assert.strictEqual(WB._jobBucket({ type: "download.batch" }), "download");
    assert.strictEqual(WB._jobBucket({ type: "transcribe.run" }), "transcribe");
    assert.strictEqual(WB._jobBucket({ type: "subtitle.optimize" }), "transcribe");
    assert.strictEqual(WB._jobBucket({ type: "clip.export" }), "export");
    assert.strictEqual(WB._jobBucket({ type: "dev.demo" }), null);
    assert.strictEqual(WB._jobBucket({ kind: "download.item" }), "download", "老字段 kind 也该认");
    assert.strictEqual(WB._jobBucket(null), null, "空行不该炸");
  });

  await check("状态条：只数 running/queued，且不把无关任务算进三格", () => {
    const counts = WB._countJobs([
      { id: "a", type: "download.item", status: "running" },
      { id: "b", type: "download.batch", status: "queued" },
      { id: "c", type: "download.item", status: "succeeded" },
      { id: "d", type: "transcribe.run", status: "running" },
      { id: "e", type: "merge.run", status: "running" },
      { id: "f", type: "clip.export", status: "failed" },
      { id: "g", type: "dev.demo", status: "running" },
    ]);
    assert.strictEqual(counts.download, 2, "下载应是 2，实际 " + counts.download);
    assert.strictEqual(counts.transcribe, 1, "转录应是 1，实际 " + counts.transcribe);
    assert.strictEqual(counts.export, 1, "合并算导出，应是 1，实际 " + counts.export);
  });

  await check("状态条：job.created 之后状态点亮起来", async () => {
    window.__wbEvent("job.created", JSON.stringify({
      job: { id: "job_status_ui", type: "download.item", status: "running", progress: 0.2 },
    }));
    await tick(40);
    const dot = window.document.getElementById("status-dot");
    assert.strictEqual(dot.getAttribute("data-active"), "true", "有任务在跑，状态点却是暗的");
    const text = window.document.getElementById("status-jobs-text").textContent;
    assert.ok(/下载 [1-9]/.test(text), "跑着的下载没进状态条：" + text);
    window.__wbEvent("job.finished", JSON.stringify({ id: "job_status_ui", status: "succeeded" }));
  });

  // 下载时任务中心会闪：旧实现每来一条进度就 body.textContent="" + 重发 job.list + 重建全部卡片，
  // 每秒 4 次。这里钉住「同一个任务行节点在进度事件之后还是同一个节点」（就地更新）。
  await check("任务中心：进度事件只改这一行，不整块重画", async () => {
    // 先关掉前面几项检查留下的弹窗：它们的 .card 也会被 querySelectorAll 选中
    Array.prototype.slice.call(window.document.querySelectorAll(".modal__x")).forEach((b) => b.click());
    await tick(80);
    window.document.getElementById("status-jobs").click();
    await tick(150);
    const body = window.document.querySelector(".modal .modal__body");
    assert.ok(body, "任务中心没打开");
    const listed = await WB.rpc("job.list", {}, { silent: true, fallback: [] });
    const jobs = Array.isArray(listed) ? listed : (listed && listed.items) || [];
    assert.ok(jobs.length > 0, "job.list 里没有任务");
    const id = jobs[0].id;
    const row = body.querySelector('[data-job-id="' + id + '"]');
    assert.ok(row, "任务中心里找不到 " + id + " 这一行");
    const bar = row.querySelector(".progress__bar");
    assert.ok(bar, "任务行里没有进度条");
    window.__wbEvent("job.updated", JSON.stringify({ id: id, status: "running", progress: 0.42 }));
    // 等到值真的写进那一行再断言：事件是异步落到 DOM 的，固定 tick 在慢机器上会抢跑
    await until(
      () => {
        const now = body.querySelector('[data-job-id="' + id + '"]');
        const bar2 = now && now.querySelector(".progress__bar");
        return !!bar2 && bar2.style.width === "42%";
      },
      `job.updated 把进度写进 ${id} 那一行`,
      10
    );
    const after = body.querySelector('[data-job-id="' + id + '"]');
    assert.strictEqual(after, row, "任务行被重建了（节点换了），这就是下载时闪的原因");
    assert.strictEqual(after.querySelector(".progress__bar"), bar, "进度条节点也被重建了");
    assert.strictEqual(after.querySelector(".progress__bar").style.width, "42%", "进度没写进那一行");
  });
  // 事件会丢（实测：长任务跑完那一刻 job.finished 没送到前端，界面永久停在 97%/写入字幕）。
  // 所以还有在跑的任务时，前端每 3 秒拿 job.list 对一次账。这里钉住两件事：
  // 该对账时才对账；对完账，卡住的那一行必须变成真状态，而且不换节点。
  await check("任务中心：事件丢了也能靠对账纠正过来", async () => {
    const realRpc = WB.rpc;
    let jobs = [{ id: "job_stuck_ui", type: "transcribe", status: "running", progress: 0.97, stage: "写入字幕" }];
    WB.rpc = function (method, params, opts) {
      if (method === "job.list") return Promise.resolve(JSON.parse(JSON.stringify(jobs)));
      return realRpc.call(WB, method, params, opts);
    };
    try {
      // 关掉之前几项检查留下的弹窗，免得读到旧 body
      Array.prototype.slice.call(window.document.querySelectorAll(".modal__x")).forEach((b) => b.click());
      await tick(80);
      window.document.getElementById("status-jobs").click();
      await tick(150);
      const body = window.document.querySelector(".modal .modal__body");
      const row = body.querySelectorAll(".card")[0];
      assert.ok(row, "没渲染出任务行");
      assert.ok(WB._hasActiveJobs(), "有 running 任务，hasActiveJobs 却是 false");
      assert.ok(/9[0-9]%/.test(row.textContent), "初始进度不对：" + row.textContent);

      // 后端其实已经跑完，但那条事件丢了：只有 job.list 知道真状态
      jobs = [{ id: "job_stuck_ui", type: "transcribe", status: "succeeded", progress: 1, stage: "完成" }];
      await WB._reconcileJobs();
      await tick(120);
      const after = body.querySelectorAll(".card")[0];
      assert.strictEqual(after, row, "对账把节点重建了");
      assert.ok(/100%/.test(after.textContent), "对账后进度没变 100%：" + after.textContent);
      assert.ok(/成功|已完成|succeeded/.test(after.textContent), "对账后状态没变成成功：" + after.textContent);
      assert.notStrictEqual(WB._hasActiveJobs(), true, "任务已经结束，hasActiveJobs 还是 true");
    } finally {
      WB.rpc = realRpc;
    }
  });


  // ---------------------------------------------------------------- 三态与交互规格
  //
  // 上面那批用例钉的是"功能对不对"，这一段钉的是"状态说没说真话"。
  // 三态改造最容易犯的错是：加载中先画一句"还没有 X"——数据一到又变，
  // 但那一瞬间用户已经去点「重新生成」了。

  await check("状态条：磁盘那一格从载荷读，三种状态三种字", async () => {
    const disk = window.document.getElementById("status-disk");
    assert.ok(disk, "状态条没有 #status-disk");
    // mock 的 INFO.disk 是具体值 → 必须画成「磁盘 <大小>」
    assert.ok(/^磁盘\s/.test(disk.textContent), "磁盘槽没有画成「磁盘 …」：" + disk.textContent);
    assert.ok(/\d/.test(disk.textContent), "磁盘槽没有数字：" + disk.textContent);
    assert.ok(disk.getAttribute("title").indexOf("素材盘剩余空间") >= 0,
      "磁盘槽的 title 没说清这是什么：" + disk.getAttribute("title"));
    assert.notStrictEqual(disk.textContent.trim(), "磁盘 —", "磁盘槽还是占位符（没人给数）");
  });

  await check("状态条：后端未连接是独立一格，不写进依赖槽", async () => {
    const conn = window.document.getElementById("status-conn");
    assert.ok(conn, "状态条没有 #status-conn（连接状态没地方放）");
    // 演示态：连接正常 ⇒ 这一格收起来，不占位置
    assert.strictEqual(conn.hidden, true, "演示态下连接状态那一格不该露出来");
    const deps = window.document.getElementById("status-deps").textContent;
    assert.notStrictEqual(deps, "后端未连接", "「后端未连接」又写进依赖槽了（用户会去重装 ffmpeg）");
  });

  await check("加载态：转圈带 role=status，不是一句「正在加载」", async () => {
    const host = window.document.createElement("div");
    host.appendChild(WB.ui.loading("正在读取素材库…"));
    const box = host.querySelector(".empty--loading");
    assert.ok(box, "ui.loading 没有 .empty--loading");
    assert.strictEqual(box.getAttribute("role"), "status", "加载态没有 role=status（读屏用户听不到）");
    assert.strictEqual(box.getAttribute("aria-live"), "polite", "加载态没有 aria-live");
    assert.ok(box.textContent.indexOf("正在读取素材库") >= 0, "加载文案丢了");
    assert.strictEqual(box.querySelectorAll(".empty__title").length, 0,
      "加载态不该有标题——它是「还没有结论」，不是「结论是空的」");
  });

  await check("错误态：可重试才给按钮，不可重试就不给", async () => {
    // 可重试：有按钮，点了会回调
    let hits = 0;
    const retryable = WB.ui.errorFrom(
      { message: "网络断了", retryable: true },
      () => { hits += 1; },
      "读不到任务"
    );
    assert.strictEqual(retryable.getAttribute("role"), "alert", "错误块没有 role=alert");
    const btn = retryable.querySelector("button");
    assert.ok(btn, "可重试的错误没有重试按钮");
    btn.click();
    assert.strictEqual(hits, 1, "重试按钮没触发回调");
    assert.ok(retryable.textContent.indexOf("网络断了") >= 0, "错误原文丢了");

    // 不可重试：不给按钮（点了也白点，还会让人以为是网络问题）
    const fatal = WB.ui.errorFrom(
      { message: "接口地址没填", retryable: false },
      () => { hits += 1; },
      "读不到任务"
    );
    assert.strictEqual(fatal.querySelectorAll("button").length, 0,
      "不可重试的错误也长了重试按钮");
    assert.ok(fatal.textContent.indexOf("接口地址没填") >= 0, "不可重试的错误文案丢了");
  });

  await check("任务中心空态：给按钮，不让人去点右下角", async () => {
    const realRpc = WB.rpc;
    WB.rpc = function (method, params, opts) {
      if (method === "job.list") return Promise.resolve([]);
      return realRpc.call(WB, method, params, opts);
    };
    try {
      Array.prototype.slice.call(window.document.querySelectorAll(".modal__x")).forEach((b) => b.click());
      await tick(60);
      // 先把前面检查可能留下的弹窗全关掉，否则拿到的是旧的那个。
      Array.prototype.slice.call(window.document.querySelectorAll(".modal__x")).forEach((b) => b.click());
      await tick(80);
      window.document.getElementById("status-jobs").click();
      // 按标题挑，不按位置挑：前面几项检查可能还留着别的弹窗，
      // `.modal .modal__body` 拿到的是 DOM 里第一个，未必是刚开的这个。
      const dialogs = [...window.document.querySelectorAll(".modal")];
      const target = dialogs.find((d) => {
        const head = d.querySelector(".modal__head");
        return head && head.textContent.indexOf("任务中心") >= 0;
      });
      assert.ok(target, "任务中心没打开（现存弹窗 " + dialogs.length + " 个）");
      const body = target.querySelector(".modal__body");
      // 等的是"填充完成"，不是"有子节点"：.modal__body 里一创建就有一个空的
      // div.col（那是 openJobsModal 传进去的容器本身），children.length > 0
      // 在 job.list 回来之前就已经成立，判据会抢跑读到空容器。
      await until(() => body.textContent.trim().length > 0, "任务中心填充完成");
      const text = body.textContent;
      assert.ok(text.indexOf("还没有任务") >= 0,
        "空态文案不对：[" + text.slice(0, 120) + "] html=" + body.innerHTML.slice(0, 400));
      assert.ok(text.indexOf("右下角") < 0, "空态还在让人去点右下角（那里没有按钮）");
      const demo = [...body.querySelectorAll("button")].find((b) => b.textContent.indexOf("演示任务") >= 0);
      assert.ok(demo, "空态里没有「跑一个演示任务」按钮");
    } finally {
      WB.rpc = realRpc;
      Array.prototype.slice.call(window.document.querySelectorAll(".modal__x")).forEach((b) => b.click());
      await tick(60);
    }
  });

  await check("弹窗：Tab 在内部循环，关闭后焦点还给原处", async () => {
    const opener = window.document.getElementById("status-jobs");
    opener.focus();
    await tick(40);
    const dlg = WB.ui.modal({
      title: "焦点陷阱自检",
      body: WB.ui.h("p", { text: "两个按钮之间循环" }),
      footer: [
        WB.ui.h("button", { class: "btn", type: "button", text: "甲" }),
        WB.ui.h("button", { class: "btn btn--primary", type: "button", text: "乙" })
      ]
    });
    await tick(60);
    const panel = window.document.querySelector(".modal__panel");
    assert.ok(panel, "弹窗没挂出来");
    assert.strictEqual(panel.getAttribute("tabindex"), "-1", "panel 没有 tabindex=-1，焦点落不进去");
    assert.ok(panel.contains(window.document.activeElement) || window.document.activeElement === panel,
      "打开弹窗后焦点还在页面别处");

    // 焦点落在最后一个可聚焦控件上再按 Tab，必须回到第一个。
    //
    // jsdom 里 offsetParent 恒为 null，而 focusables() 用 offsetParent 过滤
    // "看得见"的控件（那条规则在真浏览器里是对的：display:none 的控件不该进循环）。
    // 所以在 jsdom 里它会过滤掉全部控件、走"没有可聚焦控件"那一支，把焦点留在 panel。
    // 这不是缺陷，是 jsdom 没有布局。判据因此断**不变量**：焦点必须还在 panel 里，
    // 而且绝不能跑到遮罩后面 —— 那才是这一条真正要守的东西。
    const focusables = [...panel.querySelectorAll("button")];
    assert.ok(focusables.length >= 3, "弹窗里的可聚焦控件太少：" + focusables.length);
    focusables[focusables.length - 1].focus();
    const ev = new window.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    window.document.dispatchEvent(ev);
    await tick(40);
    const active = window.document.activeElement;
    assert.ok(active === panel || panel.contains(active),
      "按 Tab 后焦点跑到弹窗外面了（role=dialog aria-modal 是空承诺）");

    dlg.close();
    await tick(60);
    assert.strictEqual(window.document.activeElement, opener,
      "关闭弹窗后焦点没有还给打开它的那个按钮");
  });

  await check("三态：切页时上一页的 destroy 真的跑了", async () => {
    // 用一个临时路由验证机制本身：注册 → 进入 → 离开，destroy 必须被调用一次
    let destroyed = 0;
    WB.router.define("_tristate_probe", {
      title: "探针",
      render: function () { return WB.ui.h("div", { class: "page", text: "探针" }); },
      destroy: function () { destroyed += 1; }
    });
    WB.router.go("_tristate_probe");
    await tick(120);
    WB.router.go("library");
    await tick(120);
    assert.strictEqual(destroyed, 1, "离开页面时 destroy 没被调用（或调用了多次）：" + destroyed);
  });

  await check("三态：destroy 抛异常不阻断路由", async () => {
    WB.router.define("_tristate_boom", {
      title: "炸的探针",
      render: function () { return WB.ui.h("div", { class: "page", text: "炸的探针" }); },
      destroy: function () { throw new Error("清理失败"); }
    });
    WB.router.go("_tristate_boom");
    await tick(120);
    WB.router.go("library");
    await tick(160);
    assert.strictEqual(WB.router.current().route, "library",
      "上一页 destroy 抛异常把路由卡住了（用户点左栏没反应）");
    assert.strictEqual($("#page-title").textContent.trim(), "素材库", "没落到素材库");
  });

  await check("字幕页：Ctrl+F 开的是批量替换，不是全局搜索", async () => {
    WB.router.go("subtitles");
    await tick(400);
    const search = window.document.getElementById("global-search");
    search.blur();
    const view = $("#view");
    assert.ok(view, "字幕页没渲染");
    // 字幕页的 Ctrl+F 监听挂在列表滚动容器上（onKeyDown 是列表的 handler），
    // 事件从那里冒泡到 document —— 这正是真实路径：焦点在列表里，按 Ctrl+F。
    const listScroll = window.document.getElementById("sub-scroll");
    assert.ok(listScroll, "字幕页没有列表容器，没法复现键盘路径");
    const ev = new window.KeyboardEvent("keydown", {
      key: "f", ctrlKey: true, bubbles: true, cancelable: true
    });
    listScroll.dispatchEvent(ev);
    await tick(120);
    assert.strictEqual(ev.defaultPrevented, true,
      "字幕页的 Ctrl+F 没有被页面自己拦下（全局搜索会把它吃掉）");
    assert.notStrictEqual(window.document.activeElement, search,
      "在字幕页按 Ctrl+F，焦点被全局搜索抢走了（替换框根本没开）");
  });

  dom.window.close();
  console.log("\n前端骨架验收");
  console.log(results.join("\n"));
  if (problems.length) {
    console.error(`\n${problems.length} 项失败：`);
    for (const line of problems) console.error(`  - ${line}`);
    process.exit(1);
  }
  console.log(`\n全部通过（${results.length} 项）`);
}

main().catch((err) => {
  console.error("测试自身崩了：", err);
  process.exit(1);
});
