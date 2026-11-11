/* ============================================================================
 * pages/library.js —— 素材库（P1-04 / 信息架构重构 T2-1~T2-8）
 *
 * 三栏而不是一张大表：左栏按“归属”找，中栏按“阶段”筛，右栏看细节。
 *
 * 这一版把三件事拆开了（规划书 §2 D1/D2/D6）：
 *   1. 左栏 = 合集/本地导入/未归类/其他 的两级树 + 已归档入口；
 *   2. 阶段 = 列表顶部 chips，桶集合由后端 facets 驱动，不在前端二次过滤；
 *   3. 排序 = 时间 / 标题 / B站合集顺序，写进 URL，刷新后仍生效。
 *
 * 三个约束来自真实使用：
 *   - 阶段桶必须一个不少。旧版只列 6 个桶，8 阶段里的 analyzed/summarized 选不出来。
 *   - 文件没了要当场标红（missing 由后端现算，不落库）。
 *   - 全选 = 全部匹配（服务端过滤删除）。列表只装 200 条而总数可能是 1000，
 *     混用 items.length 就是数据事故（D8）。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  var PAGE_LIMIT = 200;

  var SORTS = [
    { key: "created_desc", text: "时间" },
    { key: "title", text: "标题" },
    { key: "collection_index", text: "B站合集顺序" }
  ];

  var state = {
    query: "",
    stage: "",
    sort: "created_desc",
    includeArchived: false,
    smartView: "",
    items: [],
    loadedRawCount: 0,
    hasArchivedTotal: false,
    total: 0,
    serverTotal: 0,
    archivedTotal: 0,
    facets: [],
    stageLabels: {},
    treeItems: null,
    treeTotal: 0,
    treeArchivedTotal: 0,
    loading: false,
    loadingMore: false,
    hasMore: false,
    error: "",
    selected: "",
    detail: null,
    detailLoading: false,
    detailError: "",
    detailNotice: "",
    selectedIds: {},
    allMatching: false,
    anchorIndex: -1,
    expanded: {}
  };

  var loadToken = 0;
  var detailToken = 0;
  var sentinelObserver = null;

  function call(method, params) {
    return WB.rpc(method, params || {}, { silent: true });
  }

  // 订阅后端事件：返回取消函数。events.js 会把未注册的 type 直接丢掉。
  function on(type, fn) {
    return WB.events.on(type, fn);
  }

  function validSort(key) {
    for (var i = 0; i < SORTS.length; i++) {
      if (SORTS[i].key === key) return key;
    }
    return "created_desc";
  }

  function stageLabel(key) {
    if (!key) return "全部";
    if (state.stageLabels && state.stageLabels[key]) return state.stageLabels[key];
    var facets = state.facets || [];
    for (var i = 0; i < facets.length; i++) {
      if (facets[i] && facets[i].key === key && facets[i].label) return facets[i].label;
    }
    return ui.stageText(key);
  }

  function stagePill(stage) {
    var meta = (ui.STAGES && ui.STAGES[stage]) || { cls: "" };
    return ui.h("span", {
      class: "pill " + (meta.cls || ""),
      "data-stage": stage || "",
      text: stageLabel(stage)
    });
  }

  // 阶段判断只读 ui.STAGES，不再手写 "analyzed" 这类后端没有的历史值。
  // 老库里的 analyzed 会落到 rank -1：当作“还没有文本”，给「开始转录」。
  function stageRank(stage) {
    return Object.keys(ui.STAGES || {}).indexOf(stage);
  }

  function hasTextStage(stage) {
    var rank = stageRank(stage);
    return rank >= 0 && rank >= stageRank("subtitle");
  }

  function canTranscribeStage(stage) {
    return !hasTextStage(stage);
  }

  function isArchived(row) {
    return !!(row && row.archived_at);
  }

  function hasMissing(row) {
    return !!(row && (row.missing || []).length);
  }

  // 中栏显示的集合：阶段过滤由后端做；这里只把“已归档视图”和
  // “缺文件”这两个纯客户端视图裁出来。缺文件只检查已加载的 items，
  // 不做全库扫描，界面必须把这一边界说出来。
  function visibleItems() {
    if (state.smartView === "missing") return state.items.filter(hasMissing);
    if (!state.includeArchived) return state.items;
    return state.items.filter(isArchived);
  }

  function isSelected(id) {
    return state.allMatching || !!state.selectedIds[id];
  }

  function selectedCount() {
    if (state.allMatching) return state.total;
    return Object.keys(state.selectedIds).length;
  }

  function clearSelection(repaint) {
    state.selectedIds = {};
    state.allMatching = false;
    state.anchorIndex = -1;
    if (repaint !== false) syncSelectionUI();
  }

  // ------------------------------------------------------------------ 布局

  function render(ctx) {
    var params = (ctx && ctx.params) || {};
    state.sort = validSort(params.sort || "created_desc");
    state.includeArchived = params.archived === "1";
    if (params.q !== undefined) state.query = String(params.q || "");
    if (params.project) state.selected = String(params.project);
    else if (params.id) state.selected = String(params.id);

    var page = ui.h("section", { class: "page" });
    page.appendChild(
      ui.h("div", { class: "page__head" }, [
        ui.h("h2", { class: "page__title", text: "素材库" }),
        ui.h("p", { class: "page__hint", text: "项目、源文件、字幕与切片产物" }),
        ui.h("div", { class: "page__actions" }, [
          ui.h("button", { class: "btn", text: "导入本地", onclick: importLocal }),
          ui.h("button", { class: "btn", text: "刷新", onclick: function () { load({ resetSelection: false }); } }),
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
    // 首屏必须自己拉一次：render 只画空壳，数据在 load 里。
    if (!state.loading) load({ resetSelection: true });
    return page;
  }

  function paint(root) {
    var box = root || document.getElementById("library-body");
    if (!box) return;
    box.textContent = "";
    box.appendChild(paintTree());
    box.appendChild(paintList());
    box.appendChild(paintDetail());
    syncSelectionUI();
    mountSentinel();
  }

  // ------------------------------------------------------------------ 左栏

  function groupOf(row) {
    if (isArchived(row)) return "archived";
    if (row.collection_title) return "col:" + row.collection_title;
    if (row.source_kind === "local") return "local";
    if (row.bvid) return "unassigned";
    return "other";
  }

  function groupTitle(key, row) {
    if (key === "archived") return "已归档";
    if (key === "local") return "本地导入";
    if (key === "unassigned") return "未归类";
    if (key === "other") return "其他";
    return row.collection_title || "(无合集名)";
  }

  function treeRows() {
    // 左栏是“来源”轴，不能被阶段 chips 裁掉：有独立的 treeItems 就用它。
    // 例如点「已总结」后，2026正大杯备赛 39 集的组头必须还在。
    return state.treeItems || visibleItems();
  }

  function groupRank(key) {
    if (key.indexOf("col:") === 0) return 0;
    if (key === "local") return 1;
    if (key === "unassigned") return 2;
    if (key === "other") return 3;
    if (key === "archived") return 4;
    return 5;
  }

  function groupedRows() {
    var map = {};
    var order = [];
    treeRows().forEach(function (row) {
      var key = groupOf(row);
      if (!map[key]) {
        map[key] = { key: key, title: groupTitle(key, row), rows: [], first: order.length };
        order.push(key);
      }
      map[key].rows.push(row);
    });
    var groups = order.map(function (key) { return map[key]; });
    // 归属顺序：合集 → 本地导入 → 未归类 → 其他 → 已归档；合集/单集内部保持首次出现顺序。
    groups.sort(function (a, b) {
      return groupRank(a.key) - groupRank(b.key) || a.first - b.first;
    });
    return groups;
  }

  function paintTree() {
    var tree = ui.h("div", { class: "pane3__side card card--flat" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title", text: "素材库" }),
        ui.h("span", { class: "pill", text: String(state.treeTotal || state.total) })
      ])
    ]);

    var body = ui.h("div", { class: "tree lib-tree" });

    body.appendChild(
      ui.h("button", {
        class: "tree__row" + (!state.stage && !state.includeArchived && !state.smartView ? " is-active" : ""),
        onclick: function () {
          state.stage = "";
          state.includeArchived = false;
          state.smartView = "";
          syncUrl();
          load({ resetSelection: true });
        }
      }, [
        ui.h("span", { class: "lib-tree__name" }, [
          ui.h("span", { class: "tree__label", text: "全部项目" })
        ]),
        ui.h("span", { class: "tree__count mono", text: String(state.treeTotal || state.serverTotal) })
      ])
    );

    if (!state.includeArchived) {
      groupedRows().forEach(function (group) {
        var open = !!state.expanded[group.key];
        var head = ui.h(
          "button",
          {
            class: "tree__row lib-tree__group" + (open ? " is-open" : ""),
            onclick: function () {
              state.expanded[group.key] = !open;
              paint();
            }
          },
          [
            ui.h("span", { class: "lib-tree__name" }, [
              ui.h("span", { class: "lib-tree__chev", text: open ? "▾" : "▸" }),
              ui.h("span", { class: "tree__label truncate", text: group.title })
            ]),
            ui.h("span", { class: "tree__count mono", text: String(group.rows.length) })
          ]
        );
        body.appendChild(head);
        // 折叠时不建子节点：39 集的大组不展开就没有 DOM 成本，
        // 也满足“点组头展开只渲染该组”。
        if (open) {
          var children = ui.h("div", { class: "lib-tree__children" });
          group.rows.forEach(function (row) {
            children.appendChild(
              ui.h("button", {
                class: "lib-tree__child" + (state.selected === row.id ? " is-active" : ""),
                title: row.title || "",
                onclick: function () { select(row.id, { force: true }); }
              }, [
                ui.h("span", { class: "truncate", text: row.title || "(无标题)" })
              ])
            );
          });
          body.appendChild(children);
        }
      });
    }

    body.appendChild(
      ui.h("button", {
        class: "tree__row lib-tree__archived" + (state.includeArchived ? " is-active" : ""),
        disabled: !state.includeArchived && state.treeArchivedTotal <= 0,
        onclick: function () {
          if (!state.includeArchived && state.treeArchivedTotal <= 0) return;
          state.includeArchived = !state.includeArchived;
          state.stage = "";
          state.smartView = "";
          syncUrl();
          load({ resetSelection: true });
        }
      }, [
        ui.h("span", { class: "lib-tree__name" }, [
          ui.h("span", { class: "tree__label", text: "已归档" })
        ]),
        ui.h("span", { class: "tree__count mono", text: String(state.treeArchivedTotal) })
      ])
    );

    // D1 的 3 个智能视图固定放在树底部。待转录/最近导入走服务端参数；
    // 缺文件只能检查已加载的项目，按钮 title 与空态都写明边界。
    var missingCount = state.items.filter(hasMissing).length;
    var newCount = 0;
    facetList().forEach(function (facet) {
      if (facet && facet.key === "new") newCount = Number(facet.count) || 0;
    });
    body.appendChild(
      ui.h("div", { class: "lib-smart" }, [
        ui.h("button", {
          class: "tree__row lib-smart__item" + (state.smartView === "transcribe" ? " is-active" : ""),
          title: "只看待转录的项目（服务端筛选 stage=new）",
          onclick: function () {
            state.smartView = "transcribe";
            state.stage = "new";
            state.includeArchived = false;
            load({ resetSelection: true });
          }
        }, [
          ui.h("span", { class: "lib-smart__name", text: "待转录" }),
          ui.h("span", { class: "tree__count mono", text: String(newCount) })
        ]),
        ui.h("button", {
          class: "tree__row lib-smart__item" + (state.smartView === "recent" ? " is-active" : ""),
          title: "按创建时间倒序看最近导入的项目",
          onclick: function () {
            state.smartView = "recent";
            state.stage = "";
            state.sort = "created_desc";
            state.includeArchived = false;
            syncUrl();
            load({ resetSelection: true });
          }
        }, [
          ui.h("span", { class: "lib-smart__name", text: "最近导入" })
        ]),
        ui.h("button", {
          class: "tree__row lib-smart__item" + (state.smartView === "missing" ? " is-active" : ""),
          title: "仅在已加载的 " + state.items.length + " 项里找缺文件（不做全库扫描）",
          onclick: function () {
            state.smartView = "missing";
            state.stage = "";
            state.includeArchived = false;
            load({ resetSelection: true });
          }
        }, [
          ui.h("span", { class: "lib-smart__name", text: "缺文件" }),
          ui.h("span", { class: "tree__count mono", text: String(missingCount) })
        ]),
        ui.h("p", { class: "lib-smart__hint", text: "缺文件仅检查已加载的 " + state.items.length + " 项" })
      ])
    );

    if (state.items.some(function (row) { return (row.missing || []).length; })) {
      body.appendChild(
        ui.h("p", {
          class: "tree__warn",
          text: "有项目的文件已不在磁盘上，详情里会标红。"
        })
      );
    }

    tree.appendChild(body);
    tree.appendChild(
      ui.h("button", {
        class: "btn btn--ghost btn--block",
        text: "打开媒体目录",
        onclick: openMedia
      })
    );
    return ui.h("div", { class: "pane3__col" }, [tree]);
  }

  // ------------------------------------------------------------------ 阶段 chips

  function facetList() {
    if (state.facets && state.facets.length) return state.facets;
    // 旧后端 / mock 没有 facets 时的兜底：按 ui.STAGES 的 8 个阶段建桶。
    // 后端返回 facets 后这条不会生效；计数只统计已加载的项目。
    var counts = {};
    state.items.forEach(function (row) {
      counts[row.stage] = (counts[row.stage] || 0) + 1;
    });
    var list = [];
    Object.keys(ui.STAGES || {}).forEach(function (key) {
      list.push({ key: key, label: stageLabel(key), count: counts[key] || 0 });
    });
    Object.keys(counts).forEach(function (key) {
      if (!ui.STAGES || !ui.STAGES[key]) {
        list.push({ key: key, label: stageLabel(key), count: counts[key] });
      }
    });
    return list;
  }

  function facetTotal() {
    if (!state.facets || !state.facets.length) return state.total;
    var count = 0;
    state.facets.forEach(function (facet) { count += Number(facet.count) || 0; });
    return count;
  }

  function paintChips() {
    var chips = ui.h("div", { class: "lib-chips" });
    chips.appendChild(
      ui.h("button", {
        class: "lib-chip" + (!state.stage ? " is-active" : ""),
        onclick: function () { setStage(""); }
      }, [
        ui.h("span", { text: "全部" }),
        ui.h("span", { class: "lib-chip__count", text: String(facetTotal()) })
      ])
    );
    facetList().forEach(function (facet) {
      var key = facet.key || "";
      if (!key) return;
      var zero = !facet.count;
      var active = state.stage === key;
      chips.appendChild(
        ui.h("button", {
          class: "lib-chip" + (active ? " is-active" : "") + (zero ? " is-zero" : ""),
          disabled: zero && !active,
          title: zero ? "没有处于这个阶段的项目" : "",
          onclick: function () { setStage(key); }
        }, [
          ui.h("span", { text: facet.label || stageLabel(key) }),
          ui.h("span", { class: "lib-chip__count", text: String(facet.count || 0) })
        ])
      );
    });
    return chips;
  }

  function setStage(stage) {
    if (state.stage === stage && !state.smartView) return;
    state.stage = stage;
    state.smartView = stage === "new" ? "transcribe" : "";
    load({ resetSelection: true });
  }

  // ------------------------------------------------------------------ 中栏

  function paintList() {
    var wrap = ui.h("div", { class: "pane3__main" });
    var rows = visibleItems();

    wrap.appendChild(
      ui.h("div", { class: "list__head" }, [
        ui.h("div", { class: "lib-toolbar" }, [
          ui.h("span", { class: "muted", text: "排序" }),
          sortSelect()
        ]),
        ui.h("span", { class: "pill", text: rows.length + " / " + state.total })
      ])
    );

    wrap.appendChild(paintChips());
    if (state.smartView === "missing") {
      wrap.appendChild(
        ui.h("p", {
          class: "lib-scope-note",
          text: "缺文件：仅检查已加载的 " + state.items.length + " 项，不是全库扫描。"
        })
      );
    }
    wrap.appendChild(paintBulkBar());

    if (state.loading) {
      wrap.appendChild(ui.h("div", { class: "empty" }, [ui.h("div", { class: "empty__hint", text: "正在读取素材库…" })]));
      return wrap;
    }

    if (state.error) {
      wrap.appendChild(ui.empty("读取失败", state.error));
      return wrap;
    }

    if (!rows.length) {
      var title;
      var hint;
      if (state.smartView === "missing") {
        title = "已加载的项目里没有缺文件";
        hint = "这里只检查当前已加载的 " + state.items.length + " 项，不是全库扫描。";
      } else if (state.includeArchived) {
        title = "还没有归档的项目";
        hint = "归档不会删除文件；在任意项目详情里点「归档」就会出现在这里。";
      } else if (state.query || state.stage) {
        title = "没有匹配的项目";
        hint = "换个关键词或切回「全部」。搜索会匹配标题、UP 名与 BV 号。";
      } else {
        title = "素材库还是空的";
        hint = "粘贴 B站 链接下载，或点右上角「导入本地」把已有视频加进来。";
      }
      wrap.appendChild(ui.empty(title, hint, ui.h("button", {
        class: "btn btn--primary",
        text: "去新建任务",
        onclick: function () { WB.router.go("newtask"); }
      })));
      return wrap;
    }

    var list = ui.h("div", { class: "lib-list" });
    rows.forEach(function (row, index) {
      list.appendChild(paintRow(row, index));
    });
    wrap.appendChild(list);

    if (state.hasMore) {
      wrap.appendChild(
        ui.h("button", {
          class: "lib-sentinel",
          id: "lib-sentinel",
          type: "button",
          text: state.loadingMore ? "正在加载…" : "加载更多…",
          onclick: loadMore
        })
      );
    } else {
      wrap.appendChild(ui.h("div", { class: "lib-end", text: "已全部加载" }));
    }
    return wrap;
  }

  function sortSelect() {
    var select = ui.h("select", { class: "select lib-sort", "aria-label": "排序方式" });
    SORTS.forEach(function (item) {
      select.appendChild(
        ui.h("option", { value: item.key, text: item.text, selected: state.sort === item.key })
      );
    });
    select.addEventListener("change", function () {
      state.sort = validSort(select.value);
      if (state.smartView === "recent" && state.sort !== "created_desc") state.smartView = "";
      syncUrl();
      load({ resetSelection: true });
    });
    return select;
  }

  function paintBulkBar() {
    var count = selectedCount();
    var transcribeBtn = ui.h("button", {
      class: "btn",
      id: "lib-bulk-transcribe",
      text: "转录",
      disabled: count === 0,
      onclick: transcribeSelected
    });
    var archiveBtn = ui.h("button", {
      class: "btn",
      id: "lib-bulk-archive",
      text: state.includeArchived ? "恢复" : "归档",
      disabled: count === 0,
      onclick: archiveSelected
    });
    var removeBtn = ui.h("button", {
      class: "btn btn--danger",
      id: "lib-bulk-remove",
      text: "移除",
      disabled: count === 0,
      onclick: removeSelected
    });
    var selectAll = ui.h("button", {
      class: "btn btn--ghost",
      id: "lib-select-all",
      text: "",
      hidden: true,
      onclick: toggleAllMatching
    });

    return ui.h("div", { class: "lib-bulkbar", id: "lib-bulkbar" }, [
      ui.h("label", { class: "lib-check", onclick: function (event) { event.stopPropagation(); } }, [
        ui.h("input", {
          type: "checkbox",
          id: "lib-select-page",
          class: "lib-check__box",
          onchange: function (event) { togglePage(event.target.checked); }
        }),
        ui.h("span", { text: "本页全选" })
      ]),
      ui.h("span", { class: "lib-bulkbar__count", id: "lib-selected-count", text: "已选 " + count + " 项" }),
      selectAll,
      ui.h("span", { class: "lib-bulkbar__actions" }, [transcribeBtn, archiveBtn, removeBtn])
    ]);
  }

  function syncSelectionUI() {
    var rows = visibleItems();
    var pageSelected = 0;
    rows.forEach(function (row) { if (isSelected(row.id)) pageSelected++; });
    var allPage = rows.length > 0 && pageSelected === rows.length;

    var pageBox = document.getElementById("lib-select-page");
    if (pageBox) {
      pageBox.checked = allPage;
      pageBox.indeterminate = pageSelected > 0 && !allPage;
    }

    var count = selectedCount();
    var countEl = document.getElementById("lib-selected-count");
    if (countEl) countEl.textContent = "已选 " + count + " 项";

    var allBtn = document.getElementById("lib-select-all");
    if (allBtn) {
      if (state.allMatching) {
        allBtn.hidden = false;
        allBtn.textContent = "取消全选（全部 " + state.total + " 项）";
      } else if (allPage && state.total > rows.length && state.smartView !== "missing") {
        // 缺文件视图只过滤已加载项，服务端没有对应筛选；禁止升级成“全部匹配”。
        allBtn.hidden = false;
        allBtn.textContent = "选择全部 " + state.total + " 项";
      } else {
        allBtn.hidden = true;
      }
    }

    var transcribeBtn = document.getElementById("lib-bulk-transcribe");
    if (transcribeBtn) {
      transcribeBtn.disabled = count === 0 || state.allMatching;
      transcribeBtn.title = state.allMatching ? "「全部匹配」只支持移除；转录请先取消全选" : "";
    }
    var archiveBtn = document.getElementById("lib-bulk-archive");
    if (archiveBtn) {
      archiveBtn.disabled = count === 0 || state.allMatching;
      archiveBtn.title = state.allMatching ? "「全部匹配」只支持移除；归档请先取消全选" : "";
    }
    var removeBtn = document.getElementById("lib-bulk-remove");
    if (removeBtn) removeBtn.disabled = count === 0;

    document.querySelectorAll(".lib-check__box[data-id]").forEach(function (box) {
      box.checked = isSelected(box.getAttribute("data-id"));
    });
  }

  function togglePage(checked) {
    var rows = visibleItems();
    if (state.allMatching) {
      state.allMatching = false;
      state.selectedIds = {};
    }
    rows.forEach(function (row) {
      if (checked) state.selectedIds[row.id] = true;
      else delete state.selectedIds[row.id];
    });
    state.anchorIndex = -1;
    syncSelectionUI();
  }

  function toggleRow(id, index, event) {
    var checked = event && event.target ? event.target.checked : !isSelected(id);
    if (state.allMatching) {
      state.allMatching = false;
      state.selectedIds = {};
    }
    var rows = visibleItems();
    if (event && event.shiftKey && state.anchorIndex >= 0) {
      var start = Math.min(state.anchorIndex, index);
      var end = Math.max(state.anchorIndex, index);
      for (var i = start; i <= end; i++) {
        if (rows[i]) {
          if (checked) state.selectedIds[rows[i].id] = true;
          else delete state.selectedIds[rows[i].id];
        }
      }
    } else {
      if (checked) state.selectedIds[id] = true;
      else delete state.selectedIds[id];
      state.anchorIndex = index;
    }
    syncSelectionUI();
  }

  function toggleAllMatching() {
    if (state.smartView === "missing") return;
    if (state.allMatching) {
      state.allMatching = false;
      state.selectedIds = {};
    } else {
      state.allMatching = true;
      state.selectedIds = {};
    }
    syncSelectionUI();
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

  function paintRow(row, index) {
    var active = state.selected === row.id;
    var missing = (row.missing || []).length > 0;
    return ui.h(
      "div",
      {
        class: "lib-row lib-row--select" + (active ? " is-active" : "") + (missing ? " is-missing" : "") + (isArchived(row) ? " is-archived" : ""),
        role: "button",
        tabindex: "0",
        "data-id": row.id,
        onclick: function () { select(row.id, { force: true }); },
        onkeydown: function (event) {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            select(row.id, { force: true });
          }
        }
      },
      [
        ui.h("label", { class: "lib-row__check", onclick: function (event) { event.stopPropagation(); } }, [
          ui.h("input", {
            type: "checkbox",
            class: "lib-check__box",
            "data-id": row.id,
            "aria-label": "选择 " + (row.title || "项目"),
            checked: isSelected(row.id),
            onclick: function (event) {
              event.stopPropagation();
              toggleRow(row.id, index, event);
            }
          })
        ]),
        thumbBox(row),
        ui.h("span", { class: "lib-row__body" }, [
          ui.h("span", { class: "lib-row__title truncate", text: row.title || "(无标题)" }),
          ui.h("span", { class: "lib-row__meta" }, [
            stagePill(row.stage),
            ui.h("span", { class: "muted mono", text: ui.formatDuration(row.duration_sec) }),
            ui.h("span", { class: "muted truncate", text: row.up_name || row.source_kind || "" })
          ])
        ]),
        ui.h("span", { class: "lib-row__tail" }, [
          isArchived(row) ? ui.h("span", { class: "pill", text: "已归档" }) : null,
          missing ? ui.h("span", { class: "lib-row__flag", text: "文件缺失" }) : null,
          isArchived(row)
            ? ui.h("button", {
                class: "btn btn--ghost lib-row__action",
                text: "恢复",
                onclick: function (event) {
                  event.stopPropagation();
                  setArchived([row.id], false);
                }
              })
            : null
        ])
      ]
    );
  }

  function select(id, opts) {
    if (!id) return;
    opts = opts || {};
    if (!opts.force && state.selected === id && state.detail && !state.detailLoading) return;
    state.selected = id;
    state.detail = null;
    state.detailLoading = true;
    state.detailError = "";
    if (!opts.keepNotice) state.detailNotice = "";
    detailToken += 1;
    var token = detailToken;
    applyActiveRow();
    refreshDetail();
    call("library.get", { id: id })
      .then(function (data) {
        if (token !== detailToken) return;
        var project = (data && data.project) || null;
        if (!project) {
          handleDetailError(id, { code: "NOT_FOUND", message: "项目不存在" });
          return;
        }
        state.detail = project;
        state.detailLoading = false;
        refreshDetail();
      })
      .catch(function (err) {
        if (token !== detailToken) return;
        handleDetailError(id, err || {});
      });
  }

  function handleDetailError(id, err) {
    state.detailLoading = false;
    state.detail = null;
    if (err && err.code === "NOT_FOUND") {
      state.detailNotice = "项目不存在或已被移除";
      var index = -1;
      for (var i = 0; i < state.items.length; i++) {
        if (state.items[i].id === id) { index = i; break; }
      }
      if (index >= 0) state.items.splice(index, 1);
      if (state.total > 0) state.total -= 1;
      if (state.serverTotal > 0) state.serverTotal -= 1;
      var rows = visibleItems();
      var next = rows.length ? rows[Math.min(index < 0 ? 0 : index, rows.length - 1)] : null;
      state.selected = next ? next.id : "";
      state.detailError = next ? "" : "项目不存在或已被移除";
      paint();
      if (next) select(next.id, { keepNotice: true, force: true });
      else ui.toast("项目不存在或已被移除", "warning");
      return;
    }
    state.detailError = (err && err.message) || "读取详情失败";
    refreshDetail();
  }

  function applyActiveRow() {
    var nodes = document.querySelectorAll(".lib-row[data-id]");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute("data-id") === state.selected) nodes[i].classList.add("is-active");
      else nodes[i].classList.remove("is-active");
    }
  }

  function refreshDetail() {
    var old = document.querySelector(".pane3__detail");
    var next = paintDetail();
    if (old && old.parentNode) old.parentNode.replaceChild(next, old);
    else paint();
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
    var col = ui.h("div", { class: "pane3__col pane3__col--wide pane3__detail" }, []);
    if (state.detailNotice) {
      col.appendChild(ui.h("p", { class: "warn-line", text: state.detailNotice }));
    }
    if (!state.selected) {
      col.appendChild(
        ui.empty(
          state.detailError || "选一个项目",
          state.detailError ? "它可能已经被移除；列表会自动选中下一项。" : "左栏筛选归属，中栏点一行，这里显示它的资产与产物。"
        )
      );
      return col;
    }
    if (state.detailLoading) {
      col.appendChild(ui.h("div", { class: "empty" }, [ui.h("div", { class: "empty__hint", text: "读取详情…" })]));
      return col;
    }
    if (!state.detail) {
      col.appendChild(ui.empty("读取详情失败", state.detailError || "请稍后重试。"));
      return col;
    }

    var row = state.detail;
    var missing = row.missing || [];
    var card = ui.h("div", { class: "card" }, [
      ui.h("div", { class: "card__head" }, [
        ui.h("h3", { class: "card__title truncate", text: row.title || "(无标题)" }),
        stagePill(row.stage)
      ])
    ]);

    // 有封面才给 16:9 的大图槽。没有的时候那个空槽是一大块纯黑，
    // 比“没有封面”这件事本身更抢眼，所以干脆不占位。
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
      canTranscribeStage(row.stage)
        ? ui.h("button", {
            class: "btn btn--primary",
            text: "开始转录",
            onclick: function () { transcribe([row.id], row.stage); }
          })
        : null,
      // 已经转过的项目必须留重转入口：选错语言是这套流程里最常出的错。
      hasTextStage(row.stage)
        ? ui.h("button", {
            class: "btn",
            text: "重新转录",
            onclick: function () { transcribe([row.id], row.stage); }
          })
        : null,
      hasTextStage(row.stage)
        ? ui.h("button", {
            class: "btn",
            text: "字幕",
            onclick: function () { WB.router.go("subtitles", { project: row.id }); }
          })
        : null,
      isArchived(row)
        ? ui.h("button", {
            class: "btn",
            text: "恢复",
            onclick: function () { setArchived([row.id], false); }
          })
        : ui.h("button", {
            class: "btn",
            text: "归档",
            onclick: function () { setArchived([row.id], true); }
          }),
      ui.h("button", {
        class: "btn btn--danger",
        text: "从库中移除",
        onclick: function () { removeIds([row.id]); }
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
          ["集号", row.collection_index === null || row.collection_index === undefined ? "—" : String(row.collection_index + 1), "mono"],
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
                ui.h("td", null, [ui.h("div", { class: "truncate mono cell-path", title: asset.path || "", text: baseName(asset.path) })]),
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

  // ------------------------------------------------------------------ 批量操作

  // 源语言清单只取一次。100 个码不在前端手抄——后端 infra/languages.py 是唯一真源。
  var langCache = null;

  function languageChoices() {
    if (langCache) return Promise.resolve(langCache);
    return call("transcribe.languages").then(function (data) {
      langCache = data || { common: [], all: [] };
      return langCache;
    });
  }

  function askLanguage(retry) {
    return languageChoices()
      .catch(function () {
        return { common: [{ code: "", label: "自动检测" }], all: [] };
      })
      .then(function (data) {
        return ui.pick({
          title: retry ? "重新转录 · 选择源语言" : "选择源语言",
          hint: "源语言决定识别准确度。自动检测只分析开头一小段，中英混排或开头没说话的素材容易判错。",
          options: data.common,
          more: data.all,
          moreLabel: "其余 " + (data.all || []).length + " 种语言",
          confirmText: retry ? "重新转录" : "开始转录"
        });
      });
  }

  function transcribe(ids, stage) {
    var retry = hasTextStage(stage);
    return askLanguage(retry)
      .then(function (language) {
        // null = 用户关掉了弹窗（取消 / ESC / 点遮罩）。一个 RPC 都不发。
        if (language === null) return null;
        return call("transcribe.start", {
          project_ids: ids,
          language: language || ""
        }).then(function (result) {
          var jobs = (result && result.jobs) || [];
          ui.toast(jobs.length ? "已开始转录，可在任务里看进度" : "没有可转录的项目", "success");
          load({ resetSelection: false });
          return result;
        });
      })
      .catch(function (err) {
        ui.toast("启动转录失败：" + ((err && err.message) || err), "error");
      });
  }

  function transcribeSelected() {
    var ids = Object.keys(state.selectedIds);
    if (!ids.length) return;
    // 多个项目阶段可能不同：只要有一个已经转过，就按“重新转录”算。
    var retry = visibleItems().some(function (row) {
      return ids.indexOf(row.id) >= 0 && hasTextStage(row.stage);
    });
    askLanguage(retry).then(function (language) {
      if (language === null) return null;
      return call("transcribe.start", { project_ids: ids, language: language || "" }).then(function (result) {
        var jobs = (result && result.jobs) || [];
        ui.toast(jobs.length ? "已提交 " + jobs.length + " 个转录任务" : "没有可转录的项目", "success");
        clearSelection(false);
        load({ resetSelection: false });
      });
    }).catch(function (err) {
      ui.toast("启动转录失败：" + ((err && err.message) || err), "error");
    });
  }

  function setArchived(ids, archived) {
    if (!ids.length) return;
    call("library.archive", { ids: ids, archived: archived })
      .then(function (result) {
        var changed = (result && result.changed) || 0;
        var skipped = (result && result.skipped) || [];
        ui.toast(
          (archived ? "已归档 " : "已恢复 ") + changed + " 项" + (skipped.length ? "，跳过 " + skipped.length + " 项" : ""),
          skipped.length ? "warning" : "success"
        );
        clearSelection(false);
        load({ resetSelection: false });
      })
      .catch(function (err) {
        ui.toast((err && err.message) || (archived ? "归档失败" : "恢复失败"), "error");
      });
  }

  function archiveSelected() {
    if (state.allMatching) return;
    var ids = Object.keys(state.selectedIds);
    if (!ids.length) return;
    setArchived(ids, !state.includeArchived);
  }

  function removeSelected() {
    if (state.allMatching) {
      removeByPayload({ query: state.query, stage: state.stage }, true);
      return;
    }
    removeIds(Object.keys(state.selectedIds));
  }

  function removeIds(ids) {
    if (!ids.length) return;
    removeByPayload({ ids: ids }, false);
  }

  function removeByPayload(payload, useFilter) {
    var expect = 0;
    call("library.delete_preview", payload)
      .then(function (preview) {
        var total = (preview && preview.total) || 0;
        if (!total) {
          ui.toast("没有可移除的项目", "info");
          return null;
        }
        // 预览数才是这次真正要删的条数：10 个选中里 1 个已被别人删掉时，
        // 应该删 9 个、报 1 个 skipped，而不是拿 10 去撞后端 CONFLICT。
        // 「全部匹配」仍先对账当前筛选总数，列表变了就不删。
        if (useFilter && total !== state.total) {
          ui.toast("列表已变化，请刷新后重试", "warning");
          load({ resetSelection: false });
          return null;
        }
        expect = total;
        var message = "将移除 " + total + " 项，其中 " + ((preview && preview.with_draft) || 0) + " 项有未发布草稿。";
        if (preview && preview.with_clip) message += "其中 " + preview.with_clip + " 项含切片产物。";
        if (preview && preview.with_candidate) message += "其中 " + preview.with_candidate + " 项含候选。";
        var samples = (preview && preview.sample_titles) || [];
        if (samples.length) message += "例如：" + samples.slice(0, 3).join("、") + "。";
        message += "删除前会在项目目录写一份 JSON 快照；磁盘上的视频文件不动。";
        return ui.confirm("移除这些项目？", message, { confirmText: "移除", danger: true }).then(function (ok) {
          if (!ok) return null;
          var params = {};
          Object.keys(payload).forEach(function (key) { params[key] = payload[key]; });
          params.expect_total = expect;
          return call("library.delete_many", params).then(function (result) {
            var removed = (result && result.removed) || 0;
            var skipped = (result && result.skipped) || [];
            ui.toast(
              "已移除 " + removed + " 项" + (skipped.length ? "，跳过 " + skipped.length + " 项" : ""),
              skipped.length ? "warning" : "success"
            );
            clearSelection(false);
            state.selected = "";
            state.detail = null;
            load({ resetSelection: false });
          });
        });
      })
      .catch(function (err) {
        ui.toast((err && err.message) || "移除失败", "error");
      });
  }

  // ------------------------------------------------------------------ 数据

  function listParams(offset) {
    var params = {
      query: state.query,
      limit: PAGE_LIMIT,
      offset: offset,
      sort: state.sort,
      include_archived: state.includeArchived
    };
    if (state.stage) params.stage = state.stage;
    return params;
  }

  // 左栏树只看“来源”，不带 stage；否则点一次「已总结」就把 39 集大组从树上裁掉。
  function treeParams() {
    var params = listParams(0);
    delete params.stage;
    return params;
  }

  // 分页判断必须用“已加载的原始条数”，不能用 visibleItems().length：
  // 归档视图会把未归档行裁掉，用可见条数算 offset 会反复翻同一页。
  function computeHasMore() {
    if (state.loadedRawCount >= state.serverTotal) return false;
    if (state.includeArchived && state.hasArchivedTotal) {
      var archivedLoaded = state.items.filter(isArchived).length;
      return archivedLoaded < state.archivedTotal;
    }
    return true;
  }

  function applyMeta(data, treeData) {
    var items = (data && data.items) || [];
    state.items = items;
    state.loadedRawCount = items.length;
    state.serverTotal = (data && typeof data.total === "number") ? data.total : items.length;
    state.treeItems = treeData ? ((treeData.items) || []) : null;
    state.treeTotal = treeData
      ? ((typeof treeData.total === "number") ? treeData.total : state.treeItems.length)
      : state.serverTotal;
    state.hasArchivedTotal = !!(data && typeof data.archived_total === "number");
    state.archivedTotal = state.hasArchivedTotal
      ? data.archived_total
      : items.filter(isArchived).length;
    state.treeArchivedTotal = treeData && typeof treeData.archived_total === "number"
      ? treeData.archived_total
      : state.archivedTotal;
    state.total = state.includeArchived ? state.archivedTotal : state.serverTotal;
    state.facets = (data && data.facets) || [];
    state.stageLabels = (data && data.stage_labels) || {};
    state.hasMore = computeHasMore();
  }

  function load(opts) {
    opts = opts || {};
    var token = ++loadToken;
    if (opts.resetSelection !== false) clearSelection(false);
    state.loading = true;
    state.loadingMore = false;
    state.error = "";
    paint();
    var mainCall = call("library.list", listParams(0));
    var treeCall = state.stage
      ? call("library.list", treeParams()).catch(function () { return null; })
      : Promise.resolve(null);
    Promise.all([mainCall, treeCall])
      .then(function (results) {
        var data = results[0];
        var treeData = results[1];
        if (token !== loadToken) return;
        state.loading = false;
        applyMeta(data, treeData);
        var rows = visibleItems();
        var exists = false;
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].id === state.selected) { exists = true; break; }
        }
        if (!exists) {
          state.selected = "";
          state.detail = null;
          state.detailLoading = false;
        }
        if (!state.selected && rows.length) {
          state.selected = rows[0].id;
          state.detail = null;
          state.detailLoading = false;
          paint();
          select(state.selected, { keepNotice: true, force: true });
          return;
        }
        paint();
        if (state.selected && !state.detail && !state.detailLoading) {
          select(state.selected, { keepNotice: true, force: true });
        }
      })
      .catch(function (err) {
        if (token !== loadToken) return;
        state.loading = false;
        state.items = [];
        state.loadedRawCount = 0;
        state.total = 0;
        state.serverTotal = 0;
        state.treeItems = null;
        state.treeTotal = 0;
        state.treeArchivedTotal = 0;
        state.hasArchivedTotal = false;
        state.hasMore = false;
        state.error = (err && err.message) || "读不到素材库";
        paint();
        ui.toast(state.error, "error");
      });
  }

  function loadMore() {
    if (state.loading || state.loadingMore || !state.hasMore) return;
    var token = loadToken;
    state.loadingMore = true;
    var sentinel = document.getElementById("lib-sentinel");
    if (sentinel) sentinel.textContent = "正在加载…";
    call("library.list", listParams(state.loadedRawCount))
      .then(function (data) {
        if (token !== loadToken) return;
        state.loadingMore = false;
        var incoming = (data && data.items) || [];
        var seen = {};
        state.items.forEach(function (row) { seen[row.id] = true; });
        incoming.forEach(function (row) {
          if (!seen[row.id]) {
            seen[row.id] = true;
            state.items.push(row);
          }
        });
        // offset 按服务端返回的原始条数推进，不用去重后的 items.length。
        state.loadedRawCount += incoming.length;
        state.serverTotal = (data && typeof data.total === "number") ? data.total : state.serverTotal;
        state.hasArchivedTotal = state.hasArchivedTotal || !!(data && typeof data.archived_total === "number");
        state.archivedTotal = (data && typeof data.archived_total === "number")
          ? data.archived_total
          : state.archivedTotal;
        state.total = state.includeArchived ? state.archivedTotal : state.serverTotal;
        state.hasMore = computeHasMore() && incoming.length > 0;
        var y = window.scrollY || 0;
        paint();
        try { window.scrollTo(0, y); } catch (err) { /* 宿主不支持也不影响数据 */ }
      })
      .catch(function (err) {
        if (token !== loadToken) return;
        state.loadingMore = false;
        ui.toast((err && err.message) || "加载更多失败", "error");
        var sentinel2 = document.getElementById("lib-sentinel");
        if (sentinel2) sentinel2.textContent = "加载更多…";
      });
  }

  function mountSentinel() {
    if (sentinelObserver) {
      sentinelObserver.disconnect();
      sentinelObserver = null;
    }
    var node = document.getElementById("lib-sentinel");
    if (!node || !state.hasMore) return;
    if (typeof window.IntersectionObserver === "function") {
      sentinelObserver = new window.IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].isIntersecting) loadMore();
        }
      }, { rootMargin: "240px" });
      sentinelObserver.observe(node);
    }
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
        load({ resetSelection: false });
      })
      .catch(function (err) {
        ui.toast((err && err.message) || "导入失败", "error");
      });
  }

  function openMedia() {
    WB.rpc("fs.media", {}, { silent: true })
      .then(function (info) {
        if (info && info.path) return WB.rpc("fs.reveal", { path: info.path }, { silent: true });
        return null;
      })
      .catch(function () {});
  }

  function search(query) {
    state.query = String(query || "");
    load({ resetSelection: true });
  }

  // 排序/归档视图写进 URL：刷新后恢复。不触发路由重绘。
  function syncUrl() {
    try {
      var parsed = WB.router.parse(location.hash || "");
      var params = parsed.params || {};
      if (state.sort === "created_desc") delete params.sort;
      else params.sort = state.sort;
      if (state.includeArchived) params.archived = "1";
      else delete params.archived;
      var query = Object.keys(params).map(function (key) {
        return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]);
      }).join("&");
      var next = "#/library" + (query ? "?" + query : "");
      history.replaceState(null, "", next);
    } catch (err) {
      /* 宿主禁止改地址也不影响本次操作，只是刷新后不恢复 */
    }
  }

  // 后端改了库（导入、下载完成、删除）就自己重拉——不需要用户去点刷新。
  // 只挂一次：模块体只执行一次，但 render 每次进页面都会跑。
  if (WB.events && WB.events.on) {
    on("library.changed", function () {
      if (document.getElementById("library-body")) load({ resetSelection: false });
    });
  }

  WB.pages = WB.pages || {};
  WB.pages.library = { render: render, search: search, state: state, reload: load, loadMore: loadMore };

  WB.router.define("library", {
    title: "素材库",
    crumb: "项目与产物",
    icon: "library",
    nav: true,
    render: render,
    destroy: function () {
      if (sentinelObserver) {
        sentinelObserver.disconnect();
        sentinelObserver = null;
      }
    }
  });
})();
