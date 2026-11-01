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
          paintNote();
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
  function head() {
    if (state.mode === "collection") return collHead();
    var options = state.projects.map(function (item) {
      return ui.h("option", {
        value: item.id,
        text: item.title + (item.has_transcript ? "" : "（无字幕）"),
        selected: item.id === state.projectId ? "selected" : null
      });
    });
    var picker = ui.h("select", {
      class: "input input--select",
      id: "sum-project",
      onchange: function (event) { pickProject(event.target.value); }
    }, options);
    if (!options.length) {
      picker.appendChild(ui.h("option", { value: "", text: "（素材库还是空的）" }));
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
      title: "角色决定用哪套方法论写这份总结",
      onchange: function (event) { state.role = event.target.value; }
    }, roleOptions);

    var actions = ui.h("div", { class: "page__actions" }, [
      rolePick,
      modelSelect(),
      ui.h("button", {
        class: "btn btn--primary",
        id: "sum-start",
        text: state.busy ? "生成中…" : (state.found ? "重新生成" : "生成总结"),
        disabled: state.busy || !state.projectId ? "disabled" : null,
        onclick: start
      }),
      ui.h("button", {
        class: "btn btn--ghost",
        text: "在文件夹中显示",
        disabled: state.projectId ? null : "disabled",
        onclick: reveal
      })
    ]);

    return ui.h("div", { class: "page__head" }, [
      ui.h("h2", { class: "page__title", text: "总结" }),
      ui.h("p", {
        class: "page__hint",
        text: "用字幕生成结构化总结；不满意就换个角色重出，或直接说怎么改"
      }),
      ui.h("div", { class: "sum-pick" }, [picker]),
      actions
    ]);
  }

  function versionsCard() {
    var card = ui.h("div", { class: "card card--flat" });
    card.appendChild(ui.h("div", { class: "card__head" }, [
      ui.h("span", { class: "card__title", text: "版本" }),
      ui.h("span", { class: "muted", text: state.versions.length ? state.versions.length + " 版" : "" })
    ]));
    if (!state.versions.length) {
      card.appendChild(ui.h("p", { class: "muted sum-ver-empty", text: "还没有版本。生成一次就有了。" }));
      return card;
    }
    var list = ui.h("div", { class: "ver-list" });
    var current = state.summary ? state.summary.id : "";
    state.versions.forEach(function (item) {
      var active = item.id === current;
      var meta = [whenDay(item.created_at), item.role, item.model, item.chars + " 字"]
        .filter(function (part) { return !!part; }).join(" · ");
      var row = ui.h("div", {
        class: "ver-row" + (active ? " is-active" : ""),
        "data-version": String(item.version)
      }, [
        ui.h("div", { class: "ver-row__body" }, [
          ui.h("div", { class: "ver-row__title", text: "第 " + item.version + " 版" + (active ? "（当前）" : "") }),
          ui.h("div", { class: "ver-row__meta", text: meta }),
          item.revise_note ? ui.h("div", { class: "ver-row__note", text: item.revise_note }) : null
        ]),
        active ? null : ui.h("button", {
          class: "btn btn--ghost btn--sm",
          text: "恢复",
          disabled: state.busy ? "disabled" : null,
          onclick: function () { restore(item.id, item.version); }
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
    var card = ui.h("div", { class: "card sum-body", id: "sum-body" });
    if (!state.found) {
      card.appendChild(ui.h("div", { class: "empty" }, [
        ui.h("p", { class: "empty__title", text: "还没有总结" }),
        ui.h("p", { class: "empty__text", text: "选一个项目，挑个角色，点「生成总结」。一分钟到几分钟不等，取决于视频多长。" })
      ]));
      return card;
    }
    var oneLine = String(data.one_line || "");
    if (oneLine) {
      card.appendChild(ui.h("p", { class: "sum-one", id: "sum-one", text: oneLine }));
    }
    card.appendChild(markdown(data.summary_md));

    var points = data.key_points || [];
    if (points.length) {
      card.appendChild(ui.h("h3", { class: "sum-h", text: "要点" }));
      var list = ui.h("ul", { class: "sum-points" });
      points.forEach(function (item) { list.appendChild(ui.h("li", { text: String(item) })); });
      card.appendChild(list);
    }

    var timeline = data.timeline || [];
    if (timeline.length) {
      card.appendChild(ui.h("h3", { class: "sum-h", text: "时间线" }));
      var tl = ui.h("div", { class: "sum-time" });
      timeline.forEach(function (item) {
        tl.appendChild(ui.h("div", { class: "sum-time__row" }, [
          ui.h("code", { class: "sum-time__at", text: String(item.t || "") }),
          ui.h("span", { class: "sum-time__text", text: String(item.text || "") })
        ]));
      });
      card.appendChild(tl);
    }

    var caution = String(data.caution || "");
    if (caution) {
      card.appendChild(ui.h("h3", { class: "sum-h", text: "注意" }));
      card.appendChild(ui.h("p", { class: "callout callout--warn", text: caution }));
    }

    var short = String(data.comment_short || "");
    if (short) {
      card.appendChild(ui.h("h3", { class: "sum-h", text: "评论区短版" }));
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
      card.appendChild(box);
    }

    var meta = [];
    if (state.summary.role) meta.push("角色：" + state.summary.role);
    if (state.summary.model) meta.push("模型：" + state.summary.model);
    if (state.summary.chars) meta.push(state.summary.chars + " 字");
    if (state.summary.created_at) meta.push(whenDay(state.summary.created_at));
    if (meta.length) {
      card.appendChild(ui.h("div", { class: "sum-meta", text: meta.join(" · ") }));
    }
    card.appendChild(ui.h("p", { class: "sum-ai", text: "（本总结由 AI 依据视频字幕生成，发布前请自己过一遍）" }));
    return card;
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
    host.appendChild(ui.h("div", { class: "sum-grid" }, [
      ui.h("div", { class: "sum-side" }, [childrenCard(), overviewVersionsCard()]),
      ui.h("div", { class: "sum-main" }, [overviewCard()])
    ]));
    var errorNode = document.getElementById("summary-error");
    if (errorNode) {
      errorNode.textContent = state.error || "";
      errorNode.style.display = state.error ? "" : "none";
    }
  }

  function paint() {
    swapHead();
    if (state.mode === "collection") { paintCollection(); return; }
    var host = document.getElementById("summary-host");
    if (!host) return;
    host.textContent = "";
    var grid = ui.h("div", { class: "sum-grid" }, [
      ui.h("div", { class: "sum-side" }, [versionsCard(), refineCard()]),
      ui.h("div", { class: "sum-main" }, [bodyCard()])
    ]);
    host.appendChild(grid);
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
