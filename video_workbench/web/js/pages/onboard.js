/** 首次启动向导：五件事的环境体检（WebView2 / FFmpeg / 语音模型 / B 站登录 / 大模型）。
 *
 * 三条约束：
 *
 * 1. **只报告，不代劳**。模型下载走已有的 `transcribe.model.download`（有进度、
 *    可取消）；FFmpeg 让用户自己装后指目录（我们不随包分发）。这里重复实现一遍
 *    下载器只会多一个"状态不同步"的来源。
 * 2. **不做成强制门**。第一次打开自动浮出来，但「先跳过」永远可点——缺 LLM 也能
 *    用规则兜底、没登录也能拖本地文件，把人挡在门外是把能用的功能一起锁掉。
 * 3. **跳过一次就不再弹**。看过了是事实，反复弹窗属于骚扰（后端 `ui.onboarded`
 *    落盘）。
 */
(function () {
  "use strict";

  var WB = window.WB;
  var ui = WB.ui;

  var state = {
    steps: [],
    loading: false,
    error: "",
    note: "",
    ready: false,
    blocking: [],
    platform: "",
    firstRun: false,
    busyId: ""
  };

  var els = {};

  // ------------------------------------------------------------ 状态文案

  var STATUS_TEXT = { ok: "就绪", todo: "待设置", missing: "缺失" };
  var STATUS_PILL = { ok: "ok", todo: "warn", missing: "bad" };

  function stepRow(step) {
    var pill = ui.h("span", { class: "onb-pill onb-pill--" + (STATUS_PILL[step.status] || "warn") },
      [STATUS_TEXT[step.status] || step.status]);
    var body = ui.h("div", { class: "onb-row__body" }, [
      ui.h("div", { class: "onb-row__head" }, [
        ui.h("span", { class: "onb-row__title" }, [step.title]),
        pill
      ]),
      ui.h("div", { class: "onb-row__detail" }, [step.detail || ""]),
      step.hint ? ui.h("div", { class: "onb-row__hint" }, [step.hint]) : null
    ]);

    var action = step.action;
    var actionNode = null;
    if (action) {
      var label = action.label || "去处理";
      var btn = ui.h("button", { class: "btn btn--tiny", type: "button" }, [label]);
      btn.addEventListener("click", function () { return runAction(step, btn); });
      actionNode = ui.h("div", { class: "onb-row__act" }, [btn]);
    } else {
      actionNode = ui.h("div", { class: "onb-row__act" }, [
        ui.h("span", { class: "onb-row__dot", "data-ok": step.status === "ok" ? "true" : "false" }, ["✓"])
      ]);
    }
    return ui.h("div", { class: "onb-row", "data-step": step.id }, [body, actionNode]);
  }

  function runAction(step, btn) {
    var action = step.action || {};
    if (action.kind === "url") {
      // 不给 http 链接留"点了什么都没发生"的可能：复制到剪贴板 + 提示
      WB.rpc("app.open_url", { url: action.url }, { silent: true })
        .then(function (res) {
          if (res && res.opened === false) ui.toast("地址已复制：" + action.url, "info", 6000);
        })
        .catch(function () { ui.toast("打开失败，地址：" + action.url, "warn", 8000); });
      return;
    }
    if (action.kind === "route") {
      close();
      WB.router.go(action.route || "settings", action.params || {});
      return;
    }
    if (action.kind === "rpc") {
      btn.disabled = true;
      btn.textContent = "提交中…";
      state.busyId = step.id;
      WB.rpc(action.method, action.params || {})
        .then(function (data) {
          // 下载是长任务：不在这里等它跑完，只把"已经在跑了"说清楚
          var job = (data && data.job) || {};
          ui.toast(job.id ? "已开始下载，进度在状态条里看" : "已提交", "success", 5000);
        })
        .catch(function (err) {
          ui.toast("没能开始：" + ((err && err.message) || "未知原因"), "error", 6000);
          btn.disabled = false;
          btn.textContent = "重试";
        })
        .then(function () { state.busyId = ""; return refresh(); });
      return;
    }
    // note：只解释，不改状态
    ui.toast(step.hint || "这一步需要你在本机自己装/自己登录", "info", 6000);
  }

  // ------------------------------------------------------------ 面板

  function summaryText() {
    var ok = state.steps.filter(function (item) { return item.status === "ok"; }).length;
    var total = state.steps.length;
    if (state.ready) return "五件事已就绪（" + ok + "/" + total + "），可以开始。";
    var names = state.blocking.map(function (id) {
      var found = state.steps.filter(function (item) { return item.id === id; })[0];
      return found ? found.title : id;
    });
    return "还差：" + names.join("、") + "。其余可以先跳过，用能用的部分干活。";
  }

  function panel() {
    var head = ui.h("div", { class: "onb-head" }, [
      ui.h("div", { class: "onb-head__title" }, ["开始之前，先看五件事"]),
      ui.h("div", { class: "onb-head__sub" }, [
        "这个程序不打包 FFmpeg、字体和语音模型——都放你本机（界面里能指路径）。"
      ])
    ]);

    var list = ui.h("div", { class: "onb-list" });
    if (state.loading) {
      list.appendChild(ui.h("div", { class: "onb-empty" }, ["正在检查…"]));
    } else if (state.error) {
      list.appendChild(ui.h("div", { class: "onb-empty onb-empty--bad" }, [state.error]));
    } else {
      state.steps.forEach(function (step) { list.appendChild(stepRow(step)); });
    }

    var sum = ui.h("div", { class: "onb-sum" + (state.ready ? " is-ready" : "") }, [summaryText()]);
    var meta = ui.h("div", { class: "onb-meta" }, [
      state.platform ? "运行环境：" + state.platform : "",
      state.note ? " · " + state.note : ""
    ]);

    var startBtn = ui.h("button", { class: "btn btn--primary", type: "button", id: "onb-done" },
      [state.ready ? "开始使用" : "先这样，进去试试"]);
    startBtn.addEventListener("click", function () { return done(false); });
    var skipBtn = ui.h("button", { class: "btn", type: "button", id: "onb-skip" }, ["跳过，别再提示"]);
    skipBtn.addEventListener("click", function () { return done(true); });
    var retry = ui.h("button", { class: "btn", type: "button" }, ["重新检查"]);
    retry.addEventListener("click", function () { return refresh(); });

    var foot = ui.h("div", { class: "onb-foot" }, [
      ui.h("div", { class: "onb-foot__hint" }, ["以后能在「设置 · 关于」里重看这份体检。"]),
      ui.h("div", { class: "onb-foot__btns" }, [retry, skipBtn, startBtn])
    ]);

    return ui.h("div", { class: "onb-panel" }, [head, list, sum, meta, foot]);
  }

  function paint() {
    if (!els.root) return;
    els.root.innerHTML = "";
    els.root.appendChild(panel());
  }

  // ------------------------------------------------------------ 打开/关闭/提交

  function open() {
    if (els.root) { paint(); return; }
    els.root = ui.h("div", { class: "onb-overlay", id: "onb-overlay" });
    els.root.addEventListener("mousedown", function (event) {
      // 点遮罩不关：这是体检报告，误触关掉会让人以为"检查过了"
      if (event.target === els.root) event.preventDefault();
    });
    document.body.appendChild(els.root);
    paint();
  }

  function close() {
    if (!els.root) return;
    if (els.root.parentNode) els.root.parentNode.removeChild(els.root);
    els.root = null;
  }

  function done(skipped) {
    return WB.rpc("app.onboard.done", { skipped: !!skipped })
      .then(function () {
        state.firstRun = false;
        close();
        ui.toast(skipped ? "跳过了，随时能在设置里回看" : "开始吧", "success", 4000);
      })
      .catch(function (err) {
        ui.toast("没记下来：" + ((err && err.message) || "未知原因"), "error", 5000);
      });
  }

  function load() {
    state.loading = true;
    state.error = "";
    paint();
    return WB.rpc("app.onboard", null, { silent: true })
      .then(function (data) {
        state.steps = (data && data.steps) || [];
        state.ready = !!(data && data.ready);
        state.blocking = (data && data.blocking) || [];
        state.platform = (data && data.platform) || "";
        state.firstRun = !!(data && (data.first_run !== undefined ? data.first_run : true));
        if (data && data.ok_count !== undefined && data.total !== undefined) {
          state.note = data.ok_count + "/" + data.total + " 就绪";
        }
      })
      .catch(function (err) {
        state.error = "体检没跑起来：" + ((err && err.message) || "未知原因");
      })
      .then(function () {
        state.loading = false;
        paint();
        return state;
      });
  }

  function refresh() {
    return load();
  }

  /** 首屏调用：只有第一次来、且不是"已经看过了"才浮出来。 */
  function maybeShow(info) {
    var onboarded = !!(info && info.onboarded);
    if (onboarded) return Promise.resolve(false);
    open();
    return load().then(function () { return true; });
  }

  // ------------------------------------------------------------ 页面（也当独立路由）

  function render() {
    state.loading = true;
    var host = ui.h("div", { class: "onb-page" }, []);
    // 走页面时不需要遮罩层，直接把 panel 挂进来
    setTimeout(function () {
      refresh().then(function () {
        host.innerHTML = "";
        host.appendChild(panel());
      });
    }, 0);
    return host;
  }

  WB.router.define("onboard", {
    title: "首次检查",
    crumb: "运行环境体检",
    icon: "settings",
    nav: false, // 不进左栏：它是"第一次打开才出现"的东西，不是日常页面
    render: render,
    _internals: { state: state, load: load, refresh: refresh, done: done, open: open, close: close, runAction: runAction }
  });

  WB.onboard = {
    open: open,
    close: close,
    load: load,
    refresh: refresh,
    maybeShow: maybeShow,
    done: done
  };
})();
