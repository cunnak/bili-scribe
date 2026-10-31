/* ============================================================================
 * router.js —— hash 路由
 *
 * **为什么是 hash 而不是 History API。** 界面是 `file://` 加载的（不占端口），
 * History.pushState 在 `file://` 下会把路径变成"看起来像文件"的地址，
 * 刷新就 404。hash 不参与文件解析，且和桌面版的启动参数（`#skin=ark`）能共存。
 *
 * **页面是函数，不是字符串。** 每个页面导出一个 `render(ctx) -> Node`，
 * 路由只负责"换掉 #view 里的东西 + 高亮左栏 + 改标题"。页面自己管自己的数据加载，
 * 因为它才知道自己需要什么，集中在一个 switch 里拉数据只会越写越糊。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  var routes = {};
  var order = [];
  var current = null;
  var onChange = null;

  var router = (WB.router = {
    /** 注册一个页面。`nav:true` 的会出现在左栏（按注册顺序）。 */
    define: function (name, definition) {
      routes[name] = definition;
      order.push(name);
      return router;
    },

    names: function () {
      return order.slice();
    },

    /** 左栏用的表：只取 nav 的，且带 caption/icon 供渲染。 */
    navItems: function () {
      return order
        .filter(function (name) {
          return routes[name] && routes[name].nav;
        })
        .map(function (name) {
          return {
            route: name,
            title: routes[name].title,
            icon: routes[name].icon || "library"
          };
        });
    },

    current: function () {
      return current;
    },

    /** 解析 `#/library?x=1` → {route:"library", params:{x:"1"}}。 */
    parse: function (hash) {
      var raw = String(hash || "").replace(/^#/, "");
      // `#skin=ark` 是启动参数，不是路由
      if (!raw || raw.charAt(0) !== "/") return { route: "", params: {} };
      var qIndex = raw.indexOf("?");
      var path = qIndex >= 0 ? raw.slice(0, qIndex) : raw;
      var query = qIndex >= 0 ? raw.slice(qIndex + 1) : "";
      var params = {};
      if (query) {
        query.split("&").forEach(function (pair) {
          if (!pair) return;
          var kv = pair.split("=");
          params[decodeURIComponent(kv[0])] = decodeURIComponent(kv.slice(1).join("=") || "");
        });
      }
      return { route: path.replace(/^\//, ""), params: params };
    },

    go: function (route, params) {
      var query = params
        ? Object.keys(params)
            .map(function (key) {
              return encodeURIComponent(key) + "=" + encodeURIComponent(params[key]);
            })
            .join("&")
        : "";
      var next = "#/" + route + (query ? "?" + query : "");
      // 先写 hash（让后退键和地址栏有意义），但**不依赖 hashchange**。
      // file:// 下的 WebView2、jsdom、以及"hash 本来是同一个值"这三种情况都可能
      // 不派发 hashchange；只写 hash 的写法会表现为"点了没反应"。
      try {
        if (location.hash !== next) location.hash = next;
      } catch (err) {
        /* 宿主禁止改 hash 也无所谓，下面直接渲染 */
      }
      renderRoute({ route: route, params: params || {}, hash: next });
    },

    /** 首次进入的默认路由。 */
    start: function (fallback) {
      // 监听器**无条件**挂一次。早先写成 `if (onChange === null)`，意思是
      // "没人订阅 onChange 才需要自己听 hash"——而 app.js 恰好订阅了，
      // 于是后退键、手改地址栏、以及验收脚本里设 hash 全都成了死路：
      // 地址变了、界面不动。重复 start 由 started 兜住。
      if (!started) {
        window.addEventListener("hashchange", resolve);
        started = true;
      }
      if (!location.hash || location.hash.indexOf("#/") !== 0) {
        location.hash = "#/" + (fallback || order[0] || "library");
        // 某些宿主不会因为设置 hash 触发 hashchange（同一文档内已存在时），
        // 所以这里补一次 resolve；重复 resolve 由 current 判断消化。
        resolve();
      } else {
        resolve();
      }
    },

    onChange: function (fn) {
      onChange = fn;
      return router;
    },

    resolve: resolve
  });

  var started = false;

  function resolve() {
    renderRoute(router.parse(location.hash));
  }

  /** 真正的渲染入口。go() 与 hashchange 都走这里，保证两条路径结果一致。 */
  function renderRoute(parsed) {
    var name = parsed.route;
    if (!name || !routes[name]) {
      // 未知路由（旧书签 / 手改地址）回默认页，而不是留一片空白
      name = order[0] || "library";
      if (parsed.route) {
        try {
          location.hash = "#/" + name;
        } catch (err) {
          /* 同上：改不了就只渲染 */
        }
        parsed = { route: name, params: {} };
      }
    }
    var definition = routes[name];
    var view = document.getElementById("view");
    if (!view) return;
    current = { route: name, params: parsed.params };
    view.textContent = "";
    var node = definition.render({ params: parsed.params, route: name });
    if (node) view.appendChild(node);
    highlight(name);
    if (onChange) onChange(current, definition);
  }

  function highlight(name) {
    var items = document.querySelectorAll(".rail__item[data-route]");
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.dataset.route === name) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
  }

  /** 左栏渲染：路由表 + 计数占位。折叠态由 `<html data-rail>` 决定，这里不管。 */
  router.renderRail = function (host) {
    if (!host) return;
    host.textContent = "";
    router.navItems().forEach(function (item) {
      host.appendChild(
        ui.h(
          "button",
          {
            class: "rail__item",
            type: "button",
            dataset: { route: item.route },
            title: item.title,
            onclick: function () {
              router.go(item.route);
            }
          },
          [
            ui.h("span", { html: ui.svg(item.icon) }),
            ui.h("span", { class: "rail__label", text: item.title }),
            ui.h("span", { class: "rail__count" })
          ]
        )
      );
    });
  };
})();
