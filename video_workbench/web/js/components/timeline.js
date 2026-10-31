/* ============================================================================
 * components/timeline.js —— 切片时间轴
 *
 * 一条轨道：波形 + 候选区间 + 两个把手 + 播放头。
 * 拖动把手改切点，松手才回调 `onChange`（拖动过程中回调会让界面一直打后端）。
 *
 * 三条硬规矩：
 * 1. **磁吸优先于自由拖动**：离句边界 6 像素以内就贴上去——用户想切在"这句说完"
 *    的位置，而不是差 40 毫秒的地方。贴上了要把 `snapped` 报给上层，界面才好写
 *    「已贴到句尾」。
 * 2. **最短 400 毫秒**：两个把手不能穿过彼此，这是服务端也会再拦一次的规则。
 * 3. **没有 canvas 2d 上下文时降级**：jsdom 与老显卡都没有，这时只画 DOM 层
 *    （区间块 + 把手），逻辑照跑——否则整套切片台在测试里没法验。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});

  var SNAP_PX = 6;
  // **毫秒上限必须与后端同口径**（方案书 clip.snap_tolerance_ms = 2000）。
  // 只按像素判会出事：半小时的视频在 600px 画布上，6px ≈ 18 秒，
  // 于是任何一次拖动都被"吸"到十几秒外的句边界，微调等于没有。
  var SNAP_MS = 2000;
  var MIN_SPAN_MS = 400;
  var HANDLE = 9;

  function mount(host, options) {
    var opts = options || {};
    var onChange = typeof opts.onChange === "function" ? opts.onChange : function () {};
    var onSeek = typeof opts.onSeek === "function" ? opts.onSeek : function () {};

    var state = {
      duration: Math.max(1, Number(opts.duration) || 1),
      boundaries: (opts.boundaries || []).slice().sort(function (a, b) {
        return a - b;
      }),
      peaks: [],
      candidate: null,
      time: 0,
      drag: ""
    };

    var canvas = document.createElement("canvas");
    canvas.className = "tl__canvas";
    var overlay = document.createElement("div");
    overlay.className = "tl__overlay";
    var startHandle = document.createElement("button");
    startHandle.type = "button";
    startHandle.className = "tl__handle tl__handle--start";
    startHandle.setAttribute("aria-label", "起点");
    var endHandle = document.createElement("button");
    endHandle.type = "button";
    endHandle.className = "tl__handle tl__handle--end";
    endHandle.setAttribute("aria-label", "终点");
    var range = document.createElement("div");
    range.className = "tl__range";
    var playhead = document.createElement("div");
    playhead.className = "tl__playhead";
    var hint = document.createElement("div");
    hint.className = "tl__hint";

    overlay.appendChild(range);
    overlay.appendChild(playhead);
    overlay.appendChild(startHandle);
    overlay.appendChild(endHandle);
    host.appendChild(canvas);
    host.appendChild(overlay);
    host.appendChild(hint);

    var ctx = null;
    try {
      ctx = canvas.getContext ? canvas.getContext("2d") : null;
    } catch (err) {
      ctx = null;
    }
    var degraded = !ctx;

    function width() {
      return Math.max(1, Math.round(canvas.clientWidth || host.clientWidth || 640));
    }

    function ratioOf(ms) {
      return Math.max(0, Math.min(1, Number(ms) / state.duration));
    }

    function msAt(clientX) {
      var box = canvas.getBoundingClientRect();
      var x = Math.max(0, Math.min(box.width, clientX - box.left));
      return Math.round((x / Math.max(1, box.width)) * state.duration);
    }

    /** 最近的句边界（像素距离内）；没有就返回原值。 */
    function snap(ms) {
      var pxPerMs = width() / state.duration;
      var best = null;
      var bestPx = Infinity;
      state.boundaries.forEach(function (point) {
        var distance = Math.abs(point - ms) * pxPerMs;
        if (distance < bestPx) {
          bestPx = distance;
          best = point;
        }
      });
      if (best !== null && bestPx <= SNAP_PX && Math.abs(best - ms) <= SNAP_MS) {
        return { ms: best, snapped: true };
      }
      return { ms: ms, snapped: false };
    }

    function paintOverlay() {
      var total = width();
      playhead.style.left = (ratioOf(state.time) * total).toFixed(1) + "px";
      var cand = state.candidate;
      var show = Boolean(cand);
      [range, startHandle, endHandle].forEach(function (node) {
        node.style.display = show ? "" : "none";
      });
      if (!show) return;
      var startPx = ratioOf(cand.start_ms) * total;
      var endPx = ratioOf(cand.end_ms) * total;
      range.style.left = startPx.toFixed(1) + "px";
      range.style.width = Math.max(2, endPx - startPx).toFixed(1) + "px";
      startHandle.style.left = (startPx - HANDLE / 2).toFixed(1) + "px";
      endHandle.style.left = (endPx - HANDLE / 2).toFixed(1) + "px";
    }

    function colors() {
      var style = getComputedStyle(document.documentElement);
      // 颜色只能来自 tokens.css。取不到就返回空串——canvas 会忽略非法赋值、
      // 用默认色画；jsdom 下根本走不到这里（degraded 直接 return）。
      // 在 JS 里写死兜底色等于把第二套调色板偷偷塞进代码。
      var pick = function (name) {
        return String(style.getPropertyValue(name) || "").trim();
      };
      return {
        accent: pick("--acid-tx"),
        wave: pick("--wave-line"),
        waveBg: pick("--wave-bg") || "transparent",
        line: pick("--line"),
        ink: pick("--text")
      };
    }

    function draw() {
      if (degraded) return;
      var total = width();
      var height = Math.round(canvas.clientHeight || 64);
      var dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(total * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var tone = colors();
      ctx.clearRect(0, 0, total, height);

      // 波形：peaks 是 0..1 的数组，按画布宽度重采样（不插值，取区间最大）
      if (state.peaks.length) {
        if (tone.wave) ctx.fillStyle = tone.wave;
        var bars = Math.min(state.peaks.length, Math.round(total));
        for (var i = 0; i < bars; i += 1) {
          var from = Math.floor((i / bars) * state.peaks.length);
          var to = Math.max(from + 1, Math.floor(((i + 1) / bars) * state.peaks.length));
          var peak = 0;
          for (var j = from; j < to && j < state.peaks.length; j += 1) {
            peak = Math.max(peak, Number(state.peaks[j]) || 0);
          }
          var barHeight = Math.max(1, peak * (height - 8));
          var x = (i / bars) * total;
          ctx.fillRect(x, (height - barHeight) / 2, Math.max(1, total / bars - 0.6), barHeight);
        }
      }

      // 句边界：淡淡的竖线，告诉用户"磁吸会贴到这些位置"
      if (tone.line) ctx.fillStyle = tone.line;
      var pxPerMs = total / state.duration;
      state.boundaries.forEach(function (point) {
        ctx.fillRect(Math.round(point * pxPerMs), 0, 1, height);
      });
    }

    function resize() {
      if (degraded) return;
      draw();
      paintOverlay();
    }

    function setData(next) {
      var data = next || {};
      if (data.duration) state.duration = Math.max(1, Number(data.duration));
      if (data.boundaries) {
        state.boundaries = data.boundaries.slice().sort(function (a, b) {
          return a - b;
        });
      }
      if (data.peaks) state.peaks = data.peaks;
      if ("candidate" in data) state.candidate = data.candidate || null;
      if (typeof data.time === "number") state.time = data.time;
      draw();
      paintOverlay();
    }

    function setTime(ms) {
      state.time = Math.max(0, Math.min(state.duration, Number(ms) || 0));
      paintOverlay();
    }

    function beginDrag(kind) {
      return function (event) {
        event.preventDefault();
        state.drag = kind;
        hint.textContent = "";
      };
    }

    startHandle.addEventListener("mousedown", beginDrag("start"));
    endHandle.addEventListener("mousedown", beginDrag("end"));
    startHandle.addEventListener("touchstart", beginDrag("start"), { passive: false });
    endHandle.addEventListener("touchstart", beginDrag("end"), { passive: false });

    function moveTo(kind, rawMs) {
      var cand = state.candidate;
      if (!cand) return null;
      var hit = snap(rawMs);
      var next = { start_ms: cand.start_ms, end_ms: cand.end_ms };
      if (kind === "start") {
        next.start_ms = Math.min(hit.ms, cand.end_ms - MIN_SPAN_MS);
        next.start_ms = Math.max(0, next.start_ms);
      } else {
        next.end_ms = Math.max(hit.ms, cand.start_ms + MIN_SPAN_MS);
        next.end_ms = Math.min(state.duration, next.end_ms);
      }
      return {
        start_ms: Math.round(next.start_ms),
        end_ms: Math.round(next.end_ms),
        snapped: hit.snapped
      };
    }

    function handleMove(event) {
      if (!state.drag) return;
      var ms = msAt(event.clientX !== undefined ? event.clientX : event.touches[0].clientX);
      var next = moveTo(state.drag, ms);
      if (!next) return;
      state.candidate = Object.assign({}, state.candidate, {
        start_ms: next.start_ms,
        end_ms: next.end_ms
      });
      paintOverlay();
      hint.textContent = next.snapped ? "已贴到句边界" : "自由切点";
    }

    function handleUp() {
      if (!state.drag) return;
      var kind = state.drag;
      state.drag = "";
      if (state.candidate) {
        onChange({
          start_ms: state.candidate.start_ms,
          end_ms: state.candidate.end_ms,
          handle: kind
        });
      }
    }

    function onBackgroundClick(event) {
      if (event.target !== canvas) return;
      onSeek(msAt(event.clientX));
    }

    canvas.addEventListener("mousedown", handleMove);
    canvas.addEventListener("mousemove", handleMove);
    canvas.addEventListener("click", onBackgroundClick);
    window.addEventListener("mousemove", handleMove);
    window.addEventListener("mouseup", handleUp);
    window.addEventListener("touchmove", handleMove, { passive: true });
    window.addEventListener("touchend", handleUp);

    paintOverlay();

    return {
      el: host,
      canvas: canvas,
      overlay: overlay,
      setData: setData,
      setTime: setTime,
      resize: resize,
      draw: draw,
      /** 纯逻辑入口，测试与键盘微调都走它：把某一端搬到 ms 上。 */
      moveHandle: function (kind, ms) {
        var next = moveTo(kind, ms);
        if (!next) return null;
        state.candidate = Object.assign({}, state.candidate, {
          start_ms: next.start_ms,
          end_ms: next.end_ms
        });
        paintOverlay();
        return next;
      },
      commit: function () {
        if (!state.candidate) return null;
        onChange({
          start_ms: state.candidate.start_ms,
          end_ms: state.candidate.end_ms,
          handle: "manual"
        });
        return state.candidate;
      },
      state: function () {
        return state;
      },
      destroy: function () {
        window.removeEventListener("mousemove", handleMove);
        window.removeEventListener("mouseup", handleUp);
        window.removeEventListener("touchmove", handleMove);
        window.removeEventListener("touchend", handleUp);
        if (canvas.parentNode) canvas.parentNode.removeChild(canvas);
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (hint.parentNode) hint.parentNode.removeChild(hint);
      },
      degraded: degraded
    };
  }

  WB.timeline = { mount: mount, MIN_SPAN_MS: MIN_SPAN_MS, SNAP_MS: SNAP_MS };
})();
