/* ============================================================================
 * pages/merge.js —— 合并
 *
 * 三件事必须一眼可见，因为它们决定了成片能不能直接 copy：
 *   **顺序**（列表就是成片顺序，上移/下移/反转都改这份数组）、
 *   **预检**（规格不一致必须当场说清是哪一项、哪一段）、
 *   **模式**（快速拼接灰掉时要写原因，不能让按钮莫名点不动）。
 *
 * 约束：列表里的形状由后端 `merge.preflight` 决定，前端不再自己 probe。
 * 探测失败的文件留在表里带错误——用户得看见是哪一条坏。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  // 合并可能跑很久：两小时素材的精确重编码实测要 20 分钟。轮询节奏与"什么时候
  // 放手"抽成纯函数，避免再出现"到 12 分钟就停、界面永远停在合并中"。
  var POLL_MS = 800;
  var SLOW_MS = 3000;       // 超过 POLL_LIMIT 之后放慢，界面不再密集问一次
  var POLL_LIMIT = 900;     // 12 分钟之后转慢速跟随
  var HARD_LIMIT = 3600;    // 约 2 小时 40 分：到这儿才放手，并说清"还在后台跑"

  var state = {
    sources: { clips: [], library: [] },
    query: "",
    order: [],
    preflight: null,
    loading: false,
    busy: false,
    running: false,
    jobId: "",
    detached: false,
    progress: 0,
    error: "",
    note: "",
    mode: "quick",
    modeManual: false,
    outNameManual: false,
    width: 0,
    height: 0,
    fps: 0,
    outDir: "",
    outDirManual: false,
    outName: "",
    // 上一次成功的合并：打开输出目录只认这条记录的 merge_id，不拿页面上的路径去开。
    lastMergeId: "",
    lastOutput: ""
  };

  var els = {};
  var timer = null;

  function fmtMs(ms) {
    var total = Math.max(0, Math.round((ms || 0) / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function fmtBytes(n) {
    var v = Number(n) || 0;
    if (v < 1024) return v + " B";
    if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " KB";
    return (v / 1024 / 1024).toFixed(1) + " MB";
  }

  function nameOf(path) {
    return String(path || "").split(/[\\/]/).pop();
  }

  function paths() {
    return state.order.map(function (r) {
      return r.path;
    });
  }

  // ------------------------------------------------------------ 加载来源

  function loadSources() {
    state.loading = true;
    paintSources();
    return WB.rpc("merge.sources", { query: state.query }, { silent: true }).then(function (data) {
      state.loading = false;
      state.error = "";
      state.sources = { clips: (data && data.clips) || [], library: (data && data.library) || [] };
      paintSources();
    }).catch(function (err) {
      state.loading = false;
      state.error = (err && err.message) || "来源列表读不出来";
      // 上次成功读到的列表要留着：读失败不等于来源没了，清空会让用户以为素材库被删。
      paintSources();
    });
  }

  // ------------------------------------------------------------ 列表编辑

  function addSource(row) {
    if (!row || !row.path) return;
    if (state.order.some(function (r) { return r.path === row.path; })) {
      state.note = "已经在列表里了";
      paintAll();
      return;
    }
    state.order.push({
      path: row.path,
      name: row.name || nameOf(row.path),
      origin: row.origin || "clip",
      bytes: row.bytes || 0,
      duration_ms: row.duration_ms || 0
    });
    state.note = "";
    refreshPreflight();
  }

  function addLocal() {
    state.busy = true;
    paintAll();
    WB.rpc("fs.pick", { kind: "files" }, { silent: true }).then(function (data) {
      state.busy = false;
      var picked = (data && data.path) || "";
      if (!picked) {
        paintAll();
        return;
      }
      // 系统选择框允许多选，桌面层用 "|" 把多个路径拼回来（与素材库导入共用同一约定）。
      var paths = String(picked).split("|").filter(function (path) {
        return !!String(path || "").trim();
      });
      paths.forEach(function (path) {
        var clean = String(path).trim();
        addSource({ path: clean, name: nameOf(clean), origin: "local" });
      });
    }).catch(function (err) {
      state.busy = false;
      state.error = (err && err.message) || "选择文件失败";
      paintAll();
    });
  }

  function moveAt(index, delta) {
    var next = index + delta;
    if (next < 0 || next >= state.order.length) return;
    var row = state.order.splice(index, 1)[0];
    state.order.splice(next, 0, row);
    refreshPreflight();
  }

  function removeAt(index) {
    state.order.splice(index, 1);
    refreshPreflight();
  }

  function reverse() {
    state.order.reverse();
    refreshPreflight();
  }

  function clearAll() {
    if (!state.order.length) return;
    state.order = [];
    state.preflight = null;
    // 清空 = 回到新任务的初态：手选的输出目录、上次成片引用一起放下。
    // 只清顺序表会留下「目录指向上一轮、列表却是空的」这种错位状态。
    state.outDir = "";
    state.outDirManual = false;
    state.lastMergeId = "";
    state.lastOutput = "";
    state.note = "";
    state.error = "";
    paintAll();
  }

  // ------------------------------------------------------------ 预检

  function refreshPreflight() {
    paintOrder();
    if (!state.order.length) {
      state.preflight = null;
      paintAll();
      return;
    }
    if (timer) clearTimeout(timer);
    state.busy = true;
    paintAll();
    timer = setTimeout(function () {
      WB.rpc(
        "merge.preflight",
        { paths: paths(), width: state.width, height: state.height, fps: state.fps },
        { silent: true }
      ).then(function (data) {
        state.busy = false;
        state.preflight = data || null;
        state.error = "";
        if (data && data.output) {
          // 手选过就不要再被预检的默认值盖回去——那等于用户点一次白点。
          if (!state.outDirManual) state.outDir = data.output.dir || state.outDir;
          // 文件名里的段数由后端按当前列表给：用户没亲手改过就跟它走，
          // 否则勾了三段还挂着"合并_1段"这种自相矛盾的名字。
          if (!state.outNameManual) state.outName = data.output.name || "";
        }
        // 规格一致才允许快速拼接；**只有真的存在差异**时才自动切精确。
        // 只勾了一条也算 can_quick=false，但那种情况说"规格不一致"是在骗人——
        // 用户看到的是一句莫名其妙的横幅，而不是"再加一段"。
        var diffs = (data && data.differences) || [];
        if (data && data.can_quick === false && state.mode === "quick" && diffs.length) {
          if (!state.modeManual) {
            state.mode = "precise";
            state.note = "规格不一致，已切到精确重编码";
          }
        } else if (data && data.can_quick && state.mode === "precise" && !state.modeManual) {
          // 回到一致时切回快速：否则用户删掉不合规那条后仍停在精确模式，
          // 白白多花几倍编码时间。手动选过模式的例外，别抢他的选择。
          state.mode = "quick";
          state.note = "参数已经一致，已切回快速拼接";
        }
        paintAll();
      }).catch(function (err) {
        state.busy = false;
        state.error = (err && err.message) || "预检失败";
        paintAll();
      });
    }, 220);
  }

  // ------------------------------------------------------------ 绘制

  function paintSources() {
    if (!els.sourceList) return;
    els.sourceList.textContent = "";
    // 左栏是「能加什么」的入口，它自己也有三态：读的时候不能显示成"一个来源都没有"
    // （那会让人以为素材库是空的，于是去别处翻），读失败也不能静默留白。
    if (state.loading) {
      els.sourceList.appendChild(ui.loading("正在读取可用来源…"));
      if (els.sourceCount) els.sourceCount.textContent = "";
      return;
    }
    if (state.error) {
      els.sourceList.appendChild(
        ui.errorBlock({
          title: "读不到可用来源",
          message: state.error,
          onRetry: function () { loadSources(); }
        })
      );
      if (els.sourceCount) els.sourceCount.textContent = "";
      return;
    }
    var groups = [
      { key: "clips", label: "切片成片", empty: "还没有导出的切片" },
      { key: "library", label: "素材库源视频", empty: "素材库里还没有源视频" }
    ];
    var total = 0;
    groups.forEach(function (g) {
      var rows = state.sources[g.key] || [];
      total += rows.length;
      els.sourceList.appendChild(
        ui.h("div", { class: "merge-group" }, [ui.h("span", { class: "merge-group__label", text: g.label })])
      );
      if (!rows.length) {
        els.sourceList.appendChild(ui.h("p", { class: "merge-empty", text: g.empty }));
        return;
      }
      rows.forEach(function (row) {
        var picked = state.order.some(function (r) { return r.path === row.path; });
        els.sourceList.appendChild(
          ui.h("button", {
            class: "merge-src" + (picked ? " is-picked" : ""),
            type: "button",
            title: row.path,
            "data-origin": row.origin || "",
            onclick: function () { addSource(row); }
          }, [
            ui.h("span", { class: "merge-src__name", text: row.name }),
            ui.h("span", { class: "merge-src__meta" }, [
              ui.h("span", { text: fmtMs(row.duration_ms) }),
              ui.h("span", { text: fmtBytes(row.bytes) }),
              row.exists === false ? ui.h("span", { class: "pill pill--warn", text: "文件不在了" }) : null
            ])
          ])
        );
      });
    });
    if (els.sourceCount) els.sourceCount.textContent = total + " 个来源";
  }

  function paintOrder() {
    if (!els.orderList) return;
    els.orderList.textContent = "";
    if (!state.order.length) {
      els.orderList.appendChild(
        ui.empty("列表为空", "从左边加片段，或用「添加本地文件」。顺序就是成片顺序。")
      );
    }
    var acc = 0;
    state.order.forEach(function (row, index) {
      var item = (state.preflight && state.preflight.items && state.preflight.items[index]) || {};
      if (item.path !== row.path) item = {};
      acc += Number(item.duration_ms || row.duration_ms || 0);
      var spec = item.width
        ? item.width + "×" + item.height + " · " + (item.fps || 0) + "fps · " + (item.vcodec || "—")
        : "还没探测";
      els.orderList.appendChild(
        ui.h("div", { class: "merge-row" + (item.ok === false ? " is-bad" : "") }, [
          ui.h("span", { class: "merge-row__no", text: String(index + 1) }),
          ui.h("div", { class: "merge-row__body" }, [
            ui.h("span", { class: "merge-row__name", text: row.name, title: row.path }),
            ui.h("span", { class: "merge-row__meta" }, [
              ui.h("span", { text: fmtMs(item.duration_ms || row.duration_ms) }),
              ui.h("span", { text: spec }),
              ui.h("span", { text: "累计 " + fmtMs(acc) }),
              item.ok === false
                ? ui.h("span", { class: "pill pill--warn", text: item.error || "读不出视频流" })
                : null
            ])
          ]),
          ui.h("div", { class: "merge-row__acts" }, [
            btn("↑", "上移", function () { moveAt(index, -1); }, index === 0),
            btn("↓", "下移", function () { moveAt(index, 1); }, index === state.order.length - 1),
            btn("删", "删除这一段", function () { removeAt(index); }, false, "btn--danger")
          ])
        ])
      );
    });
    if (els.orderCount) els.orderCount.textContent = state.order.length + " 段 · " + fmtMs(acc);
    if (els.orderActs) {
      els.orderActs.forEach(function (node) {
        node.disabled = !state.order.length;
      });
    }
  }

  function btn(text, title, onclick, disabled, extra) {
    return ui.h("button", {
      class: "btn btn--tiny" + (extra ? " " + extra : ""),
      type: "button",
      text: text,
      title: title,
      disabled: !!disabled,
      onclick: onclick
    });
  }

  function paintPreflight() {
    if (!els.table) return;
    els.table.textContent = "";
    var pf = state.preflight;
    if (!pf) {
      els.table.appendChild(
        ui.h("p", { class: "merge-empty", text: state.order.length ? "正在预检…" : "加片段后自动预检" })
      );
      return;
    }
    var rows = [
      [
        "文件数",
        (pf.count || 0) + " 个可用" + (pf.missing && pf.missing.length ? " · " + pf.missing.length + " 个不在" : "")
      ],
      ["总时长", pf.total_text || "—"],
      ["总素材体积", pf.total_bytes_text || "—"],
      // 规格一致时这条是"所有文件共同的规格"；不一致时它只是精确重编码的目标值，
      // 继续叫"统一规格"就是在说它们已经统一了。
      [
        pf.consistent ? "统一规格" : "目标规格",
        pf.target ? pf.target.width + "×" + pf.target.height + " · " + pf.target.fps + "fps" : "—"
      ],
      ["能否快速拼接", pf.can_quick ? "可以：参数一致，只封装不重编码" : "不行：需精确重编码"]
    ];
    var kv = ui.h("div", { class: "merge-kv" });
    rows.forEach(function (pair) {
      kv.appendChild(ui.h("span", { class: "merge-kv__k", text: pair[0] }));
      kv.appendChild(ui.h("span", { class: "merge-kv__v", text: pair[1] }));
    });
    els.table.appendChild(kv);

    if (pf.differences && pf.differences.length) {
      var diff = ui.h("div", { class: "merge-diff" });
      diff.appendChild(ui.h("div", { class: "merge-diff__head", text: "规格差异" }));
      pf.differences.forEach(function (d) {
        diff.appendChild(
          ui.h("div", { class: "merge-diff__row" }, [
            ui.h("span", { class: "merge-diff__label", text: d.label }),
            ui.h("span", {
              class: "merge-diff__vals",
              text: (d.values || []).map(function (v) { return v.name + " " + v.value; }).join(" · ")
            })
          ])
        );
      });
      els.table.appendChild(diff);
    }

    if (pf.warnings && pf.warnings.length) {
      els.table.appendChild(
        ui.h("div", { class: "callout callout--warn" }, [ui.h("p", { text: pf.warnings.join("；") })])
      );
    }

    if (pf.estimate) {
      var est = pf.estimate[state.mode] || pf.estimate.precise;
      els.table.appendChild(
        ui.h("p", {
          class: "field__help",
          text: "预计 " + est.size_text + " · " + est.seconds_text + "（估算，用于判断要不要等）"
        })
      );
    }
  }

  // ------------------------------------------------------------ 输出目录

  function pickOutDir() {
    if (state.busy || state.running) return;
    state.busy = true;
    state.error = "";
    paintAll();
    WB.rpc("fs.pick", { kind: "dir" }, { silent: true })
      .then(function (data) {
        state.busy = false;
        var picked = data && data.path ? String(data.path).trim() : "";
        // 取消选择（path 为空）不改原值：用户点了取消不等于要清空当前目录。
        if (picked) {
          state.outDir = picked;
          state.outDirManual = true;
          state.note = "";
        }
        paintAll();
      })
      .catch(function (err) {
        state.busy = false;
        state.error = (err && err.message) || "打不开文件夹选择框";
        paintAll();
      });
  }

  function openOutputDir() {
    if (!state.lastMergeId || state.busy || state.running || state.detached) return;
    state.error = "";
    WB.rpc("merge.reveal_output", { merge_id: state.lastMergeId }, { silent: true })
      .then(function () {
        paintAll();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "打不开输出目录";
        paintAll();
      });
  }

  // ------------------------------------------------------------ 开始合并

  function collect() {
    return {
      paths: paths(),
      mode: state.mode === "quick" ? "quick" : "precise",
      // 没手选就传空，让后端按「第一个可用源文件父目录下的 merge/」算。
      // 把默认值当显式路径传回去，会让后端去校验一个它自己给的目录。
      out_dir: state.outDirManual ? state.outDir || "" : "",
      out_name: (els.outName && els.outName.value || "").trim(),
      width: Number(state.width) || 0,
      height: Number(state.height) || 0,
      fps: Number(state.fps) || 0
    };
  }

  function paintRunning() {
    if (!els.start) return;
    els.start.textContent = state.detached ? "后台合并中…" : state.running ? "合并中…" : "开始合并";
    if (els.note && state.running) {
      els.note.textContent = "正在拼接：" + Math.round((state.progress || 0) * 100) + "%";
      els.note.style.display = "";
    }
  }

  function startMerge() {
    if (state.running || state.busy || state.detached) return;
    var pf = state.preflight || {};
    if ((pf.missing && pf.missing.length) || (pf.broken && pf.broken.length)) {
      state.error = "先把标红的文件移出列表再开始合并";
      paintAll();
      return;
    }
    var body = collect();
    if (!body.paths.length || body.paths.length < 2) {
      state.note = "先加两个以上可用片段";
      paintAll();
      return;
    }
    state.error = "";
    state.note = "";
    state.running = true;
    state.detached = false;
    state.jobId = "";
    state.progress = 0;
    settledJobId = "";
    paintAll();
    paintRunning();
    WB.rpc("merge.start", body, { silent: true }).then(function (data) {
      var job = data.job || {};
      if (!job.id) {
        state.running = false;
        state.error = "任务没有返回编号";
        paintAll();
        paintRunning();
        return;
      }
      state.jobId = job.id;
      watchJob(job.id);
      pollMerge(job.id);
    }).catch(function (err) {
      unwatchJob();
      state.running = false;
      state.jobId = "";
      state.error = err.message || "合并没能开始";
      // 规格不一致这类错误会带结构化差异，交给预检表去说
      if (err.detail) {
        try {
          var diffs = JSON.parse(err.detail);
          if (diffs && diffs.length) state.preflight = Object.assign({}, state.preflight, { differences: diffs, can_quick: false });
        } catch (e) { /* detail 不是 JSON 就不管 */ }
      }
      paintAll();
      paintRunning();
    });
  }

  // 轮询节奏与"什么时候放手"抽成纯函数：这两个阈值曾是现场 bug（12 分钟就停，
  // 用户以为失败，实际后台还在跑 20 分钟）。测试直接断言这两个函数。
  function pollDelay(attempts) { return attempts > POLL_LIMIT ? SLOW_MS : POLL_MS; }
  function pollGaveUp(attempts) { return attempts > HARD_LIMIT; }

  var settledJobId = "";
  var jobOff = null;

  function settleJob(jobId, status, result, errorMessage) {
    if (settledJobId === jobId) return;
    settledJobId = jobId;
    state.running = false;
    state.detached = false;
    state.jobId = "";
    if (status === "succeeded") {
      var payload = result || {};
      state.progress = 1;
      state.note = "合并完成：" + (payload.name || "") + "（" + (payload.count || 0) + " 段）";
      // 快速拼接失败后自动重编码：不说出来，用户只会觉得"慢了一点"，
      // 回头查为什么画面有轻微缩放也找不到线索。
      if (payload.fallback_used) {
        state.note += " · 快速拼接失败，已切精确重编码";
      }
      // 打开输出目录只认这条记录的 merge_id：页面自己拼路径会绕开
      // 「按库里 output_path 打开」的授权口径。
      state.lastMergeId = payload.merge_id || "";
      state.lastOutput = payload.path || "";
      if (payload.path) WB.revealLast = payload.path;
    } else {
      state.error = errorMessage || (status === "canceled" ? "已取消" : "合并失败");
    }
    paintAll();
    paintRunning();
  }

  // 事件是轮询的兜底：任务跑完那一刻即使轮询正好断了，也能把页面收尾。
  // 事件可能丢（任务中心那边有同样问题），所以不能只靠它。
  function watchJob(jobId) {
    if (jobOff) { jobOff(); jobOff = null; }
    jobOff = WB.on("job.any", function (payload) {
      if (!payload || String(payload.id || "") !== String(jobId)) return;
      if (payload.status === "succeeded") {
        settleJob(jobId, "succeeded", payload.result || (payload.job && payload.job.result) || {}, "");
      } else if (payload.status === "failed" || payload.status === "canceled") {
        var msg = (payload.job && payload.job.error_message) ||
          payload.error_message || (payload.error && payload.error.message) || "";
        settleJob(jobId, payload.status, null, msg);
      }
    });
  }

  function unwatchJob() {
    if (jobOff) { jobOff(); jobOff = null; }
  }

  function pollMerge(jobId) {
    var tries = 0;
    function tick() {
      tries += 1;
      WB.rpc("job.get", { id: jobId }, { silent: true }).then(function (data) {
        if (state.jobId !== jobId) return;
        var job = (data && data.job) || data || {};
        state.progress = Number(job.progress) || 0;
        if (job.status === "succeeded") {
          settleJob(jobId, "succeeded", job.result || {}, "");
          return;
        }
        if (job.status === "failed" || job.status === "canceled") {
          settleJob(jobId, job.status, null, job.error_message || (job.status === "canceled" ? "已取消" : "合并失败"));
          return;
        }
        paintRunning();
        if (pollGaveUp(tries)) {
          // 到硬上限不再密集问，但**不当成失败**：任务可能还在后台跑。
          // 页面保持可恢复：job.finished 事件到了照样能把结果写回来。
          state.detached = true;
          state.running = false;
          state.note = "任务还在后台跑（去任务中心看进度），这里不再等";
          paintAll();
          paintRunning();
          return;
        }
        setTimeout(tick, pollDelay(tries));
      }).catch(function (err) {
        if (state.jobId !== jobId) return;
        // 单次查询失败不判死：本地 RPC 偶尔会撞上主线程正忙，连续三次才报。
        if (tries < 3) { setTimeout(tick, POLL_MS); return; }
        state.running = false;
        state.detached = false;
        state.jobId = "";
        state.error = (err && err.message) || "查不到任务状态";
        paintAll();
        paintRunning();
      });
    }
    tick();
  }

  function paintOutput() {
    if (!els.modeNote) return;
    var canQuick = !!(state.preflight && state.preflight.can_quick);
    els.modeQuick.disabled = !canQuick;
    els.modeNote.textContent = canQuick
      ? "参数一致，快速拼接只封装不重编码"
      : "规格不一致或文件不够，快速拼接不可用";
    els.modeQuick.checked = state.mode === "quick";
    els.modePrecise.checked = state.mode !== "quick";
    if (els.outDir) {
      // 路径本身就是按钮：没有可用路径时给一句能点的说明，而不是留个空框
      var shown = state.outDir || "选择输出目录";
      if (els.outDir.textContent !== shown) els.outDir.textContent = shown;
      var tip = state.outDir || "点击选择输出目录";
      if (els.outDir.title !== tip) els.outDir.title = tip;
    }
    if (els.openDir) {
      var canOpen = !!state.lastMergeId && !state.running && !state.busy && !state.detached;
      els.openDir.disabled = !canOpen;
      els.openDir.title = canOpen
        ? "在资源管理器里打开这次的成片目录"
        : state.detached ? "后台合并完成后可打开" : state.running ? "合并完成后可打开" : "先合并一次";
    }
    if (els.outName) els.outName.value = state.outName;
    if (els.start) {
      var pf = state.preflight;
      var hasUnusable = !!(pf && ((pf.missing && pf.missing.length) || (pf.broken && pf.broken.length)));
      var ready =
        !!state.order.length && !state.busy && !state.running && !state.detached &&
        !!pf && pf.count >= 2 && !hasUnusable;
      els.start.disabled = !ready;
      els.start.title = state.detached
        ? "上一个合并还在后台跑，先别开新的"
        : state.running
          ? "正在合并，请稍候"
          : hasUnusable ? "先把标红的文件移出列表"
          : ready ? "开始合并" : "先加两个以上可用片段";
      var startText = state.detached ? "后台合并中…" : state.running ? "合并中…" : "开始合并";
      if (els.start.textContent !== startText) {
        els.start.textContent = startText;
      }
    }
  }

  function paintAll() {
    paintSources();
    paintOrder();
    paintPreflight();
    paintOutput();
    if (els.error) {
      els.error.textContent = state.error || "";
      els.error.style.display = state.error ? "" : "none";
    }
    if (els.note) {
      els.note.textContent = state.note || "";
      els.note.style.display = state.note ? "" : "none";
    }
  }

  // ------------------------------------------------------------ 骨架

  function setMode(mode) {
    state.mode = mode;
    // 手动选过就记住：预检再跑时不要替用户改回来自动判断的模式
    state.modeManual = true;
    state.note = "";
    paintAll();
  }

  function selectSpec(field, choices, label) {
    var node = ui.h("select", { class: "select" });
    choices.forEach(function (value) {
      node.appendChild(ui.h("option", { value: String(value), text: label(value) }));
    });
    node.addEventListener("change", function () {
      var raw = Number(node.value) || 0;
      if (field === "fps") {
        state.fps = raw;
      } else {
        // 只给宽度，高度按 16:9 推——竖屏合并不在本阶段范围
        state.width = raw;
        state.height = raw ? Math.round((raw * 9) / 16 / 2) * 2 : 0;
      }
      refreshPreflight();
    });
    return node;
  }

  function build() {
    els.root = ui.h("section", { class: "page merge" });

    var head = ui.h("div", { class: "page__head" }, [
      ui.h("h2", { class: "page__title", text: "合并" }),
      ui.h("p", { class: "page__hint", text: "把多个片段按顺序拼成一条成品" }),
      ui.h("div", { class: "page__actions" }, [
        ui.h("button", {
          class: "btn",
          type: "button",
          text: "添加本地文件",
          title: "一次可以选多个视频",
          onclick: addLocal
        }),
        (els.start = ui.h("button", {
          class: "btn btn--primary",
          type: "button",
          text: "开始合并",
          disabled: true,
          title: "先加两个以上可用片段",
          onclick: startMerge
        }))
      ])
    ]);

    els.error = ui.h("p", { class: "merge-flash merge-flash--error", text: "" });
    els.note = ui.h("p", { class: "merge-flash", text: "" });

    els.sourceCount = ui.h("span", { class: "card__aside", text: "0 个来源" });
    var search = ui.h("input", {
      class: "input",
      type: "search",
      placeholder: "搜文件名或项目名",
      oninput: function (event) {
        state.query = event.target.value || "";
        clearTimeout(search._t);
        search._t = setTimeout(loadSources, 260);
      }
    });
    els.sourceList = ui.h("div", { class: "merge-srcs" });
    var left = ui.h("div", { class: "card merge-card" }, [
      ui.h("div", { class: "card__head" }, [ui.h("h3", { class: "card__title", text: "来源" }), els.sourceCount]),
      ui.h("div", { class: "card__body merge-pane" }, [search, els.sourceList])
    ]);

    els.orderCount = ui.h("span", { class: "card__aside", text: "0 段" });
    els.orderList = ui.h("div", { class: "merge-rows" });
    els.orderActs = [
      ui.h("button", { class: "btn btn--tiny", type: "button", text: "反转", disabled: true, onclick: reverse }),
      ui.h("button", { class: "btn btn--tiny", type: "button", text: "清空", disabled: true, onclick: clearAll })
    ];
    var middle = ui.h("div", { class: "card merge-card" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title", text: "合并顺序" }),
        els.orderCount,
        ui.h("div", { class: "merge-head-acts" }, els.orderActs)
      ]),
      ui.h("div", { class: "card__body merge-pane" }, [els.orderList])
    ]);

    els.modeQuick = ui.h("input", { type: "radio", name: "merge-mode", class: "merge-radio" });
    els.modePrecise = ui.h("input", { type: "radio", name: "merge-mode", class: "merge-radio" });
    els.modeQuick.addEventListener("change", function () { setMode("quick"); });
    els.modePrecise.addEventListener("change", function () { setMode("precise"); });
    els.modeNote = ui.h("p", { class: "field__help", text: "" });
    // 路径本身是按钮：点它换目录。用 button 而不是 readonly input ——
    // 只读输入框看上去像能改、点了没反应，是这一栏之前最直接的坑。
    els.outDir = ui.h("button", {
      class: "btn btn--ghost merge-outdir-path",
      type: "button",
      text: "选择输出目录",
      title: "点击选择输出目录",
      onclick: pickOutDir
    });
    els.openDir = ui.h("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "打开输出目录",
      disabled: true,
      title: "先合并一次",
      onclick: openOutputDir
    });
    els.outName = ui.h("input", {
      class: "input mono",
      oninput: function (event) {
        state.outName = event.target.value;
        state.outNameManual = true;
      }
    });
    var right = ui.h("div", { class: "card merge-card" }, [
      ui.h("div", { class: "card__head" }, [ui.h("h3", { class: "card__title", text: "输出" })]),
      ui.h("div", { class: "card__body merge-pane merge-form" }, [
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "拼接模式" }),
          ui.h("label", { class: "merge-opt" }, [els.modeQuick, ui.h("span", { text: "快速拼接（copy）" })]),
          ui.h("label", { class: "merge-opt" }, [els.modePrecise, ui.h("span", { text: "精确重编码" })]),
          els.modeNote
        ]),
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "输出目录" }),
          ui.h("div", { class: "merge-outdir-row" }, [els.outDir, els.openDir])
        ]),
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "文件名" }),
          els.outName
        ]),
        ui.h("div", { class: "grid-2" }, [
          ui.h("div", { class: "field" }, [
            ui.h("span", { class: "field__label", text: "分辨率" }),
            selectSpec("width", [0, 1920, 1280, 1080, 720], function (v) {
              return v ? v + "p 宽" : "跟随第一个";
            })
          ]),
          ui.h("div", { class: "field" }, [
            ui.h("span", { class: "field__label", text: "帧率" }),
            selectSpec("fps", [0, 24, 25, 30, 50, 60], function (v) {
              return v ? v + " fps" : "跟随第一个";
            })
          ])
        ])
      ])
    ]);

    els.table = ui.h("div", { class: "merge-table" });
    var bottom = ui.h("div", { class: "card merge-card merge-card--wide" }, [
      ui.h("div", { class: "card__head" }, [ui.h("h3", { class: "card__title", text: "预检" })]),
      ui.h("div", { class: "card__body" }, [els.table])
    ]);

    els.root.appendChild(head);
    els.root.appendChild(els.error);
    els.root.appendChild(els.note);
    els.root.appendChild(ui.h("div", { class: "merge-grid" }, [left, middle, right]));
    els.root.appendChild(bottom);
    return els.root;
  }

  function render() {
    build();
    paintAll();
    loadSources();
    return els.root;
  }

  // 给前端测试用的纯函数（轮询节奏 / 何时放手）
  WB._mergePoll = {
    delay: pollDelay,
    gaveUp: pollGaveUp,
    limit: POLL_LIMIT,
    hard: HARD_LIMIT,
    slow: SLOW_MS
  };

  WB.pages = WB.pages || {};
  WB.pages.merge = {
    render: render,
    _internals: {
      state: state,
      watchJob: watchJob,
      unwatchJob: unwatchJob,
      settleJob: settleJob,
      addSource: addSource,
      moveAt: moveAt,
      removeAt: removeAt,
      reverse: reverse,
      clearAll: clearAll,
      refreshPreflight: refreshPreflight,
      startMerge: startMerge,
      pickOutDir: pickOutDir,
      openOutputDir: openOutputDir,
      paths: paths
    }
  };

  WB.router.define("merge", {
    title: "合并",
    crumb: "拼接与成片",
    icon: "merge",
    nav: true,
    render: render,
    // 离开页面必须解绑：job.any 的订阅挂在全局事件总线上，不解绑就一直收
    // 这个任务的事件、继续改已经不在屏上的 DOM。轮询那条腿靠 state.jobId
    // 比对自保，但事件腿没有这层保护。
    destroy: function () {
      unwatchJob();
      state.running = false;
      state.detached = false;
      state.jobId = "";
    }
  });
})();
