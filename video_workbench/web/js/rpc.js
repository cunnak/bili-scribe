/* ============================================================================
 * rpc.js —— 页面与 Python 之间唯一的通道
 *
 * 页面永远只做一件事：`await WB.rpc("app.info")`。
 * 它背后是 `window.pywebview.api.rpc(method, params)`，返回固定形状：
 *
 *   { ok: true,  data: ...,           request_id: "..." }
 *   { ok: false, error: {code,message,detail,retryable}, request_id: "..." }
 *
 * **为什么不把错误直接 reject 到底。** reject 之后调用点必须到处 try/catch，
 * 而大多数调用点的正确反应是一样的：一条 toast + 控制台一行 + 界面回到可用状态。
 * 所以这里把 `ok:false` 翻成一个 `RpcError` 抛出，同时**统一**在 `WB.rpc` 里
 * toast 一次（`silent: true` 可以关掉，用于"预期内的失败"，比如未配置 key）。
 *
 * **mock 模式**（`?mock=1` 或 `#mock=1`）：浏览器里打开 index.html 时没有
 * `pywebview`，这时用 mock.js 的假数据顶上。它不是"能跑就行"的摆设：
 * 方案书 8.x 的视觉验收就是靠它在浏览器里跑两套皮肤 × 全部页面。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});

  /** 统一的错误对象。`code` 直接来自 Python 的错误码表。 */
  function RpcError(code, message, detail, retryable) {
    this.name = "RpcError";
    this.code = code || "INTERNAL";
    this.message = message || "调用失败";
    this.detail = detail || "";
    this.retryable = !!retryable;
  }
  RpcError.prototype = Object.create(Error.prototype);
  RpcError.prototype.constructor = RpcError;
  WB.RpcError = RpcError;

  /** 显式要求演示数据：`?mock=1` / `#mock=1`（浏览器视觉验收与 jsdom 测试用）。 */
  var explicitMock =
    /[?&]mock=1/.test(String(location.search || "")) ||
    /[#&]mock=1/.test(String(location.hash || ""));

  /**
   * 是否在用假数据。
   *
   * **不再**把"没看到 pywebview"直接当成 mock：pywebview 是文档解析之后才注入
   * `api` 的，启动瞬间看它一眼必然是空的。早期版本这么写，结果是桌面版真的跑起来
   * 也显示 mock 的示例列表——用户看到三行假数据以为程序坏了。
   * 现在只有两种情况算 mock：URL 显式要求，或 boot() 等满超时后确认无桥（`setMock`）。
   */
  WB.isMock = function () {
    return WB._mock === true || explicitMock;
  };

  /** 由 boot() 在"等不到桥"时调用，把页面降级成演示数据。 */
  WB.setMock = function (on) {
    WB._mock = !!on;
  };

  /** 桥是否已挂上。pywebview 是在文档解析后才注入 api 的，所以要轮询等。 */
  WB.bridgeReady = function () {
    return !!(window.pywebview && window.pywebview.api && window.pywebview.api.rpc);
  };

  /**
   * 等桥出现。超时不抛错而是 resolve(false)，让调用方自己决定"降级还是报错"——
   * 抛错只会把降级的决定权藏进 catch 里，调用方还得猜是超时还是别的原因。
   */
  WB.waitBridge = function (timeoutMs) {
    if (explicitMock) return Promise.resolve(false);
    var limit = Date.now() + (timeoutMs || 8000);
    return new Promise(function (resolve) {
      (function tick() {
        if (WB.bridgeReady()) return resolve(true);
        if (Date.now() > limit) return resolve(false);
        setTimeout(tick, 40);
      })();
    });
  };

  var seq = 0;

  /** 底层调用：返回原始 `{ok, ...}` 结构，不 toast、不抛。 */
  WB.raw = function (method, params) {
    if (WB.isMock()) {
      return Promise.resolve(WB.mock ? WB.mock.call(method, params) : { ok: false, error: { code: "UNSUPPORTED", message: "mock 未装载" } });
    }
    var requestId = "r" + ++seq + "-" + Date.now().toString(36);
    return Promise.resolve(window.pywebview.api.rpc(method, params === undefined ? null : params, requestId)).catch(function (err) {
      return { ok: false, error: { code: "BRIDGE_ERROR", message: String((err && err.message) || err), detail: "", retryable: true } };
    });
  };

  /**
   * 业务调用：成功返回 `data`，失败抛 `RpcError`。
   *
   * @param {string} method  形如 "app.info"
   * @param {object} [params]
   * @param {object} [opts]  {silent:true} 不弹 toast；{fallback:any} 失败时返回它而不抛
   */
  WB.rpc = function (method, params, opts) {
    var options = opts || {};
    return WB.raw(method, params).then(function (res) {
      if (res && res.ok) return res.data;
      var error = (res && res.error) || {};
      var failure = new RpcError(error.code, error.message, error.detail, error.retryable);
      // 控制台留一份完整信息（含 detail 里的 traceback），toast 只给人看一句。
      if (window.console && console.warn) {
        console.warn("[rpc]", method, failure.code, failure.message, failure.detail);
      }
      if (!options.silent && WB.ui && WB.ui.toast) {
        WB.ui.toast(failure.message, failure.code === "VALIDATION" ? "warning" : "error");
      }
      if (Object.prototype.hasOwnProperty.call(options, "fallback")) return options.fallback;
      throw failure;
    });
  };

  /**
   * 本地绝对路径 → 可放进 `<img src>` 的 URL。
   *
   * 页面本身是 `file://` 载入的（不跑本地 HTTP 服务），所以同协议读本地文件可行；
   * 换成 `http` 模式时由 Python 侧改成 `/media?path=…`，页面这层不用动。
   * 路径里有中文/空格时**必须** encodeURI，否则 WebView2 会拿不到图。
   */
  WB.mediaUrl = function (path) {
    if (!path) return "";
    var text = String(path).replace(/\\/g, "/");
    if (/^[a-zA-Z]:\//.test(text)) return "file:///" + encodeURI(text);
    if (text.charAt(0) === "/") return "file://" + encodeURI(text);
    return encodeURI(text);
  };

  /** 批量：并行取多个接口，全部成功才 resolve，任一失败即抛（避免半截界面）。 */
  WB.rpcAll = function (list) {
    return Promise.all(
      list.map(function (item) {
        return WB.rpc(item[0], item[1], { silent: true });
      })
    );
  };
})();
