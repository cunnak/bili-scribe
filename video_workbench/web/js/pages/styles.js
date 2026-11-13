/* ============================================================================
 * pages/styles.js —— 字幕样式页（任务 5 重写）
 *
 * 这一页现在是**唯一**的样式状态 owner，只做四件事：
 *
 * 1. 作用域：设置页进来是全局，字幕页带 project_id 进来是项目；
 * 2. 自动保存：改动 400ms 后落盘，请求串行 + revision 防旧响应覆盖；
 * 3. 预设操作：选择＝复制到当前配置，覆盖/另存/重命名/删除/复制都是显式动作；
 * 4. 两种预览：快速（画布近似）与精确（FFmpeg 真图）。
 *
 * 表单与预览的画法分别住在 components/subtitle_style_form.js 与
 * subtitle_style_preview.js。它们不知道 RPC 存在，页面也不重复实现画布——
 * 两边各画一次必然漂移。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  function n(tag, attrs, children) {
    var bag = attrs || {};
    var flat = {};
    Object.keys(bag).forEach(function (key) {
      if (key === "attrs" && bag.attrs) {
        Object.keys(bag.attrs).forEach(function (name) { flat[name] = bag.attrs[name]; });
      } else {
        flat[key] = bag[key];
      }
    });
    return ui.h(tag, flat, children);
  }

  //: 自动保存的静默期。太短会在拖 range 时连发十几个请求，太长会让人以为没存。
  var AUTOSAVE_MS = 400;

  var state = {
    projectId: "",
    list: [],
    listLoading: false,
    listError: "",
    fonts: [],
    draft: null,
    scope: "global",
    inherited: true,
    revision: "",
    warnings: [],
    actualFonts: null,
    dirty: false,
    saving: false,
    note: "",
    error: "",
    errors: [],
    renderPath: "",
    renderNote: "",
    renderBusy: false,
    // 串行化：同一时刻只有一个 put 在飞，排队中的只保留最后一次
    inFlight: false,
    queued: false,
    timer: 0,
    // 每次落盘成功自增；响应回来时序号不符＝旧响应，直接丢
    seq: 0,
    appliedSeq: 0
  };

  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function form() {
    return WB.components && WB.components.subtitleStyleForm;
  }

  function previewComp() {
    return WB.components && WB.components.subtitleStylePreview;
  }

  function scopeLabel() {
    if (!state.projectId) return "全局默认";
    return state.inherited ? "正在继承全局" : "项目覆盖";
  }

  // ------------------------------------------------------------ 自动保存

  function scheduleSave() {
    state.dirty = true;
    state.renderPath = "";
    state.renderNote = "";
    paintBar();
    drawFast();
    if (state.timer) window.clearTimeout(state.timer);
    state.timer = window.setTimeout(function () {
      state.timer = 0;
      flush();
    }, AUTOSAVE_MS);
  }

  /** 立刻落盘。离页、切作用域、点保存按钮都走这里。 */
  function flush() {
    if (state.timer) {
      window.clearTimeout(state.timer);
      state.timer = 0;
    }
    if (!state.draft || !state.dirty) return Promise.resolve();
    if (state.inFlight) {
      // 在飞的时候不并发第二个请求：等它回来再发最新的一份
      state.queued = true;
      return Promise.resolve();
    }
    state.inFlight = true;
    state.saving = true;
    state.seq += 1;
    var mine = state.seq;
    var payload = {
      project_id: state.projectId,
      config: state.draft,
      base_preset_id: state.draft.base_preset_id || "",
      revision: state.revision
    };
    state.dirty = false;
    paintBar();
    return WB.rpc("style.effective.put", payload).then(function (data) {
      state.inFlight = false;
      state.saving = false;
      // 旧响应失效：期间又改过，这份结果已经不是用户眼前的样子
      if (mine < state.seq) {
        if (state.queued) { state.queued = false; return flush(); }
        return null;
      }
      state.appliedSeq = mine;
      state.revision = data.revision || "";
      state.scope = data.scope || state.scope;
      state.inherited = state.scope !== "project" && !!state.projectId;
      state.warnings = data.warnings || [];
      state.errors = [];
      state.error = "";
      state.note = "已保存";
      paintAll();
      if (state.queued) {
        state.queued = false;
        return flush();
      }
      return null;
    }).catch(function (error) {
      state.inFlight = false;
      state.saving = false;
      // 失败保留草稿：清掉草稿等于把用户刚打的字扔掉，比不保存更糟
      state.dirty = true;
      var fields = (error && error.fields) || [];
      state.errors = fields.length ? fields : [];
      state.error = (error && error.message) || "保存失败";
      state.note = "";
      paintAll();
    });
  }

  // ------------------------------------------------------------ 读

  function loadFonts() {
    return WB.rpc("style.fonts").then(function (data) {
      state.fonts = (data && data.fonts) || [];
    }).catch(function () {
      state.fonts = [];
    });
  }

  function loadList() {
    state.listLoading = true;
    state.listError = "";
    paintList();
    return WB.rpc("style.list").then(function (rows) {
      state.listLoading = false;
      state.list = rows || [];
      paintList();
    }).catch(function (error) {
      state.listLoading = false;
      state.listError = (error && error.message) || "读不到样式列表";
      paintList();
    });
  }

  /** 读当前作用域的有效样式。作用域是这一页的输入，不是它自己猜出来的。 */
  function loadEffective() {
    return WB.rpc("style.effective.get", { project_id: state.projectId })
      .then(function (data) {
        state.draft = copy(data.config);
        state.scope = data.scope || "global";
        state.inherited = !!data.inherited;
        state.revision = data.revision || "";
        state.warnings = data.warnings || [];
        state.actualFonts = data.actual_fonts || null;
        state.errors = [];
        state.error = "";
        state.dirty = false;
        paintAll();
      })
      .catch(function (error) {
        state.error = (error && error.message) || "读不到样式";
        paintAll();
      });
  }

  // ------------------------------------------------------------ 预设操作

  function pickPreset(id) {
    if (!id) return Promise.resolve();
    // 切预设前先把在途改动落地，否则"改了没存就切走"会静默丢改动
    return flush().then(function () {
      return WB.rpc("style.get", { id: id });
    }).then(function (data) {
      if (!data) return null;
      // 预设只当模板：整份复制进当前作用域，不建立"跟随某个预设"的引用
      state.draft = copy(data.style);
      state.draft.base_preset_id = data.style.id || id;
      state.warnings = data.warnings || [];
      state.dirty = true;
      state.note = "已套用预设：" + (data.style.name || id) + "（还没保存）";
      paintAll();
      return flush();
    }).catch(function (error) {
      state.error = (error && error.message) || "套用预设失败";
      paintAll();
    });
  }

  // 另存为同样当场问名字：默认带一个可用的建议值，用户直接回车就行。
  // 原来直接拿 draft.name 提交，另存出来的名字与当前预设一模一样（后端去重加成「 2」），
  // 用户看着像「多了一份莫名其妙的副本」，而不是「我另存了一个」。
  function saveAsPreset() {
    if (!state.draft) return Promise.resolve();
    var base = (state.draft.name || "新样式").replace(/\s+\d+$/, "");
    var suggested = base + " 副本";
    return WB.ui.prompt({
      title: "另存为新预设",
      label: "预设名",
      value: suggested,
      confirmText: "保存",
      hint: "保存的是当前这份配置的整份拷贝",
      validate: function (next) {
        return next ? "" : "名字不能为空";
      }
    }).then(function (name) {
      if (!name) return null;
      return flush().then(function () {
        return WB.rpc("style.save", { name: name, config: state.draft });
      }).then(function (data) {
        if (!data) return null;
        state.note = "已另存为预设：" + data.style.name;
        state.error = "";
        return loadList().then(function () { paintAll(); });
      }).catch(function (error) {
        state.error = (error && error.message) || "另存失败";
        paintAll();
      });
    });
  }

  // 重命名必须**主动问**新名字。
  //
  // 原来这里直接拿 state.draft.name 提交：用户没先去「基本」区块改那个输入框，
  // 提交的就是原名，后端保存成功、列表一字未变 —— 用户看到的是「点了重命名没反应」。
  // 重命名这个动作的语义就是「给一个新名字」，所以当场问，不指望用户先去别处改。
  function renamePreset() {
    if (!state.draft || !state.draft.id) {
      state.error = "当前这份还不是已保存的预设，先另存为新预设";
      paintAll();
      return Promise.resolve();
    }
    // 内置预设后端会拒（"内置样式不可修改，请先复制一份"）。在这里当场问，
    // 比让用户点完重命名再读一行红字强：他要的结果（有一份自己名字的预设）就在下一步。
    var row = state.list.filter(function (r) { return r.id === state.draft.id; })[0] || {};
    if (row.builtin) {
      return WB.ui.confirm(
        "内置预设不能改名",
        "「" + (row.name || state.draft.id) + "」是内置预设，后端不许改它。复制一份出来就能随便改名，配置会一起带过去。",
        { confirmText: "复制一份再改" }
      ).then(function (yes) {
        return yes ? saveAsPreset() : null;
      });
    }
    var current = state.draft.name || "";
    return WB.ui.prompt({
      title: "重命名预设",
      label: "新名字",
      value: current,
      confirmText: "重命名",
      hint: "名字重复时会自动加「 2」「 3」",
      validate: function (next) {
        if (!next) return "名字不能为空";
        if (next === current) return "名字没变，改一个再确定";
        return "";
      }
    }).then(function (next) {
      if (!next) return null;
      return WB.rpc("style.save", { id: state.draft.id, name: next, config: state.draft })
        .then(function (data) {
          if (!data) return null;
          state.draft.name = data.style.name;
          state.note = "预设已重命名为：" + data.style.name;
          state.error = "";
          return loadList().then(function () { paintAll(); });
        }).catch(function (error) {
          state.error = (error && error.message) || "重命名失败";
          paintAll();
        });
    });
  }

  function duplicatePreset() {
    if (!state.draft) return Promise.resolve();
    var draft = copy(state.draft);
    draft.name = (draft.name || "样式") + " 副本";
    return WB.rpc("style.save", { name: draft.name, config: draft }).then(function (data) {
      if (!data) return null;
      state.note = "已复制为：" + data.style.name;
      return loadList();
    }).catch(function (error) {
      state.error = (error && error.message) || "复制失败";
      paintAll();
    });
  }

  function removePreset() {
    var id = (state.draft && state.draft.id) || "";
    if (!id) {
      state.error = "当前这份还不是已保存的预设，没有可删的东西";
      paintAll();
      return Promise.resolve();
    }
    return WB.rpc("style.delete", { id: id }).then(function () {
      state.note = "已删除预设";
      return loadList();
    }).catch(function (error) {
      state.error = (error && error.message) || "删除失败";
      paintAll();
    });
  }

  function revealStyles() {
    WB.rpc("fs.reveal", { path: "" }, { silent: true }).then(function (data) {
      state.note = "已打开样式目录：" + ((data && data.path) || "");
      paintBar();
    }).catch(function (error) {
      state.error = (error && error.message) || "打不开样式目录";
      paintAll();
    });
  }

  function resetScope() {
    if (!state.projectId) {
      state.error = "全局作用域没有「继承」可恢复；要回内置默认请点「恢复内置默认」";
      paintAll();
      return Promise.resolve();
    }
    return flush().then(function () {
      return WB.rpc("style.effective.reset", { project_id: state.projectId });
    }).then(function (data) {
      if (!data) return null;
      state.draft = copy(data.config);
      state.scope = data.scope || "global";
      state.inherited = true;
      state.revision = data.revision || "";
      state.warnings = data.warnings || [];
      state.dirty = false;
      state.note = "已恢复继承全局默认";
      paintAll();
    }).catch(function (error) {
      state.error = (error && error.message) || "恢复继承失败";
      paintAll();
    });
  }

  function resetGlobal() {
    if (state.projectId) {
      state.error = "这一页是项目作用域；要回内置默认请到设置页，或在项目里点「恢复继承」";
      paintAll();
      return Promise.resolve();
    }
    return flush().then(function () {
      return WB.rpc("style.effective.reset", {});
    }).then(function (data) {
      if (!data) return null;
      state.draft = copy(data.config);
      state.revision = data.revision || "";
      state.dirty = false;
      state.note = "全局已回到内置默认";
      paintAll();
    }).catch(function (error) {
      state.error = (error && error.message) || "恢复失败";
      paintAll();
    });
  }

  // JSON 导入导出：规划书 §2 明确「保留 JSON 导入导出」——
  // 这是跨机器搬样式的唯一通道，不能因为界面上更常点预设就砍掉。
  function importJson() {
    // 走产品既有的 fs.pick 通道。以前这里调 WB.fs.pickFile —— 全仓没有 WB.fs
    // 这个对象，于是「导入」在任何环境下都只弹一句"这个环境没有文件选择器"，
    // 用户以为是自己机器缺东西。桌面层的选择器一直都在，只是没被接上。
    WB.rpc("fs.pick", { kind: "file" }, { silent: true }).then(function (picked) {
      var path = (picked && picked.path) || "";
      if (!path) return null;
      return WB.rpc("style.import", { path: path });
    }).then(function (data) {
      if (!data) return null;
      state.note = "已导入：" + data.style.name;
      return loadList();
    }).catch(function (error) {
      state.error = (error && error.message) || "导入失败";
      paintAll();
    });
  }

  function exportJson() {
    var id = (state.draft && state.draft.id) || "";
    if (!id) {
      state.error = "先把这个样式存成预设，再导出 JSON";
      paintAll();
      return;
    }
    WB.rpc("style.export", { id: id }).then(function (data) {
      state.note = "已导出：" + data.path;
      paintBar();
    }).catch(function (error) {
      state.error = (error && error.message) || "导出失败";
      paintAll();
    });
  }

  // ------------------------------------------------------------ 精确预览

  function exactPreview() {
    if (!state.draft) return;
    state.renderBusy = true;
    state.renderPath = "";
    state.renderNote = "渲染中…（要起 FFmpeg，稍等）";
    paintRender();
    WB.rpc("style.render", {
      id: state.draft.id || "",
      config: state.draft,
      project_id: state.projectId,
      time_ms: state.previewTimeMs || -1
    }).then(function (data) {
      state.renderBusy = false;
      state.renderPath = data.path || "";
      state.renderNote = data.path
        ? "精确预览：" + data.width + "×" + data.height + (data.real_frame ? "（真实帧）" : "（演示背景）")
        : (data.note || "渲染完成但没有图片");
      if ((data.warnings || []).length) state.warnings = data.warnings;
      if (data.actual_fonts) state.actualFonts = data.actual_fonts;
      paintRender();
    }).catch(function (error) {
      state.renderBusy = false;
      state.renderPath = "";
      // 失败就说失败：不退回"画布看着也行"当成功，那是假报告
      state.renderNote = "精确预览失败（" + ((error && error.message) || "未知原因") + "），上面是画布近似预览";
      paintRender();
    });
  }

  // ------------------------------------------------------------ 渲染

  function drawFast() {
    var comp = previewComp();
    if (!comp || !state.draft) return;
    comp.drawFast(document.getElementById("styles-canvas-host"), state.draft);
  }

  function paintBar() {
    var host = document.getElementById("styles-state");
    if (!host) return;
    var bits = [scopeLabel()];
    if (state.saving) bits.push("保存中…");
    else if (state.dirty) bits.push("有改动未保存");
    else if (state.note) bits.push(state.note);
    else bits.push("改动会自动保存");
    host.textContent = bits.join(" · ");
    host.setAttribute("data-scope", state.projectId ? (state.inherited ? "inherited" : "project") : "global");
    var errHost = document.getElementById("styles-error");
    if (errHost) {
      errHost.textContent = state.error || "";
      errHost.hidden = !state.error;
    }
  }

  function paintRender() {
    var comp = previewComp();
    if (!comp) return;
    comp.paintExact(document.getElementById("styles-render"), {
      path: state.renderPath,
      note: state.renderNote,
      fonts: state.actualFonts
    });
  }

  function btn(text, title, handler, opts) {
    var options = opts || {};
    var attrs = { type: "button", title: title || text };
    // 给按钮一个稳定的 id：判据要按「用户点的那个按钮」定位，
    // 靠中文文案匹配会在改一句提示语时静默失效。
    if (options.id) attrs.id = options.id;
    var node = n("button", {
      class: "btn btn--ghost" + (options.primary ? " btn--primary" : "") +
        (options.danger ? " btn--danger" : ""),
      text: text,
      attrs: attrs
    });
    if (options.disabled) node.disabled = true;
    else node.addEventListener("click", handler);
    return node;
  }

  function listPane() {
    if (state.listLoading) {
      return n("div", { class: "styles__pane styles__pane--list" }, [
        n("div", { class: "styles__pane-head" }, [n("strong", { text: "预设" })]),
        ui.loading("正在读取样式列表…")
      ]);
    }
    if (state.listError) {
      return n("div", { class: "styles__pane styles__pane--list" }, [
        n("div", { class: "styles__pane-head" }, [n("strong", { text: "预设" })]),
        ui.errorBlock({
          title: "读不到样式列表",
          message: state.listError,
          onRetry: function () { loadList(); }
        })
      ]);
    }
    var rows = state.list.map(function (row) {
      var active = state.draft && state.draft.base_preset_id === row.id;
      var node = n("button", {
        class: "styles__item" + (active ? " is-active" : ""),
        attrs: { type: "button", "data-preset-id": row.id }
      }, [
        n("span", { class: "styles__item-name", text: row.name, attrs: { "data-preset-name": row.id } }),
        n("span", { class: "styles__item-meta" }, [
          n("span", { class: "pill" + (row.mode === "rounded" ? " pill--warn" : ""),
                     text: row.mode === "rounded" ? "圆角条" : "描边" }),
          n("span", { class: "styles__hint", text: row.size + " 号" }),
          row.builtin ? n("span", { class: "styles__hint", text: "内置" }) : null
        ])
      ]);
      node.addEventListener("click", function () { pickPreset(row.id); });
      return node;
    });
    return n("div", { class: "styles__pane styles__pane--list" }, [
      n("div", { class: "styles__pane-head" }, [
        n("strong", { text: "预设" }),
        n("span", { class: "styles__hint", text: state.list.length + " 个 · 选中即套用为模板" })
      ]),
      n("div", { class: "styles__items" },
        rows.length ? rows : [n("p", { class: "styles__hint", text: "还没有预设" })]),
      n("div", { class: "styles__actions" }, [
        btn("另存为新预设", "把当前配置存成一个新预设", saveAsPreset, { id: "styles-save-as" }),
        btn("重命名", "改当前预设的名字", renamePreset, { id: "styles-rename" }),
        btn("复制", "复制当前预设", duplicatePreset, { id: "styles-duplicate" }),
        btn("删除", "删除当前预设", removePreset, { id: "styles-delete", danger: true }),
        btn("导入 JSON", "从 JSON 文件导入一份样式", importJson),
        btn("导出 JSON", "把当前预设导出成 JSON 文件", exportJson),
        btn("打开目录", "打开样式 JSON 所在目录", revealStyles)
      ])
    ]);
  }

  function errorsPane() {
    var list = state.errors || [];
    if (!list.length && !state.error) return null;
    var nodes = [];
    if (state.error) {
      nodes.push(n("p", { class: "callout callout--warn", text: state.error }));
    }
    list.forEach(function (text) {
      nodes.push(n("p", { class: "callout callout--warn", text: text }));
    });
    return n("div", { class: "styles__warnings", attrs: { id: "styles-errors" } }, nodes);
  }

  function scopeBar() {
    var hint = state.projectId
      ? (state.inherited
         ? "这个视频还没有自己的样式，正在跟随全局默认。改任何一项就会建立独立覆盖。"
         : "这个视频用的是自己的一份样式，全局改了不影响它。")
      : "这里改的是全局默认，所有没有独立覆盖的视频都会跟着变。";
    var actions = [];
    if (state.projectId) {
      actions.push(btn("恢复继承全局", "删掉这个视频的样式覆盖，回到全局默认", resetScope, {
        disabled: state.inherited
      }));
    } else {
      actions.push(btn("恢复内置默认", "把全局样式清回内置 default", resetGlobal));
    }
    return n("div", { class: "styles__scopebar" }, [
      n("span", { class: "pill styles__scope-pill", text: scopeLabel(),
                 attrs: { id: "styles-scope-pill" } }),
      n("span", { class: "styles__hint", text: hint }),
      n("div", { class: "styles__row" }, actions)
    ]);
  }

  function formPane() {
    var comp = form();
    if (!comp || !state.draft) {
      return n("div", { class: "styles__pane styles__pane--edit" }, [
        n("p", { class: "styles__hint", text: "还没有读到样式。" })
      ]);
    }
    var builtin = !!(state.list.filter(function (row) {
      return row.id === state.draft.id;
    })[0] || {}).builtin;
    // 名字输入框**不再禁用**。原来内置预设时把它 disabled，用户看得见却改不动，
    // 而「重命名」按钮又直接提交 draft.name —— 两处一起导致「预设无法重命名」。
    // 内置预设的改名在后端本来就会被拒（「内置样式不可修改，请先复制一份」），
    // 那种情况由 renamePreset 当场问「要不要复制一份再改」，而不是让输入框装死。
    return n("div", { class: "styles__pane styles__pane--edit" }, [
      scopeBar(),
      errorsPane(),
      comp.render({
        draft: state.draft,
        fonts: state.fonts,
        onChange: scheduleSave,
        nameLocked: false,
        // 这句话只讲**实测过**的行为：点重命名会先问「复制一份再改」（renamePreset 里的分支）。
        // 原来那句「内置预设只读」不成立——draft 是副本，配置项本来就能改。
        headHint: builtin ? "内置预设：点「重命名」会先复制一份再改" : ""
      }),
      n("div", { class: "styles__savebar" }, [
        btn("保存", "立即保存（平时会自动保存）", function () { flush(); }, { primary: true }),
        n("span", { class: "styles__hint",
                   text: state.saving ? "保存中…" : (state.note || "改动会自动保存") })
      ])
    ]);
  }

  function previewPane() {
    var comp = previewComp();
    if (!comp || !state.draft) return null;
    return n("div", { class: "styles__pane styles__pane--preview" }, [
      comp.render({
        draft: state.draft,
        busy: state.renderBusy,
        onChange: scheduleSave,
        onExact: exactPreview
      })
    ]);
  }

  function paintForm() {
    var host = document.getElementById("styles-edit");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    host.appendChild(formPane());
    drawFast();
    paintRender();
  }

  function paintList() {
    var host = document.getElementById("styles-list");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    host.appendChild(listPane());
  }

  function paintAll() {
    paintBar();
    paintList();
    // **先挂预览容器再画表单**：表单末尾会调 drawFast 去 canvas 上画，
    // 而 canvas 由预览组件创建。顺序反了画布根本不存在，表现为
    // "改了字号没反应"——很容易被当成预览坏了。
    var host = document.getElementById("styles-preview");
    if (host) {
      while (host.firstChild) host.removeChild(host.firstChild);
      var node = previewPane();
      if (node) host.appendChild(node);
    }
    paintForm();
  }

  function render(ctx) {
    var params = (ctx && ctx.params) || {};
    // 作用域从路由参数来：设置页不带 project_id（全局），字幕页带（项目）
    state.projectId = String(params.project_id || "");
    state.draft = null;
    state.errors = [];
    state.error = "";
    state.note = "";
    var root = n("div", { class: "styles" }, [
      n("div", { class: "styles__bar" }, [
        n("span", { class: "styles__hint", id: "styles-state", text: scopeLabel() }),
        n("span", { class: "styles__error", id: "styles-error", hidden: true })
      ]),
      n("div", { class: "styles__grid2" }, [
        n("div", { id: "styles-list" }),
        n("div", { class: "styles__col" }, [
          n("div", { id: "styles-edit" }),
          n("div", { id: "styles-preview" })
        ])
      ])
    ]);
    window.setTimeout(function () {
      loadFonts();
      loadList();
      loadEffective();
    }, 0);
    return root;
  }

  /** 离页前把在途改动落地：不 flush 就是"切走就丢"，而且丢得没有提示。
   *
   * 钩子名必须是 destroy —— router.renderRoute 调的是 definition.destroy，
   * 写成 teardown 会静默不执行（名字对不上不报错），表现成"切走就丢改动"。 */
  function destroy() {
    flush();
  }

  WB.pages = WB.pages || {};
  WB.pages.styles = {
    render: render,
    destroy: destroy,
    _internals: {
      state: state,
      flush: flush,
      scheduleSave: scheduleSave,
      loadEffective: loadEffective,
      loadList: loadList,
      pickPreset: pickPreset,
      saveAsPreset: saveAsPreset,
      renamePreset: renamePreset,
      duplicatePreset: duplicatePreset,
      removePreset: removePreset,
      importJson: importJson,
      exportJson: exportJson,
      resetScope: resetScope,
      resetGlobal: resetGlobal,
      exactPreview: exactPreview,
      drawFast: drawFast,
      scopeLabel: scopeLabel
    }
  };

  WB.router.define("styles", {
    title: "字幕样式",
    render: render,
    destroy: destroy
  });
})();