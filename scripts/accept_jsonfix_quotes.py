"""真模型 + 真引号：模型爱在正文里直接引用别人的话，结构就断在第二个引号。

用 qoder-cn/qfmodel（线上报错那个），让它在 JSON 字符串里引用两句英文双引号包裹的话，
验证：能修 → 直接过；修不了 → 重问一次再修；两次都不行才报错。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from video_workbench.config import WorkbenchConfig  # noqa: E402
from video_workbench.core.summarizer import SummaryLLM  # noqa: E402
from video_workbench.infra.llm import ChatClient  # noqa: E402

PROMPT = (
    "把下面这段整理成 JSON：one_line、summary_md、key_points。"
    "要求 summary_md 里**原样引用**字幕中带引号的两句话（照抄引号）。"
)
TRANSCRIPT = '''
[00:03] 他说"这条链路不能动"，然后又说"动了就等着回滚"。
[00:20] 我的判断是：这不是技术问题，是排期问题。
[00:41] 后来他改口说"可以动，但要灰度"，这就是我说的转折。
'''


def main() -> int:
    client = ChatClient(base_url="http://127.0.0.1:3425/v1", api_key="magpie",
                        model="qoder-cn/qfmodel", timeout=180.0)
    # 同 accept_jsonfix.py：cfg 不传就不发 `max_tokens`，这条脚本验的就不是 shipped
    # 那条路的口径（task-65）。这里只取配置里的预算，端点与模型仍按上面写死的走。
    cfg = WorkbenchConfig.load()
    llm = SummaryLLM(client, cfg=cfg)
    print("这次真发出去的 max_tokens =", cfg.llm.max_tokens)
    text = llm.chat([{"role": "user", "content": PROMPT + "\n" + TRANSCRIPT}],
                    json_mode=True, note=lambda _m: None)
    data = json.loads(text)
    print("字段:", sorted(data))
    print("正文片段:", str(data.get("summary_md"))[:80].replace("\n", " "))
    print("调用次数:", llm.calls, "| 备注:", llm.notes)
    print("引号场景验收：通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
