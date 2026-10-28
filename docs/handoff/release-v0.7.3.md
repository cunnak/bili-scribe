## 这个版本解决什么问题

**右键菜单打不开——日志说开了，实际没开。**

用户原话：「右键菜单又打不开了」。它挡的是最常用的一步：粘贴链接。`Ctrl+V` 也许还能用，但人的第一反应是右键，右键没反应就等于「输入框坏了」。

更麻烦的是**日志在撒谎**：`bili-scribe.log` 里明明有一行

```
[ctx] 已打开并读回：AreDefaultContextMenusEnabled / AreBrowserAcceleratorKeysEnabled
```

这行证明了「我们把值设进去了、而且读回来还是 True」，却依然没有菜单。说明写的**不是那个在渲染页面的实例**。

## 根因：写错了对象

pywebview 的窗口里有两个东西都能拿到 `CoreWebView2`：

| 拿法 | 是不是真在渲染的控件 |
|---|---|
| `form.webview.CoreWebView2`（我们原来用的） | **不一定**。属性读回 True，界面毫无变化 |
| `sender.CoreWebView2`（`on_webview_ready(self, sender, args)` 的第一个参数） | **是**。pywebview 自己就是往它上面写 |

`webview/platforms/edgechromium.py:283` 的 `on_webview_ready` 里，pywebview 用 `sender` 设置所有选项：

```python
settings.AreBrowserAcceleratorKeysEnabled = _state['debug']
settings.AreDefaultContextMenusEnabled = _state['debug']
```

`_state['debug']` 默认 `False`。它写的是对的对象，所以 `--debug` 启动菜单就正常；我们写的是另一个实例，所以就算读回 True 也没用。

源码直接跑时两者恰好指向同一个实例，问题只在打包成 exe 后暴露——典型的「开发机能用、打包就坏」。

## 改了什么

`bili_scribe/desktop.py`：

| 之前 | 现在 |
|---|---|
| 轮询 `form.webview.CoreWebView2` 写开关 | **包一层 `EdgeChrome.on_webview_ready`**，在它写完之后用它的 `sender` 再写一遍 |
| 读回 True 就认为成功（假信号） | 读回的是 `sender` 上的真实值 |
| 依赖时序：谁后写谁赢 | 不再抢时序：写在 pywebview 之后，同一个对象 |

新增 `_patch_webview_settings_hook()`：幂等，`attach()` 时安装（`webview.start()` 之前），`_enable_context_menu` 只负责把它挂上。

## 验证（差分实验）

同一台机器、同一个输入框、右键截图对比：

| 版本 | 右键结果 |
|---|---|
| 0.7.2 exe | **无菜单**（只有输入框焦点环） |
| 0.7.3 exe + 读回 + 6 秒看守 | **无菜单**，日志却写着「已打开并读回」 |
| 0.7.3 exe + 45 秒看守 + `loaded` 再补一轮 | **无菜单**，全程没有「被盖回去」 |
| 0.7.3 exe + `sender` 补丁 | **出菜单**：表情符号 / 撤销 / 剪切 / 复制 / 粘贴 / 粘贴为纯文本 / 全选 / 书写方向 / 更多工具 |

前三次全败、第四次才通，这个顺序本身就是证据：问题不在「设置生效的时机」，在「设置写给了谁」。

## 兼容性

- 只影响 Windows 桌面端（WebView2 后端）。`--debug` 的行为不变。
- 浏览器版 / 服务模式不经过这段代码。
