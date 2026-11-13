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
    subtitles: '<rect x="2" y="3" width="12" height="10" rx="1.2"/><path d="M4.5 6.5h7M4.5 9.5h4.5" stroke-linecap="round"/>',
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
    // 等级名归一：调用点里 "warn" 与 "warning" 并存，而 CSS 只有 .toast--warning。
    // 治本放在这里，不改 30 个调用点。
    var levelName = (level || "info") === "warn" ? "warning" : (level || "info");
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

  /** 弹窗内可聚焦控件（Tab 陷阱与"打开时聚焦"共用一份选择器）。 */
  var FOCUSABLE = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    '[tabindex]:not([tabindex="-1"])'
  ].join(",");

  function focusables(root) {
    if (!root || typeof root.querySelectorAll !== "function") return [];
    var list = root.querySelectorAll(FOCUSABLE);
    var out = [];
    for (var i = 0; i < list.length; i++) {
      // offsetParent 为 null 的（display:none / hidden）不该进循环：
      // 焦点落到看不见的控件上等于焦点消失。
      if (list[i].offsetParent !== null) out.push(list[i]);
    }
    return out;
  }

  /**
   * 打开一个弹窗，返回 { close, body }。ESC 与点遮罩都能关。
   * ``onClose`` 在任何一种关闭方式下都会跑一次——需要"关闭即结束"的调用方
   * （比如 ui.pick）靠它兜底，否则 ES​C 一按 Promise 就永远悬着。
   */
  ui.modal = function (options) {
    var opts = options || {};
    var root = document.getElementById("modal-root");
    if (!root) {
      // 没有挂载点（页面结构被改坏、或某个用例少铺了容器）时，弹窗根本弹不出来。
      // 这时**必须**把 onClose 跑一次：靠它兜底的调用方（ui.confirm / 发布确认框）
      // 否则会等一个永远不会来的 resolve —— 和 ESC 那条事故是同一个形状。
      if (typeof opts.onClose === "function") opts.onClose();
      return { close: function () {}, body: null };
    }
    var body = h("div", { class: "modal__body" }, opts.body || null);
    var foot = opts.footer || null;
    var closed = false;
    // 打开前谁拿着焦点：关闭时要还回去，否则焦点落回 <body>，
    // 键盘用户得从页首重新 Tab 一遍才能回到刚才那个按钮。
    var opener = document.activeElement;

    var close = function () {
      // panel 的父节点是 overlay，不是 #modal-root —— 之前写 root.removeChild(panel)
      // 会抛 NotFoundError（node to be removed is not a child of this node），
      // 弹窗关不掉、后面的 resolve(true) 也永远走不到，用户看到的是「点了发布没反应」。
      if (closed) return;
      closed = true;
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      document.removeEventListener("keydown", onKey, true);
      // 焦点还回去：只在原节点仍挂在文档里时才还，否则焦点会落到一个已卸载的节点上。
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        try {
          opener.focus();
        } catch (_err) {
          /* 老 WebView 对已隐藏节点 focus() 会抛，不阻断关闭 */
        }
      }
      if (typeof opts.onClose === "function") opts.onClose();
    };
    var onKey = function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
        return;
      }
      // 焦点陷阱：Tab / Shift+Tab 在 panel 内循环，不许跑到遮罩后面的页面上。
      // 没有这一条时 role="dialog" aria-modal="true" 只是一句声明——Tab 照样能出去。
      if (event.key === "Tab") trapTab(event);
    };
    /** 把 Tab 的落点收回 panel：可聚焦控件为空时留在 panel 自己身上。 */
    var trapTab = function (event) {
      var nodes = focusables(panel);
      if (!nodes.length) {
        event.preventDefault();
        panel.focus();
        return;
      }
      var first = nodes[0];
      var last = nodes[nodes.length - 1];
      var active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || active === panel || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
        return;
      }
      if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };

    var panel = h("div", { class: "modal__panel", role: "dialog", "aria-modal": "true", tabindex: "-1" }, [
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
    // 打开即把焦点移进弹窗：否则读屏与键盘用户还在页面原处，弹窗对他们是隐形的。
    // 落在 panel 自己身上（tabindex="-1"）而不是首个控件：自动聚焦到单选/输入框
    // 会顺手选中或弹出软键盘，那不是"打开弹窗"该有的副作用；panel 拿到焦点后
    // 第一次 Tab 自然落到第一个可聚焦子节点上。
    try {
      panel.focus();
    } catch (_err) {
      /* 老 WebView 对未布局节点 focus() 会抛，不阻断弹窗 */
    }
    return { close: close, body: body };
  };


  /**
   * 要一个字符串。返回 Promise：确定 resolve(新值)，任何方式关闭 resolve(null)。
   *
   * 为什么不复用 ui.confirm 再让调用方自己读输入框：`ui.modal` 有四条关闭路径
   * （确定 / 取消 / ESC / 点遮罩），只有把「结束」挂进 close 本身 Promise 才不会挂死。
   * 同一族坑在 ui.pick 上已经踩过一次。
   *
   * opts = { title, label, value, placeholder, confirmText, hint, validate }
   * `validate(next)` 返回错误文案就不允许提交（比如「名字不能为空」）。
   */
  ui.prompt = function (opts) {
    var o = opts || {};
    var settled = false;
    var resolvePromise = null;
    var dialog = null;
    // ui.h 的属性是**平铺**写法，没有 attrs 这一层（那是 subtitle_style_form 里
    // 自己包的一层 n()）。这里写成嵌套对象会被 setAttribute 成 attrs="[object Object]"，
    // aria-label 与 data-field 一起丢失——无障碍名没了，判据也定位不到这个框。
    var input = h("input", {
      class: "input",
      type: "text",
      value: o.value == null ? "" : String(o.value),
      placeholder: o.placeholder || "",
      "aria-label": o.label || o.title || "输入",
      "data-field": "prompt"
    });
    var hint = h("div", { class: "field__help", text: o.hint || "" });
    var errorLine = h("div", { class: "field__help field__help--danger", text: "" });
    errorLine.hidden = true;

    function finish(value) {
      if (settled) return;
      settled = true;
      if (resolvePromise) resolvePromise(value);
    }

    function submit() {
      var next = input.value.trim();
      var bad = "";
      if (typeof o.validate === "function") bad = o.validate(next) || "";
      if (bad) {
        errorLine.textContent = bad;
        errorLine.hidden = false;
        input.focus();
        return;
      }
      finish(next);
      if (dialog) dialog.close();
    }

    return new Promise(function (resolve) {
      resolvePromise = resolve;
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          submit();
        }
      });
      dialog = ui.modal({
        title: o.title || "输入",
        body: h("label", { class: "field" }, [
          h("span", { class: "field__label", text: o.label || "" }),
          input,
          hint,
          errorLine
        ]),
        footer: [
          h("button", {
            class: "btn",
            type: "button",
            text: "取消",
            onclick: function () {
              finish(null);
              dialog.close();
            }
          }),
          h("button", {
            class: "btn btn--primary",
            type: "button",
            text: o.confirmText || "确定",
            onclick: submit
          })
        ],
        // ESC / 点遮罩 / × 关掉时两个按钮都没被点：onClose 兜底成「放弃」
        onClose: function () {
          finish(null);
        }
      });
      try {
        input.focus();
        input.select();
      } catch (_err) {
        /* 老 WebView 对未布局节点 focus() 会抛，不阻断弹窗 */
      }
    });
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
        ],
        // 以前 ESC 关掉时两个按钮都没被点，Promise 永远悬着。onClose 兜底成交。
        onClose: function () {
          if (!settled) {
            settled = true;
            resolve(false);
          }
        }
      });
    });
  };

  /**
   * 选一个值，**一定**给结果。返回 Promise：选中 resolve(值)，任何方式关闭 resolve(null)。
   *
   * 为什么不拿 ui.modal 自己拼：它有关闭四条路径（× / ESC / 点遮罩 / 代码 close），
   * 只有把"结束"挂进 close 本身，Promise 才不会挂死。没有结果就关掉的语义是
   * 「用户放弃了」，调用方必须能区分它和「选了自动检测」——后者是空字符串。
   *
   * opts = { title, hint, options: [{code,label}], more: [...], moreLabel, confirmText }
   */
  var pickSeq = 0;

  ui.pick = function (opts) {
    var o = opts || {};
    var settled = false;
    var chosen = null;
    var hasChoice = false;
    var dialog = null;
    var confirmBtn = null;
    var resolvePromise = null;
    var radioName = "wb-pick-" + ++pickSeq;

    function finish(value) {
      if (settled) return;
      settled = true;
      if (resolvePromise) resolvePromise(value);
    }

    function rows(items) {
      var list = h("div", { class: "langpick__list" });
      (items || []).forEach(function (item) {
        var input = h("input", { type: "radio", name: radioName, value: item.code });
        input.addEventListener("change", function () {
          chosen = item.code;
          hasChoice = true;
          if (confirmBtn) confirmBtn.disabled = false;
        });
        list.appendChild(
          h("label", { class: "langpick__item" }, [
            input,
            h("span", { class: "langpick__name", text: item.label || item.code || "自动检测" }),
            item.code ? h("span", { class: "langpick__code", text: item.code }) : null
          ])
        );
      });
      return list;
    }

    var body = h("div", { class: "langpick" }, [
      o.hint ? h("p", { class: "langpick__hint", text: o.hint }) : null,
      rows(o.options),
      o.more && o.more.length
        ? h("details", { class: "langpick__more" }, [
            h("summary", { text: o.moreLabel || "更多语言" }),
            rows(o.more)
          ])
        : null
    ]);

    return new Promise(function (resolve) {
      resolvePromise = resolve;
      confirmBtn = h("button", {
        class: "btn btn--primary",
        text: o.confirmText || "确定",
        disabled: true,
        onclick: function () {
          if (!hasChoice) return;
          finish(chosen);
          if (dialog) dialog.close();
        }
      });
      dialog = ui.modal({
        title: o.title || "",
        body: body,
        footer: [
          h("button", {
            class: "btn",
            text: "取消",
            onclick: function () {
              finish(null);
              if (dialog) dialog.close();
            }
          }),
          confirmBtn
        ],
        onClose: function () {
          finish(null);
        }
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

  /**
   * 加载态：图标 + 一句**机制**文案（"读取素材库…"），不写"请稍候"这类空话。
   *
   * 为什么复用 `.empty` 而不新开一个类：三态在视觉上应当是同一块区域换内容，
   * 换类名会让每个页面各自长出一个「加载中的样子」。`.empty--loading` 只挂动画。
   *
   * **只用于首次加载。** 已有内容时的刷新走「按钮禁用 + 文案变更」
   * （summary.js 的 progressBlock()、clips.js 的 renderActions() 已是这个范式）——
   * 整块替换成 loading 会把键盘焦点吞掉。
   */
  ui.loading = function (text) {
    return h("div", { class: "empty empty--loading", role: "status", "aria-live": "polite" }, [
      h("div", { html: ui.svg("spinner") }),
      h("div", { class: "empty__hint", text: text || "读取中…" })
    ]);
  };

  /**
   * 错误态：**必须带出口**。opts = { title, message, retryText, onRetry }。
   *
   * `onRetry` 为空时才允许没有按钮，且那时 `message` 必须写清下一步
   * （例如"去 设置 → 模型 填好接口地址与 Key"）——报错后唯一出路是
   * "再点一次原动作、而原动作可能已不在视野里"的那批页面，就是这么来的。
   */
  ui.errorBlock = function (opts) {
    var o = opts || {};
    var kids = [h("div", { html: ui.svg("alert") })];
    kids.push(h("div", { class: "empty__title", text: o.title || "读取失败" }));
    if (o.message) kids.push(h("div", { class: "empty__hint", text: o.message }));
    if (o.onRetry) {
      kids.push(
        h("button", {
          class: "btn",
          type: "button",
          text: o.retryText || "重试",
          onclick: o.onRetry
        })
      );
    }
    return h("div", { class: "empty", role: "alert" }, kids);
  };

  /**
   * 从 RpcError 直接产出错误块——**全仓唯一消费 `retryable` 的地方**。
   *
   * 后端 errors.py 的 RETRYABLE_CODES 已经判定过"重试有没有意义"，前端不重新猜：
   * 说可重试才给按钮；不可重试的（VALIDATION / NOT_FOUND / LLM_NOT_CONFIGURED…）
   * 给了按钮只会让人白点一次，改为在 message 里写清该改哪儿。
   */
  ui.errorFrom = function (err, onRetry, title) {
    return ui.errorBlock({
      title: title || "读取失败",
      message: (err && err.message) || "未知原因",
      onRetry: (err && err.retryable && onRetry) ? onRetry : null
    });
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

  /** 徽标要就地改状态时用它取类名，省得重建整个 pill 节点。 */
  ui.statusClass = function (status) {
    return (STATUS[status] || {}).cls || "";
  };
})();
