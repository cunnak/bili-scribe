r"""WB 配置搬家（task-23）：迁移忠实性、新家优先、旧家零写入、以及「库仍共用」的边界锁。

## 这次改动的一句话本质

WB 的默认配置家从「与 bili-scribe 共用的 ``~/.bili-scribe/config.yaml``」搬到 WB 自己的
``~/.video-workbench/config.yaml``；旧家降级成**只读迁移来源**。要守的不变量只有一条：
**任何一次 WB 的运行或保存，都不许让 bili-scribe 的行为变。**

本文件就是把这句话拆成能红的断言。判据方向来自 ``team_task_get("task-22")``/``("task-23")``，
实现来自**现读** ``video_workbench/config.py``（本文件落盘时产品码已经搬家，锚点如下）：

- ``DEFAULT_HOME = Path.home()/".video-workbench"``、``DEFAULT_CONFIG_PATH = DEFAULT_HOME/"config.yaml"``；
- ``LEGACY_HOME = Path.home()/".bili-scribe"``、``LEGACY_CONFIG_PATH = LEGACY_HOME/"config.yaml"``，
  注释写明它只是迁移来源，「旧家目录下的字节从今往后一个都不许被写（连 ``.bak`` 都不行）」；
- ``load()``（现读 :719-780 那段）：``path is None and not target.exists() and
  (LEGACY_CONFIG_PATH.exists() or ext_path(LEGACY_CONFIG_PATH).exists())`` 才把 ``origin``
  换成旧家并记 ``migrated_from``；``source_path`` 始终是新家；``sync_providers()`` 之后
  ``if migrated_from is not None: cfg.save()``（**只写新家**，并且**不走** ``elif foreign`` 的
  重写分支），否则 ``elif foreign: cfg.save(target)``；
- ``StorageConfig`` 刻意**不重新声明** ``db_path``/``key_path``（:491-495 那段注释就是本文件
  判据 B 的产品侧对应物）；
- ``save()``：``target = 显式路径 or self.source_path``，无 source_path 抛 VALIDATION；
  目标已存在则先 ``copy2`` 出 ``.bak``，再 ``.tmp`` + ``os.replace``，然后 ``write_ext(target)``。

## 与产品注释的一处不一致（现读发现，报 lead，不改产品码）

``load()`` 里那句注释写「也不走下面的 foreign 重写：``render_yaml`` 只渲染 bili-scribe 认识的键，
写出来的新家文件天生没有脏键」——**这只对 yaml 成立，对旧家的 ``foreign`` 键是「搬过去」而不是
「清掉」**：旧 yaml 里的 ``llm.tasks`` / ``ui.onboarded`` 会经 ``_merge_raw`` 进 ``cfg``，再由
``write_ext`` 全量写进新家 sidecar（值不丢，对的），而旧家那份**仍然带着脏键**，bili-scribe 照旧
读不动。所以 ``test_the_old_home_is_no_longer_cleaned_up_for_them`` 钉的就是这个后果：搬家之后
**没人再替旧用户清理那份文件**。这不是我假设的坏行为，是「不许写对方文件」这条的直接推论，
要不要给用户一句提示属于产品决定，已报 lead。

## 分工（别和既有文件重复）

- ``tests/wb/test_config_shared.py``：一份**指定路径**的 config.yaml + sidecar 的形状契约
  （yaml 只含 bili 认识的键、扩展进 sidecar、形状迁移、坏文件兜底）。
- 本文件：**默认家在哪、迁移读谁写谁、旧家零写入、库仍共用**。两条边界不重叠。
- ``tests/wb/test_secret_paths_ignored.py``：会落 api_key 的文件名必须被 ``.gitignore`` 挡住。

## 隔离（这条是本文件最容易出事的地方）

所有用例都把 ``DEFAULT_HOME`` / ``DEFAULT_CONFIG_PATH`` / ``LEGACY_HOME`` /
``LEGACY_CONFIG_PATH`` 重定向到 ``tmp_path``，再把 ``HOME``/``USERPROFILE`` 指到同一个根。
fixture 结束时**对比真实家目录的指纹**，被写过就直接红——搬家这件事本身不该被测试
写到用户的真配置。上一轮有个牙齿脚本 ``rmtree(d.parent)`` 把整个 %TEMP% 删了，
所以本文件的清理一律只动自己建的根目录，不碰任何 parent。

## 牙齿（写在文件末尾，五条）

牙齿**不动任何产品文件**，而是运行时把产品函数换成「坏掉的那版」，再断言对应的判据必须炸。
比改文件更安全，也更对症：这五条坏法都是代码层的，不是配置层的。
（上一轮有牙齿脚本 ``rmtree(d.parent)`` 把整个 %TEMP% 删了，所以本文件一律不建 Temp 目录副本、
不删任何 parent——要模拟坏产品行为，换函数就够了。）
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_workbench import config as wb  # noqa: E402
from video_workbench.config import WorkbenchConfig  # noqa: E402

#: 真实家目录：**import 期**取一次。夹具会把 ``HOME``/``USERPROFILE`` 指到 Temp（这样
#: 任何 ``expanduser("~")`` 都落在 Temp 里），所以判据里再叫 ``Path.home()`` 拿到的
#: 就是 Temp —— 拿它去比「默认家是不是 WB 自己的目录」等于自己考自己。
REAL_HOME = Path.home()

#: bili-scribe 自己的家：从**它的**模块常量取，不在这里抄字符串。
#: 本文件的夹具会把 ``wb.LEGACY_HOME`` 指到 Temp（这样才能测搬家），所以凡是要说
#: 「数据文件仍然与对方共用同一个真实目录」的判据，必须拿这个未被改写的常量当参照。
BILI_HOME = __import__("bili_scribe.config", fromlist=["AppConfig"]).DEFAULT_HOME

#: 产品模块**被导入那一刻**的家目录常量快照。判据里凡是问「默认家到底搬没搬」的，
#: 都用这份快照而不是当下的 ``wb.*``——后者在夹具里是 Temp，拿它比就是自己考自己。
MODULE_DEFAULT_HOME = wb.DEFAULT_HOME
MODULE_DEFAULT_CONFIG_PATH = wb.DEFAULT_CONFIG_PATH
MODULE_LEGACY_HOME = wb.LEGACY_HOME
MODULE_LEGACY_CONFIG_PATH = wb.LEGACY_CONFIG_PATH

#: bili-scribe 认识的段。搬家后这份限制仍然成立（理由换成「形状由它说了算 + 不主动
#: 变成读不动的形状」，见 config.py 搬家后那段注释），所以本文件继续拿它当判据。
BILI_SECTIONS = {"server", "llm", "collect", "publish", "storage", "ui"}

#: 旧家那份文件里的值。每一个都取「有辨识度」的字面量：断言里出现的就是这些串，
#: 迁移把它们弄丢或改动一个，用例就会指出是哪个字段。
OLD_LLM = {
    "base_url": "https://legacy.example/v1",
    "api_key": "sk-LEGACY-A1",
    "model": "legacy-model-x",
    "temperature": 0.11,
    "max_tokens": 4096,
    "role": "standard",
    "proxy": "http://127.0.0.1:7897",
    "timeout_seconds": 33,
}
OLD_PROVIDER_IDS = ["prova", "provb"]
OLD_ACTIVE_PROVIDER = "provb"
OLD_TASKS_SUMMARY = "legacy-task-model"
OLD_DOWNLOAD_QUALITY = "720p"
OLD_UI_RAIL = True
NEW_HOME_DIRNAME = ".video-workbench"


# ------------------------------------------------------------------ 隔离夹具


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "MISSING"


def _tree_fingerprint(root: Path) -> dict[str, str]:
    """目录树指纹：相对路径 → sha256。不存在返回空表。

    搬家最坏的失败模式不是「读到旧值」，而是**写了对方目录**——连 ``.bak`` 都算。
    只比两个已知文件不够：多出 ``config.yaml.bak``、``config.yaml.tmp`` 这种副作用
    必须也能被看见，所以这里递归列全部条目。
    """
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): _sha(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _real_home_snapshot() -> dict[str, str]:
    """真实家目录里这次改动涉及的两处，用来在 fixture 结束时核对「没写脏」。"""
    out: dict[str, str] = {}
    for home in (Path.home() / NEW_HOME_DIRNAME, Path.home() / ".bili-scribe"):
        out[f"<dir>:{home}"] = "EXISTS" if home.is_dir() else "ABSENT"
        for name in ("config.yaml", "workbench.json"):
            out[str(home / name)] = _sha(home / name)
    return out


def _point_at(module, name: str, value: Path, monkeypatch) -> None:
    """把模块级常量指到 Temp。常量不在就红着说明缺哪一个，不许静默跳过。

    用 ``getattr`` 探测而不是 ``import``：vendor 还没落地某个常量时，报错要指名道姓，
    而不是以 ``AttributeError`` 的形式看起来像测试自己写错了。
    """
    if not hasattr(module, name):
        pytest.fail(
            f"产品里没有常量 {module.__name__}.{name}——本文件的隔离夹具依赖它。"
            "（在等 task-22：新家/旧家两个常量都得在，否则「默认家在哪」这条没法测。）"
        )
    monkeypatch.setattr(module, name, value)


@pytest.fixture
def homes(monkeypatch, tmp_path):
    """新旧两家都在 Temp 里的夹具；退出时证明真实家目录一个字节没被写。"""
    base = tmp_path / "homes"
    legacy = base / "bili-scribe"
    new = base / NEW_HOME_DIRNAME.lstrip(".")
    legacy.mkdir(parents=True)
    new.mkdir(parents=True)

    _point_at(wb, "DEFAULT_HOME", new, monkeypatch)
    _point_at(wb, "DEFAULT_CONFIG_PATH", new / "config.yaml", monkeypatch)
    _point_at(wb, "LEGACY_HOME", legacy, monkeypatch)
    _point_at(wb, "LEGACY_CONFIG_PATH", legacy / "config.yaml", monkeypatch)

    # 兜底：万一将来某个路径是运行时 expanduser("~") 算出来的，也留在 Temp 里。
    monkeypatch.setenv("HOME", str(base))
    monkeypatch.setenv("USERPROFILE", str(base))
    # 密钥的环境变量覆盖会吃掉文件里那把，本机 shell 里有值就让判据失真。
    monkeypatch.delenv(wb.ENV_API_KEY, raising=False)

    before = _real_home_snapshot()
    yield {
        "base": base,
        "legacy": legacy,
        "new": new,
        "legacy_config": legacy / "config.yaml",
        "legacy_ext": legacy / "workbench.json",
        "new_config": new / "config.yaml",
        "new_ext": new / "workbench.json",
    }
    after = _real_home_snapshot()
    assert after == before, (
        "搬家用例写到真实家目录了：\n"
        + "\n".join(f"  {k}: {before[k]} → {after[k]}" for k in before if before[k] != after[k])
    )


def _write_legacy_home(homes, *, foreign: bool = False) -> None:
    """造一份「有辨识度」的旧家：``config.yaml`` + ``workbench.json``。

    ``foreign=True`` 时把 WB 的扩展键留在 yaml 里（旧版本就是这么写的），用来测
    「形状迁移只准写新家」。默认 False —— 那是搬家后最常见的现场。
    """
    legacy_cfg: Path = homes["legacy_config"]
    llm_lines = [f"  {k}: {json.dumps(v) if isinstance(v, str) else v}" for k, v in OLD_LLM.items()]
    lines = ["llm:"] + llm_lines
    if foreign:
        lines += [
            "  tasks:",
            f"    summary: {json.dumps(OLD_TASKS_SUMMARY)}",
            "ui:",
            "  skin: ark",
            f"  rail_collapsed: {str(OLD_UI_RAIL).lower()}",
            "download:",
            f"  quality: {json.dumps(OLD_DOWNLOAD_QUALITY)}",
        ]
    lines += [
        "storage:",
        f"  db_path: {json.dumps((homes['legacy'] / 'shared.db').as_posix())}",
        f"  key_path: {json.dumps((homes['legacy'] / 'key.bin').as_posix())}",
        "",
    ]
    legacy_cfg.write_text("\n".join(lines), encoding="utf-8")

    ext = {
        "llm": {
            "providers": [
                {
                    "id": OLD_PROVIDER_IDS[0],
                    "name": "Provider A",
                    "protocol": "openai",
                    "base_url": "https://a.example/v1",
                    "api_key": "sk-A",
                    "models": ["a-model"],
                },
                {
                    "id": OLD_PROVIDER_IDS[1],
                    "name": "Provider B",
                    "protocol": "openai",
                    "base_url": "https://b.example/v1",
                    "api_key": "sk-B",
                    "models": ["b-model"],
                },
            ],
            "active_provider": OLD_ACTIVE_PROVIDER,
            "tasks": {"summary": OLD_TASKS_SUMMARY},
        },
        "download": {"quality": OLD_DOWNLOAD_QUALITY},
        "ui": {"rail_collapsed": OLD_UI_RAIL, "onboarded": True},
    }
    if not foreign:
        # 正常现场：共享键在 yaml、扩展键在 sidecar。
        ext["ui"] = {"rail_collapsed": OLD_UI_RAIL, "onboarded": True}
    homes["legacy_ext"].write_text(json.dumps(ext, ensure_ascii=False), encoding="utf-8")


def _bili_visible_subset(cfg) -> dict:
    """投影成 bili-scribe 的形状再转 dict——搬家不许改变对方能看到的东西。"""
    return dataclasses.asdict(wb.to_bili_config(cfg))


def _comparable(cfg) -> dict:
    """整份配置去掉「文件在哪」这件事，其余逐字段可比。"""
    data = dataclasses.asdict(cfg)
    data.pop("source_path", None)
    return data


# ---------------------------------------------------------------- 判据 A1：迁移忠实性


def test_migration_moves_every_value_and_writes_nothing_in_the_old_home(homes):
    """A1 新家空、旧家有东西 → load() 必须逐字段搬到新家，且旧家字节一个都不许多。

    守的是那句不变量的正面：搬家不许改值（``role=standard``/``max_tokens=4096``
    我知道是坏的，但迁移必须可预测，修配置是另一件事）。
    """
    _write_legacy_home(homes)
    legacy_before = _tree_fingerprint(homes["legacy"])

    cfg = WorkbenchConfig.load()

    # 参照系：把旧家整份复制一份再用显式路径读——不能用「直接 load 旧家那份」，
    # 那会让产品按形状迁移那条去写旧家（foreign 分支），把参照系本身弄脏。
    mirror = homes["base"] / "mirror"
    mirror.mkdir()
    for name in ("config.yaml", "workbench.json"):
        (mirror / name).write_bytes((homes["legacy"] / name).read_bytes())
    reference = WorkbenchConfig.load(mirror / "config.yaml")

    assert _comparable(cfg) == _comparable(reference), (
        "迁移搬到的值 ≠ 直接读旧家那份的值：\n"
        + "\n".join(
            f"  {k}: 迁移={a!r} 直读={b!r}"
            for k, (a, b) in sorted(_diff(_comparable(cfg), _comparable(reference)).items())
        )
    )

    assert cfg.source_path is not None
    assert Path(cfg.source_path) == homes["new_config"], (
        f"迁移后 source_path 必须指新家，实得 {cfg.source_path!r}（在等 task-22 的迁移分支）"
    )
    assert homes["new_config"].exists(), "新家没出现 config.yaml —— 迁移没落盘"
    assert homes["new_ext"].exists(), "新家没出现 workbench.json —— 服务商/tasks 这类扩展配置会丢"

    assert _tree_fingerprint(homes["legacy"]) == legacy_before, (
        "旧家被动过：搬家只许读它"
    )


def test_migration_carries_the_broken_budget_and_role_verbatim(homes):
    """搬家不许「顺手修好」值 —— 用户 2026-11-04 拍的 C：搬家什么都不改。

    A1 钉的是通用形（搬到的 == 直读的）。这条钉**具体字面量**，因为诱惑是具体的：
    ``max_tokens=4096`` 在这套产品里会把真实端到端打挂（推理型模型把预算全烧在思考上，
    报错文案见 ``video_workbench/infra/llm.py`` 的「把 max_tokens 调大，或换一个非推理
    模型」；``role="standard"`` 也已退役）。将来有人想在迁移分支里「好心」把 4096 抬到
    JSON 下限 —— 那是同时做两件事：违背搬家那句逐字段原样搬运的承诺，并把「谁负责改值」
    从用户手里挪走。值坏了要修，修的位置是设置页与首启引导，不是迁移代码。

    所以这里断言的是「坏值必须原样活着」，而不是「产品帮用户变好」。
    """
    _write_legacy_home(homes)
    legacy_before = _tree_fingerprint(homes["legacy"])

    cfg = WorkbenchConfig.load()

    assert cfg.llm.max_tokens == 4096, (
        f"迁移把 max_tokens 改了：旧家 4096 → 实得 {cfg.llm.max_tokens!r}。"
        "搬家语义是逐字段原样搬运（C 决定），要抬值得走设置页，不许在迁移分支里做。"
    )
    assert cfg.llm.role == "standard", (
        f"迁移把 role 改了：旧家 'standard' → 实得 {cfg.llm.role!r}。"
        "退役角色由 resolve_role 的回落 + 提示处理，不在搬家里替用户改选择。"
    )

    # 只在内存里对不算数：新家那份 yaml 落盘的字面量也得是原值，
    # 否则下次启动读回来的就是被抬过的值。
    parsed = yaml.safe_load(homes["new_config"].read_text(encoding="utf-8"))
    assert parsed["llm"]["max_tokens"] == 4096, parsed["llm"]
    assert parsed["llm"]["role"] == "standard", parsed["llm"]

    # 第二次 load：新家已存在 ⇒ 不该再搬一次，也不该在这一趟里改值。
    again = WorkbenchConfig.load()
    assert again.migrated_from is None, "第二次 load 仍在报告迁移，二次搬运会盖掉用户后来的改动"
    assert again.llm.max_tokens == 4096
    assert again.llm.role == "standard"

    assert _tree_fingerprint(homes["legacy"]) == legacy_before, "旧家被动过：搬家只许读它"


def _diff(a: dict, b: dict, prefix: str = "") -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if isinstance(va, dict) and isinstance(vb, dict):
            out.update(_diff(va, vb, f"{prefix}{key}."))
        elif va != vb:
            out[f"{prefix}{key}"] = (va, vb)
    return out


def test_migration_is_a_one_time_event_and_leaves_an_assertable_trace(homes):
    """A1 续：迁移发生过要留下可断言的证据（``cfg.migrated_from``），而且**只留一次**。

    合同只要求「可断言的形式」，vendor 选了挂在实例上的字段名 ``migrated_from``；
    改成别的名字或只打日志的话，这条会红，请同步这条用例并把形式写回合同——不是删掉这条。
    第二趟必须没有证据：新家已经有文件了，还在报「我刚搬完」就说明判据没条件化，
    日志与界面提示会每次启动都骗用户一次。
    """
    _write_legacy_home(homes)

    first = WorkbenchConfig.load()
    got = getattr(first, "migrated_from", None)
    assert got is not None, (
        "cfg 上没有迁移证据（在等 task-22：load() 里那条「新家没有文件且旧家有 → 搬」"
        "还没落地，或者没留可断言的标记）"
    )
    assert Path(str(got)) == homes["legacy_config"], f"迁移证据应指旧家 config，实得 {got!r}"

    second = WorkbenchConfig.load()
    assert getattr(second, "migrated_from", None) is None, (
        f"第二次 load 仍然报告迁移：{second.migrated_from!r}——迁移必须是一次性事件。"
        f"（新家 config 存在？{homes['new_config'].exists()}；"
        f"DEFAULT_CONFIG_PATH={wb.DEFAULT_CONFIG_PATH}；source_path={second.source_path}）"
    )
    assert _comparable(second) == _comparable(first), (
        "搬家落地之后，第二次读到的值与第一次不同：新家那份没把值搬全"
    )


def test_migration_carries_the_foreign_shaped_old_file_without_touching_it(homes):
    """A1 续：旧版本把扩展键写在 yaml 里的那份旧家，形状重写只准落新家。

    ``load()`` 里 ``if foreign: cfg.save(target)`` 这条是「WB 会动对方文件」的第二条
    路径：搬家之后 target 必须永远是新家，否则用户只是打开一次工作台就把 bili-scribe
    的配置重写了。
    """
    _write_legacy_home(homes, foreign=True)
    legacy_before = _tree_fingerprint(homes["legacy"])

    cfg = WorkbenchConfig.load()

    assert cfg.download.quality == OLD_DOWNLOAD_QUALITY, "旧 yaml 里的 download 段没搬过来"
    assert cfg.llm.tasks.summary == OLD_TASKS_SUMMARY, "旧 yaml 里的 llm.tasks 没搬过来"
    assert cfg.ui.rail_collapsed is OLD_UI_RAIL, "旧 yaml 里的 ui.rail_collapsed 没搬过来"
    assert _tree_fingerprint(homes["legacy"]) == legacy_before, (
        "形状重写写到了旧家：那条 save 的目标必须是新家"
    )
    new_raw = yaml.safe_load(homes["new_config"].read_text(encoding="utf-8"))
    assert wb.foreign_keys(new_raw) == [], (
        f"新家 yaml 里还留着 bili 不认识的键：{wb.foreign_keys(new_raw)}（形状迁移没做完）"
    )


# ---------------------------------------------------------------- 判据 A2：新家优先


def test_the_new_home_wins_and_the_old_home_is_never_a_fallback(homes):
    """A2 两家都有且值不同 → 读到的是新家；再把旧家改成明显不同的值，cfg 完全不变。

    这条钉的是「不许把回退链加回来」。搬家后旧家再参与读优先级，等于两份配置
    互相不知道对方存在；用户在新家删掉一行，旧家那份又活了。
    """
    _write_legacy_home(homes)
    # 新家写一个和旧家明显不同的值。
    homes["new_config"].write_text(
        yaml.safe_dump(
            {"llm": {**OLD_LLM, "model": "NEWHOME-model-z", "api_key": "sk-NEWHOME-9"}}
        ),
        encoding="utf-8",
    )
    first = WorkbenchConfig.load()
    assert first.llm.model == "NEWHOME-model-z", (
        f"读到的不是新家：model={first.llm.model!r}（旧家那份是 {OLD_LLM['model']!r}）"
    )
    assert first.llm.api_key == "sk-NEWHOME-9"

    # 把旧家换成完全不同的值，再读一次：必须一模一样。
    homes["legacy_config"].write_text(
        yaml.safe_dump({"llm": {**OLD_LLM, "model": "OLDHOME-after-edit-y", "temperature": 0.77}}),
        encoding="utf-8",
    )
    homes["legacy_ext"].write_text(
        json.dumps({"llm": {"providers": [], "active_provider": "", "tasks": {}}}),
        encoding="utf-8",
    )
    second = WorkbenchConfig.load()
    assert _comparable(second) == _comparable(first), (
        "旧家被改动之后 cfg 跟着变了，说明旧家还在读优先级里：\n"
        + "\n".join(f"  {k}" for k in sorted(_diff(_comparable(first), _comparable(second))))
    )


def test_a_missing_new_sidecar_does_not_fall_back_to_the_old_one(homes):
    """A2 续：新家 yaml 在、sidecar 被删 → 也不许去旧家拿服务商。

    旧家那份 ``workbench.json`` 里是**上一版**的服务商与密钥；拿它兜底等于
    让「删掉 sidecar」变成「悄悄换一家网关」。
    """
    _write_legacy_home(homes)
    homes["new_config"].write_text(
        yaml.safe_dump({"llm": {**OLD_LLM, "model": "NEWHOME-model-z"}}), encoding="utf-8"
    )
    if homes["new_ext"].exists():
        homes["new_ext"].unlink()

    cfg = WorkbenchConfig.load()
    assert all(p.id not in OLD_PROVIDER_IDS for p in cfg.llm.providers), (
        f"服务商是从旧家 sidecar 里捞回来的：{[p.id for p in cfg.llm.providers]}"
    )


# ---------------------------------------------------------------- 判据 A3/A4


def test_neither_home_present_gives_the_builtin_defaults(homes):
    """A3 两边都没有 → 内置默认，不抛；role 是 WB 自己那份默认。

    ``role`` 这里必须钉成字面量：它是 ``LlmConfig`` 对父类 ``standard`` 的覆写
    （``video_workbench/config.py`` 里带着整段理由的注释），首启用户点「生成」吃的就是它。
    """
    cfg = WorkbenchConfig.load()
    assert cfg.llm.role == "wengao", f"首启默认角色被改动：{cfg.llm.role!r}"
    assert cfg.ui.skin in wb.SKIN_KEYS
    assert cfg.llm.extra_prompt is False, "附加提示词默认必须关（和 JSON 契约正面冲突）"
    assert not homes["new_config"].exists(), "只是读一下就把新家建出来了"


def test_an_explicit_path_never_triggers_migration(homes):
    """A4 显式路径最高：指到哪读到哪，且不凭空创建新家。

    ``desktop.py --config`` 走的就是这条。要是显式路径也会触发迁移，测试与便携配置
    就会在用户家里凭空造出一份 ``~/.video-workbench``。
    """
    _write_legacy_home(homes)
    explicit = homes["base"] / "portable" / "config.yaml"
    explicit.parent.mkdir(parents=True)
    explicit.write_text(
        yaml.safe_dump({"llm": {**OLD_LLM, "model": "EXPLICIT-model-e"}}), encoding="utf-8"
    )

    cfg = WorkbenchConfig.load(explicit)
    assert cfg.llm.model == "EXPLICIT-model-e"
    assert Path(cfg.source_path) == explicit
    assert not any(
        (homes["new"] / name).exists() for name in ("config.yaml", "workbench.json")
    ), "显式路径也触发了迁移：新家被凭空建出来了"


def test_an_explicit_path_that_does_not_exist_does_not_read_the_old_home(homes):
    """A4 续：显式路径**不存在**时也不许转头去读旧家。

    这是 ``--config`` 最容易踩的一脚：用户（或测试夹具）指了一个还没写的路径，
    如果实现把「文件不存在」当成「该搬家了」，他就会在 A 位置读配置、拿到 B 位置的值，
    而 `save()` 又把那份值写回 A —— 现场表现为「我明明没填服务商，怎么冒出来一叠」。
    """
    _write_legacy_home(homes)
    missing = homes["base"] / "portable2" / "config.yaml"

    cfg = WorkbenchConfig.load(missing)

    assert cfg.llm.model != OLD_LLM["model"], (
        f"显式路径不存在却读到了旧家的值：{cfg.llm.model!r}"
    )
    assert cfg.llm.api_key != OLD_LLM["api_key"], "显式路径不存在却读到了旧家的密钥"
    assert not missing.exists(), "读一个不存在的显式路径不该顺手把它写出来"
    assert not any(
        (homes["new"] / name).exists() for name in ("config.yaml", "workbench.json")
    ), "显式路径的回落把新家也建出来了"


# ---------------------------------------------------------------- 判据 A5/A6


def test_saving_never_writes_the_old_home_and_keeps_the_new_yaml_bili_shaped(homes):
    """A5 迁移并保存之后：旧家指纹不变，新家 yaml 仍然只含 bili 认识的键/段。

    后半个判据把 ``render_yaml`` 的保守性在新家继续钉住：搬家换来的是**目录自由**，
    不是「可以随便写」——形状继续由 bili 的模板说了算，这份文件也仍然被 bili 读得动。
    """
    _write_legacy_home(homes)
    legacy_before = _tree_fingerprint(homes["legacy"])

    cfg = WorkbenchConfig.load()
    cfg.llm.temperature = 0.42
    cfg.download.quality = "1080p"
    info = cfg.save()

    assert _tree_fingerprint(homes["legacy"]) == legacy_before, (
        "保存写到了旧家目录（连 .bak 都不许有）："
        f"跑前 {legacy_before} → 跑后 {_tree_fingerprint(homes['legacy'])}"
    )
    assert Path(info["path"]) == homes["new_config"]
    raw = yaml.safe_load(homes["new_config"].read_text(encoding="utf-8"))
    assert set(raw) <= BILI_SECTIONS, f"新家 yaml 冒出 bili 不认识的段：{sorted(set(raw) - BILI_SECTIONS)}"
    assert wb.foreign_keys(raw) == [], f"新家 yaml 里还有扩展键：{wb.foreign_keys(raw)}"
    assert "providers" not in raw.get("llm", {}), "服务商列表必须留在 sidecar"
    assert "tasks" not in raw.get("llm", {}), "llm.tasks 必须留在 sidecar"


def test_bili_scribe_reads_the_old_home_exactly_the_same_after_we_moved_out(homes):
    """A6 真验收（最有价值的一条）：WB 跑过一次之后，bili-scribe 读旧家那份**结果逐字段相同**。

    这条就是当年把扩展键赶进 sidecar 的全部理由，搬家之后比搬家前更要紧：旧家那份
    文件现在是 bili-scribe 一个人的，WB 哪怕只是「顺手备份一次」都等于替用户改了
    对方程序的配置。这里不用 ``foreign=True`` 那份脏文件——脏文件在 WB 出现之前就
    已经把 bili-scribe 打挂了，那是另一个既有问题（见下面那条），混进来只会让这条
    判据变成「bili 一直读不动」的空断言。
    """
    _write_legacy_home(homes)
    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])
    theirs_before = dataclasses.asdict(bc.AppConfig.load(homes["legacy_config"]))

    cfg = WorkbenchConfig.load()
    cfg.save()

    theirs_after = dataclasses.asdict(bc.AppConfig.load(homes["legacy_config"]))
    assert theirs_after == theirs_before, (
        "WB 跑过一次之后，bili-scribe 读旧家的结果变了：\n"
        + "\n".join(f"  {k}" for k in sorted(_diff(theirs_before, theirs_after)))
    )


def test_the_old_home_is_no_longer_cleaned_up_for_them(homes):
    """A6 续：旧家那份带扩展键的脏文件，搬家之后**没人再替它清理**——钉住这个后果。

    搬家前 ``load()`` 里那条 ``if foreign: cfg.save(target)`` 会顺手把共享 yaml 重写干净，
    用户打开 bili-scribe 才起得来。搬家后那条重写只作用于新家（这是对的：不能写对方
    文件），代价是**老用户的 bili-scribe 仍然起不来，而且再也没人帮他清**。
    这不是测试想当然：下面三个断言把「bili 读不动旧家、WB 自己跑得好、旧家字节没变」
    这三件事同时钉住，谁改成「顺手清理旧家」这条就会红（那等于又开始写对方文件）。
    """
    _write_legacy_home(homes, foreign=True)
    legacy_before = _tree_fingerprint(homes["legacy"])
    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])

    with pytest.raises(ValueError) as bili_blows_up:
        bc.AppConfig.load(homes["legacy_config"])
    assert "未知配置项" in str(bili_blows_up.value), (
        f"旧家脏文件让 bili-scribe 报的不是那句话：{bili_blows_up.value}"
    )

    cfg = WorkbenchConfig.load()
    assert cfg.download.quality == OLD_DOWNLOAD_QUALITY, "WB 自己被旧家的脏键挡住了"
    assert _tree_fingerprint(homes["legacy"]) == legacy_before, (
        "WB 又开始写对方目录来「顺手清理」了——那条重写只能作用于新家"
    )


def test_the_moved_file_is_still_a_shape_bili_scribe_can_read(homes):
    """A6 续：新家那份 bili 也读得动——实测事实，钉成用例（不是「应该」）。

    读得动是「搬家没把它变成奇怪形状」的证据；**但搬家之后 WB 不再依赖这一点**，
    所以这条如果因为将来新家用了扩展键而变红，那是一次有意的取舍变更：
    要改成显式登记「bili-scribe 不读这份，这是搬家换来的自由」，不许直接删。
    """
    _write_legacy_home(homes)
    cfg = WorkbenchConfig.load()
    cfg.save()

    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])
    theirs = bc.AppConfig.load(homes["new_config"])  # 抛就是红，不 catch
    assert theirs.llm.model == cfg.llm.model, "bili 读到的 model 与 WB 写出去的不是一个值"
    assert theirs.llm.max_tokens == cfg.llm.max_tokens


# ---------------------------------------------------------------- 判据 B：库仍共用


def test_the_database_and_cookies_stay_shared_on_purpose(homes):
    """B 搬家拆的是**配置文件**，不是数据文件。

    ``storage.db_path`` 的默认值必须仍然指向 bili-scribe 的那个库：方案书要求两边共用
    一份数据，B 站登录凭据也在那里。把它「顺手搬进新家」的代价是用户已有的项目库和
    总结历史（本机 86 条 ``summary_version``）凭空变空——看起来像数据丢了。
    这条就是拦那个的。
    """
    # 刻意读一个**不存在的显式路径**：这样拿到的就是内置默认，不受本用例里任何
    # 旧家/新家文件的影响 —— 牙齿用例会把这条当控制断言复用，不能被前面写过的文件污染。
    cfg = WorkbenchConfig.load(homes["base"] / "nothing-here.yaml")
    # 参照系用 bili 自己的常量，不用本文件夹具改写过的 ``wb.LEGACY_HOME``。
    assert str(cfg.storage.db_path) == str(BILI_HOME / "bili-scribe.db"), (
        f"库的默认位置被搬走了：{cfg.storage.db_path!r}；"
        f"它必须仍然与 bili-scribe 共用（{BILI_HOME / 'bili-scribe.db'}）"
    )
    assert str(cfg.storage.key_path) == str(BILI_HOME / "key.bin"), (
        f"B 站凭据路径被搬走了：{cfg.storage.key_path!r}"
    )
    assert cfg.notes_dir == str(Path(cfg.db_path).parent / "notes-cache"), (
        "预读缓存目录必须仍跟着库走：搬开它等于白白重付预读"
    )
    # 配置文件换了家，数据文件没换：两件事必须在代码里分开说清楚。
    assert Path(cfg.db_path).parent == BILI_HOME
    assert Path(cfg.db_path).parent != MODULE_DEFAULT_HOME, (
        "库与配置文件又回到同一个目录了：这次拆的就是这个"
    )


def test_the_default_home_constants_are_two_different_homes() -> None:
    """B 续：新家常量指 WB 自己的目录，旧家常量指 bili-scribe 自己的家，两者不同。

    断言用**导入期快照**：本文件的夹具会把 ``wb.*`` 改到 Temp（那是为了能测搬家），
    拿改过的值去比 ``Path.home()`` 就是自己考自己。名字可以变，指向不能变——
    「旧家只读」这条判据的整个意义就在于它是**对方的**目录。
    """
    assert MODULE_DEFAULT_HOME == REAL_HOME / NEW_HOME_DIRNAME, (
        f"WB 的默认家不是 {NEW_HOME_DIRNAME}：{MODULE_DEFAULT_HOME}"
    )
    assert MODULE_DEFAULT_CONFIG_PATH == MODULE_DEFAULT_HOME / "config.yaml"
    assert MODULE_LEGACY_HOME == BILI_HOME, (
        f"旧家常量不是 bili-scribe 的家：{MODULE_LEGACY_HOME} ≠ {BILI_HOME}"
    )
    assert MODULE_LEGACY_CONFIG_PATH == MODULE_LEGACY_HOME / "config.yaml"
    assert MODULE_DEFAULT_HOME != MODULE_LEGACY_HOME, (
        "新家与旧家是同一个目录：搬家没生效（在等 task-22）"
    )


# ---------------------------------------------------------------- 现场保护


def test_the_harness_never_writes_the_real_home(homes):
    """夹具自检：本文件的用例只许在 Temp 里读写真实家目录以外的地方。

    单独占一条是因为这是**测试自己的**性质，不是产品的：``fixture`` 里的对比如果
    因为某条用例提前崩掉而没跑到，这条会用同一套指纹再量一次。
    """
    _write_legacy_home(homes)
    before = _real_home_snapshot()
    WorkbenchConfig.load()
    WorkbenchConfig.load(homes["new_config"])
    assert _real_home_snapshot() == before, "用例写到真实家目录了"
    # 常量确实被指到 Temp：没指到就是夹具失效，后面所有判据都不可信。
    assert str(wb.DEFAULT_CONFIG_PATH).startswith(str(homes["base"]))
    assert str(wb.LEGACY_CONFIG_PATH).startswith(str(homes["base"]))


# ---------------------------------------------------------------- 牙齿四条
#
# 四条坏法都用「运行时替换产品函数」模拟，不改任何产品文件（上一轮改真实工作区文件
# 把读数搅过一次，这次不再发生）。每条坏法都必须在**它该红的那条**上红，
# 在其余判据上不许连带变红——否则说明判据分不开，也就守不住东西。


def _broken_save_to_legacy(monkeypatch):
    """坏法①：保存之后顺手回写旧家（「搬家不彻底」最常见的样子）。"""
    real_save = WorkbenchConfig.save

    def save(self, path=None):
        info = real_save(self, path)
        target = Path(info["path"])
        legacy = wb.LEGACY_CONFIG_PATH
        if target != legacy:
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_bytes(target.read_bytes())
        return info

    monkeypatch.setattr(WorkbenchConfig, "save", save)


def test_tooth_1_writing_back_to_the_old_home_turns_a1_and_a5_red(homes, monkeypatch):
    """牙齿①：旧家被回写 → A1 与 A5 都必须红，而且红在「旧家被动过」那一句。

    每条牙齿都同时断两件事：**该红的红**，以及**不该受牵连的仍然绿**（控制断言写在
    每条最后一行）。只有前半句的牙齿说明判据糊成一团，坏一处就全红，那种锁指不回位置。
    """
    _broken_save_to_legacy(monkeypatch)
    with pytest.raises(AssertionError) as caught:
        test_migration_moves_every_value_and_writes_nothing_in_the_old_home(homes)
    assert "旧家" in str(caught.value), f"A1 红的不是零写入那一句：{caught.value}"
    with pytest.raises(AssertionError) as caught:
        test_saving_never_writes_the_old_home_and_keeps_the_new_yaml_bili_shaped(homes)
    assert "旧家" in str(caught.value), f"A5 红的不是零写入那一句：{caught.value}"
    # 控制：这条坏法只多写旧家，不碰常量、也不碰数据文件默认值，那两条不该被波及。
    test_the_default_home_constants_are_two_different_homes()
    test_the_database_and_cookies_stay_shared_on_purpose(homes)


def test_tooth_2_old_home_back_in_the_read_chain_turns_a2_red(homes, monkeypatch):
    """牙齿②：把旧家重新接进读优先级 → A2 必须红。"""
    real_load = WorkbenchConfig.load.__func__

    def load(cls, path=None):
        cfg = real_load(cls, path)
        if path is None and wb.LEGACY_CONFIG_PATH.exists():
            raw = yaml.safe_load(wb.LEGACY_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            merged = wb._merge_raw(raw, wb.read_ext(wb.LEGACY_CONFIG_PATH))
            if isinstance(merged.get("llm"), dict) and merged["llm"].get("model"):
                cfg.llm.model = merged["llm"]["model"]
        return cfg

    monkeypatch.setattr(WorkbenchConfig, "load", classmethod(load))
    with pytest.raises(AssertionError) as caught:
        test_the_new_home_wins_and_the_old_home_is_never_a_fallback(homes)
    assert "旧家" in str(caught.value) or "新家" in str(caught.value), str(caught.value)
    # 控制：这条坏法只碰读优先级，不碰常量与库路径，那两条不该被波及。
    test_the_default_home_constants_are_two_different_homes()
    test_the_database_and_cookies_stay_shared_on_purpose(homes)


def test_tooth_5_default_home_moved_into_the_repo_turns_the_secret_lock_red(monkeypatch):
    """牙齿⑤（我自己加的那条）：把默认家指进仓库 → ``test_secret_paths_ignored`` 那条必红。

    搬家换了目录，但「家必须在仓外」这件事以前是靠巧合成立的（``~/.bili-scribe`` 本来就在
    仓外）。搬家之后它是**设计约束**：``workbench.json`` 里是全部服务商的明文密钥。
    这条坏法证明那条锁不是摆设——它只在有人把目录挪进仓库时红，不会平白无故红。
    """
    import importlib

    sys.path.insert(0, str(ROOT / "tests" / "wb"))
    secrets = importlib.import_module("test_secret_paths_ignored")
    monkeypatch.setattr(wb, "DEFAULT_HOME", ROOT / "build" / "wb-home")
    with pytest.raises(AssertionError) as caught:
        secrets.test_the_default_config_home_lives_outside_the_repo()
    assert "api_key" in str(caught.value), f"红的不是密钥暴露那一句：{caught.value}"


def test_tooth_3_moving_the_database_default_turns_b_red(homes, monkeypatch):
    """牙齿③：把默认库搬进新家目录 → B 必须红（这条专门拦「顺手拆数据文件」）。"""
    import dataclasses as _dc

    real_storage = wb.StorageConfig
    moved = {f.name: (str(homes["new"] / "bili-scribe.db") if f.name == "db_path" else f.default)
             for f in _dc.fields(real_storage)}

    class MovedStorage(real_storage):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.db_path = moved["db_path"]

    monkeypatch.setattr(wb, "StorageConfig", MovedStorage)
    with pytest.raises(AssertionError) as caught:
        test_the_database_and_cookies_stay_shared_on_purpose(homes)
    assert "库" in str(caught.value), str(caught.value)
    # 控制：搬库不碰常量、也不碰读优先级，那两条不该被波及。
    test_the_default_home_constants_are_two_different_homes()
    test_the_new_home_wins_and_the_old_home_is_never_a_fallback(homes)


def test_tooth_4_a_wb_key_in_render_yaml_turns_a5_red(homes, monkeypatch):
    """牙齿④：往 ``render_yaml`` 塞一个 WB 扩展键 → A5 必须红（A6 也会跟着红）。

    坏法要选得准：塞的键必须是 **WB 自己认识、bili-scribe 不认识**的那种
    （``llm.tasks`` 就是现成的），否则产品读自己刚写的文件就崩，红的是一条
    启动崩溃而不是一条形状判据，看不出问题出在「保守性被打开」。
    """
    real_render = wb.render_yaml

    def render(cfg):
        text = real_render(cfg)
        assert "llm:\n" in text, "渲染出来的 yaml 里没有 llm 段，这条坏法没打进东西"
        return text.replace("llm:\n", "llm:\n  tasks:\n    summary: tooth-poison\n", 1)

    monkeypatch.setattr(wb, "render_yaml", render)
    with pytest.raises(AssertionError) as caught:
        test_saving_never_writes_the_old_home_and_keeps_the_new_yaml_bili_shaped(homes)
    assert "扩展键" in str(caught.value), f"红的不是保守性那一条：{caught.value}"


def test_tooth_4_also_blows_up_the_real_acceptance(homes, monkeypatch):
    """牙齿④的另一半：同一个坏法必须让 A6 那条「bili 读得动新家」也炸。

    A5 挡的是形状，A6 挡的是**后果**（对方程序读不动了）。两条都要红才说明这套锁
    真的连着，而不是只有一条在打空。
    """
    real_render = wb.render_yaml

    def render(cfg):
        text = real_render(cfg)
        return text.replace("llm:\n", "llm:\n  tasks:\n    summary: tooth-poison\n", 1)

    monkeypatch.setattr(wb, "render_yaml", render)
    _write_legacy_home(homes)
    cfg = WorkbenchConfig.load()  # 迁移顺手落新家，新家那份就是被污染的
    assert cfg.llm.model, "迁移本身没跑通，这条牙齿测不到点子上"

    bc = __import__("bili_scribe.config", fromlist=["AppConfig"])
    with pytest.raises(ValueError) as caught:
        bc.AppConfig.load(homes["new_config"])
    assert "未知配置项" in str(caught.value), str(caught.value)


#: 本模块**被导入那一刻**的产品文件指纹。牙齿四条都用运行时替换，理论上一次文件写
#: 都不该发生；这条用例拿导入时的快照比现在，比出来的差就是「某条用例偷偷改了产品文件」
#: ——同一次调用里算两遍 sha 是恒真的，所以快照必须在 import 期取。
_WATCHED = (
    ROOT / "video_workbench" / "config.py",
    ROOT / "video_workbench" / "core" / "summary" / "summarizer.py",
    ROOT / "video_workbench" / "core" / "summary" / "roles.py",
    ROOT / "video_workbench" / "infra" / "notecache.py",
)
_IMPORT_TIME_SHA = {str(p): _sha(p) for p in _WATCHED}


def test_teeth_do_not_edit_any_product_file() -> None:
    """牙齿的口径本身也要被钉住：四条坏法只动 monkeypatch，产品文件字节不变。

    上一轮有牙齿脚本改真实工作区文件把读数搅过一次（还有 ``rmtree(d.parent)`` 误删
    %TEMP% 那次），所以这里拿 import 期的快照比现在。副本三文件（summarizer/roles/
    notecache）也一起盯：``tests/wb/test_summary_drift.py`` 的逐字节锁就靠它们没动。
    """
    assert all(v != "MISSING" for v in _IMPORT_TIME_SHA.values()), (
        f"要盯的文件在 import 时就不在盘上：{_IMPORT_TIME_SHA}"
    )
    changed = {p: (d, _sha(Path(p))) for p, d in _IMPORT_TIME_SHA.items() if _sha(Path(p)) != d}
    assert not changed, "本文件的用例改过产品文件（牙齿应当只用运行时替换）：" + "\n".join(
        f"  {p}: {before} → {after}" for p, (before, after) in changed.items()
    )
