/* ============================================================================
 * pages/newtask.js —— 新建任务（P1-01 解析 → P1-02 下载）
 *
 * 交互只做一件事：**先把"要下哪几集"摆清楚，再让用户按开始。**
 *
 * 为什么不做"粘贴即下载"：一个合集链接可能带 40 集、几十 GB。用户粘错了链接
 * （把收藏夹当单稿）、或者只想下其中 3 集，自动开跑就是浪费带宽和磁盘。
 * 所以中间一定有一步"你看，就是这些，要不要挑一下"。
 *
 * 状态刻意只放在这个模块里（不挂 WB.state）：离开页面再回来等于重新开始，
 * 避免"上次那个下载还在等我点开始吗"的疑问。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  var QUALITIES = [
    { value: "720P", text: "720P（省空间）" },
    { value: "1080P", text: "1080P（推荐，H.264）" },
    { value: "1080P+", text: "1080P 高码率" },
    { value: "4K", text: "4K（需大会员）" }
  ];

  var state = {
    url: "",
    quality: "1080P",
    resolution: null,
    picked: {},
    busy: false,
    error: ""
  };

  function render() {
    var page = ui.h("section", { class: "page" });
    page.appendChild(
      ui.h("div", { class: "page__head" }, [
        ui.h("h2", { class: "page__title", text: "新建任务" }),
        ui.h("p", { class: "page__hint", text: "粘贴 B站 链接 → 解析 → 挑选要下载的集数" })
      ])
    );

    var input = ui.h("input", {
      class: "input",
      id: "newtask-url",
      placeholder: "BV / av / ep / ss / md 号、合集链接，或本地视频路径",
      autocomplete: "off",
      spellcheck: "false",
      value: state.url
    });
    input.addEventListener("input", function () {
      state.url = input.value;
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") resolve();
    });

    var quality = ui.h(
      "select",
      { class: "select", id: "newtask-quality" },
      QUALITIES.map(function (item) {
        return ui.h("option", { value: item.value, text: item.text, selected: item.value === state.quality ? "selected" : null });
      })
    );
    quality.addEventListener("change", function () {
      state.quality = quality.value;
    });

    page.appendChild(
      ui.h("div", { class: "card" }, [
        ui.h("div", { class: "card__head" }, [ui.h("h3", { class: "card__title", text: "链接" })]),
        ui.h("div", { class: "card__body col", style: "gap:16px" }, [
          ui.h("div", { class: "field" }, [
            ui.h("label", { class: "field__label", for: "newtask-url", text: "视频 / 合集地址" }),
            ui.h("div", { class: "field__row" }, [
              input,
              ui.h("button", { class: "btn btn--primary", text: "解析", onclick: resolve }),
              ui.h("button", { class: "btn", text: "选本地文件", onclick: importLocal })
            ]),
            ui.h("span", {
              class: "field__help",
              text: "支持单稿、分P、番剧与合集。解析只读取信息，不会开始下载。"
            })
          ]),
          ui.h("div", { class: "grid-2" }, [
            ui.h("div", { class: "field" }, [
              ui.h("label", { class: "field__label", text: "画质" }),
              quality,
              ui.h("span", { class: "field__help", text: "1080P 默认锁 H.264：剪辑软件对 HEVC 支持参差。" })
            ]),
            ui.h("div", { class: "field" }, [
              ui.h("label", { class: "field__label", text: "处理模式" }),
              ui.h("select", { class: "select", disabled: true }, [
                ui.h("option", { text: "完整处理（下载 → 转录 → 总结）——P2 开放" })
              ]),
              ui.h("span", { class: "field__help", text: "只跑下载也能用；后续阶段会接着同一个项目走。" })
            ])
          ])
        ])
      ])
    );

    var resultBox = ui.h("div", { id: "newtask-result" });
    page.appendChild(resultBox);
    paintResult(resultBox);

    // P1-08：最近的下载批次。放这里而不是素材库，是因为"下载失败"这件事
    // 发生在新任务页——用户刚点完开始，回头就能看见哪一集挂了、怎么换后端重试。
    page.appendChild(recentCard());
    return page;
  }

  // -------------------------------------------------------------- 最近下载（P1-08）

  var recent = { rows: [], backends: null, loading: false, busy: "" };

  function loadRecent() {
    recent.loading = true;
    var jobsP = WB.rpc("job.list", {}, { silent: true }).catch(function () { return []; });
    var backP = recent.backends
      ? Promise.resolve(recent.backends)
      : WB.rpc("download.backends", {}, { silent: true }).catch(function () { return null; });
    return Promise.all([jobsP, backP]).then(function (out) {
      var jobs = out[0] || [];
      recent.backends = out[1] || recent.backends;
      recent.rows = jobs
        .filter(function (job) { return String(job.type || "") === "download.batch"; })
        .slice(0, 4);
      recent.loading = false;
      paintRecent();
    });
  }

  function failedOf(job) {
    var result = job && job.result;
    if (typeof result === "string") {
      try {
        result = JSON.parse(result);
      } catch (e) {
        result = null;
      }
    }
    if (!result || !Array.isArray(result.items)) return [];
    return result.items.filter(function (row) {
      return row && row.status && row.status !== "succeeded";
    });
  }

  function retryWith(job, backend) {
    recent.busy = String(job.id || "");
    paintRecent();
    WB.rpc("download.retry_failed", { id: job.id, backend: backend || "" }, { silent: true })
      .then(function (data) {
        var started = (data && data.job) || {};
        ui.toast(
          (backend === "bbdown" ? "已用 BBDown 重新提交失败项" : "已重新提交失败项") +
            (started.id ? "（" + started.id + "）" : ""),
          "success"
        );
        return loadRecent();
      })
      .catch(function (err) {
        ui.toast((err && err.message) || "重试失败", "error");
        recent.busy = "";
        paintRecent();
      });
  }

  function paintRecent() {
    var host = document.getElementById("newtask-recent");
    if (!host) return;
    host.textContent = "";
    var bbdownReady = !!(recent.backends && recent.backends.bbdown && recent.backends.bbdown.found);

    if (recent.loading) {
      host.appendChild(ui.h("p", { class: "muted", text: "读取中…" }));
      return;
    }
    if (!recent.rows.length) {
      host.appendChild(ui.h("p", { class: "muted", text: "还没有下载记录。提交一次下载后这里会显示每一批的结果。" }));
      return;
    }

    recent.rows.forEach(function (job) {
      var failed = failedOf(job);
      var status = String(job.status || "");
      var busy = recent.busy === String(job.id || "");
      var actions = [];
      if (failed.length) {
        actions.push(
          ui.h("button", {
            class: "btn btn--ghost nt-recent__retry",
            type: "button",
            text: "重试失败项（" + failed.length + "）",
            disabled: busy ? "disabled" : null,
            onclick: function () { retryWith(job, ""); }
          })
        );
        if (bbdownReady) {
          actions.push(
            ui.h("button", {
              class: "btn nt-recent__bbdown",
              type: "button",
              text: "用 BBDown 重试",
              disabled: busy ? "disabled" : null,
              onclick: function () { retryWith(job, "bbdown"); }
            })
          );
        }
      }

      host.appendChild(
        ui.h("div", { class: "nt-recent__row" }, [
          ui.h("div", { class: "col", style: "gap:4px" }, [
            ui.h("div", { class: "row", style: "gap:8px;align-items:center" }, [
              ui.h("span", { class: "nt-recent__id mono", text: String(job.id || "") }),
              ui.h("span", {
                class: "pill" + (status === "failed" ? " pill--danger" : ""),
                text: { queued: "排队中", running: "进行中", succeeded: "已完成", failed: "有失败项", canceled: "已取消" }[status] || status
              })
            ]),
            ui.h("p", {
              class: "muted truncate",
              text: failed.length
                ? "失败 " + failed.length + " 集：" + failed.slice(0, 2).map(function (row) { return row.title || row.bvid || ""; }).join("、")
                : "批次完整结束"
            })
          ]),
          ui.h("div", { class: "row", style: "gap:8px" }, actions)
        ])
      );
    });

    if (recent.rows.some(function (job) { return failedOf(job).length > 0; }) && !bbdownReady) {
      host.appendChild(
        ui.h("p", {
          class: "field__help",
          text: "有失败项，但没检测到 BBDown。放一个 BBDown.exe 进 tools/ 目录后点「刷新」，这里会多出「用 BBDown 重试」。"
        })
      );
    }
  }

  function recentCard() {
    var host = ui.h("div", { id: "newtask-recent", class: "col", style: "gap:10px" });
    loadRecent();
    return ui.h("div", { class: "card" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title", text: "最近下载" }),
        ui.h("button", { class: "btn btn--ghost", type: "button", text: "刷新", onclick: loadRecent })
      ]),
      ui.h("div", { class: "card__body col", style: "gap:10px" }, [
        host,
        ui.h("span", {
          class: "field__help",
          text: "yt-dlp 失败时，若本机装了 BBDown，可以直接换后端重试失败的那几集；不会自动切换。"
        })
      ])
    ]);
  }


  // ------------------------------------------------------------------ 解析

  function resolve() {
    var url = String(state.url || "").trim();
    if (!url) {
      ui.toast("先粘贴一个链接", "warning");
      return;
    }
    state.busy = true;
    state.error = "";
    paintResult();
    WB.rpc("download.resolve", { url: url }, { silent: true })
      .then(function (data) {
        state.busy = false;
        state.resolution = data || null;
        state.picked = {};
        (data.items || []).forEach(function (item, index) {
          state.picked[keyOf(item, index)] = true; // 默认全选：绝大多数人就是要全部
        });
        paintResult();
      })
      .catch(function (err) {
        state.busy = false;
        state.resolution = null;
        state.error = (err && err.message) || "解析失败";
        paintResult();
      });
  }

  function keyOf(item, index) {
    return (item.bvid || "") + "#" + String(item.cid || item.page || index);
  }

  function paintResult(box) {
    var host = box || document.getElementById("newtask-result");
    if (!host) return;
    host.textContent = "";

    if (state.busy) {
      host.appendChild(ui.h("div", { class: "card" }, [
        ui.h("div", { class: "card__body" }, [ui.h("p", { class: "muted", text: "正在解析…" })])
      ]));
      return;
    }
    if (state.error) {
      host.appendChild(
        ui.empty("解析不出来", state.error, ui.h("button", { class: "btn", text: "重试", onclick: resolve }))
      );
      return;
    }
    if (!state.resolution) {
      host.appendChild(
        ui.empty("还没有解析结果", "粘一个链接点「解析」，这里会列出封面、UP、时长与每一集。")
      );
      return;
    }

    var res = state.resolution;
    var items = res.items || [];
    var keys = items.map(keyOf);
    var chosen = keys.filter(function (key) { return state.picked[key]; });

    var coverBox = ui.h("div", { class: "thumb thumb--empty", html: ui.svg("film") });
    if (res.cover) {
      coverBox = ui.h("div", { class: "thumb" }, [
        ui.h("img", { class: "thumb__img", alt: "", src: res.cover, referrerpolicy: "no-referrer" })
      ]);
    }

    var head = ui.h("div", { class: "nt-head" }, [
      coverBox,
      ui.h("div", { class: "col", style: "gap:8px" }, [
        ui.h("div", { class: "row", style: "gap:8px;align-items:center" }, [
          ui.h("span", { class: "pill", text: kindText(res.kind) }),
          ui.h("strong", { class: "truncate", text: res.title || "" })
        ]),
        ui.h("p", { class: "muted", text: (res.up_name || "未知UP") + " · " + items.length + " 集" + (res.note ? " · " + res.note : "") }),
        // 合集名有时候和标题是同一串（合集内单个视频），重复显示只添噪点。
        res.collection_title && res.collection_title !== res.title
          ? ui.h("p", { class: "muted", text: "合集：" + res.collection_title })
          : null
      ])
    ]);

    var list = ui.h("div", { class: "nt-items" });
    items.forEach(function (item, index) {
      var key = keyOf(item, index);
      var box = ui.h("input", {
        type: "checkbox",
        checked: state.picked[key] ? "checked" : null,
        onchange: function (event) {
          state.picked[key] = event.target.checked;
          paintResult();
        }
      });
      list.appendChild(
        ui.h("label", { class: "nt-item" }, [
          box,
          ui.h("span", { class: "nt-item__no", text: "P" + (item.page || index + 1) }),
          ui.h("span", { class: "truncate", text: item.part_title || item.title || item.bvid }),
          ui.h("span", { class: "muted mono", text: ui.formatDuration((item.duration_ms || 0) / 1000) })
        ])
      );
    });

    var bar = ui.h("div", { class: "nt-bar" }, [
      ui.h("button", {
        class: "btn btn--primary",
        text: "开始下载" + (chosen.length ? "（" + chosen.length + " 集）" : ""),
        disabled: chosen.length ? null : "disabled",
        onclick: function () { start(items, keys); }
      }),
      ui.h("button", {
        class: "btn",
        text: chosen.length === keys.length ? "全不选" : "全选",
        onclick: function () {
          var all = chosen.length === keys.length;
          keys.forEach(function (key) { state.picked[key] = !all; });
          paintResult();
        }
      }),
      ui.h("span", { class: "field__help", text: "下载在后台跑，进度看右下角状态条。" })
    ]);

    host.appendChild(
      ui.h("div", { class: "card" }, [
        ui.h("div", { class: "card__head" }, [
          ui.h("h3", { class: "card__title", text: res.is_batch || items.length > 1 ? "这一批共 " + items.length + " 集" : "将要下载" }),
          ui.h("span", { class: "pill", text: "已选 " + chosen.length })
        ]),
        ui.h("div", { class: "card__body col nt-result", style: "gap:14px" }, [head, list, bar])
      ])
    );
  }

  function kindText(kind) {
    return { single: "单稿", parts: "分P", collection: "合集", season: "番剧", playlist: "列表" }[kind] || kind || "视频";
  }

  // ------------------------------------------------------------------ 启动

  function start(items, keys) {
    var chosen = items.filter(function (item, index) {
      return state.picked[keys[index]];
    });
    if (!chosen.length) return;
    state.busy = true;
    paintResult();
    WB.rpc("download.start", {
      url: String(state.url || "").trim(),
      quality: state.quality,
      items: chosen,
      up_name: (state.resolution && state.resolution.up_name) || "",
      collection_title: (state.resolution && state.resolution.collection_title) || ""
    }, { silent: true })
      .then(function (data) {
        state.busy = false;
        ui.toast("已提交下载：" + (data.count || chosen.length) + " 集", "success");
        state.resolution = null;
        state.picked = {};
        paintResult();
        WB.router.go("library");
      })
      .catch(function (err) {
        state.busy = false;
        paintResult();
        ui.toast((err && err.message) || "提交失败", "error");
      });
  }

  function importLocal() {
    WB.rpc("fs.pick", { kind: "files" }, { silent: true })
      .then(function (data) {
        var picked = (data && data.path) || "";
        if (!picked) return null;
        return WB.rpc("import.local", { paths: String(picked).split("|"), recursive: true }, { silent: true });
      })
      .then(function (result) {
        if (!result) return;
        ui.toast("已导入 " + result.total + " 个视频", "success");
        WB.router.go("library");
      })
      .catch(function (err) {
        ui.toast((err && err.message) || "导入失败", "error");
      });
  }

  WB.pages = WB.pages || {};
  WB.pages.newtask = {
    render: render,
    state: state,
    // 测试用：清掉"后端探测"的缓存，好证明每次进页面都会重新探。
    _resetRecent: function () {
      recent.backends = null;
      recent.rows = [];
      recent.busy = "";
    }
  };

  WB.router.define("newtask", {
    title: "新建任务",
    crumb: "粘贴链接开始",
    icon: "plus",
    nav: true,
    render: render
  });
})();
