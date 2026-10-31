/* ============================================================================
 * components/player.js —— 预览播放器（切片台用）
 *
 * 只做三件事：装一个 <video>、按毫秒 seek、把播放进度报出来。
 * 不做播放列表、不做贴纸、不碰 ffmpeg——那是导出层的事。
 *
 * 两条约束：
 * 1. **时间统一用毫秒**：这一层是唯一把毫秒换成 `currentTime`（秒）的地方，
 *    上层不许再自己乘除 1000，否则误差会一层层放大。
 * 2. **没有源文件也要能装**：用户可能只下了音频或文件被挪走了，这时
 *    `video` 是空 src，界面靠 `hasVideo()` 决定要不要提示。
 * 3. **安全区是预览层**：`setSafeArea(true)` 只是画两块半透明遮罩，不改画面、
 *    不进导出——它提醒人"这里会被平台按钮压住"，不替人做决定。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});

  function mount(host, options) {
    var opts = options || {};
    var onTime = typeof opts.onTime === "function" ? opts.onTime : function () {};
    var onState = typeof opts.onState === "function" ? opts.onState : function () {};

    var video = document.createElement("video");
    video.className = "wb-player";
    video.setAttribute("playsinline", "");
    video.preload = "auto";

    var hasVideo = false;
    var listeners = [];

    function emitTime() {
      var ms = Math.round((Number(video.currentTime) || 0) * 1000);
      onTime(ms);
    }

    video.addEventListener("timeupdate", emitTime);
    video.addEventListener("seeked", emitTime);
    video.addEventListener("play", function () {
      onState({ playing: true });
    });
    video.addEventListener("pause", function () {
      onState({ playing: false });
    });
    video.addEventListener("loadedmetadata", function () {
      onState({ ready: true, durationMs: Math.round((Number(video.duration) || 0) * 1000) });
      if (opts.startMs) seek(opts.startMs);
    });
    video.addEventListener("error", function () {
      hasVideo = false;
      onState({ ready: false, error: "这个文件读不了（可能已被移动或格式不支持）" });
    });

    // 安全区：竖屏成片顶部 120px / 底部 260px 会被平台按钮压住，预览里画出来。
    var safe = document.createElement("div");
    safe.className = "wb-player__safe";
    safe.setAttribute("aria-hidden", "true");
    safe.style.display = "none";
    // 两块遮罩各自带一条虚线边界：纯半透明在深色画面上量不出来，也看不清边界在哪
    var safeTop = document.createElement("div");
    safeTop.className = "wb-player__safe-top";
    var safeBottom = document.createElement("div");
    safeBottom.className = "wb-player__safe-bottom";
    safe.appendChild(safeTop);
    safe.appendChild(safeBottom);
    host.appendChild(video);
    host.appendChild(safe);

    /** 开关安全区遮罩；show=false 时整块不显示，不占位、不挡鼠标。 */
    function setSafeArea(show, area) {
      var box = area || {};
      var top = Math.max(0, Number(box.top) || 120);
      var bottom = Math.max(0, Number(box.bottom) || 260);
      safe.style.setProperty("--safe-top", top + "px");
      safe.style.setProperty("--safe-bottom", bottom + "px");
      safeTop.style.height = top + "px";
      safeBottom.style.height = bottom + "px";
      safe.style.display = show ? "" : "none";
    }

    function load(url) {
      hasVideo = Boolean(url);
      video.src = url || "";
      if (url) {
        // 预加载元数据，不然时间轴拿不到时长，拖动会跳到 0
        try {
          video.load();
        } catch (err) {
          hasVideo = false;
        }
      }
    }

    function seek(ms) {
      var seconds = Math.max(0, Number(ms) || 0) / 1000;
      try {
        video.currentTime = seconds;
      } catch (err) {
        /* jsdom 里没有真实解码器，赋值失败不影响逻辑 */
      }
      onTime(Math.round(seconds * 1000));
    }

    function play() {
      var result = null;
      try {
        result = video.play();
      } catch (err) {
        return;
      }
      if (result && typeof result.catch === "function") result.catch(function () {});
    }

    function pause() {
      try {
        video.pause();
      } catch (err) {
        /* 同上 */
      }
    }

    /** 从 A 播到 B 自动停：预览一段候选时最常用的动作。
     *
     * preRollMs：提前这么多开始播，让用户听到切点前面的上下文（P3-04 的"3 秒预卷"）。
     * 只影响预览起点，不影响候选的 start_ms——预卷是给人耳朵用的，不是切点。
     */
    function playRange(startMs, endMs, options) {
      var opts = options || {};
      var preRoll = Math.max(0, Number(opts.preRollMs) || 0);
      var from = Math.max(0, (Number(startMs) || 0) - preRoll);
      seek(from);
      play();
      var limit = Math.max(0, Number(endMs) || 0);
      var stop = function () {
        if (limit && Math.round((video.currentTime || 0) * 1000) >= limit) {
          pause();
          video.removeEventListener("timeupdate", stop);
        }
      };
      video.addEventListener("timeupdate", stop);
    }

    /** 逐帧步进：帧长由调用方给（后端探到的真实 fps），拿不到才退回 1/25 秒。 */
    function step(frames, frameMs) {
      var one = Math.max(1, Number(frameMs) || 40);
      var delta = ((Number(frames) || 0) * one) / 1000;
      seek(Math.max(0, Math.round(((Number(video.currentTime) || 0) + delta) * 1000)));
    }

    function destroy() {
      pause();
      video.removeAttribute("src");
      try {
        video.load();
      } catch (err) {
        /* 忽略 */
      }
      if (safe.parentNode) safe.parentNode.removeChild(safe);
      if (video.parentNode) video.parentNode.removeChild(video);
      listeners.forEach(function (off) {
        off();
      });
      listeners = [];
    }

    return {
      el: video,
      load: load,
      seek: seek,
      play: play,
      pause: pause,
      playRange: playRange,
      step: step,
      setSafeArea: setSafeArea,
      destroy: destroy,
      hasVideo: function () {
        return hasVideo;
      },
      currentMs: function () {
        return Math.round((Number(video.currentTime) || 0) * 1000);
      }
    };
  }

  WB.player = { mount: mount };
})();
