"""退役角色的**两条路径**回归（task-7）。

合同来自 task-6 描述里的 ``resolve_role(explicit, fallback) -> (role_key, notice)``，
四条语义加一条分类规则。写的时候**只照合同断言，不照当时 `service.py` 的写法断言**——
当时那个写法本身就是这次的缺陷，跟着它写就等于把 bug 固化成合同。

## 两条路径为什么必须分开

* **配置/历史来源的退役值 → 回落默认角色 + 写日志。** 用户从没选过它：老配置文件里
  写着 `role: standard`、或者照 `config.wb.example.yaml` 抄的，第一次点「开始总结」
  就该出结果，而不是被一句「已停用」挡在门外。
* **显式提交的退役值 → 硬拒。** 显式提交＝意图，悄悄改写就是把用户的选择吃掉。
  文案必须是权威层原话，不能再由 service 自己拼一句「没有这个角色：」。

分类规则（合同原话，别自己发明）：**只有这次刚提交的值算 `explicit`**。
`payload.role`（RPC 入参）＝ explicit；`cfg.llm.role`、历史版本的 role、
job 重放时从已落库 payload 读出的 role ＝ fallback。

## 这批用例红过多久、又为什么必须留着

写这个文件的过程中跑了两次，两次都留了记录：

* **13:34（task-6 未落地）：8 红 / 8 绿。** 三条断掉的路实测如下——
  1. `summary.start`（不传 role、配置写着 standard）→ `VALIDATION 没有这个角色：standard`
     （原 `service.py:2548` 的预校验排在兜底前面，把兜底整个吞掉）。
  2. `_summary_job` 把解析出来的值当 **explicit** 传给 `summarizer.run(role=...)`，
     于是适配器里 `core/summarizer.py:627-638` 那条兜底**从服务层根本走不到**——
     它的判据是 `if role or key not in RETIRED_ROLES: raise`，见 role 非空就抛。
  3. `summary.roles` 的 `default` 直接回 `self.cfg.llm.role`，脏配置下就是 `standard`，
     前端下拉默认选中一个「一提交就被拒」的项。
* **写完复查时（task-6 已落地）：17 全绿。** 六处手抄收敛成 `summarizer.resolve_role(...)`
  （现在位于 :2492 / :2560 / :2625 / :2770 / :2831 / :4934）。

所以这批用例的作用不再是「催一个还没做的功能」，而是**把这三条已经断过的路焊住**。
它们没有一个看起来像 bug：预校验像是负责，传参像是复用，直接回配置像是忠实。
断点全在「谁来决定优先级」这一件事被抄了六遍之后各自长出的小逻辑上。

## 这些用例不是永远绿的摆设（怎么复查）

13:34 那次红是**真实缺陷跑出来的**，不是构造的；落地之后我又用两种模拟复查了一遍有效性
（探针跑完即删，方法留在这里，谁都能重做）：

* 把 `resolve_role` 换回「`or` 链取值 + 不在清单就报 `没有这个角色：X`」的旧等价行为 →
  上面 5 条行为用例**全部失败**（配置退役值、作业回落、job 重放、显式硬拒文案、历史行改稿）；
* 把 `summary_roles` 换回原写法 `self.cfg.llm.role or summarizer.DEFAULT_ROLE` →
  前端默认值那条**失败**（这一条用前一种模拟盖不住，因为现在 `summary_roles` 外面还包了
  一层 try/except，见 `service.py:2491-2494`）；
* 两条反面对照在旧行为下**仍然通过**：干净配置不该出现退役说明、未知脏值照样被拒——
  它们本来就不依赖兜底。旧行为下也红的对照，说明它在测兜底而不是测自己的前提。

形状检查（文件末尾两条）另有一层永久自证：`PRE_FIX_LINES` 里存着 task-6 前那六行的原文，
检测器必须逐条认出来，否则这条探针自己就红。

## 一条曾经有两种读法的合同，现在有了实现背书

「job 重放时从已落库 payload 读出的 role ＝ fallback」（见
`test_replayed_job_role_is_a_fallback_not_an_explicit_choice`）我按合同字面写死了。
vendor 的落地方式与此一致：`:2831` / `:4934` 都写成 `resolve_role("", payload.get("role")
or self.cfg.llm.role)`——已落库的值进 fallback 位，显式位永远是空。
这条留着的作用是防止以后有人「顺手」把 job 侧改成 `resolve_role(payload["role"], ...)`，
那一步改动会让所有停在队列里的老任务在重放时集体报错。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from test_summary import FakeCtx, StubClient, configure, err, make_project, rpc

SERVICE_PY = Path(__file__).resolve().parents[2] / "video_workbench" / "app" / "service.py"

RETIRED = "standard"
AUTHORITY_WORDING = "标准（standard）角色已停用，请选一个方法论角色"
DEFAULT_KEY = "wengao"

#: 这批断言全部按 task-6 的 `resolve_role` 合同写。它们红的时候不要放宽——
#: 先看下面三条历史断点，对得上就是某条路又断了（文件头有它们的实测复现步骤）。
CONTRACT_GUARD = (
    "这条按 task-6 的 resolve_role 合同断言，不是照实现抄的答案。历史断点三条："
    "① 入口预校验抢在兜底前面；② job 把已落库的 role 放进显式位；"
    "③ roles_view 的 default 直接回配置值。红＝其中一条复活了，去 `service.py` "
    "取角色的地方找，别改这里的期望值。"
)


# -------------------------------------------------------------------------- 辅助


def _stub_payload(**over) -> dict:
    """三轮回角色需要 3 次回复；这里给一次就够的单轮回色，避免脚本用完。"""
    base = {"one_line": "一句话", "summary_md": "## 正文", "key_points": ["要点"],
            "timeline": [], "caution": "", "comment_short": "短评", "revise_note": ""}
    return {**base, **over}


def _use_dirty_role(service, monkeypatch, value: str) -> StubClient:
    """把配置里的默认角色改成 value（等价于用户配置文件里写着这一行）。

    改的是**运行期配置对象的值**，不是 `video_workbench/config.py` 的默认值——后者是
    产品码，不归测试动。真实用户手上那份 `config.yaml` 里就是可能有 `role: standard`。
    """
    client = StubClient([json.dumps(_stub_payload())] * 3)
    configure(service, monkeypatch, client)
    service.cfg.llm.role = value
    return client


def _submitted_payload(service, monkeypatch, params: dict) -> dict:
    """调 `summary.start` 并把投给 job 的 payload 截下来（不起线程）。"""
    seen: dict = {}
    monkeypatch.setattr(
        service.jobs, "submit",
        lambda job_type, project_id=None, payload=None: (
            seen.update(payload or {}) or {"id": "job_s"}))
    data = rpc(service, "summary.start", params)
    return {**seen, "_return": data}


# ================================================ 合同第 3 行：两个都空 → 默认，无通知


def test_clean_config_submits_the_default_role_without_a_notice(service, tmp_path, monkeypatch):
    """什么都没配、也没人选角色 → 默认角色，且**不该**出现「已停用」那类说明。

    这条现在就是绿的，它守的是反方向：兜底逻辑不能被写成「只要没传 role 就念一句
    已停用」。用户没做过任何错事时，日志里不该有道歉。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, "")
    submitted = _submitted_payload(service, monkeypatch, {"project_id": project["id"]})

    assert submitted["role"] == DEFAULT_KEY
    assert submitted["_return"]["role"] == DEFAULT_KEY

    ctx = FakeCtx()
    result = service._summary_job(ctx, {"project_id": project["id"], "kind": "fresh"})
    assert result["role"] == DEFAULT_KEY
    joined = "\n".join(ctx.logs)
    assert "已停用" not in joined, (
        f"没人提交过退役角色，日志里不该出现退役说明，实际 logs={ctx.logs}")


# ==================================== 合同第 3 行：fallback 里的退役值 → 回落 + 说明


def test_retired_value_in_config_falls_back_at_the_service_entry(service, tmp_path, monkeypatch):
    """配置写着退役值、调用方没传 role → 不许报 VALIDATION，实投的 role 必须是默认角色。

    钉的是 `service.py:2548` 那条预校验：它排在兜底前面，所以今天这条路一进 RPC 就死。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, RETIRED)

    # 现在红在这里：dispatch 返回 ok=False，错误码 VALIDATION、文案「没有这个角色：standard」
    submitted = _submitted_payload(service, monkeypatch, {"project_id": project["id"]})

    assert submitted.get("role") == DEFAULT_KEY, (
        f"{CONTRACT_GUARD}\n配置里的 {RETIRED!r} 属于 fallback，退役值应回落成 "
        f"{DEFAULT_KEY!r}，实际投出去的是 {submitted.get('role')!r}")


def test_retired_value_in_config_still_produces_a_summary_and_logs_why(service, tmp_path,
                                                                      monkeypatch):
    """同一条回落必须在**任务侧**也成立，并把原因写进用户看得见的任务日志。

    分两层断言，因为这两层今天各断一次：
      · 作业真的跑完并入库，入库行的 role == 默认角色（用户拿到的东西）；
      · 日志里同时出现「已停用」与「默认角色」——只回落到 wengao 却不交代原因，
        用户会以为自己选的就是文稿，事后对不上账。

    现在红在第 2 层之前：`_summary_job` 把解析出来的值当 explicit 传进适配器，
    适配器里 `if role or key not in RETIRED_ROLES: raise` 见 role 非空就抛，
    所以 vendor 写的那条兜底从服务层**完全走不到**。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, RETIRED)
    ctx = FakeCtx()

    result = service._summary_job(ctx, {"project_id": project["id"], "kind": "fresh"})

    assert result["role"] == DEFAULT_KEY, (
        f"{CONTRACT_GUARD}\n作业侧也没回落，实际 role={result['role']!r}")
    stored = service.repo.summary_latest(project["id"])
    assert stored["role"] == DEFAULT_KEY, f"入库行的 role 要跟着实际用的角色走：{stored}"

    joined = "\n".join(ctx.logs)
    assert "已停用" in joined, f"任务日志必须交代为什么换了角色，实际 logs={ctx.logs}"
    assert "默认角色" in joined, f"任务日志要点名回落到了默认角色，实际 logs={ctx.logs}"


def test_replayed_job_role_is_a_fallback_not_an_explicit_choice(service, tmp_path, monkeypatch):
    """job 重放：已落库 payload 里带着退役值 → 算 fallback，回落 + 说明，不许硬拒。

    这是合同分类规则里最容易读成两种的一行，我按字面写死了（理由见文件头最后一节）。
    场景是真实的：task-6 之前提交的作业停在队列里，退役之后重放；或者用户重跑一条
    老任务。那句 role 不是这次有人选的，拿它拒绝用户等于用历史数据惩罚现在的人。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, "")      # 配置是干净的，脏值只来自已落库 payload
    ctx = FakeCtx()

    result = service._summary_job(ctx, {"project_id": project["id"], "kind": "fresh",
                                        "role": RETIRED})

    assert result["role"] == DEFAULT_KEY, (
        f"{CONTRACT_GUARD}\n从已落库 payload 读出的 {RETIRED!r} 按合同属于 fallback，"
        f"应回落成 {DEFAULT_KEY!r}，实际 role={result['role']!r}。"
        "若 vendor 取另一种读法，请改 task-6 的描述而不是改这条用例。")
    assert "已停用" in "\n".join(ctx.logs), f"日志要交代原因，实际 logs={ctx.logs}"


# ================================== 合同第 4 行：explicit 的退役值 → 硬拒 + 权威文案


def test_explicit_retired_role_is_rejected_with_the_authority_wording(service, tmp_path,
                                                                     monkeypatch):
    """显式提交 `role="standard"` → `VALIDATION`，且文案必须是权威层原话。

    两个断言各有分工，缺一个就漏一半：
      · code：退役角色不能生成总结（这条今天就对）；
      · **消息逐字等于**权威层那句（这条今天红）：service 里自己拼的
        「没有这个角色：standard」把「已停用」和「该选什么」两个信息都丢了，
        用户读完不知道下一步。同时这也封掉错误文案分叉——以后谁改措辞，
        得同时改权威层与这条用例，改不动就暴露。

    不许把它断成「消息里包含 standard 就行」，那是放宽。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, "")

    error = err(service, "summary.start",
                {"project_id": project["id"], "role": RETIRED})

    assert error["code"] == "VALIDATION"
    assert error["message"] == AUTHORITY_WORDING, (
        f"{CONTRACT_GUARD}\n显式提交的退役值要由 resolve_role 抛权威层原话，"
        f"实际文案是 {error['message']!r}（service 自己拼的那句没有信息量）。")


def test_explicit_retired_role_is_rejected_by_the_adapter_too(service, tmp_path, monkeypatch):
    """同一条硬拒在适配层入口也要成立——不能只在 RPC 参数校验里拦。

    老脚本 / 别的调用方会直接 `summarizer.run(role=...)` 绕过 RPC。这条今天就是绿的
    （`test_summary_drift.py` 也有一条），放在这里是为了让「explicit＝硬拒」这条合同
    在两个入口都有人守，而不是只守用户界面那一个门。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer

    client = _use_dirty_role(service, monkeypatch, "")
    with pytest.raises(WBError) as exc:
        summarizer.run({"title": "t"}, [{"start_ms": 0, "text": "内容"}],
                       client=client, cfg=service.cfg, ctx=FakeCtx(), role=RETIRED)
    assert exc.value.code == "VALIDATION"
    assert str(exc.value.message) == AUTHORITY_WORDING


# ============================== 合同第 4 行后半段：未知值不算退役，不许静默修正


def test_unknown_value_in_config_is_still_rejected(service, tmp_path, monkeypatch):
    """配置里写错角色名 → 仍然拒。

    这条**现在就是绿的，而且必须一直绿**：它守的是回落与掩盖的界线。把
    `role: xueshuu` 这种拼写错误悄悄修正成默认角色，用户永远不知道自己配的
    东西没生效——回落只能给「曾经合法、如今退役」的值，不能给「从来不存在」的值。

    只锁错误码 + 「报出脏值本身」，不锁整句措辞：合同没有钉未知值的文案，
    钉了就变成照实现猜。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, "ghost")

    error = err(service, "summary.start", {"project_id": project["id"]})

    assert error["code"] == "VALIDATION", (
        f"配置里的未知角色名不能被静默修正成 {DEFAULT_KEY}，实际 error={error}")
    assert "ghost" in error["message"], (
        f"报错必须把那个脏值本身念出来，否则用户找不到是哪一行配置错了：{error['message']!r}")


# ============================== 合同第 3 行：resolve_role 本体的四行语义（直接单元）


RESOLVE_ROWS = (
    pytest.param("", "", (DEFAULT_KEY, ""), id="both-empty"),
    pytest.param("fenxi", "", ("fenxi", ""), id="explicit-usable"),
    pytest.param("", "fenxi", ("fenxi", ""), id="fallback-usable"),
    pytest.param("", RETIRED, "FALLBACK", id="fallback-retired"),
)


@pytest.mark.parametrize("explicit,fallback,expected", RESOLVE_ROWS)
def test_resolve_role_matches_the_contract_table(explicit, fallback, expected):
    """`resolve_role` 的四行语义直接单元对账（函数不存在即红，报错点名 task-6）。

    前三行是纯返回值；第四行（fallback 里的退役值）合同只保证两件事：结果是
    默认角色、并且**返回一个非空中文说明**——说明的具体措辞留给调用方去拼日志，
    这里不锁字，锁字就成了照着实现抄答案。
    """
    from video_workbench.core import summarizer

    resolve = getattr(summarizer, "resolve_role", None)
    if resolve is None:
        pytest.fail(f"{CONTRACT_GUARD}\n`video_workbench.core.summarizer.resolve_role` 还不存在。")

    key, notice = resolve(explicit, fallback)

    if expected == "FALLBACK":
        assert key == DEFAULT_KEY
        assert isinstance(notice, str) and notice.strip(), (
            "回落到默认角色必须带回一句给人看的说明，否则日志里只能看到角色被换了")
        assert "停用" in notice or "默认角色" in notice, f"说明要交代原因，实际 {notice!r}"
        return

    assert (key, notice) == expected


def test_resolve_role_rejects_unknown_and_explicit_retired():
    """`resolve_role` 的拒绝分支：显式退役值与任何未知值都抛 `WBError(VALIDATION)`。

    合同把「未知」和「退役」分开：前者从来不存在，后者被显式提交时同样要拒。
    只有「退役 + 来自 fallback」这一种组合才允许回落——所以这里要同时钉住三个反例。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer

    resolve = getattr(summarizer, "resolve_role", None)
    if resolve is None:
        pytest.fail(f"{CONTRACT_GUARD}\n`resolve_role` 还不存在。")

    for explicit, fallback in ((RETIRED, ""), (RETIRED, DEFAULT_KEY),
                               ("ghost", ""), ("", "ghost"), ("ghost", RETIRED)):
        with pytest.raises(WBError) as exc:
            resolve(explicit, fallback)
        assert exc.value.code == "VALIDATION", (
            f"explicit={explicit!r} fallback={fallback!r} 的错误码不对：{exc.value.code}")

    # 反例的反面：唯一允许回落的那种组合。
    key, notice = resolve("", RETIRED)
    assert key == DEFAULT_KEY and notice


# ============================================ 合同第 3 行：前端默认值必须选得到


def test_roles_rpc_default_is_selectable_even_with_a_dirty_config(service, tmp_path, monkeypatch):
    """`summary.roles` 的 default 不能是退役角色——脏配置下也一样。

    `service.py:2489` 现在直接把 `self.cfg.llm.role` 吐出去。配置文件写着 standard 时，
    前端下拉会默认选中一个「一提交就被拒」的项：用户什么也没做错，界面先骗了他一次。
    合同要求这一处也走 `resolve_role`。
    """
    _use_dirty_role(service, monkeypatch, RETIRED)

    data = rpc(service, "summary.roles")
    from video_workbench.core.summary import RETIRED_ROLES, ROLE_KEYS

    assert data["default"] not in RETIRED_ROLES, (
        f"{CONTRACT_GUARD}\n脏配置下 default 吐出了退役角色 {data['default']!r}")
    assert data["default"] in ROLE_KEYS, (
        f"default 必须在可用清单里，否则前端一进来就选中一个不可用的项：{data['default']!r}")
    assert data["default"] == DEFAULT_KEY


# ==================================== 收敛性负向检查：拦下一个照抄的人


#: 手抄的取值形状：`self.cfg.llm.role or summarizer.DEFAULT_ROLE`
#: 两个词起步就够——`:2489` 那段只写了 `(self.cfg.llm.role or summarizer.DEFAULT_ROLE)`，
#: 前面没有第三个 `or`；如果正则要求「前面还得有个 or」，就正好漏掉这一处，
#: 于是「:2489 没改」这种半成品会被上面的负向检查放过去（实测漏过，第一次跑就红了）。
#: 匹配整段源码而不是逐行——`:2612` 那处被折成两行写，按行扫也会漏。
CHAIN_RE = re.compile(r"self\.cfg\.llm\.role\s+or\s+summarizer\.DEFAULT_ROLE", re.S)


def _chain_hits(src: str) -> list[int]:
    return [src[:m.start()].count("\n") + 1 for m in CHAIN_RE.finditer(src)]


def test_service_no_longer_hand_copies_the_role_chain():
    """service.py 里不许再有手抄的取值顺序——这条是给下一个照抄的人准备的。

    同一套 `... or self.cfg.llm.role or summarizer.DEFAULT_ROLE` 在 service.py 里手抄了
    六处（:2489 / :2548 / :2612 / :2757 / :2814 / :4911）。这类重复的真实代价不是难看，
    而是**修一处漏五处**：这次退役兜底被 :2548 吞掉，正是因为其中一处后面紧跟了一段
    自己写的校验。所以这里做负向断言：形状必须消失。

    允许它作为 `resolve_role(...)` 的**实参**出现（把配置值传进去本来就得写这个名字），
    但不允许出现「用 or 串起来决定优先级」的形状——优先级是合同，只能有一个地方负责。
    """
    src = SERVICE_PY.read_text(encoding="utf-8")
    hits = _chain_hits(src)
    assert not hits, (
        f"{CONTRACT_GUARD}\nservice.py 里还剩 {len(hits)} 处手抄的取值顺序：{hits}")


#: task-6 落地**前**这六行的原样抄录（grep 于 13:29，:2612 那处占两行）。
#: 它们是检测器的考题：现在的 service.py 已经没有这些行了，所以不能靠「读真实文件
#: 数一遍」来证明检测器有效——那样这条用例要么依赖行号（一改就失真），要么永远绿。
#: 把历史原文钉在这里，检测器就得对每一个曾经存在的实例认出来。
PRE_FIX_LINES = (
    '            "default": (self.cfg.llm.role or summarizer.DEFAULT_ROLE),',
    '        picked = (role or self.cfg.llm.role or summarizer.DEFAULT_ROLE).strip()',
    '        picked = (role or base.get("role") or self.cfg.llm.role\n'
    '                  or summarizer.DEFAULT_ROLE)',
    '        picked = (role or self.cfg.llm.role or summarizer.DEFAULT_ROLE).strip()',
    '        role = str(payload.get("role") or self.cfg.llm.role or summarizer.DEFAULT_ROLE)',
    '        role = str(payload.get("role") or self.cfg.llm.role or summarizer.DEFAULT_ROLE)',
)


def test_chain_detector_recognises_every_historical_instance():
    """灵敏度自证：上面那条负向检查不是「永远绿」的摆设。

    正则写错时 `assert not hits` 会**白白通过**，收敛检查就变成装饰——这不是假想：
    第一版正则要求链首还有第三个 `or`，结果漏掉 :2489 那处两项形状，第一次跑就暴露了
    （它当时只命中 5 处）。所以两头钉：

    1. **历史原文逐条必须命中**：六条原样抄录，其中一条是折行写法——三种形状
       （两项 / 三项 / 折行）全覆盖，少认一种就是漏掉一个未来的照抄者；
    2. **合规写法必须不命中**：`resolve_role(role, self.cfg.llm.role)` 这种把配置值当
       实参传进去的写法是合同要求的形态，如果它也报警，vendor 就只能来把断言改松，
       这条检查随即失效。
    """
    for index, text in enumerate(PRE_FIX_LINES):
        assert len(_chain_hits(text)) == 1, (
            f"检测器漏认了 task-6 前的第 {index + 1} 处手抄形状：{text!r}")

    compliant = [
        'key, notice = summarizer.resolve_role(role, self.cfg.llm.role)',
        '"default": summarizer.resolve_role(None, self.cfg.llm.role)[0],',
        'role = str(payload.get("role") or summarizer.resolve_role(\n'
        '    "", str(payload.get("role") or "") or self.cfg.llm.role)[0])',
    ]
    for text in compliant:
        assert not _chain_hits(text), f"合规写法被误判成手抄：{text!r}"

    assert not _chain_hits(SERVICE_PY.read_text(encoding="utf-8")), (
        f"{CONTRACT_GUARD}\n上一条已经守住了这件事，这里重复一次是为了：当正则被改坏时，"
        "本文件不会出现「检测器失效但用例通过」的组合。")


def test_role_authority_lives_in_the_adapter_and_service_calls_it():
    """正向配对：负向的形状检查之外，得确认**真的有人负责**这件事。

    只写 `assert not hits`，把六处全删掉、换成一个从不看配置的角色常量也能通过。
    所以这里正向确认两件事：权威层有 `resolve_role`，且 service.py 至少调用它一次。

    这里刻意**不**写成「`resolve_role(` 至少出现 6 次」。合同要求六处都收敛，但如果
    vendor 抽一个 `self._role_for(...)` 包一层再让六处调它，收敛同样成立、调用计数却是 1；
    把次数写成 6 就是惩罚一种更好的实现，属于死抠内部结构。「每一处真的行为正确」由
    上面那些行为用例 + 形状检查共同负责，不由计数负责。
    """
    from video_workbench.core import summarizer

    assert hasattr(summarizer, "resolve_role"), CONTRACT_GUARD
    src = SERVICE_PY.read_text(encoding="utf-8")
    assert src.count("resolve_role(") >= 1, (
        f"{CONTRACT_GUARD}\nservice.py 一次都没调用 resolve_role，"
        "说明六处还各自在决定优先级（与形状检查互为正反：那条要求手抄消失，"
        "这条要求真有人接手）")


def test_legacy_summary_row_does_not_block_a_refine(service, tmp_path, monkeypatch):
    """上一版总结是 standard 时代存的 → 点「按要求改一版」不许把用户挡在门外。

    `:2612` 的取值顺序里 `base.get("role")` 是**历史行**，按合同属于 fallback：
    那句 role 不是这次有人选的，拿它拒绝用户等于用历史数据惩罚现在的人。
    这条也是 `test_summary_drift.py` 里「历史行仍可读」的行为版——读得到，
    而且还能在它基础上继续改。

    同一入口再钉一次反面：**这次**显式提交退役值仍然硬拒，区别只在「有没有人选」。
    """
    project = make_project(service, tmp_path)
    _use_dirty_role(service, monkeypatch, "")
    saved = service.repo.summary_insert(
        project["id"], content={"one_line": "旧版", "summary_md": "旧版正文"},
        content_md="旧版正文", role=RETIRED, model="stub-summary", usage={}, status="ready")

    submitted = _submitted_payload(
        service, monkeypatch,
        {"project_id": project["id"], "requirement": "再短一点", "summary_id": saved["id"]})

    assert submitted.get("role") == DEFAULT_KEY, (
        f"{CONTRACT_GUARD}\n历史行里的 {RETIRED!r} 按合同属于 fallback，应回落成 "
        f"{DEFAULT_KEY!r}，实际投出 {submitted.get('role')!r}")

    error = err(service, "summary.refine",
                {"project_id": project["id"], "requirement": "再短一点",
                 "summary_id": saved["id"], "role": RETIRED})
    assert error["code"] == "VALIDATION"
    assert error["message"] == AUTHORITY_WORDING, (
        f"改稿入口的文案也要走权威层，不许与生成入口各说各话：{error['message']!r}")

def test_resolve_role_never_returns_a_role_outside_the_public_list():
    """反向证据（task-46 第 2 件事）：`resolve_role` 的返回值**恒在 `ROLE_KEYS` 内**。

    为什么必须有这条：`test_the_adapter_never_hands_the_copy_a_retired_key` 钉的是
    「适配层交给副本的键不含退役键」，而我在交付里对用户说过「坏输入在真实路径上
    不可达，实际风险可能比我说的小」——那句当时只有从调用点 grep 推出来的支撑，
    没有机械证据。这条把推断换成断言：入参空间逐格验（显式退役键、配置脏值、
    空值、现役键及其组合）。

    抛 `WBError` 是**合法出口**（拒绝比越界安全）；返回 `ROLE_KEYS` 之外的值才是真缺陷
    ——那时请单独立缺陷、贴复现命令原文，不许放宽本断言。
    """
    from video_workbench.app.errors import WBError
    from video_workbench.core import summarizer
    from video_workbench.core.summary import RETIRED_ROLES as LIVE_RETIRED
    from video_workbench.core.summary import ROLE_KEYS as LIVE_PUBLIC

    resolve = getattr(summarizer, "resolve_role", None)
    if resolve is None:
        pytest.fail(f"{CONTRACT_GUARD}\n`resolve_role` 还不存在。")

    grid = [(e, f) for e in [None, "", "   ", *LIVE_PUBLIC, *sorted(LIVE_RETIRED),
                             "ghost", " wengao "]
            for f in [None, "", "   ", *sorted(LIVE_RETIRED), "ghost"]]
    assert len(grid) >= 40, f"入参空间只有 {len(grid)} 格，不足以当反向证据"

    offenders, raised = [], 0
    for explicit, fallback in grid:
        try:
            key, _notice = resolve(explicit, fallback)
        except WBError:
            raised += 1
            continue
        if key not in LIVE_PUBLIC:
            offenders.append(f"explicit={explicit!r} fallback={fallback!r} → {key!r}")

    assert not offenders, (
        "这些入参让 `resolve_role` 返回了公开名单之外的角色——「适配层永远不会把退役键"
        "交给副本」这条前提就此失效：\n  " + "\n  ".join(offenders))
    assert raised, (
        f"{len(grid)} 格入参一次都没抛——夹具多半没把退役/未知值喂进去。"
        "显式提交退役键必须硬拒（A 支），一条不抛就是入参形状错了")


def test_a_public_key_survives_resolve_role_as_itself():
    """配对腿：现役键**原样透传**，不许被换成默认值。

    只断「返回值在名单内」不够——实现若把谁都映射成默认角色，上一条照样绿。
    这条钉「显式传现役键就得拿回那把键」，两条合起来才夹住返回值。
    """
    from video_workbench.core import summarizer
    from video_workbench.core.summary import RETIRED_ROLES as LIVE_RETIRED
    from video_workbench.core.summary import ROLE_KEYS as LIVE_PUBLIC

    resolve = getattr(summarizer, "resolve_role", None)
    if resolve is None:
        pytest.fail(f"{CONTRACT_GUARD}\n`resolve_role` 还不存在。")

    dirty_fallback = sorted(LIVE_RETIRED)[0]
    for key in LIVE_PUBLIC:
        got, notice = resolve(key, dirty_fallback)
        assert got == key, f"显式传现役键 {key!r} 却被换成 {got!r}：现役键不许被覆盖"
        assert notice == "", f"干净输入不该带提示，实得 {notice!r}"
