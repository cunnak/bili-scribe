"""真实接入本机 magpie 的端到端验收：走服务自身的 RPC，不绕过任何一层。

用临时配置（库也临时），不碰用户的 config.yaml 与 bili-scribe.db。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from video_workbench.app.service import WorkbenchService  # noqa: E402

FAILS: list[str] = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    print(("[ok  ] " if cond else "[FAIL] ") + name + ("  " + extra if extra else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wb_magpie_"))
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "app:",
                "  language: zh-CN",
                "  media_server: auto",
                "ui:",
                "  skin: ark",
                "  rail_collapsed: false",
                "llm:",
                "  base_url: http://127.0.0.1:3425/v1",
                "  api_key: magpie",
                "  model: auto/hy4-preview",
                "  temperature: 0.3",
                "  max_tokens: 4096",
                "  max_input_chars: 24000",
                "  chunk_size: 6000",
                "  chunk_overlap: 400",
                "  thinking: auto",
                "  timeout_seconds: 120",
                "  proxy: ''",
                "  role: standard",
                "  providers: []",
                "  active_provider: ''",
                "  tasks:",
                "    summary: ''",
                "    collection_overview: ''",
                "    translate: ''",
                "    clip_analyze: ''",
                "    copy: ''",
                "    refine: ''",
                "    vision: ''",
                "storage:",
                "  db_path: " + json.dumps(str(tmp / "wb.db")),
                "  media_root: " + json.dumps(str(tmp / "media")),
                "  cache_root: " + json.dumps(str(tmp / "cache")),
                "  models_root: " + json.dumps(str(tmp / "models")),
                "",
            ]
        ),
        encoding="utf-8",
    )

    service = WorkbenchService(config_path=cfg)
    try:
        # 1) 配置归一：扁平字段应被镜像成一张卡
        provs = service.cfg.llm.providers
        ok("配置归一后有一张服务商卡", len(provs) == 1, "实际 %d" % len(provs))
        card = provs[0] if provs else None
        ok("卡的地址是本机 magpie", bool(card) and "127.0.0.1:3425" in card.base_url,
           getattr(card, "base_url", ""))
        ok("本机地址的 id 是 local", bool(card) and card.id == "local", getattr(card, "id", ""))

        # 2) llm.models：真打网关
        card_id = getattr(card, "id", "") or ""
        r = service.dispatch("llm.models", {"provider": card_id})
        ok("llm.models 成功", r.get("ok") is True, json.dumps(r.get("error", {}), ensure_ascii=False)[:200])
        models = (r.get("data") or {}).get("models") or []
        ok("拿到 magpie 的模型清单", len(models) >= 20, "实际 %d 个" % len(models))
        ids = [m.get("id") for m in models]
        ok("清单里有 auto/ 前缀的聚合模型", any(str(i).startswith("auto/") for i in ids),
           str(ids[:3]))

        # 3) llm.test：真发一次对话
        t = service.dispatch("llm.test", {"provider": card_id, "model": "auto/hy4-preview"})
        ok("llm.test 成功（推理模型也没被判成坏的）", t.get("ok") is True,
           json.dumps(t.get("error", {}), ensure_ascii=False)[:240])
        if t.get("ok"):
            data = t.get("data") or {}
            ok("测试连接回了耗时", isinstance(data.get("latency_ms"), int), str(data.get("latency_ms")))
        print("[detail] llm.test =", json.dumps(t, ensure_ascii=False)[:300])
    finally:
        service.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print("失败 %d 项：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("magpie 接入验收：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
