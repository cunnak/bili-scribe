/* ============================================================================
 * pages/publish.js —— 发布中心（P2-09）
 *
 * **发布是不可撤销的动作**（方案书原则 7：AI 产出先当草稿）。所以这一页的顺序是：
 * 存草稿 → 三关门禁 → 勾确认 → 才允许点「发布笔记」。
 * 门禁没过时按钮**禁用且写清原因**；放一个点了没反应的按钮比不放更糟，
 * 用户会以为是自己没登录。
 *
 * 三条实现约束：
 * 1. **字数口径跟后端一致**：标题/摘要按码点数（`Array.from`，emoji 算 1），
 *    正文用后端算好的 `content_text`。前端另算一套，两个数字迟早对不上。
 * 2. **改过的不能被重新生成打回**：生成时把用户已改的标题/摘要带上；
 *    保存永远提交输入框里的当前值。
 * 3. **正文只读预览用 textContent**：正文来自模型，里面出现 `<` 是常态。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;
  var h = ui.h;

  var state = {
    projects: [],
    projectId: "",
    notes: [],
    current: null,   // 已保存的草稿（publish.get）
    draft: null,     // 刚生成、还没保存的预览（publish.preview）
    busy: false,
    regenerating: false,
    gates: null,
    records: [], // 发布记录（发过 / 撤过 / 发失败）
    recordCounts: { published: 0, withdrawn: 0, failed: 0 },     // 三道门禁的当前状态（publish.get / preview 里带回来）
    confirmed: false, // 「我已阅读并确认」勾没勾
    publishing: false,
    error: "",
    note: ""
  };

  function call(method, params) {
    return WB.rpc(method, params || {}, { silent: true });
  }

  /** 码点数，不是 UTF-16 长度：中文 1，emoji 也 1（与后端 len() 口径一致）。 */
  function len(text) {
    return Array.from(String(text || "")).length;
  }

  function chars(text) {
    return Array.from(String(text || "").replace(/\s+/g, "")).length;
  }

  function stamp(iso) {
    if (!iso) return "—";
    return String(iso).replace("T", " ").slice(0, 16);
  }

  function btn(label, title, onClick, opts) {
    var options = opts || {};
    var node = h("button", {
      class:
        "btn" +
        (options.primary ? " btn--primary" : "") +
        (options.danger ? " btn--danger" : "") +
        (options.tiny ? " btn--tiny" : ""),
      type: "button",
      title: title || "",
      text: label
    });
    if (options.disabled) node.disabled = true;
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  function valueOf(selector) {
    var node = document.querySelector(selector);
    return node ? String(node.value || "") : "";
  }

  // ------------------------------------------------------------------ 数据

  function loadProjects() {
    return call("library.list", { limit: 200, offset: 0 }).then(function (data) {
      state.projects = (data && data.items) || [];
      if (!state.projectId && state.projects.length) {
        var pick = state.projects[0];
        for (var i = 0; i < state.projects.length; i += 1) {
          if (state.projects[i].has_transcript !== false) {
            pick = state.projects[i];
            break;
          }
        }
        state.projectId = pick.id;
      }
    });
  }

  function loadNotes(selectId) {
    return call("publish.list", {}).then(function (data) {
      state.notes = (data && data.notes) || [];
      if (selectId) return loadNote(selectId);
      if (state.notes.length) return loadNote(state.notes[0].id);
      state.current = null;
      state.draft = null;
      return null;
    });
  }

  function loadRecords() {
    return call("publish.records", { limit: 50 }, { silent: true }).then(function (data) {
      state.records = (data && data.records) || [];
      state.recordCounts = (data && data.counts) || { published: 0, withdrawn: 0, failed: 0 };
      return state.records;
    }, function () {
      // 记录拿不到不该让整页红掉：它只是旁证，发布流程本身照常
      state.records = [];
      return [];
    });
  }

  function openUrl(url) {
    if (!url) return;
    call("app.open_url", { url: url }, { silent: true }).then(null, function (err) {
      state.error = (err && err.message) || "打不开这个链接";
      paint();
    });
  }

  /** 记录行里那个"查看草稿"：库里还在就跳过去，不在了只提示一句。 */
  function showRecord(id) {
    return loadNote(id).then(function (note) {
      state.error = "";
      paint();
      return note;
    }, function (err) {
      state.error = (err && err.message) || "这份记录对应的草稿已经不在了";
      paint();
    });
  }

  function loadNote(id) {
    return call("publish.get", { id: id, confirmed: state.confirmed }).then(function (data) {
      state.current = data.note;
      state.draft = null;
      state.gates = data.gates || null;
      state.projectId = state.current.project_id;
      return state.current;
    });
  }

  /** 重新生成标题/摘要。默认只重算摘要——正文是用户可能已经改过的东西。 */
  function regenerate(part) {
    if (!state.current) {
      ui.toast("先选一份草稿", "error");
      return Promise.resolve();
    }
    state.regenerating = true;
    state.error = "";
    paint();
    return call("publish.regenerate", { id: state.current.id, part: part || "summary" })
      .then(function (data) {
        state.regenerating = false;
        state.draft = data.draft;
        state.gates = data.gates || null;
        state.note = part === "all" ? "标题与摘要已按最新总结重写" : "摘要已重写";
        paint();
      })
      .catch(function (err) {
        state.regenerating = false;
        state.error = err.message || "重新生成失败";
        paint();
      });
  }

  function generate() {
    if (!state.projectId) {
      state.error = "先选一个项目";
      paint();
      return Promise.resolve();
    }
    state.busy = true;
    state.error = "";
    paint();
    return call("publish.preview", { project_id: state.projectId })
      .then(function (data) {
        state.draft = data;
        state.current = data.note || null;
        state.gates = data.gates || null;
        state.busy = false;
        paint();
      })
      .catch(function (err) {
        state.busy = false;
        state.error = err.message || "生成草稿失败";
        paint();
      });
  }

  function save() {
    var title = valueOf("#pb-title");
    var summary = valueOf("#pb-summary");
    if (!title.trim()) {
      state.error = "标题不能为空";
      paint();
      return Promise.resolve();
    }
    var body = state.draft
      ? state.draft.content
      : state.current
        ? state.current.content
        : "";
    state.busy = true;
    state.error = "";
    paint();
    return call("publish.note", {
      project_id: state.projectId,
      note_pk: state.current ? state.current.id : "",
      title: title,
      summary: summary,
      content: body
    })
      .then(function (data) {
        state.busy = false;
        state.current = data.note;
        state.draft = null;
        state.note = "草稿已保存";
        return loadNotes(data.note.id);
      })
      .then(function () {
        paint();
      })
      .catch(function (err) {
        state.busy = false;
        state.error = err.message || "保存失败";
        paint();
      });
  }

  function remove() {
    if (!state.current) return Promise.resolve();
    return call("publish.delete", { id: state.current.id })
      .then(function () {
        state.current = null;
        state.draft = null;
        state.note = "草稿已删除";
        return loadNotes();
      })
      .then(function () {
        paint();
      })
      .catch(function (err) {
        state.error = err.message || "删除失败";
        paint();
      });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = h("textarea", { class: "pb-clip" });
      area.value = text;
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(area);
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }

  // ------------------------------------------------------------------ 渲染

  var STATUS_TEXT = {
    draft: "草稿",
    published: "已发布",
    withdrawn: "已撤回",
    failed: "失败"
  };

  function statusPill(status) {
    var key = String(status || "draft");
    return h("span", {
      class: "pb-status pb-status--" + key,
      text: STATUS_TEXT[key] || key
    });
  }

  var GATE_TEXT = { ok: "通过", todo: "待确认", blocked: "不通过" };

  /** 三道门禁卡片：能发的说清哪一关过了，不能发的说清卡在哪、还差什么。 */
  function gateCard() {
    var report = state.gates;
    if (!report || !report.gates) return null;
    var rows = report.gates.map(function (gate) {
      var status = String(gate.status || "todo");
      var items = (gate.items || []).map(function (item) {
        return h("li", { class: "pb-gate__item" + (item.ok ? " is-ok" : " is-bad") }, [
          h("span", { class: "pb-gate__mark", text: item.ok ? "✓" : "·" }),
          h("span", { text: item.text })
        ]);
      });
      return h("div", { class: "pb-gate__row" }, [
        h("div", { class: "pb-gate__head" }, [
          h("span", { class: "pb-gate__dot pb-gate__dot--" + status }),
          h("span", { class: "pb-gate__label", text: gate.label }),
          h("span", { class: "pb-gate__state", text: GATE_TEXT[status] || status })
        ]),
        gate.reason
          ? h("div", { class: "pb-gate__reason pb-gate__reason--" + status, text: gate.reason })
          : null,
        items.length ? h("ul", { class: "pb-gate__items" }, items) : null
      ]);
    });
    return h("div", { class: "pb-card pb-gate", "data-ready": report.ready ? "1" : "0" }, [
      h("div", { class: "pb-card__head" }, [
        h("span", { class: "pb-card__title", text: "发布门禁" }),
        h("span", {
          class: "pb-card__count",
          text: report.ready ? "三关都过了" : "还不能发"
        })
      ]),
      h("div", { class: "pb-gates" }, rows),
      h("div", {
        class: "pb-hint",
        text: "三关都过了才允许点「发布笔记」；限流按账号算（距上次发布 60 秒），不按视频算。"
      })
    ]);
  }

  function listCard() {
    var rows = state.notes.map(function (item) {
      var active = state.current && state.current.id === item.id;
      var row = h("button", {
        class: "pb-row" + (active ? " is-active" : ""),
        type: "button",
        title: item.title
      }, [
        h("div", { class: "pb-row__title", text: item.title || "（无标题）" }),
        h("div", { class: "pb-row__meta" }, [
          h("span", { class: "pb-row__proj", text: item.project_title || "未知项目" }),
          h("span", { text: "·" }),
          h("span", { text: item.up_name || "UP 未知" }),
          h("span", { text: "·" }),
          h("span", { text: (item.chars || 0) + " 字" }),
          h("span", { text: "·" }),
          // 更新时间而不是创建时间：用户改完标题再看列表，看到的得是刚才那次改动
          h("span", { text: stamp(item.updated_at || item.created_at) })
        ]),
        statusPill(item.status)
      ]);
      row.addEventListener("click", function () {
        state.error = "";
        state.note = "";
        loadNote(item.id).then(paint, function (err) {
          state.error = err.message || "读取失败";
          paint();
        });
      });
      return row;
    });

    if (!rows.length) {
      rows.push(h("div", { class: "pb-empty", text: "还没有草稿。选个视频、点「从总结生成」。" }));
    }

    return h("div", { class: "pb-card" }, [
      h("div", { class: "pb-card__head" }, [
        h("span", { class: "pb-card__title", text: "草稿" }),
        h("span", { class: "pb-card__count", text: state.notes.length + " 份" })
      ]),
      h("div", { class: "pb-list" }, rows)
    ]);
  }

  function recordsCard() {
    var total = state.records.length;
    var rows = state.records.map(function (item) {
      var meta = [];
      meta.push(h("span", { text: item.project_title || "未知项目" }));
      if (item.note_id) {
        meta.push(h("span", { text: "·" }));
        meta.push(h("span", { text: "note_id " + item.note_id }));
      }
      meta.push(h("span", { text: "·" }));
      // 撤回过的看撤回时间，否则看发布时间——用户关心的是"最后一次动作"发生在什么时候
      meta.push(h("span", { text: stamp(item.withdrawn_at || item.published_at) }));

      var actions = [];
      if (item.url) {
        actions.push(btn("打开视频", item.url, function () { openUrl(item.url); }, { tiny: true }));
      }
      // 不叫「查看草稿」：记录里这条早就发出去了，点开是只读的一版快照
      actions.push(btn("查看详情", "打开这份记录对应的那一版内容", function () { showRecord(item.id); }, { tiny: true }));

      return h("div", { class: "pb-record" + (item.status === "failed" ? " pb-record--bad" : "") }, [
        h("div", { class: "pb-record__head" }, [
          h("span", { class: "pb-record__title", text: item.title || "（无标题）" }),
          statusPill(item.status)
        ]),
        h("div", { class: "pb-record__meta" }, meta),
        item.status === "failed" && item.error
          ? h("div", { class: "pb-record__err", text: item.error })
          : null,
        h("div", { class: "pb-record__acts" }, actions)
      ]);
    });

    if (!rows.length) {
      rows.push(h("div", { class: "pb-empty", text: "还没有发布记录。发出去或撤回之后会留在这里。" }));
    }

    var summary = state.recordCounts;
    return h("div", { class: "pb-card" }, [
      h("div", { class: "pb-card__head" }, [
        h("span", { class: "pb-card__title", text: "发布记录" }),
        h("span", {
          class: "pb-card__count",
          text: total
            ? "已发 " + summary.published + " · 已撤 " + summary.withdrawn +
              (summary.failed ? " · 失败 " + summary.failed : "")
            : "0 条"
        })
      ]),
      h("div", { class: "pb-records" }, rows)
    ]);
  }

  function pickCard() {
    var select = h("select", { class: "pb-select", id: "pb-project" },
      state.projects.map(function (item) {
        var node = h("option", {
          value: item.id,
          text: item.title + (item.has_summary === false ? "（还没总结）" : "")
        });
        if (item.id === state.projectId) node.selected = true;
        return node;
      })
    );
    select.addEventListener("change", function () {
      state.projectId = select.value;
    });

    return h("div", { class: "pb-card pb-card--pick" }, [
      h("div", { class: "pb-card__head" }, [
        h("span", { class: "pb-card__title", text: "新建草稿" }),
        h("span", { class: "pb-card__count", text: "取最新一版总结" })
      ]),
      h("div", { class: "pb-pick" }, [
        select,
        btn(state.busy ? "生成中…" : "从总结生成", "用最新一版总结拼出标题、摘要和正文", generate, {
          primary: true,
          disabled: state.busy
        })
      ]),
      h("div", { class: "pb-hint", text: "标题 ≤ 80 字、摘要 ≤ 200 字；超了会被拦下，不会偷偷截断。" })
    ]);
  }

  function confirmDelete() {
    return new Promise(function (resolve) {
      var modal = ui.modal({
        title: "删除这份草稿？",
        body: h("p", { text: "只删本地草稿，B站 上什么都没发出去过。" }),
        footer: h("div", { class: "modal__actions" }, [
          btn("取消", "", function () {
            modal.close();
            resolve(false);
          }),
          btn("删除", "", function () {
            modal.close();
            resolve(true);
          }, { danger: true, primary: true })
        ])
      });
    });
  }

  /** 勾/取消「我已阅读并确认」：门禁状态要跟着变，不用等点发布才发现少一步。 */
  function setConfirmed(checked) {
    state.confirmed = !!checked;
    if (!state.current) {
      paint();
      return Promise.resolve();
    }
    var id = state.current.id;
    return call("publish.get", { id: id, confirmed: state.confirmed })
      .then(function (data) {
        state.current = data.note;
        state.gates = data.gates || null;
        paint();
      })
      .catch(function (error) {
        state.error = (error && error.message) || "门禁状态刷新失败";
        paint();
      });
  }

  function publishReady() {
    return !!(state.gates && state.gates.ready);
  }

  function gateBlockedText() {
    if (!state.gates) return "门禁状态还没拿到";
    var bad = (state.gates.gates || []).filter(function (g) { return g.status !== "ok"; });
    if (!bad.length) return "";
    return bad.map(function (g) { return g.label + "：" + (g.reason || g.status); }).join("；");
  }

  function confirmPublish() {
    return new Promise(function (resolve) {
      var modal = ui.modal({
        title: "发布这篇笔记？",
        body: h("div", {}, [
          h("p", { text: "笔记会发到视频《" + ((state.current && state.current.project_title) || "") + "》下，并自动出现在评论区。" }),
          h("p", { class: "muted", text: "发布之后不能改，只能撤回；撤回后评论区里的笔记卡也会消失。" })
        ]),
        footer: h("div", { class: "modal__actions" }, [
          btn("再想想", "", function () {
            modal.close();
            resolve(false);
          }),
          btn("发布", "", function () {
            modal.close();
            resolve(true);
          }, { primary: true, danger: true })
        ])
      });
    });
  }

  function publishNow() {
    if (!state.current || state.publishing) return Promise.resolve();
    if (!publishReady()) {
      ui.toast(gateBlockedText() || "门禁没过", "error");
      return Promise.resolve();
    }
    return confirmPublish().then(function (yes) {
      if (!yes) return null;
      state.publishing = true;
      state.error = "";
      state.note = "正在发布…";
      paint();
      return call("publish.publish", { id: state.current.id, confirmed: true })
        .then(function (data) {
          state.note = "已发布（note_id " + (data.note_id || "未知") + "）";
          ui.toast("已发布");
          return Promise.all([loadNotes(state.current.id), loadRecords()]);
        })
        .catch(function (error) {
          state.error = (error && error.message) || "发布失败";
          state.note = "";
          return Promise.all([loadNotes(state.current.id), loadRecords()]);
        })
        .then(function () {
          state.publishing = false;
          paint();
        });
    });
  }

  function confirmWithdraw() {
    return new Promise(function (resolve) {
      var modal = ui.modal({
        title: "撤回这篇笔记？",
        body: h("p", { text: "撤回后评论区里的笔记卡会消失，本地记录保留为「已撤回」。" }),
        footer: h("div", { class: "modal__actions" }, [
          btn("取消", "", function () {
            modal.close();
            resolve(false);
          }),
          btn("撤回", "", function () {
            modal.close();
            resolve(true);
          }, { primary: true, danger: true })
        ])
      });
    });
  }

  function withdrawNow() {
    if (!state.current || state.publishing) return Promise.resolve();
    return confirmWithdraw().then(function (yes) {
      if (!yes) return null;
      state.publishing = true;
      state.error = "";
      state.note = "";
      paint();
      return call("publish.withdraw", { id: state.current.id })
        .then(function () {
          ui.toast("已撤回");
          return Promise.all([loadNotes(state.current.id), loadRecords()]);
        })
        .catch(function (error) {
          state.error = (error && error.message) || "撤回失败";
          return Promise.all([loadNotes(state.current.id), loadRecords()]);
        })
        .then(function () {
          state.publishing = false;
          paint();
        });
    });
  }

  function editor() {
    var note = state.current;
    var draft = state.draft;
    var source = draft || note;
    if (!source) {
      return h("div", { class: "pb-card pb-card--empty" }, [
        h("div", { class: "pb-empty", text: "左边选一份草稿，或者先「从总结生成」。" })
      ]);
    }

    var title = draft ? draft.title : note.title;
    var summary = draft ? draft.summary : note.summary;
    var body = source.content_text || "";

    var titleInput = h("input", {
      class: "pb-input",
      id: "pb-title",
      type: "text",
      value: title,
      placeholder: "笔记标题"
    });
    var summaryInput = h("textarea", {
      class: "pb-textarea",
      id: "pb-summary",
      rows: "3",
      placeholder: "摘要（评论区卡片上显示的就是它）"
    });
    summaryInput.value = summary;

    var bodyNode = h("div", { class: "pb-body", id: "pb-body" });
    bodyNode.textContent = body;

    var titleCount = h("span", { class: "pb-count", text: len(title) + " / 80" });
    var summaryCount = h("span", { class: "pb-count", text: len(summary) + " / 200" });
    var bodyCount = h("span", { class: "pb-count", text: chars(body) + " 字" });

    titleInput.addEventListener("input", function () {
      var n = len(titleInput.value);
      titleCount.textContent = n + " / 80";
      titleCount.className = "pb-count" + (n > 80 ? " is-over" : "");
    });
    summaryInput.addEventListener("input", function () {
      var n = len(summaryInput.value);
      summaryCount.textContent = n + " / 200";
      summaryCount.className = "pb-count" + (n > 200 ? " is-over" : "");
    });

    var warnings = (draft && draft.warnings) || [];
    var warnBox = warnings.length
      ? h("div", { class: "callout callout--warn", text: warnings.join("；") })
      : null;

    var actions = h("div", { class: "pb-actions" }, [
      btn(state.busy ? "保存中…" : "保存草稿", "存在本地，不会发出去", save, {
        primary: true,
        disabled: state.busy
      }),
      note
        ? btn(state.regenerating ? "生成中…" : "重新生成摘要", "按最新一版总结重写摘要，标题与正文不动", function () {
            regenerate("summary");
          }, { disabled: state.regenerating })
        : null,
      btn("复制正文", "复制成纯文本，直接贴到 B站", function () {
        copyText(body).then(
          function () {
            ui.toast("正文已复制");
          },
          function () {
            ui.toast("复制失败，请手动选中", "error");
          }
        );
      }),
      note && note.status === "draft"
        ? btn("删除", "只删本地草稿", function () {
            confirmDelete().then(function (ok) {
              if (ok) remove();
            });
          }, { danger: true })
        : null
    ]);

    // 确认框与发布/撤回单独一行：这是"不可撤销"的入口，不该挤在一排灰按钮里
    var publishRow = null;
    if (note) {
      if (note.status === "published") {
        publishRow = h("div", { class: "pb-publish" }, [
          h("span", { class: "pb-publish__hint", text: "已发布，note_id " + (note.note_id || "未知") }),
          btn(state.publishing ? "撤回中…" : "撤回笔记", "按 note_id 撤回，撤回后评论区卡片消失", withdrawNow, {
            danger: true,
            disabled: state.publishing
          })
        ]);
      } else {
        var box = h("input", { type: "checkbox", id: "pb-confirm", class: "pb-publish__check" });
        box.checked = !!state.confirmed;
        box.addEventListener("change", function () {
          setConfirmed(box.checked);
        });
        publishRow = h("div", { class: "pb-publish" }, [
          h("label", { class: "pb-publish__label", for: "pb-confirm" }, [
            box,
            h("span", { text: "我已阅读并确认标题、摘要、正文都要发出去" })
          ]),
          btn(
            state.publishing ? "发布中…" : "发布笔记",
            publishReady() ? "会真的发到 B站，发出去只能撤回" : gateBlockedText() || "门禁没过",
            publishNow,
            { primary: true, disabled: !publishReady() || state.publishing }
          )
        ]);
        if (!publishReady()) {
          publishRow.appendChild(
            h("span", { class: "pb-publish__blocked", text: gateBlockedText() })
          );
        }
      }
    }

    if (!note) {
      // 还没存过的草稿：不摆一个点不动的按钮，直接说清下一步做什么。
      // （发布读的是库里那一行，没存就发，等于让用户对着屏幕猜自己发的是什么。）
      publishRow = h("div", { class: "pb-publish" }, [
        h("span", {
          class: "pb-publish__hint",
          text: "先点「保存草稿」：发布会用库里这一版的标题、摘要与正文"
        })
      ]);
    }

    return h("div", { class: "pb-card pb-card--edit" }, [
      h("div", { class: "pb-card__head" }, [
        h("span", { class: "pb-card__title", text: draft ? "新草稿（未保存）" : "草稿详情" }),
        note ? statusPill(note.status) : null,
        h("span", { class: "pb-card__count", text: (note && note.project_title) || "" })
      ]),
      warnBox,
      h("label", { class: "pb-field" }, [
        h("span", { class: "pb-field__label" }, [h("span", { text: "标题" }), titleCount]),
        titleInput
      ]),
      h("label", { class: "pb-field" }, [
        h("span", { class: "pb-field__label" }, [h("span", { text: "摘要" }), summaryCount]),
        summaryInput
      ]),
      h("div", { class: "pb-field" }, [
        h("span", { class: "pb-field__label" }, [h("span", { text: "正文" }), bodyCount]),
        bodyNode
      ]),
      actions,
      publishRow
    ]);
  }

  function paint() {
    var host = document.getElementById("publish-host");
    if (!host) return;
    host.textContent = "";
    host.appendChild(
      h("div", { class: "pb-grid" }, [
        h("div", { class: "pb-side" }, [listCard(), pickCard(), recordsCard()]),
        h("div", { class: "pb-main" }, [editor(), gateCard()])
      ])
    );
    var errorNode = document.getElementById("publish-error");
    if (errorNode) {
      errorNode.textContent = state.error || "";
      errorNode.style.display = state.error ? "" : "none";
      errorNode.className = "callout callout--warn" + (state.error ? "" : " is-hidden");
    }
    var noteNode = document.getElementById("pb-note");
    if (noteNode) noteNode.textContent = state.note || "";
  }

  function render() {
    state.error = "";
    state.note = "";
    state.gates = null;
    var page = ui.h("section", { class: "page" });
    page.appendChild(
      h("div", { class: "pb-bar" }, [
        h("span", {
          class: "pb-bar__hint",
          text: "发布不可撤销：三关门禁全绿 + 勾确认之后才能点「发布笔记」。"
        }),
        h("span", { class: "muted", id: "pb-note" })
      ])
    );
    page.appendChild(ui.h("div", { class: "callout callout--warn is-hidden", id: "publish-error" }));
    page.appendChild(ui.h("div", { id: "publish-host" }));

    Promise.all([loadProjects(), loadNotes(), loadRecords()])
      .then(function () {
        paint();
      })
      .catch(function (error) {
        state.error = (error && error.message) || "加载失败";
        paint();
      });
    return page;
  }

  WB.pages = WB.pages || {};
  WB.pages.publish = {
    render: render,
    state: state,
    _internals: {
      state: state,
      loadNotes: loadNotes,
      loadNote: loadNote,
      generate: generate,
      save: save,
      remove: remove,
      chars: chars,
      len: len,
      regenerate: regenerate,
      setConfirmed: setConfirmed,
      publishNow: publishNow,
      withdrawNow: withdrawNow,
      gateCard: gateCard,
      paint: paint
    }
  };

  WB.router.define("publish", {
    title: "发布",
    crumb: "草稿与字数门禁",
    icon: "send",
    nav: true,
    render: render
  });
})();
