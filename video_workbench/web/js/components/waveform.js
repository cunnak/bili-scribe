/* ============================================================================
 * components/waveform.js —— 波形画布 + 时间轴（P2-03）
 *
 * **为什么自己画，不用波形库。** 需要的只有三件事：峰值条、段落色块、播放头。
 * 引一个库（wavesurfer 之类）会顺手把解码、播放、插件体系都搬进来，而这些我们
 * 已经有了（音频由 <video> 播，峰值由后端算）。
 *
 * 画布用 devicePixelRatio 放大，否则 2x 屏上线条是毛的。
 *
 * 交互：
 *   - 单击 / 拖动 → 跳转（`onSeek(ms)`）
 *   - 段落色块按选中状态变色
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;
  var h = ui.h;

  // 颜色一律从 css/tokens.css 的变量读：脚本里出现颜色字面量会被
  // scripts/check_colors.py 判红，而且切皮肤时画布会留一块"永远深色"的补丁。
  // 皮肤切换只改 <html data-skin>，所以缓存里带上 skin，变了才重读。
  var COLOR_FIELDS = {
    wave: "--wave",
    waveDim: "--wave-dim",
    played: "--wave-played",
    segment: "--wave-segment",
    segmentActive: "--wave-segment-active",
    playhead: "--wave-playhead",
    tick: "--wave-tick"
  };
  var colorCache = { skin: null, value: null };

  function colors_() {
    var root = window.document.documentElement;
    var skin = root.getAttribute("data-skin") || "";
    if (colorCache.skin === skin && colorCache.value) return colorCache.value;
    var style = window.getComputedStyle(root);
    var out = {};
    Object.keys(COLOR_FIELDS).forEach(function (key) {
      out[key] = String(style.getPropertyValue(COLOR_FIELDS[key]) || "").trim();
    });
    colorCache.skin = skin;
    colorCache.value = out;
    return out;
  }

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{onSeek?:function(number), height?:number}} options
   */
  function mount(canvas, options) {
    var opts = options || {};
    var ctx = canvas.getContext("2d");
    // jsdom / 老显卡下可能拿不到 2d 上下文。波形是"锦上添花"，拿不到就退化：
    // 画布留空，但字幕列表、跳转、质检全部照常工作。
    var degraded = !ctx;
    var state = {
      peaks: [],
      segments: [],
      activeId: "",
      durationMs: 0,
      timeMs: 0,
      height: opts.height || 96,
      dragging: false
    };

    function ratio() {
      return window.devicePixelRatio || 1;
    }

    /** 画布尺寸跟着 CSS 走，像素比跟着屏幕走。容器变化时由外部调 resize()。 */
    function resize() {
      var rect = canvas.getBoundingClientRect();
      var width = Math.max(320, Math.round(rect.width || canvas.clientWidth || 640));
      var height = state.height;
      var scale = ratio();
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      canvas.style.height = height + "px";
      if (!degraded) ctx.setTransform(scale, 0, 0, scale, 0, 0);
      state.width = width;
      draw();
    }

    function xOf(ms) {
      if (!state.durationMs) return 0;
      return Math.max(0, Math.min(state.width, (ms / state.durationMs) * state.width));
    }

    function msOf(x) {
      if (!state.width) return 0;
      return Math.max(0, Math.min(1, x / state.width)) * state.durationMs;
    }

    function drawSegments() {
      state.segments.forEach(function (seg) {
        var x0 = xOf(seg.start_ms);
        var x1 = xOf(seg.end_ms);
        var w = Math.max(1, x1 - x0);
        ctx.fillStyle = seg.id === state.activeId ? colors_().segmentActive : colors_().segment;
        ctx.fillRect(x0, 0, w, state.height);
        // 段边界：一条 1px 竖线，方便看出"这里切过"
        ctx.fillStyle = colors_().tick;
        ctx.fillRect(x0, 0, 1, state.height);
      });
    }

    function drawWave() {
      var peaks = state.peaks;
      var width = state.width || 1;
      var mid = state.height / 2;
      if (!peaks.length) {
        ctx.fillStyle = colors_().waveDim;
        ctx.fillRect(0, mid, width, 1);
        return;
      }
      var perBucket = width / peaks.length;
      var playedX = xOf(state.timeMs);
      for (var i = 0; i < peaks.length; i++) {
        var value = peaks[i] / 255;
        var bar = Math.max(1, value * (state.height - 8));
        var x = i * perBucket;
        ctx.fillStyle = x + perBucket <= playedX ? colors_().played : colors_().wave;
        ctx.fillRect(x, mid - bar / 2, Math.max(1, perBucket - 0.6), bar);
      }
    }

    function drawPlayhead() {
      var x = xOf(state.timeMs);
      ctx.fillStyle = colors_().playhead;
      ctx.fillRect(Math.round(x), 0, 2, state.height);
    }

    function draw() {
      if (degraded || !state.width) return;
      ctx.clearRect(0, 0, state.width, state.height);
      drawSegments();
      drawWave();
      drawPlayhead();
    }

    function seekFromEvent(event) {
      var rect = canvas.getBoundingClientRect();
      var x = event.clientX - rect.left;
      var ms = msOf(x);
      setTime(ms);
      if (opts.onSeek) opts.onSeek(Math.round(ms));
    }

    function setTime(ms) {
      state.timeMs = Math.max(0, Math.min(state.durationMs || ms, ms));
      draw();
    }

    canvas.addEventListener("pointerdown", function (event) {
      state.dragging = true;
      canvas.setPointerCapture(event.pointerId);
      seekFromEvent(event);
    });
    canvas.addEventListener("pointermove", function (event) {
      if (state.dragging) seekFromEvent(event);
    });
    canvas.addEventListener("pointerup", function (event) {
      state.dragging = false;
      try {
        canvas.releasePointerCapture(event.pointerId);
      } catch (err) {
        /* 指针已经释放，忽略 */
      }
    });

    resize();

    return {
      resize: resize,
      draw: draw,
      setPeaks: function (peaks, durationMs) {
        state.peaks = Array.isArray(peaks) ? peaks : [];
        if (durationMs) state.durationMs = durationMs;
        draw();
      },
      setSegments: function (segments, durationMs) {
        state.segments = Array.isArray(segments) ? segments : [];
        if (durationMs) state.durationMs = durationMs;
        if (!state.durationMs) {
          // 波形还没到也能画：用最后一段的结束时间当总长
          state.durationMs = state.segments.length
            ? state.segments[state.segments.length - 1].end_ms
            : 0;
        }
        draw();
      },
      setActive: function (id) {
        state.activeId = id || "";
        draw();
      },
      setTime: setTime,
      state: state
    };
  }

  /** 时间轴刻度条（单独的 DOM，不画进 canvas，方便选中/复制） */
  function timeline(durationMs, width) {
    var ticks = h("div", { class: "wave__ticks" });
    var total = Math.max(1, Math.round(durationMs / 1000));
    var step = total > 1800 ? 600 : total > 600 ? 300 : total > 180 ? 120 : 60;
    for (var sec = 0; sec <= total; sec += step) {
      var pct = durationMs ? (sec * 1000) / durationMs : 0;
      if (pct > 1) break;
      ticks.appendChild(
        h("span", {
          class: "wave__tick",
          style: "left:" + (pct * 100).toFixed(3) + "%",
          text: fmt(sec)
        })
      );
    }
    return ticks;
  }

  function fmt(sec) {
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  WB.waveform = { mount: mount, timeline: timeline };
})();
