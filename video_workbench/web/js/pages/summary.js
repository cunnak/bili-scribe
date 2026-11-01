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
  var POLL_LIMIT = 900;     // 12 分钟：长视频的分段预读 + 三轮回会真的跑很久

  var state = {
    projects: [],
    projectId: "",
    roles: [],
    role: "",
    models: [],
    model: "",           // "" = 跟随设置（llm.tasks.summary → llm.model）
    modelDefault: "",
    found: false,
    jobProgress: 0,
    jobStage: "",
    summary: null,
    versions: [],
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
    collBusy: false
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

  function loadRoles() {
    call("summary.models").then(function (data) {
      state.models = (data && data.items) || [];
      state.modelDefault = (data && data.default) || "";
    }).catch(function () {
      state.models = [];
    });

    return call("summary.roles").then(function (data) {
      state.roles = (data && data.roles) || [];
      if (!state.role) {
        state.role = (data && data.default) || (state.roles[0] || {}).key || "";
      }
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
      if (state.overview && state.overview.role) state.role = state.overview.role;
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
    return call("summary.get", { id: state.projectId }).then(function (data) {
      state.found = !!(data && data.found);
      state.summary = (data && data.summary) || null;
      state.versions = (data && data.versions) || [];
      if (state.summary && state.summary.role) state.role = state.summary.role;
      return state.summary;
    });
  }

  function pollJob(jobId) {
    var attempts = 0;
    return new Promise(function (resolve, reject) {
      function tick() {
        attempts += 1;
        call("job.get", { id: jobId }).then(function (job) {
          if (!job) { reject(new Error("任务不见了")); return; }
          if (job.status === "succeeded") { resolve(job); return; }
          if (job.status === "failed" || job.status === "canceled") {
            var error = new Error((job.error && job.error.message) || "生成失败");
            error.job = job;
            reject(error);
            return;
          }
          if (attempts > POLL_LIMIT) { reject(new Error("等太久了，先停在这里")); return; }
          state.note = (job.stage || "生成中") + " " + Math.round((job.progress || 0) * 100) + "%";
          state.jobProgress = job.progress || 0;
          state.jobStage = job.stage || "";
          paintNote();
          paintProgress();
          setTimeout(tick, POLL_MS);
        }).catch(reject);
      }
      tick();
    });
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
      .then(function () { return loadSummary(); })
      .then(function () {
        state.busy = false;
        state.note = "";
        state.jobId = "";
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
      .then(function () { return loadSummary(); })
      .then(function () {
        state.busy = false;
        state.note = "";
        state.jobId = "";
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
      .then(function () { return loadCollections(); })
      .then(function () { return loadCollection(); })
      .then(function () {
        state.collBusy = false;
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
      onchange: function (event) { state.role = event.target.value; }
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
      var picked = state.roles.filter(function (r) { return r.key === state.role; })[0];
      box.push(ui.h("span", {}, [ui.h("em", { text: (picked ? picked.name : state.role) }),
        document.createTextNode(picked && picked.passes >= 3 ? " · 三轮" : "")]));
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

  function head() {
    if (state.mode === "collection") return collHead();

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
      ui.h("div", { class: "sum-fields" }, [
        field("sum-project", "PROJECT", "选一条素材，总结只用它自己的字幕",
          projectOptions, pickProject),
        field("sum-role", "ROLE", "角色决定用哪套方法论写这份总结",
          roleOptions, function (value) { state.role = value; }),
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

    var short = String(data.comment_short || "");
    if (short) {
      var box = ui.h("div", { class: "sum-short" });
      box.appendChild(ui.h("p", { class: "sum-short__text", text: short }));
      box.appendChild(ui.h("div", { class: "row row--end" }, [
        ui.h("span", { class: "muted", text: chars(short) + " 字" }),
        ui.h("button", {
          class: "btn btn--ghost btn--sm",
          text: "复制",
          onclick: function () {
            if (navigator.clipboard) navigator.clipboard.writeText(short);
            WB.toast("复制了");
          }
        })
      ]));
      section("评论区短版", [box]);
    }

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
      var meta = [whenDay(item.created_at), item.role, item.chars + " 字"]
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
    if (state.overview.role) meta.push("角色：" + state.overview.role);
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

  function paintCollection() {
    var host = document.getElementById("summary-host");
    if (!host) return;
    host.textContent = "";
    // 和单条版同序：正文在左、版本栏在右（grid 的第一列是 1fr）
    host.appendChild(ui.h("div", { class: "sum-grid" }, [
      ui.h("div", { class: "sum-main" }, [overviewCard()]),
      ui.h("div", { class: "sum-side" }, [childrenCard(), overviewVersionsCard()])
    ]));
    var errorNode = document.getElementById("summary-error");
    if (errorNode) {
      errorNode.textContent = state.error || "";
      errorNode.style.display = state.error ? "" : "none";
    }
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
        bodyCard()
      ]),
      ui.h("div", { class: "sum-side" }, [versionsCard(), refineCard()])
    ]));
    var errorNode = document.getElementById("summary-error");
    if (errorNode) {
      errorNode.textContent = state.error || "";
      errorNode.style.display = state.error ? "" : "none";
    }
  }

  function render() {
    state.error = "";
    state.note = "";
    var page = ui.h("section", { class: "page" });
    page.appendChild(tabsBar());
    page.appendChild(ui.h("div", { id: "sum-head" }, [head()]));
    page.appendChild(ui.h("div", { class: "sum-bar" }, [
      ui.h("span", { class: "callout callout--warn", id: "summary-error", style: "display:none" }),
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
    switchMode: switchMode, startCollection: startCollection, pickCollection: pickCollection,
    loadCollection: loadCollection
  } };

  WB.router.define("summary", {
    title: "总结",
    crumb: "结构化笔记与版本",
    icon: "note",
    nav: true,
    render: render
  });
})();
