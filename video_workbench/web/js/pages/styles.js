/* ============================================================================
 * pages/styles.js —— 字幕样式（P3-07）
 *
 * 布局照方案书 5.11：全宽两栏，左预设、右编辑 + 实时预览。
 *
 * 三条约束：
 *
 * 1. **内置样式只读**。改不动就直说（按钮禁用 + 一句"内置样式不可修改，
 *    请先复制一份"），不要让人点进去改半天才发现保存失败。
 * 2. **编辑中的值不落盘**。所有改动只进 `state.draft`，点保存才调
 *    `style.save`；否则"随手试一下字号"会污染样式库。
 * 3. **预览是近似的，真渲染才是证据**。画布按 ASS 的排版规则估（字号以像素、
 *    底边距从底往上、圆角条按文本宽算），但字体度量、换行、半透明叠加只有
 *    FFmpeg 说了算——所以有「真实渲染」按钮，失败就明说失败。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  // ui.h 的属性写法是"平铺"的，没有 attrs 这一层。这里包一层：本页要写的
  // aria-label / min / max / step 太多，平铺开会让每个控件都变成一行长调用。
  function n(tag, attrs, children) {
    var bag = attrs || {};
    var flat = {};
    Object.keys(bag).forEach(function (key) {
      if (key === "attrs" && bag.attrs) {
        Object.keys(bag.attrs).forEach(function (name) {
          flat[name] = bag.attrs[name];
        });
      } else {
        flat[key] = bag[key];
      }
    });
    return ui.h(tag, flat, children);
  }

  var FACES = [
    { key: "primary", label: "主字幕", hint: "原文（或译文在上时的第一行）" },
    { key: "secondary", label: "副字幕", hint: "译文那行；只在双语时出现" }
  ];

  var MODES = [["ass", "普通字幕（描边）"], ["rounded", "圆角背景条"]];
  var LAYOUTS = [
    ["original_top", "原文在上"],
    ["translated_top", "译文在上"],
    ["original_only", "仅原文"],
    ["translated_only", "仅译文"]
  ];
  var LENGTHS = [["short", "短句"], ["medium", "中句"], ["long", "长句"]];
  var ORIENTS = [["landscape", "16:9 横屏"], ["portrait", "9:16 竖屏"]];
  var BACKDROPS = [["dark", "深色"], ["light", "浅色"], ["image", "图片"]];
  var SAMPLES = {
    short: "这里是短句",
    medium: "先把反例摆出来，说明直觉为什么不可靠",
    long: "先把反例摆出来，说明直觉为什么不可靠；再把变量拆开，逐个控制做对比；最后落到可复现的操作顺序上"
  };

  var state = {
    list: [],
    id: "",
    builtin: false,
    draft: null,
    warnings: [],
    dirty: false,
    fonts: [],
    note: "",
    error: "",
    renderPath: "",
    renderNote: "",
    busy: false
  };

  // ------------------------------------------------------------ 小工具

  function copy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function face(key) {
    return (state.draft && state.draft[key]) || {};
  }

  function knob(faceKey, field, label, opts) {
    var conf = opts || {};
    var input = n("input", {
      class: "input styles__num",
      type: conf.type || "number",
      value: face(faceKey)[field],
      min: conf.min,
      max: conf.max,
      step: conf.step || 1,
      attrs: { "aria-label": label }
    });
    input.addEventListener("input", function () {
      var raw = conf.type === "color" ? input.value : Number(input.value);
      if (conf.type !== "color" && !isFinite(raw)) return;
      state.draft[faceKey][field] = raw;
      markDirty();
    });
    return n("label", { class: "field styles__field" }, [
      n("span", { class: "field__label", text: label }),
      input
    ]);
  }

  function faceBlock(spec) {
    var key = spec.key;
    var fontSelect = n("select", { class: "select", attrs: { "aria-label": spec.label + "字体" } });
    var current = face(key).font || "Microsoft YaHei";
    var fonts = state.fonts.slice();
    if (fonts.indexOf(current) < 0) fonts.unshift(current);
    fonts.forEach(function (name) {
      var opt = n("option", { text: name, attrs: { value: name } });
      if (name === current) opt.selected = true;
      fontSelect.appendChild(opt);
    });
    fontSelect.addEventListener("change", function () {
      state.draft[key].font = fontSelect.value;
      markDirty();
    });
    var bold = n("input", { type: "checkbox", attrs: { "aria-label": spec.label + "加粗" } });
    bold.checked = !!face(key).bold;
    bold.addEventListener("change", function () {
      state.draft[key].bold = bold.checked;
      markDirty();
    });
    return n("div", { class: "styles__block" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: spec.label }),
        n("span", { class: "styles__hint", text: spec.hint })
      ]),
      n("label", { class: "field styles__field" }, [
        n("span", { class: "field__label", text: "字体" }), fontSelect
      ]),
      n("div", { class: "styles__grid" }, [
        knob(key, "size", "字号", { min: 12, max: 120 }),
        knob(key, "outline_width", "描边", { min: 0, max: 10 }),
        knob(key, "spacing", "字间距", { min: -20, max: 20 }),
        knob(key, "color", "文字色", { type: "color" }),
        knob(key, "outline_color", "描边色", { type: "color" }),
        n("label", { class: "field styles__field" }, [
          n("span", { class: "field__label", text: "加粗" }), bold
        ])
      ])
    ]);
  }

  function roundedBlock() {
    var rnd = (state.draft && state.draft.rounded) || {};
    function num(field, label, min, max, step) {
      var input = n("input", {
        class: "input styles__num", type: "number",
        value: rnd[field], min: min, max: max, step: step || 1,
        attrs: { "aria-label": label }
      });
      input.addEventListener("input", function () {
        var raw = Number(input.value);
        if (!isFinite(raw)) return;
        state.draft.rounded[field] = raw;
        markDirty();
      });
      return n("label", { class: "field styles__field" }, [
        n("span", { class: "field__label", text: label }), input
      ]);
    }
    function color(field, label) {
      var input = n("input", {
        class: "input styles__num", type: "color", value: rnd[field],
        attrs: { "aria-label": label }
      });
      input.addEventListener("input", function () {
        state.draft.rounded[field] = input.value;
        markDirty();
      });
      return n("label", { class: "field styles__field" }, [
        n("span", { class: "field__label", text: label }), input
      ]);
    }
    var alpha = n("input", {
      class: "styles__range", type: "range", min: 0, max: 1, step: 0.05,
      value: rnd.bg_alpha, attrs: { "aria-label": "背景不透明度" }
    });
    var alphaLabel = n("span", { class: "styles__hint", text: Math.round((rnd.bg_alpha || 0) * 100) + "%" });
    alpha.addEventListener("input", function () {
      state.draft.rounded.bg_alpha = Number(alpha.value);
      alphaLabel.textContent = Math.round(Number(alpha.value) * 100) + "%";
      markDirty();
    });
    function toggle(field, label) {
      var box = n("input", { type: "checkbox", attrs: { "aria-label": label } });
      box.checked = rnd[field] !== false;
      box.addEventListener("change", function () {
        state.draft.rounded[field] = box.checked;
        markDirty();
      });
      return n("label", { class: "field styles__field" }, [
        n("span", { class: "field__label", text: label }), box
      ]);
    }
    return n("div", { class: "styles__block" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: "圆角背景条" }),
        n("span", { class: "styles__hint", text: "只在「圆角背景条」模式下生效；libass 没有真圆角，导出时按方框近似" })
      ]),
      n("div", { class: "styles__grid" }, [
        num("size", "字号", 12, 120),
        num("corner_radius", "圆角", 0, 40),
        num("padding_h", "左右内边距", 0, 200),
        num("padding_v", "上下内边距", 0, 100),
        num("margin_bottom", "底边距", 0, 400),
        num("line_spacing", "行距", 0, 40),
        num("letter_spacing", "字间距", -20, 20),
        color("text_color", "文字色"),
        color("bg_color", "背景色")
      ]),
      n("label", { class: "field styles__field styles__field--wide" }, [
        n("span", { class: "field__label", text: "背景不透明度" }),
        n("div", { class: "styles__row" }, [alpha, alphaLabel])
      ]),
      n("div", { class: "styles__grid" }, [
        toggle("primary_box", "主字幕带背景"),
        toggle("secondary_box", "副字幕带背景")
      ])
    ]);
  }

  // ------------------------------------------------------------ 画布预览

  function token(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || "").trim() || fallback || "";
  }

  function roundRect(ctx, x, y, w, h, r) {
    var radius = Math.max(0, Math.min(r, Math.min(w, h) / 2));
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.lineTo(x + w - radius, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
    ctx.lineTo(x + w, y + h - radius);
    ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    ctx.lineTo(x + radius, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
    ctx.lineTo(x, y + radius);
    ctx.quadraticCurveTo(x, y, x + radius, y);
    ctx.closePath();
  }

  /** 字幕色：画面上的字不跟皮肤走，所以读 tokens.css 里的 --cap-*。 */
  function capFg() {
    return token("--cap-fg");
  }

  function capBg() {
    return token("--cap-bg");
  }

  function capStroke() {
    return token("--cap-stroke");
  }

  function drawPreview() {
    var host = document.getElementById("styles-canvas-host");
    if (!host || !state.draft) return;
    var canvas = host.querySelector("canvas");
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.className = "styles__canvas";
      host.appendChild(canvas);
    }
    var conf = state.draft;
    var portrait = (conf.preview || {}).orientation === "portrait";
    var width = portrait ? 720 : 1280;
    var height = portrait ? 1280 : 720;
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = width + " / " + height;
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) {
      host.setAttribute("data-degraded", "1");
      return;
    }
    host.removeAttribute("data-degraded");
    // 背景：深色用页面底色、浅色用面板色；"图片" 只是示意——真背景来自视频帧，
    // 这里画一道斜纹同时点明"预览不等于成片"
    var backdrop = (conf.preview || {}).background || "dark";
    var bg = backdrop === "light" ? token("--panel") : token("--void");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);
    if (backdrop === "dark") {
      // 纯页面底色是近黑，圆角背景条（黑底 60% 透明）叠上去就看不见了，
      // 用户会以为"圆角条没生效"。叠一层浅灰把画布抬到"视频画面"的亮度区间。
      ctx.globalAlpha = 0.35;
      ctx.fillStyle = token("--muted");
      ctx.fillRect(0, 0, width, height);
      ctx.globalAlpha = 1;
    }
    if (backdrop === "image") {
      ctx.strokeStyle = token("--line");
      ctx.lineWidth = 2;
      for (var x = -height; x < width; x += 48) {
        ctx.beginPath();
        ctx.moveTo(x, height);
        ctx.lineTo(x + height, 0);
        ctx.stroke();
      }
    }
    var text = SAMPLES[(conf.preview || {}).text_length || "medium"] || SAMPLES.medium;
    var isRounded = conf.mode === "rounded";
    var rnd = conf.rounded || {};
    var size = Number((isRounded ? rnd.size : (conf.primary || {}).size) || 48);
    var bottom = Number((isRounded ? rnd.margin_bottom : 42) || 42);
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.font = (isRounded ? "" : (conf.primary || {}).bold ? "bold " : "") + size + "px " +
      ((isRounded ? rnd.font : (conf.primary || {}).font) || "Microsoft YaHei");
    // 长句要真的折行：文本长度选「长」就是为了看换行和背景条宽度，
    // 不折行时文字直接被画布边缘切掉，看不出任何问题。
    var maxWidth = width * 0.9;
    var lines = wrapText(ctx, text, maxWidth, 3);
    if (conf.layout !== "original_only") lines.push("（副字幕示例：译文这一行）");
    var lineHeight = size * 1.35;
    var baseY = height - bottom;
    lines.forEach(function (line, index) {
      var y = baseY - (lines.length - 1 - index) * lineHeight;
      var fontSize = index === 0 ? size : Number((conf.secondary || {}).size || 36);
      ctx.font = fontSize + "px " + ((conf.secondary || {}).font || "Microsoft YaHei");
      var metrics = ctx.measureText(line);
      var padH = Number(rnd.padding_h || 0);
      var padV = Number(rnd.padding_v || 0);
      if (isRounded) {
        var boxW = metrics.width + padH * 2;
        var boxH = fontSize + padV * 2;
        // 半透明靠 globalAlpha：拼 rgba() 字符串既绕开了 token 校验，
        // 也会在 canvas 里引入一次性颜色字面量
        ctx.globalAlpha = rnd.bg_alpha == null ? 0.6 : Math.max(0, Math.min(1, Number(rnd.bg_alpha)));
        ctx.fillStyle = rnd.bg_color || capBg();
        roundRect(ctx, width / 2 - boxW / 2, y - boxH + padV, boxW, boxH, Number(rnd.corner_radius || 0));
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = rnd.text_color || capFg();
      } else {
        ctx.lineWidth = Number((conf.primary || {}).outline_width || 2);
        ctx.strokeStyle = (conf.primary || {}).outline_color || capStroke();
        ctx.fillStyle = (index === 0 ? (conf.primary || {}).color : (conf.secondary || {}).color) || capFg();
        ctx.strokeText(line, width / 2, y);
      }
      ctx.fillText(line, width / 2, y);
    });
  }

  // ------------------------------------------------------------ 数据

  // 中文按字切、西文按词切；超过 maxLines 用省略号收尾（预览只示意 2–3 行）
  function wrapText(ctx, text, maxWidth, maxLines) {
    var out = [];
    var line = "";
    var tokens = String(text).match(/[A-Za-z0-9'’]+|\s+|[^\s]/g) || [];
    tokens.forEach(function (token) {
      var next = line + token;
      if (line && ctx.measureText(next).width > maxWidth) {
        out.push(line.replace(/\s+$/, ""));
        line = token.replace(/^\s+/, "");
      } else {
        line = next;
      }
    });
    if (line) out.push(line.replace(/\s+$/, ""));
    if (out.length > maxLines) {
      var kept = out.slice(0, maxLines);
      while (kept.length && ctx.measureText(kept[kept.length - 1] + "…").width > maxWidth) {
        kept[kept.length - 1] = kept[kept.length - 1].slice(0, -1);
      }
      kept[kept.length - 1] += "…";
      return kept;
    }
    return out;
  }

  function markDirty() {
    state.dirty = true;
    state.renderPath = "";
    state.renderNote = "";
    // **只重画画布和状态条**。重建整个表单会让正在输入的控件丢焦点——
    // 名字打到第二个字光标就跳走，这种"活着但不跟手"的界面比报错更烦人。
    drawPreview();
    paintBar();
    paintRender();
  }

  function paintBar() {
    var host = document.getElementById("styles-state");
    if (host) host.textContent = state.dirty ? "有未保存的改动" : "改动实时预览，保存前不落盘";
  }

  function loadFonts() {
    WB.rpc("style.fonts").then(function (data) {
      state.fonts = (data && data.fonts) || [];
      if (state.draft) paintForm();
    }).catch(function () {
      state.fonts = [];
    });
  }

  function loadList(selectId) {
    return WB.rpc("style.list").then(function (rows) {
      state.list = rows || [];
      var wanted = selectId || state.id || (state.list[0] && state.list[0].id) || "";
      var exists = state.list.some(function (row) { return row.id === wanted; });
      return pick(exists ? wanted : (state.list[0] && state.list[0].id) || "");
    });
  }

  function pick(id) {
    if (!id) return Promise.resolve();
    state.busy = true;
    return WB.rpc("style.get", { id: id }).then(function (data) {
      state.id = data.style.id;
      state.builtin = !!data.builtin;
      state.draft = copy(data.style);
      state.warnings = data.warnings || [];
      state.dirty = false;
      state.error = "";
      state.renderPath = "";
      paintAll();
    }).catch(function (error) {
      state.error = (error && error.message) || "读不到这个样式";
      paintAll();
    }).then(function () {
      state.busy = false;
    });
  }

  function save() {
    if (!state.draft) return;
    state.busy = true;
    var payload = {
      id: state.builtin ? "" : state.id,
      name: state.draft.name,
      config: state.draft
    };
    WB.rpc("style.save", payload).then(function (data) {
      state.id = data.style.id;
      state.builtin = false;
      state.draft = copy(data.style);
      state.warnings = data.warnings || [];
      state.dirty = false;
      state.note = "已保存：" + data.style.name;
      return loadList(state.id);
    }).catch(function (error) {
      state.error = (error && error.message) || "保存失败";
      paintAll();
    }).then(function () {
      state.busy = false;
    });
  }

  function duplicate() {
    if (!state.draft) return;
    state.busy = true;
    var draft = copy(state.draft);
    draft.name = draft.name + " 副本";
    WB.rpc("style.save", { name: draft.name, config: draft }).then(function (data) {
      state.note = "已复制为：" + data.style.name;
      return loadList(data.style.id);
    }).catch(function (error) {
      state.error = (error && error.message) || "复制失败";
      paintAll();
    }).then(function () {
      state.busy = false;
    });
  }

  function remove() {
    if (!state.draft || state.builtin) return;
    var name = state.draft.name;
    WB.rpc("style.delete", { id: state.id }).then(function () {
      state.note = "已删除：" + name;
      state.id = "";
      return loadList();
    }).catch(function (error) {
      state.error = (error && error.message) || "删除失败";
      paintAll();
    });
  }

  function importJson() {
    if (!WB.fs || !WB.fs.pickFile) {
      state.error = "这个环境没有文件选择器，请用「导出」确认格式后手工放到 styles 目录";
      paintAll();
      return;
    }
    WB.fs.pickFile({ title: "选择样式 JSON" }).then(function (path) {
      if (!path) return null;
      return WB.rpc("style.import", { path: path });
    }).then(function (data) {
      if (!data) return null;
      state.note = "已导入：" + data.style.name;
      return loadList(data.style.id);
    }).catch(function (error) {
      state.error = (error && error.message) || "导入失败";
      paintAll();
    });
  }

  function exportJson() {
    if (!state.draft) return;
    WB.rpc("style.export", { id: state.id }).then(function (data) {
      state.note = "已导出：" + data.path;
      paintAll();
    }).catch(function (error) {
      state.error = (error && error.message) || "导出失败";
      paintAll();
    });
  }

  function realRender() {
    if (!state.draft) return;
    state.busy = true;
    state.renderNote = "渲染中…";
    paintRender();
    WB.rpc("style.render", { id: state.builtin ? state.id : state.id, config: state.draft })
      .then(function (data) {
        state.renderPath = data.path;
        state.renderNote = "真实渲染：" + data.width + "×" + data.height;
        if ((data.warnings || []).length) state.warnings = data.warnings;
      })
      .catch(function (error) {
        state.renderPath = "";
        state.renderNote = "真实渲染失败（" + ((error && error.message) || "未知原因") + "），上面是画布近似预览";
      })
      .then(function () {
        state.busy = false;
        paintRender();
      });
  }

  // ------------------------------------------------------------ 渲染

  function listPane() {
    var rows = state.list.map(function (row) {
      var active = row.id === state.id;
      var node = n("button", {
        class: "styles__item" + (active ? " is-active" : ""),
        attrs: { type: "button" }
      }, [
        n("span", { class: "styles__item-name", text: row.name }),
        n("span", { class: "styles__item-meta" }, [
          n("span", { class: "pill" + (row.mode === "rounded" ? " pill--warn" : ""),
                         text: row.mode === "rounded" ? "圆角条" : "描边" }),
          n("span", { class: "styles__hint", text: row.size + " 号" }),
          row.builtin ? n("span", { class: "styles__hint", text: "内置" }) : null
        ])
      ]);
      node.addEventListener("click", function () {
        if (state.dirty && !window.confirm("当前改动还没保存，切走就丢了。继续？")) return;
        pick(row.id);
      });
      return node;
    });
    return n("div", { class: "styles__pane styles__pane--list" }, [
      n("div", { class: "styles__pane-head" }, [
        n("strong", { text: "预设" }),
        n("span", { class: "styles__hint", text: state.list.length + " 个" })
      ]),
      n("div", { class: "styles__items" }, rows.length ? rows : [n("p", { class: "styles__hint", text: "还没有样式" })]),
      n("div", { class: "styles__actions" }, [
        btn("新建", "新建一个空样式（从当前预设抄底）", duplicate),
        btn("导入", "从 JSON 文件导入", importJson),
        btn("导出", "导出当前样式为 JSON", exportJson),
        btn("删除", state.builtin ? "内置样式不可删除" : "删除当前样式", remove,
            state.builtin ? { disabled: true } : {})
      ])
    ]);
  }

  function btn(text, title, handler, opts) {
    var options = opts || {};
    var node = n("button", {
      class: "btn btn--ghost" + (options.primary ? " btn--primary" : "") +
        (options.danger ? " btn--danger" : ""),
      text: text,
      attrs: { type: "button", title: title || text }
    });
    if (options.disabled) node.disabled = true;
    else node.addEventListener("click", handler);
    return node;
  }

  function formPane() {
    var conf = state.draft;
    if (!conf) {
      return n("div", { class: "styles__pane styles__pane--edit" }, [
        n("p", { class: "styles__hint", text: "左边选一个预设开始改；没有就点「新建」。" })
      ]);
    }
    var name = n("input", { class: "input", value: conf.name, attrs: { "aria-label": "样式名" } });
    name.addEventListener("input", function () {
      state.draft.name = name.value;
      markDirty();
    });
    var mode = n("select", { class: "select", attrs: { "aria-label": "模式" } });
    MODES.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === conf.mode) opt.selected = true;
      mode.appendChild(opt);
    });
    mode.addEventListener("change", function () {
      state.draft.mode = mode.value;
      markDirty();
    });
    var layout = n("select", { class: "select", attrs: { "aria-label": "版式" } });
    LAYOUTS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === conf.layout) opt.selected = true;
      layout.appendChild(opt);
    });
    layout.addEventListener("change", function () {
      state.draft.layout = layout.value;
      markDirty();
    });
    var warnings = (state.warnings || []).map(function (text) {
      return n("p", { class: "callout callout--warn", text: text });
    });

    return n("div", { class: "styles__pane styles__pane--edit" }, [
      n("div", { class: "styles__block" }, [
        n("div", { class: "styles__block-head" }, [
          n("strong", { text: "基本" }),
          state.builtin ? n("span", { class: "styles__hint", text: "内置样式：改了要先另存（保存会生成副本）" }) : null
        ]),
        n("div", { class: "styles__grid" }, [
          n("label", { class: "field styles__field" }, [
            n("span", { class: "field__label", text: "名称" }), name
          ]),
          n("label", { class: "field styles__field" }, [
            n("span", { class: "field__label", text: "模式" }), mode
          ]),
          n("label", { class: "field styles__field" }, [
            n("span", { class: "field__label", text: "版式" }), layout
          ])
        ])
      ]),
      previewBlock(),
      faceBlock(FACES[0]),
      faceBlock(FACES[1]),
      roundedBlock(),
      warnings.length ? n("div", { class: "styles__warnings" }, warnings) : null,
      n("div", { class: "styles__savebar" }, [
        btn(state.builtin ? "另存为新样式" : "保存", "写入 wb_style 与 styles/*.json", save, { primary: true }),
        btn("真实渲染", "用 FFmpeg 渲一张 PNG 检查实际效果", realRender),
        n("span", { class: "styles__hint", text: state.busy ? "处理中…" : (state.note || "") })
      ])
    ]);
  }

  function previewBlock() {
    // 放在右栏最上面并 position:sticky：改字号/描边时画布一直在眼前。
    // 之前它在表最底下（y≈1134），改完参数要滚到底才看得见效果。
    return n("div", { class: "styles__block styles__preview" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: "预览" }),
        n("span", { class: "styles__hint", text: "画布是近似排版；字体度量与半透明叠加以真实渲染为准" })
      ]),
      n("div", { class: "styles__grid" }, [
        n("label", { class: "field styles__field" }, [
          n("span", { class: "field__label", text: "文本长度" }), previewLength()
        ]),
        n("label", { class: "field styles__field" }, [
          n("span", { class: "field__label", text: "画幅" }), previewOrient()
        ]),
        n("label", { class: "field styles__field" }, [
          n("span", { class: "field__label", text: "背景" }), previewBackdrop()
        ])
      ]),
      n("div", { class: "styles__canvas-host", id: "styles-canvas-host" }),
      n("div", { class: "styles__render", id: "styles-render" })
    ]);
  }

  function previewLength() {
    var select = n("select", { class: "select", attrs: { "aria-label": "文本长度" } });
    LENGTHS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === state.draft.preview.text_length) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      state.draft.preview.text_length = select.value;
      markDirty();
    });
    return select;
  }

  function previewOrient() {
    var select = n("select", { class: "select", attrs: { "aria-label": "预览画幅" } });
    ORIENTS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === state.draft.preview.orientation) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      state.draft.preview.orientation = select.value;
      markDirty();
    });
    return select;
  }

  function previewBackdrop() {
    var select = n("select", { class: "select", attrs: { "aria-label": "预览背景" } });
    BACKDROPS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === state.draft.preview.background) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      state.draft.preview.background = select.value;
      markDirty();
    });
    return select;
  }

  function paintRender() {
    var host = document.getElementById("styles-render");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    if (state.renderPath && WB.mediaUrl) {
      var img = n("img", { class: "styles__render-img", attrs: { alt: "真实渲染结果" } });
      // 文件被挪走/删除时浏览器只剩一个破图标：直接换成一句说明
      img.addEventListener("error", function () {
        if (img.parentNode) img.parentNode.removeChild(img);
        host.appendChild(n("p", { class: "styles__hint", text: "渲染图读不到（文件可能已被移动或删除）" }));
      });
      img.src = WB.mediaUrl(state.renderPath);
      host.appendChild(img);
    }
    if (state.renderNote) {
      host.appendChild(n("p", { class: "styles__hint", text: state.renderNote }));
    }
  }

  function paintForm() {
    var host = document.getElementById("styles-edit");
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    host.appendChild(formPane());
    drawPreview();
    paintRender();
  }

  function paintAll() {
    paintBar();
    paintForm();
    var listHost = document.getElementById("styles-list");
    if (listHost) {
      while (listHost.firstChild) listHost.removeChild(listHost.firstChild);
      listHost.appendChild(listPane());
    }
    var errorHost = document.getElementById("styles-error");
    if (errorHost) {
      errorHost.textContent = state.error || "";
      errorHost.hidden = !state.error;
    }
  }

  function render(ctx) {
    var root = n("div", { class: "styles" }, [
      n("div", { class: "styles__bar" }, [
        n("span", { class: "styles__hint", id: "styles-state",
                   text: state.dirty ? "有未保存的改动" : "改动实时预览，保存前不落盘" }),
        n("span", { class: "styles__error", id: "styles-error", hidden: true })
      ]),
      n("div", { class: "styles__grid2" }, [
        n("div", { id: "styles-list" }),
        n("div", { id: "styles-edit" })
      ])
    ]);
    // 先挂进 DOM 再画：画布/表单都要 getElementById
    window.setTimeout(function () {
      if (!state.list.length && !state.busy) {
        loadFonts();
        loadList();
        return;
      }
      paintAll();
    }, 0);
    return root;
  }

  WB.pages = WB.pages || {};
  WB.pages.styles = {
    render: render,
    _internals: {
      state: state,
      pick: pick,
      save: save,
      duplicate: duplicate,
      remove: remove,
      realRender: realRender,
      drawPreview: drawPreview,
      loadList: loadList
    }
  };

  WB.router.define("styles", {
    title: "字幕样式",
    crumb: "预设与预览",
    icon: "subtitles",
    nav: false,
    render: render
  });
})();
