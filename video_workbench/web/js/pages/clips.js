/* ============================================================================
 * pages/clips.js —— 切片台（P3-03）
 *
 * 三栏：左候选卡片 / 中播放器 + 时间轴 + 检查器 / 右导出队列与规格。
 *
 * 五条不能破的规矩：
 * 1. **播放器从属时间轴**：拖动改的是时间轴上的候选区间，松手才写库；
 *    播放只负责"让人听见这是哪儿"。
 * 2. **时间码一律毫秒进、毫秒出**，只在显示时格式化成 `mm:ss.mmm`。
 * 3. **兜底候选要说清楚**：`notes.origin === "rule"` 的卡片打「兜底」标，
 *    并写明"位置可用、标题与分数没有内容含义"——不能让规则结果冒充模型判断。
 * 4. **吸不上边界要提示**：`notes.boundary` 为空时检查器里写「切点不在句边界」。
 * 5. **锁定的候选改不动**：按钮变「解锁」，切点输入只读——重跑分析也不会冲掉它
 *    （后端 `candidate_clear` 保留 locked）。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  var state = {
    projects: [],
    projectId: "",
    data: null,
    selectedId: "",
    peaks: [],
    playing: false,
    frameMs: 40,
    preRoll: true,
    previewFailed: "",
    busy: false,
    jobId: "",
    exports: [],
    exportBusy: false,
    exportMode: "precise",
    subtitleMode: "none",
    aspect: "16x9",
    verticalMode: "center",
    cropX: 0.5,
    safeArea: false,
    safeAreaBox: { top: 120, bottom: 260 },
    focus: [],
    focusDetector: "",
    focusBusy: false,
    focusNote: "",
    // P6-02 跟随增强：多主体、切点、是否开增强
    focusSubjects: [],
    focusSubject: "",
    focusCuts: 0,
    followEnhance: true,
    bgm: "",
    bgmVolume: 25,
    bgmDuck: true,
    fadeMs: 200,
    // P6-04 三通道交付：剪映草稿 / 达芬奇 OTIO / 降级包
    nle: null,
    nleLoading: false,
    placeInApp: false,
    deliverBusy: "",
    deliverNote: "",
    deliverResult: null,
    copy: {},
    copyBusy: false,
    copyNote: "",
    coverFrames: [],
    coverBusy: false,
    exportNote: "",
    // 批量导出（P3-09）：勾选集合、提交中标记、这批的回执
    picked: {},
    batchBusy: false,
    batchNote: "",
    note: "",
    error: ""
  };

  var player = null;
  var timeline = null;
  var els = {};

  // ------------------------------------------------------------------ 工具

  function pad(n, width) {
    var text = String(Math.abs(Math.round(n)));
    while (text.length < width) text = "0" + text;
    return text;
  }

  /** 毫秒 → `mm:ss.mmm`（超过一小时给 `h:mm:ss.mmm`）。 */
  function fmt(ms) {
    var total = Math.max(0, Math.round(Number(ms) || 0));
    var seconds = Math.floor(total / 1000);
    var millis = total % 1000;
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var rest = seconds % 60;
    var body = pad(rest, 2) + "." + pad(millis, 3);
    return hours ? hours + ":" + pad(minutes, 2) + ":" + body : pad(minutes, 2) + ":" + body;
  }

  /** `mm:ss.mmm` / `12.5` / `12500` 都收——用户会按自己顺手的方式敲。 */
  function parseMs(text) {
    var raw = String(text || "").trim();
    if (!raw) return NaN;
    if (raw.indexOf(":") === -1) {
      var plain = Number(raw);
      return Number.isFinite(plain) ? Math.round(plain * 1000) : NaN;
    }
    var parts = raw.split(":");
    var seconds = 0;
    parts.forEach(function (piece) {
      seconds = seconds * 60 + (Number(piece) || 0);
    });
    return Math.round(seconds * 1000);
  }

  /** 一帧多少毫秒。后端 `clip.candidates` 给的是真帧长；拿不到按 25fps 估算。 */
  function frameMs() {
    var got = Number(state.data && state.data.frame_ms);
    return got > 0 ? got : 40;
  }

  function selected() {
    var rows = (state.data && state.data.candidates) || [];
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].id === state.selectedId) return rows[i];
    }
    return null;
  }

  function originOf(cand) {
    var notes = (cand && cand.notes) || {};
    return String(notes.origin || cand.source || "ai");
  }

  function kindName(kind) {
    return kind === "segment" ? "长段" : "高光";
  }

  // ------------------------------------------------------------------ 数据

  function loadProjects() {
    return WB.rpc("library.list", { limit: 60 }, { silent: true }).then(function (data) {
      state.projects = (data && data.items) || [];
      if (!state.projectId && state.projects.length) {
        var withTranscript = state.projects.filter(function (item) {
          return item.has_transcript;
        });
        state.projectId = (withTranscript[0] || state.projects[0]).id;
      }
      return state.projects;
    });
  }

  function loadProject() {
    if (!state.projectId) return Promise.resolve(null);
    return WB.rpc("clip.candidates", { id: state.projectId }, { silent: true }).then(function (data) {
      state.data = data;
      var rows = data.candidates || [];
      if (!rows.some(function (item) {
        return item.id === state.selectedId;
      })) {
        state.selectedId = rows.length ? rows[0].id : "";
      }
      state.peaks = [];
      return WB.rpc("audio.peaks", { project_id: state.projectId }, { silent: true })
        .then(function (peaks) {
          state.peaks = (peaks && peaks.peaks) || [];
        })
        .catch(function () {
          state.peaks = [];
        })
        .then(function () {
          return data;
        });
    });
  }

  function load() {
    state.error = "";
    return loadProjects()
      .then(loadProject)
      .then(function () {
        repaint();
        syncSelection(true);
        loadExports();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "加载失败";
        repaint();
      });
  }

  function pollJob(jobId) {
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      WB.rpc("job.get", { id: jobId }, { silent: true }).then(function (job) {
        if (!job) return;
        state.note = job.stage ? "生成中：" + job.stage : "生成中…";
        if (job.progress) state.note += " " + Math.round(job.progress * 100) + "%";
        renderNote();
        if (job.status === "succeeded" || job.status === "failed" || job.status === "canceled") {
          clearInterval(timer);
          state.jobId = "";
          state.busy = false;
          if (job.status === "succeeded") {
            // P6-01：精析有没有跑、改了几条，得在这里说清楚。
            // 不然"候选已更新"和"精析跑了但一条没改"在界面上长得一模一样。
            var res = job.result || {};
            var note = res.notes && res.notes.length ? String(res.notes[0]) : "";
            if (res.refined) {
              state.note = "候选已更新（画面精析改了 " + res.refined + " 条）";
            } else {
              state.note = note || "候选已更新";
            }
            load();
          } else {
            state.note = "";
            state.error = job.error_message || "生成失败";
            render();
          }
        }
      });
      if (tries > 600) {
        clearInterval(timer);
        state.busy = false;
        state.jobId = "";
        state.note = "";
        state.error = "等太久了，看看状态栏里的任务是不是卡住了";
        render();
      }
    }, 800);
  }

  function analyze() {
    if (!state.projectId || state.busy) return;
    state.busy = true;
    state.error = "";
    state.note = "提交中…";
    renderNote();
    renderActions();
    WB.rpc("clip.analyze", { project_id: state.projectId, count: 12 })
      .then(function (data) {
        state.jobId = (data.job && data.job.id) || "";
        state.note = "生成中…";
        if (state.jobId) pollJob(state.jobId);
      })
      .catch(function (err) {
        state.busy = false;
        state.error = (err && err.message) || "提交失败";
        render();
      });
  }

  // ------------------------------------------------------------------ 播放与选择

  function sourceUrl() {
    var path = (state.data && state.data.source_path) || "";
    return path ? WB.mediaUrl(path) : "";
  }

  function syncSelection(reset) {
    var cand = selected();
    if (!player) return;
    if (els.playerHint) {
      var playable = Boolean(sourceUrl()) && !state.previewFailed;
      els.playerHint.style.display = playable ? "none" : "";
      if (!sourceUrl()) els.playerHint.textContent = "这个项目还没有可播放的源文件。";
    }
    if (player.setSafeArea) {
      player.setSafeArea(state.aspect === "9x16" && state.safeArea, safeArea());
    }
    if (!player.hasVideo() && sourceUrl()) player.load(sourceUrl());
    if (timeline) {
      timeline.setData({
        duration: (state.data && state.data.duration_ms) || 1,
        boundaries: (state.data && state.data.boundaries) || [],
        peaks: state.peaks,
        candidate: cand,
        time: cand ? cand.start_ms : 0
      });
      timeline.resize();
    }
    if (cand && reset) {
      player.seek(cand.start_ms);
      if (timeline) timeline.setTime(cand.start_ms);
    }
    paintRows();
    paintInspector();
  }

  function select(id, playIt) {
    state.selectedId = id;
    state.coverFrames = [];
    state.copyNote = "";
    syncSelection(true);
    buildExportForm();
    paintCopy();
    loadFocus().then(function () {
      buildExportForm();
    });
    if (playIt) {
      playSelected();
    }
  }

  /** 播当前候选；预卷开着就提前 3 秒起，让人听见切点前面那句话。 */
  function playSelected() {
    var cand = selected();
    if (!cand || !player) return;
    player.playRange(cand.start_ms, cand.end_ms, {
      preRollMs: state.preRoll ? 3000 : 0
    });
  }

  /** 改切点：先本地更新，再写库；失败要把值退回去。 */
  function commitCut(next, options) {
    var cand = selected();
    if (!cand) return Promise.resolve(null);
    var opts = options || {};
    var before = { start_ms: cand.start_ms, end_ms: cand.end_ms };
    cand.start_ms = next.start_ms;
    cand.end_ms = next.end_ms;
    paintRows();
    paintInspector();
    if (opts.skipSave) return Promise.resolve(cand);
    var payload = { id: cand.id, start_ms: cand.start_ms, end_ms: cand.end_ms };
    if (opts.alignFrame) payload.align = "frame";
    return WB.rpc("clip.update", payload, { silent: true })
      .then(function (data) {
        if (data && data.candidate) {
          Object.keys(data.candidate).forEach(function (key) {
            cand[key] = data.candidate[key];
          });
        }
        state.note = next.snapped ? "已贴到句边界并保存" : "已保存";
        renderNote();
        paintRows();
        paintInspector();
        return cand;
      })
      .catch(function (err) {
        cand.start_ms = before.start_ms;
        cand.end_ms = before.end_ms;
        state.error = (err && err.message) || "保存失败";
        render();
        return null;
      });
  }

  function stepFrame(frames, which) {
    var cand = selected();
    if (!cand || !timeline) return;
    var edge = which === "end" ? cand.end_ms : cand.start_ms;
    var moved = timeline.moveHandle(which, edge + frames * frameMs());
    if (!moved) return;
    commitCut(moved, { alignFrame: true });
    if (player) player.seek(which === "end" ? moved.end_ms : moved.start_ms);
  }

  function updateFields() {
    var cand = selected();
    if (!cand) return Promise.resolve(null);
    var start = parseMs(els.start.value);
    var end = parseMs(els.end.value);
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      state.error = "时间码看不懂，试试 00:12.400 或直接写秒数";
      render();
      return Promise.resolve(null);
    }
    if (end - start < 400) {
      state.error = "切片至少要 0.4 秒";
      render();
      return Promise.resolve(null);
    }
    var moved = timeline ? timeline.moveHandle("start", start) : null;
    if (timeline) timeline.moveHandle("end", end);
    return commitCut({
      start_ms: start,
      end_ms: end,
      snapped: Boolean(moved && moved.snapped)
    });
  }

  function setStatus(status) {
    var cand = selected();
    if (!cand) return;
    WB.rpc("clip.update", { id: cand.id, status: status })
      .then(function (data) {
        if (data && data.candidate) {
          Object.keys(data.candidate).forEach(function (key) {
            cand[key] = data.candidate[key];
          });
        }
        state.note = status === "locked" ? "已锁定，重跑分析也不会覆盖它" : "已解锁";
        renderNote();
        paintRows();
        paintInspector();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "没改成";
        render();
      });
  }

  function createManual() {
    if (!state.projectId) return;
    var now = player ? player.currentMs() : 0;
    var start = Math.max(0, now - 1000);
    WB.rpc("clip.create", {
      project_id: state.projectId,
      kind: "highlight",
      start_ms: start,
      end_ms: start + 20_000,
      title: "手动切片"
    })
      .then(function (data) {
        var cand = data && data.candidate;
        if (cand) state.selectedId = cand.id;
        state.note = "手动切片已加入";
        return load();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "新建失败";
        render();
      });
  }

  function removeCandidate() {
    var cand = selected();
    if (!cand) return;
    WB.rpc("clip.delete", { id: cand.id })
      .then(function () {
        state.selectedId = "";
        state.note = "已删除";
        return load();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "删除失败";
        render();
      });
  }

  // ------------------------------------------------------------------ 渲染

  /**
   * 重排骨架（不动持久节点）。
   *
   * `els.playerHost` / `els.tlHost` 是同一批节点，被 paint() 挪进新骨架里，
   * 里面的 <video> 与画布不重建——刷新候选不该把正在播的片打断。
   */
  function repaint() {
    paint();
  }

  function renderNote() {
    if (!els.note) return;
    els.note.textContent = state.note || "";
  }

  function paintRows() {
    if (!els.list) return;
    var rows = (state.data && state.data.candidates) || [];
    els.list.textContent = "";
    if (!rows.length) {
      els.list.appendChild(
        ui.empty(
          "还没有候选",
          (state.data && state.data.has_transcript)
            ? "点右上角「生成候选」，AI 会按 高光 / 长段 给出带理由与分数的片段。"
            : "这个项目还没有字幕，先去转录。"
        )
      );
      return;
    }
    rows.forEach(function (cand) {
      var active = cand.id === state.selectedId;
      var origin = originOf(cand);
      var card = ui.h("div", {
        class: "clip-card" + (active ? " is-active" : ""),
        "data-id": cand.id,
        role: "button",
        tabindex: "0"
      }, [
        ui.h("div", { class: "clip-card__top" }, [
          ui.h("input", {
            class: "clip-card__pick",
            type: "checkbox",
            checked: !!state.picked[cand.id],
            "aria-label": "选中 " + (cand.title || "这条切片")
          }),
          ui.h("span", { class: "clip-card__score", text: String(cand.score || 0) }),
          ui.h("span", { class: "clip-card__title", text: cand.title || "（没有标题）" }),
          cand.status === "locked"
            ? ui.h("span", { class: "pill pill--ok", text: "已锁定" })
            : null,
          origin === "rule" ? ui.h("span", { class: "pill pill--warn", text: "兜底" }) : null
        ]),
        ui.h("div", { class: "clip-card__meta" }, [
          ui.h("span", { class: "mono", text: fmt(cand.start_ms) + " → " + fmt(cand.end_ms) }),
          ui.h("span", {
            class: "clip-card__dur mono",
            text: ((cand.end_ms - cand.start_ms) / 1000).toFixed(1) + "s"
          }),
          ui.h("span", { class: "clip-card__kind", text: kindName(cand.kind) })
        ]),
        cand.reason ? ui.h("div", { class: "clip-card__reason", text: cand.reason }) : null
      ]);
      card.addEventListener("click", function () {
        select(cand.id, false);
      });
      card.addEventListener("dblclick", function () {
        select(cand.id, true);
      });
      // 勾选只影响批量导出，不该顺带把这条选中——两件事分开，点勾时别冒泡到卡片
      var pickBox = card.querySelector(".clip-card__pick");
      if (pickBox) {
        pickBox.addEventListener("click", function (ev) {
          ev.stopPropagation();
        });
        pickBox.addEventListener("change", function (ev) {
          ev.stopPropagation();
          togglePick(cand.id, !!pickBox.checked);
        });
      }
      els.list.appendChild(card);
    });
    paintPicks();
  }

  // ------------------------------------------------------ 批量导出（P3-09）

  function pickedIds() {
    var rows = (state.data && state.data.candidates) || [];
    return rows
      .map(function (cand) {
        return cand.id;
      })
      .filter(function (id) {
        return !!state.picked[id];
      });
  }

  function togglePick(id, on) {
    if (!id) return;
    if (on) state.picked[id] = true;
    else delete state.picked[id];
    paintPicks();
  }

  function pickAll(on) {
    var rows = (state.data && state.data.candidates) || [];
    state.picked = {};
    if (on) {
      rows.forEach(function (cand) {
        state.picked[cand.id] = true;
      });
    }
    paintRows();
  }

  /** 候选栏顶部那条：已选 N 条 + 全选/清空 + 批量导出。 */
  function paintPicks() {
    if (!els.pickBar) return;
    var ids = pickedIds();
    els.pickBar.textContent = "";
    els.pickBar.appendChild(
      ui.h("span", { class: "clip-picks__count", text: "已选 " + ids.length + " 条" })
    );
    els.pickBar.appendChild(
      ui.h("button", {
        class: "btn btn--ghost btn--tiny",
        text: "全选",
        onclick: function () {
          pickAll(true);
        }
      })
    );
    els.pickBar.appendChild(
      ui.h("button", {
        class: "btn btn--ghost btn--tiny",
        text: "清空",
        onclick: function () {
          pickAll(false);
        }
      })
    );
    els.pickBar.appendChild(
      ui.h("button", {
        class: "btn btn--tiny",
        text: state.batchBusy ? "提交中…" : "批量导出 " + (ids.length || ""),
        disabled: state.batchBusy || !ids.length,
        onclick: function () {
          batchExport();
        }
      })
    );
    if (state.batchNote) {
      els.pickBar.appendChild(
        ui.h("span", { class: "clip-picks__note", text: state.batchNote })
      );
    }
  }

  /** 勾上的一起导出：一条一个 job，并发由后端信号量（2）管。 */
  function batchExport() {
    var ids = pickedIds();
    if (!ids.length || state.batchBusy) return;
    state.batchBusy = true;
    state.batchNote = "";
    paintPicks();
    WB.rpc(
      "clip.export.batch",
      {
        project_id: state.projectId,
        candidate_ids: ids,
        mode: state.exportMode,
        subtitle_mode: state.subtitleMode,
        aspect: state.aspect,
        vertical_mode: state.verticalMode,
        crop_x: state.cropX,
        bgm: state.bgm,
        bgm_volume: state.bgmVolume,
        bgm_duck: state.bgmDuck,
        fade_ms: state.fadeMs
      },
      { silent: true }
    )
      .then(function (data) {
        state.batchBusy = false;
        state.picked = {};
        var submitted = (data && data.submitted) || 0;
        var skipped = (data && data.skipped) || [];
        state.batchNote =
          "已提交 " + submitted + " 条" + (skipped.length ? "，跳过 " + skipped.length + " 条" : "");
        paintRows();
        return loadExports().then(function () {
          return pollBatch((data && data.jobs) || []);
        });
      })
      .catch(function (err) {
        state.batchBusy = false;
        state.batchNote = "";
        state.error = (err && err.message) || "批量导出失败";
        render();
      });
  }

  /** 等这批里所有任务落地再停：队列每行状态来自任务，轮询整队比逐条轮询简单。 */
  function pollBatch(jobs) {
    var ids = (jobs || [])
      .map(function (row) {
        return row && row.job_id;
      })
      .filter(Boolean);
    if (!ids.length) return Promise.resolve(state.exports);
    var tries = 0;
    return new Promise(function (resolve) {
      function tick() {
        tries += 1;
        loadExports().then(function (rows) {
          var pending = (rows || []).filter(function (row) {
            return ids.indexOf(row.job_id) >= 0 && /queued|running/.test(String(row.status));
          });
          if (!pending.length || tries > 900) {
            resolve(rows);
            return;
          }
          window.setTimeout(tick, 900);
        });
      }
      tick();
    });
  }

  function paintInspector() {
    if (!els.inspector) return;
    var cand = selected();
    els.inspector.textContent = "";
    if (!cand) {
      els.inspector.appendChild(
        ui.h("div", { class: "page__hint", text: "左边选一条候选，这里能改切点。" })
      );
      return;
    }
    var locked = cand.status === "locked";
    var notes = cand.notes || {};
    var boundaryText = notes.boundary ? "切点已对齐句边界" : "切点不在句边界（磁吸没贴上）";

    els.start.value = fmt(cand.start_ms);
    els.end.value = fmt(cand.end_ms);
    els.start.disabled = locked;
    els.end.disabled = locked;
    els.apply.disabled = locked;

    els.inspector.appendChild(
      ui.h("div", { class: "clip-inspect" }, [
        ui.h("div", { class: "clip-inspect__row" }, [
          ui.h("label", { class: "field__label", text: "起点" }),
          els.start,
          ui.h("button", {
            class: "btn btn--ghost",
            text: "−1帧",
            disabled: locked,
            onclick: function () {
              stepFrame(-1, "start");
            }
          }),
          ui.h("button", {
            class: "btn btn--ghost",
            text: "+1帧",
            disabled: locked,
            onclick: function () {
              stepFrame(1, "start");
            }
          }),
          ui.h("button", {
            class: "btn btn--ghost",
            text: "定位",
            onclick: function () {
              if (player) player.seek(cand.start_ms);
              if (timeline) timeline.setTime(cand.start_ms);
            }
          })
        ]),
        ui.h("div", { class: "clip-inspect__row" }, [
          ui.h("label", { class: "field__label", text: "终点" }),
          els.end,
          ui.h("button", {
            class: "btn btn--ghost",
            text: "−1帧",
            disabled: locked,
            onclick: function () {
              stepFrame(-1, "end");
            }
          }),
          ui.h("button", {
            class: "btn btn--ghost",
            text: "+1帧",
            disabled: locked,
            onclick: function () {
              stepFrame(1, "end");
            }
          }),
          ui.h("span", {
            class: "clip-inspect__dur mono",
            text:
              ((cand.end_ms - cand.start_ms) / 1000).toFixed(2) +
              " 秒 · 一帧 " +
              frameMs() +
              "ms"
          })
        ]),
        ui.h("div", { class: "clip-inspect__row" }, [
          els.apply,
          ui.h("button", {
            class: "btn",
            text: locked ? "解锁" : "锁定切点",
            onclick: function () {
              setStatus(locked ? "draft" : "locked");
            }
          }),
          ui.h("button", {
            class: "btn btn--danger",
            text: "删除",
            onclick: removeCandidate
          }),
          ui.h("span", { class: "clip-inspect__note", text: boundaryText })
        ]),
        notes.origin === "rule"
          ? ui.h("div", {
              class: "callout callout--warn",
              text: "这条是规则兜底出来的：位置可用，但标题和分数没有内容含义。配好模型再生成一次会更准。"
            })
          : null,
        notes.edited_ms
          ? ui.h("div", {
              class: "clip-inspect__note",
              text: "手动改过切点；模型原值是 " + fmt(notes.raw_start_ms) + " → " + fmt(notes.raw_end_ms)
            })
          : null
      ])
    );
  }

  function renderActions() {
    if (!els.analyze) return;
    els.analyze.disabled = state.busy || !state.projectId;
    els.analyze.textContent = state.busy ? "生成中…" : "生成候选";
  }

  // ------------------------------------------------------------------ 导出

  var EXPORT_MODES = [
    { value: "precise", label: "精确（重编码）", hint: "切点准到 1 帧，慢" },
    { value: "fast", label: "快速（关键帧）", hint: "几乎瞬间，切点可能前移" }
  ];
  var ASPECTS = [
    { value: "16x9", label: "原画幅 16:9", hint: "横屏，直接用" },
    { value: "9x16", label: "竖屏 9:16", hint: "1080×1920，给抖音/小红书" }
  ];
  var VERTICAL_MODES = [
    { value: "center", label: "居中裁切", hint: "取中间一条竖条，取景位置可调" },
    { value: "blur", label: "模糊背景", hint: "整幅画面留着，上下补模糊背景" },
    { value: "follow", label: "人脸跟随", hint: "每 0.5 秒找一次人脸，可手工改" }
  ];
  var SUBTITLE_MODES = [
    { value: "none", label: "不烧字幕", hint: "成片干净，字幕单独给" },
    { value: "hard", label: "硬烧字幕", hint: "把字幕画进画面，要重编码" },
    { value: "soft", label: "软字幕", hint: "mp4 里带字幕轨，样式会丢" }
  ];

  function selectField(label, items, value, onChange) {
    var select = ui.h(
      "select",
      { class: "select" },
      items.map(function (item) {
        return ui.h("option", { value: item.value, text: item.label });
      })
    );
    select.value = value;
    select.addEventListener("change", function () {
      onChange(select.value);
    });
    return ui.h("div", { class: "field" }, [
      ui.h("span", { class: "field__label", text: label }),
      select
    ]);
  }

  function hintOf(items, value) {
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].value === value) return items[i].hint;
    }
    return "";
  }

  function buildExportForm() {
    if (!els.exportForm) return;
    els.exportForm.textContent = "";
    var cand = selected();
    var disabled = !cand || state.exportBusy;
    els.exportForm.appendChild(
      ui.h("div", { class: "exp-form__row" }, [
        selectField("字幕", SUBTITLE_MODES, state.subtitleMode, function (value) {
          state.subtitleMode = value;
          buildExportForm();
        }),
        selectField("模式", EXPORT_MODES, state.exportMode, function (value) {
          state.exportMode = value;
          buildExportForm();
        })
      ])
    );
    els.exportForm.appendChild(
      ui.h("div", { class: "exp-form__row" }, [
        selectField("画幅", ASPECTS, state.aspect, function (value) {
          state.aspect = value;
          if (player && player.setSafeArea) player.setSafeArea(value === "9x16" && state.safeArea);
          buildExportForm();
        }),
        state.aspect === "9x16"
          ? selectField("竖屏", VERTICAL_MODES, state.verticalMode, function (value) {
              state.verticalMode = value;
              buildExportForm();
            })
          : null
      ])
    );
    if (state.aspect === "9x16") {
      els.exportForm.appendChild(vmapRow());
      els.exportForm.appendChild(
        ui.h("p", { class: "exp-form__hint", text: hintOf(VERTICAL_MODES, state.verticalMode) })
      );
    }
    els.exportForm.appendChild(
      ui.h("p", { class: "exp-form__hint", text: hintOf(EXPORT_MODES, state.exportMode) })
    );
    els.exportForm.appendChild(audioRow());
    els.exportForm.appendChild(
      ui.h("button", {
        class: "btn btn--primary exp-form__go",
        text: state.exportBusy ? "导出中…" : "导出这一段",
        disabled: disabled,
        onclick: function () {
          startExport();
        }
      })
    );
    if (state.exportNote) {
      els.exportForm.appendChild(ui.h("p", { class: "exp-form__note", text: state.exportNote }));
    }
    els.exportForm.appendChild(deliverBlock());
  }

  /** 音轨设置：背景音乐（可留空）、音量、人声闪避、淡入淡出。P6-03。 */
  function audioRow() {
    var name = ui.h("span", {
      class: "exp-form__bgm-name",
      text: state.bgm ? baseName(state.bgm) : "不配背景音乐"
    });
    var pick = ui.h("button", {
      class: "btn btn--tiny exp-form__bgm-pick",
      text: state.bgm ? "换一首" : "选一首",
      disabled: state.exportBusy,
      onclick: function () {
        pickBgm();
      }
    });
    var clear = state.bgm
      ? ui.h("button", {
          class: "btn btn--tiny exp-form__bgm-clear",
          text: "去掉",
          onclick: function () {
            state.bgm = "";
            buildExportForm();
          }
        })
      : null;

    var volume = ui.h("input", {
      class: "exp-form__range exp-form__bgm-volume",
      type: "range",
      min: "0",
      max: "100",
      step: "5",
      "aria-label": "背景音乐音量"
    });
    volume.value = String(state.bgmVolume);
    volume.addEventListener("input", function () {
      state.bgmVolume = Number(volume.value);
      paintVolumeText();
    });

    var duck = ui.h("input", { type: "checkbox", class: "exp-form__bgm-duck" });
    duck.checked = state.bgmDuck;
    duck.addEventListener("change", function () {
      state.bgmDuck = Boolean(duck.checked);
      paintVolumeText();
    });

    var fade = ui.h("input", {
      class: "field__input exp-form__fade",
      type: "number",
      min: "0",
      max: "3000",
      step: "50",
      "aria-label": "淡入淡出毫秒"
    });
    fade.value = String(state.fadeMs);
    fade.addEventListener("change", function () {
      var value = Number(fade.value);
      state.fadeMs = isFinite(value) && value > 0 ? Math.round(value) : 0;
      fade.value = String(state.fadeMs);
    });

    var node = ui.h("div", { class: "exp-form__audio" }, [
      ui.h("div", { class: "exp-form__row" }, [
        ui.h("div", { class: "exp-form__bgm" }, [
          ui.h("span", { class: "field__label", text: "背景音乐" }),
          name,
          pick,
          clear
        ]),
        ui.h("div", { class: "exp-form__range-wrap" }, [
          ui.h("span", { class: "field__label", text: "音量" }),
          volume
        ])
      ]),
      ui.h("div", { class: "exp-form__row" }, [
        ui.h("label", { class: "exp-form__check" }, [duck, ui.h("span", { text: "人声闪避" })]),
        ui.h("div", { class: "exp-form__range-wrap" }, [
          ui.h("span", { class: "field__label", text: "淡入淡出 ms" }),
          fade
        ])
      ]),
      ui.h("p", { class: "exp-form__hint exp-form__audio-hint" })
    ]);
    els.volumeText = node.querySelector(".exp-form__audio-hint");
    paintVolumeText();
    return node;
  }

  function paintVolumeText() {
    if (!els.volumeText) return;
    var bits = ["音量 " + state.bgmVolume + "%"];
    bits.push(state.bgmDuck ? "人声一出来自动压低" : "不闪避（原样混着）");
    if (state.fadeMs > 0) bits.push("淡入淡出 " + state.fadeMs + " ms");
    if (!state.bgm) bits.push("没配背景音乐时只压原声与淡入淡出");
    els.volumeText.textContent = bits.join(" · ");
  }

  function baseName(path) {
    var text = String(path || "");
    var cut = Math.max(text.lastIndexOf("/"), text.lastIndexOf("\\"));
    return cut >= 0 ? text.slice(cut + 1) : text;
  }

  function pickBgm() {
    WB.rpc("fs.pick", { kind: "audio" }, { silent: true })
      .then(function (data) {
        var path = (data && data.path) || "";
        if (!path) return;
        state.bgm = path;
        state.exportNote = "已选背景音乐：" + baseName(path);
        buildExportForm();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "选文件失败";
        render();
      });
  }

  var DEFAULT_CHANNELS = [
    { key: "jianying", label: "剪映草稿" },
    { key: "otio", label: "达芬奇 OTIO" },
    { key: "bundle", label: "降级包" }
  ];

  /** 交付到剪辑软件（P6-04）：三个通道、可勾进草稿箱、结果给路径与说明。 */
  function deliverBlock() {
    if (!state.nle && !state.nleLoading) ensureNle();
    var info = state.nle || {};
    var channels = (info.channels && info.channels.length ? info.channels : DEFAULT_CHANNELS);
    var apps = info.apps || {};
    var jianying = apps.jianying || {};
    var cand = selected();
    var busy = Boolean(state.deliverBusy);

    var buttons = channels.map(function (item) {
      var label = state.deliverBusy === item.key ? "生成中…" : (item.label || item.key);
      return ui.h("button", {
        class: "btn btn--tiny exp-form__deliver-btn",
        "data-channel": item.key,
        text: label,
        title: item.note || "",
        disabled: !cand || busy,
        onclick: function () {
          startDeliver(item.key);
        }
      });
    });

    var rows = [
      ui.h("div", { class: "exp-form__deliver-head" }, [
        ui.h("span", { class: "field__label", text: "交付到剪辑软件" })
      ]),
      ui.h("div", { class: "exp-form__deliver-btns" }, buttons)
    ];

    if (jianying.found) {
      var place = ui.h("input", { type: "checkbox", class: "exp-form__place" });
      place.checked = state.placeInApp;
      place.addEventListener("change", function () {
        state.placeInApp = Boolean(place.checked);
      });
      rows.push(
        ui.h("label", { class: "exp-form__check exp-form__check--place" }, [
          place,
          ui.h("span", { text: "直接放进剪映草稿箱" })
        ])
      );
    }

    rows.push(
      ui.h("p", {
        class: "exp-form__hint",
        text: "草稿里字幕样式不跟随，想改样式回本应用改；竖屏取景已烘进画面。"
      })
    );

    if (state.deliverNote) {
      rows.push(ui.h("p", { class: "exp-form__deliver-note", text: state.deliverNote }));
    }

    var result = state.deliverResult;
    if (result) {
      rows.push(
        ui.h("div", { class: "exp-form__deliver-result" }, [
          ui.h("span", {
            class: "exp-form__deliver-path",
            text: result.path || "",
            title: result.path || ""
          }),
          ui.h("button", {
            class: "btn btn--tiny exp-form__deliver-open",
            text: "打开目录",
            onclick: function () {
              WB.rpc("fs.reveal", { path: result.path || "" }, { silent: true }).catch(
                function () {}
              );
            }
          })
        ])
      );
      (result.notes || []).forEach(function (note) {
        rows.push(ui.h("p", { class: "exp-form__hint exp-form__deliver-why", text: note }));
      });
    }
    return ui.h("div", { class: "exp-form__deliver" }, rows);
  }

  /** 通道信息只取一次；本机装没装剪映/达芬奇决定要不要显示"放进草稿箱"。 */
  function ensureNle() {
    if (state.nle || state.nleLoading) return Promise.resolve(state.nle);
    state.nleLoading = true;
    return WB.rpc("nle.info", {}, { silent: true })
      .then(function (data) {
        state.nleLoading = false;
        state.nle = data || {};
        buildExportForm();
        return state.nle;
      })
      .catch(function () {
        state.nleLoading = false;
        return null;
      });
  }

  function startDeliver(channel) {
    var cand = selected();
    if (!cand || state.deliverBusy) return;
    state.deliverBusy = channel;
    state.deliverResult = null;
    state.deliverNote = "已提交，正在生成…";
    buildExportForm();
    WB.rpc(
      "clip.deliver",
      {
        project_id: state.projectId,
        candidate_id: cand.id,
        channel: channel,
        place_in_app: Boolean(state.placeInApp)
      },
      { silent: true }
    )
      .then(function (data) {
        var jobId = data && data.job && data.job.id;
        if (!jobId) {
          state.deliverBusy = "";
          state.deliverNote = "";
          buildExportForm();
          return null;
        }
        return pollDeliver(jobId);
      })
      .catch(function (err) {
        state.deliverBusy = "";
        state.deliverNote = "";
        state.error = (err && err.message) || "交付失败";
        render();
      });
  }

  function pollDeliver(jobId) {
    var tries = 0;
    return new Promise(function (resolve) {
      function tick() {
        tries += 1;
        WB.rpc("job.get", { id: jobId }, { silent: true })
          .then(function (data) {
            var job = (data && data.job) || data || {};
            if (job.status === "failed" || job.status === "canceled") {
              state.deliverBusy = "";
              state.deliverNote = job.error_message || "交付失败";
              resolve(null);
              buildExportForm();
              return null;
            }
            if (job.status === "succeeded") {
              state.deliverBusy = "";
              state.deliverResult = job.result || null;
              state.deliverNote =
                job.result && job.result.placed_in_app
                  ? "已放进剪映草稿箱，打开剪映就能看到。"
                  : "生成好了，路径在下面。";
              resolve(state.deliverResult);
              buildExportForm();
              return null;
            }
            if (tries > 900) {
              state.deliverBusy = "";
              state.deliverNote = "等太久了，去任务列表看看进度。";
              resolve(null);
              buildExportForm();
              return null;
            }
            window.setTimeout(tick, 700);
            return null;
          })
          .catch(function () {
            window.setTimeout(tick, 1200);
            return null;
          });
      }
      tick();
    });
  }

  /** 取景位置滑块（0–1）。居中/跟随都靠它，拖动后立刻在预览里生效。 */
  function vmapRow() {
    var slider = ui.h("input", {
      class: "exp-form__range",
      type: "range",
      min: "0",
      max: "100",
      step: "1",
      "aria-label": "取景位置"
    });
    slider.value = String(Math.round(state.cropX * 100));
    slider.addEventListener("input", function () {
      state.cropX = Math.max(0, Math.min(1, Number(slider.value) / 100));
      paintVmapText();
    });
    // input 是每像素都触发，落库放到 change（松手）——别一次拖动写几十个关键帧
    slider.addEventListener("change", function () {
      saveManualFocus(state.cropX);
    });
    els.cropSlider = slider;
    var safe = ui.h("input", { type: "checkbox", "aria-label": "显示安全区" });
    safe.checked = state.safeArea;
    safe.addEventListener("change", function () {
      state.safeArea = Boolean(safe.checked);
      if (player && player.setSafeArea) {
        player.setSafeArea(state.aspect === "9x16" && state.safeArea, safeArea());
      }
    });
    els.safeToggle = safe;
    var row = ui.h("div", { class: "exp-form__vmap" }, [
      ui.h("div", { class: "exp-form__range-wrap" }, [
        ui.h("span", { class: "field__label", text: "取景位置" }),
        slider
      ]),
      ui.h("label", { class: "exp-form__check" }, [safe, ui.h("span", { text: "安全区" })]),
      state.verticalMode === "follow"
        ? ui.h("label", { class: "exp-form__check" }, [
            enhanceToggle(),
            ui.h("span", { text: "增强跟随" })
          ])
        : null,
      state.verticalMode === "follow"
        ? ui.h("button", {
            class: "btn btn--tiny exp-form__focus",
            text: state.focusBusy ? "检测中…" : "检测人脸",
            disabled: state.focusBusy,
            onclick: function () {
              analyzeFocus();
            }
          })
        : null
    ]);
    if (state.verticalMode === "follow") {
      var picker = subjectPicker();
      if (picker) row.appendChild(picker);
      row.appendChild(
        ui.h("p", {
          class: "exp-form__hint",
          text: focusText()
        })
      );
    }
    return row;
  }

  /** 「增强跟随」勾选框：关掉就回到 P3-06 的单脸最大跟随。 */
  function enhanceToggle() {
    var box = ui.h("input", {
      type: "checkbox",
      class: "exp-form__enhance",
      "aria-label": "增强跟随"
    });
    box.checked = state.followEnhance !== false;
    box.addEventListener("change", function () {
      state.followEnhance = Boolean(box.checked);
      buildExportForm();
      renderNote();
    });
    return box;
  }

  /** 多主体选择：只有一个主体时不占地方（没得选）。 */
  function subjectPicker() {
    var subjects = state.focusSubjects || [];
    if (subjects.length < 2 || state.followEnhance === false) return null;
    var select = ui.h("select", {
      class: "field__input exp-form__subject",
      "aria-label": "跟随哪个主体"
    });
    subjects.forEach(function (item) {
      var option = ui.h("option", {
        value: item.id,
        text: (item.label || item.id) + "（" + Math.round(item.score || 0) + " 分）"
      });
      option.value = item.id;
      if (item.id === state.focusSubject) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      switchSubject(select.value);
    });
    return ui.h("label", { class: "exp-form__check exp-form__check--subject" }, [
      ui.h("span", { class: "field__label", text: "跟谁" }),
      select
    ]);
  }

  /** 换主体：只换一条已经算好的轨迹，不重跑抽帧——点了就该立刻换。 */
  function switchSubject(subjectId) {
    if (!state.projectId || !subjectId || subjectId === state.focusSubject) return;
    state.focusNote = "正在换跟随对象…";
    buildExportForm();
    renderNote();
    WB.rpc("clip.focus.subject", { project_id: state.projectId, subject: subjectId }, { silent: true })
      .then(function (data) {
        state.focus = (data && data.focus) || [];
        state.focusSubject = (data && data.subject) || subjectId;
        state.focusSubjects = (data && data.subjects) || state.focusSubjects;
        state.focusCuts = ((data && data.cuts) || []).length;
        state.focusNote = "现在跟" + ((data && data.label) || subjectId);
        buildExportForm();
        renderNote();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "换主体失败";
        render();
      });
  }

  function paintVmapText() {
    if (!els.cropLabel) return;
    els.cropLabel.textContent = Math.round(state.cropX * 100) + "%";
  }

  function safeArea() {
    var data = state.data || {};
    return data.safe_area || state.safeAreaBox || { top: 120, bottom: 260 };
  }

  /** 跟随模式的人话说明：检测了几次、几个点、几个主体、有没有人改过。 */
  function focusText() {
    var points = state.focus || [];
    if (!points.length) {
      return state.followEnhance === false
        ? "增强已关闭：点「检测人脸」只跟最大那张脸（P3 的基础跟随）。"
        : "还没检测过：点「检测人脸」生成轨迹，多人画面会挑一个跟，检测不到就按居中出片。";
    }
    var manual = 0;
    points.forEach(function (point) {
      if (point.manual) manual += 1;
    });
    var detector = state.focusDetector || "检测";
    var bits = [
      "轨迹 " + points.length + " 个点",
      detector + (manual ? "，" + manual + " 个手工点" : "，全自动")
    ];
    if (state.focusCuts) bits.push(state.focusCuts + " 处镜头切换不插值");
    var subjects = state.focusSubjects || [];
    if (state.followEnhance !== false && subjects.length > 1) {
      var current = null;
      subjects.forEach(function (item) {
        if (item.id === state.focusSubject) current = item;
      });
      bits.push("跟" + ((current && current.label) || state.focusSubject || "打分最高的那个"));
    } else if (state.followEnhance === false) {
      bits.push("增强关闭");
    }
    return bits.join("，") + "。";
  }

  /** 拖动滑块时，跟随模式把当前位置记成一个手工关键帧——这就是"可手动修正"。 */
  function saveManualFocus(x) {
    if (!state.projectId || state.aspect !== "9x16" || state.verticalMode !== "follow") return;
    var at = Math.round((timeline && timeline.state ? timeline.state().time : 0) || 0);
    var points = (state.focus || []).filter(function (point) {
      return point.t_ms !== at;
    });
    points.push({ t_ms: at, x: x, manual: true });
    WB.rpc("clip.focus.save", { project_id: state.projectId, focus: points }, { silent: true })
      .then(function (data) {
        state.focus = (data && data.focus) || points;
        state.focusNote = "已记下第 " + Math.round(at / 1000) + " 秒的取景位置";
        buildExportForm();
        renderNote();
      })
      .catch(function (err) {
        state.error = (err && err.message) || "关键帧没存上";
        render();
      });
  }

  function analyzeFocus() {
    if (!state.projectId || state.focusBusy) return;
    state.focusBusy = true;
    state.focusNote = state.followEnhance === false
      ? "正在检测人脸（只跟最大那张脸）…"
      : "正在检测人脸（多人画面会分别跟）…";
    buildExportForm();
    WB.rpc(
      "clip.focus.analyze",
      { project_id: state.projectId, enhance: state.followEnhance !== false },
      { silent: true }
    )
      .then(function (data) {
        var jobId = data && data.job && data.job.id;
        return pollFocus(jobId);
      })
      .then(function () {
        state.focusBusy = false;
        return loadFocus();
      })
      .then(function () {
        state.focusBusy = false;
        buildExportForm();
      })
      .catch(function (err) {
        state.focusBusy = false;
        state.focusNote = "";
        state.error = (err && err.message) || "检测失败";
        render();
      });
  }

  function pollFocus(jobId) {
    if (!jobId) return Promise.resolve(null);
    return new Promise(function (resolve, reject) {
      var tries = 0;
      var tick = function () {
        tries += 1;
        WB.rpc("job.get", { id: jobId }, { silent: true })
          .then(function (data) {
            var row = (data && data.job) || data || {};
            if (row.status === "succeeded") return resolve(row);
            if (row.status === "failed" || row.status === "canceled") {
              return reject(new Error(row.error_message || "检测没有完成"));
            }
            if (tries > 600) return reject(new Error("检测超时"));
            setTimeout(tick, 800);
            return null;
          })
          .catch(reject);
      };
      tick();
    });
  }

  function loadFocus() {
    if (!state.projectId) return Promise.resolve(null);
    return WB.rpc("clip.focus", { project_id: state.projectId }, { silent: true })
      .then(function (data) {
        state.focus = (data && data.focus) || [];
        state.focusDetector = (data && data.detector) || "";
        state.focusSubjects = (data && data.subjects) || [];
        state.focusSubject = (data && data.subject) || "";
        state.focusCuts = ((data && data.cuts) || []).length;
        // 没检测过就没有 enhance 字段：保持界面默认值，别把开关拨回去
        if (data && typeof data.enhance === "boolean") state.followEnhance = data.enhance;
        if (data && data.safe_area) state.safeAreaBox = data.safe_area;
        return data;
      })
      .catch(function () {
        state.focus = [];
        state.focusSubjects = [];
        state.focusSubject = "";
        state.focusCuts = 0;
        return null;
      });
  }

  /** 队列行的画幅标记：竖屏要能一眼看出来，别把 9:16 当成 16:9 发出去。 */
  function exportMeta(row) {
    var bits = [];
    if (row.aspect === "9x16") {
      var names = { center: "居中", blur: "模糊背景", follow: "人脸跟随" };
      bits.push("竖屏·" + (names[row.vertical_mode] || row.vertical_mode || "居中"));
    } else {
      bits.push("原画幅");
    }
    if (row.mode === "fast") bits.push("快速");
    return bits.join(" · ");
  }

  function exportStatusText(row) {
    if (row.status === "succeeded") {
      return row.exists === false ? "完成（文件不在了）" : "完成";
    }
    if (row.status === "failed") return "失败";
    if (row.status === "canceled") return "已取消";
    if (row.status === "running") return "导出中";
    return "排队中";
  }

  function paintExports() {
    if (!els.exportList) return;
    els.exportList.textContent = "";
    var rows = state.exports || [];
    if (!rows.length) {
      els.exportList.appendChild(
        ui.empty("队列为空", "选一条候选，点「导出这一段」。每条一个任务，可同时跑两条。")
      );
      return;
    }
    var seenBatch = {};
    rows.forEach(function (row) {
      var failed = row.status === "failed";
      // 同一批的任务打一抬头：一批 3 条，2 成功 1 失败，比三行散着看清楚
      var batch = String(row.batch_id || "");
      if (batch && !seenBatch[batch]) {
        seenBatch[batch] = true;
        els.exportList.appendChild(batchHead(batch, rows));
      }
      var meta = [
        exportMeta(row),
        row.mode === "fast" ? "快速" : row.mode === "precise" ? "精确" : "",
        row.subtitle_mode === "hard" ? "硬烧" : row.subtitle_mode === "soft" ? "软字幕" : "",
        row.duration_ms ? (row.duration_ms / 1000).toFixed(2) + " 秒" : "",
        row.bytes ? Math.round(row.bytes / 1024) + " KB" : ""
      ]
        .filter(Boolean)
        .join(" · ");
      var line = ui.h("div", { class: "exp-row" }, [
        ui.h("div", { class: "exp-row__top" }, [
          ui.h("span", {
            class:
              "pill " +
              (failed ? "pill--warn" : row.status === "succeeded" ? "pill--ok" : ""),
            text: exportStatusText(row)
          }),
          ui.h("span", { class: "exp-row__name", text: row.name || row.error || "（没有文件名）" })
        ]),
        ui.h("div", { class: "exp-row__meta mono", text: meta })
      ]);
      if (row.note) {
        line.appendChild(ui.h("p", { class: "exp-row__note", text: row.note }));
      }
      if (failed && row.error) {
        line.appendChild(ui.h("p", { class: "exp-row__note", text: row.error }));
      }
      if (row.status === "succeeded" && row.path) {
        line.appendChild(
          ui.h("div", { class: "exp-row__acts" }, [
            ui.h("button", {
              class: "btn btn--ghost",
              text: "打开文件",
              onclick: function () {
                WB.rpc("fs.reveal", { path: row.path }, { silent: true }).catch(function () {});
              }
            }),
            ui.h("button", {
              class: "btn btn--ghost",
              text: "打开目录",
              onclick: function () {
                WB.rpc("fs.reveal", { path: row.dir || row.path }, { silent: true }).catch(
                  function () {}
                );
              }
            })
          ])
        );
      }
      if (failed && row.job_id) {
        line.appendChild(
          ui.h("div", { class: "exp-row__acts" }, [
            ui.h("button", {
              class: "btn btn--ghost",
              text: "重试",
              onclick: function () {
                WB.rpc("job.retry", { id: row.job_id }, { silent: true })
                  .then(function () {
                    return loadExports();
                  })
                  .catch(function (err) {
                    state.error = (err && err.message) || "重试失败";
                    render();
                  });
              }
            })
          ])
        );
      }
      els.exportList.appendChild(line);
    });
  }

  /** 一批导出的抬头：这批几条、几条成功、几条失败，失败的可一键重试。 */
  function batchHead(batchId, rows) {
    var mine = rows.filter(function (row) {
      return String(row.batch_id || "") === batchId;
    });
    var ok = mine.filter(function (row) {
      return row.status === "succeeded";
    }).length;
    var bad = mine.filter(function (row) {
      return row.status === "failed";
    }).length;
    var running = mine.filter(function (row) {
      return /queued|running/.test(String(row.status));
    }).length;
    var text = "这批 " + mine.length + " 条 · " + ok + " 成功";
    if (bad) text += " / " + bad + " 失败";
    if (running) text += " · " + running + " 进行中";
    var parts = [
      ui.h("span", { class: "exp-batch__text", text: text }),
      ui.h("span", { class: "exp-batch__id mono", text: batchId })
    ];
    if (bad) {
      parts.push(
        ui.h("button", {
          class: "btn btn--ghost btn--tiny",
          text: "重试失败项",
          onclick: function () {
            retryBatch(mine);
          }
        })
      );
    }
    return ui.h("div", { class: "exp-batch" }, parts);
  }

  /** 把这一批里失败的重提一遍（新建任务，失败历史留着）。 */
  function retryBatch(mine) {
    var bad = (mine || []).filter(function (row) {
      return row.status === "failed" && row.job_id;
    });
    if (!bad.length) return Promise.resolve([]);
    return Promise.all(
      bad.map(function (row) {
        return WB.rpc("job.retry", { id: row.job_id }, { silent: true })
          .then(function (data) {
            // job.retry 新建一个任务（旧的不动），新 id 才是要等的
            var job = (data && data.job) || data || {};
            return job.id || "";
          })
          .catch(function () {
            return "";
          });
      })
    )
      .then(function (newIds) {
        var ids = newIds.filter(Boolean);
        return loadExports().then(function () {
          return pollBatch(
            ids.map(function (id) {
              return { job_id: id };
            })
          );
        });
      });
  }

  function loadExports() {
    if (!state.projectId) {
      state.exports = [];
      paintExports();
      return Promise.resolve([]);
    }
    return WB.rpc("clip.exports", { project_id: state.projectId }, { silent: true })
      .then(function (data) {
        state.exports = (data && data.exports) || [];
        paintExports();
        return state.exports;
      })
      .catch(function () {
        state.exports = [];
        paintExports();
        return [];
      });
  }

  // ------------------------------------------------------ 文案与封面（P3-08）

  /** 当前候选的文案。后端把 copy 存在候选行上，跟着 clip.candidates 一起回来。 */
  function currentCopy() {
    var cand = selected();
    return (cand && cand.copy) || {};
  }

  function copyParts() {
    var copy = currentCopy();
    return {
      title: String(copy.title || ""),
      description: String(copy.description || ""),
      tags: (copy.tags || []).join(" ")
    };
  }

  /** 投稿用的一整块文本：标题一行，空行，简介，空行，话题词。 */
  function publishText() {
    var parts = copyParts();
    var lines = [parts.title, "", parts.description];
    if (parts.tags) lines.push("", parts.tags.split(/\s+/).map(function (tag) {
      return "#" + tag.replace(/^#/, "");
    }).join(" "));
    return lines.join("\n").trim();
  }

  function paintCopy() {
    if (!els.copyPane) return;
    els.copyPane.textContent = "";
    var cand = selected();
    if (!cand) {
      els.copyPane.appendChild(
        ui.h("p", { class: "copy-empty", text: "先左边选一条切片，再来写文案。" })
      );
      return;
    }
    var copy = currentCopy();
    var has = Boolean(copy.title);
    var parts = copyParts();

    // 标题 / 简介 / 标签：受控输入，改动只在本地，点保存才落库
    els.copyTitle = ui.h("input", {
      class: "input",
      type: "text",
      "aria-label": "标题",
      value: parts.title,
      placeholder: "标题（发布时用，30 字以内）"
    });
    els.copyDesc = ui.h("textarea", {
      class: "input copy-desc",
      rows: 4,
      "aria-label": "简介",
      placeholder: "简介（120 字以内，说清这段讲了什么）"
    });
    els.copyDesc.value = parts.description;
    els.copyTags = ui.h("input", {
      class: "input",
      type: "text",
      "aria-label": "标签",
      value: parts.tags,
      placeholder: "标签，空格分隔（3–5 个）"
    });

    var cover = copy.cover || {};
    // 有封面就看 at_ms/source，不看 path：mock 与"图还没落盘"时 path 是空的，
    // 但"取自哪一帧"这条信息仍然要说清楚。
    var hasCover = cover.at_ms !== undefined && cover.at_ms !== null;
    els.coverBox = ui.h("div", { class: "cover-box" }, [
      cover.path
        ? ui.h("img", {
            class: "cover-img",
            alt: "封面",
            src: WB.mediaUrl ? WB.mediaUrl(cover.path) : cover.path
          })
        : ui.h("div", {
            class: "cover-img cover-img--empty",
            text: hasCover ? "封面已选定" : "还没封面"
          }),
      ui.h("div", { class: "cover-meta" }, [
        ui.h("span", {
          class: "mono",
          text: hasCover
            ? (cover.source === "upload" ? "自己上传的图" : "取自 " + fmt(cover.at_ms)) +
              (cover.bytes ? " · " + Math.round(cover.bytes / 1024) + " KB" : "")
            : "封面默认取切点后 1 秒那一帧"
        })
      ])
    ]);

    els.coverFrames = ui.h("div", { class: "cover-frames" });
    (state.coverFrames || []).forEach(function (item) {
      // path 为空时（mock 或抽帧没落盘）不要塞个空 src 的 <img>：浏览器会画坏图标
      var face = item.path
        ? ui.h("img", {
            class: "cover-frame__img",
            alt: fmt(item.at_ms),
            src: WB.mediaUrl ? WB.mediaUrl(item.path) : item.path
          })
        : ui.h("div", { class: "cover-frame__img cover-frame__img--empty mono", text: fmt(item.at_ms) });
      els.coverFrames.appendChild(
        ui.h("button", {
          class: "cover-frame",
          type: "button",
          title: "用这一帧（" + fmt(item.at_ms) + "）",
          onclick: function () {
            pickCover(item.at_ms);
          }
        }, [face])
      );
    });

    els.copyPane.appendChild(
      ui.h("div", { class: "copy-form" }, [
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "标题" }),
          els.copyTitle
        ]),
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "简介" }),
          els.copyDesc
        ]),
        ui.h("div", { class: "field" }, [
          ui.h("span", { class: "field__label", text: "标签" }),
          els.copyTags
        ]),
        ui.h("div", { class: "copy-actions" }, [
          ui.h("button", {
            class: "btn btn--primary",
            text: state.copyBusy ? "写文案…" : has ? "重新生成" : "写文案",
            disabled: state.copyBusy,
            onclick: function () {
              writeCopy(true);
            }
          }),
          ui.h("button", {
            class: "btn",
            text: "保存修改",
            onclick: saveCopy
          }),
          ui.h("button", {
            class: "btn btn--ghost",
            text: "复制",
            onclick: copyToClipboard
          })
        ]),
        ui.h("p", {
          class: "copy-note",
          text:
            state.copyNote ||
            (has
              ? copy.source === "rule"
                ? "这条是兜底文案（模型没给出可用结果）：位置能用，措辞要自己改。"
                : "标题 " + parts.title.length + " 字 · 简介 " + parts.description.length + " 字" +
                  (copy.edited ? " · 你改过" : "")
              : "还没写文案。模型会按这段字幕拟标题、简介和标签。")
        }),
        ui.h("div", { class: "cover-block" }, [
          ui.h("div", { class: "cover-head" }, [
            ui.h("span", { class: "field__label", text: "封面" }),
            ui.h("div", { class: "cover-actions" }, [
              ui.h("button", {
                class: "btn btn--tiny",
                text: state.coverBusy ? "抽帧…" : "抽 4 帧",
                disabled: state.coverBusy,
                onclick: loadCoverFrames
              }),
              ui.h("button", {
                class: "btn btn--tiny",
                text: "上传图片",
                onclick: uploadCover
              })
            ])
          ]),
          els.coverBox,
          els.coverFrames
        ])
      ])
    );
  }

  /** 写文案；`regenerate` 为 false 且已有文案时后端会直接复用，不浪费请求。 */
  function writeCopy(regenerate) {
    var cand = selected();
    if (!cand || state.copyBusy) return;
    state.copyBusy = true;
    state.copyNote = "";
    paintCopy();
    WB.rpc(
      "clip.copy",
      { candidate_id: cand.id, regenerate: Boolean(regenerate) },
      { silent: true }
    )
      .then(function (data) {
        if (data && data.reused) {
          state.copyBusy = false;
          state.copyNote = "已经有文案了；要重写点「重新生成」。";
          paintCopy();
          return null;
        }
        var jobId = data && data.job && data.job.id;
        if (!jobId) {
          state.copyBusy = false;
          paintCopy();
          return null;
        }
        return pollCopy(jobId);
      })
      .catch(function (err) {
        state.copyBusy = false;
        state.copyNote = (err && err.message) || "写文案失败";
        paintCopy();
      });
  }

  function pollCopy(jobId) {
    var tries = 0;
    return new Promise(function (resolve) {
      function tick() {
        tries += 1;
        WB.rpc("job.get", { id: jobId }, { silent: true })
          .then(function (data) {
            var job = (data && data.job) || data || {};
            if (job.status === "succeeded" || job.status === "failed" || job.status === "canceled") {
              state.copyBusy = false;
              if (job.status === "failed") {
                state.copyNote = job.error_message || "写文案失败";
                paintCopy();
                resolve(null);
                return null;
              }
              return reloadCandidates().then(function () {
                paintCopy();
                resolve(null);
                return null;
              });
            }
            if (tries > 600) {
              state.copyBusy = false;
              paintCopy();
              resolve(null);
              return null;
            }
            window.setTimeout(tick, 800);
            return null;
          })
          .catch(function () {
            if (tries > 600) {
              state.copyBusy = false;
              paintCopy();
              resolve(null);
              return null;
            }
            window.setTimeout(tick, 1200);
            return null;
          });
      }
      tick();
    });
  }

  /** 写完文案后重新拉候选（文案存在候选行上，只能整批重读）。 */
  function reloadCandidates() {
    if (!state.projectId) return Promise.resolve(null);
    return WB.rpc("clip.candidates", { id: state.projectId }, { silent: true }).then(function (data) {
      if (data) {
        state.data = data;
      }
      syncSelection(false);
      return data;
    });
  }

  function saveCopy() {
    var cand = selected();
    if (!cand) return;
    var tags = String((els.copyTags && els.copyTags.value) || "")
      .split(/[\s,，、]+/)
      .filter(function (item) {
        return item;
      });
    WB.rpc(
      "clip.copy.save",
      {
        candidate_id: cand.id,
        title: (els.copyTitle && els.copyTitle.value) || "",
        description: (els.copyDesc && els.copyDesc.value) || "",
        tags: tags
      },
      { silent: true }
    )
      .then(function (data) {
        var problems = (data && data.problems) || [];
        state.copyNote = problems.length ? problems.join("；") : "已保存。";
        if (data && data.copy && cand) cand.copy = data.copy;
        paintCopy();
      })
      .catch(function (err) {
        state.copyNote = (err && err.message) || "保存失败";
        paintCopy();
      });
  }

  /** 复制投稿文本。没有 clipboard 权限（file:// 下常见）就退回选中 textarea。 */
  function copyToClipboard() {
    var text = publishText();
    if (!text) return;
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(area);
      state.copyNote = ok ? "文案已复制，可以直接去投稿页粘。" : "复制不了，手动选一下吧。";
      paintCopy();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        state.copyNote = "文案已复制，可以直接去投稿页粘。";
        paintCopy();
      }, fallback);
      return;
    }
    fallback();
  }

  function loadCoverFrames() {
    var cand = selected();
    if (!cand || state.coverBusy) return;
    state.coverBusy = true;
    paintCopy();
    WB.rpc("clip.cover.frames", { candidate_id: cand.id, count: 4 }, { silent: true })
      .then(function (data) {
        state.coverBusy = false;
        state.coverFrames = (data && data.frames) || [];
        state.copyNote = state.coverFrames.length
          ? "点一帧就能当封面。"
          : "没抽出帧，检查源视频是否还在。";
        paintCopy();
      })
      .catch(function (err) {
        state.coverBusy = false;
        state.copyNote = (err && err.message) || "抽帧失败";
        paintCopy();
      });
  }

  function pickCover(atMs) {
    var cand = selected();
    if (!cand) return;
    WB.rpc("clip.cover", { candidate_id: cand.id, at_ms: Math.round(atMs) }, { silent: true })
      .then(function (data) {
        if (data && data.copy && cand) cand.copy = data.copy;
        state.copyNote = "封面换成 " + fmt(atMs) + " 这一帧了。";
        paintCopy();
      })
      .catch(function (err) {
        state.copyNote = (err && err.message) || "换封面失败";
        paintCopy();
      });
  }

  function uploadCover() {
    var cand = selected();
    if (!cand) return;
    WB.rpc("fs.pick", { kind: "files" }, { silent: true })
      .then(function (data) {
        var path = (data && data.path) || "";
        if (!path) {
          state.copyNote = "没选文件。";
          paintCopy();
          return null;
        }
        return WB.rpc("clip.cover.upload", { candidate_id: cand.id, path: path }, { silent: true })
          .then(function (out) {
            if (out && out.copy && cand) cand.copy = out.copy;
            state.copyNote = "封面换成你上传的图了。";
            paintCopy();
            return null;
          });
      })
      .catch(function (err) {
        state.copyNote = (err && err.message) || "上传封面失败";
        paintCopy();
      });
  }

  function startExport() {
    var cand = selected();
    if (!cand || state.exportBusy) return;
    state.exportBusy = true;
    state.exportNote = "已提交，导出在后台跑；进度看右边队列。";
    buildExportForm();
    WB.rpc(
      "clip.export",
      {
        project_id: state.projectId,
        candidate_id: cand.id,
        mode: state.exportMode,
        subtitle_mode: state.subtitleMode,
        aspect: state.aspect,
        vertical_mode: state.verticalMode,
        crop_x: state.cropX,
        bgm: state.bgm,
        bgm_volume: state.bgmVolume,
        bgm_duck: state.bgmDuck,
        fade_ms: state.fadeMs
      },
      { silent: true }
    )
      .then(function (data) {
        var jobId = data && data.job && data.job.id;
        state.exportBusy = false;
        buildExportForm();
        paintExports();
        return loadExports().then(function () {
          if (jobId) return pollExport(jobId);
          return null;
        });
      })
      .catch(function (err) {
        state.exportBusy = false;
        state.exportNote = "";
        state.error = (err && err.message) || "导出失败";
        render();
      });
  }

  /** 等导出任务结束再刷队列：模式、切点偏移、时长都在任务结果里，不在资产表。 */
  function pollExport(jobId) {
    var tries = 0;
    return new Promise(function (resolve) {
      function tick() {
        tries += 1;
        WB.rpc("job.get", { id: jobId }, { silent: true })
          .then(function (data) {
            // job.get 有的实现回 {job:{...}}，有的直接回任务对象——两种都收
            var job = (data && data.job) || data || {};
            if (
              job.status === "succeeded" ||
              job.status === "failed" ||
              job.status === "canceled"
            ) {
              state.exportNote = "";
              resolve(loadExports());
              return null;
            }
            if (tries > 1200) {
              resolve(null);
              return null;
            }
            window.setTimeout(tick, 800);
            return null;
          })
          .catch(function () {
            if (tries > 1200) {
              resolve(null);
              return null;
            }
            window.setTimeout(tick, 1500);
            return null;
          });
      }
      tick();
    });
  }

  function renderPick() {
    if (!els.pick) return;
    var current = state.projectId;
    els.pick.textContent = "";
    var options = state.projects.map(function (item) {
      var label = item.title || item.bvid || item.id;
      if (item.collection_title) label = item.collection_title + " · " + label;
      return ui.h("option", { value: item.id, text: label, selected: item.id === current });
    });
    if (!options.length) options.push(ui.h("option", { value: "", text: "还没有素材" }));
    options.forEach(function (node) {
      els.pick.appendChild(node);
    });
    els.pick.value = current;
  }

  function renderActions() {
    if (!els.analyze) return;
    els.analyze.disabled = state.busy || !state.projectId;
    els.analyze.textContent = state.busy ? "生成中…" : "生成候选";
  }

  /**
   * 重排整页骨架。
   *
   * `els` 里的节点只建一次（播放器、时间轴、输入框都在里面），每次 paint 只是
   * 把它们摆进新的骨架里——appendChild 会把节点从旧父节点摘下，所以不会重复。
   * 这样刷新数据不会重建 <video>，播放进度与音频缓冲都还在。
   */
  function paint() {
    if (!els.root) return;
    els.root.textContent = "";

    var head = ui.h("div", { class: "page__head" }, [
      ui.h("h2", { class: "page__title", text: "切片台" }),
      ui.h("p", { class: "page__hint", text: "看候选 → 改切点 → 预览 → 导出" }),
      ui.h("div", { class: "page__actions" }, [
        els.pick,
        ui.h("button", {
          class: "btn btn--primary",
          text: "生成候选",
          onclick: analyze
        }),
        ui.h("button", { class: "btn", text: "手动新建", onclick: createManual })
      ])
    ]);
    // 按钮是骨架的一部分，每次 paint 都会换新节点，动作绑定在节点上
    els.analyze = head.querySelector(".btn--primary");
    els.root.appendChild(head);

    var count = ((state.data && state.data.candidates) || []).length;
    els.root.appendChild(
      ui.h("div", { class: "clip-grid" }, [
        ui.h("div", { class: "card clip-col" }, [
          ui.h("div", { class: "card__head" }, [
            ui.h("h3", { class: "card__title", text: "候选片段" }),
            ui.h("span", { class: "card__hint", text: count + " 条" })
          ]),
          els.pickBar,
          els.list
        ]),
        ui.h("div", { class: "clip-center" }, [
          ui.h("div", { class: "card" }, [
            ui.h("div", { class: "card__body clip-player" }, [els.playerHost, els.playerHint]),
            ui.h("div", { class: "clip-tl" }, [els.tlHost]),
            ui.h("div", { class: "clip-transport" }, [
              ui.h("button", {
                class: "btn",
                text: "播放这一段",
                onclick: function () {
                  var cand = selected();
                  if (cand && player) playSelected();
                }
              }),
              ui.h("button", {
                class: "btn btn--ghost",
                text: "暂停",
                onclick: function () {
                  if (player) player.pause();
                }
              }),
              els.preRoll,
              els.clock,
              els.note
            ])
          ]),
          ui.h("div", { class: "card" }, [
            ui.h("div", { class: "card__head" }, [
              ui.h("h3", { class: "card__title", text: "检查器" })
            ]),
            ui.h("div", { class: "card__body" }, [els.inspector])
          ])
        ]),
        ui.h("div", { class: "clip-col" }, [
          ui.h("div", { class: "card" }, [
            ui.h("div", { class: "card__head" }, [
              ui.h("h3", { class: "card__title", text: "文案与封面" })
            ]),
            ui.h("div", { class: "card__body" }, [els.copyPane])
          ]),
          ui.h("div", { class: "card" }, [
            ui.h("div", { class: "card__head" }, [
              ui.h("h3", { class: "card__title", text: "导出" })
            ]),
            ui.h("div", { class: "card__body" }, [els.exportForm])
          ]),
          ui.h("div", { class: "card" }, [
            ui.h("div", { class: "card__head" }, [
              ui.h("h3", { class: "card__title", text: "导出队列" })
            ]),
            ui.h("div", { class: "card__body" }, [els.exportList])
          ])
        ])
      ])
    );

    if (state.error) {
      els.root.appendChild(ui.h("div", { class: "callout callout--warn", text: state.error }));
    }

    renderPick();
    renderActions();
    renderNote();
    paintRows();
    paintInspector();
    buildExportForm();
    paintCopy();
    paintExports();
  }

  function build() {
    els.root = ui.h("section", { class: "page" });
    els.playerHost = ui.h("div", { class: "clip-player__host" });
    // 没有源文件（或还没下载）时，别给用户一块纯黑：说清楚为什么没画面。
    els.playerHint = ui.h("div", {
      class: "clip-player__hint",
      text: "这个项目还没有可播放的源文件。"
    });
    els.tlHost = ui.h("div", { class: "tl" });
    els.clock = ui.h("span", { class: "clip-transport__clock mono", text: "00:00.000" });
    // P3-04：预卷只影响预览起点，不改候选的 start_ms
    els.preRoll = ui.h("label", { class: "clip-transport__check" }, [
      ui.h("input", { type: "checkbox", "aria-label": "3 秒预卷" }),
      ui.h("span", { text: "3 秒预卷" })
    ]);
    els.preRoll.firstChild.checked = state.preRoll;
    els.preRoll.firstChild.addEventListener("change", function (ev) {
      state.preRoll = Boolean(ev.target && ev.target.checked);
      if (player && player.playRange) {
        state.note = state.preRoll ? "播放会从切点前 3 秒起" : "播放从切点起";
        renderNote();
      }
    });
    els.note = ui.h("span", { class: "clip-transport__note", text: "" });
    els.list = ui.h("div", { class: "clip-list" });
    els.pickBar = ui.h("div", { class: "clip-picks" });
    // 导出面板（P3-05）：节点一次建好，paint 时只换内容（select 重建会丢焦点）
    els.exportForm = ui.h("div", { class: "exp-form" });
    els.exportList = ui.h("div", { class: "exp-list" });
    // 文案与封面（P3-08）：同样一次建好，paint 只换内容
    els.copyPane = ui.h("div", { class: "copy-pane" });
    els.inspector = ui.h("div", { class: "clip-inspector" });
    els.start = ui.h("input", { class: "input mono", type: "text", "aria-label": "起点" });
    els.end = ui.h("input", { class: "input mono", type: "text", "aria-label": "终点" });
    els.apply = ui.h("button", {
      class: "btn btn--primary",
      text: "应用切点",
      onclick: updateFields
    });
    els.pick = ui.h("select", { class: "select clip-pick", "aria-label": "选择项目" });
    els.pick.addEventListener("change", function () {
      state.projectId = els.pick.value;
      state.selectedId = "";
      load();
    });
  }

  function attachPlayer() {
    if (!els.playerHost) return;
    if (player) {
      player.destroy();
      player = null;
    }
    player = WB.player.mount(els.playerHost, {
      onTime: function (ms) {
        els.clock.textContent = fmt(ms);
        els.clock.setAttribute("data-ms", String(ms));
        if (timeline && !timeline.state().drag) timeline.setTime(ms);
      },
      onState: function (info) {
        state.playing = Boolean(info && info.playing);
        if (info && info.error) {
          // 文件被挪走/编码不支持时 <video> 只会给你一块黑，必须自己说一句
          state.previewFailed = info.error;
          if (els.playerHint) {
            els.playerHint.textContent = "预览加载不了：" + info.error;
            els.playerHint.style.display = "";
          }
        }
      }
    });
  }

  function attachTimeline() {
    if (!els.tlHost) return;
    if (timeline) {
      timeline.destroy();
      timeline = null;
    }
    timeline = WB.timeline.mount(els.tlHost, {
      duration: (state.data && state.data.duration_ms) || 1,
      onSeek: function (ms) {
        if (player) player.seek(ms);
        if (timeline) timeline.setTime(ms);
      },
      onChange: function (next) {
        commitCut(next, { alignFrame: true });
      }
    });
  }

  // ------------------------------------------------------------------ 生命周期

  var resizeHandler = null;

  /**
   * 路由入口：返回根节点（路由负责插进 #view）。
   * 播放器与时间轴要在节点进 DOM 后才能量宽度，所以挂载排在微任务里。
   */
  function render$() {
    if (!els.root) build();
    paint();
    Promise.resolve().then(function () {
      attachPlayer();
      attachTimeline();
      syncSelection(true);
      if (player && player.setSafeArea) {
        player.setSafeArea(state.aspect === "9x16" && state.safeArea, safeArea());
      }
    });
    if (!resizeHandler) {
      resizeHandler = function () {
        if (timeline) timeline.resize();
      };
      window.addEventListener("resize", resizeHandler);
    }
    load();
    return els.root;
  }

  WB.pages = WB.pages || {};
  WB.pages.clips = {
    render: render$,
    _internals: {
      fmt: fmt,
      parseMs: parseMs,
      select: select,
      commitCut: commitCut,
      updateFields: updateFields,
      analyze: analyze,
      createManual: createManual,
      writeCopy: writeCopy,
      saveCopy: saveCopy,
      publishText: publishText,
      loadCoverFrames: loadCoverFrames,
      pickCover: pickCover,
      togglePick: togglePick,
      pickAll: pickAll,
      pickedIds: pickedIds,
      batchExport: batchExport,
      retryBatch: retryBatch,
      state: function () {
        return state;
      },
      timeline: function () {
        return timeline;
      },
      player: function () {
        return player;
      }
    }
  };

  WB.router.define("clipper", {
    title: "切片台",
    crumb: "候选与切点",
    icon: "scissors",
    nav: true,
    render: render$
  });
})();
