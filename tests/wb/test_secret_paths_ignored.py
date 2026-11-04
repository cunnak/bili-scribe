"""守住一条：任何会落 api_key 的文件都必须在 .gitignore 里。

## task-23 搬家之后：这条判据的理由变了，判据本身不变

原来写的是「这个仓库把**共享键**写进 ``config.yaml``（bili-scribe 认识的那几个），
把**全量**配置写进同目录的 sidecar」。搬家（``DEFAULT_HOME`` → ``~/.video-workbench``）
之后，「共享」这个词不再成立：默认那份 config.yaml 是 **WB 自己的文件**，
里面照样有当前服务商的 ``api_key``，所以它照样敏感。

真正的新情况是**位置**：默认家与它的 sidecar 现在都在本仓之外，``.gitignore`` 根本
管不到它们（管得到的是仓库根那一份——测试与 ``--config`` 用的）。于是这条判据要同时
钉住两件事：

- 仓内这几行忽略规则一条都不许删（``config.yaml`` / ``workbench.json`` / ``.bak`` / ``.tmp``
  都还在仓内被写出来，见 ``tests/wb/test_config.py`` 的夹具与 ``--config`` 用法）；
- 默认家必须在仓库之外（``test_the_default_config_home_lives_outside_the_repo``）：
  有人把 ``DEFAULT_HOME`` 挪进工作区，等于把「全量含多家密钥」的 sidecar 摆进
  ``git status`` 的未跟踪列表——那正是 2026-11 那次 api_key 事件的形状。

而 `.gitignore` 当初只挡了 config.yaml / .bak / .tmp（见该文件 3-5 行的注释），
没挡 sidecar。2026-11 接入本机 magpie、第一次跑 `cfg.save()` 时，
`workbench.json` 就这么带着 aihubmix 那把 51 字符的 key 出现在 `git status` 的
未跟踪列表里——下一次 `git add -A` 就进公开仓库。这是 api_key 事件的同一个坑。

判据从常量真源推，不写死文件名：`EXT_NAME` 改了，这里跟着改；
从 `.gitignore` 里删掉一行，这里必红（我实测过，见交付说明的牙齿证明）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench import config as wb_config  # noqa: E402

GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
IGNORED = {line.strip() for line in GITIGNORE.splitlines()
           if line.strip() and not line.strip().startswith("#")}


def _secret_bearing_names() -> list[str]:
    """所有可能写着 api_key 的配置文件名，取自代码里的常量，不是抄的。"""
    names = [
        "config.yaml",       # bili-scribe 读的共享键（含当前服务商的 key）
        "config.yml",        # 同名兼容写法
        wb_config.EXT_NAME,                 # workbench.json：全量快照，含每家的 key
        wb_config.LEGACY_LLM_EXT_NAME,      # 旧 sidecar，仍可能被读到/重写
    ]
    # save() 用 .tmp 写完再 os.replace，并留 .bak —— 这两个瞬时文件同样含明文 key。
    for base in list(names):
        names.append(base + ".bak")
        names.append(base + ".tmp")
    return names


def test_every_secret_bearing_file_is_gitignored() -> None:
    missing = [n for n in _secret_bearing_names() if n not in IGNORED]
    assert not missing, (
        "这些文件会写明文 api_key，但 .gitignore 没挡：" + ", ".join(missing)
        + "。补进 .gitignore，别靠 git add 时手动挑文件。"
    )


def test_sidecar_is_strictly_more_sensitive_than_config_yaml() -> None:
    """把「sidecar 装的是全部服务商的 key」这个事实钉住，防止有人日后精简成不含 providers。

    精简本身可以，但那时这条会红，逼对方看见：忽略规则的理由还成不成立。
    """
    import dataclasses

    llm = wb_config.LlmConfig()
    field_names = {f.name for f in dataclasses.fields(llm)}
    assert {"providers", "api_key"} <= field_names, (
        "LlmConfig 的形状变了：sidecar 现在还会不会写多家密钥？"
        "确认后同步本文件的判据与 .gitignore 注释"
    )


def test_legacy_sidecar_name_is_still_read_only_compatible() -> None:
    """旧 sidecar 文件名不许漂走：漂了就等于有一条含密钥的历史文件不再被忽略。"""
    assert wb_config.LEGACY_LLM_EXT_NAME.endswith(".json")
    assert wb_config.LEGACY_LLM_EXT_NAME in IGNORED


def test_the_default_config_home_lives_outside_the_repo() -> None:
    """默认配置家必须在仓库之外——否则含多家密钥的 sidecar 会直接进 `git status`。

    搬家把 ``DEFAULT_HOME`` 从 ``~/.bili-scribe`` 换成 ``~/.video-workbench``，两边都在仓外，
    ``.gitignore`` 因此**管不到**默认那份文件（它本来也不该被提交）。
    这条盯的是反向的坏改法：有人为了省事把默认家指进工作区（``./config`` 之类），
    那 ``workbench.json`` 里每一家服务商的明文 ``api_key`` 就落在版本控制的眼皮底下——
    与本文件开头那次 api_key 事件同一个坑，只是换成人把目录挪进仓库。
    """
    home = Path(str(wb_config.DEFAULT_HOME))
    assert not home.is_relative_to(ROOT), (
        f"默认配置家被挪进仓库了：{home} 在 {ROOT} 下面；"
        "含 api_key 的 workbench.json 会出现在 git status 里"
    )
    legacy = Path(str(wb_config.LEGACY_HOME))
    assert not legacy.is_relative_to(ROOT), f"旧家（迁移来源）也不该在仓内：{legacy}"
