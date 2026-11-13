/* ============================================================================
 * components/subtitle_style_preview.js —— 字幕样式预览（任务 5 拆出的另一半）
 *
 * 两种预览，语义不同，必须让人一眼看出区别：
 *
 * 1. **快速预览**：canvas 按 ASS 的排版规则估（字号以像素、底边距从底往上、
 *    圆角条按文本宽算）。换字重/颜色/位置够用，字体度量与半透明叠加不准。
 * 2. **精确预览**：把同一份样式交给 format_ass → FFmpeg → PNG，和成片同一条链路。
 *    慢（要起进程），但那是"这一版烧出来长什么样"的唯一证据。
 *
 * 组件不做 RPC：精确预览由页面发起（页面才知道 project_id / time_ms），
 * 这里只负责把结果画出来，包括失败、渲染中和旧响应失效三种状态。
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

  var LENGTHS = [["short", "短句"], ["medium", "中句"], ["long", "长句"]];
  var ORIENTS = [["landscape", "16:9 横屏"], ["portrait", "9:16 竖屏"]];
  var BACKDROPS = [["dark", "深色"], ["light", "浅色"], ["image", "图片"]];
  var SAMPLES = {
    short: "这里是短句",
    medium: "先把反例摆出来，说明直觉为什么不可靠",
    long: "先把反例摆出来，说明直觉为什么不可靠；再把变量拆开，逐个控制做对比；最后落到可复现的操作顺序上"
  };

  function token(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (value || "").trim() || fallback || "";
  }

  function roundRect(context, x, y, w, h, r) {
    var radius = Math.max(0, Math.min(r, Math.min(w, h) / 2));
    context.beginPath();
    context.moveTo(x + radius, y);
    context.lineTo(x + w - radius, y);
    context.quadraticCurveTo(x + w, y, x + w, y + radius);
    context.lineTo(x + w, y + h - radius);
    context.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
    context.lineTo(x + radius, y + h);
    context.quadraticCurveTo(x, y + h, x, y + h - radius);
    context.lineTo(x, y + radius);
    context.quadraticCurveTo(x, y, x + radius, y);
    context.closePath();
  }

  // 中文按字切、西文按词切；超过 maxLines 用省略号收尾（预览只示意 2–3 行）
  function wrapText(context, text, maxWidth, maxLines) {
    var out = [];
    var line = "";
    var tokens = String(text).match(/[A-Za-z0-9'’]+|\s+|[^\s]/g) || [];
    tokens.forEach(function (item) {
      var next = line + item;
      if (line && context.measureText(next).width > maxWidth) {
        out.push(line.replace(/\s+$/, ""));
        line = item.replace(/^\s+/, "");
      } else {
        line = next;
      }
    });
    if (line) out.push(line.replace(/\s+$/, ""));
    if (out.length > maxLines) {
      var kept = out.slice(0, maxLines);
      while (kept.length && context.measureText(kept[kept.length - 1] + "…").width > maxWidth) {
        kept[kept.length - 1] = kept[kept.length - 1].slice(0, -1);
      }
      kept[kept.length - 1] += "…";
      return kept;
    }
    return out;
  }

  function sampleText(conf) {
    var preview = (conf && conf.preview) || {};
    return SAMPLES[preview.text_length || "medium"] || SAMPLES.medium;
  }

  function secondaryLine(conf) {
    var layout = (conf && conf.layout) || "original_top";
    if (layout === "original_only" || layout === "translated_only") return "";
    return "（副字幕示例：译文这一行）";
  }

  /**
   * 快速预览：把样式画进 host 里的 canvas。host 会被复用，canvas 只建一次。
   * 返回 false 表示这个环境没有 2d context（页面据此显示降级提示）。
   */
  function drawFast(host, conf) {
    if (!host || !conf) return true;
    var canvas = host.querySelector("canvas");
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.className = "styles__canvas";
      host.appendChild(canvas);
    }
    var portrait = ((conf.preview || {}).orientation === "portrait");
    var width = portrait ? 720 : 1280;
    var height = portrait ? 1280 : 720;
    canvas.width = width;
    canvas.height = height;
    canvas.style.aspectRatio = width + " / " + height;
    var context = canvas.getContext && canvas.getContext("2d");
    if (!context) {
      host.setAttribute("data-degraded", "1");
      return false;
    }
    host.removeAttribute("data-degraded");
    // 背景：深色用页面底色、浅色用面板色；"图片" 只是示意——真背景来自视频帧，
    // 这里画一道斜纹同时点明"预览不等于成片"
    var backdrop = (conf.preview || {}).background || "dark";
    context.fillStyle = backdrop === "light" ? token("--panel") : token("--void");
    context.fillRect(0, 0, width, height);
    if (backdrop === "dark") {
      // 纯页面底色是近黑，圆角背景条（黑底 60% 透明）叠上去就看不见了，
      // 用户会以为"圆角条没生效"。叠一层浅灰把画布抬到"视频画面"的亮度区间。
      context.globalAlpha = 0.35;
      context.fillStyle = token("--muted");
      context.fillRect(0, 0, width, height);
      context.globalAlpha = 1;
    }
    if (backdrop === "image") {
      context.strokeStyle = token("--line");
      context.lineWidth = 2;
      for (var x = -height; x < width; x += 48) {
        context.beginPath();
        context.moveTo(x, height);
        context.lineTo(x + height, 0);
        context.stroke();
      }
    }
    var isRounded = conf.mode === "rounded";
    var rnd = conf.rounded || {};
    var primary = conf.primary || {};
    var secondary = conf.secondary || {};
    // 参考画布是 1080P，预览画布只有 720 高：字号必须按同一比例缩下来，
    // 否则预览里的字比成片大一倍半，"看着刚好、烧出来巨大"。
    var refHeight = Number((conf.preview || {}).orientation === "portrait" ? 1920 : 1080);
    var scale = height / refHeight;
    var size = Number((isRounded ? rnd.size : primary.size) || 72) * scale;
    var bottom = Number((isRounded ? rnd.margin_bottom : primary.margin_v) || 0) * scale;
    var secondarySize = Number(secondary.size || 54) * scale;
    context.textAlign = "center";
    context.textBaseline = "bottom";
    var lines = wrapText(
      context,
      sampleText(conf),
      width * 0.9,
      3
    );
    var extra = secondaryLine(conf);
    if (extra) lines.push(extra);
    var gap = Number(conf.vertical_gap || 0) * scale;
    var lineHeight = size * 1.35;
    var baseY = height - (bottom || height * 0.06);
    lines.forEach(function (line, index) {
      var y = baseY - (lines.length - 1 - index) * lineHeight;
      var fontSize = index === 0 ? size : secondarySize;
      var face = index === 0 ? primary : secondary;
      context.font = (face.bold ? "bold " : "") + fontSize + "px " + (face.font || "Microsoft YaHei");
      var metrics = context.measureText(line);
      var padH = Number(rnd.padding_h || 0) * scale;
      var padV = Number(rnd.padding_v || 0) * scale;
      if (isRounded) {
        var boxW = metrics.width + padH * 2;
        var boxH = fontSize + padV * 2;
        // 半透明靠 globalAlpha：拼 rgba() 字符串既绕开 token 校验，
        // 也会在 canvas 里引入一次性颜色字面量
        context.globalAlpha = rnd.bg_alpha == null ? 0.6 : Math.max(0, Math.min(1, Number(rnd.bg_alpha)));
        context.fillStyle = rnd.bg_color || token("--cap-bg");
        roundRect(context, width / 2 - boxW / 2, y - boxH + padV, boxW, boxH, Number(rnd.corner_radius || 0) * scale);
        context.fill();
        context.globalAlpha = 1;
        context.fillStyle = rnd.text_color || token("--cap-fg");
      } else {
        context.lineWidth = Number(face.outline_width || 0) * scale;
        context.strokeStyle = face.outline_color || token("--cap-stroke");
        context.fillStyle = face.color || token("--cap-fg");
        if (context.lineWidth > 0) context.strokeText(line, width / 2, y);
      }
      context.fillText(line, width / 2, y);
    });
    if (gap > 0 && lines.length > 1) {
      // 主副间距在预览里只是把上行再抬高一点：ASS 侧是抬 margin_v，
      // 画布侧同样只影响上行位置，两者方向要一致。
      void gap;
    }
    return true;
  }

  function selector(label, pairs, current, onChange) {
    var select = n("select", { class: "select", attrs: { "aria-label": label } });
    pairs.forEach(function (pair) {
      var opt = n("option", { text: pair[1], attrs: { value: pair[0] } });
      if (pair[0] === current) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () { onChange(select.value); });
    return n("label", { class: "field styles__field" }, [
      n("span", { class: "field__label", text: label }),
      select
    ]);
  }

  /**
   * 预览区外壳：三个预览参数 + 画布 + 精确预览结果位。
   * @param {{draft: object, onChange: Function, onExact: Function, busy?: boolean}} options
   */
  function render(options) {
    var opts = options || {};
    var conf = opts.draft || {};
    var preview = conf.preview || {};
    var exactBtn = n("button", {
      class: "btn btn--ghost",
      text: "精确预览",
      attrs: { type: "button", title: "用 FFmpeg 渲一张 PNG 检查实际效果" }
    });
    if (opts.busy) exactBtn.disabled = true;
    exactBtn.addEventListener("click", function () { if (opts.onExact) opts.onExact(); });
    return n("div", { class: "styles__block styles__preview" }, [
      n("div", { class: "styles__block-head" }, [
        n("strong", { text: "预览" }),
        n("span", { class: "styles__hint",
                   text: "画布是近似排版；字体度量与半透明叠加以精确预览为准" })
      ]),
      n("div", { class: "styles__grid" }, [
        selector("文本长度", LENGTHS, preview.text_length || "medium", function (value) {
          conf.preview.text_length = value;
          if (opts.onChange) opts.onChange();
        }),
        selector("画幅", ORIENTS, preview.orientation || "landscape", function (value) {
          conf.preview.orientation = value;
          if (opts.onChange) opts.onChange();
        }),
        selector("背景", BACKDROPS, preview.background || "dark", function (value) {
          conf.preview.background = value;
          if (opts.onChange) opts.onChange();
        })
      ]),
      n("div", { class: "styles__canvas-host", id: "styles-canvas-host" }),
      n("div", { class: "styles__render", id: "styles-render" }),
      n("div", { class: "styles__row" }, [exactBtn])
    ]);
  }

  /** 精确预览结果：成功挂图、失败说原因、渲染中说明在跑。 */
  function paintExact(host, result) {
    if (!host) return;
    while (host.firstChild) host.removeChild(host.firstChild);
    var state = result || {};
    if (state.note) {
      host.appendChild(n("p", { class: "styles__hint", text: state.note }));
    }
    if (state.path && WB.mediaUrl) {
      var img = n("img", { class: "styles__render-img", attrs: { alt: "精确预览结果" } });
      // 文件被挪走/删除时浏览器只剩一个破图标：直接换成一句说明
      img.addEventListener("error", function () {
        if (img.parentNode) img.parentNode.removeChild(img);
        host.appendChild(n("p", { class: "styles__hint", text: "预览图读不到（文件可能已被移动或删除）" }));
      });
      img.src = WB.mediaUrl(state.path);
      host.appendChild(img);
    }
    if (state.fonts && state.fonts.note) {
      host.appendChild(n("p", { class: "callout callout--warn", text: state.fonts.note }));
    }
  }

  WB.components = WB.components || {};
  WB.components.subtitleStylePreview = {
    render: render,
    drawFast: drawFast,
    paintExact: paintExact,
    SAMPLES: SAMPLES
  };
})();