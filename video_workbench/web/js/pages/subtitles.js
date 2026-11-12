/* ============================================================================
 * pages/subtitles.js —— 字幕编辑器（P2-03）
 *
 * **三条设计约束，都是被真实使用逼出来的：**
 *
 * 1. **1000 段必须不卡。** 所以列表是虚拟滚动：只渲染视口里的十几行，滚动时
 *    换数据不换 DOM 数量。编辑一句不该重排整张表。
 * 2. **编辑要走服务端。** 分割/合并/平移/替换的语义只在 `core/subtitles.py`
 *    里实现一遍，前端只发"意图"。这样导出、切片、总结拿到的和界面看到的是同一份。
 * 3. **改动必须能撤。** 撤销栈存的是"操作前的整份段表"快照——1000 段一份 JSON
 *    约 80KB，存 30 份也就 2.4MB，比实现一套逆操作安全得多。
 *
 * 键盘（焦点在列表内）：Space 播放 / S 在播放头分割 / M 合并选中 / Del 删除 /
 * Enter 编辑 / Ctrl+Z 撤销 / Ctrl+Shift+Z 重做 / Ctrl+F 批量替换 / Alt+↑↓ 上下一段。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;
  var h = ui.h;

  var ROW_HEIGHT = 38;      // 与 CSS .sub-row 高度一致
  var OVERSCAN = 6;
  var AUTOSAVE_MS = 700;
  var UNDO_LIMIT = 40;

  function call(method, params) {
    return WB.rpc(method, params || {}, { silent: true });
  }

  function ms(value) {
    var total = Math.max(0, Math.round(Number(value) || 0));
    var minutes = Math.floor(total / 60000);
    var seconds = Math.floor((total % 60000) / 1000);
    var rest = total % 1000;
    return minutes + ":" + (seconds < 10 ? "0" : "") + seconds + "." + String(rest).padStart(3, "0");
  }

  function shortMs(value) {
    var total = Math.max(0, Math.round(Number(value) || 0));
    var minutes = Math.floor(total / 60000);
    var seconds = Math.floor((total % 60000) / 1000);
    return minutes + ":" + (seconds < 10 ? "0" : "") + seconds;
  }

  var state = {
    projectId: "",
    project: null,
    segments: [],
    qc: {},             // segment_id -> [{kind, level, message}]
    issues: 0,
    stats: null,
    selected: {},       // id -> true
    activeId: "",
    editingId: "",
    undo: [],
    redo: [],
    dirty: false,
    saving: false,
    savedAt: "",
    scrollTop: 0,
    follow: true,
    loading: false,
    error: "",
    pending: 0,
    optJob: "",         // 正在跑的优化任务
    opt: null,          // {changes, failed, model, segments, accepted:{id:true}}
    optBusy: false,
    transJob: "",       // 正在跑的翻译任务
    trans: null,        // {target, layout, segments, accepted:{id:true}, overrides:{}, failed:[]}
    transBusy: false,
    // 项目条（左栏入口化之后字幕页自带项目选择；列表数据跨项目共用）
    projects: [],
    projectTotal: 0,
    projectQuery: "",
    projectLoading: false,
    // 请求代次：load/peaks/save 各一条，回调只认最新一次（规划 §4.6）
    loadSeq: 0,
    peaksSeq: 0,
    saveSeq: 0
  };

  var nodes = {};       // 复用 DOM 引用
  var wave = null;
  var video = null;
  var saveTimer = 0;
  var rafPending = false;
  // 字幕页是一级入口后，只有"当前这一次进入"的异步回调才允许写界面。
  // pageToken 每次 render()/destroy() 自增：旧页面挂着的请求回来时，
  // 页面可能已经换了项目甚至换了路由，一律丢弃（规划 §4.6）。
  var pageToken = 0;
  var resizeHandler = null;
  var videoProjectId = "";
  var saveChain = Promise.resolve();
  var notice = null;    // {kind, text}：空态/错误卡片（带行动按钮的那类提示）

  // ------------------------------------------------------------ 数据

  function indexQc(list) {
    var map = {};
    var issues = 0;
    (list || []).forEach(function (item) {
      var ids = item.segment_ids || (item.segment_id ? [item.segment_id] : []);
      ids.forEach(function (id) {
        (map[id] = map[id] || []).push(item);
      });
      issues += 1;
    });
    return { map: map, issues: issues };
  }

  function applyPayload(data) {
    state.segments = data.segments || [];
    var qc = indexQc(data.qc || []);
    state.qc = qc.map;
    state.issues = qc.issues;
    state.stats = data.stats || null;
    // 选中的段可能已经被合并/删除，清掉失效的
    var alive = {};
    state.segments.forEach(function (seg) {
      alive[seg.id] = true;
    });
    Object.keys(state.selected).forEach(function (id) {
      if (!alive[id]) delete state.selected[id];
    });
    if (state.activeId && !alive[state.activeId]) state.activeId = "";
  }

  function snapshot() {
    return { segments: state.segments.map(function (seg) { return Object.assign({}, seg); }) };
  }

  function pushUndo() {
    state.undo.push(snapshot());
    if (state.undo.length > UNDO_LIMIT) state.undo.shift();
    state.redo.length = 0;
    updateToolbar();
  }

  function load(token) {
    if (!state.projectId) return;
    var pid = state.projectId;
    var seq = ++state.loadSeq;
    state.loading = true;
    state.error = "";
    showNotice(null);
    paint();
    Promise.all([
      call("subtitle.get", { project_id: pid }),
      call("library.get", { id: pid })
    ])
      .then(function (results) {
        // 旧请求守护：页已销毁 / 已切项目 / 更新的一轮已发出，都不写（规划 §4.6）
        if (!samePage(token) || seq !== state.loadSeq || pid !== state.projectId) return;
        var data = results[0] || {};
        applyPayload(data);
        state.project = (results[1] && results[1].project) || state.project;
        state.loading = false;
        state.error = "";
        // 「没有字幕」是**空态**，不是错误：以前它被塞进 state.error，而这个字段
        // 在 paint() 里根本没有读者——于是用户看到的是一条空的段落列表，
        // 屏幕上没有任何一句话解释为什么是空的。空态走 notice 容器，带出口。
        if (data.has_transcript === false) {
          showNotice({
            kind: "no-transcript",
            text: "这条视频还没有字幕。转录之后才能在这里改字、质检、导出。"
          });
        }
        paint();
        mountMedia();
        loadPeaks(pid, token);
        if (state.segments.length && !state.activeId) {
          setActive(state.segments[0].id, false);
          renderRows(true);
        }
      })
      .catch(function (err) {
        if (!samePage(token) || seq !== state.loadSeq || pid !== state.projectId) return;
        state.loading = false;
        if (err && err.code === "NOT_FOUND") {
          // URL 里带来的项目已经不在库里：给一条可行动的错误，不静默换项目
          showNotice({ kind: "project-missing", text: "项目不存在或已删除" });
        } else {
          state.error = (err && err.message) || String(err);
          // 读取失败也必须有一条看得见、点得动的出路：重试就是重发这一次 load。
          showNotice({
            kind: "load-failed",
            text: "读不到这条视频的字幕：" + state.error
          });
        }
        paint();
      });
  }

  function loadPeaks(pid, token) {
    if (!wave) return;
    var seq = ++state.peaksSeq;
    var target = pid || state.projectId;
    call("audio.peaks", { project_id: target })
      .then(function (data) {
        if (!samePage(token) || target !== state.projectId || seq !== state.peaksSeq) return;
        wave.setPeaks(data.peaks || [], data.duration_ms || 0);
        wave.setSegments(state.segments, data.duration_ms || 0);
      })
      .catch(function () {
        if (!samePage(token) || target !== state.projectId || seq !== state.peaksSeq) return;
        wave.setSegments(state.segments, 0);
      });
  }

  // ------------------------------------------------------------ 保存

  function markDirty() {
    state.dirty = true;
    updateStatus();
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(save, AUTOSAVE_MS);
  }

  function save() {
    if (!state.dirty) return;
    var pid = state.projectId;
    var token = pageToken;
    var seq = ++state.saveSeq;
    var payload = { project_id: pid, segments: snapshot().segments };
    state.saving = true;
    state.dirty = false;
    updateStatus();
    // 保存串行：同一时刻最多一个 subtitle.save 在飞，旧快照不许后到覆盖新快照（规划 §5.2.5）。
    // 响应回来时已切项目/已销毁：服务端写成功了就不动 UI；失败则 toast，用户在别的页面也要知道。
    state.savePromise = saveChain.then(function () {
      return call("subtitle.save", payload);
    });
    state.savePromise
      .then(function (data) {
        if (pid !== state.projectId || seq !== state.saveSeq || !samePage(token)) return data;
        state.saving = false;
        state.savedAt = new Date().toLocaleTimeString();
        var qc = indexQc(data.qc || []);
        state.qc = qc.map;
        state.issues = qc.issues;
        state.stats = data.stats || state.stats;
        state.error = "";
        updateStatus();
        renderRows(true);
        return data;
      })
      .catch(function (err) {
        if (pid !== state.projectId || !samePage(token)) {
          ui.toast("字幕保存失败：" + ((err && err.message) || err), "error");
          return undefined;
        }
        state.saving = false;
        state.dirty = true;
        state.error = "保存失败：" + ((err && err.message) || err);
        updateStatus();
        return undefined;
      });
    saveChain = state.savePromise.then(function () {}, function () {});
  }

  /**
   * 把当前草稿落盘再返回。切换项目 / 离开页面之前调用：
   * 有在飞的保存先等它结束；等完仍有 dirty 就立即补一次保存；
   * 保存失败（dirty 仍为真）reject，调用方负责阻断切换，绝不静默丢草稿。
   */
  function flushDraft() {
    function waitInflight() {
      var flight = state.savePromise;
      if (!flight) return Promise.resolve();
      return flight.catch(function () { /* 失败由下面的 dirty 分支接住 */ });
    }
    return waitInflight().then(function () {
      if (!state.dirty) return undefined;
      save();
      return waitInflight().then(function () {
        if (state.dirty) {
          throw new Error(String(state.error || "").replace(/^保存失败：/, "") || "字幕还没保存成功");
        }
        return undefined;
      });
    });
  }

  /** 走服务端语义操作。成功后整体替换段表与质检。 */
  function runOp(params, options) {
    var opts = options || {};
    if (!opts.skipUndo) pushUndo();
    state.pending += 1;
    updateStatus();
    var pid = state.projectId;
    var token = pageToken;
    return call("subtitle.op", Object.assign({ project_id: pid }, params))
      .then(function (data) {
        state.pending -= 1;
        // 已切项目/已销毁：数据交还调用方，但不再写当前页面（规划 §5.2.6）
        if (pid !== state.projectId || !samePage(token)) return data;
        applyPayload(data);
        if (opts.select) {
          state.selected = {};
          opts.select(state.segments);
        }
        if (opts.active) state.activeId = opts.active(state.segments) || state.activeId;
        state.error = "";
        markDirty();
        renderRows(true);
        wave && wave.setSegments(state.segments, 0);
        updateToolbar();
        if (typeof opts.toast === "string") ui.toast(opts.toast, "success");
        return data;
      })
      .catch(function (err) {
        state.pending -= 1;
        ui.toast("操作失败：" + ((err && err.message) || err), "error");
        if (pid === state.projectId && samePage(token)) updateStatus();
        throw err;
      });
  }

  // ------------------------------------------------------------ 选择与播放头

  function selectedIds() {
    return Object.keys(state.selected);
  }

  function setActive(id, seek) {
    state.activeId = id || "";
    renderRows(true);
    var seg = byId(state.activeId);
    if (seg) {
      if (wave) wave.setActive(seg.id);
      if (seek !== false) seekTo(seg.start_ms);
    }
  }

  function byId(id) {
    for (var i = 0; i < state.segments.length; i++) {
      if (state.segments[i].id === id) return state.segments[i];
    }
    return null;
  }

  function indexOfId(id) {
    for (var i = 0; i < state.segments.length; i++) {
      if (state.segments[i].id === id) return i;
    }
    return -1;
  }

  function seekTo(msValue) {
    if (video && isFinite(msValue)) {
      video.currentTime = Math.max(0, msValue / 1000);
    }
    if (wave) wave.setTime(Number(msValue) || 0);
    syncActiveToTime(Number(msValue) || 0);
  }

  function currentTimeMs() {
    return video ? Math.round((video.currentTime || 0) * 1000) : 0;
  }

  function syncActiveToTime(timeMs) {
    var found = "";
    for (var i = 0; i < state.segments.length; i++) {
      var seg = state.segments[i];
      if (timeMs >= seg.start_ms && timeMs < seg.end_ms) {
        found = seg.id;
        break;
      }
    }
    if (found && found !== state.activeId) {
      state.activeId = found;
      if (wave) wave.setActive(found);
      renderRows(true);
      if (state.follow) scrollToId(found);
    }
  }

  function scrollToId(id) {
    var index = indexOfId(id);
    if (index < 0 || !nodes.listBody) return;
    var top = index * ROW_HEIGHT;
    var view = nodes.listScroll;
    if (!view) return;
    if (top < view.scrollTop || top + ROW_HEIGHT > view.scrollTop + view.clientHeight) {
      view.scrollTop = Math.max(0, top - view.clientHeight / 2);
    }
  }

  // ------------------------------------------------ 项目条与页面生命周期（一级入口改造）

  function samePage(token) {
    return token === pageToken;
  }

  /** 项目级状态整体重置；projects/projectTotal/projectQuery 是列表数据，跨项目共用。 */
  function resetForProject() {
    state.project = null;
    state.segments = [];
    state.qc = {};
    state.issues = 0;
    state.stats = null;
    state.selected = {};
    state.activeId = "";
    state.editingId = "";
    state.undo = [];
    state.redo = [];
    state.dirty = false;
    state.saving = false;
    state.savedAt = "";
    state.scrollTop = 0;
    state.follow = true;
    state.loading = false;
    state.error = "";
    state.pending = 0;
    state.optJob = "";
    state.opt = null;
    state.optBusy = false;
    state.transJob = "";
    state.trans = null;
    state.transBusy = false;
    state.expJob = "";
    state.expBusy = false;
    state.expResult = null;
    state.transLayout = "";
  }

  /** 播放器与波形随页面走：离开/重进/换项目都重建，不允许出现"没有播放器"（规划 §4.7）。 */
  function disposeMedia() {
    if (video) {
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch (err) {
        /* 元素已分离时这些调用可能抛，清引用即可 */
      }
    }
    video = null;
    videoProjectId = "";
    wave = null;
  }

  function detachResize() {
    if (resizeHandler) {
      window.removeEventListener("resize", resizeHandler);
      resizeHandler = null;
    }
  }

  function showNotice(next) {
    notice = next || null;
  }

  /** 空态/错误卡片：带行动按钮的三种提示；普通错误走 state.error 的纯文本分支。 */
  function paintNotice() {
    var host = document.getElementById("sub-notice");
    if (!host) return;
    host.textContent = "";
    var grid = host.parentElement ? host.parentElement.querySelector(".sub-grid") : null;
    if (!notice) {
      if (grid) grid.style.display = "";
      return;
    }
    if (grid && notice.kind === "no-projects") grid.style.display = "none";
    var card = h("div", { class: "callout callout--warn" }, [h("span", { text: notice.text })]);
    if (notice.kind === "no-projects") {
      card.appendChild(h("button", {
        class: "btn btn--ghost", type: "button", text: "去素材库",
        onclick: function () { WB.router.go("library"); }
      }));
    } else if (notice.kind === "projects-failed") {
      card.appendChild(h("button", {
        class: "btn btn--ghost", type: "button", text: "重试",
        onclick: function () { showNotice(null); init(pageToken); }
      }));
    } else if (notice.kind === "project-missing") {
      card.appendChild(h("button", {
        class: "btn btn--ghost", type: "button", text: "回素材库",
        onclick: function () { WB.router.go("library"); }
      }));
    } else if (notice.kind === "no-transcript") {
      card.appendChild(h("button", {
        class: "btn btn--ghost", type: "button", text: "去转录",
        onclick: function () { WB.router.go("library", { project: state.projectId }); }
      }));
    } else if (notice.kind === "load-failed") {
      card.appendChild(h("button", {
        class: "btn btn--ghost", type: "button", text: "重试",
        onclick: function () { showNotice(null); load(pageToken); }
      }));
    }
    host.appendChild(card);
  }

  function projectLabel(item) {
    var label = (item && item.title) || (item && item.id) || "";
    if (item && item.has_transcript === false) label += "（无字幕）";
    return label;
  }

  function paintProjectBar() {
    var selectEl = nodes.projectSelect || document.getElementById("sub-project-select");
    var countEl = nodes.projectCount || document.getElementById("sub-project-count");
    var revealBtn = nodes.reveal || document.getElementById("sub-reveal");
    if (selectEl) {
      selectEl.textContent = "";
      var titles = {};
      state.projects.forEach(function (item) {
        var key = item.title || "";
        titles[key] = (titles[key] || 0) + 1;
      });
      state.projects.forEach(function (item) {
        var label = projectLabel(item);
        // 同名标题用 id 后 4 位消歧，用户才知道选的是哪一条
        if (titles[item.title || ""] > 1) label += " · " + String(item.id || "").slice(-4);
        selectEl.appendChild(h("option", {
          value: item.id,
          text: label,
          selected: item.id === state.projectId ? "selected" : null
        }));
      });
      if (state.projectId && !state.projects.some(function (item) { return item.id === state.projectId; })) {
        // 当前项目在搜索词/分页外：补一个占位 option 保持选中，不静默换项目
        var fallback = (state.project && state.project.title) || state.projectId;
        selectEl.appendChild(h("option", {
          value: state.projectId,
          text: fallback,
          selected: "selected"
        }));
      }
      selectEl.disabled = state.projectLoading || !state.projects.length;
    }
    if (countEl) {
      if (state.projectLoading) countEl.textContent = "读取项目列表…";
      else if (!state.projectTotal) countEl.textContent = "";
      else if (state.projectTotal > state.projects.length) {
        countEl.textContent = "共 " + state.projectTotal + " 个，输入关键词搜索";
      } else {
        countEl.textContent = "共 " + state.projectTotal + " 个项目";
      }
    }
    if (revealBtn) {
      var root = state.project && state.project.root_dir;
      revealBtn.disabled = !root;
      revealBtn.title = root ? "在资源管理器里打开这个项目的根目录" : "这个项目没有记录根目录";
    }
  }

  /** 搜索（防抖后）只重建 options，不动输入框——否则焦点会丢（规划 §4.2）。 */
  function loadProjectOptions(query, token) {
    state.projectQuery = query || "";
    state.projectLoading = true;
    paintProjectBar();
    call("library.list", { query: state.projectQuery, limit: 50, offset: 0 })
      .then(function (data) {
        if (!samePage(token)) return;
        state.projectLoading = false;
        state.projects = (data && data.items) || [];
        state.projectTotal = (data && data.total) || state.projects.length;
        paintProjectBar();
        if (state.projectId && !state.projects.some(function (item) { return item.id === state.projectId; })) {
          // 当前项目在搜索词外：单独拉它的资料插回首位，保持选中
          call("library.get", { id: state.projectId })
            .then(function (detail) {
              if (!samePage(token) || state.projectQuery !== (query || "")) return;
              var project = detail && detail.project;
              if (project && !state.projects.some(function (item) { return item.id === project.id; })) {
                state.projects.unshift(project);
                paintProjectBar();
              }
            })
            .catch(function () {
              /* 拉不回详情就靠 paintProjectBar 的占位 option 兜底，不打断搜索 */
            });
        }
      })
      .catch(function (err) {
        if (!samePage(token)) return;
        state.projectLoading = false;
        showNotice({ text: "项目列表搜索失败：" + ((err && err.message) || err) });
        paintNotice();
        paintProjectBar();
      });
  }

  function defaultProjectId(items) {
    for (var i = 0; i < items.length; i++) {
      if (items[i].has_transcript === true) return items[i].id;
    }
    return items.length ? items[0].id : "";
  }

  /** 切换项目：先落盘旧草稿，失败阻断并回弹；成功走 replace，不产生历史条目（规划 §4.5）。 */
  function switchProject(nextId) {
    var selectEl = nodes.projectSelect || document.getElementById("sub-project-select");
    if (!nextId || nextId === state.projectId) {
      if (selectEl) selectEl.value = state.projectId;
      return;
    }
    var oldId = state.projectId;
    var searchEl = nodes.projectSearch || document.getElementById("sub-project-search");
    var revealBtn = nodes.reveal || document.getElementById("sub-reveal");
    if (selectEl) selectEl.disabled = true;
    if (searchEl) searchEl.disabled = true;
    if (revealBtn) revealBtn.disabled = true;
    if (nodes.status) nodes.status.textContent = "保存中…";
    flushDraft()
      .then(function () {
        if (!samePage(pageToken)) return;
        WB.router.replace("subtitles", { project: nextId });
      })
      .catch(function (err) {
        if (!samePage(pageToken)) return;
        // 外壳里的 error 文案只在 render 时评估，paint() 不重建外壳——
        // 切换失败这种"页中途出现"的提示必须走 notice 容器（paintNotice 会重画）。
        showNotice({
          kind: "switch-failed",
          text: "切换失败：字幕还没保存成功（" + ((err && err.message) || err) + "）"
        });
        if (selectEl) {
          selectEl.disabled = false;
          selectEl.value = oldId;
        }
        if (searchEl) searchEl.disabled = false;
        paintNotice();
        paintProjectBar();
        paint();
      });
  }

  /** 「在文件夹中显示」：只传 root_dir；失败让 WB.rpc 默认 toast 后端原话（规划 §4.4）。 */
  function revealProject() {
    var root = state.project && state.project.root_dir;
    if (!root) return;
    WB.rpc("fs.reveal", { path: root }).catch(function () { /* 默认已 toast */ });
  }

  /** 进入页面后的总入口：先拿项目列表，再决定进哪个项目（规划 §4.3）。 */
  function init(token) {
    state.projectLoading = true;
    paintProjectBar();
    call("library.list", { query: state.projectQuery, limit: 50, offset: 0 })
      .then(function (data) {
        if (!samePage(token)) return;
        state.projectLoading = false;
        state.projects = (data && data.items) || [];
        state.projectTotal = (data && data.total) || state.projects.length;
        if (!state.projects.length) {
          showNotice({
            kind: "no-projects",
            text: "还没有项目。先去「新建任务」下载，或在「素材库」导入本地视频。"
          });
          paint();
          return;
        }
        if (!state.projectId) {
          var first = defaultProjectId(state.projects);
          if (first) {
            // 无参数进入：自动选默认项目并替换当前历史条目，由路由重渲染完整页面
            WB.router.replace("subtitles", { project: first });
            return;
          }
        }
        paint();
        load(token);
      })
      .catch(function (err) {
        if (!samePage(token)) return;
        state.projectLoading = false;
        showNotice({ kind: "projects-failed", text: "项目列表加载失败：" + ((err && err.message) || err) });
        paint();
      });
  }

  /** 离开页面：停定时器、补存草稿、拆播放器与 resize 监听（规划 §4.8）。 */
  function destroy() {
    pageToken += 1;
    window.clearTimeout(saveTimer);
    if (state.dirty && state.projectId) {
      var payload = { project_id: state.projectId, segments: snapshot().segments };
      state.dirty = false;
      saveChain = saveChain
        .then(function () { return call("subtitle.save", payload); })
        .then(function () {}, function (err) {
          ui.toast("字幕保存失败：" + ((err && err.message) || err), "error");
        });
    }
    disposeMedia();
    detachResize();
  }

  // ------------------------------------------------------------ 渲染：外壳

  function render(ctx) {
    pageToken += 1;
    var token = pageToken;
    showNotice(null);
    disposeMedia();
    detachResize();
    window.clearTimeout(saveTimer);
    resetForProject();
    nodes = {};
    state.projectId = (ctx && ctx.params && ctx.params.project) || "";

    var page = h("div", { class: "page page--subtitles" }, [
      h("header", { class: "page__head" }, [
        h("div", {}, [
          h("h1", { class: "page__title", text: "字幕编辑器" }),
          h("p", { class: "page__hint", text: "双击改字，S 分割，M 合并，Ctrl+Z 撤销" })
        ]),
        h("div", { class: "page__actions sub-toolbar" }, toolbar())
      ]),
      h("div", { class: "sub-projects" }, [
        h("label", { class: "sum-fld sub-projects__pick" }, [
          h("span", { class: "k", text: "PROJECT" }),
          h("select", { id: "sub-project-select" })
        ]),
        h("label", { class: "sum-fld sub-projects__search" }, [
          h("span", { class: "k", text: "搜索" }),
          h("input", { id: "sub-project-search", type: "search", placeholder: "标题 / UP / BV 号" })
        ]),
        h("button", {
          id: "sub-reveal",
          class: "sum-ghost",
          type: "button",
          text: "在文件夹中显示",
          onclick: revealProject
        }),
        h("span", { id: "sub-project-count", class: "muted", text: "" })
      ]),
      h("div", { id: "sub-notice" }),
      h("div", { class: "sub-grid" }, [
        h("section", { class: "card card--flat sub-stage" }, [
          h("div", { class: "sub-stage__video", id: "sub-video" }),
          h("div", { class: "wave" }, [
            h("canvas", { class: "wave__canvas", id: "sub-wave" }),
            h("div", { class: "wave__legend", id: "sub-ticks" })
          ]),
          h("div", { class: "sub-stage__foot" }, [
            h("span", { class: "mono", id: "sub-time", text: "0:00.000" }),
            h("span", { class: "muted", id: "sub-note", text: "" })
          ])
        ]),
        h("section", { class: "card card--flat sub-list" }, [
          h("div", { class: "sub-list__head" }, [
            h("span", { text: "时间" }),
            h("span", { text: "文本" }),
            h("span", { class: "ta-r", text: "质检" })
          ]),
          h("div", { class: "sub-list__scroll", id: "sub-scroll", tabindex: "0" }, [
            h("div", { class: "sub-list__inner", id: "sub-inner" })
          ])
        ])
      ]),
      h("footer", { class: "sub-status" }, [
        h("span", { id: "sub-status-text", text: "就绪" }),
        h("span", { class: "sub-status__spacer" }),
        h("span", { id: "sub-stats", class: "mono", text: "" })
      ]),
      h("div", { class: "sub-opt", id: "sub-opt", hidden: "hidden" }),
      h("div", { class: "sub-opt", id: "sub-trans", hidden: "hidden" }),
      h("div", { class: "sub-opt", id: "sub-export", hidden: "hidden" })
    ]);

    nodes.page = page;
    window.setTimeout(function () {
      if (!samePage(token)) return;
      nodes.listScroll = document.getElementById("sub-scroll");
      nodes.listBody = document.getElementById("sub-inner");
      nodes.status = document.getElementById("sub-status-text");
      nodes.stats = document.getElementById("sub-stats");
      nodes.time = document.getElementById("sub-time");
      nodes.note = document.getElementById("sub-note");
      nodes.projectSelect = document.getElementById("sub-project-select");
      nodes.projectSearch = document.getElementById("sub-project-search");
      nodes.projectCount = document.getElementById("sub-project-count");
      nodes.reveal = document.getElementById("sub-reveal");
      var canvas = document.getElementById("sub-wave");
      if (canvas && WB.waveform) {
        wave = WB.waveform.mount(canvas, { onSeek: function (value) { seekTo(value); } });
        // 保存 handler 引用，离开页面时拆掉；旧写法每次进入都挂一个新的，从不移除
        resizeHandler = function () { wave && wave.resize(); };
        window.addEventListener("resize", resizeHandler);
      }
      if (nodes.listScroll) {
        nodes.listScroll.addEventListener("scroll", function () {
          state.scrollTop = nodes.listScroll.scrollTop;
          renderRows(false);
        });
        nodes.listScroll.addEventListener("keydown", onKeyDown);
      }
      if (nodes.projectSelect) {
        nodes.projectSelect.addEventListener("change", function () {
          switchProject(nodes.projectSelect.value);
        });
      }
      if (nodes.projectSearch) {
        var searchTimer = 0;
        nodes.projectSearch.addEventListener("input", function () {
          window.clearTimeout(searchTimer);
          searchTimer = window.setTimeout(function () {
            loadProjectOptions(nodes.projectSearch.value.trim(), token);
          }, 250);
        });
      }
      mountTicks();
      paint();
      init(token);
    }, 0);

    return page;
  }

  function toolbar() {
    function btn(text, title, onclick, extraClass) {
      return h("button", {
        class: "btn " + (extraClass || "btn--ghost"),
        title: title,
        text: text,
        onclick: onclick
      });
    }
    return [
      btn("分割", "在播放头处把当前段切成两段（S）", doSplit),
      btn("合并", "把选中的相邻段合成一段（M）", doMerge),
      btn("删除", "删除选中的段（Del）", doDelete, "btn--ghost btn--danger"),
      btn("平移", "把选中段（不选=全部）整体前后移", doShift),
      btn("替换", "批量查找替换（Ctrl+F）", doReplace),
      btn("撤销", "Ctrl+Z", doUndo),
      btn("重做", "Ctrl+Shift+Z", doRedo),
      btn("AI 优化", "用大模型补标点、合并碎句、去口头禅（先预览再应用）", doOptimize),
      btn("回滚优化", "退回上一次「应用优化」之前的版本", doRollback),
      btn("AI 翻译", "机器翻译出译文草稿，逐条确认后才写双语字幕", doTranslate),
      btn("导出", "出 SRT / ASS / VTT，或连视频一起出软字幕、硬字幕", doExport)
    ];
  }

  // ------------------------------------------------------------ AI 优化（P2-04）
  //
  // 流程刻意拆成三步：跑 → 预览 → 应用。中间那步是必须的：模型偶尔会把
  // 一句正常的话改成别的意思，没有预览就等于让用户闭眼签字。

  function optHost() {
    return document.getElementById("sub-opt");
  }

  function setStatus(text) {
    if (nodes.status) nodes.status.textContent = text;
  }

  function doOptimize() {
    if (!state.projectId || state.optBusy) return;
    state.optBusy = true;
    state.opt = null;
    paintOpt({ phase: "running", message: "正在让模型读字幕…" });
    setStatus("AI 优化中…");
    WB.rpc("subtitle.optimize", { project_id: state.projectId, batch_size: 30 })
      .then(function (data) {
        state.optJob = (data && data.job && data.job.id) || "";
        if (!state.optJob) throw new Error("没有拿到任务编号");
        pollOpt(0);
      })
      .catch(function (err) {
        state.optBusy = false;
        setStatus("就绪");
        paintOpt({ phase: "error", message: optErrorText(err) });
      });
  }

  function optErrorText(err) {
    var code = (err && err.code) || "";
    if (code === "LLM_NOT_CONFIGURED") return "还没配置大模型。去 设置 → 模型 填好接口地址与 Key，再回来点「AI 优化」。";
    return (err && err.message) || "优化失败";
  }

  function pollOpt(tries, pid, token) {
    if (!state.optJob) return;
    pid = pid || state.projectId;
    token = (token === undefined) ? pageToken : token;
    WB.rpc("job.get", { id: state.optJob }, { silent: true })
      .then(function (job) {
        var status = (job && job.status) || "";
        if (!samePage(token) || pid !== state.projectId) {
          // 已换项目/已销毁：只清自己的任务状态，不写界面（服务端任务不中断）
          state.optBusy = false;
          state.optJob = "";
          return undefined;
        }
        if (status === "succeeded") {
          state.optBusy = false;
          setStatus("就绪");
          var result = (job && job.result) || {};
          var changes = result.changes || [];
          if (!changes.length) {
            paintOpt({
              phase: "empty",
              message: "模型没提出改动" + (result.failed && result.failed.length ? "（有 " + result.failed.length + " 批失败）" : ""),
              result: result
            });
            return;
          }
          var accepted = {};
          changes.forEach(function (c) { accepted[c.id] = true; });
          state.opt = { changes: changes, failed: result.failed || [], model: result.model || "", accepted: accepted };
          paintOpt({ phase: "preview" });
          return;
        }
        if (status === "failed" || status === "canceled") {
          state.optBusy = false;
          setStatus("就绪");
          paintOpt({ phase: "error", message: job.error_message || (status === "canceled" ? "已取消" : "优化失败") });
          return;
        }
        paintOpt({ phase: "running", message: "已处理 " + Math.round(((job && job.progress) || 0) * 100) + "%" + (job && job.stage ? " · " + job.stage : "") });
        if (tries > 900) {
          state.optBusy = false;
          paintOpt({ phase: "error", message: "等太久了，任务还没结束。可以去任务面板看看。" });
          return;
        }
        window.setTimeout(function () { pollOpt(tries + 1, pid, token); }, 700);
      })
      .catch(function (err) {
        state.optBusy = false;
        if (!samePage(token) || pid !== state.projectId) return;
        paintOpt({ phase: "error", message: (err && err.message) || "查不到任务状态" });
      });
  }

  function paintOpt(view) {
    var host = optHost();
    if (!host) return;
    host.hidden = false;
    host.textContent = "";

    var head = h("div", { class: "sub-opt__head" }, [
      h("strong", { text: "AI 字幕优化" }),
      h("span", { class: "muted", id: "sub-opt-note", text: "" })
    ]);
    host.appendChild(head);

    if (view.phase === "running") {
      host.appendChild(h("p", { class: "muted", text: view.message || "处理中…" }));
      host.appendChild(h("button", { class: "btn btn--ghost", text: "收起", onclick: closeOpt }));
      return;
    }
    if (view.phase === "error" || view.phase === "empty") {
      host.appendChild(h("p", { class: "callout callout--warn", text: view.message || "" }));
      if (view.result && view.result.failed && view.result.failed.length) {
        host.appendChild(
          h("p", { class: "muted", text: "失败原因（这些段保持原文）：" + view.result.failed.map(function (f) { return f.code + " " + f.message; }).join("；") })
        );
      }
      host.appendChild(h("button", { class: "btn btn--ghost", text: "收起", onclick: closeOpt }));
      return;
    }

    var list = h("div", { class: "sub-opt__list" });
    state.opt.changes.forEach(function (change, index) {
      var box = h("input", {
        type: "checkbox",
        checked: "checked",
        onchange: function (ev) {
          state.opt.accepted[change.id] = !!ev.target.checked;
          refreshOptCount();
        }
      });
      box.checked = !!state.opt.accepted[change.id];
      list.appendChild(
        h("div", { class: "sub-opt__row" }, [
          box,
          h("span", { class: "mono sub-opt__time", text: fmtShort(change.start_ms) }),
          h("span", { class: "sub-opt__before", text: change.before || "（空）" }),
          h("span", { class: "sub-opt__arrow", text: "→" }),
          h("span", { class: "sub-opt__after", text: change.kind === "merge" ? "（并入上一句）" : (change.after || "（清空）") })
        ])
      );
    });
    host.appendChild(list);

    var note = head.querySelector("#sub-opt-note");
    if (note) {
      note.textContent = state.opt.changes.length + " 段有改动 · 模型 " + (state.opt.model || "未知") +
        (state.opt.failed.length ? " · " + state.opt.failed.length + " 批失败（这些段保持原文）" : "");
    }

    var count = h("span", { class: "muted", text: "" });
    function refreshOptCount() {
      var n = Object.keys(state.opt.accepted).filter(function (k) { return state.opt.accepted[k]; }).length;
      count.textContent = "已选 " + n + " / " + state.opt.changes.length;
      applyBtn.disabled = n === 0;
    }
    var applyBtn = h("button", {
      class: "btn btn--primary",
      text: "应用所选",
      onclick: function () { applyOpt(applyBtn); }
    });
    host.appendChild(
      h("div", { class: "sub-opt__bar" }, [
        count,
        h("span", { class: "sub-status__spacer" }),
        h("button", { class: "btn btn--ghost", text: "全部取消", onclick: closeOpt }),
        applyBtn
      ])
    );
    refreshOptCount();
  }

  function applyOpt(btn) {
    if (!state.opt || state.optBusy) return;
    var ids = Object.keys(state.opt.accepted).filter(function (k) { return state.opt.accepted[k]; });
    if (!ids.length) return;
    state.optBusy = true;
    btn.disabled = true;
    btn.textContent = "应用中…";
    WB.rpc("subtitle.optimize.apply", {
      project_id: state.projectId,
      changes: state.opt.changes,
      accepted_ids: ids
    })
      .then(function (data) {
        state.optBusy = false;
        state.opt = null;
        state.undo.push(snapshot());
        state.segments = (data && data.segments) || state.segments;
        state.qc = (data && data.qc) || state.qc;
        state.stats = (data && data.stats) || state.stats;
        state.redo = [];
        state.dirty = false;
        closeOpt();
        renderRows(true);
        wave && wave.setSegments(state.segments, 0);
        updateStatus();
        updateToolbar();
        WB.toast("已应用 " + ((data && data.applied) || 0) + " 段改动，可随时「回滚优化」", "success");
      })
      .catch(function (err) {
        state.optBusy = false;
        btn.disabled = false;
        btn.textContent = "应用所选";
        WB.toast((err && err.message) || "应用失败", "error");
      });
  }

  function doRollback() {
    if (!state.projectId || state.optBusy) return;
    state.optBusy = true;
    WB.rpc("subtitle.rollback", { id: state.projectId })
      .then(function (data) {
        state.optBusy = false;
        state.undo.push(snapshot());
        state.segments = (data && data.segments) || state.segments;
        state.qc = (data && data.qc) || state.qc;
        state.stats = (data && data.stats) || state.stats;
        closeOpt();
        renderRows(true);
        wave && wave.setSegments(state.segments, 0);
        updateStatus();
        updateToolbar();
        WB.toast("已回到优化前（" + ((data && data.restored_at) || "上一版") + "）", "success");
      })
      .catch(function (err) {
        state.optBusy = false;
        var code = (err && err.code) || "";
        WB.toast(code === "NOT_FOUND" ? "没有可回滚的版本" : ((err && err.message) || "回滚失败"), "warning");
      });
  }

  function closeOpt() {
    state.opt = null;
    var host = optHost();
    if (host) {
      host.hidden = true;
      host.textContent = "";
    }
  }

  // ------------------------------------------------ P2-05 机器翻译与双语字幕
  //
  // 和「AI 优化」同一条纪律：译文是草稿，逐条确认才落文件。
  // 界面上改过的译文走 `overrides` 回后端，和确认动作一起落盘——
  // 不然"可逐条编辑"只是句空话。
  function transHost() {
    return document.getElementById("sub-trans");
  }

  function draftView(target, segments, failed) {
    var accepted = {};
    (segments || []).forEach(function (seg) {
      if (String(seg.translated_text || "").trim()) accepted[seg.id] = true;
    });
    return {
      target: target || "en",
      layout: "",
      segments: segments || [],
      accepted: accepted,
      overrides: {},
      failed: failed || []
    };
  }

  function doTranslate() {
    if (!state.projectId || state.transBusy) return;
    state.trans = null;
    paintTrans({ phase: "loading" });
    call("subtitle.translation", { project_id: state.projectId, target: "" })
      .then(function (data) {
        if (data && data.found && (data.segments || []).length) {
          state.trans = draftView(data.target, data.segments, []);
          paintTrans({ phase: "preview", note: "这是上一次的译文草稿" });
        } else {
          paintTrans({ phase: "form" });
        }
      })
      .catch(function () { paintTrans({ phase: "form" }); });
  }

  function transErrorText(err) {
    var code = (err && err.code) || "";
    if (code === "LLM_NOT_CONFIGURED") return "还没配置大模型。去 设置 → 模型 填好接口地址与 Key，再回来点「AI 翻译」。";
    return (err && err.message) || "翻译失败";
  }

  function startTranslate(retryIds) {
    if (!state.projectId || state.transBusy) return;
    var targetSel = document.getElementById("tr-target");
    var layoutSel = document.getElementById("tr-layout");
    var target = (targetSel && targetSel.value) || (state.trans && state.trans.target) || "auto";
    var layout = (layoutSel && layoutSel.value) || (state.trans && state.trans.layout) || "";
    state.transBusy = true;
    state.transLayout = layout;
    setStatus("翻译中…");
    paintTrans({ phase: "running", message: "正在让模型逐批翻译…" });
    WB.rpc("subtitle.translate", {
      project_id: state.projectId, target: target, layout: layout,
      batch_size: 10, segment_ids: retryIds || []
    })
      .then(function (data) {
        state.transJob = (data && data.job && data.job.id) || "";
        if (!state.transJob) throw new Error("没有拿到任务编号");
        pollTrans(0);
      })
      .catch(function (err) {
        state.transBusy = false;
        setStatus("就绪");
        paintTrans({ phase: "error", message: transErrorText(err) });
      });
  }

  function pollTrans(tries, pid, token) {
    if (!state.transJob) return;
    pid = pid || state.projectId;
    token = (token === undefined) ? pageToken : token;
    WB.rpc("job.get", { id: state.transJob }, { silent: true })
      .then(function (job) {
        var status = (job && job.status) || "";
        if (!samePage(token) || pid !== state.projectId) {
          // 已换项目/已销毁：只清自己的任务状态，不写界面（服务端任务不中断）
          state.transBusy = false;
          state.transJob = "";
          return undefined;
        }
        if (status === "succeeded") {
          var result = (job && job.result) || {};
          return call("subtitle.translation", {
            project_id: pid, target: result.target || ""
          }).then(function (data) {
            state.transBusy = false;
            setStatus("就绪");
            state.trans = draftView((data && data.target) || result.target,
                                    (data && data.segments) || [], result.failed || []);
            if (state.transLayout) state.trans.layout = state.transLayout;
            paintTrans({ phase: "preview", result: result });
          });
        }
        if (status === "failed" || status === "canceled") {
          state.transBusy = false;
          setStatus("就绪");
          paintTrans({ phase: "error",
                       message: (job && job.error_message) || (status === "canceled" ? "已取消" : "翻译失败") });
          return undefined;
        }
        paintTrans({
          phase: "running",
          message: "已处理 " + Math.round(((job && job.progress) || 0) * 100) + "%" +
            (job && job.stage ? " · " + job.stage : "")
        });
        if (tries > 1800) {
          state.transBusy = false;
          paintTrans({ phase: "error", message: "等太久了，任务还没结束。可以去任务面板看看。" });
          return undefined;
        }
        window.setTimeout(function () { pollTrans(tries + 1, pid, token); }, 700);
        return undefined;
      })
      .catch(function (err) {
        state.transBusy = false;
        if (!samePage(token) || pid !== state.projectId) return;
        paintTrans({ phase: "error", message: (err && err.message) || "查不到任务状态" });
      });
  }

  var TARGETS = [["auto", "自动（中文→英文，其余→中文）"], ["zh", "中文"], ["en", "英文"], ["ja", "日文"]];
  var LAYOUTS = [["original_top", "原文在上"], ["translated_top", "译文在上"],
                 ["original_only", "仅原文"], ["translated_only", "仅译文"]];

  function select(id, options, value) {
    return h("select", { class: "select", id: id }, options.map(function (pair) {
      return h("option", { value: pair[0], text: pair[1], selected: pair[0] === value ? "selected" : null });
    }));
  }

  function field(label, node) {
    return h("label", { class: "field" }, [h("span", { class: "field__label", text: label }), node]);
  }

  function paintTrans(view) {
    var host = transHost();
    if (!host) return;
    host.hidden = false;
    host.textContent = "";
    host.appendChild(h("div", { class: "sub-opt__head" }, [
      h("strong", { text: "机器翻译" }),
      h("span", { class: "muted", text: view.note || "" })
    ]));

    if (view.phase === "loading" || view.phase === "running") {
      host.appendChild(h("p", { class: "muted", text: view.message || "处理中…" }));
      host.appendChild(h("button", { class: "btn btn--ghost", text: "收起", onclick: closeTrans }));
      return;
    }
    if (view.phase === "error") {
      host.appendChild(h("p", { class: "callout callout--warn", text: view.message || "翻译失败" }));
      host.appendChild(h("div", { class: "sub-opt__bar" }, [
        h("button", { class: "btn btn--ghost", text: "收起", onclick: closeTrans }),
        h("button", { class: "btn btn--primary", text: "重试", onclick: function () { paintTrans({ phase: "form" }); } })
      ]));
      return;
    }
    if (view.phase === "form") {
      var target = ((WB.state && WB.state.settings) || {}).subtitle;
      target = (target && target.translate && target.translate.target) || "auto";
      host.appendChild(h("p", {
        class: "muted",
        text: "译文先落成草稿，不动原字幕。逐条看过、勾过，再写 subtitle.语言.srt 与双语 ASS。"
      }));
      host.appendChild(h("div", { class: "tr-form" }, [
        field("目标语言", select("tr-target", TARGETS, target)),
        field("双语版式", select("tr-layout", LAYOUTS, ""))
      ]));
      host.appendChild(h("div", { class: "sub-opt__bar" }, [
        h("span", { class: "muted", text: "术语表在 设置 → 字幕 里维护" }),
        h("span", { class: "sub-status__spacer" }),
        h("button", { class: "btn btn--ghost", text: "收起", onclick: closeTrans }),
        h("button", { class: "btn btn--primary", text: "开始翻译", onclick: function () { startTranslate([]); } })
      ]));
      return;
    }

    // 预览：一行一段，原文只读、译文可改，勾选＝接受
    var list = h("div", { class: "sub-opt__list" });
    var translatedCount = 0;
    state.trans.segments.forEach(function (seg) {
      var text = String(seg.translated_text || "");
      if (text.trim()) translatedCount += 1;
      var box = h("input", {
        type: "checkbox",
        disabled: text.trim() ? null : "disabled",
        onchange: function (ev) {
          state.trans.accepted[seg.id] = !!ev.target.checked;
          refreshTransCount();
        }
      });
      box.checked = !!state.trans.accepted[seg.id];
      var edit = h("input", {
        class: "input tr-edit",
        type: "text",
        value: text,
        placeholder: "（这段没译出来，可手填或重试）",
        oninput: function (ev) {
          state.trans.overrides[seg.id] = ev.target.value;
          if (ev.target.value.trim() && !state.trans.accepted[seg.id]) {
            state.trans.accepted[seg.id] = true;
            box.checked = true;
            box.disabled = false;
          }
          refreshTransCount();
        }
      });
      list.appendChild(h("div", { class: "tr-row" }, [
        box,
        h("span", { class: "mono sub-opt__time", text: fmtShort(seg.start_ms) }),
        h("span", { class: "sub-opt__before", text: seg.text || "（空）" }),
        edit
      ]));
    });
    host.appendChild(list);

    var count = h("span", { class: "muted", text: "" });
    function refreshTransCount() {
      var n = Object.keys(state.trans.accepted).filter(function (k) { return state.trans.accepted[k]; }).length;
      var edited = Object.keys(state.trans.overrides).length;
      count.textContent = "已接受 " + n + " / " + state.trans.segments.length +
        (edited ? " · 手改 " + edited + " 段" : "");
      applyBtn.disabled = n === 0;
    }
    var applyBtn = h("button", {
      class: "btn btn--primary",
      text: "写入双语字幕",
      onclick: function () { applyTranslation(applyBtn); }
    });
    var bar = h("div", { class: "sub-opt__bar" }, [
      count,
      h("span", { class: "muted", text: state.trans.failed.length
        ? " · " + state.trans.failed.length + " 批失败" : "" }),
      h("span", { class: "sub-status__spacer" }),
      state.trans.failed.length
        ? h("button", {
            class: "btn btn--ghost",
            text: "重试失败段",
            onclick: function () {
              var ids = [];
              state.trans.failed.forEach(function (f) { ids = ids.concat(f.ids || []); });
              startTranslate(ids);
            }
          })
        : null,
      h("button", { class: "btn btn--ghost", text: "收起", onclick: closeTrans }),
      h("span", { class: "tr-bar__select", title: "双语版式：决定 subtitle.bilingual.ass 的行序" },
        [select("tr-layout-2", LAYOUTS, state.trans.layout || "")]),
      applyBtn
    ]);
    bar.addEventListener("change", function (ev) {
      if (ev.target && ev.target.id === "tr-layout-2") state.trans.layout = ev.target.value;
    });
    host.appendChild(bar);
    refreshTransCount();
  }

  function applyTranslation(btn) {
    if (!state.trans || state.transBusy) return;
    var ids = Object.keys(state.trans.accepted).filter(function (k) { return state.trans.accepted[k]; });
    if (!ids.length) return;
    state.transBusy = true;
    btn.disabled = true;
    btn.textContent = "写入中…";
    WB.rpc("subtitle.translation.apply", {
      project_id: state.projectId,
      target: state.trans.target,
      layout: state.trans.layout,
      accepted_ids: ids,
      overrides: state.trans.overrides
    })
      .then(function (data) {
        state.transBusy = false;
        closeTrans();
        setStatus("就绪");
        WB.toast("已写入 " + ((data && data.srt) || "译文 SRT") + " 与 " + ((data && data.ass) || "双语 ASS"), "success");
      })
      .catch(function (err) {
        state.transBusy = false;
        btn.disabled = false;
        btn.textContent = "写入双语字幕";
        WB.toast((err && err.message) || "写入失败", "error");
      });
  }

  function closeTrans() {
    state.trans = null;
    state.transJob = "";
    var host = transHost();
    if (host) {
      host.hidden = true;
      host.textContent = "";
    }
  }

  function fmtShort(ms) {
    var total = Math.max(0, Math.round((ms || 0) / 1000));
    var m = Math.floor(total / 60);
    var s = total % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function mountTicks() {
    var host = document.getElementById("sub-ticks");
    if (!host) return;
    host.innerHTML = "";
    nodes.ticksHost = host;
  }

  // ------------------------------------------------------------ 渲染：虚拟列表

  // ------------------------------------------------------------ 导出（P2-06）
  //
  // 三个格式同步写，视频两种模式进任务队列（全片重编码几分钟很正常）。
  // 软字幕必须说清楚"样式不保留"——MP4 里只能放 mov_text，
  // 不说清楚，用户会以为导出没生效。

  var EXPORT_FORMATS = [["srt", "SRT"], ["ass", "ASS"], ["vtt", "VTT"]];
  var EXPORT_MODES = [
    ["none", "只出字幕文件"],
    ["soft", "软字幕（挂进 MP4，可开关，样式不保留）"],
    ["hard", "硬字幕（烧进画面，样式保留）"]
  ];
  var EXPORT_HEIGHTS = [["0", "跟随源视频"], ["720", "720P"], ["1080", "1080P"]];

  function exportHost() {
    return document.getElementById("sub-export");
  }

  function closeExport() {
    var host = exportHost();
    if (!host) return;
    host.hidden = true;
    host.textContent = "";
    state.expBusy = false;
  }

  function doExport() {
    if (!state.projectId) return;
    state.expBusy = false;
    state.expJob = "";
    paintExport({ phase: "form" });
  }

  function fmtBytes(value) {
    var n = Number(value || 0);
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function paintExport(view) {
    var host = exportHost();
    if (!host) return;
    host.hidden = false;
    host.textContent = "";
    host.appendChild(h("div", { class: "sub-opt__head" }, [
      h("strong", { text: "导出字幕" }),
      h("span", { class: "muted", text: view.note || "" })
    ]));

    if (view.phase === "running") {
      host.appendChild(h("p", { class: "muted", id: "exp-status", text: view.message || "导出中…" }));
      host.appendChild(h("button", { class: "btn btn--ghost", text: "收起", onclick: closeExport }));
      return;
    }
    if (view.phase === "error") {
      host.appendChild(h("p", { class: "callout callout--warn", id: "exp-error", text: view.message || "导出失败" }));
      host.appendChild(h("div", { class: "sub-opt__bar" }, [
        h("button", { class: "btn btn--ghost", text: "收起", onclick: closeExport }),
        h("button", { class: "btn btn--primary", text: "重来", onclick: function () { paintExport({ phase: "form" }); } })
      ]));
      return;
    }
    if (view.phase === "done") {
      return paintExportDone(view);
    }

    host.appendChild(h("p", {
      class: "muted",
      text: "字号按 720P 为基准等比缩放：选 1080P 时字号/描边/边距自动 ×1.5。"
    }));
    var boxes = h("div", { class: "tr-form" }, EXPORT_FORMATS.map(function (pair) {
      var box = h("input", { type: "checkbox", class: "exp-fmt", value: pair[0] });
      box.checked = true;
      return h("label", { class: "tr-form__item" }, [box, h("span", { text: pair[1] })]);
    }));
    host.appendChild(boxes);
    host.appendChild(h("div", { class: "tr-form" }, [
      h("label", { class: "tr-form__item" }, [
        h("span", { class: "muted", text: "出片方式" }),
        select("exp-mode", EXPORT_MODES, "none")
      ]),
      h("label", { class: "tr-form__item" }, [
        h("span", { class: "muted", text: "输出高度" }),
        select("exp-height", EXPORT_HEIGHTS, "0")
      ])
    ]));
    host.appendChild(h("div", { class: "sub-opt__bar" }, [
      h("span", { class: "muted", text: "译文单独导出在「AI 翻译」里确认时完成" }),
      h("span", { class: "sub-status__spacer" }),
      h("button", { class: "btn btn--ghost", text: "收起", onclick: closeExport }),
      h("button", { class: "btn btn--primary", id: "exp-start", text: "开始导出", onclick: startExport })
    ]));
  }

  function paintExportDone(view) {
    var host = exportHost();
    var result = view.result || {};
    var files = result.files || [];
    host.appendChild(h("p", {
      class: "muted",
      id: "exp-done",
      text: (result.mode === "none" ? "字幕文件已写出" : "视频导出完成") +
        " · 目标高度 " + (result.height || "?") + "p"
    }));
    var list = h("div", { class: "exp-files" });
    files.forEach(function (file) {
      list.appendChild(h("div", { class: "exp-file" }, [
        h("span", { class: "mono", text: file.format.toUpperCase() }),
        h("span", { class: "exp-file__path", text: file.path }),
        h("span", { class: "exp-file__bytes", text: fmtBytes(file.bytes) })
      ]));
    });
    if (result.output) {
      list.appendChild(h("div", { class: "exp-file" }, [
        h("span", { class: "mono", text: result.mode === "soft" ? "软字幕" : "硬字幕" }),
        h("span", { class: "exp-file__path", text: result.output }),
        h("span", { class: "exp-file__bytes", text: view.jobNote || "" })
      ]));
    }
    host.appendChild(list);
    var reveal = (result.output || (files[0] && files[0].path) || "");
    host.appendChild(h("div", { class: "sub-opt__bar" }, [
      h("span", { class: "muted", text: view.warn || "" }),
      h("span", { class: "sub-status__spacer" }),
      reveal ? h("button", {
        class: "btn btn--ghost", text: "打开文件夹",
        onclick: function () { WB.rpc("fs.reveal", { path: reveal }, { silent: true }); }
      }) : null,
      h("button", { class: "btn btn--primary", text: "完成", onclick: closeExport })
    ]));
  }

  function startExport() {
    if (!state.projectId || state.expBusy) return;
    var formats = [];
    var boxes = document.querySelectorAll(".exp-fmt");
    for (var i = 0; i < boxes.length; i += 1) {
      if (boxes[i].checked) formats.push(boxes[i].value);
    }
    var modeSel = document.getElementById("exp-mode");
    var heightSel = document.getElementById("exp-height");
    var mode = (modeSel && modeSel.value) || "none";
    var height = parseInt((heightSel && heightSel.value) || "0", 10) || 0;
    if (!formats.length && mode === "none") {
      paintExport({ phase: "error", message: "至少勾一种字幕格式" });
      return;
    }
    state.expBusy = true;
    setStatus("导出中…");
    paintExport({ phase: "running", message: "正在写文件…" });
    call("subtitle.export", {
      project_id: state.projectId, formats: formats, mode: mode, height: height
    })
      .then(function (data) {
        if (!data || !data.job_id) {
          state.expBusy = false;
          setStatus("就绪");
          paintExport({ phase: "done", result: data || {} });
          return;
        }
        state.expJob = data.job_id;
        state.expResult = data;
        pollExport(0);
      })
      .catch(function (err) {
        state.expBusy = false;
        setStatus("就绪");
        paintExport({ phase: "error", message: (err && err.message) || "导出失败" });
      });
  }

  function pollExport(tries, pid, token) {
    if (!state.expJob) return;
    pid = pid || state.projectId;
    token = (token === undefined) ? pageToken : token;
    WB.rpc("job.get", { id: state.expJob }, { silent: true })
      .then(function (job) {
        var status = (job && job.status) || "";
        if (!samePage(token) || pid !== state.projectId) {
          // 已换项目/已销毁：只清自己的任务状态，不写界面（服务端任务不中断）
          state.expBusy = false;
          state.expJob = "";
          return undefined;
        }
        if (status === "succeeded") {
          state.expBusy = false;
          setStatus("就绪");
          var result = state.expResult || {};
          result.output = result.output || ((job && job.result) || {}).output;
          paintExport({
            phase: "done", result: result,
            jobNote: fmtBytes((job.result || {}).bytes || 0),
            warn: result.mode === "soft" ? "软字幕在播放器里可开关；样式不会保留。" : ""
          });
          return undefined;
        }
        if (status === "failed" || status === "canceled") {
          state.expBusy = false;
          setStatus("就绪");
          paintExport({
            phase: "error",
            message: (job && job.error_message) || (status === "canceled" ? "已取消" : "导出失败")
          });
          return undefined;
        }
        var pct = Math.round(((job && job.progress) || 0) * 100);
        var line = document.getElementById("exp-status");
        if (line) line.textContent = "导出中 " + pct + "%" + (job && job.stage ? " · " + job.stage : "");
        if (tries > 3600) {
          state.expBusy = false;
          paintExport({ phase: "error", message: "等太久了，任务还没结束。可以去任务面板看看。" });
          return undefined;
        }
        window.setTimeout(function () { pollExport(tries + 1, pid, token); }, 900);
        return undefined;
      })
      .catch(function (err) {
        state.expBusy = false;
        if (!samePage(token) || pid !== state.projectId) return;
        paintExport({ phase: "error", message: (err && err.message) || "查不到任务状态" });
      });
  }

  function renderRows(force) {
    if (!nodes.listBody) return;
    var count = state.segments.length;
    var view = nodes.listScroll;
    var height = view ? view.clientHeight : 480;
    var top = view ? view.scrollTop : 0;
    var start = Math.max(0, Math.floor(top / ROW_HEIGHT) - OVERSCAN);
    var visible = Math.ceil(height / ROW_HEIGHT) + OVERSCAN * 2;
    var end = Math.min(count, start + visible);

    nodes.listBody.style.height = count * ROW_HEIGHT + "px";
    // 结构没变且只是滚动：只重画窗口里的行
    var key = start + ":" + end + ":" + count + ":" + force;
    if (key === nodes.lastKey && !force) return;
    nodes.lastKey = key;

    var frag = document.createDocumentFragment();
    for (var i = start; i < end; i++) {
      frag.appendChild(rowNode(state.segments[i], i));
    }
    nodes.listBody.innerHTML = "";
    nodes.listBody.appendChild(frag);
    updateStatus();
  }

  function rowNode(seg, index) {
    var issues = state.qc[seg.id] || [];
    var level = issues.some(function (i) { return i.level === "error"; })
      ? "error"
      : issues.length
        ? "warn"
        : "";
    var classes = ["sub-row"];
    if (state.selected[seg.id]) classes.push("is-selected");
    if (seg.id === state.activeId) classes.push("is-active");
    if (level) classes.push("is-" + level);
    if (state.editingId === seg.id) classes.push("is-editing");

    var textCell;
    if (state.editingId === seg.id) {
      var input = h("input", {
        class: "sub-row__input",
        value: seg.text || "",
        onkeydown: function (event) {
          if (event.key === "Enter") {
            event.preventDefault();
            commitEdit(seg.id, event.target.value);
          } else if (event.key === "Escape") {
            event.preventDefault();
            state.editingId = "";
            renderRows(true);
          }
        },
        onblur: function (event) {
          commitEdit(seg.id, event.target.value);
        }
      });
      window.setTimeout(function () {
        input.focus();
        input.select();
      }, 0);
      textCell = input;
    } else {
      textCell = h("span", { class: "sub-row__text", text: seg.text || "" });
    }

    var row = h("div", {
      class: classes.join(" "),
      style: "top:" + index * ROW_HEIGHT + "px",
      dataset: { id: seg.id },
      onmousedown: function (event) {
        if (event.shiftKey && state.activeId) {
          selectRange(state.activeId, seg.id);
        } else if (event.ctrlKey || event.metaKey) {
          state.selected[seg.id] = !state.selected[seg.id];
        } else {
          state.selected = {};
          if (state.activeId === seg.id) {
            // 再点一次当前行 = 放行给双击编辑；此处不重建行：
            // mousedown 里同步重建会 detach 目标，真实指针序列下 Chromium 不再合成 click/dblclick
            return;
          } else {
            state.selected[seg.id] = true;
            setActive(seg.id);
          }
        }
        renderRows(true);
      },
      ondblclick: function () {
        state.editingId = seg.id;
        state.selected = {};
        state.selected[seg.id] = true;
        setActive(seg.id);
        renderRows(true);
      }
    }, [
      h("span", { class: "mono sub-row__time", text: shortMs(seg.start_ms) }),
      textCell,
      issues.length
        ? h("span", {
            class: "sub-row__flag sub-row__flag--" + (level || "warn"),
            title: issues.map(function (i) { return i.message; }).join("\n"),
            text: String(issues.length)
          })
        : h("span", { class: "sub-row__flag is-quiet", text: "" })
    ]);
    return row;
  }

  function selectRange(fromId, toId) {
    var a = indexOfId(fromId);
    var b = indexOfId(toId);
    if (a < 0 || b < 0) return;
    state.selected = {};
    for (var i = Math.min(a, b); i <= Math.max(a, b); i++) {
      state.selected[state.segments[i].id] = true;
    }
    state.activeId = toId;
  }

  // ------------------------------------------------------------ 编辑操作

  function commitEdit(id, text) {
    var seg = byId(id);
    state.editingId = "";
    if (!seg || seg.text === text) {
      renderRows(true);
      return;
    }
    runOp({ op: "update", segment_id: id, text: String(text) })
      .then(function () {
        state.activeId = id;
        renderRows(true);
        nodes.listScroll && nodes.listScroll.focus();
      })
      .catch(function () {
        renderRows(true);
      });
  }

  function doSplit() {
    var id = state.activeId;
    var seg = byId(id);
    if (!seg) {
      ui.toast("先选一段再分割", "warning");
      return;
    }
    var at = currentTimeMs();
    if (at <= seg.start_ms + 120 || at >= seg.end_ms - 120) {
      ui.toast("播放头不在这一段中间，无法分割", "warning");
      return;
    }
    runOp({ op: "split", segment_id: id, at_ms: at }, {
      active: function (list) {
        var index = list.findIndex(function (s) { return s.start_ms === at; });
        return (list[index] || list[0] || {}).id;
      }
    });
  }

  function doMerge() {
    var ids = selectedIds();
    if (ids.length < 2) {
      ui.toast("至少选两段（按住 Shift 选连续段）", "warning");
      return;
    }
    ids.sort(function (a, b) { return indexOfId(a) - indexOfId(b); });
    runOp({ op: "merge", segment_ids: ids }, {
      select: function () { return {}; },
      active: function () { return ids[0]; }
    });
  }

  function doDelete() {
    var ids = selectedIds();
    if (!ids.length) {
      ui.toast("先选要删除的段", "warning");
      return;
    }
    ui.confirm("删除字幕段", "将删除 " + ids.length + " 段，可以用 Ctrl+Z 撤回。", {
      confirmText: "删除",
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      runOp({ op: "delete", segment_ids: ids }, {
        select: function () { return {}; }
      });
    });
  }

  function doShift() {
    var ids = selectedIds();
    var scope = ids.length ? "选中的 " + ids.length + " 段" : "全部段";
    ui.modal({
      title: "时间平移",
      body: h("div", { class: "form" }, [
        h("label", { class: "form__row" }, [
          h("span", { text: "偏移（毫秒，负数为提前）" }),
          h("input", { class: "input", id: "sub-shift", value: "200", type: "number" })
        ]),
        h("p", { class: "muted", text: "作用于" + scope })
      ]),
      footer: function (close) {
        return [
          h("button", { class: "btn btn--ghost", text: "取消", onclick: close }),
          h("button", {
            class: "btn btn--primary",
            text: "应用",
            onclick: function () {
              var input = document.getElementById("sub-shift");
              var offset = parseInt(input && input.value, 10);
              close();
              if (!isFinite(offset) || offset === 0) return;
              runOp({ op: "shift", offset_ms: offset,
                      segment_ids: ids.length ? ids : [] });
            }
          })
        ];
      }
    });
  }

  function doReplace() {
    var ids = selectedIds();
    ui.modal({
      title: "批量替换",
      body: h("div", { class: "form" }, [
        h("label", { class: "form__row" }, [
          h("span", { text: "查找" }),
          h("input", { class: "input", id: "sub-find", value: "" })
        ]),
        h("label", { class: "form__row" }, [
          h("span", { text: "替换为" }),
          h("input", { class: "input", id: "sub-repl", value: "" })
        ]),
        h("label", { class: "form__row form__row--inline" }, [
          h("input", { type: "checkbox", id: "sub-regex" }),
          h("span", { text: "正则（替换里可用 $1）" })
        ]),
        h("p", { class: "muted", text: ids.length ? "只替换选中的 " + ids.length + " 段" : "替换全文" })
      ]),
      footer: function (close) {
        return [
          h("button", { class: "btn btn--ghost", text: "取消", onclick: close }),
          h("button", {
            class: "btn btn--primary",
            text: "替换",
            onclick: function () {
              var find = (document.getElementById("sub-find") || {}).value || "";
              var repl = (document.getElementById("sub-repl") || {}).value || "";
              var regex = !!(document.getElementById("sub-regex") || {}).checked;
              close();
              if (!find) return;
              runOp({
                op: "replace",
                pattern: find,
                replacement: repl,
                regex: regex,
                segment_ids: ids
              }, {
                toast: null
              }).then(function (data) {
                ui.toast(data.hits ? "替换了 " + data.hits + " 段" : "没有命中", data.hits ? "success" : "warning");
              });
            }
          })
        ];
      }
    });
  }

  function doUndo() {
    if (!state.undo.length) return;
    var prev = state.undo.pop();
    state.redo.push(snapshot());
    state.segments = prev.segments;
    state.dirty = true;
    save();
    renderRows(true);
    wave && wave.setSegments(state.segments, 0);
    updateToolbar();
  }

  function doRedo() {
    if (!state.redo.length) return;
    var next = state.redo.pop();
    state.undo.push(snapshot());
    state.segments = next.segments;
    state.dirty = true;
    save();
    renderRows(true);
    wave && wave.setSegments(state.segments, 0);
    updateToolbar();
  }

  function onKeyDown(event) {
    var tag = (event.target && event.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    var meta = event.ctrlKey || event.metaKey;
    if (meta && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) doRedo();
      else doUndo();
      return;
    }
    if (meta && event.key.toLowerCase() === "y") {
      event.preventDefault();
      doRedo();
      return;
    }
    if (meta && event.key.toLowerCase() === "f") {
      event.preventDefault();
      doReplace();
      return;
    }
    if (event.key === " ") {
      event.preventDefault();
      togglePlay();
      return;
    }
    if (event.key === "Enter" && state.activeId) {
      event.preventDefault();
      state.editingId = state.activeId;
      renderRows(true);
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      doDelete();
      return;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      var step = (event.shiftKey ? 5000 : 1000) * (event.key === "ArrowLeft" ? -1 : 1);
      seekTo(Math.max(0, currentTimeMs() + step));
      return;
    }
    if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      var index = indexOfId(state.activeId);
      var next = index + (event.key === "ArrowDown" ? 1 : -1);
      if (next >= 0 && next < state.segments.length) {
        state.selected = {};
        state.selected[state.segments[next].id] = true;
        setActive(state.segments[next].id);
        renderRows(true);
      }
      return;
    }
    if (event.key.toLowerCase() === "s") {
      event.preventDefault();
      doSplit();
      return;
    }
    if (event.key.toLowerCase() === "m") {
      event.preventDefault();
      doMerge();
    }
  }

  // ------------------------------------------------------------ 媒体与状态

  function mountMedia() {
    var host = document.getElementById("sub-video");
    if (!host || !state.project) return;
    host.innerHTML = "";
    var source = null;
    (state.project.assets || []).forEach(function (asset) {
      if (asset.kind === "source") source = asset;
    });
    if (!source || !source.path) {
      host.appendChild(h("p", { class: "muted", text: "找不到源文件，只能编辑文本" }));
      return;
    }
    if (video && videoProjectId === state.projectId) {
      // 同一项目同页重挂：复用播放器，保留播放位置
      host.appendChild(video);
      return;
    }
    if (video) disposeMedia();
    video = h("video", {
      class: "sub-video",
      src: WB.mediaUrl(source.path),
      controls: "controls",
      preload: "metadata",
      ontimeupdate: function () {
        var now = currentTimeMs();
        if (nodes.time) nodes.time.textContent = ms(now);
        if (wave) wave.setTime(now);
        if (!rafPending) {
          rafPending = true;
          window.requestAnimationFrame(function () {
            rafPending = false;
            syncActiveToTime(now);
          });
        }
      },
      onloadedmetadata: function () {
        if (nodes.note) {
          nodes.note.textContent = "时长 " + shortMs((video.duration || 0) * 1000) +
            " · 共 " + state.segments.length + " 段";
        }
        mountTicks();
      },
      onerror: function () {
        if (nodes.note) nodes.note.textContent = "浏览器无法直接播放这个文件（编码不支持），时间轴仍可用";
      }
    });
    videoProjectId = state.projectId;
    host.appendChild(video);
  }

  function mountTicks() {
    if (!nodes.ticksHost) return;
    var duration = video && isFinite(video.duration) ? video.duration * 1000 : 0;
    if (!duration && state.segments.length) {
      duration = state.segments[state.segments.length - 1].end_ms;
    }
    if (!duration || !WB.waveform) return;
    nodes.ticksHost.innerHTML = "";
    var ticks = WB.waveform.timeline(duration);
    while (ticks.firstChild) nodes.ticksHost.appendChild(ticks.firstChild);
  }

  function togglePlay() {
    if (!video) return;
    if (video.paused) video.play().catch(function () {});
    else video.pause();
  }

  function paint() {
    if (!nodes.page) return;
    paintProjectBar();
    paintNotice();
    updateToolbar();
    updateStatus();
    renderRows(true);
  }

  function updateToolbar() {
    var host = nodes.page && nodes.page.querySelector(".sub-toolbar");
    if (!host) return;
    var undoBtn = host.children[5];
    var redoBtn = host.children[6];
    if (undoBtn) undoBtn.disabled = !state.undo.length;
    if (redoBtn) redoBtn.disabled = !state.redo.length;
  }

  function updateStatus() {
    if (!nodes.status) return;
    var parts = [];
    if (state.loading) parts.push("载入中…");
    else if (state.saving) parts.push("保存中…");
    else if (state.dirty) parts.push("有未保存的改动");
    else if (state.savedAt) parts.push("已保存 " + state.savedAt);
    else parts.push("就绪");
    if (state.pending) parts.push("处理中 " + state.pending);
    nodes.status.textContent = parts.join(" · ");

    if (nodes.stats && state.stats) {
      var stats = state.stats;
      nodes.stats.textContent =
        stats.count + " 段 · " + stats.chars + " 字 · " +
        (stats.chars_per_minute ? stats.chars_per_minute + " 字/分" : "") +
        (state.issues ? " · 质检 " + state.issues : "");
    }
  }

  // 导出给测试用的内部把手：不改变生产行为
  WB.pages = WB.pages || {};
  WB.pages.subtitles = {
    render: render,
    state: state,
    _internals: {
      indexQc: indexQc,
      renderRows: renderRows,
      shortMs: shortMs,
      ms: ms,
      selectRange: selectRange,
      rowHeight: ROW_HEIGHT,
      doExport: doExport,
      startExport: startExport,
      flushDraft: flushDraft,
      switchProject: switchProject,
      defaultProjectId: defaultProjectId,
      disposeMedia: disposeMedia
    }
  };

  WB.router.define("subtitles", {
    title: "字幕",
    crumb: "转录文本",
    icon: "subtitles",
    nav: true,
    render: render,
    destroy: destroy
  });
})();
