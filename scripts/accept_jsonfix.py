"""真模型验证：坏 JSON 会被修好或重问救回来，而不是整份总结白跑。

用本机 magpie 的 `qoder-cn/qfmodel`（就是线上报 `Expecting ',' delimiter` 那个），
让它按总结的格式输出，然后走 `SummaryLLM.chat(json_mode=True)`：
  - 能修 → 直接返回规范 JSON；
  - 修不了 → 自动重问一次再修；
  - 两次都不行才算失败。

不写用户库、不改配置，只发几次对话请求。
（读配置是必须的：不读就拿不到 shipped 那条路的 `max_tokens`，见 main 里的注释。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_workbench.config import WorkbenchConfig  # noqa: E402
from video_workbench.core.summarizer import SummaryLLM  # noqa: E402
from video_workbench.infra.llm import ChatClient  # noqa: E402

BASE = "http://127.0.0.1:3425/v1"
KEY = "magpie"
MODEL = "qoder-cn/qfmodel"

TRANSCRIPT = """
[00:00] 今天我们聊注意力机制。先说结论：它不是新的东西，是把"该看哪里"变成可计算的权重。
[00:12] 举个反例：如果你只按字面关键词匹配，遇到同义改写就废了。
[01:05] 我们把变量拆开：查询、键、值，各自的作用不一样。
[02:10] 第一个坑：把权重当成概率解释，其实它只是相对重要度。
[03:33] 第二个坑：位置信息不加，顺序就丢了。
"""

PROMPT = (
    "把这段视频字幕整理成结构化总结，只输出一个 JSON 对象，字段："
    "one_line（一句话）、summary_md（正文，多段落用换行分隔）、"
    "key_points（字符串数组）、timeline（数组，每项 {at, text}）。"
)


def main() -> int:
    client = ChatClient(base_url=BASE, api_key=KEY, model=MODEL, timeout=180.0)
    # cfg 必须传进来。不传的时候 `SummaryLLM._parity_max_tokens(None)` 读不到配置，
    # 返回 None，`ChatClient` 就干脆不发 `max_tokens` 这个字段——于是这条脚本验的是
    # 「端点自己的默认预算」，而线上那三条路（总结、打磨、总览）发的一直是
    # `cfg.llm.max_tokens`。两端不同口径，读数就不能当 shipped 行为的证据（task-65）。
    cfg = WorkbenchConfig.load()
    llm = SummaryLLM(client, cfg=cfg)
    print("这次真发出去的 max_tokens =", cfg.llm.max_tokens, "（读的就是 shipped 那条路读的键）")
    notes: list[str] = []
    text = llm.chat(
        [{"role": "user", "content": PROMPT + "\n\n字幕：\n" + TRANSCRIPT}],
        json_mode=True,
        note=notes.append,
    )
    data = json.loads(text)  # 严格解析：这一步过不去就等于没修好
    print("字段:", sorted(data.keys()))
    print("one_line:", str(data.get("one_line"))[:60])
    print("正文行数:", len(str(data.get("summary_md") or "").splitlines()))
    print("重问过:", bool(llm.notes), "| 调用次数:", llm.calls)
    for note in llm.notes:
        print("  备注:", note)
    print("JSON 修复验收：通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
