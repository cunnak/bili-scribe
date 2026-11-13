/* ============================================================================
 * components/subtitle_style_form.js —— 字幕样式表单（任务 5 拆出的那一半）
 *
 * 只负责「样式对象 → 控件 → 改样式对象」，不做 RPC、不持页面状态。
 * 页面级 owner（pages/styles.js）把 draft 与 onchange 传进来，所以：
 *
 * 1. 这里没有任何 WB.rpc 调用——表单不该知道"存哪"；
 * 2. 这里不判「能不能保存」——内置预设只读是页面的规则；
 * 3. 每个字段都必须真的写进 ASS，否则就是假支持（字段清单与
 *    core/styles.py 的 FACE_* 常量一一对应，两边加字段要一起加）。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  // ui.h 的属性是平铺写法，没有 attrs 这一层。本组件要写的 aria-label 太多，
  // 平铺开每个控件都变成一行长调用，所以在这里包一层。
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

  // 字段 → 控件。数值字段统一走 number，颜色走 color，开关走 checkbox；
  // 每一项都在 core/styles.py 的校验范围里，越界会被 validate 一次列全。
  var FACE_NUMBERS = [
    ["size", "字号", 12, 120, 1],
    ["outline_width", "描边", 0, 100, 0.5],
    ["spacing", "字间距", -20, 20, 0.1],
    ["shadow_distance", "阴影距离", 0, 100, 0.5],
    ["blur", "模糊", 0, 100, 0.1],
    ["scale_x", "横向缩放 %", 10, 1000, 1],
    ["scale_y", "纵向缩放 %", 10, 1000, 1],
    ["angle", "旋转角", -360, 360, 0.5],
    ["margin_l", "左边距", 0, 1000, 1],
    ["margin_r", "右边距", 0, 1000, 1],
    ["margin_v", "底边距", 0, 1000, 1]
  ];
  var FACE_COLORS = [
    ["color", "文字色"],
    ["outline_color", "描边色"],
    ["shadow_color", "阴影色"],
    ["background_color", "背景色"]
  ];
  var FACE_ALPHAS = [
    ["opacity", "不透明度"],
    ["outline_opacity", "描边不透明度"],
    ["shadow_opacity", "阴影不透明度"],
    ["background_opacity", "背景不透明度"]
  ];
  var FACE_TOGGLES = [
    ["bold", "加粗"],
    ["italic", "斜体"],
    ["underline", "下划线"],
    ["strikeout", "删除线"]
  ];
  var ALIGNMENTS = [
    [1, "左下"], [2, "中下"], [3, "右下"],
    [4, "左中"], [5, "正中"], [6, "右中"],
    [7, "左上"], [8, "中上"], [9, "右上"]
  ];

  var ctx = {
    draft: null,
    fonts: [],
    onChange: function () {},
    readOnly: false
  };

  function face(key) {
    return (ctx.draft && ctx.draft[key]) || {};
  }

  function touch() {
    if (ctx.onChange) ctx.onChange();
  }

  function field(label, control) {
    return n("label", { class: "field styles__field" }, [
      n("span", { class: "field__label", text: label }),
      control
    ]);
  }

  function numberInput(faceKey, fieldName, label, min, max, step) {
    var input = n("input", {
      class: "input styles__num",
      type: "number",
      value: face(faceKey)[fieldName],
      min: min, max: max, step: step || 1,
      attrs: { "aria-label": label, "data-face": faceKey, "data-field": fieldName }
    });
    if (ctx.readOnly) input.disabled = true;
    input.addEventListener("input", function () {
      var raw = Number(input.value);
      // 空串/半截输入先不写：用户清空重打时写 NaN 会让预览闪一下空白
      if (input.value === "" || !isFinite(raw)) return;
      ctx.draft[faceKey][fieldName] = raw;
      touch();
    });
    return field(label, input);
  }

  function colorInput(faceKey, fieldName, label) {
    var input = n("input", {
      class: "input styles__num",
      type: "color",
      value: face(faceKey)[fieldName],
      attrs: { "aria-label": label, "data-face": faceKey, "data-field": fieldName }
    });
    if (ctx.readOnly) input.disabled = true;
    input.addEventListener("input", function () {
      ctx.draft[faceKey][fieldName] = input.value;
      touch();
    });
    return field(label, input);
  }

  function alphaInput(faceKey, fieldName, label) {
    var value = Number(face(faceKey)[fieldName]);
    if (!isFinite(value)) value = 1;
    var readout = n("span", { class: "styles__hint", text: Math.round(value * 100) + "%" });
    var range = n("input", {
      class: "styles__range",
      type: "range",
      min: 0, max: 1, step: 0.05,
      value: value,
      attrs: { "aria-label": label, "data-face": faceKey, "data-field": fieldName }
    });
    if (ctx.readOnly) range.disabled = true;
    range.addEventListener("input", function () {
      var next = Number(range.value);
      ctx.draft[faceKey][fieldName] = next;
      readout.textContent = Math.round(next * 100) + "%";
      touch();
    });
    return n("label", { class: "field styles__field" }, [
      n("span", { class: "field__label", text: label }),
      n("div", { class: "styles__row" }, [range, readout])
    ]);
  }

  function toggleInput(faceKey, fieldName, label) {
    var box = n("input", { type: "checkbox", attrs: { "aria-label": label, "data-field": fieldName } });
    box.checked = !!face(faceKey)[fieldName];
    if (ctx.readOnly) box.disabled = true;
    box.addEventListener("change", function () {
      ctx.draft[faceKey][fieldName] = box.checked;
      touch();
    });
    return field(label, box);
  }

  function fontSelect(faceKey, label) {
    var select = n("select", { class: "select", attrs: { "aria-label": label + "字体", "data-field": "font" } });
    var current = face(faceKey).font || "Microsoft YaHei";
    var fonts = (ctx.fonts || []).slice();
    // 当前字体可能已从系统里卸掉：仍要出现在下拉里，否则界面会静默显示成别的字体
    if (fonts.indexOf(current) < 0) fonts.unshift(current);
    fonts.forEach(function (name) {
      var opt = n("option", { text: name, attrs: { value: name } });
      if (name === current) opt.selected = true;
      select.appendChild(opt);
    });
    if (ctx.readOnly) select.disabled = true;
    select.addEventListener("change", function () {
      ctx.draft[faceKey].font = select.value;
      touch();
    });
    return field("字体", select);
  }

  function alignSelect(faceKey) {
    var select = n("select", { class: "select", attrs: { "aria-label": "对齐", "data-field": "alignment" } });
    var current = Number(face(faceKey).alignment || 2);
    ALIGNMENTS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: String(pair[0]) } });
      if (pair[0] === current) opt.selected = true;
      select.appendChild(opt);
    });
    if (ctx.readOnly) select.disabled = true;
    select.addEventListener("change", function () {
      ctx.draft[faceKey].alignment = Number(select.value);
      touch();
    });
    return field("对齐", select);
  }

  function borderSelect(faceKey) {
    var select = n("select", { class: "select", attrs: { "aria-label": "边框样式", "data-field": "border_style" } });
    [[1, "描边 + 阴影"], [3, "实心框"]].forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: String(pair[0]) } });
      if (Number(face(faceKey).border_style || 1) === pair[0]) opt.selected = true;
      select.appendChild(opt);
    });
    if (ctx.readOnly) select.disabled = true;
    select.addEventListener("change", function () {
      ctx.draft[faceKey].border_style = Number(select.value);
      touch();
    });
    return field("边框样式", select);
  }

  function faceBlock(spec) {
    var key = spec.key;
    var rows = [fontSelect(key, spec.label), alignSelect(key), borderSelect(key)];
    var numbers = FACE_NUMBERS.map(function (row) {
      return numberInput(key, row[0], row[1], row[2], row[3], row[4]);
    });
    var colors = FACE_COLORS.map(function (row) { return colorInput(key, row[0], row[1]); });
    var alphas = FACE_ALPHAS.map(function (row) { return alphaInput(key, row[0], row[1]); });
    var toggles = FACE_TOGGLES.map(function (row) { return toggleInput(key, row[0], row[1]); });
    return n("div", { class: "styles__block", attrs: { "data-face-block": key } }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: spec.label }),
        n("span", { class: "styles__hint", text: spec.hint })
      ]),
      n("div", { class: "styles__grid" }, rows),
      n("div", { class: "styles__grid" }, numbers),
      n("div", { class: "styles__grid" }, colors),
      n("div", { class: "styles__grid" }, alphas),
      n("div", { class: "styles__grid" }, toggles)
    ]);
  }

  var ROUNDED_NUMBERS = [
    ["size", "字号", 12, 120, 1],
    ["corner_radius", "圆角", 0, 40, 1],
    ["padding_h", "左右内边距", 0, 200, 1],
    ["padding_v", "上下内边距", 0, 100, 1],
    ["margin_bottom", "底边距", 0, 400, 1],
    ["line_spacing", "行距", 0, 40, 1],
    ["letter_spacing", "字间距", -20, 20, 0.1]
  ];

  function roundedBlock() {
    var rnd = (ctx.draft && ctx.draft.rounded) || {};
    function num(fieldName, label, min, max, step) {
      var input = n("input", {
        class: "input styles__num", type: "number",
        value: rnd[fieldName], min: min, max: max, step: step || 1,
        attrs: { "aria-label": label, "data-field": fieldName }
      });
      if (ctx.readOnly) input.disabled = true;
      input.addEventListener("input", function () {
        var raw = Number(input.value);
        if (input.value === "" || !isFinite(raw)) return;
        ctx.draft.rounded[fieldName] = raw;
        touch();
      });
      return field(label, input);
    }
    function color(fieldName, label) {
      var input = n("input", {
        class: "input styles__num", type: "color", value: rnd[fieldName],
        attrs: { "aria-label": label, "data-field": fieldName }
      });
      if (ctx.readOnly) input.disabled = true;
      input.addEventListener("input", function () {
        ctx.draft.rounded[fieldName] = input.value;
        touch();
      });
      return field(label, input);
    }
    function toggle(fieldName, label) {
      var box = n("input", { type: "checkbox", attrs: { "aria-label": label, "data-field": fieldName } });
      box.checked = rnd[fieldName] !== false;
      if (ctx.readOnly) box.disabled = true;
      box.addEventListener("change", function () {
        ctx.draft.rounded[fieldName] = box.checked;
        touch();
      });
      return field(label, box);
    }
    var alpha = Number(rnd.bg_alpha);
    if (!isFinite(alpha)) alpha = 0.6;
    var alphaLabel = n("span", { class: "styles__hint", text: Math.round(alpha * 100) + "%" });
    var alphaRange = n("input", {
      class: "styles__range", type: "range", min: 0, max: 1, step: 0.05, value: alpha,
      attrs: { "aria-label": "背景不透明度", "data-field": "bg_alpha" }
    });
    if (ctx.readOnly) alphaRange.disabled = true;
    alphaRange.addEventListener("input", function () {
      ctx.draft.rounded.bg_alpha = Number(alphaRange.value);
      alphaLabel.textContent = Math.round(Number(alphaRange.value) * 100) + "%";
      touch();
    });
    return n("div", { class: "styles__block" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: "圆角背景条" }),
        n("span", { class: "styles__hint",
                   text: "只在「圆角背景条」模式下生效；libass 没有真圆角，导出时按方框近似" })
      ]),
      n("div", { class: "styles__grid" }, ROUNDED_NUMBERS.map(function (row) {
        return num(row[0], row[1], row[2], row[3], row[4]);
      })),
      n("div", { class: "styles__grid" }, [color("text_color", "文字色"), color("bg_color", "背景色")]),
      n("label", { class: "field styles__field styles__field--wide" }, [
        n("span", { class: "field__label", text: "背景不透明度" }),
        n("div", { class: "styles__row" }, [alphaRange, alphaLabel])
      ]),
      n("div", { class: "styles__grid" }, [
        toggle("primary_box", "主字幕带背景"),
        toggle("secondary_box", "副字幕带背景")
      ])
    ]);
  }

  function basicBlock(options) {
    var conf = ctx.draft;
    var opts = options || {};
    var name = n("input", { class: "input", value: conf.name, attrs: { "aria-label": "样式名", "data-field": "name" } });
    if (opts.nameLocked) name.disabled = true;
    name.addEventListener("input", function () {
      ctx.draft.name = name.value;
      touch();
    });
    var mode = n("select", { class: "select", attrs: { "aria-label": "模式", "data-field": "mode" } });
    MODES.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === conf.mode) opt.selected = true;
      mode.appendChild(opt);
    });
    if (ctx.readOnly) mode.disabled = true;
    mode.addEventListener("change", function () {
      ctx.draft.mode = mode.value;
      touch();
    });
    var layout = n("select", { class: "select", attrs: { "aria-label": "版式", "data-field": "layout" } });
    LAYOUTS.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === conf.layout) opt.selected = true;
      layout.appendChild(opt);
    });
    if (ctx.readOnly) layout.disabled = true;
    layout.addEventListener("change", function () {
      ctx.draft.layout = layout.value;
      touch();
    });
    var gap = n("input", {
      class: "input styles__num", type: "number", value: conf.vertical_gap,
      min: 0, max: 1000, step: 1,
      attrs: { "aria-label": "主副间距", "data-field": "vertical_gap" }
    });
    if (ctx.readOnly) gap.disabled = true;
    gap.addEventListener("input", function () {
      var raw = Number(gap.value);
      if (gap.value === "" || !isFinite(raw)) return;
      ctx.draft.vertical_gap = raw;
      touch();
    });
    return n("div", { class: "styles__block" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: "基本" }),
        opts.headHint ? n("span", { class: "styles__hint", text: opts.headHint }) : null
      ]),
      n("div", { class: "styles__grid" }, [
        field("名称", name),
        field("模式", mode),
        field("版式", layout),
        field("主副间距", gap)
      ])
    ]);
  }

  /**
   * 渲染整份表单。返回的节点由调用方挂进页面；改值只改传入的 draft 对象。
   * @param {{draft: object, fonts?: string[], onChange?: Function, readOnly?: boolean,
   *          nameLocked?: boolean, headHint?: string}} options
   */
  function render(options) {
    var opts = options || {};
    ctx.draft = opts.draft || null;
    ctx.fonts = opts.fonts || [];
    ctx.onChange = opts.onChange || function () {};
    ctx.readOnly = !!opts.readOnly;
    if (!ctx.draft) {
      return n("p", { class: "styles__hint", text: "还没有可编辑的样式。" });
    }
    var isRounded = ctx.draft.mode === "rounded";
    return n("div", { class: "styles__form" }, [
      basicBlock({ nameLocked: opts.nameLocked, headHint: opts.headHint }),
      isRounded ? roundedBlock() : null,
      faceBlock(FACES[0]),
      faceBlock(FACES[1])
    ]);
  }

  WB.components = WB.components || {};
  WB.components.subtitleStyleForm = {
    render: render,
    FACES: FACES,
    LAYOUTS: LAYOUTS,
    MODES: MODES,
    ALIGNMENTS: ALIGNMENTS
  };
})();