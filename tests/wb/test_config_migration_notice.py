"""「配置搬家把坑留在旧家」那条一次性提示的常驻回归锁（task-30，回归对象 = task-29）。

产品合同（现读，行号是 2026-11-04 17:5x 的盘）：

* `video_workbench/config.py:587 migration_notice: dict | None = None` —— 声明在 **WB 自己的
  `UiConfig`** 上。为什么只能声明、不能往 sidecar 里塞一个裸键：`_fill()`（`:918-927`）对段内
  未知子键是**直接抛 `VALIDATION`** 而不是忽略，塞一个没声明的键等于让所有用户的 WB 起不来。
  先例是 `onboarded` / `rail_collapsed`。
* `:212 _migration_notice(...)` 两条记录路径：①本次真搬了（`migrated_from` 非空）直接用刚读到的
  `foreign`，零额外盘；②新家早就存在（这台机器在这条提示写出来之前就搬完了）→ 懒读一次旧家 yaml。
  已确认过（`dismissed`）就**连读都不读**（`:236-238`）。
* `:825-828` 把 notice 塞进 `merged["ui"]`，于是它随 sidecar 落盘；
  **`render_yaml` 按 bili 的 `AppConfig` 投影，天生不进 yaml**（`:585-586` 注释）。
* `app/service.py:5572 app_migration_notice()` / `:5586 ..._dismiss()`；
  `web/js/app.js:538 maybeMigrationNotice()`，`:711` 启动调用外包 `.catch`。

## 这套用例守的是用户的什么

搬家之后，重写旧家那一步被正确地取消了（不许写对方文件），于是**老用户的 bili-scribe
会因为那几个未知键起不来，而且不知道原因在 WB 这边**。提示是唯一一处把这个代价说给人听的地方，
所以它的措辞是判据不是装饰：**说「我没改旧家」的那句必须是真的**（M7 用 sha 盯零写入），
**不许出现「已清理 / 已修复 / 已同步 / 自动帮你」这类揽功的话**（M5）——
用户要是信了「已清理」，就会直接去开 bili-scribe，然后撞上同一句报错，且更不知道找谁。

## 三条刻意不与合同字面重复的写法

1. **M7 钉「零写入」，不钉「零访问」**。懒路径在未确认期间每次 `load()` 都会读一次旧家 yaml
   （`:244-246`），这是有意的；点过「知道了」之后连读都没有。钉「零访问」会红在一次正常读取上，
   钉「不读」又会放走真写入 —— 所以这里 import 期取旧家两文件的 sha256 快照，跑前跑后各比一次。
2. **M1 的期望键名从 `foreign_keys()` 现读**，不写死清单（写死的那份会随 bili 的字段表演进
   过期，红的是我的常量不是产品）。同时留一条**正对照**证明夹具真把旧家弄脏了 ——
   否则「两边都是空列表」也算相等，整套 M1 就空转了。
3. **页面侧不许用 fetch/XHR 打 RPC**。产品 CSP 含 `connect-src 'none'`
   （`video_workbench/web/index.html:13`），本文件按 `AGENTS.md` 第四节那条做法：
   `page.expose_binding` 把进程内 `WorkbenchService.dispatch` 挂成 `window.pywebview.api.rpc`，
   与 `desktop.py:142` 同形。**没为测试放宽产品 CSP 一个字符**。

## 演示态（M8）为什么单独钉

`maybeMigrationNotice()` 第一行就 `if (WB.isMock()) return false`，而这条判定**不许被烘进
`mock.js`**（mock 只承载形状，不承载会变的判定结果）。所以 M8 两半：演示态页面必须没有弹窗；
`mock.js` 里必须搜不到 `migration_notice` 这个字面量 —— 谁把判定结果抄进 mock，后一条就红。

## 隔离

`homes` 夹具从 `test_config_home_split` 引进：新旧两家全在 Temp，退出时断言真实
`~/.video-workbench` 与 `~/.bili-scribe` 的 `(sha256, mtime_ns, size)` 快照不变。
`storage.db_path` **不跟配置家走**（两程序共库），夹具里必须显式指 Temp，
否则所谓沙盒其实在读真实库。起真窗口的话只重定向 `HOME`/`USERPROFILE`，
保留真实 `APPDATA`/`LOCALAPPDATA`（四个一起指 Temp 会连撞三次 pythonnet 的
`RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize`，那看起来像产品坏了）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = Path(os.environ.get("WB_WEB_ROOT") or ROOT / "video_workbench" / "web")
ACCEPT_DIR = ROOT / "build" / "accept"
SENTINEL_KEY = "sk-MIGRATION-NOTICE-LEAKTEST-DO-NOT-PRINT"
# M10 的现场：旧家那份 yaml 里的**键名**是用户自己打出来的字符串。
EVIL_KEY = "<img src=x onerror=alert(1)>"

# 跨文件复用 fixture：同目录、无 __init__.py，靠 conftest 的 sys.path 直接引名字。
from test_config_home_split import homes  # noqa: E402  fixture：新旧两家都在 Temp
from test_settings_role_warning_ui import browser, web_base_url  # noqa: E402

# ---------------------------------------------------------------- 工具


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fore(path: Path) -> list[str]:
    """这份旧家 yaml 里 bili-scribe 不认识的键（现读，不写死）。"""
    from video_workbench.config import foreign_keys

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return sorted(str(k) for k in foreign_keys(raw))


def _legacy_yaml(homes_dir: Path, *, dirty: bool, extra_lines: tuple[str, ...] = (),
                 cache_root: str | None = None) -> Path:
    """造一份旧家 `config.yaml`，段顺序照 bili 的模板写。

    `dirty=True` 时放进三个 bili 不认识的键，**三种形状各一个**：
    ①顶层整段（``download``）②`llm` 段内（``tasks``）③`ui` 段内（``rail_collapsed``，
    因为 bili 的 `ui` 只认 ``skin``）。只造一种的话，「foreign_keys 只查顶层」这种
    坏法测不到。

    `cache_root` 是给 M6 用的：搬家那两句日志由 `app/service.py:678` 在服务装配时打，
    所以必须让**第一次** load 就看到 Temp 的 cache_root，否则日志落在真实 `%APPDATA%`，
    用例既读不到又在往用户目录写。
    """
    lines = [
        "llm:",
        f"  api_key: {json.dumps(SENTINEL_KEY)}",
        "  model: \"m\"",
        "  base_url: \"http://127.0.0.1:9/v1\"",
        "  role: \"standard\"",
        "  max_tokens: 4096",
    ]
    if dirty:
        lines += ["  tasks:", "    summary: \"旧版本才有的键\""]
    lines += ["ui:", "  skin: ark"]
    if dirty:
        lines += ["  rail_collapsed: true"]
    lines += [
        "storage:",
        f"  db_path: {json.dumps((homes_dir / 'shared.db').as_posix())}",
        f"  key_path: {json.dumps((homes_dir / 'key.bin').as_posix())}",
    ]
    if cache_root:
        lines.append(f"  cache_root: {json.dumps(cache_root)}")
    if dirty:
        lines += ["download:", "  quality: 1080p"]
    lines += list(extra_lines) + [""]
    path = homes_dir / "config.yaml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _load_default():
    from video_workbench import config as wb

    return wb.WorkbenchConfig.load()


def _seed_new_home(homes) -> Path:
    """先搬一次家并落盘，把新家变成「早就存在」，好让后面的现场走懒发现那一支。"""
    cfg = _load_default()
    cfg.save()
    assert homes["new_config"].exists(), "夹具：新家必须已落文件"
    return homes["new_config"]


def _mark_onboarded() -> None:
    """把「第一次打开」那张体检浮卡关掉，只留我要测的那一条模态。

    为什么必须在夹具里做（而不是让两条模态叠着）：`ui.modal` 的面板选择器是
    `.modal .modal__panel`（`components/ui.js:145`），两条模态同时出现时
    `.first` 拿到的**未必**是搬家那条 —— 上一轮 `test_the_boot_shows…` 就是
    拿着一张「第一次打开 Workbench？三句话」的卡去比搬家文案，红得莫名其妙。
    这里用产品自己的开关（`UiConfig.onboarded`，`app_onboard_done` 写的那个字段），
    不碰任何 DOM、不改产品码。
    """
    cfg = _load_default()
    cfg.apply({"ui": {"onboarded": True}})
    cfg.save()


def _notice_of(cfg):
    return getattr(cfg.ui, "migration_notice", None)


# ------------------------------------------------------------------ M1


def test_a_dirty_legacy_home_leaves_a_notice_with_the_computed_key_names(homes):
    """M1：本次真搬了家 + 旧家有未知键 ⇒ 必须记下这条事实。

    期望的键名**从 `foreign_keys()` 现读**（合同原话），并且留一条正对照：
    夹具确实把旧家弄脏了 —— 不然「notice.keys == [] == foreign_keys([])」这种
    两边都空的相等会让整套 M1 空转。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    expected = _fore(homes["legacy_config"])
    assert expected, "夹具没把旧家弄脏：正对照失效，下面比什么都证明不了"
    assert "download" in expected and "llm.tasks" in expected and "ui.rail_collapsed" in expected, (
        f"三种形状应当都被认出来，现读到 {expected}")

    cfg = _load_default()
    notice = _notice_of(cfg)
    assert isinstance(notice, dict), f"搬了家、旧家脏，却没记提示：cfg.migrated_from={cfg.migrated_from!r}"
    assert notice["keys"] == expected, (
        f"记下来的键名集合与 foreign_keys() 现读结果不同：{notice['keys']} vs {expected}")
    assert notice["from"] == str(homes["legacy_config"]), notice
    assert notice["to"] == str(cfg.source_path), notice
    assert notice["dismissed"] is False, notice
    assert cfg.migrated_from, "走了懒发现而不是真搬家 —— 夹具的新家本该是空的"


def test_the_notice_is_recorded_even_on_a_machine_that_moved_earlier(homes):
    """M1 的懒发现腿：新家早就存在（这台机器在提示写出来之前就搬完了），照样得说。

    这是任务存在的理由那一半：产品主线那批用户永远不会再触发「真搬家」分支，
    只有懒发现能让他们看见。少这条，功能对老用户等于没上。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    first = _load_default()
    assert _notice_of(first), "第一次（真搬家）就该记一条"
    first.save()

    again = _load_default()
    notice = _notice_of(again)
    assert notice, "新家已存在时懒发现没补上提示：老用户永远看不见这件事"
    assert notice["keys"] == _fore(homes["legacy_config"]), notice["keys"]


# ------------------------------------------------------------------ M2


def test_a_clean_legacy_home_records_nothing_and_prompts_nothing(homes):
    """M2：旧家干净 ⇒ 不记、不提示、不写日志。

    这台机器真实旧家现读 `foreign_keys() == []`，所以「本机没弹」既可能是「根本没搬」
    也可能是「搬了但旧家干净」—— 用 `migrated_from` 把这两种分开（lead 指定的写法），
    别拿「我这台没弹」当通过证据。
    """
    _legacy_yaml(homes["legacy"], dirty=False)
    cfg = _load_default()
    assert cfg.migrated_from, "夹具应当走真搬家分支；没搬的话这条测的是另一种现场"
    assert _notice_of(cfg) is None, "旧家一个未知键都没有，凭什么弹提示"
    assert not _fore(homes["legacy_config"]), "夹具本该是干净的，实际却脏了"


def test_an_explicit_config_path_invents_no_notice(homes):
    """M2 的第二种「不该弹」：显式 `--config` 指到哪读到哪，不替用户发明提示。

    这条防的是把 notice 挂在「只要旧家有未知键就记」上：用户把配置放在第三个位置
    （U 盘、CI、临时目录）时，旧家那份跟他这次启动没有关系，弹窗纯属噪音。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    assert _fore(homes["legacy_config"]), "旧家必须有未知键，否则这条不测任何东西"
    explicit = homes["new"] / "elsewhere.yaml"
    explicit.write_text(
        homes["legacy_config"].read_text(encoding="utf-8"), encoding="utf-8")
    from video_workbench import config as wb

    cfg = wb.WorkbenchConfig.load(explicit)
    assert _notice_of(cfg) is None, (
        "显式 --config 也发明了一条提示：那份旧家跟这次启动没关系")


def test_a_broken_or_absent_legacy_home_never_breaks_an_installed_machine(homes):
    """M2 的健壮性腿（**懒发现那一支**）：旧家不存在 / 坏 YAML / 顶层不是映射 ⇒ 都不影响启动。

    `config.py:196-209` 的三个分支各造一次现场，让它们**各自**给出空清单，
    而不是「产品看起来没抛」。旧家是别人的文件，它坏了不能把已经装好的 WB 一起拖倒。

    注意这支的前提：**新家已经有配置文件**。新家还没有文件的那一支见下一条 ——
    那一支产品是**故意抛**的，别混成一条。
    """
    from video_workbench import config as wb

    # ① 旧家不存在（`homes` 夹具的新家默认也没文件，先落一份，锁死「懒发现」这一支）
    _seed_new_home(homes)
    assert wb._legacy_foreign_keys() == [], "旧家不存在时不该报任何东西"
    assert _notice_of(_load_default()) is None, "旧家不存在却发明了一条提示"

    # ② 坏 YAML
    _legacy_yaml(homes["legacy"], dirty=True)
    homes["legacy_config"].write_text("llm:\n  api_key: \"x\"\n : 冒号前面什么都没有\n",
                                      encoding="utf-8")
    assert wb._legacy_foreign_keys() == [], "旧家 YAML 坏了不该影响 WB 启动"
    assert _notice_of(_load_default()) is None, "旧家读不出东西时不该猜一份键名清单"

    # ③ 顶层不是映射
    homes["legacy_config"].write_text("- 就是一行列表\n- 另一行\n", encoding="utf-8")
    assert wb._legacy_foreign_keys() == []
    assert _notice_of(_load_default()) is None


def test_a_broken_legacy_home_stops_the_first_migration_loudly(homes):
    """第一次搬家碰上坏 YAML 的旧家：产品**必须**停下来报错，这条钉住那个选择。

    和上一条的区别是有意的：懒发现只是「顺手看一眼」，看不到就当没事；
    搬家那一步是要**把这份文件当用户的配置搬走**，读不懂却默默用内置默认值，
    用户会得到一个「设置全丢了但看起来正常」的新家 —— 那比启动失败更糟
    （`config.py:812-819` 的注释说的就是这件事：坏 YAML 必须在这里停住）。

    所以这条断言的是**报错的形状**：`VALIDATION`、一句中文、指出缩进、带上旧家路径。
    为什么不是「不许抛」：那是 lead 口头给的「旧家坏掉不影响启动」，现读只在懒发现那支成立；
    搬家那支产品选的是响亮地失败。**两种设计都站得住，但合同得写对是哪一种** —— 已报 lead。
    """
    from video_workbench import config as wb
    from video_workbench.app.errors import WBError

    _legacy_yaml(homes["legacy"], dirty=True)
    homes["legacy_config"].write_text("llm:\n  api_key: \"x\"\n : 冒号前面什么都没有\n",
                                       encoding="utf-8")
    assert not homes["new_config"].exists(), "前置：新家必须还没落文件，走的才是搬家那支"
    with pytest.raises(WBError) as exc:
        wb.WorkbenchConfig.load()
    text = str(exc.value)
    assert "VALIDATION" in text, text
    assert "缩进" in text or "YAML" in text, f"报错没告诉用户怎么修：{text}"
    assert str(homes["legacy_config"]) in text or "config.yaml" in text, (
        f"报错没指出是哪个文件：{text}")
    assert not homes["new_config"].exists(), (
        "读不懂旧家却已经把默认值写成新家了：用户下次启动就再也找不到原文件")


# ------------------------------------------------------------------ M3


def test_the_notice_key_never_reaches_the_shared_yaml(homes):
    """M3 前半：搬家提示只进 sidecar，**不许进 yaml**。

    单独拆成一条（原来两半挤在一条里）：合同要求牙齿 b2「把键塞进 render 出的 yaml」
    之后 **M3 两半都红**；合成一条就只能看到第一个断言，第二半被挡住看不见。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    cfg = _load_default()
    assert _notice_of(cfg), "前置：得先有一条提示"
    cfg.save()

    text = homes["new_config"].read_text(encoding="utf-8")
    assert "migration_notice" not in text, "提示漏进共享 yaml 了"
    sidecar = json.loads(homes["new_ext"].read_text(encoding="utf-8"))
    assert sidecar.get("ui", {}).get("migration_notice", {}).get("keys"), sidecar


def test_bili_scribe_can_still_read_the_migrated_config(homes):
    """M3 后半：搬完家的那份 yaml，**bili-scribe 必须还能读** —— 这才是前半的「为什么」。

    不是格式洁癖：那份文件是两程序共用的，bili 的加载器遇到不认识的键直接抛
    「未知配置项」，本来要修的坑反而被 WB 自己又造一个。
    用 bili 自己的加载器读，不用我的猜测。
    末尾带一条**对照**：同一个键塞进 yaml，bili 那边确实会拒 ——
    没有这条对照，上面那句「不抛」可能就是空话（键根本没被读到）。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    cfg = _load_default()
    assert _notice_of(cfg), "前置：得先有一条提示"
    cfg.save()

    text = homes["new_config"].read_text(encoding="utf-8")
    import bili_scribe.config as bili_cfg

    bili_cfg.AppConfig.load(homes["new_config"])     # 不抛 = 用户那边的 bili 还起得来

    poisoned = text.replace("ui:\n", "ui:\n  migration_notice: {keys: []}\n", 1)
    assert "migration_notice" in poisoned, "夹具的对照写法没生效"
    bad = homes["base"] / "poisoned.yaml"
    bad.write_text(poisoned, encoding="utf-8")
    with pytest.raises(Exception) as exc:
        bili_cfg.AppConfig.load(bad)
    assert "migration_notice" in str(exc.value) or "未知" in str(exc.value), str(exc.value)


# ------------------------------------------------------------------ M4


def _dismiss_through_the_service(homes) -> None:
    """按前端点「知道了」走的**同一条**代码把提示确认掉（不自己改字段）。

    刻意走 RPC 而不是直接 `cfg.ui.migration_notice["dismissed"] = True`：
    后者测的是我自己在改字典，产品那条 `save()` 一步根本没参与，
    M4 的两条腿就变成摆设。
    """
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=None)
    try:
        got = svc.dispatch("app.migration_notice", {}, "t30-read")["data"]
        assert got["notice"], f"前置：后端没把提示给出来 {got}"
        out = svc.dispatch("app.migration_notice.dismiss", {}, "t30-ok")["data"]
        assert out["dismissed"] is True, out
    finally:
        svc.close()


def test_an_unrelated_save_before_confirmation_keeps_the_notice(homes):
    """M4③：还没确认过的时候，一次无关保存不许把提示冲掉。

    这条单独拆出来（原来和①②挤在一条里）：牙齿要能指名「哪一条腿坏了」，
    合成一条就没法区分「确认没落盘」和「保存把没看过的东西弄丢了」。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    cfg = _load_default()
    assert _notice_of(cfg), "前置：刚搬完家应当有提示"

    cfg.apply({"ui": {"skin": "exhibition"}})   # 改的是完全无关的字段
    cfg.save()
    assert _notice_of(_load_default()), "一次无关保存就把还没看过的提示冲没了"


def test_the_dismissal_survives_a_fresh_process(homes):
    """M4①：确认之后，新进程 `load()` 读回来必须仍是已确认，且**事实还在**。

    `keys` 不许被确认抹掉 —— 提示的内容是「旧家有哪些键会让 bili-scribe 起不来」，
    这个事实不会因为用户点过「知道了」就变成假的（要复查时它得还在）。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    assert _notice_of(_load_default()), "前置：先要有提示"
    _dismiss_through_the_service(homes)

    notice = _notice_of(_load_default())
    assert notice and notice["dismissed"] is True, notice
    assert notice["keys"] == _fore(homes["legacy_config"]), "确认只该改标记，不该把事实删掉"

    # 后端口径：确认之后必须回答「没有要提示的」，而且只在这一处判
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=None)
    try:
        assert svc.dispatch("app.migration_notice", {}, "t30-after")["data"]["notice"] is None
    finally:
        svc.close()


def test_the_dismissal_survives_an_unrelated_save(homes):
    """M4②：确认之后改一个无关字段并保存，已确认标记必须熬过去。

    这是真正会灭的那条：`save()` 整体重写 sidecar，如果 `dismissed` 只活在内存里、
    或者写 sidecar 时没带上它，用户下次改皮肤就又被弹一次。
    牙齿变体「确认改成：下一次 save() 把它抹掉」专门冲这条来（合同第 4 条）。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    assert _notice_of(_load_default()), "前置：先要有提示"
    _dismiss_through_the_service(homes)

    after = _load_default()
    assert after.ui.migration_notice["dismissed"] is True
    after.apply({"ui": {"skin": "ark"}})
    after.save()
    notice = _notice_of(_load_default())
    assert notice and notice["dismissed"] is True, (
        "无关保存把 dismissed 抹掉了：用户改个皮肤就被重新弹一次")


# ------------------------------------------------------------------ M6


def test_no_value_of_a_foreign_key_reaches_the_notice_logs_or_screen(homes):
    """M6：提示只许带**键名**；密钥的值不许出现在提示经过的任何出口。

    顺序很重要：搬家那两句日志由 `app/service.py:678` 在**服务装配**时打，
    所以要让服务来做第一次 load（我自己先 load 一次就把迁移消耗掉了，日志腿变成
    无事可查的空断言）。而 `cache_root` 必须写进**旧家**那份 yaml —— 新家的默认值
    来得太晚，日志会落进真实 `%APPDATA%/VideoWorkbench/logs`：既读不到，又在污染用户目录。

    同时钉住反方向：这把密钥必须原样搬进新家的 `config.yaml` 与 sidecar ——
    那是用户自己的配置，搬家丢掉才是事故（task-23 已钉「逐字段原样搬运」）。
    我上一轮把 sidecar 也列进「不许出现哨兵」，那是在要求程序忘掉用户的密钥：
    M6 防的是**提示**把别人的值印出来，不是要求配置存储清空密钥。
    """
    cache = homes["base"] / "cache"
    legacy = _legacy_yaml(homes["legacy"], dirty=True, cache_root=cache.as_posix())
    assert SENTINEL_KEY in legacy.read_text(encoding="utf-8"), "哨兵没写进旧家，下面白测"

    from video_workbench import config as wb
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=None)      # 由服务完成第一次 load + 打那两句
    try:
        payload = json.dumps(svc.dispatch("app.migration_notice", {}, "t30-6"),
                             ensure_ascii=False)
        assert SENTINEL_KEY not in payload, f"RPC 载荷里带了旧家的密钥值：{payload[:200]}"
        # 界面读的就是这份载荷；载荷干净 + 弹窗只排 `text:`，屏幕上就不可能有值。
        assert "api_key" not in payload, f"提示里出现了 llm.api_key 这个键名：{payload[:200]}"
    finally:
        svc.close()

    cfg = wb.WorkbenchConfig.load()
    notice = _notice_of(cfg)
    assert notice, "前置：提示必须还在（确认之前）"
    assert SENTINEL_KEY not in json.dumps(notice, ensure_ascii=False)
    assert SENTINEL_KEY in homes["new_config"].read_text(encoding="utf-8"), (
        "搬家把用户自己的 api_key 弄丢了 —— 提示只该不带别人的**值**，配置必须照搬")
    assert SENTINEL_KEY in homes["new_ext"].read_text(encoding="utf-8"), (
        "sidecar 丢了用户自己的密钥 —— 搬家该搬的值没搬过去")

    logs = sorted((cache.parent / "logs").glob("*.log"))
    assert logs, f"日志没落到 Temp（夹具的 cache_root 没生效？）：{cache.parent}"
    lines = [ln for log in logs for ln in
             log.read_text(encoding="utf-8", errors="replace").splitlines()
             if "搬到新家" in ln or "不认识的键" in ln]
    assert lines, "那两句搬家日志没落盘：M6 的日志腿无事可查（它同时是 M7 的锚点）"
    for line in lines:
        assert SENTINEL_KEY not in line, f"日志里印出了密钥值：{line}"


# ------------------------------------------------------------------ M7


def test_the_legacy_files_are_read_but_never_written(homes):
    """M7：**零写入**，不是零访问。

    懒路径在未确认期间每次 `load()` 都会读一次旧家 yaml —— 那是有意的（`:244-246`），
    点过「知道了」之后连读都不读（`:236-238`）。所以这条只钉写入：
    旧家 `config.yaml` + `workbench.json` 的 sha256 在一整轮「load → 提示 → 确认 → 保存」
    之后必须一个字节都没变。旧家归 bili-scribe，那是合同里唯一不许 WB 越界的地方。
    """
    _legacy_yaml(homes["legacy"], dirty=True)
    (homes["legacy"] / "workbench.json").write_text(
        json.dumps({"llm": {"providers": []}}), encoding="utf-8")
    before = {p: _sha(p) for p in (homes["legacy_config"], homes["legacy_ext"])}

    cfg = _load_default()
    assert _notice_of(cfg)
    cfg.save()
    from video_workbench.app.service import WorkbenchService

    svc = WorkbenchService(config_path=None)
    try:
        svc.dispatch("app.migration_notice", {}, "t30-7")
        svc.dispatch("app.migration_notice.dismiss", {}, "t30-8")
    finally:
        svc.close()
    _load_default()

    after = {p: _sha(p) for p in before}
    assert after == before, f"写到别人家去了：{ {k: (before[k], after[k]) for k in before if before[k] != after[k]} }"


def test_the_notice_text_says_what_the_code_actually_did(homes):
    """M5 的源码腿：弹窗那段话里的每个断言都要有出处，反向词一个都不许出现。

    正向（必须说出来）：搬到哪、旧家**没改动**、具体键名、以及「要清得你自己去 bili-scribe 那边」。
    反向（出现就红）：「已清理」「已修复」「已同步」「自动帮你」—— 这类揽功的话会让用户
    直接去开 bili-scribe，撞上同一句报错还不知道找谁。反向比正向重要：这条锁是整个任务的理由。
    页面那一半在 M9/M5 的用例里，用真弹窗文本再断一次（这里只钉源码，红得快、不依赖浏览器）。
    """
    text = (WEB_DIR / "js" / "app.js").read_text(encoding="utf-8")
    start = text.index("function maybeMigrationNotice")
    body = text[start:start + 3000]
    for phrase in ("我没有改动", "不认识的", "WB 不会替你删", "只列键名"):
        assert phrase in body, f"弹窗文案丢了「{phrase}」这一段，用户就只剩一句没信息量的提示"
    for lie in ("已清理", "已修复", "已同步", "自动帮你", "已帮你删"):
        assert lie not in body, f"文案里出现了「{lie}」：旧家其实没被改过，这话是假的"


# ------------------------------------------------------- 页面侧（M5/M8/M9/M10）

BRIDGE_INIT = """
window.pywebview = window.pywebview || {};
window.pywebview.api = window.pywebview.api || {};
window.pywebview.api.rpc = function (method, params, requestId) {
  return window.__wbRpc(method, params, requestId);
};
"""
# 上一版这里写的是 `window.pywebview = window.pywebview.api ? window.pywebview : {};` ——
# 页面刚起来时 `window.pywebview` 是 undefined，`.api` 直接 TypeError，整段 init script
# 中断，于是 bridgeReady() 永远 false、boot 等满 8 秒降级成演示态，
# 用例看到的「一次 RPC 都没打」其实是**我的桥没挂上**，不是产品没调用。
# 探针实测（挂对之后）：bridgeReady=True、isMock=False、calls 里有 app.migration_notice、
# 搬家面板 1 个、`.modal img` 0 个、无原生对话框。


def _plain_url(web_base_url: str) -> str:
    """把 `web_base_url` 里的 `?mock=1#/settings?...` 整段摘掉，只留 origin + `/index.html`。

    那个夹具给的是**完整页面 URL**（既带演示态参数又带路由哈希）。搬家提示第一行就是
    `if (WB.isMock()) return false`，照抄那个 URL 等于让被测代码当场跳过自己，
    下面四条绿灯全是假的。上一轮我把 `/index.html?mock=1` 直接接在它后面，
    结果页面地址变成 `#/settings?section=llm/index.html?mock=1` —— 哈希后面拼路径
    不是导航，所以桥和弹窗都没起来。**要测真桥就必须自己把 query 与 hash 去掉。**
    """
    from urllib.parse import urlsplit

    parts = urlsplit(web_base_url)
    assert parts.query and "mock=1" in parts.query, (
        f"夹具的 URL 形状变了，先看清楚再改这里：{web_base_url}")
    origin = f"{parts.scheme}://{parts.netloc}"
    return f"{origin}/index.html"


@pytest.fixture
def desktop_page(homes, browser, web_base_url):
    """真桥起一次整站（不是设置页），返回 (page, errors, svc, homes, dialogs, calls)。

    与 `desktop.py:142` 的 `bridge.rpc = self._service.dispatch(...)` 同形，产品 CSP
    一字未动。等待条件用**桥的调用记录**而不是 sleep，也不用 `.nav__item` 那种
    盘上不存在的选择器（上一轮就是卡在这儿白跑一次）：`calls` 里出现
    `app.migration_notice` 就说明 boot 真的跑到了那一步。
    """
    pytest.importorskip(
        "playwright.sync_api",
        reason="界面验收没跑（不是通过，是没跑）：装一下 `pip install playwright==1.64.1` "
               "并确认盘上有 ms-playwright/chromium-1248",
    )
    from video_workbench.app.service import WorkbenchService

    # 恶意键名只有 M10 那条需要；它同时检验「弹窗清单里多一个怪键名」不影响别的判定。
    _legacy_yaml(homes["legacy"], dirty=True, extra_lines=(f"\"{EVIL_KEY}\": 1",))
    errors: list[str] = []
    dialogs: list[str] = []
    calls: list[str] = []
    # 先搬家、再关掉「第一次打开」那张体检卡，最后才起服务：
    # 服务构造时会 load 一次配置，顺序反了弹窗就叠两张（见 `_mark_onboarded` 的说明）。
    _mark_onboarded()
    svc = WorkbenchService(config_path=None)

    def _bridge(source, method, params, request_id):      # noqa: ARG001
        calls.append(str(method))
        return svc.dispatch(method, params or {}, request_id or "t30")

    page = browser.new_page(viewport={"width": 1384, "height": 797})
    # favicon 那条 404 是静态服务器的噪音，不是页面的错；其它 error 一律算失败。
    page.on("console", lambda m: errors.append(f"console:{m.type}:{m.text}")
            if m.type == "error" and "favicon" not in (m.text or "") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.expose_binding("__wbRpc", _bridge)
    page.add_init_script(BRIDGE_INIT)
    url = _plain_url(web_base_url)
    try:
        page.goto(url, wait_until="load")
        page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'",
                               timeout=15000)
        _wait_until(lambda: "app.migration_notice" in calls,
                    f"boot 没打 app.migration_notice（桥/演示态不对？）URL={url} calls={calls}",
                    page)
        yield page, errors, svc, homes, dialogs, calls
    finally:
        page.close()
        svc.close()


def _wait_until(predicate, message: str, page) -> None:
    """有上限地等一个 Python 侧条件（每 50ms 让页面跑一点事，别把浏览器挂在那儿）。"""
    for _ in range(200):
        if predicate():
            return
        page.wait_for_timeout(50)
    pytest.fail(message)


def _notice_panel(page):
    """按标题定位搬家那条模态，不用 `.first`。

    `ui.modal` 的面板选择器是 `.modal .modal__panel`（`components/ui.js:145`），
    而启动路径上还有「第一次打开」那张卡也用同一个类。上一轮 `.first` 拿到的是体检卡，
    文案怎么比都不对 —— 界面用例按**内容**定位，别按顺序赌。
    """
    return page.locator(".modal__panel", has_text="配置搬家了")


def test_the_boot_shows_the_notice_once_and_never_again(desktop_page):
    """M5 + M9 的「按钮那条路」合成一条真弹窗断言（用户实际看见的东西）。

    正向文案在**渲染之后**再断一次 —— 源码里有那句话不等于屏幕上出现了那句话。
    """
    page, errors, _svc, hm, dialogs, _calls = desktop_page
    modal = _notice_panel(page)
    assert modal.count() == 1, f"启动后应当有一条搬家提示。控制台={errors}"
    text = modal.inner_text()
    assert str(hm["new_config"]) in text, text[:300]
    assert "我没有改动" in text and "bili-scribe" in text, text[:300]
    for key in _fore(hm["legacy_config"]):
        assert key in text, f"键名 {key} 没出现在弹窗里：{text[:400]}"
    assert SENTINEL_KEY not in text, "屏幕上印出了旧家的密钥值"
    for lie in ("已清理", "已修复", "已同步", "自动帮你"):
        assert lie not in text, f"文案在说谎：{lie}"
    assert dialogs == [], f"页面弹了原生对话框：{dialogs}"
    ACCEPT_DIR.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(ACCEPT_DIR / "wb_t30_notice_shown.png"))

    page.get_by_role("button", name="知道了").click()
    _wait_until(lambda: _notice_panel(page).count() == 0, "点了「知道了」弹窗还在", page)
    notice = json.loads(hm["new_ext"].read_text(encoding="utf-8"))["ui"]["migration_notice"]
    assert notice["dismissed"] is True, notice
    assert notice["keys"] == _fore(hm["legacy_config"]), "确认把事实也删了"

    page.reload(wait_until="load")
    page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'",
                           timeout=15000)
    _wait_until(lambda: "app.migration_notice" in _calls or _notice_panel(page).count(),
                "重载后 boot 什么都没打，下面的「不再弹」是空话", page)
    assert _notice_panel(page).count() == 0, "重载之后又弹了一次"
    assert errors == [], f"控制台有 error：{errors}"


@pytest.mark.parametrize("way", ["esc", "mask", "x"])
def test_every_close_path_records_the_dismissal(desktop_page, way):
    """M9：四条关闭路径（知道了 / ESC / 点遮罩 / ×）**每条都要验满三件事**。

    弹窗消失 + 后端 `dismissed` 落盘 + 重新起一次页面不再出现。
    只验按钮是半覆盖：`ui.modal` 的 `onClose` 在四种关法里都该跑且只跑一次
    （`components/ui.js:128-137` 的 `closed` 旗标），少一条腿就是「看着都关了，
    其实有的路径没记账」。按钮那条路在上一条用例里，四条合起来才是全覆盖。
    """
    page, errors, _svc, hm, _dialogs, calls = desktop_page
    assert _notice_panel(page).count() == 1, "前置：弹窗没出现"

    if way == "esc":
        page.keyboard.press("Escape")
    elif way == "mask":
        # 点遮罩本体：面板是居中的，左上角 8px 处一定是 `.modal` 而不是 `.modal__panel`
        page.locator(".modal").click(position={"x": 8, "y": 8})
    else:
        page.locator(".modal__x").click()

    _wait_until(lambda: _notice_panel(page).count() == 0, f"{way} 关不掉弹窗", page)
    _wait_until(lambda: "app.migration_notice.dismiss" in calls,
                f"{way} 这条路径根本没通知后端", page)
    notice = json.loads(hm["new_ext"].read_text(encoding="utf-8"))["ui"]["migration_notice"]
    assert notice["dismissed"] is True, f"{way} 没把确认记进后端：{notice}"

    # 重新起一次页面：只 reload 不够，得确认 boot 真去打过后端接口，否则「不再弹」是空话。
    # 这里刻意用 `reload()` 而不是 `goto(page.url)`：URL 带 `#/library` 片段，
    # goto 到同一个带片段的地址只做片段跳转、**不重新加载文档**，boot 根本没再跑，
    # 于是「没打接口」看起来像产品漏调用，其实是我自己没导航成功。
    calls.clear()
    page.reload(wait_until="load")
    page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'",
                           timeout=15000)
    _wait_until(lambda: "app.migration_notice" in calls,
                f"{way} 之后重载没打接口，第三件事没测到：calls={calls}", page)
    assert _notice_panel(page).count() == 0, f"{way} 之后重载又弹了"
    assert errors == [], f"{way}：控制台有 error：{errors}"


def test_a_key_name_that_looks_like_markup_is_rendered_as_text(desktop_page):
    """M10：旧家里那个「键名」是用户自己写出来的字符串，必须只当文本渲染。

    现场：顶层键名 `<img src=x onerror=alert(1)>` —— bili 不认识它 ⇒ `foreign_keys()`
    原样返回 ⇒ 弹窗把它列进清单。断言三件事：屏幕上出现的是这段**文字**；没有注入出
    `img` 元素；没有触发原生对话框。这条同时钉住 `ui.js:4-6` 那句「一律不用 innerHTML
    拼业务数据」—— 源码级那条在下面，页面级这条才是用户的安全边界。
    """
    page, errors, _svc, hm, dialogs, _calls = desktop_page
    assert EVIL_KEY in _fore(hm["legacy_config"]), f"夹具没造出这个键名：{_fore(hm['legacy_config'])}"
    modal = _notice_panel(page)
    assert modal.count() == 1, "带恶意键名的旧家应当照样弹一次事实"
    text = modal.inner_text()
    assert EVIL_KEY in text, "键名没被当成文字列出来"
    assert page.locator(".modal img").count() == 0, "键名被当 HTML 插进了弹窗"
    assert dialogs == [], f"注入成功，弹了原生对话框：{dialogs}"
    assert errors == [], errors


def test_the_retired_control_never_enters_the_markup_builder():
    """M10 的源码腿：弹窗那几行只许走 `text:`，不许出现 innerHTML 拼接。

    页面级那条要用真浏览器；这条不用，谁把业务数据塞进 innerHTML 它就红，
    跑得快，适合当默认门禁。
    """
    text = (WEB_DIR / "js" / "app.js").read_text(encoding="utf-8")
    start = text.index("function maybeMigrationNotice")
    body = text[start:start + 3000]
    assert "innerHTML" not in body, "搬家提示的文案改成 innerHTML 拼了：键名是用户写的字符串"


def test_demo_mode_page_shows_no_modal_even_with_a_dirty_legacy_home(
        homes, browser, web_base_url):
    """M8 的页面腿：演示态（`?mock=1`）绝不弹这条 —— 演示机没有真旧家。

    这条必须真开一次浏览器：只看源码里 `WB.isMock()` 那行，测不到「启动路径上还有
    没有别的地方绕过它直接弹窗」。上一轮我用 `f"{web_base_url}/index.html?mock=1"`
    拼 URL，而那个夹具给的**本来就是完整 URL**，拼出来的地址落在哈希里、根本没导航，
    于是「没弹窗」是白赚的 —— 现在直接用夹具那个 URL。
    """
    pytest.importorskip(
        "playwright.sync_api",
        reason="界面验收没跑（不是通过，是没跑）：装一下 `pip install playwright==1.64.1` "
               "并确认盘上有 ms-playwright/chromium-1248",
    )
    _legacy_yaml(homes["legacy"], dirty=True, extra_lines=(f"\"{EVIL_KEY}\": 1",))
    assert _fore(homes["legacy_config"]), "夹具没造出脏旧家，这条不测任何东西"
    page = browser.new_page(viewport={"width": 1384, "height": 797})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))
    try:
        page.goto(web_base_url, wait_until="load")
        page.wait_for_function("() => window.WB && typeof window.WB.rpc === 'function'",
                               timeout=15000)
        assert page.evaluate("() => WB.isMock()"), "夹具没把页面带进演示态，这条不测任何东西"
        page.evaluate("() => window.WB.router.go('settings', {section: 'llm'})")
        page.wait_for_selector("label.field__label", timeout=15000)
        page.wait_for_timeout(600)                # 让 boot 末尾那条 .catch 链跑完
        assert _notice_panel(page).count() == 0, "演示态弹了搬家提示"
    finally:
        page.close()
    assert errors == [], errors


def test_the_mock_carries_an_empty_shape_and_never_a_fabricated_notice():
    """M8 的源码腿：`mock.js` 对这两个方法只能给**空形状**，不许编一份真清单。

    我上一轮把这条写成「`mock.js` 里不许出现 `migration_notice` 字面量」—— 那是读合同
    读过头：vendor 的实现给了 `ok({ notice: null, dismissed: false })`，属于「没有提示」
    这个形状，不是把判定结果抄进假数据。真正要防的是**另一件事**：mock 给出一条带 keys
    的假搬家记录，那样演示态与真后端就会各说一套话，而且演示机上永远复现不了真问题。
    所以这条钉的是「mock 里的 notice 必须是 null」，不是「mock 里不许有这个方法」。
    前端那行 `WB.isMock()` 判断也一起钉住（两道闸说的是同一件事，任何一道被拆都要红）。
    """
    app_text = (WEB_DIR / "js" / "app.js").read_text(encoding="utf-8")
    start = app_text.index("function maybeMigrationNotice")
    assert "WB.isMock()" in app_text[start:start + 320], "演示态那行判断没了"
    mock_text = (WEB_DIR / "js" / "mock.js").read_text(encoding="utf-8")
    at = mock_text.index('"app.migration_notice"')
    window = mock_text[at:at + 260]
    assert "notice: null" in window, (
        f"mock 对 app.migration_notice 不再给空形状，而是在编搬家记录：{window[:200]}")
    assert "keys" not in window, f"mock 里编造了一份搬家键名清单：{window[:200]}"
