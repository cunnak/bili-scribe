/* ============================================================================
 * pages/library.js —— 素材库（P1-04：树 / 列表 / 详情三栏）
 *
 * 三栏而不是一张大表，是因为"挑一个项目"和"看这个项目有什么"是两件不同的事：
 * 表格里塞不下资产清单，详情页塞不下 100 个项目的对比。
 *
 * 三个约束来自真实使用：
 *
 * 1. **左栏按阶段分组，不是按目录树。** 用户找东西的动机几乎总是
 *    "哪些还没转录"「哪些可以切片了」，而不是"它在哪个文件夹"。
 * 2. **列表行必须有缩略图。** 40 个"第X讲"靠标题认不出来，靠封面一眼就认出来。
 * 3. **文件没了要当场标红。** 库里记着、磁盘上被删了，是最常见的"点了没反应"来源。
 *    这件事只能在读的时候判（`missing` 由后端现算），不能落库。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  function call(method, params) {
    return WB.rpc(method, params || {}, { silent: true });
  }

  // 订阅后端事件：返回取消函数。events.js 会把未注册的 type 直接丢掉，
  // 所以这里只关心自己那几种。
  function on(type, fn) {
    return WB.events.on(type, fn);
  }

  function nz(value) {
    return value === null || value === undefined || value === "" ? "—" : value;
  }

  function pct(done, total) {
    if (!total || total <= 0) return "";
    return Math.min(100, Math.round((done / total) * 100)) + "%";
  }

  var state = {
    query: "",
    stage: "",
    items: [],
    total: 0,
    loading: false,
    selected: "",
    detail: null,
    detailLoading: false
  };

  var STAGE_FILTERS = [
    { key: "", text: "全部" },
    { key: "new", text: "待转录" },
    { key: "subtitle", text: "有字幕" },
    { key: "downloaded", text: "已下载" },
    { key: "transcribed", text: "已转录" },
    { key: "clipped", text: "已切片" }
  ];

  // ------------------------------------------------------------------ 布局

  function render(ctx) {
    var params = (ctx && ctx.params) || {};
    if (params.q !== undefined) state.query = params.q;
    if (params.id) state.selected = params.id;

    var page = ui.h("section", { class: "page" });
    page.appendChild(
      ui.h("div", { class: "page__head" }, [
        ui.h("h2", { class: "page__title", text: "素材库" }),
        ui.h("p", { class: "page__hint", text: "项目、源文件、字幕与切片产物" }),
        ui.h("div", { class: "page__actions" }, [
          ui.h("button", { class: "btn", text: "导入本地", onclick: importLocal }),
          ui.h("button", { class: "btn", text: "刷新", onclick: function () { load(); } }),
          ui.h("button", {
            class: "btn btn--primary",
            text: "新建任务",
            onclick: function () { WB.router.go("newtask"); }
          })
        ])
      ])
    );

    var body = ui.h("div", { class: "pane3", id: "library-body" });
    page.appendChild(body);
    paint(body);
    // 首屏必须自己拉一次：render 只画空壳，数据在 load 里。早先版本把 load 交给
    // "别处会调 reload" 的假设，结果素材库打开永远是空的（0/0），刷新按钮才出数据。
    if (!state.loading) load();
    return page;
  }

  function paint(root) {
    var box = root || document.getElementById("library-body");
    if (!box) return;
    box.textContent = "";
    box.appendChild(paintTree());
    box.appendChild(paintList());
    box.appendChild(paintDetail());
  }

  // ------------------------------------------------------------------ 左栏

  function countOf(stage) {
    var hit = 0;
    state.items.forEach(function (row) {
      if (!stage || row.stage === stage) hit++;
    });
    return hit;
  }

  function paintTree() {
    var tree = ui.h("div", { class: "pane3__side card card--flat" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title", text: "阶段" }),
        ui.h("span", { class: "pill", text: String(state.total) })
      ])
    ]);
    var list = ui.h("div", { class: "tree" });
    STAGE_FILTERS.forEach(function (filter) {
      var active = state.stage === filter.key;
      list.appendChild(
        ui.h(
          "button",
          {
            class: "tree__row" + (active ? " is-active" : ""),
            onclick: function () {
              state.stage = filter.key;
              paint();
            }
          },
          [
            ui.h("span", { class: "tree__label", text: filter.text }),
            ui.h("span", { class: "tree__count mono", text: String(countOf(filter.key)) })
          ]
        )
      );
    });
    tree.appendChild(list);
    if (state.items.some(function (r) { return (r.missing || []).length; })) {
      tree.appendChild(
        ui.h("p", {
          class: "tree__warn",
          text: "有项目的文件已不在磁盘上，详情里会标红。"
        })
      );
    }
    tree.appendChild(
      ui.h("button", {
        class: "btn btn--ghost btn--block",
        text: "打开媒体目录",
        onclick: function () {
          WB.rpc("fs.media", {}, { silent: true })
            .then(function (info) {
              if (info && info.path) return WB.rpc("fs.reveal", { path: info.path }, { silent: true });
              return null;
            })
            .catch(function () {});
        }
      })
    );
    return ui.h("div", { class: "pane3__col" }, [tree]);
  }

  // ------------------------------------------------------------------ 中栏

  function visible() {
    return state.items.filter(function (row) {
      return !state.stage || row.stage === state.stage;
    });
  }

  function paintList() {
    var rows = visible();
    var wrap = ui.h("div", { class: "pane3__main" });

    var head = ui.h("div", { class: "list__head" }, [
      ui.h("span", { class: "muted", text: state.stage ? ui.stageText(state.stage) : "全部项目" }),
      ui.h("span", { class: "pill", text: rows.length + " / " + state.total })
    ]);
    wrap.appendChild(head);

    if (state.loading) {
      wrap.appendChild(ui.h("div", { class: "empty" }, [ui.h("div", { class: "empty__hint", text: "正在读取素材库…" })]));
      return wrap;
    }

    if (!rows.length) {
      wrap.appendChild(
        ui.empty(
          state.query || state.stage ? "没有匹配的项目" : "素材库还是空的",
          state.query || state.stage
            ? "换个关键词或切回「全部」。搜索会匹配标题、UP 名与 BV 号。"
            : "粘贴 B站 链接下载，或点右上角「导入本地」把已有视频加进来。",
          ui.h("button", {
            class: "btn btn--primary",
            text: "去新建任务",
            onclick: function () { WB.router.go("newtask"); }
          })
        )
      );
      return wrap;
    }

    var list = ui.h("div", { class: "lib-list" });
    rows.forEach(function (row) {
      list.appendChild(paintRow(row));
    });
    wrap.appendChild(list);
    return wrap;
  }

  function thumbBox(row) {
    if (row.has_thumb && row.thumb) {
      var img = ui.h("img", { class: "thumb__img", alt: "", src: WB.mediaUrl(row.thumb), loading: "lazy" });
      img.addEventListener("error", function () {
        img.replaceWith(placeholderThumb());
      });
      return ui.h("span", { class: "thumb" }, [img]);
    }
    return placeholderThumb();
  }

  function placeholderThumb() {
    return ui.h("span", { class: "thumb thumb--empty", html: ui.svg("film") });
  }

  function paintRow(row) {
    var active = state.selected === row.id;
    var missing = (row.missing || []).length > 0;
    return ui.h(
      "button",
      {
        class: "lib-row" + (active ? " is-active" : "") + (missing ? " is-missing" : ""),
        onclick: function () {
          select(row.id);
        }
      },
      [
        thumbBox(row),
        ui.h("span", { class: "lib-row__body" }, [
          ui.h("span", { class: "lib-row__title truncate", text: row.title || "(无标题)" }),
          ui.h("span", { class: "lib-row__meta" }, [
            ui.stagePill(row.stage),
            ui.h("span", { class: "muted mono", text: ui.formatDuration(row.duration_sec) }),
            ui.h("span", { class: "muted truncate", text: row.up_name || row.source_kind || "" })
          ])
        ]),
        missing ? ui.h("span", { class: "lib-row__flag", text: "文件缺失" }) : null
      ]
    );
  }

  function select(id) {
    state.selected = id;
    state.detailLoading = true;
    paint();
    WB.rpc("library.get", { id: id }, { silent: true })
      .then(function (data) {
        state.detail = (data && data.project) || null;
        state.detailLoading = false;
        paint();
      })
      .catch(function () {
        state.detailLoading = false;
        state.detail = null;
        paint();
      });
  }

  // ------------------------------------------------------------------ 右栏

  function keyValue(pairs) {
    return ui.h(
      "dl",
      { class: "kv" },
      pairs.reduce(function (acc, pair) {
        acc.push(ui.h("dt", { text: pair[0] }));
        acc.push(ui.h("dd", { class: pair[2] || "", text: pair[1] }));
        return acc;
      }, [])
    );
  }

  function paintDetail() {
    var col = ui.h("div", { class: "pane3__col pane3__col--wide" }, []);
    if (!state.selected) {
      col.appendChild(ui.empty("选一个项目", "左栏筛选阶段，中栏点一行，这里显示它的资产与产物。"));
      return col;
    }
    if (state.detailLoading || !state.detail) {
      col.appendChild(ui.h("div", { class: "empty" }, [ui.h("div", { class: "empty__hint", text: "读取详情…" })]));
      return col;
    }

    var row = state.detail;
    var missing = row.missing || [];
    var card = ui.h("div", { class: "card" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title truncate", text: row.title || "(无标题)" }),
        ui.stagePill(row.stage)
      ])
    ]);

    // 有封面才给 16:9 的大图槽。没有的时候那个空槽是一大块纯黑，
    // 比"没有封面"这件事本身更抢眼，所以干脆不占位。
    var cover = row.has_thumb ? ui.h("div", { class: "detail__cover" }, [thumbBox(row)]) : null;

    var actions = ui.h("div", { class: "row", style: "gap:8px;flex-wrap:wrap" }, [
      ui.h("button", {
        class: "btn",
        text: "打开目录",
        onclick: function () { WB.rpc("fs.reveal", { path: row.root_dir }, { silent: true }).catch(function () {}); }
      }),
      ui.h("button", {
        class: "btn",
        text: "复制路径",
        onclick: function () { copy(row.root_dir); }
      }),
      row.stage === "new" || row.stage === "downloaded"
        ? ui.h("button", {
            class: "btn btn--primary",
            text: "开始转录",
            onclick: function () { transcribe(row); }
          })
        : null,
      // 有字幕就一定给入口：编辑器的价值全在"改"上，藏两级菜单等于没做
      row.stage === "transcribed" || row.stage === "subtitle" || row.stage === "summarized"
        || row.stage === "clipped" || row.stage === "merged" || row.stage === "published"
        ? ui.h("button", {
            class: "btn",
            text: "字幕",
            onclick: function () { WB.router.go("subtitles", { project: row.id }); }
          })
        : null,
      ui.h("button", {
        class: "btn btn--danger",
        text: "从库中移除",
        onclick: function () { remove(row); }
      })
    ]);

    card.appendChild(
      ui.h("div", { class: "card__body col", style: "gap:14px" }, [
        cover,
        missing.length
          ? ui.h("p", { class: "warn-line", text: "以下文件已不在磁盘上：" + missing.join("、") })
          : null,
        keyValue([
          ["视频号", row.bvid || "—", "mono"],
          ["UP", row.up_name || "—", ""],
          ["分P", row.page_no ? "第 " + row.page_no + " P" : "—", "mono"],
          ["时长", ui.formatDuration(row.duration_sec), "mono"],
          ["大小", row.bytes ? ui.formatBytes(row.bytes) : "—", "mono"],
          ["合集", row.collection_title || "—", ""],
          ["更新时间", String(row.updated_at || "").slice(0, 16), "mono"]
        ]),
        actions
      ])
    );

    col.appendChild(card);
    col.appendChild(assetsCard(row));
    return col;
  }

  function assetsCard(row) {
    var assets = row.assets || [];
    var body;
    if (!assets.length) {
      body = ui.h("p", { class: "muted", text: "还没有产物。下载或导入后这里会列出源文件、音频、字幕与切片。" });
    } else {
      body = ui.h(
        "table",
        { class: "table table--tight" },
        [
          ui.h("thead", null, [
            ui.h("tr", null, [
              ui.h("th", { text: "类型" }),
              ui.h("th", { text: "文件" }),
              ui.h("th", { text: "大小" }),
              ui.h("th", { text: "状态" })
            ])
          ]),
          ui.h(
            "tbody",
            null,
            assets.map(function (asset) {
              var gone = asset.exists === false;
              return ui.h("tr", { class: gone ? "row--missing" : "" }, [
                ui.h("td", null, [ui.h("span", { class: "pill", text: asset.kind || "?" })]),
                ui.h("td", null, [ui.h("div", { class: "truncate mono", title: asset.path || "", text: baseName(asset.path) })]),
                ui.h("td", { class: "mono", text: asset.bytes ? ui.formatBytes(asset.bytes) : "—" }),
                ui.h("td", null, [
                  gone ? ui.h("span", { class: "pill pill--ember", text: "缺失" }) : ui.h("span", { class: "muted", text: "在位" })
                ])
              ]);
            })
          )
        ]
      );
    }
    return ui.h("div", { class: "card" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title", text: "资产" }),
        ui.h("span", { class: "pill", text: String(assets.length) })
      ]),
      ui.h("div", { class: "card__body" }, [body])
    ]);
  }

  function baseName(path) {
    var text = String(path || "");
    var parts = text.split(/[\\/]/);
    return parts[parts.length - 1] || text;
  }

  function copy(text) {
    var value = String(text || "");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(
        function () { ui.toast("路径已复制", "success"); },
        function () { ui.toast("复制失败，请手动选中", "warning"); }
      );
      return;
    }
    ui.toast(value, "info", 6000);
  }

  /** 起一次转录。进度不在这里轮询——job 事件会推过来，素材库收到就刷新。 */
  function transcribe(row) {
    return call("transcribe.start", { project_ids: [row.id] })
      .then(function (data) {
        var jobs = (data && data.jobs) || [];
        ui.toast(jobs.length ? "已开始转录，可在任务里看进度" : "没有可转录的项目", "success");
        load();
      })
      .catch(function (err) {
        ui.toast("启动转录失败：" + ((err && err.message) || err), "error");
      });
  }

  function remove(row) {
    ui.confirm("从库中移除？", "只删除库里的记录，磁盘上的文件不动（「" + (row.title || "") + "」）。").then(function (ok) {
      if (!ok) return;
      WB.rpc("library.delete", { id: row.id }, { silent: true })
        .then(function () {
          state.selected = "";
          state.detail = null;
          ui.toast("已移除", "success");
          load();
        })
        .catch(function (err) { ui.toast((err && err.message) || "移除失败", "error"); });
    });
  }

  // ------------------------------------------------------------------ 数据

  function load() {
    state.loading = true;
    paint();
    WB.rpc("library.list", { query: state.query, limit: 200, offset: 0 }, { silent: true })
      .then(function (data) {
        state.items = (data && data.items) || [];
        state.total = (data && data.total) || state.items.length;
        state.loading = false;
        // 中栏没选中或选中的已经不在了，就自动挑第一行——省掉一次点击
        if (!state.selected || !state.items.some(function (r) { return r.id === state.selected; })) {
          state.selected = state.items.length ? state.items[0].id : "";
          state.detail = null;
          if (state.selected) {
            select(state.selected); // select 自己会重绘，别再 paint 一次
            return;
          }
        }
        paint();
      })
      .catch(function (err) {
        state.loading = false;
        state.items = [];
        state.total = 0;
        paint();
        ui.toast((err && err.message) || "读不到素材库", "error");
      });
  }

  /** 导入本地：先弹系统选择框，再交给后端建项目。 */
  function importLocal() {
    WB.rpc("fs.pick", { kind: "files" }, { silent: true })
      .then(function (data) {
        var picked = (data && data.path) || "";
        if (!picked) return null;
        ui.toast("正在导入…", "info", 2000);
        return WB.rpc("import.local", { paths: String(picked).split("|"), recursive: true }, { silent: true });
      })
      .then(function (result) {
        if (!result) return;
        ui.toast("导入完成：新增 " + result.total + " 个", "success");
        if (result.failed && result.failed.length) {
          ui.toast((result.failed[0] && result.failed[0].message) || "部分文件导入失败", "warning");
        }
        load();
      })
      .catch(function (err) {
        ui.toast((err && err.message) || "导入失败", "error");
      });
  }

  function search(query) {
    state.query = String(query || "");
    load();
  }

  // 后端改了库（导入、下载完成、删除）就自己重拉——不需要用户去点刷新。
  // 只挂一次：模块体只执行一次，但 render 每次进页面都会跑。
  if (WB.events && WB.events.on) {
    on("library.changed", function () {
      if (document.getElementById("library-body")) load();
    });
  }

  WB.pages = WB.pages || {};
  WB.pages.library = { render: render, search: search, state: state, reload: load };

  WB.router.define("library", {
    title: "素材库",
    crumb: "项目与产物",
    icon: "library",
    nav: true,
    render: render
  });
})();
