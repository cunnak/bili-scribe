/* ============================================================================
 * components/ui.js —— 基础件：DOM 构造、图标、toast、弹窗、格式化
 *
 * **一律不用 innerHTML 拼业务数据。** 项目标题、UP 名、错误信息都来自外部
 * （B站 接口 / LLM 输出 / 本地文件名），拼进 innerHTML 等于把注入面直接交给它们。
 * 需要 HTML 的地方（图标、固定文案）单独走 `WB.ui.svg()`，那里不接受变量。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = (WB.ui = WB.ui || {});

  // ---------------------------------------------------------------- DOM

  /**
   * 建节点。
   * @param {string} tag
   * @param {object} [attrs]  class / text / html(仅固定文案) / on* / 其余走 setAttribute
   * @param {Array|Node|string} [children]
   */
  function h(tag, attrs, children) {
    var node = document.createElement(tag);
    var a = attrs || {};
    Object.keys(a).forEach(function (key) {
      var value = a[key];
      if (value === null || value === undefined || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key === "html") node.innerHTML = value; // 只给固定文案用
      else if (key === "dataset") {
        Object.keys(value).forEach(function (d) {
          node.dataset[d] = String(value[d]);
        });
      } else if (key.slice(0, 2) === "on" && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "checked" || key === "selected" || key === "disabled" || key === "value") {
        node[key] = value;
      } else {
        node.setAttribute(key, String(value));
      }
    });
    append(node, children);
    return node;
  }

  function append(parent, children) {
    if (children === null || children === undefined || children === false) return parent;
    if (Array.isArray(children)) {
      children.forEach(function (child) {
        append(parent, child);
      });
      return parent;
    }
    parent.appendChild(children instanceof Node ? children : document.createTextNode(String(children)));
    return parent;
  }

  ui.h = h;
  ui.append = append;

  // ---------------------------------------------------------------- 图标

  var ICONS = {
    plus: '<path d="M8 3v10M3 8h10" stroke-linecap="round"/>',
    library: '<path d="M3 3h4v10H3zM9 3h4v10H9z" stroke-linejoin="round"/>',
    scissors:
      '<circle cx="4.5" cy="4.5" r="1.8"/><circle cx="4.5" cy="11.5" r="1.8"/><path d="M6 6l7 6M6 10l7-6" stroke-linecap="round"/>',
    merge: '<path d="M3 4h4l6 8h-4M3 12h4l6-8h-4" stroke-linecap="round" stroke-linejoin="round"/>',
    send: '<path d="M14 2 7 9M14 2l-4 12-3-5-5-3z" stroke-linejoin="round"/>',
    gear:
      '<circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1"/>',
    folder:
      '<path d="M2 5.2A1.2 1.2 0 0 1 3.2 4h3l1.2 1.6h5.4A1.2 1.2 0 0 1 14 6.8v5.4A1.2 1.2 0 0 1 12.8 13H3.2A1.2 1.2 0 0 1 2 11.8z" stroke-linejoin="round"/>',
    spinner: '<path d="M8 2.4a5.6 5.6 0 1 1-5.6 5.6" stroke-linecap="round"/>',
    check: '<path d="M3 8.5l3.2 3.2L13 5" stroke-linecap="round" stroke-linejoin="round"/>',
    alert: '<path d="M8 3l5.5 10h-11z" stroke-linejoin="round"/><path d="M8 6.6v2.6M8 11.2v.6" stroke-linecap="round"/>',
    film: '<rect x="2" y="3.5" width="12" height="9" rx="1.2"/><path d="M5 3.5v9M11 3.5v9M2 7h12" stroke-linecap="round"/>',
    note: '<path d="M4 2.4h5.2L13 6.2v7.4a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3.4a1 1 0 0 1 1-1z" stroke-linejoin="round"/><path d="M9 2.6v3.8h3.8M5.4 9h5.2M5.4 11.4h3.4" stroke-linecap="round"/>',
    trash:
      '<path d="M3.4 4.8h9.2M6.4 4.8V3.4h3.2v1.4M4.8 4.8l.4 7.8a.9.9 0 0 0 .9.8h3.8a.9.9 0 0 0 .9-.8l.4-7.8M6.6 7v4M9.4 7v4" stroke-linecap="round" stroke-linejoin="round"/>'
  };

  /** 固定图标。**不接受变量**，所以这里的 HTML 拼接是安全的。 */
  ui.svg = function (name, extraClass) {
    var body = ICONS[name] || "";
    return (
      '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" class="' +
      (extraClass || "") +
      '">' +
      body +
      "</svg>"
    );
  };

  // ---------------------------------------------------------------- toast

  ui.toast = function (message, level, timeoutMs) {
    var host = document.getElementById("toasts");
    if (!host) return null;
    var levelName = level || "info";
    var node = h("div", { class: "toast toast--" + levelName, role: "status", text: String(message) });
    host.appendChild(node);
    // 错误留在屏幕上久一点：读者往往正在看别处，一闪而过等于没提示。
    var ttl = timeoutMs || (levelName === "error" ? 6000 : 3000);
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, ttl);
    return node;
  };

  // ---------------------------------------------------------------- 弹窗

  /** 打开一个弹窗，返回 { close }。ESC 与点遮罩都能关。 */
  ui.modal = function (options) {
    var opts = options || {};
    var root = document.getElementById("modal-root");
    if (!root) return { close: function () {} };
    var body = h("div", { class: "modal__body" }, opts.body || null);
    var foot = opts.footer || null;

    var close = function () {
      // panel 的父节点是 overlay，不是 #modal-root —— 之前写 root.removeChild(panel)
      // 会抛 NotFoundError（node to be removed is not a child of this node），
      // 弹窗关不掉、后面的 resolve(true) 也永远走不到，用户看到的是「点了发布没反应」。
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      document.removeEventListener("keydown", onKey, true);
    };
    var onKey = function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };

    var panel = h("div", { class: "modal__panel", role: "dialog", "aria-modal": "true" }, [
      h("div", { class: "modal__head" }, [
        h("span", { text: opts.title || "" }),
        h("span", { class: "topbar__spacer" }),
        h("button", {
          class: "btn btn--ghost btn--icon modal__x",
          title: "关闭",
          "aria-label": "关闭",
          text: "×",
          onclick: close
        })
      ]),
      body,
      foot ? h("div", { class: "modal__foot" }, foot) : null
    ]);
    var overlay = h("div", { class: "modal", onclick: function (event) {
      if (event.target === overlay) close();
    } }, [panel]);

    root.appendChild(overlay);
    document.addEventListener("keydown", onKey, true);
    return { close: close, body: body };
  };

  ui.confirm = function (title, message, options) {
    var opts = options || {};
    var label = opts.confirmText || "确认";
    return new Promise(function (resolve) {
      var settled = false;
      var dialog = ui.modal({
        title: title,
        body: h("p", { class: "page__hint", text: message }),
        footer: [
          h("button", {
            class: "btn",
            text: "取消",
            onclick: function () {
              if (!settled) {
                settled = true;
                resolve(false);
              }
              dialog.close();
            }
          }),
          h("button", {
            class: "btn " + (opts.danger ? "btn--danger" : "btn--primary"),
            text: label,
            onclick: function () {
              if (!settled) {
                settled = true;
                resolve(true);
              }
              dialog.close();
            }
          })
        ]
      });
    });
  };

  // ---------------------------------------------------------------- 空态

  ui.empty = function (title, hint, action) {
    return h("div", { class: "empty" }, [
      h("div", { html: ui.svg("folder") }),
      h("div", { class: "empty__title", text: title }),
      hint ? h("div", { class: "empty__hint", text: hint }) : null,
      action || null
    ]);
  };

  /** 统一的"这里还没做"占位：Phase 0 的五个页面用它，Phase 1+ 逐个替换。 */
  ui.placeholder = function (title, hint, todos) {
    var list = (todos || []).map(function (item) {
      return h("li", { text: item });
    });
    return h("div", { class: "card" }, [
      h("div", { class: "card__body" }, [
        h("div", { class: "empty__title", text: title }),
        h("p", { class: "page__hint", text: hint }),
        list.length ? h("ul", { class: "col", style: "margin:12px 0 0;padding-left:18px" }, list) : null
      ])
    ]);
  };

  // ---------------------------------------------------------------- 格式化

  ui.formatDuration = function (seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var hh = Math.floor(total / 3600);
    var mm = Math.floor((total % 3600) / 60);
    var ss = total % 60;
    var pad = function (n) {
      return n < 10 ? "0" + n : String(n);
    };
    return hh > 0 ? hh + ":" + pad(mm) + ":" + pad(ss) : mm + ":" + pad(ss);
  };

  ui.formatBytes = function (bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    var units = ["KB", "MB", "GB", "TB"];
    var index = -1;
    do {
      n /= 1024;
      index++;
    } while (n >= 1024 && index < units.length - 1);
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + " " + units[index];
  };

  ui.formatPercent = function (value) {
    return Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100) + "%";
  };

  // 状态 → 语义色/文案。集中一处，避免每个页面各写一套中文。
  var STATUS = {
    ready: { text: "就绪", cls: "pill--acid" },
    done: { text: "完成", cls: "pill--acid" },
    running: { text: "进行中", cls: "pill--acid" },
    queued: { text: "排队", cls: "" },
    pending: { text: "待处理", cls: "" },
    failed: { text: "失败", cls: "pill--ember" },
    canceled: { text: "已取消", cls: "" },
    paused: { text: "暂停", cls: "" }
  };

  // 项目阶段 → 中文。与后端 stage 字段一一对应（download_worker / importer 写入）。
  var STAGES = {
    new: { text: "待转录", cls: "" },
    downloaded: { text: "已下载", cls: "pill--acid" },
    subtitle: { text: "有字幕", cls: "pill--acid" },
    transcribed: { text: "已转录", cls: "pill--acid" },
    summarized: { text: "已总结", cls: "pill--acid" },
    clipped: { text: "已切片", cls: "pill--acid" },
    merged: { text: "已合并", cls: "pill--acid" },
    published: { text: "已发布", cls: "pill--acid" }
  };
  ui.STAGES = STAGES;

  ui.stageText = function (stage) {
    return (STAGES[stage] || {}).text || stage || "未知";
  };

  ui.stagePill = function (stage) {
    var meta = STAGES[stage] || { text: stage || "未知", cls: "" };
    return h("span", { class: "pill " + meta.cls, "data-stage": stage || "", text: meta.text });
  };

  ui.statusPill = function (status) {
    var meta = STATUS[status] || { text: status || "未知", cls: "" };
    return h("span", { class: "pill " + meta.cls, text: meta.text });
  };

  ui.statusText = function (status) {
    return (STATUS[status] || {}).text || status || "未知";
  };
})();
