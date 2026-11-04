"""预读笔记的磁盘缓存：拿素材内容本身寻址，跨任务、跨进程复用。

## 为什么必须有这个东西

预读（长素材分段抽取笔记）是一次总结里**最贵且最可复用**的一步：4 小时视频
7 段预读占 38% 的输入。而它只依赖素材本身——不依赖角色、不依赖修改要求。
也就是说同一份素材迭代多少次、被重开几个任务，它都只需要跑一次。

原先是纯内存字典（`AppState.prepared`），注释里写着「刻意只放内存」。那条决定
在「单个任务内反复打磨」的场景下是对的：进程重启丢了就重新预读，只是贵。

但账本说明了它的漏洞——同一个 bvid 被重开成两个任务时，两次各自付了约 13 万
prompt tokens 的完整预读，而第二遍本可以一分不花。所以这里改成落盘。

## 为什么是文件而不是数据库表

- 单条笔记典型 39 KB、最坏 94 KB（见 `AppState.MAX_PREPARED` 的实测记录）。
  塞进 sqlite 会让主库体积随缓存膨胀，而缓存是**可以整个删掉**的东西——它的
  生命周期不该和你的任务历史绑在一起。
- 删缓存 = 删一个目录，不需要写 SQL，也不怕误删到别的表。
- 原子写用 `os.replace`，并发写同一 key 是无害的（内容由 key 唯一决定）。

## 为什么不加配置项

零损优化：命中就是省下一整段预读，未命中就退化成本来的行为，写缓存失败也只是
下次重跑预读。没有任何一种失败会让产物变差。所以默认开启、不设开关——少一个
配置项就少一处「忘了配」的可能。

要腾空间，删目录即可；设置页面另有一个清空按钮走 `clear()`。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

# 缓存格式版本。改了笔记的生成方式（比如 MAP_SYSTEM 变结构）就要升这个数，
# 让旧条目不命中。提示词本身的变动由 recipe 里的摘要兜住，这里是最后一道保险。
CACHE_VERSION = 2

# 条目数上限。单条典型 39 KB，300 条约 12 MB；真正占空间的是超长视频的笔记，
# 所以同时有字节上限。
MAX_ENTRIES = 300
MAX_BYTES = 192 * 1024 * 1024


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NoteCache:
    """按内容寻址的预读笔记缓存。

    目录结构扁平（`<key>.json`），不分子目录——条目上限只有几百个，
    分片反而让「按 mtime 淘汰」多一层遍历。
    """

    def __init__(self, root: str | os.PathLike, logger=None):
        self.root = Path(root)
        self.log = logger or (lambda message: None)
        self.hits = 0
        self.misses = 0

    # ---------------------------------------------------------------- 寻址

    @staticmethod
    def make_key(body: str, source_label: str, recipe: dict[str, Any], map_prompt: str) -> str:
        """算出这条笔记的地址。

        地址必须覆盖**所有会影响笔记内容的输入**，否则会出现「素材没变、配置变了，
        却拿到旧笔记」这种静默错误：

        - `body` 的摘要：素材内容本身
        - `source_label`：素材类型会写进预读提示词（「素材类型：人工字幕」），
          同一段文字标成弹幕还是字幕，抽出来的笔记不一样
        - `recipe`：分块参数。chunk_size 变了，分段边界就变了，笔记不可比
        - `map_prompt` 的摘要：预读指令变了，产出也不可比

        反过来，凡是不影响笔记的（角色、修改要求、模型名）**刻意不进地址**——
        放进去会让本该命中的场合全部落空。模型名尤其容易被误加：换了模型确实
        会得到不同笔记，但那是「换模型后重新预读」这件事本身该付的钱，不是缓存
        该管的；把它写进 key 只会让复用率归零。
        """
        material = json.dumps(
            {
                "v": CACHE_VERSION,
                "source_label": source_label,
                "recipe": recipe,
                "map_prompt": _digest(map_prompt),
                "body": _digest(body),
                "body_chars": len(body),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return _digest(material)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    # ---------------------------------------------------------------- 读写

    def get(self, key: str) -> dict[str, Any] | None:
        """取一条笔记。任何异常都当成未命中——缓存坏了不该让任务失败。"""
        path = self._path(key)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except FileNotFoundError:
            self.misses += 1
            return None
        except (OSError, ValueError) as exc:
            self.log(f"预读缓存读取失败，按未命中处理：{type(exc).__name__}: {exc}")
            self.misses += 1
            return None

        if not isinstance(entry, dict) or entry.get("v") != CACHE_VERSION:
            self.misses += 1
            return None
        body = entry.get("body")
        chunks = entry.get("chunks")
        if not isinstance(body, str) or not body or not isinstance(chunks, int):
            self.misses += 1
            return None

        # 摸一下 mtime，让「最近用过」的条目排在淘汰序列后面。
        try:
            os.utime(path, None)
        except OSError:
            pass
        self.hits += 1
        return {"body": body, "chunks": chunks}

    def put(
        self,
        key: str,
        body: str,
        chunks: int,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """写一条笔记。失败只记日志，不影响本次任务。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            entry = {
                "v": CACHE_VERSION,
                "body": body,
                "chunks": chunks,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                **(meta or {}),
            }
            tmp = self._path(key).with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False)
            os.replace(tmp, self._path(key))
        except OSError as exc:
            self.log(f"预读缓存写入失败：{type(exc).__name__}: {exc}")
            return
        self._evict()

    # ---------------------------------------------------------------- 淘汰

    def _evict(self) -> None:
        """超限就按最久未用删到限内。

        条目上限只有几百个，所以每次写入后全量扫一遍是可以接受的；换成后台线程
        或增量统计只会带来更多出错的地方。
        """
        try:
            files = list(self.root.glob("*.json"))
        except OSError:
            return
        if len(files) <= MAX_ENTRIES:
            total = sum(f.stat().st_size for f in files if f.exists())
            if total <= MAX_BYTES:
                return
        stats = []
        for path in files:
            try:
                info = path.stat()
            except OSError:
                continue
            stats.append((info.st_mtime, info.st_size, path))
        stats.sort()
        total = sum(item[1] for item in stats)
        while stats and (len(stats) > MAX_ENTRIES or total > MAX_BYTES):
            _mtime, size, path = stats.pop(0)
            try:
                path.unlink()
                total -= size
            except OSError:
                pass

    # ---------------------------------------------------------------- 运维

    def stats(self) -> dict[str, Any]:
        files = list(self.root.glob("*.json")) if self.root.exists() else []
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return {
            "entries": len(files),
            "bytes": total,
            "dir": str(self.root),
            "session_hits": self.hits,
            "session_misses": self.misses,
        }

    def clear(self) -> int:
        """清空缓存，返回删掉的条目数。"""
        removed = 0
        if not self.root.exists():
            return 0
        for path in self.root.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
