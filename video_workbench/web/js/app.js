/* ============================================================================
 * app.js —— 外壳：启动、皮肤、左栏、搜索、任务中心、状态条
 *
 * 这个文件是**最后一个**加载的，它负责把前面几块拼起来：
 *   boot() 等桥 → 拉 bootstrap → 装皮肤 → 渲染左栏 → 起路由 → 告诉 Python「我好了」
 *
 * 三处刻意的设计：
 * 1. **`mark_ready()` 由前端发**，不是后端猜。pywebview 的 evaluate_js 在文档
 *    就绪前调用会静默丢失事件，只有页面自己说"我好了"才可靠（desktop.py 里
 *    `__wbEvent` 的缓冲队列就是等这个信号）。
 * 2. **皮肤是 HTML 上的一个属性**（`<html data-skin>`），不是 JS 里的一堆分支。
 *    换肤只改一个属性，CSS 变量整份跟着换，中间不会有"半深半浅"的帧。
 * 3. **状态条的每个数字都来自真实事件**。没数据的写 "—"，不编。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  // 便利别名：后续页面统一用 WB.toast，不必到处 ui.toast
  WB.toast = function (message, level, timeoutMs) {
    return ui.toast(message, level, timeoutMs);
  };

  var state = {
    info: null,
    settings: null,
    jobs: [],
    connected: false
  };
  WB.state = state;

  // ------------------------------------------------------------ 事件总线
  //
  // Python 侧通过 `window.__wbEvent(name, json)` 推事件。这里转成本地订阅，
  // 顺带处理三件"所有页面都关心"的事：toast、任务计数、皮肤变更。

  var listeners = {};
  WB.on = function (name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
    return function () {
      listeners[name] = listeners[name].filter(function (item) {
        return item !== fn;
      });
    };
  };

  function emit(name, payload) {
    (listeners[name] || []).forEach(function (fn) {
      try {
        fn(payload);
      } catch (err) {
        if (window.console) console.error("[event]", name, err);
      }
    });
    (listeners["*"] || []).forEach(function (fn) {
      try {
        fn(name, payload);
      } catch (err) {
        if (window.console) console.error("[event*]", name, err);
      }
    });
  }
  WB.emit = emit;

  window.__wbEvent = function (name, payloadJson) {
    var payload = payloadJson;
    if (typeof payloadJson === "string") {
      try {
        payload = JSON.parse(payloadJson);
      } catch (_err) {
        payload = { raw: payloadJson };
      }
    }
    handleBuiltin(name, payload);
    emit(name, payload);
  };

  function handleBuiltin(name, payload) {
    if (name === "toast") {
      ui.toast((payload && payload.message) || "", (payload && payload.level) || "info");
      return;
    }
    if (name === "app.skin" && payload && payload.skin) {
      applySkin(payload.skin, false);
      return;
    }
    if (name === "app.settings" && payload && payload.settings) {
      state.settings = payload.settings;
      return;
    }
    if (name.indexOf("job.") === 0) {
      refreshJobText(payload);
      // 任务事件种类多（queued/progress/log/done/cancelled），订阅方只关心"有变化"
      emit("job.any", payload);
    }
  }

  // ------------------------------------------------------------ 皮肤

  function applySkin(skin, persist) {
    var next = skin === "exhibition" ? "exhibition" : "ark";
    document.documentElement.setAttribute("data-skin", next);
    var chips = document.querySelectorAll("[data-skin-chip]");
    for (var i = 0; i < chips.length; i++) {
      chips[i].setAttribute("aria-pressed", chips[i].dataset.skinChip === next ? "true" : "false");
    }
    if (persist && !WB.isMock()) {
      WB.rpc("app.skin.set", { skin: next }, { silent: true }).catch(function () {
        /* 落盘失败不拦着换肤：视觉已经变了，下次启动再说 */
      });
    }
  }
  WB.applySkin = applySkin;

  // ------------------------------------------------------------ 左栏

  function applyRail(collapsed) {
    document.documentElement.setAttribute("data-rail", collapsed ? "collapsed" : "expanded");
    var toggle = document.getElementById("rail-toggle");
    if (toggle) toggle.setAttribute("title", collapsed ? "展开左栏" : "折叠左栏");
  }
  WB.applyRail = applyRail;

  function wireRailToggle() {
    var toggle = document.getElementById("rail-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var collapsed = document.documentElement.getAttribute("data-rail") !== "collapsed";
      applyRail(collapsed);
      if (!WB.isMock()) {
        WB.rpc("app.rail.set", { collapsed: collapsed }, { silent: true }).catch(function () {});
      }
    });
  }

  // ------------------------------------------------------------ 搜索

  function wireSearch() {
    var input = document.getElementById("global-search");
    if (!input) return;
    var timer = null;

    function submit(value) {
      var query = String(value || "").trim();
      if (WB.router.current() && WB.router.current().route === "library") {
        if (WB.pages.library && WB.pages.library.search) WB.pages.library.search(query);
      } else {
        WB.router.go("library", query ? { q: query } : undefined);
      }
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      var value = input.value;
      timer = setTimeout(function () {
        submit(value);
      }, 220);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        clearTimeout(timer);
        submit(input.value);
      }
      if (event.key === "Escape") {
        input.value = "";
        submit("");
        input.blur();
      }
    });

    document.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        event.preventDefault();
        input.focus();
        input.select();
      }
    });
  }

  // ------------------------------------------------------------ 任务中心

  // 任务事件有两种形状：job.created 带 {job:{...}}，job.updated 是扁平的一层
  // （后端 jobs._report 发的就是 {id,status,stage,progress}）。两种都要能合并，
  // 否则状态条只在「新建」那一刻记得住，之后每条进度都白丢——用户看到的一直是 0。
  function jobRowOf(payload) {
    if (!payload) return null;
    if (payload.job && payload.job.id) return payload.job;
    if (payload.id) return payload;
    return null;
  }

  function mergeJob(incoming) {
    if (!incoming || !incoming.id) return;
    var found = false;
    state.jobs = (state.jobs || []).map(function (job) {
      if (job.id === incoming.id) {
        found = true;
        return Object.assign({}, job, incoming);
      }
      return job;
    });
    if (!found) state.jobs.unshift(incoming);
  }

  // 任务的类型列在数据库里叫 type（wb_job.type）；早期代码按 kind 读，永远拿到空串，
  // 于是下载中的任务一个都不计进「下载」。两个都认，但以 type 为准。
  function jobKind(job) {
    return String((job && (job.type || job.kind)) || "");
  }

  var EXPORT_PREFIXES = ["clip.export", "merge", "publish", "nle", "video.export"];

  function jobBucket(job) {
    var kind = jobKind(job);
    if (kind.indexOf("download") === 0) return "download";
    if (kind.indexOf("transcribe") === 0) return "transcribe";
    if (kind.indexOf("subtitle") === 0) return "transcribe";
    for (var i = 0; i < EXPORT_PREFIXES.length; i++) {
      if (kind.indexOf(EXPORT_PREFIXES[i]) === 0) return "export";
    }
    return null; // 认不出来的（demo、诊断包之类）不占这三格
  }

  // 给测试留的纯函数 seams：状态条的两个坑（type/kind、先合并再统计）都在这里，
  // 直接对着数据断言比在 jsdom 里跟 mock 的模拟任务抢时序可靠得多。
  WB._jobBucket = jobBucket;
  WB._countJobs = function (rows) {
    var counts = { download: 0, transcribe: 0, export: 0 };
    (rows || []).forEach(function (job) {
      if (!job || (job.status !== "running" && job.status !== "queued")) return;
      var bucket = jobBucket(job);
      if (bucket === "download") counts.download++;
      else if (bucket === "transcribe") counts.transcribe++;
      else if (bucket === "export") counts.export++;
    });
    return counts;
  };
  function refreshJobText(payload) {
    // 先合并再统计：反过来的话，新任务的那一刻永远是 0，要等下一个事件才补上
    mergeJob(jobRowOf(payload));
    var counts = WB._countJobs(state.jobs);
    var text = "下载 " + counts.download + " · 转录 " + counts.transcribe + " · 导出 " + counts.export;
    var node = document.getElementById("status-jobs-text");
    if (node) node.textContent = text;
    var dot = document.getElementById("status-dot");
    if (dot) dot.setAttribute("data-active", counts.download + counts.transcribe + counts.export > 0 ? "true" : "false");
  }

  function openJobsModal() {
    var body = ui.h("div", { class: "col", style: "gap:12px" });
    var dialog = ui.modal({
      title: "任务中心",
      body: body,
      footer: [
        ui.h("button", {
          class: "btn",
          text: "跑一个演示任务",
          title: "验证进度 / 取消 / 重试三条链路（Phase 0 自检）",
          onclick: function () {
            WB.rpc("job.demo.submit", { seconds: 6 }, { silent: true })
              .then(function () {
                ui.toast("演示任务已提交，进度会出现在状态条与这里", "success");
                paintJobs(body, rows, function () {});
              })
              .catch(function (err) {
                ui.toast((err && err.message) || "提交失败", "error");
              });
          }
        }),
        ui.h("button", {
          class: "btn",
          text: "刷新",
          onclick: function () {
            paintJobs(body, rows);
          }
        }),
        ui.h("button", { class: "btn btn--primary", text: "关闭", onclick: function () { dialog.close(); } })
      ]
    });
    var rows = {};
    paintJobs(body, rows);

    // 弹窗关掉就把订阅摘掉，否则每次开窗都留一个往已卸载节点里写的回调
    var off = WB.on("job.any", function (payload) {
      if (!body.isConnected) {
        off();
        return;
      }
      syncJobEvent(body, rows, payload);
    });
    var offSync = WB.on("jobs.synced", function (list) {
      if (!body.isConnected) {
        offSync();
        return;
      }
      applyJobs(body, rows, toJobArray(list));
    });
    return dialog;
  }

  /** 把一行任务的易变部分写进 DOM（不重建节点）。 */
  function fillJobRow(refs, job) {
    var percent = Math.round((Number(job.progress) || 0) * 100);
    refs.title.textContent = jobKind(job) || job.id;
    refs.percent.textContent = percent + "%";
    refs.bar.style.width = percent + "%";
    refs.stage.textContent = job.stage || job.message || "";
    refs.pill.className = ("pill " + ui.statusClass(job.status)).trim();
    refs.pill.textContent = ui.statusText(job.status);
    var running = job.status === "running" || job.status === "queued";
    refs.act.textContent = running ? "取消" : "重试";
    refs.act.setAttribute("data-act", running ? "cancel" : "retry");
    refs.job = job;
  }


  function jobRow(job, repaint) {
    var refs = {};
    var title = ui.h("span", { class: "mono", text: jobKind(job) || job.id });
    var pill = ui.statusPill(job.status);
    var percent = ui.h("span", { class: "mono muted" });
    var bar = ui.h("div", { class: "progress__bar" });
    var stage = ui.h("span", { class: "muted truncate" });
    var act = ui.h("button", { class: "btn btn--ghost", text: "取消" });
    act.addEventListener("click", function () {
      var current = refs.job || job;
      var running = current.status === "running" || current.status === "queued";
      var method = running ? "job.cancel" : "job.retry";
      WB.rpc(method, { id: current.id }, { silent: true })
        .then(function () { repaint(); })
        .catch(function (err) {
          ui.toast((err && err.message) || (running ? "取消失败" : "重试失败"), "error");
        });
    });
    var row = ui.h("div", {
      class: "card",
      style: "background:transparent",
      dataset: { jobId: String(job.id || "") }
    }, [
      ui.h("div", { class: "card__body col", style: "gap:8px" }, [
        ui.h("div", { class: "row", style: "justify-content:space-between" }, [
          ui.h("div", { class: "row", style: "gap:8px" }, [title, pill]),
          percent
        ]),
        ui.h("div", { class: "progress" }, [bar]),
        ui.h("div", { class: "row", style: "justify-content:space-between" }, [stage, act])
      ])
    ]);
    refs.row = row;
    refs.title = title;
    refs.pill = pill;
    refs.percent = percent;
    refs.bar = bar;
    refs.stage = stage;
    refs.act = act;
    fillJobRow(refs, job);
    return refs;
  }

  /**
   * `rows` 是「任务 id → 那几行节点的引用」，跨事件复用。
   * `force` 为真时整块重建（任务集合变了、或用户点了刷新）。
   */
  function paintJobs(body, rows, done) {
    WB.rpc("job.list", {}, { silent: true, fallback: [] })
      .then(function (data) {
        applyJobs(body, rows, toJobArray(data), done);
      })
      .catch(function (err) {
        body.textContent = "";
        body.appendChild(ui.empty("读不到任务", (err && err.message) || "任务表不可用"));
      });
  }

  /**
   * 对账拿回来的是磁盘上的状态，可能比界面刚收到的进度**旧**半拍。
   * 两边都在跑时取进度大的那个，别让对账把进度条往回拽。
   */
  function preferFresher(list) {
    var byId = {};
    state.jobs.forEach(function (job) {
      if (job && job.id) byId[job.id] = job;
    });
    return list.map(function (job) {
      var old = job && job.id ? byId[job.id] : null;
      if (!old) return job;
      var sameRunning = old.status === "running" && job.status === "running";
      if (!sameRunning) return job;
      var a = Number(old.progress) || 0;
      var b = Number(job.progress) || 0;
      return b >= a ? job : old;
    });
  }
  /** 已有一份任务列表时走这里：集合没变就只刷数值，变了才重建。 */
  function applyJobs(body, rows, list, done) {
        var jobs = preferFresher(list).slice(0, 30);
        state.jobs = jobs;
        refreshJobText(null);
        var sameSet =
          jobs.length === Object.keys(rows).length &&
          jobs.every(function (job) { return !!rows[job.id]; });

        if (sameSet) {
          // 同一批任务：只刷数值，不碰节点结构，也就不会闪
          jobs.forEach(function (job) { fillJobRow(rows[job.id], job); });
          if (done) done();
          return;
        }

        var frag = document.createDocumentFragment();
        var next = {};
        if (!jobs.length) {
          frag.appendChild(ui.empty("没有任务", "Phase 1 起，下载 / 转录 / 导出都会出现在这里。可以先点右下角跑一个演示任务。"));
        } else {
          jobs.forEach(function (job) {
            var repaint = function () { paintJobs(body, next); };
            var refs = jobRow(job, repaint);
            next[job.id] = refs;
            frag.appendChild(refs.row);
          });
        }
        Object.keys(rows).forEach(function (id) { delete rows[id]; });
        Object.keys(next).forEach(function (id) { rows[id] = next[id]; });
        body.textContent = "";
        body.appendChild(frag);
        if (done) done();
  }

  /** 事件来了只动一行：新建 / 进度 / 结束都走这里。 */
  function syncJobEvent(body, rows, payload) {
    var job = jobRowOf(payload);
    if (!job || !job.id) return;
    mergeJob(job);
    refreshJobText(null);
    var refs = rows[job.id];
    if (!refs) {
      paintJobs(body, rows); // 新任务：这时候才需要重建（一次）
      return;
    }
    fillJobRow(refs, job);
  }
  /** job.list 可能返回数组，也可能是 {items:[…]}；两处都收，省得以后再改。 */
  function toJobArray(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.items)) return data.items;
    return [];
  }

  // ------------------------------------------------------------ 对账

  /**
   * 事件可能丢（尤其一条长任务跑完那一刻）。只要内存里还有在跑的任务，就定期拿
   * `job.list` 覆盖一次真实状态——丢一条事件最多让界面晚 3 秒，而不是永久停在旧快照。
   */
  var RECONCILE_MS = 3000;

  function hasActiveJobs() {
    for (var i = 0; i < state.jobs.length; i++) {
      var s = state.jobs[i] && state.jobs[i].status;
      if (s === "running" || s === "queued") return true;
    }
    return false;
  }

  function reconcileJobs() {
    if (state.reconciling) return;
    if (!hasActiveJobs()) return;
    state.reconciling = true;
    WB.rpc("job.list", {}, { silent: true, fallback: [] })
      .then(function (data) {
        var jobs = toJobArray(data);
        state.jobs = jobs;
        refreshJobText(null);
        emit("jobs.synced", jobs);
      })
      .catch(function () {})
      .then(function () {
        state.reconciling = false;
      });
  }

  function startReconcile() {
    setInterval(reconcileJobs, RECONCILE_MS);
    window.addEventListener("focus", reconcileJobs);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) reconcileJobs();
    });
  }

  WB._reconcileJobs = reconcileJobs;
  WB._hasActiveJobs = hasActiveJobs;

  // ------------------------------------------------------------ 状态条

  function paintInfo(info) {
    if (!info) return;
    state.info = info;
    var version = document.getElementById("status-version");
    if (version) version.textContent = "v" + (info.version || "—");

    var deps = info.deps || {};
    var missing = [];
    if (!deps.ffmpeg) missing.push("ffmpeg");
    if (!deps.yt_dlp) missing.push("yt-dlp");
    if (!deps.faster_whisper) missing.push("faster-whisper");
    var depsNode = document.getElementById("status-deps");
    if (depsNode) {
      depsNode.textContent = missing.length ? "缺少 " + missing.join(" / ") : "依赖齐全";
      depsNode.setAttribute("title", missing.length ? "缺这些跑不了对应环节，设置页有说明" : "ffmpeg / yt-dlp / faster-whisper 都已就绪");
    }
    document.title = (info.name || "VideoWorkbench") + " · " + (WB.router.current() ? WB.router.current().route : "");
  }

  // ------------------------------------------------------------ 启动

  function boot() {
    // 1) 皮肤：URL 参数（`#skin=ark`）优先，其次后端配置，最后默认。
    var skinFromHash = (function () {
      var match = /[#&]skin=([a-z]+)/.exec(String(location.hash || ""));
      return match ? match[1] : "";
    })();
    applySkin(skinFromHash || "ark", false);
    startReconcile();
    applyRail(false);

    // 2) 左栏与交互
    WB.router.renderRail(document.getElementById("rail-nav"));
    // 左栏底部的「设置」是静态 HTML（不在 navItems 里，renderRail 管不到），
    // 之前点了没反应。这里统一接管左栏内所有带 data-route 的按钮。
    var railEl = document.querySelector(".rail"); // aside.rail 没有 id
    if (railEl) {
      railEl.addEventListener("click", function (event) {
        var node = event.target;
        var item = node && node.closest ? node.closest(".rail__item[data-route]") : null;
        if (!item) return;
        if (item.closest("#rail-nav")) return; // 上面那批已自带 onclick
        WB.router.go(item.dataset.route);
      });
    }
    wireRailToggle();
    wireSearch();

    var chips = document.querySelectorAll("[data-skin-chip]");
    for (var i = 0; i < chips.length; i++) {
      chips[i].addEventListener("click", function () {
        applySkin(this.dataset.skinChip, true);
      });
    }

    var jobsBtn = document.getElementById("btn-jobs");
    if (jobsBtn) jobsBtn.addEventListener("click", openJobsModal);
    var statusJobs = document.getElementById("status-jobs");
    if (statusJobs) statusJobs.addEventListener("click", openJobsModal);

    // 3) 路由：切页时同步标题与面包屑
    WB.router.onChange(function (current, definition) {
      var title = document.getElementById("page-title");
      if (title) title.textContent = (definition && definition.title) || "";
      var crumb = document.getElementById("page-crumb");
      if (crumb) crumb.textContent = (definition && definition.crumb) || "";
      document.title = "VideoWorkbench · " + ((definition && definition.title) || "");
      // 搜索框只在素材库页有意义；别的页清空，免得看到不生效的旧关键词
      if (current.route !== "library") {
        var input = document.getElementById("global-search");
        if (input) input.value = "";
      }
    });

    // 4) 数据
    // 已经明确是演示模式（?mock=1）就别等了；否则先等桥，等不到再降级。
    if (WB.isMock()) {
      // 演示模式也要把状态条填上：否则版本号停在"—"、依赖卡在"检查中…"，
      // 截图和录屏里看的人会以为真机就是这样。（假信息和没信息一样糟。）
      state.connected = false;
      WB.rpc("app.bootstrap", null, { silent: true })
        .then(function (data) {
          if (!data) return;
          state.settings = data.settings || null;
          state.jobs = toJobArray(data.jobs);
          paintInfo(data.info);
        })
        .catch(function () { /* 演示模式拿不到就保持占位，不打断渲染 */ })
        .then(function () { finishBoot(null); });
      return;
    }

    WB.waitBridge(8000)
      .then(function (ready) {
        if (!ready) {
          // 桥没等来：画面照画（空白窗口更难判断问题），但必须说清楚是假数据。
          WB.setMock(true);
          state.connected = false;
          ui.toast("没连上本地服务，当前显示的是演示数据", "warn", 8000);
          finishBoot(null);
          return null;
        }
        state.connected = true;
        return WB.rpc("app.bootstrap", null, { silent: true });
      })
      .then(function (data) {
        if (data) {
          state.settings = data.settings || null;
          state.jobs = toJobArray(data.jobs);
          paintInfo(data.info);
          if (data.settings && data.settings.ui) {
            if (!skinFromHash) applySkin(data.settings.ui.skin, false);
            applyRail(!!data.settings.ui.rail_collapsed);
          }
          if (state.jobs.length) refreshJobText(null);
        }
        finishBoot(data);
      })
      .catch(function (err) {
        // 桥没起来也要把界面画出来：空白窗口比"能看但没数据"更难判断问题
        ui.toast("与后端的连接没有建立：" + ((err && err.message) || "未知原因"), "error", 8000);
        finishBoot(null);
      });
  }

  function finishBoot(data) {
    WB.router.start("library");
    // 告诉 Python：可以开始推事件了（desktop.py 会重放缓冲里的事件）
    if (state.connected && window.pywebview && window.pywebview.api && window.pywebview.api.mark_ready) {
      try {
        window.pywebview.api.mark_ready();
      } catch (_err) {
        /* 早期版本没有这个方法，不影响主流程 */
      }
    }
    if (!data && !WB.isMock()) {
      var depsNode = document.getElementById("status-deps");
      if (depsNode) depsNode.textContent = "后端未连接";
    }
    // 首次启动的"五件事体检"：只在没看过时浮出来，跳过一次就不再弹。
    // 放在 finishBoot 之后：先让用户看见界面，再叠一层说明，而不是先白屏再出东西。
    if (WB.onboard && data && data.info) {
      WB.onboard.maybeShow(data.info).catch(function () { /* 体检失败不挡启动 */ });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
