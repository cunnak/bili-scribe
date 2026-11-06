"""按皮肤、按页面给 VideoWorkbench 界面截图（视觉验收用）。

为什么走 headless Chrome + `?mock=1` 而不是开真窗口：
桌面版窗口每截一张都要起一次 pywebview，两套皮肤 × 四五个页面就是十几次冷启动；
而这里要看的是**像素**（配色、密度、对齐），假数据完全够用。真后端那条线由
`pytest tests/wb` 与 `tests/frontend_test.js` 的契约断言守着，两边各管一段。

用法：
    python scripts/shoot_wb.py --out docs/acceptance/p1
    python scripts/shoot_wb.py --skin exhibition --out build/shots

页面清单写死在 SHOTS 里，加页面就加一行——刻意不做"自动遍历路由"：
路由表里有登录态的 settings 页，自动遍历会把它连同半截渲染一起截进来。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websockets

PROJ = Path(__file__).resolve().parents[1]
INDEX = PROJ / "video_workbench" / "web" / "index.html"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# (文件名, hash 路由, 截图前要执行的界面动作)
# 路由必须写成 `#/xxx`：app.js 在 boot 时会把 hash 规范成带斜杠的形式，
# 写成 `#xxx` 会被当场纠正回默认页（踩过，五张图全一样）。
SHOTS: list[tuple[str, str, str]] = [
    ("library", "#/library", ""),
    ("library-detail", "#/library?id=p_demo_1", ""),
    ("newtask", "#/newtask", ""),
    (
        "newtask-parse",
        "#/newtask",
        """
        (async () => {
          const input = document.querySelector('#newtask-url');
          input.value = 'https://www.bilibili.com/video/BV1xx411c7mD';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '解析');
          btn.click();
          await new Promise(r => setTimeout(r, 600));
        })()
        """,
    ),
    ("subtitles", "#/subtitles?project=p_demo_1", ""),
    (
        "clips",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const card = document.querySelectorAll('.clip-card')[1];
          if (card) card.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          await new Promise(r => setTimeout(r, 400));
        })()
        """,
    ),
    (
        "clips-export",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const card = document.querySelectorAll('.clip-card')[0];
          if (card) card.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          await new Promise(r => setTimeout(r, 300));
          const btn = [...document.querySelectorAll('.clip-inspect button')]
            .find(b => b.textContent.trim() === '+1帧');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 400));
        })()
        """,
    ),
    (
        # 导出队列要有内容才算验过：mock 的导出任务 400ms 完成，队列轮询 800ms 一拍
        "clips-queue",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const card = document.querySelectorAll('.clip-card')[0];
          if (card) card.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          await new Promise(r => setTimeout(r, 300));
          const mode = document.querySelectorAll('.exp-form select')[1];
          if (mode) {
            mode.value = 'fast';
            mode.dispatchEvent(new Event('change', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 200));
          const go = [...document.querySelectorAll('.exp-form button')]
            .find(b => b.textContent.indexOf('导出这一段') >= 0);
          if (go) go.click();
          await new Promise(r => setTimeout(r, 1500));
        })()
        """,
    ),
    (
        # 竖屏：切到 9:16 + 人脸跟随 + 开安全区，导出面板要能看出这三件事
        "clips-vertical",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const card = document.querySelectorAll('.clip-card')[0];
          if (card) card.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          await new Promise(r => setTimeout(r, 300));
          const aspect = [...document.querySelectorAll('.exp-form select')]
            .find(s => [...s.options].some(o => o.value === '9x16'));
          if (aspect) {
            aspect.value = '9x16';
            aspect.dispatchEvent(new Event('change', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 300));
          const vmode = [...document.querySelectorAll('.exp-form select')]
            .find(s => [...s.options].some(o => o.value === 'follow'));
          if (vmode) {
            vmode.value = 'follow';
            vmode.dispatchEvent(new Event('change', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 400));
          const safe = document.querySelector('.exp-form__check input');
          if (safe) {
            safe.checked = true;
            safe.dispatchEvent(new Event('change', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 400));
        })()
        """,
    ),
    (
        # 切片文案：选第一条 → 写文案 → 抽 4 帧候选（文案与封面一起入镜）
        "clips-copy",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const card = document.querySelectorAll('.clip-card')[0];
          if (card) card.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
          await new Promise(r => setTimeout(r, 300));
          const write = [...document.querySelectorAll('.copy-pane button')]
            .find(b => b.textContent.indexOf('写文案') >= 0);
          if (write) write.click();
          await new Promise(r => setTimeout(r, 1500));
          const grab = [...document.querySelectorAll('.copy-pane button')]
            .find(b => b.textContent.indexOf('抽 4 帧') >= 0);
          if (grab) grab.click();
          await new Promise(r => setTimeout(r, 500));
        })()
        """,
    ),
    (
        # 批量导出：勾前两条 → 批量导出 → 等队列落地（一条故意失败，抬头写 1 失败）
        "clips-batch",
        "#/clipper",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const boxes = [...document.querySelectorAll('.clip-card__pick')];
          [0, 1].forEach(i => {
            if (!boxes[i]) return;
            boxes[i].checked = true;
            boxes[i].dispatchEvent(new Event('change', {bubbles: true}));
          });
          await new Promise(r => setTimeout(r, 150));
          const go = [...document.querySelectorAll('.clip-picks button')]
            .find(b => b.textContent.indexOf('批量导出') >= 0);
          if (go) go.click();
          await new Promise(r => setTimeout(r, 1800));
          // 不滚动：整页滚动会把播放器挤出视口、中间一列变空白。要看导出队列就加高视口拍（--height 1500）。
        })()
        """,
    ),
    (
        # 合并页：加两条同规格切片（可快速拼接），再反转一次证明顺序会变
        "merge",
        "#/merge",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const srcs = [...document.querySelectorAll('.merge-src')];
          [0, 1].forEach(i => { if (srcs[i]) srcs[i].click(); });
          await new Promise(r => setTimeout(r, 700));
          const rev = [...document.querySelectorAll('button')].find(b => b.textContent === '反转');
          if (rev) rev.click();
          await new Promise(r => setTimeout(r, 600));
        })()
        """,
    ),
    (
        # 合并页：混入一条 720p 素材 → 差异表出现、快速拼接自动禁用
        "merge-diff",
        "#/merge",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
        const pick = async (which) => {
          // 每次点击后左栏会重渲染，旧的 DOM 引用就失效了（点上去毫无反应），
          // 所以每次都要重新查询。
          const sel = which === 'lib'
            ? '.merge-src[data-origin="library"]'
            : '.merge-src[data-origin="clips"], .merge-src:not([data-origin="library"])';
          const el = document.querySelector(sel);
          if (el) el.click();
        };

          await pick('clip');   // 切片 1280×720/25fps
          await new Promise(r => setTimeout(r, 600));
          await pick('lib');    // 素材库源片 1920×1080/30fps
          await new Promise(r => setTimeout(r, 1200));
        })()
        """,
    ),
    (
        # 合并页：精确合并跑完（规格不一致时快速被禁，只剩精确这一条路）
        "merge-precise",
        "#/merge",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
        const pick = async (which) => {
          // 每次点击后左栏会重渲染，旧的 DOM 引用就失效了（点上去毫无反应），
          // 所以每次都要重新查询。
          const sel = which === 'lib'
            ? '.merge-src[data-origin="library"]'
            : '.merge-src[data-origin="clips"], .merge-src:not([data-origin="library"])';
          const el = document.querySelector(sel);
          if (el) el.click();
        };

          await pick('clip');
          await new Promise(r => setTimeout(r, 500));
          await pick('lib');
          await new Promise(r => setTimeout(r, 1200));
          const precise = [...document.querySelectorAll('input[type=radio]')]
            .find(el => (el.closest('.merge-opt') || {}).textContent?.indexOf('精确') >= 0);
          if (precise && !precise.disabled) precise.click();
          await new Promise(r => setTimeout(r, 400));
          const btn = [...document.querySelectorAll('button')].find(b => b.textContent === '开始合并');
          if (btn && !btn.disabled) btn.click();
          await new Promise(r => setTimeout(r, 2500));
        })()
        """,
    ),
    (
        # 合并页：点开始合并 → 横幅出现「合并完成」
        "merge-done",
        "#/merge",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
        const pick = async (which) => {
          // 每次点击后左栏会重渲染，旧的 DOM 引用就失效了（点上去毫无反应），
          // 所以每次都要重新查询。
          const sel = which === 'lib'
            ? '.merge-src[data-origin="library"]'
            : '.merge-src[data-origin="clips"], .merge-src:not([data-origin="library"])';
          const el = document.querySelector(sel);
          if (el) el.click();
        };

          await pick('clip');
          await new Promise(r => setTimeout(r, 400));
          await pick('clip');
          await new Promise(r => setTimeout(r, 1000));
          const btn = [...document.querySelectorAll('button')].find(b => b.textContent === '开始合并');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 1400));
        })()
        """,
    ),
    (
        # 样式页：选中圆角预设 + 长句预览 + 真实渲染（mock 给一张 PNG）
        "styles",
        "#/styles",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 900));
          const items = [...document.querySelectorAll('.styles__item')];
          const rounded = items.find(n => n.textContent.indexOf('圆角') >= 0);
          if (rounded) rounded.dispatchEvent(new MouseEvent('click', {bubbles: true}));
          await new Promise(r => setTimeout(r, 500));
          const length = [...document.querySelectorAll('select')]
            .find(s => [...s.options].some(o => o.value === 'long'));
          if (length) {
            length.value = 'long';
            length.dispatchEvent(new Event('change', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 300));
          const render = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '真实渲染');
          if (render) render.click();
          // 真实渲染要等假端点回图；等图出现再截，否则截到"处理中…"
          for (let i = 0; i < 30; i++) {
            if (document.querySelector('.styles__render-img')) break;
            await new Promise(r => setTimeout(r, 150));
          }
        })()
        """,
    ),
    (
        "subtitles-qc",
        "#/subtitles?project=p_demo_1",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          // 行选中挂在 mousedown 上（为了支持 shift 连选），element.click() 只发
          // click 事件，不会触发行选中——这里必须显式派发 mousedown。
          const row = document.querySelectorAll('.sub-row')[1];
          if (row) row.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
          await new Promise(r => setTimeout(r, 200));
        })()
        """,
    ),
    (
        "subtitles-optimize",
        "#/subtitles?project=p_demo_1",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          const btn = [...document.querySelectorAll('.sub-toolbar button')]
            .find(b => b.textContent.trim() === 'AI 优化');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 900));
        })()
        """,
    ),
    (
        "subtitles-translate",
        "#/subtitles?project=p_demo_1",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          const btn = [...document.querySelectorAll('.sub-toolbar button')]
            .find(b => b.textContent.trim() === 'AI 翻译');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 700));
          // 顺手点「开始翻译」：空表单看不出双语版式和逐条译文长什么样
          const go = [...document.querySelectorAll('#sub-trans button')]
            .find(b => b.textContent.trim() === '开始翻译');
          if (go) go.click();
          await new Promise(r => setTimeout(r, 900));
        })()
        """,
    ),
    (
        "subtitles-export",
        "#/subtitles?project=p_demo_1",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          const btn = [...document.querySelectorAll('.sub-toolbar button')]
            .find(b => b.textContent.trim() === '导出');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 600));
          // 勾上软字幕再导出：空表单看不出"软字幕不保留样式"这句提醒和结果行
          const mode = document.querySelector('#exp-mode');
          if (mode) {
            mode.value = 'soft';
            mode.dispatchEvent(new Event('change', { bubbles: true }));
          }
          const go = [...document.querySelectorAll('#sub-export button')]
            .find(b => b.textContent.trim() === '开始导出');
          if (go) go.click();
          await new Promise(r => setTimeout(r, 900));
        })()
        """,
    ),
    ("summary", "#/summary", ""),
    (
        "summary-refine",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          // 换到三轮角色并把打磨要求填上：空表单看不出"版本＋改动说明"长什么样
          const role = document.querySelector('#sum-role');
          if (role) {
            const opt = [...role.options].find(o => /学术/.test(o.textContent));
            if (opt) {
              role.value = opt.value;
              role.dispatchEvent(new Event('change', {bubbles: true}));
            }
          }
          const box = document.querySelector('#sum-requirement');
          if (box) {
            const setter = Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype, 'value').set;
            setter.call(box, '语气再口语一些，短版压到 200 字以内');
            box.dispatchEvent(new Event('input', {bubbles: true}));
          }
          await new Promise(r => setTimeout(r, 300));
        })()
        """,
    ),
    (
        "summary-collection",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 600));
          // 切到合集模式：点第二个 tab（单条视频 / 合集）
          const tabs = document.querySelectorAll('.sum-tab');
          if (tabs.length > 1) tabs[1].click();
          await new Promise(r => setTimeout(r, 900));
        })()
        """,
    ),
    (
        "publish",
        "#/summary",
        """
        (async () => {
          // 空态先看一眼（这一张就是"还没有草稿"的样子）
          await new Promise(r => setTimeout(r, 700));
        })()
        """,
    ),
    (
        "publish-draft",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 600));
          const gen = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '从总结生成');
          if (gen) gen.click();
          await new Promise(r => setTimeout(r, 700));
          const save = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '保存草稿');
          if (save) save.click();
          await new Promise(r => setTimeout(r, 700));
        })()
        """,
    ),
    (
        "publish-gates",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 600));
          const gen = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '从总结生成');
          if (gen) gen.click();
          await new Promise(r => setTimeout(r, 900));
          // 不滚动：门禁卡就在一屏里（--height 1400）。滚了会把右栏草稿列表顶出画面，
          // 截出来左上角一大片空白，看着像页面坏了。
          await new Promise(r => setTimeout(r, 250));
        })()
        """,
    ),
    (
        "publish-records",
        "#/summary",
        """
        (async () => {
          // mock 里已经种了一条已发布、一条已撤回：记录卡一进页面就有内容
          await new Promise(r => setTimeout(r, 800));
        })()
        """,
    ),
    (
        "publish-ready",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 600));
          const gen = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '从总结生成');
          if (gen) gen.click();
          await new Promise(r => setTimeout(r, 900));
          // 先存草稿：#pb-confirm 只在"库里已有这一版"时出现（发布读的是那一行）
          const save0 = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '保存草稿');
          if (save0) save0.click();
          await new Promise(r => setTimeout(r, 900));
          // 勾上「我已阅读并确认」：这一张要拍出门禁三关全绿 + 发布按钮从灰变亮
          const box = document.querySelector('#pb-confirm');
          if (box && !box.checked) {
            box.checked = true;
            box.dispatchEvent(new Event('change', { bubbles: true }));
          }
          await new Promise(r => setTimeout(r, 700));
        })()
        """,
    ),
    (
        "publish-card",
        "#/summary",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 700));
          const gen = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '从总结生成');
          if (gen) gen.click();
          await new Promise(r => setTimeout(r, 800));
          const save = [...document.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '保存草稿');
          if (save) save.click();
          await new Promise(r => setTimeout(r, 800));
          // 发布卡按 1:1 卡序排在正文卡后面，默认视口看不到，滚过去再拍
          const host = document.getElementById('publish-main');
          if (host) host.scrollIntoView({ block: 'start' });
          await new Promise(r => setTimeout(r, 500));
        })()
        """,
    ),
    (
        "settings-glossary",
        "#/settings?section=subtitle",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          const box = document.querySelector('#settings-body textarea');
          if (box) box.scrollIntoView({ block: 'center' });
          await new Promise(r => setTimeout(r, 200));
        })()
        """,
    ),
    (
        "settings-llm",
        "#/settings?section=llm",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 400));
          // 「测试连接」在分区块底部。滚动容器不一定是 #settings-body（可能是 #view），
          // 所以不猜容器，直接把这个按钮滚进视野。
          const probe = [...document.querySelectorAll('#settings-body button')]
            .find(b => b.textContent.trim() === '测试连接');
          if (probe) probe.scrollIntoView({ block: 'center' });
          await new Promise(r => setTimeout(r, 200));
        })()
        """,
    ),
    (
        "settings-providers",
        "#/settings?section=llm",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 450));
          // 服务商卡片在分区最上面。滚动容器不统一（可能是 #view 或 #settings-body），
          // 所以不猜：直接滚到卡片本身。顺便把第二张切成当前，让「当前使用」标记落在
          // 非第一张上——截图要能看出这个状态，不然改了 active 也看不出来。
          const second = document.querySelectorAll('.provider-card')[1];
          if (second) {
            const btn = [...second.querySelectorAll('button')]
              .find(b => b.textContent.trim() === '设为当前');
            if (btn) btn.click();
          }
          await new Promise(r => setTimeout(r, 200));
          const card = document.querySelector('.provider-card');
          if (card) card.scrollIntoView({ block: 'start' });
          await new Promise(r => setTimeout(r, 250));
        })()
        """,
    ),
    (
        "settings-model-picker",
        "#/settings?section=llm",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 450));
          // 弹窗里的候选列表要能看见「已配的预勾上、没配的留空」，这才是这个功能的卖点。
          const card = document.querySelectorAll('.provider-card')[0];
          const btn = card && [...card.querySelectorAll('button')]
            .find(b => b.textContent.trim() === '获取可用模型');
          if (btn) btn.click();
          await new Promise(r => setTimeout(r, 600));
        })()
        """,
    ),
    (
        "settings-llm-adv",
        "#/settings?section=llm",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 450));
          // 折叠区默认收起（刻意的），截图要展开才看得到里面七个任务映射与三档分段
          const adv = document.querySelector('#settings-body details.adv');
          if (adv) {
            adv.open = true;
            adv.scrollIntoView({ block: 'start' });
          }
          await new Promise(r => setTimeout(r, 250));
        })()
        """,
    ),
    (
        "settings-about",
        "#/settings?section=about",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 500));
          // 点一下「导出诊断包」，让这一行有真实反馈（mock 回一个假路径）
          const btn = [...document.querySelectorAll('#settings-body button')]
            .find(b => b.textContent.trim() === '导出诊断包');
          if (btn) {
            btn.click();
            await new Promise(r => setTimeout(r, 400));
          }
        })()
        """,
    ),
    (
        "onboard",
        "#/onboard",
        """
        (async () => {
          await new Promise(r => setTimeout(r, 600));
        })()
        """,
    ),
    ("settings", "#/settings", ""),
]


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    raise SystemExit("没有找到 Chrome / Edge，无法截图")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Cdp:
    def __init__(self, sock) -> None:
        self.ws = sock
        self.mid = 0

    async def cmd(self, method: str, **params):
        self.mid += 1
        ident = self.mid
        await self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    async def js(self, expression: str):
        result = await self.cmd(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        return result.get("result", {}).get("value")


def new_tab(port: int, url: str) -> dict:
    """开一个新的页面 target。

    为什么每张图都新开 tab：同一个文档只换 hash 或只换查询串时，Page.navigate
    在 headless 下不保证重新加载，五张截图会全变成第一张（实测踩过两次）。
    新 target 是唯一可靠的"重新开始"。
    """
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?&=#')}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read())


def close_tab(port: int, target_id: str) -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout=5).read()
    except Exception:
        pass


async def shoot_one(sock_url: str, skin: str, route: str, action: str, target: Path) -> None:
    async with websockets.connect(sock_url, max_size=48 * 1024 * 1024) as sock:
        cdp = Cdp(sock)
        await cdp.cmd("Runtime.enable")
        await cdp.cmd("Page.enable")
        # 界面是异步 boot 的：先等 router 与首个 .page 都出现，再动像素。
        await cdp.js(
            """
            (async () => {
              for (let i = 0; i < 80; i++) {
                if (window.WB && window.WB.router && document.querySelector('.page')) return true;
                await new Promise(r => setTimeout(r, 100));
              }
              return false;
            })()
            """
        )
        # 皮肤只落在 <html data-skin>（app.js 的 applySkin 就这一件事），
        # 而 `#skin=` 会和路由抢 hash，所以直接改属性。
        # 走应用自己的换肤函数：直接改属性会让右上角的皮肤切换器停在旧状态，
        # 截图就会出现「浅色页面 + 高亮的深色按钮」这种自相矛盾的证据。
        await cdp.js(
            f"(window.WB && WB.applySkin) ? WB.applySkin('{skin}', false)"
            f" : document.documentElement.setAttribute('data-skin', '{skin}')"
        )
        if route:
            # 走 router.go 而不是只写 hash：hash 已存在时宿主可能不派发 hashchange，
            # 截图就会停在上一页（真 app 里表现为"点了没反应"）。
            name, _, query = route.lstrip("#/").partition("?")
            params = "{}"
            if query:
                pairs = []
                for pair in query.split("&"):
                    if not pair:
                        continue
                    key, _, value = pair.partition("=")
                    pairs.append(json.dumps(key.strip()) + ": " + json.dumps(value))
                params = "{" + ", ".join(pairs) + "}"
            await cdp.js(f"window.WB.router.go({json.dumps(name)}, {params})")
            await asyncio.sleep(0.8)
        if action:
            await cdp.js(action)
            await asyncio.sleep(0.6)
        await asyncio.sleep(0.4)
        payload = await cdp.cmd("Page.captureScreenshot", format="png")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(payload["data"]))


async def run(
    skin: str, out_dir: Path, width: int, height: int, only: list[str] | None = None
) -> list[Path]:
    chrome = find_chrome()
    port = free_port()
    profile = Path(PROJ / "build" / f"cdp-wb-{port}")
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--window-size={width},{height}",
            "--force-device-scale-factor=1",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("Chrome 没能起来")

        written: list[Path] = []
        for name, route, action in SHOTS:
            if only and name not in only:
                continue
            tab = new_tab(port, INDEX.as_uri() + "?mock=1" + route)
            try:
                await asyncio.sleep(0.5)
                target = out_dir / f"{skin}-{name}.png"
                await shoot_one(tab["webSocketDebuggerUrl"], skin, route, action, target)
                written.append(target)
                print(f"[shot] {target.relative_to(PROJ)}")
            finally:
                close_tab(port, tab.get("id", ""))
        return written
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VideoWorkbench 界面截图")
    parser.add_argument("--skin", default="ark", choices=["ark", "exhibition"])
    parser.add_argument("--out", default="build/shots")
    parser.add_argument("--width", type=int, default=1360)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument(
        "--only",
        default="",
        help="只拍这几张（逗号分隔的 SHOT 名），排查单页时省时间",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJ / out_dir
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    files = asyncio.run(run(args.skin, out_dir, args.width, args.height, only))
    print(f"共 {len(files)} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
