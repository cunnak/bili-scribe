/* ============================================================================
 * pages/summary.js —— 总结（P2-07）
 *
 * **这一页的定位是"草稿台"，不是"结果展示页"。** 生成只是起点：
 * 用户可以换角色重出一版、用一句话让模型改（打磨）、跳回任意旧版本。
 * 所以右边永远是当前版本，左边永远有版本列表——"刚才那版更好"是最常见的一句话，
 * 界面上必须让它一键可达。
 *
 * 三条实现约束：
 *
 * 1. **模型输出只当文本渲染。** 简易 Markdown 渲染器一律先建 DOM 节点再填 textContent，
 *    不拼 innerHTML——总结正文是模型产出的，里面出现 `<` 是常态，拼字符串就是把
 *    XSS 的开关交给模型。
 * 2. **生成/打磨都走任务**，一次几分钟是常态，不能卡在 RPC 上。
 * 3. **不自动发布。** 方案书原则 7：AI 产出一律是草稿；发布入口在 P5。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;
  var h = ui.h;

  var POLL_MS = 800;
  var SLOW_MS = 3000;       // 超过 POLL_LIMIT 之后放慢，界面不再密集问一次
  var POLL_LIMIT = 900;     // 12 分钟之后转慢速跟随
  var HARD_LIMIT = 3600;    // 约 2 小时 40 分：到这儿才放手，并说清"还在后台跑"

  var state = {
    projects: [],
    projectId: "",
    roles: [],
    role: "",
    roleDefault: "",     // 后端 summary.roles 给的默认角色（退役角色不在角色列表里）
    roleRetired: [],     // 后端给的退役角色名单：界面靠它区分「已停用」与「不认识」
    roleNotice: "",      // 历史行存的是已退役角色时，写清楚「这次换成哪个角色重做」
    models: [],
    model: "",           // "" = 跟随设置（llm.tasks.summary → llm.model）
    modelDefault: "",
    found: false,
    jobProgress: 0,
    jobStage: "",
    detached: false,     // 前端不再等（任务仍在后台跑），用来跳过"好了"的收尾
    summary: null,
    versions: [],
    totals: null,   // 这条视频累计用量（后端按全部版本求和）
    busy: false,
    jobId: "",
    requirement: "",
    error: "",
    note: "",
    // P2-08 合集模式：单条视频 / 整个合集共用这一页，靠 mode 切换。
    // 合集没有"打磨"（改一句要求就重排所有子项不现实），所以左栏换成子项清单。
    mode: "video",
    collections: [],
    collection: "",
    overview: null,
    overviewVersions: [],
    children: [],
    collBusy: false,
    // 轻量模式：只输 av/BV 拿字幕，不下载视频。它与「本地项目」是同一个标签页里的
    // 两种素材来源，不是第三个标签——标签切的是"总结一条还是一整个合集"，
    // 来源切的是"素材从哪来"，两个维度混在一个按钮条里会让人分不清。
    source: "local",
    lightUrl: "",
    lightBusy: false,
    lightInfo: null,
    lightSubs: [],
    lightLang: "",
    lightSrt: "",
    lightNote: "",
    lightError: ""
  };

  function call(method, params) {
    return WB.rpc(method, params || {}, { silent: true });
  }

  function project() {
    for (var i = 0; i < state.projects.length; i += 1) {
      if (state.projects[i].id === state.projectId) return state.projects[i];
    }
    return null;
  }

  // chars 仍然导出在 _internals 里（前端用例拿它算字数），所以别删——
  // 删了会让页面在加载期 ReferenceError，整页白屏。
  function chars(text) {
    return String(text || "").replace(/\s+/g, "").length;
  }

  function whenDay(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return String(iso);
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return (date.getMonth() + 1) + "-" + pad(date.getDate()) + " " +
      pad(date.getHours()) + ":" + pad(date.getMinutes());
  }

  // --------------------------------------------------------------- 简易 Markdown
  //
  // 只认总结里真的会出现的那几种：标题、列表、引用、加粗、行内代码、分割线。
  // 认识得少不是偷懒——认识得越多，越容易把模型的原文"改写得像"界面自己说的话。

  var INLINE = /(\*\*[^*]+\*\*|`[^`]+`)/g;

  function inline(text) {
    var host = document.createDocumentFragment();
    var rest = String(text || "");
    var match;
    var at = 0;
    INLINE.lastIndex = 0;
    while ((match = INLINE.exec(rest)) !== null) {
      if (match.index > at) {
        host.appendChild(document.createTextNode(rest.slice(at, match.index)));
      }
      var token = match[0];
      if (token.charAt(0) === "*") {
        host.appendChild(h("strong", { text: token.slice(2, -2) }));
      } else {
        host.appendChild(h("code", { text: token.slice(1, -1) }));
      }
      at = match.index + token.length;
    }
    if (at < rest.length) host.appendChild(document.createTextNode(rest.slice(at)));
    return host;
  }

  function markdown(text) {
    var host = h("div", { class: "md" });
    var lines = String(text || "").split(/\r?\n/);
    var list = null;

    function flush() {
      if (list) { host.appendChild(list); list = null; }
    }

    lines.forEach(function (line) {
      var trimmed = line.trim();
      if (!trimmed) { flush(); return; }
      if (/^---+$/.test(trimmed)) {
        flush();
        host.appendChild(h("hr", { class: "md__rule" }));
        return;
      }
      var head = /^(#{1,4})\s+(.*)$/.exec(trimmed);
      if (head) {
        flush();
        var level = Math.min(4, head[1].length + 1);   // 正文里的 ## 不再抢 h1
        host.appendChild(h("h" + level, { class: "md__h", text: head[2] }));
        return;
      }
      if (/^[-*]\s+/.test(trimmed)) {
        if (!list) list = h("ul", { class: "md__list" });
        var item = h("li", { class: "md__item" });
        item.appendChild(inline(trimmed.replace(/^[-*]\s+/, "")));
        list.appendChild(item);
        return;
      }
      if (/^>\s?/.test(trimmed)) {
        flush();
        var quote = h("blockquote", { class: "md__quote" });
        quote.appendChild(inline(trimmed.replace(/^>\s?/, "")));
        host.appendChild(quote);
        return;
      }
      flush();
      var para = h("p", { class: "md__p" });
      para.appendChild(inline(trimmed));
      host.appendChild(para);
    });
    flush();
    return host;
  }

  // --------------------------------------------------------------- 数据

  function loadProjects() {
    return call("library.list", { limit: 200, offset: 0 }).then(function (data) {
      state.projects = (data && data.items) || [];
      if (!state.projectId && state.projects.length) {
        // 默认挑"有字幕的"第一个：没字幕的项目点了也只会得到一条"先转录"。
        var ready = state.projects.filter(function (item) { return item.has_transcript; });
        state.projectId = (ready[0] || state.projects[0]).id;
      }
      return state.projects;
    });
  }

  function findRole(key) {
    for (var i = 0; i < state.roles.length; i += 1) {
      if (state.roles[i].key === key) return state.roles[i];
    }
    return null;
  }

  // 版本行与总览脚注里的角色：现役 key 原样显示（那是后端存进库里的值，改动它
  // 等于篡改出处）；下拉框里已经选不到的 key（退役或脏数据）标出来，
  // 既不抛错也不留白——和适配器 to_markdown 那一行同一个口径。
  // 但「已停用」和「不认识」是两件事：前者要换角色，后者要改配置，界面不能混着说。
  // 退役名单来自后端 summary.roles.retired，前端不硬编码具体 key（下次退役不用改这里）。
  function roleText(key) {
    var wanted = String(key || "");
    if (!wanted || findRole(wanted)) return wanted;
    if (state.roleRetired.indexOf(wanted) >= 0) return wanted + "（已停用）";
    return wanted + "（未知角色）";
  }

  // 历史版本行里存的可能是已经退役的角色（standard）。直接把那个 key 交回后端，
  // summary_start 每次都报「没有这个角色」，用户点一次错一次。这里换成可选项里的
  // 角色，并把换掉这件事写在界面上——静默替换会让人以为文风变了是自己选的。
  function adoptRole(key) {
    var wanted = String(key || "");
    if (!wanted) return;
    if (!state.roles.length) {   // 角色列表还没到（理论上不会发生：boot 先 loadRoles）
      state.role = wanted;
      return;
    }
    var hit = findRole(wanted);
    if (hit) {
      state.role = hit.key;
      state.roleNotice = "";
      return;
    }
    var fallback = findRole(state.roleDefault) || state.roles[0] || null;
    if (!fallback) return;
    state.role = fallback.key;
    // 措辞跟着原因走：退役＝我们撤掉了它，不认识＝这个 key 从来不在列表里（多半是配置写错
    // 后存下来的），两种情况用户要做的事不一样。名单同样来自后端，不在这儿硬编码。
    var why = state.roleRetired.indexOf(wanted) >= 0 ? "已停用" : "已经不在角色列表里";
    state.roleNotice = "上一版的角色「" + wanted + "」" + why + "，重新生成改用「" + fallback.name + "」";
  }

  function loadRoles() {
    call("summary.models").then(function (data) {
      state.models = (data && data.items) || [];
      state.modelDefault = (data && data.default) || "";
    }).catch(function () {
      state.models = [];
    });

    return call("summary.roles").then(function (data) {
      state.roles = (data && data.roles) || [];
      // 退役名单与提示都由后端给（判定只在 core/summarizer.resolve_role 那一处）；
      // 老后端不带这两个字段时就退化成空名单 + 不提示，界面照常能用。
      state.roleRetired = (data && data.retired) || [];
      // default 来自配置，配置里可能还留着已退役的角色——不能直接当选中项用，
      // 统一过一遍 adoptRole：可选项就收下，不可选项换成列表里的第一个。
      state.roleDefault = (data && data.default) || "";
      adoptRole(state.role || state.roleDefault);
      return state.roles;
    });
  }

  function loadCollections() {
    return call("collection.list").then(function (data) {
      state.collections = (data && data.collections) || [];
      if (!state.collection && state.collections.length) {
        state.collection = state.collections[0].title;
      }
      return state.collections;
    });
  }

  function currentCollection() {
    for (var i = 0; i < state.collections.length; i += 1) {
      if (state.collections[i].title === state.collection) return state.collections[i];
    }
    return null;
  }

  function loadCollection() {
    if (!state.collection) {
      state.overview = null;
      state.overviewVersions = [];
      state.children = [];
      return Promise.resolve(null);
    }
    return call("summary.collection.get", { id: state.collection }).then(function (data) {
      state.overview = (data && data.overview) || null;
      state.overviewVersions = (data && data.versions) || [];
      state.children = (data && data.children) || [];
      adoptRole(state.overview && state.overview.role);
      return state.overview;
    });
  }

  function loadSummary() {
    if (!state.projectId) {
      state.found = false;
      state.summary = null;
      state.versions = [];
      return Promise.resolve(null);
    }
    state.loading = true;
    return call("summary.get", { id: state.projectId }).then(function (data) {
      state.loading = false;
      state.found = !!(data && data.found);
      state.summary = (data && data.summary) || null;
      state.versions = (data && data.versions) || [];
      state.totals = (data && data.totals) || null;
      adoptRole(state.summary && state.summary.role);
      return state.summary;
    }).catch(function (err) {
      // 不清 loading 就会永远停在"读取中"——错误条在下面照样画得出来，
      // 但正文区会一直是转圈，用户以为还没读完。
      state.loading = false;
      throw err;
    });
  }

  // 轮询节奏与"什么时候放手"抽成纯函数：这两条曾是现场 bug（到 12 分钟就
  // 报"等太久了，先停在这里"，用户以为失败、其实后台还在跑），要能被测住。
  function pollDelay(attempts) { return attempts > POLL_LIMIT ? SLOW_MS : POLL_MS; }
  function pollGaveUp(attempts) { return attempts > HARD_LIMIT; }

  // 离开页面时把在飞的那条轮询作废：以前只有 state.projectId 变了才会自己停，
  // 而 destroy 之后 DOM 已经不在，轮询还会每 1.2 秒往 #sum-note 写一次进度。
  var pollToken = 0;

  function pollJob(jobId) {
    var attempts = 0;
    var myToken = pollToken;
    return new Promise(function (resolve, reject) {
      function tick() {
        if (myToken !== pollToken) { resolve(null); return; }
        attempts += 1;
        call("job.get", { id: jobId }).then(function (job) {
          if (myToken !== pollToken) { resolve(null); return; }
          if (!job) { reject(new Error("任务不见了")); return; }
          if (job.status === "succeeded") { resolve(job); return; }
          if (job.status === "failed" || job.status === "canceled") {
            // 行上只有 error_code / error_message 两列（没有 `job.error` 这个键，
            // `pragma table_info(wb_job)` 现读），失败原因与 detail 原文由后端拼在
            // error_message 一句里（app/jobs.py 的 error_message_for_row）。
            // 以前这里读 `job.error.message` ⇒ 恒 undefined ⇒ 用户只看得到写死的
            // 「生成失败」，`finish_reason=length…` 那句被丢掉。前端不再拼第二次，
            // 免得同一句话两个 owner。
            var error = new Error(job.error_message || "生成失败");
            error.job = job;
            reject(error);
            return;
          }
          // 任务真的能跑很久（两小时视频的三轮回 20 分钟是常态）。以前到 12 分钟就
          // reject 成"等太久了，先停在这里"，界面看着像失败、其实后台还在跑——现在
          // 改成放慢节奏继续跟，只有到 HARD_LIMIT 才松手，并且**不当成失败**。
          if (pollGaveUp(attempts)) {
            state.detached = true;
            state.note = "任务还在后台跑（去任务中心看进度），这里不再等";
            paintNote();
            resolve(null);
            return;
          }
          state.note = (job.stage || "生成中") + " " + Math.round((job.progress || 0) * 100) + "%"
            + (attempts > POLL_LIMIT ? " · 已跑 " + Math.round(attempts * SLOW_MS / 60000) + " 分钟" : "");
          state.jobProgress = job.progress || 0;
          state.jobStage = job.stage || "";
          paintNote();
          paintProgress();
          setTimeout(tick, pollDelay(attempts));
        }).catch(reject);
      }
      tick();
    });
  }

  // 给前端测试用的两个纯函数（轮询节奏 / 何时放手）
  if (WB) {
    WB._summaryPoll = {
      delay: pollDelay,
      gaveUp: pollGaveUp,
      limit: POLL_LIMIT,
      hard: HARD_LIMIT,
      slow: SLOW_MS
    };
  }

  // --------------------------------------------------------------- 动作

  function start() {
    if (!state.projectId || state.busy) return;
    var target = project();
    // 只有在**明确知道**没字幕时才拦。字段缺失（老数据/别的来源）不该被当成"没字幕"——
    // 那样用户会看到一句"先去转录"，而后端其实有字幕，白白多跑一趟。
    if (target && target.has_transcript === false) {
      state.error = "这条视频还没有字幕。总结只用素材里的信息，先去转录。";
      paint();
      return;
    }
    state.busy = true;
    state.error = "";
    state.note = "正在排队…";
    paint();
    call("summary.start", { project_id: state.projectId, role: state.role, model: state.model })
      .then(function (data) {
        state.jobId = (data.job && data.job.id) || "";
        return pollJob(state.jobId);
      })
      .then(function () { return state.detached ? null : loadSummary(); })
      .then(function () {
        state.busy = false;
        state.jobId = "";
        if (state.detached) { state.detached = false; paint(); return; }
        state.note = "";
        WB.toast("总结好了（第 " + ((state.summary && state.summary.version) || 1) + " 版）");
        paint();
      })
      .catch(function (error) {
        state.busy = false;
        state.note = "";
        state.jobId = "";
        state.error = error && error.message ? error.message : "生成失败";
        paint();
      });
  }

  function refine() {
    var demand = String(state.requirement || "").trim();
    if (!state.projectId || state.busy) return;
    if (demand.length < 4) {
      state.error = "说一下你想怎么改，比如「要点压到三条」「少用术语」。";
      paint();
      return;
    }
    state.busy = true;
    state.error = "";
    state.note = "正在按你的要求改…";
    paint();
    call("summary.refine", { project_id: state.projectId, requirement: demand, model: state.model })
      .then(function (data) {
        state.jobId = (data.job && data.job.id) || "";
        return pollJob(state.jobId);
      })
      .then(function () { return state.detached ? null : loadSummary(); })
      .then(function () {
        state.busy = false;
        state.jobId = "";
        if (state.detached) { state.detached = false; paint(); return; }
        state.note = "";
        state.requirement = "";
        WB.toast("改好了");
        paint();
      })
      .catch(function (error) {
        state.busy = false;
        state.note = "";
        state.jobId = "";
        state.error = error && error.message ? error.message : "改不动";
        paint();
      });
  }

  function restore(summaryId, version) {
    if (state.busy) return;
    state.busy = true;
    state.error = "";
    call("summary.restore", { project_id: state.projectId, summary_id: summaryId })
      .then(function () { return loadSummary(); })
      .then(function () {
        state.busy = false;
        WB.toast("已恢复第 " + version + " 版（存成了新的一版，旧版都还在）");
        paint();
      })
      .catch(function (error) {
        state.busy = false;
        state.error = error && error.message ? error.message : "恢复失败";
        paint();
      });
  }

  function reveal() {
    var row = project();
    if (row && row.root_dir) call("fs.reveal", { path: row.root_dir });
  }

  // --------------------------------------------------------------- 渲染

  function switchMode(mode) {
    if (state.mode === mode) return;
    state.mode = mode;
    state.error = "";
    state.note = "";
    if (WB.router && WB.router.go) WB.router.go("summary");
  }

  function pickCollection(title) {
    state.collection = title;
    state.error = "";
    state.note = "";
    loadCollection().then(paint).catch(function (error) {
      state.error = error && error.message ? error.message : "读不到合集总览";
      paint();
    });
  }

  function startCollection() {
    if (!state.collection || state.collBusy) return;
    var box = currentCollection();
    if (box && !box.ready) {
      // 先在前端拦一道：后端也会拦，但用户不该为了看一句话等一次 RPC。
      state.error = "这个合集还有 " + (box.items - box.summarized) + " 集没总结。先逐条生成，总览才有素材。";
      paint();
      return;
    }
    state.collBusy = true;
    state.error = "";
    state.note = "正在读子项总结…";
    paint();
    call("summary.collection.overview", { collection: state.collection, role: state.role, model: state.model })
      .then(function (data) {
        return pollJob(data.job.id).then(function (job) {
          return { job: job, items: data.items };
        });
      })
      .then(function () { return state.detached ? null : loadCollections(); })
      .then(function () { return state.detached ? null : loadCollection(); })
      .then(function () {
        state.collBusy = false;
        if (state.detached) { state.detached = false; paint(); return; }
        state.note = "";
        paint();
        WB.ui.toast("总览已生成");
      })
      .catch(function (error) {
        state.collBusy = false;
        state.note = "";
        state.error = error && error.message ? error.message : "生成失败";
        paint();
      });
  }

  function paintNote() {
    var node = document.getElementById("sum-note");
    if (node) node.textContent = state.note || "";
  }

  function pickProject(id) {
    state.projectId = id;
    state.error = "";
    state.note = "";
    loadSummary().then(paint).catch(function (error) {
      state.error = error && error.message ? error.message : "读不到总结";
      paint();
    });
  }

  // head() 里的下拉和按钮都读 state，而 state 是异步填的：render() 先建 DOM、
  // 数据后到，于是首屏的按钮永远是灰的。每次 paint() 重建这一块，状态跟着数据走。
  function swapHead() {
    var holder = document.getElementById("sum-head");
    if (!holder) return;
    holder.textContent = "";
    holder.appendChild(head());
  }

  function tabsBar() {
    function tab(mode, text, hint) {
      return ui.h("button", {
        class: "sum-tab" + (state.mode === mode ? " is-active" : ""),
        type: "button",
        title: hint,
        text: text,
        onclick: function () { switchMode(mode); }
      });
    }
    return ui.h("div", { class: "sum-tabs" }, [
      tab("video", "单条视频", "一次总结一条视频，可打磨、可回溯版本"),
      tab("collection", "合集", "把整个合集的子项总结压成一份总览")
    ]);
  }

  function collHead() {
    var options = state.collections.map(function (item) {
      return ui.h("option", {
        value: item.title,
        text: item.title + "（" + item.summarized + "/" + item.items + " 集有总结）",
        selected: item.title === state.collection ? "selected" : null
      });
    });
    var picker = ui.h("select", {
      class: "input input--select",
      id: "sum-collection",
      onchange: function (event) { pickCollection(event.target.value); }
    }, options);
    if (!options.length) {
      picker.appendChild(ui.h("option", { value: "", text: "（还没有合集：同一合集下载两条以上就会分组）" }));
    }

    var roleOptions = state.roles.map(function (item) {
      return ui.h("option", {
        value: item.key,
        text: item.name + (item.passes >= 3 ? "（三轮）" : ""),
        selected: item.key === state.role ? "selected" : null
      });
    });
    var rolePick = ui.h("select", {
      class: "input input--select",
      id: "sum-role",
      title: "角色只影响这一份总览的文风，不会改动子项总结",
      onchange: function (event) { state.role = event.target.value; state.roleNotice = ""; }
    }, roleOptions);

    var box = currentCollection();
    var actions = ui.h("div", { class: "page__actions" }, [
      rolePick,
      modelSelect("sum-coll-model"),
      ui.h("button", {
        class: "btn btn--primary",
        id: "sum-coll-start",
        text: state.collBusy ? "生成中…" : (state.overview ? "重新生成总览" : "生成总览"),
        disabled: state.collBusy || !state.collection ? "disabled" : null,
        onclick: startCollection
      }),
      ui.h("button", {
        class: "btn btn--ghost",
        text: "在文件夹中显示",
        disabled: state.collection ? null : "disabled",
        onclick: function () {
          var first = state.children[0];
          if (!first) return;
          WB.rpc("fs.reveal", { project_id: first.id }).catch(function () { /* 打开失败不拦人 */ });
        }
      })
    ]);
    if (box && !box.ready) {
      actions.appendChild(ui.h("span", {
        class: "muted",
        text: "还有 " + (box.items - box.summarized) + " 集没总结"
      }));
    }

    return ui.h("div", { class: "page__head" }, [
      ui.h("h2", { class: "page__title", text: "合集总览" }),
      ui.h("p", {
        class: "page__hint",
        text: "只读子项总结，不重读视频、不改子项；不满意就换个角色重出一版"
      }),
      ui.h("div", { class: "sum-pick" }, [picker]),
      actions
    ]);
  }

  function shortSpec(spec) {
    var text = String(spec || "");
    var cut = text.indexOf("/");
    return cut > 0 ? text.slice(cut + 1) : text;
  }

  function modelSelect(id) {
    var hint = state.modelDefault
      ? "跟随设置 · " + shortSpec(state.modelDefault)
      : "跟随设置";
    var options = [ui.h("option", {
      value: "",
      text: hint,
      selected: state.model ? null : "selected"
    })];
    state.models.forEach(function (item) {
      options.push(ui.h("option", {
        value: item.spec,
        text: item.provider_name + " · " + item.model,
        selected: item.spec === state.model ? "selected" : null
      }));
    });
    if (!state.models.length) {
      options.push(ui.h("option", { value: "", text: "（还没有配置服务商）", disabled: "disabled" }));
    }
    return ui.h("select", {
      class: "input input--select",
      id: id || "sum-model",
      title: "这一版用哪个模型写；换个模型重出一版就能对比",
      onchange: function (event) {
        state.model = event.target.value;
        render();
      }
    }, options);
  }
  function docHead() {
    var item = project() || {};
    var box = [];
    if (item.up_name) box.push(ui.h("span", {}, [ui.h("em", { text: "UP " }), document.createTextNode(item.up_name)]));
    if (item.stage) box.push(ui.h("span", {}, [ui.h("em", { text: "素材 " }),
      document.createTextNode(item.has_transcript ? "字幕" : "无字幕")]));
    if (state.versions.length) {
      box.push(ui.h("span", {}, [ui.h("em", { text: "第 " + ((state.summary && state.summary.version) || state.versions.length) + " 版 · " }),
        document.createTextNode("共 " + state.versions.length + " 版")]));
    }
    if (state.role) {
      var picked = findRole(state.role);
      box.push(ui.h("span", {}, [ui.h("em", { text: (picked ? picked.name : state.role) }),
        document.createTextNode(picked && picked.passes >= 3 ? " · 三轮" : "")]));
    }
    if (state.roleNotice) {
      // 只用现有的 chip 结构（.sum-chips > span / em），不新造 class——CSS 不在本次改动范围。
      box.push(ui.h("span", { title: "后端已不再提供这个角色；历史版本仍按原角色存档，只是不能再原样重做。" }, [
        ui.h("em", { text: "注意 " }),
        document.createTextNode(state.roleNotice)
      ]));
    }
    box.push(ui.h("span", { text: whenDay(new Date().toISOString()) }));
    return ui.h("div", { class: "sum-hd" }, [
      ui.h("i"),
      ui.h("div", {}, [
        ui.h("h1", { id: "sum-title", text: item.title || "还没选素材" }),
        ui.h("div", { class: "sum-chips", id: "sum-chips" }, box)
      ])
    ]);
  }

  function progressBlock() {
    var active = state.busy || !!state.jobId;
    if (!active) return ui.h("span", { id: "sum-prog", style: "display:none" });
    var pct = Math.max(0, Math.min(100, Math.round((state.jobProgress || 0) * 100)));
    var filled = Math.round(pct / 100 * 8);
    var segs = [];
    for (var i = 0; i < 8; i += 1) {
      var cls = i < filled ? "on" : (i === filled ? "cur" : "");
      segs.push(ui.h("i", { class: cls }));
    }
    return ui.h("div", { class: "sum-prog", id: "sum-prog" }, [
      ui.h("div", { class: "sum-seg" }, segs),
      ui.h("div", { class: "sum-prow" }, [
        ui.h("span", { class: "l", id: "sum-stage", text: state.jobStage || state.note || "生成中" }),
        ui.h("span", { class: "r", id: "sum-pct", text: pct + "%" })
      ])
    ]);
  }

  function warnBlock() {
    var item = project() || {};
    if (!item.has_transcript) return null;
    return ui.h("div", { class: "sum-warn" }, [
      ui.h("div", { class: "wt", text: "采集提示" }),
      ui.h("p", {
        text: "总结只依据这条视频的字幕文字；语音识别有同音字与断词误差，"
          + "专有名词、数字、人名建议回看视频核对后再用。"
      })
    ]);
  }

  function switchSource(src) {
    var next = src === "light" ? "light" : "local";
    if (state.source === next) return;
    state.source = next;
    state.error = "";
    state.note = "";
    state.lightError = "";
    paint();
  }

  function lightBasename(path) {
    var text = String(path || "");
    var cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
    return cut >= 0 ? text.slice(cut + 1) : text;
  }

  function sourceBar() {
    function chip(key, text, hint) {
      return ui.h("button", {
        class: "sum-src" + (state.source === key ? " is-active" : ""),
        type: "button",
        id: "sum-src-" + key,
        title: hint,
        text: text,
        onclick: function () { switchSource(key); }
      });
    }
    return ui.h("div", { class: "sum-srcs" }, [
      chip("local", "本地项目", "用已经下载/导入的项目自己的字幕总结"),
      chip("light", "轻量", "只输 av/BV 号取 B站字幕，或选一个字幕文件，不下载视频")
    ]);
  }

  function probe() {
    var url = String(state.lightUrl || "").trim();
    if (!url || state.lightBusy) return;
    state.lightBusy = true;
    state.lightError = "";
    state.lightNote = "正在解析…";
    paint();
    call("summary.light.probe", { url: url })
      .then(function (data) {
        state.lightBusy = false;
        state.lightInfo = data || null;
        state.lightSubs = (data && data.subtitles) || [];
        state.lightLang = state.lightSubs.length ? state.lightSubs[0].lan : "";
        state.lightNote = (data && data.note) || "";
        if (!state.lightSubs.length && !state.lightNote) {
          state.lightNote = "这个视频没有字幕，选一个字幕文件也能总结。";
        }
        paint();
      })
      .catch(function (error) {
        state.lightBusy = false;
        state.lightNote = "";
        state.lightError = error && error.message ? error.message : "解析失败";
        paint();
      });
  }

  function pickSrt() {
    call("fs.pick", { kind: "file" })
      .then(function (data) {
        var path = (data && (data.path || data.value)) || "";
        if (!path) return;
        state.lightSrt = String(path);
        state.lightError = "";
        paint();
      })
      .catch(function (error) {
        state.lightError = error && error.message ? error.message : "选不了文件";
        paint();
      });
  }

  /** 取素材落成项目，然后**复用 start()** 的排队与轮询——不复制第二份。 */
  function lightStart() {
    if (state.lightBusy || state.busy) return;
    var url = String(state.lightUrl || "").trim();
    var path = String(state.lightSrt || "");
    if (!url && !path) {
      state.lightError = "先输一个 av/BV 号，或选一个字幕文件。";
      paint();
      return;
    }
    state.lightBusy = true;
    state.lightError = "";
    state.note = "正在取字幕…";
    paint();
    call("summary.light.prepare", { url: url, lang: state.lightLang, path: path })
      .then(function (data) {
        state.lightBusy = false;
        state.projectId = (data && data.project_id) || "";
        var warns = (data && data.warnings) || [];
        if (warns.length) WB.toast(warns[0]);
        // 新项目要进下拉，否则本地模式的列表里看不到它
        return loadProjects().then(function () { return data; });
      })
      .then(function (data) {
        if (!state.projectId) {
          state.lightError = "没取到可总结的项目";
          paint();
          return null;
        }
        state.note = "";
        start();
        return data;
      })
      .catch(function (error) {
        state.lightBusy = false;
        state.note = "";
        state.lightError = error && error.message ? error.message : "取字幕失败";
        paint();
      });
  }

  function lightHead() {
    var info = state.lightInfo;
    var subs = state.lightSubs || [];
    var subOptions = subs.map(function (item) {
      return ui.h("option", {
        value: item.lan,
        text: (item.lan_doc || item.lan) + (item.is_ai ? " · AI" : ""),
        selected: item.lan === state.lightLang ? "selected" : null
      });
    });
    var input = ui.h("input", {
      class: "input",
      id: "sum-light-url",
      type: "text",
      value: state.lightUrl || "",
      placeholder: "BV / av 号、视频链接，回车解析",
      oninput: function (event) { state.lightUrl = event.target.value; },
      onkeydown: function (event) {
        if (event.key === "Enter") { state.lightUrl = event.target.value; probe(); }
      }
    });
    var rows = [
      ui.h("label", { class: "sum-fld", title: "只解析、不下载：拿到这条视频的字幕清单" }, [
        ui.h("span", { class: "k", text: "AV / BV" }), input
      ])
    ];
    if (subs.length) {
      rows.push(ui.h("label", { class: "sum-fld", title: "选一条字幕轨；AI 字幕有同音字风险" }, [
        ui.h("span", { class: "k", text: "字幕" }),
        ui.h("select", {
          id: "sum-light-subs",
          onchange: function (event) { state.lightLang = event.target.value; }
        }, subOptions)
      ]));
    }
    rows.push(ui.h("button", {
      class: "sum-ghost",
      id: "sum-light-probe",
      type: "button",
      text: state.lightBusy ? "解析中…" : "解析",
      disabled: state.lightBusy ? "disabled" : null,
      onclick: probe
    }));
    rows.push(ui.h("button", {
      class: "sum-ghost",
      id: "sum-light-file",
      type: "button",
      title: "用本地 srt 文件总结，不需要登录 B站",
      text: state.lightSrt ? "换一个文件…" : "选字幕文件…",
      onclick: pickSrt
    }));
    if (state.lightSrt) {
      rows.push(ui.h("span", {
        class: "muted", id: "sum-light-srt", text: lightBasename(state.lightSrt)
      }));
    }
    rows.push(ui.h("button", {
      class: "sum-go",
      id: "sum-light-go",
      type: "button",
      text: state.lightBusy ? "取字幕中…" : "提取字幕并总结",
      disabled: state.lightBusy || state.busy ? "disabled" : null,
      onclick: lightStart
    }));
    if (info && info.title) {
      rows.push(ui.h("span", {
        class: "muted", id: "sum-light-info",
        text: info.title + (info.up_name ? " · " + info.up_name : "")
      }));
    }
    if (state.lightNote) {
      rows.push(ui.h("span", { class: "muted", id: "sum-light-note", text: state.lightNote }));
    }
    // 错误要真的出现在界面上。曾经只写进 state.lightError 而这里没渲染，
    // 于是「什么都不填就点提取」看起来像按钮坏了——点了，没有任何反应。
    if (state.lightError) {
      rows.push(ui.h("span", { class: "sum-light-err", id: "sum-light-error", text: state.lightError }));
    }
    return ui.h("div", { class: "page__head sum-head sum-light" }, [
      sourceBar(),
      ui.h("div", { class: "sum-fields" }, rows)
    ]);
  }

  function head() {
    if (state.mode === "collection") return collHead();
    if (state.source === "light") return lightHead();

    function field(id, key, title, options, onchange) {
      var sel = ui.h("select", { id: id, onchange: function (event) { onchange(event.target.value); } },
        options);
      if (!options.length) sel.appendChild(ui.h("option", { value: "", text: "（空）" }));
      return ui.h("label", { class: "sum-fld", title: title }, [
        ui.h("span", { class: "k", text: key }),
        sel
      ]);
    }

    var projectOptions = state.projects.map(function (item) {
      return ui.h("option", {
        value: item.id,
        text: item.title + (item.has_transcript ? "" : "（无字幕）"),
        selected: item.id === state.projectId ? "selected" : null
      });
    });
    var roleOptions = state.roles.map(function (item) {
      return ui.h("option", {
        value: item.key,
        text: item.name + (item.passes >= 3 ? " · 三轮" : ""),
        selected: item.key === state.role ? "selected" : null
      });
    });

    return ui.h("div", { class: "page__head sum-head" }, [
      sourceBar(),
      ui.h("div", { class: "sum-fields" }, [
        field("sum-project", "PROJECT", "选一条素材，总结只用它自己的字幕",
          projectOptions, pickProject),
        field("sum-role", "ROLE", "角色决定用哪套方法论写这份总结",
          roleOptions, function (value) { state.role = value; state.roleNotice = ""; }),
        modelSelect(),
        ui.h("button", {
          class: "sum-go",
          id: "sum-start",
          text: state.busy ? "生成中…" : "开始总结",
          disabled: state.busy || !state.projectId ? "disabled" : null,
          onclick: start
        }),
        ui.h("button", {
          class: "sum-ghost",
          id: "sum-goto-publish",
          title: "跳到正文下面的发布卡（草稿、三关门禁、发布与撤回）",
          text: "去发布",
          onclick: gotoPublish
        }),
        ui.h("button", {
          class: "sum-ghost",
          text: "在文件夹中显示",
          disabled: state.projectId ? null : "disabled",
          onclick: reveal
        })
      ])
    ]);
  }

  function versionsCard() {
    var card = ui.h("div", { class: "card" });
    var current = state.summary ? state.summary.id : "";
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "版本 · VERSION" }),
      ui.h("span", { text: state.versions.length ? "共 " + state.versions.length + " 版" : "" })
    ]));
    if (!state.versions.length) {
      card.appendChild(ui.h("p", { class: "ver-empty", text: "还没有版本。生成一次就有了；之后每次「按这个改」都会新增一版，旧的不会被覆盖。" }));
      return card;
    }
    var list = ui.h("div", { class: "ver-list", id: "sum-versions" });
    state.versions.forEach(function (item) {
      var active = item.id === current;
      var meta = [whenDay(item.created_at), item.model, (item.chars || 0) + " 字"]
        .filter(function (part) { return !!part; }).join(" · ");
      var row = ui.h("div", {
        class: "ver-row" + (active ? " is-active" : ""),
        "data-version": String(item.version),
        onclick: active ? null : function () { restore(item.id, item.version); }
      }, [
        ui.h("div", { class: "ver-row__body" }, [
          ui.h("div", { class: "ver-row__title" }, [
            document.createTextNode("第 " + item.version + " 版"),
            active ? ui.h("em", { text: "当前" }) : null
          ]),
          ui.h("div", { class: "ver-row__meta", text: meta }),
          item.revise_note
            ? ui.h("div", { class: "ver-row__note" }, [
              ui.h("b", { text: "实际改动" }),
              document.createTextNode(String(item.revise_note))
            ])
            : null
        ]),
        active ? null : ui.h("button", {
          class: "btn btn--ghost btn--sm",
          text: "恢复",
          disabled: state.busy ? "disabled" : null,
          onclick: function (event) {
            if (event && event.stopPropagation) event.stopPropagation();
            restore(item.id, item.version);
          }
        })
      ]);
      list.appendChild(row);
    });
    card.appendChild(list);
    return card;
  }

  // ---- 用量：这一版花了多少、这条视频一共花了多少 -------------------
  //
  // 数字只在有记录时显示。老版本（只有 calls 的）显示已记录的那几项，
  // 缺的写「—」——不拿 0 冒充「没花」。
  function groupNum(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return "—";
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function groupSeconds(value) {
    var n = Number(value);
    if (!isFinite(n) || n <= 0) return "—";
    if (n < 90) return (Math.round(n * 10) / 10).toFixed(1) + "s";
    var total = Math.round(n);
    var m = Math.floor(total / 60);
    var sec = total % 60;
    return m + "m" + (sec < 10 ? "0" : "") + sec + "s";
  }

  function costRow(label, value, extra) {
    return ui.h("div", { class: "sum-cost__row" + (extra ? " " + extra : "") }, [
      ui.h("span", { class: "sum-cost__label", text: label }),
      ui.h("span", { class: "sum-cost__value", text: value })
    ]);
  }

  function costCard() {
    var usage = (state.summary && state.summary.usage) || {};
    var totals = state.totals || {};
    var hasOwn = Number(usage.calls || 0) > 0;
    if (!hasOwn && !Number(totals.calls || 0)) return null;
    var card = ui.h("div", { class: "card", id: "sum-cost" });
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "用量 · TOKENS" }),
      ui.h("span", { text: state.summary && state.summary.model ? String(state.summary.model) : "" })
    ]));
    card.appendChild(ui.h("div", { class: "sum-cost__head", text: "这一版开销" }));
    card.appendChild(costRow("输入 tokens", groupNum(usage.prompt_tokens)));
    card.appendChild(costRow("输出 tokens", groupNum(usage.completion_tokens)));
    card.appendChild(costRow("调用次数", hasOwn ? String(usage.calls) + " 次" : "—"));
    card.appendChild(costRow("耗时", groupSeconds(usage.seconds)));
    card.appendChild(costRow("素材预读", usage.chunks ? String(usage.chunks) + " 段" : "—"));
    if (Number(totals.calls || 0) > 0) {
      card.appendChild(ui.h("div", { class: "sum-cost__head", text: "这条视频累计" }));
      card.appendChild(costRow("模型调用", String(totals.calls) + " 次"));
      card.appendChild(costRow("输入合计", groupNum(totals.prompt_tokens)));
      card.appendChild(costRow("输出合计", groupNum(totals.completion_tokens)));
      card.appendChild(costRow("总 tokens", groupNum(totals.total_tokens), "is-total"));
      if (totals.versions) {
        card.appendChild(ui.h("div", { class: "sum-cost__foot", text:
          "共 " + totals.versions + " 版" + (totals.seconds ? " · 累计 " + groupSeconds(totals.seconds) : "") }));
      }
    }
    return card;
  }

  function refineCard() {
    var input = ui.h("textarea", {
      class: "input textarea",
      id: "sum-requirement",
      rows: "2",
      placeholder: "想怎么改？例如：要点压到三条、别用「赋能」这种词、开头先给结论",
      value: state.requirement
    });
    input.addEventListener("input", function () { state.requirement = input.value; });
    return ui.h("div", { class: "card card--flat" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("span", { class: "card__title", text: "打磨" }),
        ui.h("span", { class: "muted", text: "素材会一起带上，不会越改越偏" })
      ]),
      input,
      ui.h("div", { class: "row row--end" }, [
        ui.h("button", {
          class: "btn",
          id: "sum-refine",
          text: "按这个改",
          disabled: state.busy || !state.found ? "disabled" : null,
          onclick: refine
        })
      ])
    ]);
  }

  function bodyCard() {
    var data = (state.summary && state.summary.content) || {};
    // 读取中：不能先画「还没有总结」——那句话是结论，而此刻我们还没有结论。
    // 首屏一进来就闪一下"还没有总结"，用户会以为生成的东西丢了。
    if (state.loading) {
      return ui.h("div", { class: "sum-card" }, [ui.loading("正在读取这一条的总结…")]);
    }
    if (!state.found) {
      return ui.h("div", { class: "sum-card" }, [
        ui.h("div", { class: "idx", text: "NO DOC" }),
        ui.h("h2", { class: "s" }, [ui.h("span", { class: "n", text: "01" }),
          document.createTextNode("还没有总结")]),
        ui.h("p", { class: "sum-tx", text: "选一条素材、挑个角色，点「开始总结」。长视频会先分段预读再成文，几分钟起步。" }),
        ui.h("p", { class: "sum-ai", text: "（生成后这里会出现一句话结论、主体、要点、时间线与注意事项）" })
      ]);
    }

    var sections = [];
    var seq = 0;
    function section(title, nodes) {
      // 编号只数「小节」，不能数节点：sections 里还有正文段落，用 length 会跳号（01 → 03）。
      seq += 1;
      var n = ("0" + seq).slice(-2);
      sections.push(ui.h("h2", { class: "s" }, [
        ui.h("span", { class: "n", text: n }),
        document.createTextNode(title)
      ]));
      nodes.forEach(function (node) { sections.push(node); });
    }

    var one = String(data.one_line || "");
    if (one) section("一句话", [ui.h("p", { class: "sum-tx", id: "sum-one", text: one })]);
    if (data.summary_md) section("主体", [markdown(data.summary_md)]);

    var points = data.key_points || [];
    if (points.length) {
      var ul = ui.h("ul", { class: "sum-tx-ul" });
      points.forEach(function (item) { ul.appendChild(ui.h("li", { text: String(item) })); });
      section("关键点", [ul]);
    }

    var timeline = data.timeline || [];
    if (timeline.length) {
      var tl = ui.h("div", { class: "sum-time" });
      timeline.forEach(function (item) {
        tl.appendChild(ui.h("div", { class: "sum-time__row" }, [
          ui.h("code", { class: "sum-time__at", text: String(item.t || "") }),
          ui.h("span", { class: "sum-time__text", text: String(item.text || "") })
        ]));
      });
      section("时间线", [tl]);
    }

    var caution = String(data.caution || "");
    if (caution) section("注意", [ui.h("p", { class: "sum-tx", text: caution })]);

    // 「评论区短版」这一节 2026-11-07 下线：它和发布卡原来的「摘要」是同一段文字
    // （摘要默认就取 comment_short），两处显示同一份 268 字只会让人以为有两版。
    // 数据还在（模型照旧产出 comment_short），只是不再渲染——要恢复就把这一节加回来。

    var ver = (state.summary && state.summary.version) || 1;
    return ui.h("div", { class: "sum-card", id: "sum-body" }, [
      ui.h("div", { class: "idx", text: "V" + ("0" + ver).slice(-2) + " / CURRENT" }),
      ui.h("div", {}, sections),
      ui.h("p", { class: "sum-ai", text: "（AI 依据字幕生成，发布前请自己过一遍）" })
    ]);
  }

  function childrenCard() {
    var card = ui.h("div", { class: "card card--flat" });
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "子项" }),
      ui.h("span", {
        class: "muted",
        text: state.children.length ? state.children.length + " 集" : ""
      })
    ]));
    if (!state.children.length) {
      card.appendChild(ui.h("p", { class: "muted", text: "这个合集还没有子项。" }));
      return card;
    }
    var list = ui.h("div", { class: "coll-list" });
    state.children.forEach(function (item, index) {
      list.appendChild(ui.h("div", { class: "coll-row", "data-project": item.id }, [
        ui.h("span", { class: "coll-row__no", text: String(index + 1) }),
        ui.h("div", { class: "coll-row__body" }, [
          ui.h("div", { class: "coll-row__title", text: item.title }),
          ui.h("div", {
            class: "coll-row__meta",
            // 不借用 stage 文案：素材库里的 stage 可能还停在「已转录」，
            // 而这一集其实已经有总结了。写 stage 会让两处说法打架。
            text: item.has_summary ? "有总结" : "还没总结"
          })
        ]),
        item.has_summary
          ? null
          : ui.h("span", { class: "pill pill--warn", text: "缺" })
      ]));
    });
    card.appendChild(list);
    return card;
  }

  function overviewVersionsCard() {
    var card = ui.h("div", { class: "card card--flat" });
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "总览版本" }),
      ui.h("span", {
        class: "muted",
        text: state.overviewVersions.length ? state.overviewVersions.length + " 版" : ""
      })
    ]));
    if (!state.overviewVersions.length) {
      card.appendChild(ui.h("p", {
        class: "muted",
        text: "还没有总览。子项都总结完之后，点右上角生成一次。"
      }));
      return card;
    }
    var list = ui.h("div", { class: "ver-list" });
    var current = state.overview ? state.overview.id : "";
    state.overviewVersions.forEach(function (item) {
      var active = item.id === current;
      var meta = [whenDay(item.created_at), roleText(item.role), item.chars + " 字"]
        .filter(function (part) { return !!part; }).join(" · ");
      list.appendChild(ui.h("div", {
        class: "ver-row" + (active ? " is-active" : ""),
        "data-version": String(item.version)
      }, [
        ui.h("div", { class: "ver-row__body" }, [
          ui.h("div", { class: "ver-row__title", text: "第 " + item.version + " 版" + (active ? "（当前）" : "") }),
          ui.h("div", { class: "ver-row__meta", text: meta }),
          item.revise_note ? ui.h("div", { class: "ver-row__note", text: item.revise_note }) : null
        ])
      ]));
    });
    card.appendChild(list);
    return card;
  }

  function overviewCard() {
    var card = ui.h("div", { class: "card card--flat" });
    var content = state.overview ? state.overview.content : null;
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "总览" }),
      ui.h("span", { class: "muted", text: content ? "" : "未生成" })
    ]));
    if (!content) {
      var hint = state.collections.length
        ? "选一个合集，点「生成总览」。它只读子项的总结，不会动子项一个字。"
        : "素材库里同一个合集下有两集以上，这里就会出现。";
      card.appendChild(ui.h("p", { class: "muted", text: hint }));
      return card;
    }
    var body = ui.h("div", { class: "sum-body" });
    if (content.theme) body.appendChild(ui.h("p", { class: "sum-one", text: content.theme }));
    if (content.conclusions && content.conclusions.length) {
      body.appendChild(ui.h("h3", { class: "sum-h", text: "核心结论" }));
      var points = ui.h("ul", { class: "sum-points" });
      content.conclusions.forEach(function (line) {
        points.appendChild(ui.h("li", { text: line }));
      });
      body.appendChild(points);
    }
    if (content.watch_order && content.watch_order.length) {
      body.appendChild(ui.h("h3", { class: "sum-h", text: "推荐观看顺序" }));
      var order = ui.h("ol", { class: "sum-order" });
      content.watch_order.forEach(function (line) {
        order.appendChild(ui.h("li", { text: line }));
      });
      body.appendChild(order);
    }
    if (content.items && content.items.length) {
      body.appendChild(ui.h("h3", { class: "sum-h", text: "分集" }));
      var rows = ui.h("div", { class: "coll-time" });
      content.items.forEach(function (item) {
        // one_line 常常以标题开头（模型爱这么写），再拼一次标题就成了复读机。
        var line = String(item.one_line || "");
        if (item.title && line.indexOf(item.title) === 0) {
          line = line.slice(item.title.length).replace(/^[：:，,\s]+/, "");
        }
        rows.appendChild(ui.h("div", { class: "sum-time__row" }, [
          ui.h("span", { class: "sum-time__at", text: item.minutes ? item.minutes + " 分钟" : "—" }),
          ui.h("span", { text: item.title + (line ? "：" + line : "") })
        ]));
      });
      body.appendChild(rows);
    }
    if (content.overlap && content.overlap.length) {
      body.appendChild(ui.h("h3", { class: "sum-h", text: "重复、分歧与缺口" }));
      var over = ui.h("ul", { class: "sum-points" });
      content.overlap.forEach(function (line) { over.appendChild(ui.h("li", { text: line })); });
      body.appendChild(over);
    }
    if (content.notes && content.notes.length) {
      body.appendChild(ui.h("div", {
        class: "callout callout--warn",
        text: "总览里有 " + content.notes.length + " 处对不上（子项标题或顺序），已按子项纠正。"
      }));
    }
    card.appendChild(body);

    var meta = [];
    if (state.overview.role) meta.push("角色：" + roleText(state.overview.role));
    if (state.overview.model) meta.push("模型：" + state.overview.model);
    if (state.overview.chars) meta.push(state.overview.chars + " 字");
    if (state.overview.created_at) meta.push(whenDay(state.overview.created_at));
    if (meta.length) {
      card.appendChild(ui.h("div", { class: "sum-meta", text: meta.join(" · ") }));
    }
    card.appendChild(ui.h("p", {
      class: "sum-ai",
      text: "（本总览由 AI 依据各集总结生成，发布前请自己过一遍）"
    }));
    return card;
  }

  /**
   * 报错提示条（B 方案：主文案一句 + 可展开）。
   *
   * 这里**不切分那一行**。`wb_job` 只有 error_code / error_message 两格，
   * 「原因 —— 原文」由 app/jobs.py 的 error_message_for_row 拼好并按
   * ERROR_MESSAGE_MAX_CHARS 封顶——分隔符与截断只在那一处。前端按 " —— " 把它切回来
   * 就是第二个 owner，并且实测会把常驻用例
   * test_the_page_does_not_rebuild_the_sentence_the_backend_already_joined 打红
   * （2026-11-05 现跑：1 failed, 6 passed；那句「有一条锁比对分隔符」并不存在）。
   * 所以收起态交给 CSS 收成第一视觉行，展开态还是同一句全文，一字不丢、也不重拼。
   *
   * 不用 title= 悬浮提示——WebView2 里等于没有；开关是真 <button>，真点击路径。
   */
  var errorOpenText = "";

  function errorExpanded(text) {
    return errorOpenText !== "" && errorOpenText === text;
  }

  /**
   * 把 state.error 渲染进提示条。两个 paint 路径共用这一个函数。
   * 同一句错误重复 paint（轮询每次都画）**不重建节点**，否则键盘焦点会被吞掉。
   */
  function setErrorBar(node) {
    if (!node) return;
    var text = state.error || "";
    node.style.display = text ? "" : "none";
    if (!text) { node.textContent = ""; return; }
    var body = document.getElementById("summary-error-body");
    var more = document.getElementById("summary-error-more");
    if (!body || !more || body.textContent !== text) {
      node.textContent = "";
      body = ui.h("div", { class: "callout__body is-clamped", id: "summary-error-body", text: text });
      more = ui.h("button", {
        class: "callout__more", id: "summary-error-more", type: "button",
        "aria-expanded": "false", onclick: toggleErrorBar
      });
      node.appendChild(body);
      node.appendChild(more);
    }
    paintErrorBar(body, more);
  }

  /** 开关露不露，量的是「这一句真被裁了没有」，不是文本长度——短错误不该有开关。 */
  function paintErrorBar(body, more) {
    var open = errorExpanded(state.error || "");
    if (open) { body.classList.remove("is-clamped"); } else { body.classList.add("is-clamped"); }
    var clipped = body.scrollHeight > body.clientHeight + 1;
    more.style.display = (clipped || open) ? "" : "none";
    more.textContent = open ? "收起" : "展开";
    more.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function toggleErrorBar() {
    var text = state.error || "";
    errorOpenText = (errorOpenText === text) ? "" : text;
    var body = document.getElementById("summary-error-body");
    var more = document.getElementById("summary-error-more");
    if (!body || !more) return;
    paintErrorBar(body, more);
    // 展开之后开关可能被顶出屏幕：360x740 实测提示条顶边在 400，正文框限高 45vh
    // 之后开关落在 740~758，正好整条掉出视口 ⇒ document.elementFromPoint 返回
    // null，用户「看得见全文、点不到收起」。收起开关是展开态唯一的出口，它必须
    // 留在屏幕上。block:"nearest" 只把露出去的那一小截推回来，不会跳整页；
    // behavior 用默认的瞬间滚动，免得动画期间量到的还是旧位置。
    // 展开之后开关可能被顶出「能点到的地方」：360x740 实测正文框限高 45vh 之后
    // 开关落在 740~758，整条掉出视口；375x812 / 440x800 实测开关顶边 772 / 767
    // 看似在屏内，其实已经被 `.page` 的滚动框底边裁进状态条那一行，
    // document.elementFromPoint 命中的是状态条 ⇒ 用户「看得见全文、点不到收起」。
    // 收起开关是展开态唯一的出口，判据直接用「它自己能不能被命中」，
    // 不去猜视口高度、也不去猜状态条多高。
    var rect = more.getBoundingClientRect();
    var hit = (rect.width > 0 && rect.height > 0)
      ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      : null;
    if (hit !== more && !(hit && more.contains(hit))) {
      try {
        more.scrollIntoView({ block: "nearest" });
      } catch (err) {
        // 与 gotoPublish 同一套兜底：老 WebView 不认 options。
        more.scrollIntoView();
      }
    }
  }

  function paintCollection() {
    var host = document.getElementById("summary-host");
    if (!host) return;
    host.textContent = "";
    // 和单条版同序：正文在左、版本栏在右（grid 的第一列是 1fr）
    host.appendChild(ui.h("div", { class: "sum-grid" }, [
      ui.h("div", { class: "sum-main" }, [overviewCard()]),
      ui.h("div", { class: "sum-side" }, [childrenCard(), overviewVersionsCard()])
    ]));
    setErrorBar(document.getElementById("summary-error"));
  }

  function paintProgress() {
    var host = document.getElementById("summary-host");
    if (!host) return;
    var old = document.getElementById("sum-prog");
    if (!old) return;
    var pct = Math.max(0, Math.min(100, Math.round((state.jobProgress || 0) * 100)));
    var filled = Math.round(pct / 100 * 8);
    var segs = old.querySelectorAll(".sum-seg i");
    for (var i = 0; i < segs.length; i += 1) {
      segs[i].className = i < filled ? "on" : (i === filled ? "cur" : "");
    }
    var stage = document.getElementById("sum-stage");
    if (stage) stage.textContent = state.jobStage || state.note || "生成中";
    var pctNode = document.getElementById("sum-pct");
    if (pctNode) pctNode.textContent = pct + "%";
  }

  function paint() {
    swapHead();
    if (state.mode === "collection") { paintCollection(); return; }
    var host = document.getElementById("summary-host");
    if (!host) return;
    host.textContent = "";
    host.appendChild(ui.h("div", { class: "sum-grid" }, [
      ui.h("div", { class: "sum-main" }, [
        docHead(),
        progressBlock(),
        warnBlock(),
        bodyCard(),
        // 发布卡：bili_scribe 里它本来就是正文卡的兄弟节点（index.html:880 的 #pub-card）。
        // 给它独立容器，发布区只清自己的子节点，和正文/版本互不抹（C1）。
        ui.h("div", { id: "publish-main" })
      ]),
      ui.h("div", { class: "sum-side" }, [
        versionsCard(),
        // 草稿列表并进 322px 右栏，与版本栏同构（2026-11-02 决策 ③A）
        ui.h("div", { id: "publish-rail" }),
        costCard(),
        refineCard()
      ].filter(Boolean))
    ]));
    mountPublish();
    setErrorBar(document.getElementById("summary-error"));
  }

  /** 发布卡排在正文之后（bili_scribe 的卡序），长正文下给一个一步跳过去的入口。 */
  function gotoPublish() {
    var node = document.getElementById("publish-main");
    if (!node || typeof node.scrollIntoView !== "function") return;
    try {
      node.scrollIntoView({ block: "start", behavior: "smooth" });
    } catch (err) {
      // 老 WebView 不认 options，退回不带参数的调用
      node.scrollIntoView();
    }
  }

  /** 把发布卡挂进刚建好的两个容器。没装发布模块（老构建 / 测试桩）就安静跳过。 */
  function mountPublish() {
    var main = document.getElementById("publish-main");
    var publish = WB.pages && WB.pages.publish;
    if (!main || !publish || !publish.mount) return;
    publish.mount({ main: main, rail: document.getElementById("publish-rail") });
  }

  function render() {
    state.error = "";
    state.note = "";
    var page = ui.h("section", { class: "page" });
    page.appendChild(tabsBar());
    page.appendChild(ui.h("div", { id: "sum-head" }, [head()]));
    page.appendChild(ui.h("div", { class: "sum-bar" }, [
      ui.h("div", { class: "callout callout--warn", id: "summary-error", style: "display:none" }),
      ui.h("span", { class: "muted", id: "sum-note" })
    ]));
    page.appendChild(ui.h("div", { id: "summary-host" }));

    var boot = state.mode === "collection"
      ? Promise.all([loadCollections(), loadRoles()]).then(function () { return loadCollection(); })
      : Promise.all([loadProjects(), loadRoles()]).then(function () { return loadSummary(); });
    boot
      .then(function () { paint(); })
      .catch(function (error) {
        state.error = error && error.message ? error.message : "加载失败";
        paint();
      });
    return page;
  }

  WB.pages = WB.pages || {};
  WB.pages.summary = { render: render, state: state, reload: loadSummary, _internals: {
    markdown: markdown, start: start, refine: refine, restore: restore, chars: chars,
    groupNum: groupNum, groupSeconds: groupSeconds,
    switchMode: switchMode, startCollection: startCollection, pickCollection: pickCollection,
    loadCollection: loadCollection,
    switchSource: switchSource, probe: probe, lightStart: lightStart, pickSrt: pickSrt
  } };

  WB.router.define("summary", {
    title: "总结",
    crumb: "结构化笔记与版本",
    icon: "note",
    nav: true,
    render: render,
    // 轮询、事件订阅、发布卡都挂在全局对象上：不在这里收，离开页面后
    // 它们还会往已经不存在的 DOM 上写。
    destroy: function () {
      // 作废在飞的轮询：自增即让上一轮 tick 在下一个检查点退出。
      pollToken += 1;
      // 发布卡挂在 summary 的两个容器里，容器随页面一起没了，它自己的
      // 缓存与监听也要一起收，否则下次进页面拿到的还是上一条视频的草稿。
      var publish = WB.pages && WB.pages.publish;
      if (publish && publish.unmount) publish.unmount();
    }
  });
})();
