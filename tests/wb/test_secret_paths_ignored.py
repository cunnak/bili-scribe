"""守住一条：任何会落 api_key 的文件都必须**真的被 git 忽略**，且没被跟踪。

## 2026-11-05 判据换轴：从「`.gitignore` 里有没有这一行」改成「git 会不会忽略它」

以前这条比的是字面行集合（`name in IGNORED`）。lead 为了给 `config.yaml.bak-32768`
这类带后缀的备份兜底，把 `.gitignore` 里的 `config.yaml.bak` 改成 `config.yaml.bak*`
——**忽略行为一点没变**（`git check-ignore` 前后都说它被忽略），那条锁却红了。
按 prove-it 的判据自审：把功能弄坏它才该红；只换一种合法写法它就红，是
implementation-coupled（死抠实现细节）。⇒ 判据改成跑 `git check-ignore --no-index`，
rc==0 才算被忽略；并把「已被跟踪」这条单独钉住（`.gitignore` 拦不住跟踪中的文件，
那才是真漏路径）。

## 换成行为判据之后，本文件现在报的是什么（交付时它是红的，而且是该红）

`config.yaml` / `config.yml` 一族已经 glob 化，带后缀变体都兜得住；但 sidecar 那四行
（`workbench.json.bak`、`workbench.json.tmp`、`workbench.llm.json.bak`、
`workbench.llm.json.tmp`）**仍是精确名**，于是
`workbench.json.bak-32768` 这类名字今天**不被忽略**——而 sidecar 装的是**每一家**服务商的
明文 key，比 `config.yaml` 只有一把当前 key 更敏感。

⇒ 这条红是**真缺陷**，不是判据太严，也不是牙齿。修法在 `.gitignore` 那四行加 `*`
（lead 名下，产品/配置面），不在这里放宽：把候选名单缩回精确名、或给这条挂
`xfail`，都是把一条密钥泄漏锁改成摆设。**它变绿的那天，就是泄漏路径被堵上的那天。**

## 候选名单为什么必须含带后缀的形状

以前只有 `base + ".bak"` / `base + ".tmp"` 两个精确名，所以 lead 这次的
`config.yaml.bak-32768` 压根不在射程里——**这才是他能漏过去的真正原因**，
不是 `.gitignore` 少写一个星号。名单从代码常量真源推（`EXT_NAME` 改了这里跟着改），
后缀形状写在 `BACKUP_SUFFIXES`：空串＝本体，`.bak`/`.tmp` 是 `save()` 自己留的，
带 `-32768` 与时间戳的是人手留的（这台机器上真出现过）。

## 位置相关的两条事实（task-23 搬家之后）

- 默认配置家与它的 sidecar 在本仓之外，`.gitignore` **管不到**它们；管得到的是仓库根
  那一份（测试夹具与 `--config` 用的）。所以「默认家在仓外」单独钉住
  （`test_the_default_config_home_lives_outside_the_repo`）。

历史一句：`.gitignore` 当初只挡了 config.yaml / .bak / .tmp，没挡 sidecar。
2026-11 接入本机 magpie、第一次跑 `cfg.save()` 时，`workbench.json` 就这么带着
aihubmix 那把 51 字符的 key 出现在 `git status` 的未跟踪列表里——下一次
`git add -A` 就进公开仓库。

删掉一条**真正在起作用**的规则，这里必红；删掉一条被别的规则覆盖的冗余行不再红——
那是故意的，冗余行删掉不构成密钥泄漏路径。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench import config as wb_config  # noqa: E402

#: 判据跑哪个仓库。**只换「读哪份忽略规则」，不换判据语义**
#: （照 `WB_WEB_ROOT` / `WB_CLI_SOURCE_OVERRIDE` 的先例）——牙齿用它把 `.gitignore`
#: 的坏写法放进 `%TEMP%` 的哨兵仓库里验，工作区零写入。
IGNORE_REPO = Path(os.environ.get("WB_IGNORE_REPO", str(ROOT))).resolve()

#: 反面对照：这些是仓库里人人都该提交的文件，**不许**被判成「已忽略」。
#: 留着它是因为 `git check-ignore` 一旦用错（跑错目录、rc 判断反了、参数写错），
#: 表现就是「所有文件都被忽略」⇒ 上面两条全绿而一句话都没守住。只有这条会红。
NOT_SECRET = ("README.md", "pytest.ini", "video_workbench/config.py")

#: 备份/临时文件的后缀形状。空串＝本体。带 `-32768` 与时间戳的那两种是这台机器上
#: 真出现过的写法（`cp -p` 手留 `-32768`；人工留时间戳备份）。
BACKUP_SUFFIXES = (
    "",
    ".bak",
    ".tmp",
    ".bak-32768",
    ".tmp-20261105-113000",
    ".bak-20261105-113000",
)


def _secret_bearing_bases() -> list[str]:
    """所有可能写着 api_key 的**本体**文件名，取自代码里的常量，不是抄的。"""
    return [
        "config.yaml",  # 含当前服务商的 key
        "config.yml",  # 同名兼容写法
        wb_config.EXT_NAME,  # workbench.json：全量快照，每家的 key 都在
        wb_config.LEGACY_LLM_EXT_NAME,  # 旧 sidecar，仍可能被读到/重写
    ]


def _secret_bearing_names() -> list[str]:
    """每个本体配上 `BACKUP_SUFFIXES` 里的每一种形状。

    `save()` 用 `.tmp` 写完再 `os.replace`，并留 `.bak`；人还会手工留带后缀的备份。
    这些瞬时文件同样含明文 key——2026-11-05 漏过去的就是这一类。
    """
    return [base + suffix for base in _secret_bearing_bases() for suffix in BACKUP_SUFFIXES]


def _ignored_by_git(name: str) -> bool:
    """git 自己怎么说：rc==0 ＝ 这个路径被忽略。比对字面行不算判据。"""
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", name],
        cwd=str(IGNORE_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode not in (0, 1):
        raise AssertionError(
            f"`git check-ignore` 对 {name!r} 返回 rc={proc.returncode}（只该是 0 或 1）："
            f"{(proc.stderr or '').strip()}——跑错目录或 git 不可用，"
            "这种读数既不能当成「忽略」也不能当成「没忽略」"
        )
    return proc.returncode == 0


def _tracked_by_git(name: str) -> bool:
    """已被跟踪的文件 `.gitignore` 拦不住，那才是真漏路径。"""
    proc = subprocess.run(
        ["git", "ls-files", "--", name],
        cwd=str(IGNORE_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"`git ls-files` 对 {name!r} 失败 rc={proc.returncode}：{(proc.stderr or '').strip()}"
        )
    return bool(proc.stdout.strip())


def test_a_secret_bearing_candidate_list_is_not_vacuous() -> None:
    """形状锁：候选名单必须真含「本体 + 各种后缀」，缩水就等于把锁拆了。

    2026-11-05 之前这里只有精确名 `.bak`/`.tmp`，带后缀的备份根本不在射程里，
    所以 `config.yaml.bak-32768` 漏过去没人喊。这条就是拦「把名单缩回去」。
    """
    names = _secret_bearing_names()
    bases = _secret_bearing_bases()
    assert len(names) == len(bases) * len(BACKUP_SUFFIXES), (
        f"候选名单 {len(names)} 条 ≠ 本体 {len(bases)} × 后缀 {len(BACKUP_SUFFIXES)} "
        "⇒ 有本体或某种后缀被摘掉"
    )
    for base in bases:
        assert base in names, f"本体 {base} 不在名单里"
        assert f"{base}-32768" not in base, "后缀形状别写成含本体名的怪东西"
        for suffix in BACKUP_SUFFIXES:
            if suffix:
                assert base + suffix in names, f"{base}{suffix} 漏出名单"
    assert wb_config.EXT_NAME in names, "sidecar 本体必须在名单里"


def test_every_secret_bearing_file_is_gitignored() -> None:
    """行为判据：git 说没忽略的，一个都不许有。名字沿用原样，改的是内部。

    以前比字面行集合，lead 把 `config.yaml.bak` 改成合法 glob `config.yaml.bak*`
    就把它误红了（忽略行为没变）⇒ 那条红是 implementation-coupled，不是缺陷。
    现在只问 git 会不会真的忽略这个路径。

    反过来也得说清：判据不看字面行，**不等于**放过规则退化。名单里带了后缀的候选
    （`.bak-32768`、`.tmp-20261105-113000`、`.bak-20261105-113000`）就是退化的探针——
    谁把 `.gitignore` 那八行 glob 改回精确名，忽略行为当场变窄，本用例必红，missing
    列出的正是 config.yml / config.yaml / sidecar 三族的带后缀名（2026-11-05 自证：
    星号全改回精确名 → 本用例红，missing = `config.yaml.bak-32768, config.yaml.tmp-…,
    config.yml.bak-32768, …`；逐字照抄现行规则 → 本用例绿）。所以这里**不再**单开一条
    常驻「钉 glob 还在」的断言：那会把「行为对不对」重新写成「文本长什么样」。
    """
    not_ignored = [n for n in _secret_bearing_names() if not _ignored_by_git(n)]
    assert not not_ignored, (
        f"这些文件会写明文 api_key，但 git 说没忽略（跑在 {IGNORE_REPO}）："
        + ", ".join(not_ignored)
        + "。补进 .gitignore（用 `*` 覆盖带后缀的备份），别靠 git add 时手动挑文件。"
        + " 也别把这条改回比字面行——那正是今天被合法 glob 写法误红的老判据。"
    )


def test_no_secret_bearing_file_is_tracked() -> None:
    """「写进 .gitignore」不等于「安全」：已跟踪的文件忽略规则拦不住。"""
    tracked = [n for n in _secret_bearing_names() if _tracked_by_git(n)]
    assert not tracked, (
        "这些含明文 key 的文件已经在版本库里："
        + ", ".join(tracked)
        + "。`git rm --cached` 之后还要清历史，别只加 .gitignore。"
    )


def test_the_ignore_check_actually_discriminates() -> None:
    """反面对照：`git check-ignore` 用错时它会「全都忽略」，这条专治那个假绿。"""
    wrongly_ignored = [n for n in NOT_SECRET if _ignored_by_git(n)]
    assert not wrongly_ignored, (
        "这些人人都该提交的文件被判成「已忽略」："
        + ", ".join(wrongly_ignored)
        + f"（跑在 {IGNORE_REPO}）⇒ 忽略判据本身不可信，上面几条的红/绿都别信"
    )
    assert NOT_SECRET and _secret_bearing_names(), "对照名单或候选名单空了＝这条是摆设"


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
    assert _ignored_by_git(wb_config.LEGACY_LLM_EXT_NAME), (
        f"{wb_config.LEGACY_LLM_EXT_NAME} 不再被 git 忽略——旧 sidecar 里也是多家 key"
    )


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
