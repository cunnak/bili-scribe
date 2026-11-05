"""用例层：把所有基础设施拼起来，对外只暴露 RPC。

这一层负责**装配顺序**和**跨模块规则**，不负责具体干活：

- 配置 → 数据库 → 仓储 → 事件 → 任务，顺序不能乱（任务要写库，库要读配置）；
- "保存设置"要同时做三件事：改内存、写文件、通知界面。少一步就是
  "看起来保存了但下次打开又回去了"或者"保存了但界面不变"。

Phase 0 只有骨架方法（信息、设置、框架、任务控制）。下载 / 转录 / 切片
等真实业务在后续阶段挂到同一个 ``jobs`` 上，不改这里的结构。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import copy
import csv
import io
import json
import tempfile
import threading
import time
from importlib.util import find_spec
from typing import Any, Callable

from pydantic import BaseModel, Field, field_validator

from video_workbench import APP_NAME, __version__
from video_workbench.app.errors import ERROR_CODES, WBError
from video_workbench.app.events import EventBus
from video_workbench.app.jobs import JobContext, JobManager
from video_workbench.app.rpc import Router
from video_workbench.config import (
    BILINGUAL_MODES,
    TRANSLATE_TARGETS,
    WorkbenchConfig,
    find_provider,
    parse_model_spec,
    resolve_task_model,
    render_yaml,
)
from video_workbench.core import resolver as resolver_mod
from video_workbench.core import prompt_source
from video_workbench.core import subtitles, summarizer, waveform
from video_workbench.core import overview as overview_core
from video_workbench.core import recovery as recovery_core
from video_workbench.core import subtitle_llm
from video_workbench.core import audio_mix, clipper, exporter, merger
from video_workbench.core import nle
from video_workbench.core import copywriter
from video_workbench.core import cover as cover_core
from video_workbench.core import reframe
from video_workbench.core import downloader as downloader_mod
from video_workbench.core.downloader import BACKENDS
from video_workbench.core import translator
from video_workbench.core import publisher as publisher_core
from video_workbench.core import styles as styles_core
from video_workbench.core import onboard as onboard_core
from video_workbench.core.transcriber import format_srt
from video_workbench.core.bili import default_gateway
from video_workbench.core import clip_refine
from video_workbench.core.importer import import_videos
from video_workbench.infra import languages
from video_workbench.core.probe import make_thumbnail, probe_file
from video_workbench.core.transcriber import make_transcribe_handler
from video_workbench.infra import models as models_mod
from video_workbench.infra import log as log_mod
from video_workbench.infra.db import open_and_migrate, schema_version
from video_workbench.infra.ffmpeg import FFmpeg, filter_path, subtitle_filter
from video_workbench.infra.files import is_within, unique_path
from video_workbench.infra.llm import ChatClient, ChatError
from video_workbench.infra.notecache import NoteCache
from video_workbench.infra.db import now_iso as db_now_iso
from video_workbench.infra.files import atomic_write_text, ensure_free_space, sanitize_name
from video_workbench.infra.repo import Repo, new_id
from video_workbench.workers.batch_worker import (
    BATCH_TYPE,
    CHILD_TYPE,
    failed_items,
    make_batch_handler,
)
from video_workbench.workers.download_worker import make_download_handler


# ============================================================ 参数模型
#
# 每个模型只声明"这一个接口需要的字段"。用 BaseModel 而不是手工 if：
# 手工判断会漏掉类型（`limit: "abc"` 会一路传到 SQL 参数里）。


class EmptyParams(BaseModel):
    pass


class SettingsPutParams(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SkinParams(BaseModel):
    skin: str


class RailParams(BaseModel):
    collapsed: bool


class DiagnosticsParams(BaseModel):
    # 空表示"按默认位置生成"（media_root/diagnostics/…zip）；
    # 给了路径就必须落在媒体/缓存/日志目录里（诊断包里有脱敏配置，别随便落盘）
    path: str = ""
    log_lines: int = 400


class OnboardDoneParams(BaseModel):
    """向导收尾：`skipped=True` 表示用户按了"先跳过"，同样算看过。"""

    skipped: bool = False


class LogTailParams(BaseModel):
    lines: int = 200


class ListParams(BaseModel):
    query: str = ""
    limit: int = 50
    offset: int = 0


class IdParams(BaseModel):
    id: str


class RevealParams(BaseModel):
    path: str


class OpenUrlParams(BaseModel):
    url: str


class DemoParams(BaseModel):
    # 只是让 P0-05 的验收可执行：一个能跑、能取消、能重试的假任务。
    seconds: float = 10.0


class ResolveParams(BaseModel):
    url: str


class DownloadParams(BaseModel):
    url: str = ""
    quality: str = "1080P"
    # 前端解析后可能让用户取消勾选某些分P，所以允许直接把 items 传回来
    items: list[dict[str, Any]] | None = None
    up_name: str = ""
    collection_title: str = ""
    # 空 = 用配置里的默认后端；"bbdown" 只在用户点「用 BBDown 重试」时出现。
    backend: str = ""


class DownloadRetryParams(BaseModel):
    id: str = ""
    backend: str = ""


class NoParams(BaseModel):
    pass


class ImportParams(BaseModel):
    paths: list[str] = Field(default_factory=list)
    recursive: bool = True


class TranscribeStartParams(BaseModel):
    # 允许一次点多个项目。串行由 jobs 的 transcribe 限流保证（显存只有一份），
    # 所以这里不需要再写调度逻辑。
    project_ids: list[str] = Field(default_factory=list)
    model: str = ""
    language: str = ""
    device: str = ""
    compute_type: str = ""
    vad: bool | None = None
    word_timestamps: bool | None = None

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        # 在这里挡，而不是在 transcribe_start 里：非法语言码一旦进了 jobs 队列，
        # 用户会看到一条 PROCESS_FAILED 的"假失败"。而真正的报错要等到 worker
        # 建 tokenizer 时才出现——那时 VAD 和模型加载的钱已经付掉了。
        return languages.normalize(value)


class ModelParams(BaseModel):
    name: str = ""


class SubtitleGetParams(BaseModel):
    project_id: str = ""


class SubtitleSaveParams(BaseModel):
    project_id: str = ""
    segments: list[dict[str, Any]] = Field(default_factory=list)


class OptimizeParams(BaseModel):
    """P2-04：一次只优化一个项目的字幕。"""

    project_id: str = ""
    batch_size: int = 30
    segment_ids: list[str] = Field(default_factory=list)


class ApplyOptimizeParams(BaseModel):
    """应用优化结果。``accepted_ids`` 为空＝全接受。"""

    project_id: str = ""
    changes: list[dict[str, Any]] = Field(default_factory=list)
    accepted_ids: list[str] = Field(default_factory=list)


class TranslateParams(BaseModel):
    """P2-05：翻译。``target`` 留空/``auto`` 由源语言决定；``segment_ids``＝只重跑这几段。"""

    project_id: str = ""
    target: str = "auto"
    layout: str = ""
    batch_size: int = 10
    segment_ids: list[str] = Field(default_factory=list)


class TranslationParams(BaseModel):
    project_id: str = ""
    target: str = ""


class ApplyTranslationParams(BaseModel):
    """人工确认后落盘。``accepted_ids`` 为空＝全部已确认的译文都写。

    ``overrides`` 是界面里改过的译文（``{段 id: 新译文}``）：方案书 5.5.4 允许
    逐条编辑，编辑必须在落盘前生效，否则"改完再确认"是假的。
    """

    project_id: str = ""
    target: str = ""
    layout: str = ""
    accepted_ids: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)


class GlossaryPutParams(BaseModel):
    entries: list[str] = Field(default_factory=list)


class GlossaryCsvParams(BaseModel):
    path: str = ""
    merge: bool = True


class GlossaryExportParams(BaseModel):
    path: str = ""


class SubtitleExportParams(BaseModel):
    """P2-06：字幕导出。

    ``mode``：``none`` 只出字幕文件；``soft`` 软字幕 mux（``mov_text``，样式不保留）；
    ``hard`` 硬字幕烧录（``ass`` 滤镜，样式保留）。

    ``height`` 留 0 ＝ 跟源视频高度；字号按 720P 基准等比缩放（方案书 5.11.2）。
    ``style_id`` 留空 ＝ 配置里的默认样式。
    """

    project_id: str = ""
    formats: list[str] = Field(default_factory=lambda: ["srt", "ass", "vtt"])
    mode: str = "none"
    height: int = 0
    style_id: str = ""
    language: str = ""


class StyleKeyParams(BaseModel):
    id: str = ""


class StyleSaveParams(BaseModel):
    """P3-07：保存一次样式编辑。

    ``id`` 留空 ＝ 新建（自动生成 ``st_`` 前缀 id）；给了 id 就是更新——内置样式
    在 repo 层会被拒（``CONFLICT 内置样式不可修改``），复制请用 ``style.import``
    或前端先改 id。
    """

    id: str = ""
    name: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class StyleFileParams(BaseModel):
    """导入/导出走文件。``path`` 留空时导出写到 ``media_root/styles/`` 下。"""

    id: str = ""
    path: str = ""


class StyleRenderParams(BaseModel):
    """真实渲染：用 ffmpeg 渲一张 PNG 检查 ASS 实际效果。

    ``config`` 优先于 ``id``——界面上改了没保存也要能试渲染。
    """

    id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class SummaryStartParams(BaseModel):
    """P2-07：生成一版总结。

    ``role`` 留空 ＝ 用配置里的默认角色（``config.llm.role``）。
    ``danmaku`` 可以带一批弹幕正文；不带就只有字幕——弹幕是"观众反应"，
    缺了不影响正文，只是模型会少一个判断观众情绪的依据。
    """

    project_id: str = ""
    role: str = ""
    danmaku: list[str] = Field(default_factory=list)
    model: str = ""


class SummaryRefineParams(BaseModel):
    """按用户的话改一版。``summary_id`` 留空 ＝ 拿最新一版当底稿。"""

    project_id: str = ""
    requirement: str = ""
    role: str = ""
    summary_id: str = ""


class SummaryRestoreParams(BaseModel):
    """把旧版恢复成新的一版（不是"回滚"，历史不删）。"""

    project_id: str = ""
    summary_id: str = ""


class PublishRegenerateParams(BaseModel):
    id: str
    part: str = "all"   # all | summary | title


class PublishPreviewParams(BaseModel):
    """预览一份发布草稿（不落库）。

    ``summary_id`` 为空时取这个项目**最新**的一版总结——"我刚出的那版"是最常见的意图，
    让界面每次先查一遍版本列表只为了拿 id，等于多一次往返还容易拿错版。
    """

    project_id: str
    summary_id: str = ""
    # 勾了"我已阅读并确认"之后要重新算一遍门禁：确认本身是内容关的输入
    confirmed: bool = False


class PublishGetParams(BaseModel):
    """读一份草稿。

    带上 ``confirmed`` 是为了让界面勾完确认框能自己把门禁刷新成"全绿"，
    不用等到点发布才发现少一步。
    """

    id: str
    confirmed: bool = False


class PublishDoParams(BaseModel):
    """发布这篇笔记。

    ``confirmed`` 必为真才会发出去——这是"不可撤销动作"的闸门，
    默认 False 让"忘了勾"变成一次可恢复的拒绝，而不是一条已经发出去的笔记。
    """

    id: str
    confirmed: bool = False


class PublishWithdrawParams(BaseModel):
    id: str


class PublishRecordsParams(BaseModel):
    limit: int = 50


class PublishNoteParams(BaseModel):
    """保存草稿。

    ``note_pk`` 是本地行 id（``draft_xxx``）；给了就更新那一行，不给就按项目找。
    标题/摘要/正文都可为空串——空表示"用总结重新生成"，不是"清空"，
    因为清空一个必填字段只可能发生在用户手滑的时候。
    """

    project_id: str
    note_pk: str = ""
    summary_id: str = ""
    title: str = ""
    summary: str = ""
    content: str = ""


class PublishListParams(BaseModel):
    project_id: str = ""


class CollectionOverviewParams(BaseModel):
    """P2-08：给一个合集出总览。

    ``collection`` 是**合集名**（``wb_project.collection_title``），不是 id——
    库里 ``wb_collection`` 直到第一次出总览才建行，让界面先拿 id 会多一次往返。
    """

    collection: str = ""
    role: str = ""
    extra: str = ""


class ClipAnalyzeParams(BaseModel):
    project_id: str = ""
    kinds: list[str] = []
    count: int = 12
    #: P6-01：要不要走画面精析。``None`` = 看配置（配了 vision 模型就开）。
    refine: bool | None = None


class ClipUpdateParams(BaseModel):
    id: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    title: str | None = None
    score: int | None = None
    status: str = ""
    # P3-04：对齐到帧栅格。留空=用户写的毫秒原样存（手动精确编辑的场景）
    align: str = ""


class ClipCopyParams(BaseModel):
    candidate_id: str
    regenerate: bool = False


class ClipCopySaveParams(BaseModel):
    candidate_id: str
    title: str = ""
    description: str = ""
    tags: list[str] = []


class ClipCoverParams(BaseModel):
    candidate_id: str
    at_ms: int = -1


class ClipCoverFramesParams(BaseModel):
    candidate_id: str
    count: int = 4


class ClipCoverUploadParams(BaseModel):
    candidate_id: str
    path: str


class ClipCreateParams(BaseModel):
    project_id: str = ""
    kind: str = "highlight"
    start_ms: int = 0
    end_ms: int = 0
    title: str = ""
    align: str = ""


class ClipExportParams(BaseModel):
    project_id: str = ""
    candidate_id: str = ""
    mode: str = "precise"
    subtitle_mode: str = "none"
    aspect: str = "16x9"
    vertical_mode: str = "center"
    crop_x: float = 0.5
    style_id: str = ""
    # P6-03：-1 / None 是「按配置来」的哨兵，前端不传就走 clip.* 的默认值
    bgm: str = ""
    bgm_volume: float = -1
    bgm_duck: bool | None = None
    fade_ms: int = -1


class ClipFocusParams(BaseModel):
    project_id: str = ""


class ClipDeliverParams(BaseModel):
    """P6-04：把一段切片交给别的软件（剪映草稿 / 达芬奇 OTIO / 降级包）。"""

    project_id: str = ""
    candidate_id: str = ""
    channel: str = "jianying"
    # 剪映：直接放进剪映的草稿箱（检测到才生效），否则放项目 export/ 下
    place_in_app: bool = False


class NleInfoParams(BaseModel):
    pass


class ClipFocusAnalyzeParams(ClipFocusParams):
    # P6-02：None = 跟随配置 clip.follow_enhance；false = 显式回退 P3-06 的基础跟随
    enhance: bool | None = None
    # 想跟哪个主体（sub_a/sub_b…），空 = 用打分最高的那个
    subject: str = ""


class ClipFocusSubjectParams(BaseModel):
    project_id: str = ""
    subject: str = ""


class ClipFocusSaveParams(BaseModel):
    project_id: str = ""
    focus: list[dict] = []
    clear: bool = False


class ClipExportsParams(BaseModel):
    project_id: str = ""


class ClipExportBatchParams(BaseModel):
    project_id: str = ""
    candidate_ids: list[str] = []
    mode: str = "precise"
    subtitle_mode: str = "none"
    aspect: str = "16x9"
    vertical_mode: str = "center"
    crop_x: float = 0.5
    style_id: str = ""
    skip_exported: bool = False
    bgm: str = ""
    bgm_volume: float = -1
    bgm_duck: bool | None = None
    fade_ms: int = -1


class MergeSourcesParams(BaseModel):
    """合并来源：``project_id`` 空表示全库都列（跨项目拼片是常态）。"""

    project_id: str = ""
    query: str = ""


class MergePreflightParams(BaseModel):
    """预检入参。``width/height/fps`` 为 0 表示跟随第一个文件。"""

    paths: list[str] = []
    width: int = 0
    height: int = 0
    fps: float = 0.0


class MergeStartParams(BaseModel):
    """合并入参。``out_dir``/``out_name`` 留空就按预检给的默认值。

    ``mode=quick`` 走 `-c copy`（不重编码，秒级完成）；``precise`` 留给 P4-03。
    """

    paths: list[str] = []
    mode: str = "quick"
    out_dir: str = ""
    out_name: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0


class PeaksParams(BaseModel):
    project_id: str = ""
    refresh: bool = False


class SubtitleOpParams(BaseModel):
    """一次编辑。``op`` 决定其余字段怎么解释，具体见 ``subtitle_op`` 的注释。"""

    project_id: str = ""
    op: str = ""
    segment_id: str = ""
    segment_ids: list[str] = Field(default_factory=list)
    at_ms: int = 0
    offset_ms: int = 0
    pattern: str = ""
    replacement: str = ""
    text: str = ""
    regex: bool = False
    case_sensitive: bool = True
    segment: dict[str, Any] = Field(default_factory=dict)


# ============================================================ 服务


def _llm_code(exc: Exception) -> str:
    """把 ChatError 的码尽量原样交给界面。

    只折成 ``LLM_ERROR`` 会把「还没配」「被网关拦」「只出了思考」三种完全不同的
    处置方式揉成一句「模型调用失败」。未登记的码仍回落 ``LLM_ERROR``。
    """
    code = str(getattr(exc, "code", "") or "")
    keep = {"LLM_NOT_CONFIGURED", "LLM_FORBIDDEN", "LLM_EMPTY"}
    if code in keep and code in ERROR_CODES:
        return code
    return "LLM_ERROR"


def _reject_dotted(values: dict[str, Any], prefix: str = "") -> None:
    """挡掉「用点号路径提交设置」这种半对不的做法。

    前端 `buildPatch()` 生成的是嵌套对象；但手搓脚本很容易写成
    `{"llm.timeout_seconds": 900}`——那种键在任何 section 里都找不到，
    `apply()` 会一路静默略过，接口还回 ok=true，人就会以为改成功了。
    这里直接判参数不合法，把话说在明处。
    """
    for key, value in (values or {}).items():
        name = str(key)
        where = prefix + name
        if "." in name:
            raise WBError(
                "VALIDATION",
                f"设置项名字不对：{where}",
                "用嵌套对象提交，例如 {\"llm\": {\"timeout_seconds\": 900}}",
            )
        if isinstance(value, dict):
            _reject_dotted(value, where + ".")


def roles_payload(cfg: WorkbenchConfig) -> dict[str, Any]:
    """角色名单载荷 —— `summary.roles` 的唯一计算处，只需要配置，不需要数据库。

    为什么提成模块级函数而不是留在方法里：`video_workbench/cli.py` 的 `roles` 与
    打包版的 `--roles` 都是**纯读出口**，而 `WorkbenchService.__init__` 会
    `setup_logging`（建 `logs/`）、`open_and_migrate`（建库并落 `workbench.db.bak-*`）、
    连带写出 `workbench.json`。一条"看看有哪些角色"的命令在用户盘上留下四个文件，
    这个代价和它做的事不成比例 —— 2026-11-04 实测（`Temp/wb_t34/repro.py`）：一次
    `roles --config` 造成 6 项变化。所以把计算收在这里，装配留给真正要干活的入口
    （`--check` / `info` 这类体检本来就必须验"配置写得进去、目录可写"，它们照旧装配，
    别跟着改，否则体检就成了 no-op）。

    **这不是第二份真源**：`WorkbenchService.summary_roles()` 只是 `return roles_payload(self.cfg)`
    一层委托，设置页、RPC、CLI、打包自检走的都是同一条链；判定规则本身仍然只在
    `core/summarizer.resolve_role` / `configured_role_issue`，这里一个都不自己算。

    默认值也走角色取值的唯一去处（`resolve_role`）：配置文件里写着退役角色时，前端不能
    默认选中一个「一提交就被拒」的项——用户没做错任何事，界面先骗他一次。这里连「配置把
    角色名拼错了」也不能报错：这一页的作用正是让用户改配置，报了错他就没有改的地方了。
    """
    try:
        picked_default, _ = summarizer.resolve_role("", cfg.llm.role)
    except WBError:
        picked_default = summarizer.DEFAULT_ROLE
    # 配置里那个值到底能不能直接用，也问同一个 owner（不建第二条判定链）：
    # 能用时两个字段都是空，界面就不显示任何东西。
    dirty, hint = summarizer.configured_role_issue(cfg.llm.role)
    return {
        "roles": summarizer.roles_view(),
        "default": picked_default,
        "configured_unrecognized": dirty,
        "configured_hint": hint,
        # 退役名单：前端靠它把「这个角色被撤了」和「这个 key 我压根不认识」
        # 分成两句话（换角色 vs 改配置）。名单只有一个出处——权威层的
        # RETIRED_ROLES，界面不许自己写死 "standard"，否则下次退役就得改前端。
        "retired": sorted(summarizer.RETIRED_ROLES),
        # 同一批 key 的**中文显示名**（`{"key","display"}`），owner 是权威层 `role_label()`：
        # 命令行以前直接打裸 key，界面打「学术分析（已停用）」，同一个事实两个说法。
        # 为什么不改上面那个 `retired` 的形状：前端 `roleText()` 用 `indexOf(key)` 判退役，
        # 换成对象数组会恒判"不认识"，把历史行显示成「未知角色」。两个字段同源于
        # RETIRED_ROLES，不是第二份真源（细节见 core/summarizer.retired_row_labels 的注释）。
        "retired_labels": summarizer.retired_row_labels(),
        # 附加提示词开关的当前值。前端不自己数、不自己猜默认：这一栏和「默认角色」
        # 一样，唯一出处是后端配置（cfg.llm.extra_prompt，默认关）。
        "extra_prompt": bool(getattr(cfg.llm, "extra_prompt", False)),
    }


class WorkbenchService:
    def __init__(
        self,
        config_path: str | None = None,
        picker: Callable[[str], str | None] | None = None,
    ) -> None:
        self.started_at = time.time()
        # picker 由桌面层注入（需要 webview 窗口句柄才能弹系统对话框）。
        # 调试模式下为 None，相关接口直接报 UNSUPPORTED。
        self.picker = picker
        self._cookie_file: str | None = None

        self.cfg = WorkbenchConfig.load(config_path)
        # 日志要在打开数据库之前就位：迁移失败正是最需要日志的时候
        # 用 cfg.cache_root（已展开的 property），不是 cfg.storage.cache_root——
        # 后者是原始字符串，`%APPDATA%` 没展开会把日志写进当前工作目录下的
        # 字面 `%APPDATA%` 文件夹（P5-07 验收时被抓到）。
        self.logs = log_mod.setup_logging(
            log_mod.log_dir_for(self.cfg.cache_root),
            session_tag=f"version={__version__} skin={self.cfg.ui.skin}",
        )
        self.logger = log_mod.get_logger("service")
        self.logger.info("配置 %s / 数据库 %s", self.cfg.source_path or "(默认)", self.cfg.db_path)
        # 搬家发生过就要在日志里留一句：用户第一次启动会发现「生效的配置不是我打开过
        # 的那份文件」，没有这行日志就只能靠猜。只报路径与「没改值」这件事，不打印任何
        # 配置内容 —— 这一份里可能有密钥。
        if getattr(self.cfg, "migrated_from", None):
            self.logger.info(
                "首次启动：配置已从旧家 %s 搬到新家 %s（逐字段原样搬运，没有改任何值）；"
                "旧家是 bili-scribe 的文件，此后只读、不再被写入",
                self.cfg.migrated_from,
                self.cfg.source_path,
            )
            # 搬家时旧家留下的「挡住 bili-scribe 的键」：界面上有一次性弹窗
            # （app.migration_notice），日志这边留同一件事的事后追溯。
            # **只报键名，一个值都不打** —— 那些键的值里可能有密钥。
            notice = getattr(self.cfg.ui, "migration_notice", None)
            if isinstance(notice, dict) and notice.get("keys") and not notice.get("dismissed"):
                self.logger.info(
                    "旧家 %s 里有 %d 个 bili-scribe 不认识的键：%s —— WB 不替你删，"
                    "但 bili-scribe 会因为它们报「未知配置项」而起不来，需要你在 bili-scribe 那边自己清",
                    notice.get("from", ""),
                    len(notice.get("keys") or []),
                    ", ".join(str(k) for k in (notice.get("keys") or [])),
                )
        # 配置文件里有 bili-scribe 不认识的键：只报，不修（`config.load()` 里那段注释写了
        # 为什么不修——一次纯读查询就重写用户的配置文件，用户手里连原本那份都没了）。
        # 这里是有日志的现场，所以把那句话从"读盘时静默改文件"换成"启动时打一行"。
        # 仍然**只报键名、不报值**：那些键里可能有 api_key。
        if getattr(self.cfg, "foreign_keys", None):
            self.logger.warning(
                "生效配置 %s 里有 %d 个 bili-scribe 不认识的键：%s —— WB 不替你删、"
                "也不会自己重写这份配置；要清掉请在设置页保存一次（保存是显式动作）",
                self.cfg.source_path,
                len(self.cfg.foreign_keys),
                ", ".join(str(k) for k in self.cfg.foreign_keys),
            )
        log_mod.prune(self.logs["dir"], keep_days=30)
        self.conn = open_and_migrate(self.cfg.db_path)
        self.repo = Repo(self.conn)
        # 预读笔记的磁盘缓存（按素材内容寻址，跨任务、跨进程复用）。
        # 目录是 `cfg.notes_dir`——以前只是算得出来、没有任何代码往里写，现在接上了：
        # 一次长素材预读占一次总结输入的 38%（bs 实测 4 小时视频 32,663 tokens），
        # 同一条视频重开一个任务或反复「再改一版」时，这一整段应该一分不花。
        # 缓存坏了不影响正确性：读不出就按未命中重跑，写失败只记日志（见 infra/notecache.py）。
        self.notes = NoteCache(
            self.cfg.notes_dir,
            logger=lambda message: self.logger.info("预读缓存 %s", message),
        )
        self.events = EventBus()
        # 发布是全应用唯一不可撤销的动作，而且 pywebview 每个 js_api 调用一个线程：
        # 两次并发点「发布」会各自读到 status=draft、各自 POST 一次。这里用非阻塞锁
        # 把 check-then-act 串起来，第二个请求直接拿到 CONFLICT（D2）。
        self._publish_lock = threading.Lock()

        limits = {
            # 下载可并行：网络 IO，互不争用。上限取配置值。
            "download": max(1, int(self.cfg.download.concurrency)),
            # 转录必须串行：显存只有一份，两路同时跑只会互相拖慢甚至 OOM。
            "transcribe": 1,
            "subtitle": 1,
            "clip.export": 2,
            # 文案是纯网络等待，跟总结同一类，可以并行
            "clip.copy": 3,
            # 软/硬字幕导出走 ffmpeg 重编码，两个一起跑会互相抢 CPU 且看不出慢在哪。
            "video.export": 1,
            # 总结走外部模型、纯网络等待，可以并行；但上限压到 3——
            # 再多只会让同一家服务一起被限流，用户的等待时间反而更长。
            "summary.run": 3,
            "summary.collection": 1,
            # 生成候选是纯网络等待，和总结同池（3 路），再多会一起被限流。
            "clip.analyze": 3,
            "merge": 1,
            "publish": 1,
            "dev.demo": 1,
        }
        self.jobs = JobManager(
            self.repo,
            self.events,
            limits=limits,
            # 每个任务的临时目录由 JobManager 在收尾时删：handler 里漏一个 finally
            # 就会在用户盘上留一堆中间文件，清理不该靠每个 handler 自己记得。
            tmp_root=Path(self.cfg.storage.cache_root) / "tmp",
            # 任务日志同时落盘（界面抽屉关掉之后还得留得住）
            logger=log_mod.log_line,
        )
        self.jobs.register("dev.demo", self._demo_job)
        # 上次崩/被杀留下的临时目录在这里清掉（`JobManager` 已把残留任务标失败，
        # 所以此刻 cache/tmp 里的一切都是垃圾）
        self.tmp_swept = self._sweep_tmp()
        # 重启恢复：任务标 interrupted、合并记录收尾、项目阶段按产物回退。
        # 放在这里而不是后台线程里跑——它是"打开窗口"的一部分，
        # 界面第一眼看到的阶段必须是收敛后的结果。
        self.recovery = recovery_core.recover(
            self.repo, jobs=getattr(self.jobs, "interrupted", None)
        )

        # 真实业务：单集下载 + 批量下载。并发由 limits 里的 semaphore 控制，
        # batch 只派活，所以它自己的 limit=1 不影响子项并行度。
        self._gateway = default_gateway(self.cfg.db_path, self.cfg.storage.key_path)
        self._resolver = resolver_mod.Resolver(self._gateway)
        self.jobs.register(
            CHILD_TYPE,
            make_download_handler(
                repo=self.repo,
                media_root=self.cfg.media_root,
                cookie_provider=self._cookies,
                prober=self._probe,
                thumbs=self._thumb,
                backend=str(self.cfg.download.backend or "ytdlp"),
                bbdown_path=str(self.cfg.download.bbdown_path or ""),
                bbdown_args=str(self.cfg.download.bbdown_args or ""),
            ),
            limit=max(1, int(self.cfg.download.concurrency)),
        )
        self.jobs.register(BATCH_TYPE, make_batch_handler(manager=self.jobs), limit=1)

        # 转录：faster-whisper 跑在独立子进程里（见 workers/asr_worker.py 顶部
        # 三条理由）。父进程只做编排、进度映射和入库。
        self._ffmpeg = FFmpeg(prefer_dir=self.cfg.ffmpeg_dir)
        self.jobs.register(
            "transcribe",
            make_transcribe_handler(
                repo=self.repo,
                models_root=self.cfg.models_root,
                cfg=self.cfg.transcribe,
                ff=self._ffmpeg,
            ),
            limit=1,
        )
        self.jobs.register("model.download", self._model_download_job, limit=1)
        # 字幕优化要走外部 LLM，一次几分钟是常态，所以也进任务队列而不是堵 RPC。
        self.jobs.register("subtitle.optimize", self._optimize_job, limit=1)
        # 翻译和优化同池串行：两个都要打同一家模型服务，并行只会一起被限流。
        self.jobs.register("subtitle.translate", self._translate_job, limit=1)
        # 软/硬字幕都要过一遍 ffmpeg（硬字幕是全片重编码），按 RPC 同步跑会把界面卡死。
        self.jobs.register("video.export", self._export_job, limit=1)
        self.jobs.register("summary.run", self._summary_job, limit=3)
        self.jobs.register("summary.collection", self._collection_job, limit=1)
        self.jobs.register("clip.analyze", self._analyze_job, limit=3)
        # 切片导出：精确模式是重编码，两个并行已经能把 CPU 吃满；快速模式几乎瞬间。
        self.jobs.register("clip.export", self._clip_export_job, limit=2)
        # 人脸检测要解码抽帧，同时开两条只会互相拖慢
        self.jobs.register("clip.focus", self._clip_focus_job, limit=1)
        self.jobs.register("merge.run", self._merge_job, limit=1)
        # 文案：一条切片一次请求，纯等待，可以并行
        self.jobs.register("clip.copy", self._copy_job, limit=3)
        # 交付：写草稿/OTIO 是磁盘活，降级包可能拷几百 MB，串行更稳
        self.jobs.register("clip.deliver", self._clip_deliver_job, limit=1)

        self.router = Router()
        self._register_rpc()

    # ------------------------------------------------------- RPC 装配
    #
    # 显式列一遍而不是反射：这张表就是整个应用的攻击面，写出来才能审。

    def _register_rpc(self) -> None:
        r = self.router
        r.add("app.info", self.app_info)
        r.add("app.bootstrap", self.app_bootstrap)
        r.add("app.settings.get", self.settings_get)
        r.add("app.settings.put", self.settings_put, SettingsPutParams)
        r.add("app.skin.set", self.skin_set, SkinParams)
        r.add("app.rail.set", self.rail_set, RailParams)
        r.add("library.list", self.library_list, ListParams)
        r.add("library.get", self.library_get, IdParams)
        r.add("library.delete", self.library_delete, IdParams)
        r.add("style.list", self.style_list)
        r.add("style.get", self.style_get, StyleKeyParams)
        r.add("style.save", self.style_save, StyleSaveParams)
        r.add("style.delete", self.style_delete, StyleKeyParams)
        r.add("style.import", self.style_import, StyleFileParams)
        r.add("style.export", self.style_export, StyleFileParams)
        r.add("style.fonts", self.style_fonts)
        r.add("style.render", self.style_render, StyleRenderParams)
        r.add("job.list", self.job_list)
        r.add("job.get", self.job_get, IdParams)
        r.add("job.cancel", self.job_cancel, IdParams)
        r.add("job.retry", self.job_retry, IdParams)
        r.add("job.demo.submit", self.job_demo_submit, DemoParams)
        r.add("download.resolve", self.download_resolve, ResolveParams)
        r.add("download.start", self.download_start, DownloadParams)
        r.add("download.retry_failed", self.download_retry_failed, DownloadRetryParams)
        r.add("download.backends", self.download_backends, NoParams)
        r.add("import.local", self.import_local, ImportParams)
        r.add("transcribe.start", self.transcribe_start, TranscribeStartParams)
        r.add("transcribe.cancel", self.transcribe_cancel, IdParams)
        r.add("transcribe.models", self.transcribe_models)
        r.add("transcribe.languages", self.transcribe_languages)
        r.add("transcribe.model.download", self.transcribe_model_download, ModelParams)
        r.add("transcribe.model.remove", self.transcribe_model_remove, ModelParams)
        r.add("audio.peaks", self.audio_peaks, PeaksParams)
        r.add("subtitle.get", self.subtitle_get, SubtitleGetParams)
        r.add("subtitle.save", self.subtitle_save, SubtitleSaveParams)
        r.add("subtitle.op", self.subtitle_op, SubtitleOpParams)
        r.add("subtitle.optimize", self.subtitle_optimize, OptimizeParams)
        r.add("subtitle.optimize.apply", self.subtitle_optimize_apply, ApplyOptimizeParams)
        r.add("subtitle.rollback", self.subtitle_rollback, IdParams)
        r.add("subtitle.translate", self.subtitle_translate, TranslateParams)
        r.add("subtitle.translation", self.subtitle_translation, TranslationParams)
        r.add("subtitle.translation.apply", self.subtitle_translation_apply, ApplyTranslationParams)
        r.add("glossary.get", self.glossary_get)
        r.add("glossary.put", self.glossary_put, GlossaryPutParams)
        r.add("glossary.import_csv", self.glossary_import_csv, GlossaryCsvParams)
        r.add("glossary.export_csv", self.glossary_export_csv, GlossaryExportParams)
        r.add("subtitle.export", self.subtitle_export, SubtitleExportParams)
        r.add("summary.models", self.summary_models)
        r.add("summary.start", self.summary_start, SummaryStartParams)
        r.add("summary.get", self.summary_get, IdParams)
        r.add("summary.versions", self.summary_versions, IdParams)
        r.add("summary.refine", self.summary_refine, SummaryRefineParams)
        r.add("summary.restore", self.summary_restore, SummaryRestoreParams)
        r.add("summary.roles", self.summary_roles)
        # 预读缓存的两个入口（↔ bs 的 notes_stats / clear_notes）。名字跟着 WB 的点号
        # 命名域走（summary. 域下已有 9 个），返回体形状照 bs：stats() 的键名与
        # {"removed": n} 都不改，将来两侧对照不用动结构。
        r.add("summary.notes.stats", self.summary_notes_stats)
        r.add("summary.notes.clear", self.summary_notes_clear)
        # 角色提示词真源清单（哪个角色吃哪个 txt、多少字节、sha 前 8 位）。
        # 设置页那个「默认角色」下拉要能说出选中项现在用的是用户写的原文还是压缩版，
        # 判定只有一个出处：core/prompt_source.py，前端不复制映射表。
        r.add("summary.prompt.list", self.summary_prompt_list)
        r.add("collection.list", self.collection_list)
        r.add("summary.collection.overview", self.summary_collection_overview,
              CollectionOverviewParams)
        r.add("summary.collection.get", self.summary_collection_get, IdParams)
        r.add("clip.analyze", self.clip_analyze, ClipAnalyzeParams)
        r.add("clip.candidates", self.clip_candidates, IdParams)
        r.add("clip.update", self.clip_update, ClipUpdateParams)
        r.add("clip.create", self.clip_create, ClipCreateParams)
        r.add("clip.delete", self.clip_delete, IdParams)
        r.add("clip.export", self.clip_export, ClipExportParams)
        r.add("clip.export.batch", self.clip_export_batch, ClipExportBatchParams)
        r.add("clip.exports", self.clip_exports, ClipExportsParams)
        r.add("clip.deliver", self.clip_deliver, ClipDeliverParams)
        r.add("nle.info", self.nle_info, NleInfoParams)
        r.add("clip.focus", self.clip_focus, ClipFocusParams)
        r.add("clip.focus.save", self.clip_focus_save, ClipFocusSaveParams)
        r.add("clip.focus.analyze", self.clip_focus_analyze, ClipFocusAnalyzeParams)
        r.add("clip.focus.subject", self.clip_focus_subject, ClipFocusSubjectParams)
        r.add("clip.copy", self.clip_copy, ClipCopyParams)
        r.add("clip.copy.save", self.clip_copy_save, ClipCopySaveParams)
        r.add("clip.cover", self.clip_cover, ClipCoverParams)
        r.add("clip.cover.frames", self.clip_cover_frames, ClipCoverFramesParams)
        r.add("clip.cover.upload", self.clip_cover_upload, ClipCoverUploadParams)
        r.add("merge.sources", self.merge_sources, MergeSourcesParams)
        r.add("merge.preflight", self.merge_preflight, MergePreflightParams)
        r.add("merge.start", self.merge_start, MergeStartParams)
        r.add("merge.list", self.merge_list)
        r.add("publish.preview", self.publish_preview, PublishPreviewParams)
        r.add("publish.note", self.publish_note, PublishNoteParams)
        r.add("publish.list", self.publish_list, PublishListParams)
        r.add("publish.get", self.publish_get, PublishGetParams)
        r.add("publish.delete", self.publish_delete, IdParams)
        r.add("publish.publish", self.publish_publish, PublishDoParams)
        r.add("publish.withdraw", self.publish_withdraw, PublishWithdrawParams)
        r.add("publish.records", self.publish_records, PublishRecordsParams)
        r.add("publish.regenerate", self.publish_regenerate, PublishRegenerateParams)
        r.add("llm.test", self.llm_test)
        r.add("llm.models", self.llm_models)
        r.add("fs.reveal", self.fs_reveal, RevealParams)
        r.add("app.open_url", self.open_url, OpenUrlParams)
        r.add("app.diagnostics", self.app_diagnostics, DiagnosticsParams)
        r.add("app.onboard", self.app_onboard)
        r.add("app.onboard.done", self.app_onboard_done, OnboardDoneParams)
        # 搬家一次性提示（task-29）：读 = 有没有该说的，dismiss = 说过了。
        # 注册写在这里，不动 app/rpc.py（那是 bili 的注册表）。
        r.add("app.migration_notice", self.app_migration_notice)
        r.add("app.migration_notice.dismiss", self.app_migration_notice_dismiss)
        r.add("app.log_tail", self.app_log_tail, LogTailParams)
        r.add("fs.pick", self.fs_pick, _PickParams)
        r.add("fs.media", self.fs_media)

    def dispatch(self, method: str, params: Any = None, request_id: str | None = None) -> dict[str, Any]:
        return self.router.dispatch(method, params, request_id)

    # ------------------------------------------------------- 信息

    def app_info(self) -> dict[str, Any]:
        """首屏需要的一切元信息。

        ``deps`` 报的是"装了没有"，不是"能用"——真实可用性要等到真调用才知道，
        但先把"没装"标出来能省掉一大半"点了没反应"的困惑。
        """
        return {
            "name": APP_NAME,
            "version": __version__,
            "config_path": self.cfg.source_path,
            "db_path": self.cfg.db_path,
            "log_dir": self.logs["dir"],
            "log_file": self.logs["file"],
            "media_root": self.cfg.media_root,
            "skin": self.cfg.ui.skin,
            "rail_collapsed": self.cfg.ui.rail_collapsed,
            # 首屏据此决定要不要浮出"五件事体检"（看过就不再弹）
            "onboarded": self.cfg.ui.onboarded,
            "uptime": round(time.time() - self.started_at, 1),
            # 上次退出留下的东西收拾了多少：界面据此决定要不要提醒"有 N 个任务被中断"
            "recovery": getattr(self, "recovery", {}),
            # 生效配置里「bili-scribe 不认识的键」的**键名**（值一律不给，里面可能有密钥）。
            # `load()` 以前会把这些键顺手重写掉，现在只报（见 config.py 那段注释），所以
            # 这里必须给一个能看见的地方：`info` / `--check` 的 json 里能看到，
            # 启动日志里同一条是 warning。空列表 = 干净，界面无需为此多画一栏。
            "foreign_keys": list(getattr(self.cfg, "foreign_keys", [])),
            "deps": {
                "ffmpeg": bool(shutil.which("ffmpeg") or self._ffmpeg_in_config_dir()),
                "yt_dlp": find_spec("yt_dlp") is not None,
                "faster_whisper": find_spec("faster_whisper") is not None,
            },
        }

    def _ffmpeg_in_config_dir(self) -> bool:
        candidate = os.path.join(self.cfg.ffmpeg_dir, "ffmpeg.exe")
        return os.path.isfile(candidate)

    def app_bootstrap(self) -> dict[str, Any]:
        """首屏一次拿全。分四次调用会让界面在弱机器上闪四帧空状态。"""
        return {
            "info": self.app_info(),
            "settings": self.cfg.public_view(),
            "library": self.library_list("", 50, 0),
            "jobs": self.repo.job_list(limit=20),
            "styles": self.style_list(),
        }

    # ------------------------------------------------------- 设置

    def settings_get(self) -> dict[str, Any]:
        return self.cfg.settings_view()

    def settings_put(self, values: dict[str, Any]) -> dict[str, Any]:
        _reject_dotted(values)
        restarted = self.cfg.apply(values)
        saved = self.cfg.save()
        # 先落盘再通知：通知里带着新皮肤，界面会立刻重绘；
        # 如果落盘失败，前面已经 raise，界面不会显示成"已保存"。
        self.events.emit_now(
            "app.settings",
            {"settings": self.cfg.public_view(), "saved": saved, "restart": restarted},
        )
        if restarted:
            self.events.toast("服务端配置已保存，重开程序后生效", "warning")
        else:
            self.events.toast("设置已保存", "success")
        return {"settings": self.cfg.settings_view(), "saved": saved, "restart": restarted}

    def skin_set(self, skin: str) -> dict[str, Any]:
        """切皮肤是高频操作，走独立接口：不整份提交设置，也不弹"已保存"。

        仍然要落盘——否则下次启动会闪一下默认皮肤。
        """
        if skin == self.cfg.ui.skin:
            return {"skin": skin}
        self.cfg.apply({"ui": {"skin": skin}})
        self.cfg.save()
        self.events.emit_now("app.skin", {"skin": skin})
        return {"skin": skin}

    def rail_set(self, collapsed: bool) -> dict[str, Any]:
        self.cfg.apply({"ui": {"rail_collapsed": bool(collapsed)}})
        self.cfg.save()
        return {"collapsed": bool(collapsed)}

    # ------------------------------------------------------- 项目库

    def _decorate(self, project: dict[str, Any], with_assets: bool = False) -> dict[str, Any]:
        """给列表行补上前端真正要用的派生字段。

        为什么不把这些字段写进表里：``has_thumb``、``missing`` 都是**磁盘现状**，
        落库那一刻就过期了。用户删掉一个 mp4 再从库里读，必须立刻标红。
        """
        root = str(project.get("root_dir") or "")
        assets = self.repo.asset_list(str(project.get("id") or ""))
        by_kind: dict[str, dict[str, Any]] = {}
        for asset in assets:
            by_kind.setdefault(str(asset.get("kind")), asset)
        source = by_kind.get("source") or {}
        thumb_file = os.path.join(root, "frames", "thumb.jpg")
        row = dict(project)
        row["duration_sec"] = round(float(source.get("duration_ms") or 0) / 1000.0, 1)
        row["bytes"] = int(source.get("bytes") or 0)
        row["asset_count"] = len(assets)
        row["kinds"] = sorted(by_kind)
        row["has_thumb"] = os.path.isfile(thumb_file)
        row["thumb"] = thumb_file if row["has_thumb"] else ""
        row["missing"] = [
            str(a.get("path"))
            for a in assets
            if a.get("path") and not os.path.exists(str(a.get("path")))
        ]
        # 转录状态列表行也要看得到：只转了几个项目时，用户需要一眼分辨"转过没"。
        row["has_transcript"] = os.path.isfile(os.path.join(root, "transcript.json"))
        if with_assets:
            row["assets"] = [
                {**a, "exists": bool(a.get("path")) and os.path.exists(str(a.get("path")))}
                for a in assets
            ]
            transcript = self.repo.transcript_get(str(project.get("id") or ""))
            row["transcript"] = (
                {
                    "id": transcript.get("id"),
                    "language": transcript.get("language") or "",
                    "model": transcript.get("model") or "",
                    "status": transcript.get("status") or "",
                    "segments": len(transcript.get("segments") or []),
                    "srt_path": transcript.get("srt_path") or "",
                    "created_at": transcript.get("created_at") or "",
                }
                if transcript
                else None
            )
        return row

    def library_list(self, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        items = [self._decorate(p) for p in self.repo.project_list(query, limit, offset)]
        return {"items": items, "total": self.repo.project_count(query), "limit": limit, "offset": offset}

    def library_get(self, id: str) -> dict[str, Any]:
        project = self.repo.project_get(id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        return {"project": self._decorate(project, with_assets=True)}

    def library_delete(self, id: str) -> dict[str, Any]:
        """只删记录，不删磁盘。删文件是不可逆操作，得走「打开目录」让用户自己动手。"""
        project = self.repo.project_get(id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        self.repo.project_delete(id)
        self.events.emit_now("library.changed", {"id": id, "op": "delete"})
        return {"id": id, "removed": True}

    def style_list(self) -> list[dict[str, Any]]:
        rows = self.repo.style_list()
        out = []
        for row in rows:
            conf = styles_core.normalize_base(row)
            out.append({
                "id": conf["id"],
                "name": conf["name"],
                "mode": conf["mode"],
                "layout": conf["layout"],
                "builtin": bool(row.get("builtin")),
                "size": int(conf["primary"]["size"]),
                "font": conf["primary"]["font"],
                "config": conf,
            })
        return out

    # ------------------------------------------------- 样式编辑（P3-07）
    #
    # 样式的"三处一致"是这里唯一的难点：内存里的 config、wb_style 行、
    # styles/{id}.json 文件。少了文件，导出前换个机器就丢了；少了库行，
    # 界面列表就空。所以 save 一次写三处，delete 一次清两处。

    def _style_dir(self) -> Path:
        path = Path(str(self.cfg.storage.media_root)) / "styles"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _style_file(self, style_id: str) -> Path:
        return self._style_dir() / f"{style_id}.json"

    def _unique_style_name(self, name: str, *, exclude_id: str = "") -> str:
        """样式名唯一：重名自动加「 2」「 3」……

        `wb_style.name` 上有 UNIQUE。不去重的话，"导入一个同名样式""复制两次副本"
        会直接把 `sqlite3.IntegrityError` 抛成 INTERNAL——用户看到的是一串英文，
        而不是"名字重复了"。自动改名比报错好：约定俗成的做法与文件名去重一致。
        """
        wanted = str(name or "").strip() or "新样式"
        taken = {
            str(row.get("name") or "").strip()
            for row in self.repo.style_list()
            if str(row.get("id") or "") != str(exclude_id or "")
        }
        if wanted not in taken:
            return wanted
        index = 2
        while f"{wanted} {index}" in taken:
            index += 1
        return f"{wanted} {index}"

    def _style_resolve(self, key: str) -> tuple[dict[str, Any], bool, str]:
        """按 id/名字取样式；库里没有就退到内置预设。返回 (样式, 是否内置, 文件路径)。"""
        wanted = str(key or "").strip() or str(self.cfg.subtitle.default_style or "default")
        row = self.repo.style_get(wanted)
        if row:
            conf = styles_core.normalize_base(row)
            return conf, bool(row.get("builtin")), str(self._style_file(conf["id"]))
        preset_id = styles_core.preset_key(wanted)
        for preset in styles_core.builtin_presets():
            if preset_id in {preset["id"], preset["name"]}:
                return styles_core.normalize(preset), True, str(self._style_file(preset["id"]))
        raise WBError("NOT_FOUND", "样式不存在", str(key))

    def style_get(self, id: str = "") -> dict[str, Any]:
        conf, builtin, file_path = self._style_resolve(id)
        _style, warnings = styles_core.validate(conf)
        return {
            "style": conf,
            "builtin": builtin,
            "warnings": warnings,
            "file": file_path,
            "canvas": styles_core.canvas_size(conf),
        }

    def style_save(self, id: str = "", name: str = "",
                   config: dict[str, Any] | None = None) -> dict[str, Any]:
        style_id = str(id or "").strip()
        existing: dict[str, Any] | None = None
        if style_id:
            row = self.repo.style_get(style_id)
            if row and row.get("builtin"):
                raise WBError("CONFLICT", "内置样式不可修改，请先复制一份")
            existing = styles_core.normalize_base(row) if row else None
        if not style_id:
            style_id = new_id("sty")
        merged = styles_core.normalize(
            {"id": style_id, "name": name or (existing or {}).get("name") or "新样式",
             **(config or {})},
            base=existing or styles_core.default_preset(),
        )
        merged["id"] = style_id
        merged["name"] = str(name or merged.get("name") or "新样式").strip() or "新样式"
        style, warnings = styles_core.validate(merged)
        unique = self._unique_style_name(style["name"], exclude_id=style_id)
        renamed = unique != style["name"]
        style["name"] = unique
        row = self.repo.style_upsert(style_id, style["name"], style["mode"], style)
        file_path = self._style_file(style_id)
        _dump_json(file_path, style)
        return {
            "style": style,
            "builtin": False,
            "warnings": warnings,
            "file": str(file_path),
            "renamed": renamed,
            "row": {"id": row.get("id"), "name": row.get("name"), "mode": row.get("mode")},
        }

    def style_delete(self, id: str = "") -> dict[str, Any]:
        row = self.repo.style_delete(str(id or ""))
        file_path = self._style_file(str(row.get("id")))
        try:
            if file_path.is_file():
                file_path.unlink()
        except OSError:
            pass  # 库行已删，残留文件下次 save 会覆盖；不该因此让界面报错
        return {"id": row.get("id"), "removed": True}

    def style_import(self, id: str = "", path: str = "") -> dict[str, Any]:
        source = Path(str(path or ""))
        if not source.is_file():
            raise WBError("NOT_FOUND", "找不到样式文件", str(source))
        try:
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise WBError("VALIDATION", "样式文件不是合法 JSON", str(exc)) from exc
        if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
            raw = {**raw["config"], "name": raw.get("name") or raw["config"].get("name", "")}
        if not isinstance(raw, dict):
            raise WBError("VALIDATION", "样式文件应该是一个对象")
        # 导入永远生成新 id：直接覆盖同名样式会让"导入个预设看看"变成事故。
        name = str(raw.get("name") or source.stem or "导入样式").strip()
        return self.style_save(id=new_id("sty"), name=name, config=raw)

    def style_export(self, id: str = "", path: str = "") -> dict[str, Any]:
        conf, builtin, _file = self._style_resolve(id)
        target = Path(str(path or "")).expanduser() if path else self._style_dir() / f"{conf['id']}.json"
        if path:
            roots = self._allowed_roots()
            if not any(is_within(root, target) for root in roots):
                raise WBError("VALIDATION", "样式只能导出到媒体/缓存目录里", str(target))
        _dump_json(target, conf)
        return {"path": str(target), "bytes": target.stat().st_size if target.is_file() else 0,
                "builtin": builtin, "style": conf}

    def style_fonts(self) -> dict[str, Any]:
        """系统已装字体家族。只读注册表，不扫描字体文件（扫描要解析 TTF，慢且没必要）。"""
        families: list[str] = []
        try:
            import winreg  # noqa: PLC0415 —— 只在 Windows 有，导入失败要能降级

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts",
            )
            try:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    name, _value, _kind = winreg.EnumValue(key, index)
                    family = str(name).split("(")[0].split("&")[0].strip()
                    if family:
                        families.append(family)
            finally:
                winreg.CloseKey(key)
        except (ImportError, OSError):
            families = []
        seen: dict[str, str] = {}
        for family in families:
            seen.setdefault(family.lower(), family)
        fonts = sorted(seen.values(), key=str.lower)
        return {"fonts": fonts, "count": len(fonts),
                "note": "" if fonts else "读不到系统字体列表，可直接手填字体名"}

    def style_render(self, id: str = "", config: dict[str, Any] | None = None,
                     text: str = "") -> dict[str, Any]:
        """真渲一张 PNG。失败就该失败——界面自己退画布预览，别在这里返回假图。"""
        if config:
            base = None
            if id:
                try:
                    base, _builtin, _file = self._style_resolve(id)
                except WBError:
                    base = None
            style = styles_core.normalize({**config, "id": id or "preview"}, base=base)
        else:
            style, _builtin, _file = self._style_resolve(id)
        style, warnings = styles_core.validate(style)
        canvas = styles_core.canvas_size(style)
        height = int(canvas["height"])
        texts = [text] if str(text or "").strip() else None
        ass_body = styles_core.preview_ass(style, height=height, texts=texts)
        ffmpeg, _probe = self._ffmpeg.require()
        cache = Path(str(self.cfg.storage.cache_root)) / "previews"
        cache.mkdir(parents=True, exist_ok=True)
        # 文件名带时间戳只到秒：同一秒里连点两次「真实渲染」会互相覆盖，
        # 用户看到的可能是上一次的图。unique_path 保证每次都留一张。
        stamp = time.strftime("%Y%m%d-%H%M%S")
        ass_path = unique_path(cache / f"style-{stamp}.ass")
        png_path = unique_path(Path(str(self.cfg.storage.media_root)) / "previews" / f"style-{stamp}.png")
        png_path.parent.mkdir(parents=True, exist_ok=True)
        ass_path.write_text(ass_body, encoding="utf-8")
        background = str((style.get("preview") or {}).get("background") or "dark")
        color = {"light": "0xF7F6F1", "image": "0x14181A"}.get(background, "0x14181A")
        self._ffmpeg.run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:s={canvas['width']}x{canvas['height']}:d=1",
            "-vf", subtitle_filter(ass_path),
            "-frames:v", "1",
            str(png_path),
        ])
        if not png_path.is_file() or png_path.stat().st_size == 0:
            raise WBError("PROCESS_FAILED", "渲染没有产生图片")
        return {"path": str(png_path), "width": canvas["width"], "height": canvas["height"],
                "style_id": str(style.get("id") or ""), "warnings": warnings,
                "ass": str(ass_path), "bytes": png_path.stat().st_size}

    # ------------------------------------------------------- 任务

    def job_list(self) -> list[dict[str, Any]]:
        return self.jobs.list(limit=50)

    def job_get(self, id: str) -> dict[str, Any]:
        return self.jobs.get(id)

    def job_cancel(self, id: str) -> dict[str, Any]:
        return self.jobs.cancel(id)

    def job_retry(self, id: str) -> dict[str, Any]:
        return self.jobs.retry(id)

    def job_demo_submit(self, seconds: float = 10.0) -> dict[str, Any]:
        return self.jobs.submit("dev.demo", payload={"seconds": float(seconds)})

    def _demo_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Phase 0 的验收替身：验证进度 / 取消 / 重试三条链路都通。

        Phase 1 起真实任务（download / transcribe / …）陆续注册进同一个 JobManager，
        这个 handler 保留为"环境自检"入口。
        """
        total = max(0.5, float(payload.get("seconds", 10.0)))
        steps = max(1, int(total * 10))
        for index in range(steps + 1):
            ctx.check_cancel()
            ctx.progress(index / steps, stage=f"演示任务 {index * 100 // steps}%")
            if index % 10 == 0:
                ctx.log(f"[demo] {index // 10}s / {total:.0f}s")
            time.sleep(0.1)
        return {"seconds": total}


    # ------------------------------------------------------- 下载 / 导入

    def _cookies(self) -> str | None:
        """把共用的登录态导成 Netscape cookie 文件，路径交给 yt-dlp。

        裸登录失败时 yt-dlp 还能下 480P，但"用户明明登录了却只能看 480P"
        是最容易被当成 bug 报上来的事，所以有 cookie 就一定带上。
        文件写在临时目录、进程存活期间复用，退出即失效——不往用户家里多塞一份凭据。
        """
        if self._cookie_file and os.path.isfile(self._cookie_file):
            return self._cookie_file
        try:
            jar = dict(self._gateway.client().cookies or {})
        except WBError:
            return None
        if not jar.get("SESSDATA"):
            return None
        lines = ["# Netscape HTTP Cookie File", ""]
        for name, value in jar.items():
            if not value:
                continue
            lines.append("	".join([".bilibili.com", "TRUE", "/", "FALSE", "0", name, str(value)]))
        path = os.path.join(tempfile.gettempdir(), "video-workbench-cookies.txt")
        with open(path, "w", encoding="utf-8", newline=chr(10)) as fh:
            fh.write(chr(10).join(lines) + chr(10))
        self._cookie_file = path
        return path

    @staticmethod
    def _probe(path: str | os.PathLike[str]) -> Any:
        return probe_file(path)

    @staticmethod
    def _thumb(video: str | os.PathLike[str], root: str) -> Any:
        """下载完抽一帧当封面。ffmpeg 不在就返回 None，绝不抛到下载链上。"""
        return make_thumbnail(video, root)

    def download_resolve(self, url: str) -> dict[str, Any]:
        """解析链接，但**不建任务**：先让用户看清要下多少集再决定。"""
        if not str(url or "").strip():
            raise WBError("VALIDATION", "请先粘贴 B站链接")
        return self._resolver.resolve(str(url).strip()).to_payload()

    def download_start(self, url: str = "", quality: str = "1080P", items: list[dict[str, Any]] | None = None,
                       up_name: str = "", collection_title: str = "", backend: str = "") -> dict[str, Any]:
        """建一个批量下载任务。单集也走批量——只有一条代码路径才不会被漏测。"""
        quality = str(quality or "1080P").upper()
        backend = str(backend or "").strip().lower()
        if backend and backend not in BACKENDS:
            raise WBError("VALIDATION", "下载后端只能是 ytdlp 或 bbdown", backend)
        rows = [dict(i) for i in (items or []) if isinstance(i, dict)]
        title = ""
        page_count = len(rows)
        if not rows:
            res = self._resolver.resolve(str(url or "").strip())
            rows = [it.to_payload() for it in res.items]
            title = res.title
            up_name = up_name or res.up_name
            collection_title = collection_title or res.collection_title
            page_count = max(1, len(res.items))
        if not rows:
            raise WBError("BILI_ERROR", "这个链接里没有可下载的视频")
        job = self.jobs.submit(
            BATCH_TYPE,
            None,
            {
                "items": rows,
                "quality": quality,
                "up_name": up_name,
                "collection_title": collection_title,
                "page_count": page_count,
                "title": title,
                "backend": backend,
            },
        )
        return {"job": job, "count": len(rows), "title": title or str(rows[0].get("title") or "")}

    def download_backends(self) -> dict[str, Any]:
        """两个下载后端的状态（P1-08：设置页要显示 BBDown 有没有）。

        探测只回答"能不能找到"，不真起进程——设置页每切一次分区都调它，
        起子进程太贵。真跑的时候找不到会抛 ``DEPENDENCY_MISSING``。
        """
        cfg = self.cfg.download
        bbdown = downloader_mod.bbdown_info(cfg.bbdown_path)
        return {
            "default": str(cfg.backend or "ytdlp"),
            "ytdlp": downloader_mod.ytdlp_info(),
            "bbdown": bbdown,
        }

    def download_retry_failed(self, id: str, backend: str = "") -> dict[str, Any]:
        """只重跑失败项。失败项从父任务的 result 里捞，不重下已经成功的。

        ``backend="bbdown"`` 是 P1-08 的兜底入口：用户在失败项上点一下才走 BBDown，
        这里**不会**因为"yt-dlp 失败了"就自动换后端。
        """
        backend = str(backend or "").strip().lower()
        if backend and backend not in BACKENDS:
            raise WBError("VALIDATION", "下载后端只能是 ytdlp 或 bbdown", backend)
        job = self.jobs.get(id)
        result = job.get("result") or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {}
        todo = failed_items(result if isinstance(result, dict) else {})
        if not todo:
            raise WBError("CONFLICT", "这个任务没有失败项")
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        new_job = self.jobs.submit(
            BATCH_TYPE,
            None,
            {
                "items": todo,
                "quality": payload.get("quality", "1080P"),
                "up_name": payload.get("up_name", ""),
                "collection_title": payload.get("collection_title", ""),
                "page_count": len(todo),
                "backend": backend or str(payload.get("backend") or ""),
            },
        )
        return {"job": new_job, "count": len(todo)}

    def import_local(self, paths: list[str] | None = None, recursive: bool = True) -> dict[str, Any]:
        """本地导入。跑在 RPC 线程里：只建库记录，慢的是抽帧，失败也不影响入库。"""
        picked = [str(p) for p in (paths or []) if str(p).strip()]
        if not picked:
            raise WBError("VALIDATION", "没有选择文件或文件夹")
        return import_videos(
            picked,
            media_root=self.cfg.media_root,
            repo=self.repo,
            prober=self._probe,
            thumbs=self._thumb,
            make_thumbs=True,
            recursive=recursive,
        )

    # ------------------------------------------------------- 转录

    def transcribe_start(self, project_ids: list[str] | None = None, model: str = "",
                         language: str = "", device: str = "", compute_type: str = "",
                         vad: bool | None = None, word_timestamps: bool | None = None) -> dict[str, Any]:
        """一个项目一个任务。

        不给"批量父任务"的理由：转录是串行的（一份显存），父任务除了把进度加起来
        没有任何作用，反而多一层失败传递。界面上按项目看进度更直接。
        """
        ids = [str(p) for p in (project_ids or []) if str(p).strip()]
        if not ids:
            raise WBError("VALIDATION", "请先选择要转录的项目")
        payload_base: dict[str, Any] = {
            "model": model or "",
            "language": language or "",
            "device": device or "",
            "compute_type": compute_type or "",
        }
        if vad is not None:
            payload_base["vad"] = bool(vad)
        if word_timestamps is not None:
            payload_base["word_timestamps"] = bool(word_timestamps)

        jobs: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for pid in ids:
            project = self.repo.project_get(pid)
            if not project:
                skipped.append({"project_id": pid, "reason": "项目不存在"})
                continue
            if project.get("status") == "downloading":
                skipped.append({"project_id": pid, "reason": "还在下载"})
                continue
            jobs.append(self.jobs.submit("transcribe", pid, {**payload_base, "project_id": pid}))
        if not jobs:
            reason = skipped[0]["reason"] if skipped else "没有可转录的项目"
            raise WBError("CONFLICT", reason)
        return {"jobs": jobs, "count": len(jobs), "skipped": skipped}

    def transcribe_cancel(self, id: str) -> dict[str, Any]:
        """取消转录。真正的杀进程发生在 handler 的 cancel 检查里（taskkill 进程树）。"""
        return self.jobs.cancel(id)

    def transcribe_models(self) -> dict[str, Any]:
        current = str(self.cfg.transcribe.model or models_mod.DEFAULT_MODEL)
        items = models_mod.list_status(self.cfg.models_root)
        return {
            "items": items,
            "current": current if any(i["name"] == current for i in items) else models_mod.DEFAULT_MODEL,
            "models_root": self.cfg.models_root,
            "engine": str(self.cfg.transcribe.engine or "faster-whisper"),
        }

    def transcribe_languages(self) -> dict[str, Any]:
        """源语言选项。弹窗与设置页共用这一份，别在前端手抄 100 个码。"""
        try:
            current = languages.normalize(self.cfg.transcribe.language)
        except WBError:
            # 老版本可能把 "zh-CN" 之类的坏值写进了配置。列个清单不该因此报错，
            # 静默降级成"自动检测"，真正的拦截在 transcribe.start 那侧。
            current = ""
        data = languages.catalog()
        return {
            "common": data["common"],
            "all": data["all"],
            "current": current,
            "count": len(languages.CODES),
        }

    def transcribe_model_download(self, name: str = "") -> dict[str, Any]:
        target = name or str(self.cfg.transcribe.model or models_mod.DEFAULT_MODEL)
        found = models_mod.spec(target)
        if models_mod.is_downloaded(self.cfg.models_root, found.name):
            return {"job": None, "downloaded": True, "name": found.name}
        job = self.jobs.submit("model.download", None, {"name": found.name})
        return {"job": job, "downloaded": False, "name": found.name}

    def transcribe_model_remove(self, name: str = "") -> dict[str, Any]:
        if not name:
            raise WBError("VALIDATION", "请指定要删除的模型")
        return models_mod.remove(self.cfg.models_root, name)

    # ------------------------------------------------------- 波形

    def audio_peaks(self, project_id: str = "", refresh: bool = False) -> dict[str, Any]:
        """给编辑器画布用的峰值包络。失败返回空列表 + error，不抛。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        root = Path(str(project.get("root_dir") or ""))
        assets = self.repo.asset_list(project_id)
        source = next((a for a in assets if a.get("kind") == "source"), None)
        media = Path(str((source or {}).get("path") or "")) if source else None
        # 时长只存在于 wb_asset.duration_ms（wb_project 没有这一列）；
        # 读错了整条波形就退化成"时长 0"，播放头拖不动。
        duration_ms = int((source or {}).get("duration_ms") or 0)
        if not media or not media.is_file():
            return {"project_id": project_id, "peaks": [], "buckets": waveform.BUCKETS,
                    "duration_ms": duration_ms, "cached": False, "error": "找不到源文件"}
        return {"project_id": project_id,
                **waveform.get_peaks(root, media, ffmpeg_bin=self._ffmpeg.ffmpeg,
                                     refresh=refresh, duration_ms=duration_ms)}

    # ------------------------------------------------------- 字幕编辑

    def subtitle_get(self, project_id: str = "") -> dict[str, Any]:
        """一次给全：段、质检、统计。编辑器首屏只发一个请求。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        row = self.repo.transcript_get(project_id) or {}
        segments = subtitles.normalize(row.get("segments") or [])
        return {
            "cleared": False,
            "project_id": project_id,
            "title": project.get("title"),
            "has_transcript": bool(row),
            "language": row.get("language") or "",
            "model": row.get("model") or "",
            "segments": segments,
            "qc": subtitles.qc(segments),
            "stats": subtitles.stats(segments),
        }

    def subtitle_save(self, project_id: str = "", segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """整份保存。前端自动保存走这条；编辑动作走 ``subtitle.op``。

        整份保存不做语义校验，只做结构归一——它是"用户已经完成的编辑结果"的落盘
        通道，服务端再校验一遍语义只会把用户的合法状态判成错。
        """
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        items = subtitles.normalize(segments or [])
        if not items:
            raise WBError("VALIDATION", "字幕为空，保存会把已有字幕清掉")
        self._store_subtitles(project_id, items)
        return {
            "project_id": project_id,
            "count": len(items),
            "qc": subtitles.qc(items),
            "stats": subtitles.stats(items),
        }

    def subtitle_op(self, project_id: str = "", op: str = "", **kw: Any) -> dict[str, Any]:
        """语义编辑：一条 RPC 表达一个编辑意图，服务端算完再返回全量结果。

        支持的操作与必填字段：

        ==================  ==========================================
        ``update``          segment_id, text
        ``split``           segment_id, at_ms
        ``merge``           segment_ids（必须是相邻段）
        ``shift``           offset_ms, segment_ids（空=整份）
        ``replace``         pattern, replacement, regex, case_sensitive
        ``delete``          segment_ids
        ``insert``          segment（含 start_ms/end_ms/text）
        ==================  ==========================================
        """
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕，先转录")
        current = subtitles.normalize(row.get("segments") or [])

        try:
            hits = 0
            if op == "update":
                items = subtitles.update_text(current, str(kw.get("segment_id") or ""), str(kw.get("text") or ""))
            elif op == "split":
                items = subtitles.split(current, str(kw.get("segment_id") or ""), int(kw.get("at_ms") or 0))
            elif op == "merge":
                items = subtitles.merge(current, kw.get("segment_ids") or [])
            elif op == "shift":
                items = subtitles.shift(current, int(kw.get("offset_ms") or 0), kw.get("segment_ids") or None)
            elif op == "replace":
                items, hits = subtitles.replace_text(
                    current, str(kw.get("pattern") or ""), str(kw.get("replacement") or ""),
                    regex=bool(kw.get("regex")), ids=kw.get("segment_ids") or None,
                    case_sensitive=kw.get("case_sensitive") is not False,
                )
            elif op == "delete":
                items = subtitles.delete(current, kw.get("segment_ids") or [])
            elif op == "insert":
                payload = dict(kw.get("segment") or {})
                if not payload.get("text"):
                    raise ValueError("新段要有文本")
                payload.setdefault("start_ms", 0)
                payload.setdefault("end_ms", int(payload["start_ms"]) + 1000)
                items = subtitles.insert(current, payload)
            else:
                raise WBError("VALIDATION", f"不认识的字幕操作：{op}")
        except KeyError as exc:
            raise WBError("NOT_FOUND", f"找不到这一段：{exc.args[0] if exc.args else ''}") from exc
        except ValueError as exc:
            raise WBError("VALIDATION", str(exc)) from exc

        self._store_subtitles(project_id, items)
        return {
            "project_id": project_id,
            "op": op,
            "hits": hits,
            "segments": items,
            "qc": subtitles.qc(items),
            "stats": subtitles.stats(items),
        }

    # ------------------------------------------------------- 字幕优化（P2-04）

    def _llm_client(self, task: str = "subtitle") -> ChatClient:
        """取模型客户端：**服务商和模型都由 ``llm.tasks.<task>`` 决定**。

        这里不再自己拼 ``client.model = override``。以前只能覆盖模型名、地址永远是
        那一家；现在取值走 ``config.resolve_task_model`` 一个入口，
        ``tasks.vision = "bigmodel/glm-4.5v"`` 才会真的打到 GLM 而不是 DeepSeek。
        """
        return ChatClient.from_config(self.cfg, task=task)

    def _llm_client_for(
        self, provider: str = "", base_url: str = "", api_key: str = "", task: str = "",
        model: str = ""
    ):
        """按界面上**当前**的地址/密钥造客户端，允许它们还没保存。

        设置页的用法是「敲完地址和密钥、还没点保存，就直接点测试连接 / 获取可用模型」。
        磁盘上的配置那会儿还是旧的（甚至是空的），所以这里把配置**拷一份**再改副本：
        既能让这次请求用上新填的值，又不会把 self.cfg 改脏——用户随后点「放弃改动」时，
        内存里的配置必须跟磁盘一致，否则界面回显与文件对不上。
        """
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()
        if provider:
            # 指名了某一家的情形，**不许**静默退到「当前生效服务商」：
            # 用户看着「本机 magpie」这张卡点「获取可用模型」，实际却打到了另一家的地址，
            # 报出来是「被 CDN 拦了」——排查方向全错。地址空就直接说这张卡缺地址。
            card = find_provider(self.cfg.llm, provider)
            if card is None:
                raise WBError(
                    "LLM_NOT_CONFIGURED",
                    f"找不到服务商「{provider}」（可能是刚删掉还没保存）",
                    "刷新页面后重试；或先在卡片里保存这家",
                )
            if not base_url and not (card.base_url or "").strip():
                raise WBError(
                    "LLM_NOT_CONFIGURED",
                    f"「{card.name or card.id}」还没填 API 地址",
                    "填好地址和密钥再点；本机网关的地址形如 http://127.0.0.1:3425/v1",
                )
            if not api_key and not (card.api_key or "").strip():
                raise WBError(
                    "LLM_NOT_CONFIGURED",
                    f"「{card.name or card.id}」还没填 API Key",
                    "填好再点",
                )
        if not base_url and not api_key and not provider:
            client = self._llm_client(task)
            if model:
                # 只选了模型、没指名服务商：还是这条早退路径，但模型照样要换
                client.model = model
            return client
        cfg = copy.deepcopy(self.cfg)
        card = find_provider(cfg.llm, provider) if provider else None
        if card is not None:
            if base_url:
                card.base_url = base_url
            if api_key:
                card.api_key = api_key
        else:
            # 没指名哪一家：改「当前生效那张卡」+ 扁平字段两处。
            # 只改扁平字段是不够的——``resolve_task_model`` 优先用生效卡里的地址，
            # 于是「界面上填了新地址、点测试连接」实际打到的还是老地址。
            if base_url:
                cfg.llm.base_url = base_url
            if api_key:
                cfg.llm.api_key = api_key
            try:
                from video_workbench.config import resolve_task_model

                target, _ = resolve_task_model(cfg.llm, task)
                if target is not None:
                    if base_url:
                        target.base_url = base_url
                    if api_key:
                        target.api_key = api_key
            except Exception:  # noqa: BLE001 - 取不到就退回扁平字段，不因这个把请求打死
                pass
        client = ChatClient.from_config(cfg, provider=provider, task=task)
        if model:
            # 「这一次用哪个模型」是调用方的显式选择，优先级高于任务映射与默认模型
            client.model = model
        return client

    def llm_test(
        self, provider: str = "", model: str = "", base_url: str = "", api_key: str = ""
    ) -> dict[str, Any]:
        """设置页的「测试连接」。失败必须说清是没配还是连不上。

        ``provider`` 指名某张服务商卡（卡片右下角那个按钮）；留空就用当前生效那家。
        打的是这家自己的地址与密钥，不借当前生效服务商的配置——
        否则「新加的一家没填 key」会被误测成能通过。
        """
        client = self._llm_client_for(
            provider, base_url, api_key, task="" if provider else "subtitle"
        )
        if model:
            client.model = model
        # 注入的客户端不一定是 ChatClient（测试用桩、以后可能有别的实现），
        # 所以取属性一律用 getattr——直接点会撞 AttributeError，报成「内部错误」，
        # 跟真实原因（桩没有这个字段）隔着一层，最难查。
        who = str(getattr(client, "provider_name", "") or "")
        if not client.configured:
            raise WBError(
                "LLM_NOT_CONFIGURED",
                "这家服务商还没配置完整" + (f"：{who}" if who else ""),
                "在 设置 → 模型 对应的服务商卡上补齐 API 地址、密钥与模型名再来测试",
            )
        started = time.time()
        try:
            reply = client.chat(
                [
                    {"role": "system", "content": "你是一个连接测试。只回复 OK。"},
                    {"role": "user", "content": "ping"},
                ],
                temperature=0.0,
                # 推理型模型（magpie 的 auto/*、deepseek-reasoner）会先把预算花在思考上，
                # 给 8 个 token 的话正文必然是空的——「测试连接」会把好模型判成坏的。
                max_tokens=512,
            )
        except ChatError as exc:
            raise WBError(_llm_code(exc), str(exc), getattr(exc, "detail", "")) from exc
        return {
            "ok": True,
            "model": client.model,
            "base_url": client.base_url,
            # 回显服务商名：界面上「测试的是哪张卡」必须写清楚，
            # 不然多服务商之后延迟数字对不上号。
            "provider": who,
            "latency_ms": int((time.time() - started) * 1000),
            "reply": (reply or "").strip()[:40],
        }

    def llm_models(
        self, provider: str = "", base_url: str = "", api_key: str = ""
    ) -> dict[str, Any]:
        """「获取可用模型」：问这家密钥能用哪些模型。

        和 ``llm.test`` 一样指名某张卡（界面上的按钮长在卡片里）。区别是
        **不需要模型名**——用户点它的时候正是还不知道有哪些模型，所以这里
        只校验地址 + 密钥，不借 ``require()``。

        ``LLM_NOT_CONFIGURED`` 原样透出（界面要显示「这家还没填地址/密钥」），
        其余一律折成 ``LLM_ERROR`` + 原始文案（401 说密钥、网络说连不上）。
        """
        client = self._llm_client_for(provider, base_url, api_key)
        who = str(getattr(client, "provider_name", "") or "")
        try:
            models = client.list_models()
        except ChatError as exc:
            raise WBError(_llm_code(exc), str(exc), getattr(exc, "detail", "")) from exc
        return {
            "provider": who,
            "base_url": client.base_url,
            "models": models,
            "count": len(models),
        }

    def subtitle_optimize(self, project_id: str = "", batch_size: int = 30,
                          segment_ids: list[str] | None = None) -> dict[str, Any]:
        """提交一次优化任务。``require()`` 放在提交前——没配好就别建一个注定失败的任务。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕，先转录")
        client = self._llm_client("subtitle")
        if not client.configured:
            raise WBError(
                "LLM_NOT_CONFIGURED",
                "还没有配置大模型",
                "在 设置 → AI 里填好 base_url / api_key / model 再来优化",
            )
        items = subtitles.normalize(row.get("segments") or [])
        picked = [str(i) for i in (segment_ids or []) if str(i).strip()]
        if picked:
            wanted = set(picked)
            items = [seg for seg in items if str(seg.get("id")) in wanted]
        if not items:
            raise WBError("VALIDATION", "没有可优化的字幕")
        job = self.jobs.submit(
            "subtitle.optimize",
            project_id,
            {
                "project_id": project_id,
                "batch_size": max(1, min(80, int(batch_size or 30))),
                "segment_ids": picked,
                "model": client.model,
            },
        )
        return {"job": job, "segments": len(items), "model": client.model}

    def _optimize_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        """跑 LLM 优化，把结果放进 job.result。**这里不改库**——改库要用户点「应用」。"""
        project_id = str(payload.get("project_id") or "")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕")
        items = subtitles.normalize(row.get("segments") or [])
        picked = [str(i) for i in (payload.get("segment_ids") or []) if str(i)]
        if picked:
            wanted = set(picked)
            items = [seg for seg in items if str(seg.get("id")) in wanted]

        client = self._llm_client("subtitle")
        ctx.log(f"用 {client.model} 优化 {len(items)} 段字幕")

        def on_progress(done: int, total: int) -> None:
            ctx.progress(min(1.0, done / total) if total else 0.0, stage=f"优化中 {done}/{total}")

        result = subtitle_llm.optimize(
            items,
            client,
            batch_size=int(payload.get("batch_size") or 30),
            cancel=ctx.should_cancel,
            on_progress=on_progress,
        )
        ctx.check_cancel()
        failed = result.get("failed") or []
        batches = int(result.get("batches") or 0)
        if batches and len(failed) >= batches and not result.get("changes"):
            first = failed[0] if failed else {}
            raise WBError(
                "LLM_ERROR",
                "所有批次都失败了，字幕没有改动",
                f"{first.get('code', '')}: {first.get('message', '')} {first.get('detail', '')}".strip(),
            )
        if failed:
            ctx.log(f"{len(failed)} 批失败，这些段保持原文")
        ctx.log(f"共 {result.get('changed', 0)} 段有改动")
        return result

    def subtitle_optimize_apply(self, project_id: str = "", changes: list[dict[str, Any]] | None = None,
                                accepted_ids: list[str] | None = None) -> dict[str, Any]:
        """把（用户接受的）改动落库。落库前先存快照，随时能回滚。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕")
        current = subtitles.normalize(row.get("segments") or [])
        accepted = {str(i) for i in (accepted_ids or []) if str(i).strip()} or None
        items = subtitle_llm.apply_changes(current, changes or [], accepted)
        applied = sum(1 for a, b in zip(items, current) if a != b)
        if items == current:
            return {
                "project_id": project_id,
                "applied": 0,
                "segments": current,
                "qc": subtitles.qc(current),
                "stats": subtitles.stats(current),
                "snapshot": "",
            }
        snapshot = self._write_snapshot(project_id, current)
        self._store_subtitles(project_id, items)
        self.events.emit("library.changed", {"project_id": project_id, "reason": "subtitle.optimize"})
        return {
            "project_id": project_id,
            "applied": applied,
            "segments": items,
            "qc": subtitles.qc(items),
            "stats": subtitles.stats(items),
            "snapshot": str(snapshot),
        }

    def _snapshot_path(self, project_id: str) -> Path:
        project = self.repo.project_get(project_id) or {}
        root = Path(str(project.get("root_dir") or ""))
        return root / "transcript.prev.json"

    def _write_snapshot(self, project_id: str, items: list[dict[str, Any]]) -> Path:
        """在项目目录里留一份改前副本。放在媒体目录而不是临时目录，是为了用户自己也能翻。"""
        path = self._snapshot_path(project_id)
        payload = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "reason": "subtitle.optimize", "segments": items}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.events.toast(f"优化前快照没写成：{exc}", "warning")
        return path

    def subtitle_rollback(self, id: str = "") -> dict[str, Any]:
        """回到上一次「应用」之前。只保留一层，够用且不会让人猜。"""
        if not id:
            raise WBError("VALIDATION", "缺少 project_id")
        path = self._snapshot_path(id)
        if not path.is_file():
            raise WBError("NOT_FOUND", "没有找到可回滚的版本")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WBError("INTERNAL", "快照文件读不出来", str(exc)) from exc
        items = subtitles.normalize(payload.get("segments") or [])
        if not items:
            raise WBError("CONFLICT", "快照是空的，无法回滚")
        self._store_subtitles(id, items)
        self.events.emit("library.changed", {"project_id": id, "reason": "subtitle.rollback"})
        return {
            "project_id": id,
            "segments": items,
            "qc": subtitles.qc(items),
            "stats": subtitles.stats(items),
            "restored_at": payload.get("at") or "",
        }

    # ------------------------------------------------------------ P2-05 翻译
    #
    # 翻译**不碰原始字幕**。译文一律先落 ``wb_subtitle``（kind="translation"、
    # status="draft"），用户确认后才写文件——方案书原则 7：AI 产出是草稿。
    # 好处不止一个：重译同一种语言是替换草稿，原始转录永远安全；
    # 单条重试也能直接拿草稿当底稿，不用把整篇重新喂给模型。

    def subtitle_translate(self, project_id: str = "", target: str = "auto",
                           layout: str = "", batch_size: int = 10,
                           segment_ids: list[str] | None = None) -> dict[str, Any]:
        """提交一次翻译任务。目标语言在这里就定死，任务里不再猜。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕，先转录")
        items = subtitles.normalize(row.get("segments") or [])
        if not items:
            raise WBError("VALIDATION", "字幕是空的，没什么可翻译")
        client = self._llm_client("translate")
        if not client.configured:
            raise WBError(
                "LLM_NOT_CONFIGURED",
                "还没有配置大模型",
                "在 设置 → AI 里填好 base_url / api_key / model 再来翻译",
            )
        want = (target or self.cfg.subtitle.translate.target or "auto").strip().lower()
        if want not in TRANSLATE_TARGETS:
            raise WBError("VALIDATION", f"target 只能是 {' / '.join(TRANSLATE_TARGETS)}", want)
        resolved = translator.resolve_target(want, items)
        if resolved not in ("zh", "en", "ja"):
            raise WBError("VALIDATION", "目标语言只能是 zh / en / ja", resolved)
        picked = [str(i) for i in (segment_ids or []) if str(i).strip()]
        job = self.jobs.submit(
            "subtitle.translate",
            project_id,
            {
                "project_id": project_id,
                "target": resolved,
                "requested_target": want,
                "layout": (layout or self.cfg.subtitle.translate.layout),
                "batch_size": max(1, min(50, int(batch_size or translator.DEFAULT_BATCH))),
                "segment_ids": picked,
                "model": client.model,
            },
        )
        return {
            "job": job,
            "target": resolved,
            "source": translator.detect_source(items),
            "segments": len(items),
            "model": client.model,
        }

    def _translate_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        """跑翻译，把草稿写进 ``wb_subtitle``。原始字幕一个字节都不改。"""
        project_id = str(payload.get("project_id") or "")
        row = self.repo.transcript_get(project_id)
        if not row:
            raise WBError("NOT_FOUND", "这条视频还没有字幕")
        target = str(payload.get("target") or "")
        base = subtitles.normalize(row.get("segments") or [])
        # 底稿优先取已有草稿：重试几段时，其它段的译文必须还在（否则用户会看到
        # "重试一条、丢掉整篇"）。草稿里没有的段就留空，由模型补。
        draft = self.repo.subtitle_get(project_id, kind="translation", language=target)
        if draft and draft.get("segments"):
            have = {str(seg.get("id")): str(seg.get("translated_text") or "")
                    for seg in draft["segments"]}
            for seg in base:
                seg["translated_text"] = have.get(str(seg.get("id")), "")
        else:
            for seg in base:
                seg["translated_text"] = ""

        client = self._llm_client("translate")
        glossary = list(self.cfg.subtitle.translate.glossary or [])
        ctx.log(f"用 {client.model} 翻译成{translator.LANG_NAMES.get(target, target)}，{len(base)} 段")

        def on_progress(done: int, total: int) -> None:
            ctx.progress(
                min(1.0, done / total) if total else 0.0,
                stage=f"翻译中 {done}/{total}",
            )

        result = translator.translate(
            base,
            client,
            target=target,
            glossary=glossary,
            batch_size=int(payload.get("batch_size") or translator.DEFAULT_BATCH),
            cancel=ctx.should_cancel,
            on_progress=on_progress,
            retry_ids=[str(i) for i in (payload.get("segment_ids") or []) if str(i)],
        )
        ctx.check_cancel()
        if not result.get("translated") and result.get("failed"):
            first = result["failed"][0]
            raise WBError(
                "LLM_ERROR",
                "所有批次都失败了，没有译文",
                f"{first.get('code', '')}: {first.get('message', '')} {first.get('detail', '')}".strip(),
            )
        if result.get("failed"):
            ctx.log(f"{len(result['failed'])} 段失败，可以在界面上单独重试")
        self.repo.subtitle_upsert(
            project_id,
            kind="translation",
            language=target,
            format="json",
            segments=result["segments"],
            status="draft",
        )
        return {
            "target": target,
            "translated": result["translated"],
            "filled": result.get("filled", result["translated"]),
            "total": result["total"],
            "failed": result["failed"],
            "batches": result["batches"],
            "model": result["model"],
            "segments": result["segments"],
        }

    def subtitle_translation(self, project_id: str = "", target: str = "") -> dict[str, Any]:
        """回读草稿（编辑器打开时恢复上次翻译结果）。"""
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        want = (target or self.cfg.subtitle.translate.target or "").strip().lower()
        if want in ("", "auto"):
            # auto 在这里不能留：草稿是按具体语言存的，取已存的那一份即可。
            rows = [r for r in self.repo.subtitle_list(project_id) if r.get("kind") == "translation"]
            row = rows[0] if rows else None
        else:
            row = self.repo.subtitle_get(project_id, kind="translation", language=want)
        if not row:
            return {"found": False, "project_id": project_id, "target": want, "segments": []}
        return {
            "found": True,
            "project_id": project_id,
            "target": row.get("language") or want,
            "status": row.get("status") or "draft",
            "updated_at": row.get("updated_at") or "",
            "segments": row.get("segments") or [],
        }

    def subtitle_translation_apply(self, project_id: str = "", target: str = "",
                                   layout: str = "",
                                   accepted_ids: list[str] | None = None,
                                   overrides: dict[str, str] | None = None) -> dict[str, Any]:
        """人工确认后落盘：``subtitle.{lang}.srt``（纯译文）+ ``subtitle.bilingual.ass``（双语）。

        ``accepted_ids`` 为空＝全接受；给了列表就只认这些段有译文，其余退回原文。
        ``overrides`` 是界面改过的译文，先合进草稿再落盘（草稿同步更新，重开还能看到）。
        """
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        want = (target or self.cfg.subtitle.translate.target or "").strip().lower()
        rows = [r for r in self.repo.subtitle_list(project_id) if r.get("kind") == "translation"]
        row = None
        for cand in rows:
            if cand.get("language") == want:
                row = cand
                break
        if row is None and want in ("", "auto") and rows:
            row = rows[0]
        if row is None:
            raise WBError("NOT_FOUND", "这个语言还没有译文，先翻译")
        items = subtitles.normalize(row.get("segments") or [])
        if not items:
            raise WBError("CONFLICT", "草稿是空的，无法写入文件")
        # 界面改过的译文先合进来，再谈接受/拒绝——合完的 items 下面会回写草稿，
        # 所以"编辑 → 确认 → 重开还在"这条链是真的，不是只改了内存。
        for key, value in (overrides or {}).items():
            for seg in items:
                if str(seg.get("id")) == str(key):
                    seg["translated_text"] = str(value or "")
                    break
        accepted = {str(i) for i in (accepted_ids or []) if str(i).strip()}
        if accepted:
            for seg in items:
                if str(seg.get("id")) not in accepted:
                    seg["translated_text"] = ""
        mode = layout or self.cfg.subtitle.translate.layout
        merged = translator.merge_bilingual(items, mode)
        project = self.repo.project_get(project_id) or {}
        root = Path(str(project.get("root_dir") or ""))
        lang = str(row.get("language") or want or "en")
        srt_path = root / f"subtitle.{lang}.srt"
        ass_path = root / "subtitle.bilingual.ass"
        # 纯译文 SRT：把译文搬到 text 上再复用 format_srt，不为译文再造一个格式化函数
        srt_items = [
            {"id": s.get("id"), "start_ms": s.get("start_ms"), "end_ms": s.get("end_ms"),
             "text": str(s.get("translated_text") or s.get("text") or "")}
            for s in items
        ]
        written: list[str] = []
        try:
            root.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(format_srt(srt_items), encoding="utf-8")
            ass_path.write_text(subtitles.format_ass(merged), encoding="utf-8")
            written = [str(srt_path), str(ass_path)]
        except OSError as exc:
            raise WBError("IO", "字幕文件写入失败", str(exc)) from exc
        self.repo.asset_upsert(project_id, "subtitle", str(srt_path), language=lang,
                               bytes=srt_path.stat().st_size)
        self.repo.asset_upsert(project_id, "subtitle", str(ass_path), language="bilingual",
                               bytes=ass_path.stat().st_size)
        self.repo.subtitle_upsert(
            project_id, kind="bilingual", language=lang, format="ass",
            segments=merged, path=str(ass_path), status="ready",
        )
        self.repo.subtitle_upsert(
            project_id, kind="translation", language=lang, format="json",
            segments=items, path=str(srt_path), status="confirmed",
        )
        self.events.emit("library.changed", {"project_id": project_id, "reason": "subtitle.translate"})
        return {
            "project_id": project_id,
            "target": lang,
            "layout": mode,
            "files": written,
            "segments": len(merged),
            "translated": sum(1 for s in items if str(s.get("translated_text") or "")),
        }

    # ------------------------------------------------------------ P2-05 术语表

    def glossary_get(self) -> dict[str, Any]:
        cfg = self.cfg.subtitle.translate
        pairs = translator.parse_glossary(cfg.glossary)
        return {
            "entries": list(cfg.glossary or []),
            "pairs": [{"source": a, "target": b} for a, b in pairs],
            "target": cfg.target,
            "bilingual": bool(cfg.bilingual),
            "layout": cfg.layout,
        }

    def glossary_put(self, entries: list[str] | None = None) -> dict[str, Any]:
        """整表替换术语表并落盘。非法行在这里就挡掉，别等翻译时静默少一条。"""
        raw = [str(e).strip() for e in (entries or []) if str(e).strip()]
        bad = [e for e in raw if not translator.parse_glossary([e])]
        if bad:
            raise WBError(
                "VALIDATION",
                "这些行不是「原文=译文」的格式",
                "；".join(bad[:3]),
            )
        self.cfg.apply({"subtitle": {"translate": {"glossary": raw}}})
        saved = self.cfg.save()
        self.events.emit_now("app.settings", {"settings": self.cfg.public_view(), "saved": saved})
        return {"entries": raw, "count": len(raw), "saved": saved}

    def glossary_import_csv(self, path: str = "", merge: bool = True) -> dict[str, Any]:
        """从 CSV 导入术语表。表头可有可无，只认「第一列=原文，第二列=译文」。"""
        if not path:
            raise WBError("VALIDATION", "缺少文件路径")
        src = Path(path)
        if not src.is_file():
            raise WBError("NOT_FOUND", "找不到这个 CSV", str(src))
        try:
            text = src.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            # 国内导出的表格常见 GBK，直接失败会让用户以为是软件坏了
            text = src.read_text(encoding="gbk", errors="replace")
        except OSError as exc:
            raise WBError("IO", "CSV 读不出来", str(exc)) from exc
        rows = list(csv.reader(io.StringIO(text)))
        entries: list[str] = []
        for row in rows:
            cells = [str(c).strip() for c in row if str(c).strip()]
            if len(cells) < 2:
                continue
            if cells[0].lower() in {"source", "原文", "term"}:
                continue  # 表头
            entries.append(f"{cells[0]}={cells[1]}")
        if not entries:
            raise WBError("VALIDATION", "这个 CSV 里没有可用的术语行", str(src))
        if merge:
            have = {str(e).split("=", 1)[0]: i for i, e in
                    enumerate([str(x).strip() for x in (self.cfg.subtitle.translate.glossary or [])])}
            merged = [str(x).strip() for x in (self.cfg.subtitle.translate.glossary or []) if str(x).strip()]
            for e in entries:
                key = e.split("=", 1)[0]
                if key in have:
                    merged[have[key]] = e
                else:
                    merged.append(e)
                    have[key] = len(merged) - 1
            entries = merged
        return self.glossary_put(entries)

    def glossary_export_csv(self, path: str = "") -> dict[str, Any]:
        """导出术语表 CSV。不给路径就写到项目媒体根目录下，方便直接拿走出去。"""
        target_path = Path(path) if path else Path(self.cfg.media_root) / "glossary.csv"
        rows = [["source", "target"]]
        for source, dest in translator.parse_glossary(self.cfg.subtitle.translate.glossary):
            rows.append([source, dest])
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("w", newline="", encoding="utf-8-sig") as handle:
                csv.writer(handle).writerows(rows)
        except OSError as exc:
            raise WBError("IO", "CSV 写不出去", str(exc)) from exc
        return {"path": str(target_path), "count": len(rows) - 1}

    # ------------------------------------------------------------ P2-06 导出
    #
    # 三件事在这里决定清楚：
    # 1. **文本格式同步写**（srt/ass/vtt 只是几 KB），视频 mux/烧录进任务队列；
    # 2. **字号按输出高度缩放**（720P 基准，方案书 5.11.2）——样式表里存的是基准值，
    #    导出时才算目标值，不能把 1080P 的字号写进样式库；
    # 3. **软字幕必须回头验流**：ffmpeg 退出码 0 不等于字幕进了容器，
    #    只有 ffprobe 里真有一条 subtitle 流才算成功。

    EXPORT_FORMATS = ("srt", "ass", "vtt")

    def subtitle_export(self, project_id: str = "",
                        formats: list[str] | None = None,
                        mode: str = "none", height: int = 0,
                        style_id: str = "", language: str = "") -> dict[str, Any]:
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        mode = str(mode or "none").strip().lower()
        if mode not in {"none", "soft", "hard"}:
            raise WBError("VALIDATION", f"不认识的导出方式：{mode}")
        formats = [f.strip().lower() for f in (formats or []) if str(f).strip()]
        formats = [f for f in dict.fromkeys(formats) if f in self.EXPORT_FORMATS]
        if mode != "none" and "ass" not in formats:
            # 软/硬字幕都要有 ASS 源文件，没选也要生成——否则任务一开始就缺输入。
            formats.append("ass")
        if not formats and mode == "none":
            raise WBError("VALIDATION", "至少选一种字幕格式")

        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        row = self.repo.transcript_get(project_id) or {}
        items = subtitles.normalize(row.get("segments") or [])
        if not items:
            raise WBError("CONFLICT", "还没有字幕，先转录或导入字幕")
        root = Path(str(project.get("root_dir") or ""))
        out_dir = root / "export"
        assets = self.repo.asset_list(project_id)
        source = next((a for a in assets if a.get("kind") == "source"), None)
        source_path = Path(str((source or {}).get("path") or ""))

        lang = str(language or "").strip()
        layout = self.cfg.subtitle.translate.layout
        payload_items = items
        # 纯译文版本单独留一份：SRT/VTT 是单层格式，塞"原文＋译文"进去
        # 只会让播放器把两句挤在一起。ASS 才用 lines 做双行。
        plain_items = items
        if lang:
            draft = self.repo.subtitle_get(project_id, kind="translation", language=lang)
            if not draft:
                raise WBError("NOT_FOUND", f"还没有 {lang} 的译文，先翻译再导出")
            draft_items = draft.get("segments") or []
            payload_items = translator.merge_bilingual(draft_items, layout)
            plain_items = [
                {"id": seg.get("id"), "start_ms": seg.get("start_ms"),
                 "end_ms": seg.get("end_ms"),
                 "text": str(seg.get("translated_text") or seg.get("text") or "")}
                for seg in subtitles.normalize(draft_items)
            ]

        want = int(height or 0)
        if want <= 0:
            want = 0
            if source_path.is_file():
                try:
                    want = int(self._ffmpeg.probe(source_path).height or 0)
                except WBError:
                    want = 0
            want = want or subtitles.ASS_BASE_HEIGHT
        style = self.repo.style_get(style_id or self.cfg.subtitle.default_style) or {}

        suffix = lang or "orig"
        files: list[dict[str, Any]] = []
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            for fmt in formats:
                path = out_dir / f"subtitle.{suffix}.{fmt}"
                if fmt == "srt":
                    body = format_srt(plain_items)
                elif fmt == "vtt":
                    body = subtitles.format_vtt(plain_items)
                else:
                    body = subtitles.format_ass(payload_items, style=style, height=want)
                path.write_text(body, encoding="utf-8")
                self.repo.asset_upsert(project_id, "subtitle", str(path),
                                       language=suffix, bytes=path.stat().st_size)
                files.append({"format": fmt, "path": str(path),
                              "bytes": path.stat().st_size})
        except OSError as exc:
            raise WBError("IO", "字幕文件写入失败", str(exc)) from exc

        result: dict[str, Any] = {
            "project_id": project_id,
            "mode": mode,
            "height": want,
            "style_id": str(style.get("id") or ""),
            "files": files,
            "job_id": "",
        }
        if mode == "none":
            self.events.emit("library.changed", {"project_id": project_id,
                                                 "reason": "subtitle.export"})
            return result

        ass_path = next((Path(f["path"]) for f in files if f["format"] == "ass"), None)
        if ass_path is None:
            raise WBError("INTERNAL", "ASS 没写出来，无法导出视频")
        if not source_path.is_file():
            raise WBError("NOT_FOUND", "找不到源视频，无法导出视频")
        if not self._ffmpeg.available:
            self._ffmpeg.require()  # 抛 DEPENDENCY_MISSING，文案统一
        output = out_dir / f"{source_path.stem}.{want}p.{mode}.mp4"
        job = self.jobs.submit(
            "video.export",
            project_id,
            {
                "source": str(source_path),
                "subtitle": str(ass_path),
                "output": str(output),
                "mode": mode,
                "height": want,
                "language": suffix,
            },
        )
        result["job_id"] = job.get("id") or ""
        result["job"] = job
        result["output"] = str(output)
        return result

    def _export_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "soft")
        source = Path(str(payload.get("source") or ""))
        subtitle = Path(str(payload.get("subtitle") or ""))
        output = Path(str(payload.get("output") or ""))
        if not source.is_file() or not subtitle.is_file():
            raise WBError("NOT_FOUND", "导出输入缺文件")
        ctx.check_cancel()
        ctx.progress(0.05, "准备导出")
        ctx.log(f"{'软字幕' if mode == 'soft' else '硬字幕'}：{output.name}")
        try:
            if mode == "soft":
                self._ffmpeg.mux_subtitle(source, subtitle, output,
                                          language="chi", title=subtitle.stem,
                                          **ctx.guard_kwargs())
            else:
                self._ffmpeg.burn_subtitle(source, subtitle, output, **ctx.guard_kwargs())
        except WBError:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        ctx.check_cancel()
        ctx.progress(0.9, "校验产物")
        info = self._ffmpeg.probe(output)
        if mode == "soft" and not self._ffmpeg.subtitle_streams(output):
            # 退出码 0 但容器里没有字幕流——只可能是 -map 写错，
            # 让用户拿到一个"看起来成功、打开没字幕"的文件比直接失败更糟。
            raise WBError("PROCESS_FAILED", "软字幕没有进容器，导出文件不可用", str(output))
        project_id = ctx.project_id or str(payload.get("project_id") or "")
        if project_id:
            self.repo.asset_upsert(project_id, "export", str(output),
                                   bytes=info.bytes, duration_ms=info.duration_ms,
                                   language=str(payload.get("language") or ""))
            self.events.emit("library.changed", {"project_id": project_id,
                                                 "reason": "subtitle.export"})
        ctx.progress(1.0, "完成")
        return {
            "output": str(output),
            "mode": mode,
            "height": info.height,
            "bytes": info.bytes,
            "duration_ms": info.duration_ms,
        }

    def _store_subtitles(self, project_id: str, items: list[dict[str, Any]]) -> None:
        """写三处：数据库、``transcript.json``、``transcript.srt``。

        少写任何一处都会造成"界面上是对的、导出出来是旧的"这类很难查的问题，
        所以干脆一次写完。写文件失败只记日志不抛——数据库已经是最新，导出会重建。
        """
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        root = Path(str(project.get("root_dir") or ""))
        row = self.repo.transcript_get(project_id) or {}
        json_path = root / "transcript.json"
        srt_path = root / "transcript.srt"
        payload: dict[str, Any] = {}
        if json_path.is_file():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
        payload["segments"] = items
        payload["duration_ms"] = items[-1]["end_ms"] if items else 0
        payload.setdefault("language", row.get("language") or "")
        payload.setdefault("model", row.get("model") or "")
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            srt_path.write_text(format_srt(items), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 磁盘满/权限问题
            self.events.toast(f"字幕文件写入失败：{exc}", "warning")

        self.repo.transcript_upsert(
            project_id,
            segments=items,
            language=str(row.get("language") or ""),
            model=str(row.get("model") or ""),
            srt_path=str(srt_path),
        )
        if self.repo.project_get(project_id):
            self.repo.project_update(project_id, stage="transcribed")
        self.events.emit_now("library.changed", {"project_id": project_id, "reason": "subtitle"})

    # ------------------------------------------------------------- 总结（P2-07）
    #
    # 这一层的职责只有三件：**定素材、交给适配器、把产出入库**。
    # 提示词、分块预读、三轮回都在 bili_scribe.core.summarizer 里（P2-07 的验收
    # 要求就是"包装它"，不是重写它）。这里多做一件事：把每次产出都存成一个版本，
    # 因为总结是给人改的草稿，用户会想回去看上一版怎么写。

    def summary_roles(self) -> dict[str, Any]:
        # 载荷本身在模块级 `roles_payload`：CLI 的 `roles` 与打包版的 `--roles` 都要读它，
        # 而它们不该为了取一份只读名单去装配数据库和日志。这里保留一层委托，
        # **owner 仍然只有一个**，设置页读的也是这一份。
        return roles_payload(self.cfg)

    def summary_notes_stats(self) -> dict[str, Any]:
        """预读缓存的现状（↔ bs ``app/service.py:1512 notes_stats``）。

        让「省了多少」看得见：条目数、占用字节、目录，以及本次进程的命中/未命中。
        """
        return self.notes.stats()

    def summary_notes_clear(self) -> dict[str, Any]:
        """清空预读缓存（↔ bs ``app/service.py:1517 clear_notes``）。

        只删磁盘那一层：缓存是可以整个删掉的东西，下一次总结自己会重跑预读，产物不差
        分毫。内存里那份预读结果不动——它属于「正在打磨的那一版」，删了反而让下一轮
        多付一次预读的钱。
        """
        return {"removed": self.notes.clear()}

    def summary_prompt_list(self) -> dict[str, Any]:
        """包内提示词 txt 的清单：谁被绑成角色、谁只是躺在目录里等拍去向。

        设置页要的是"用户写的那一份到底生效了没有"，所以这里连 sha256 与字节数一起给
        （值由 prompt_source 现算，不在配置或前端里存副本）。
        """
        return {"prompts": prompt_source.manifest()}

    def _pick_model(self, spec: str, task: str) -> tuple[str, str]:
        """把界面上选的模型翻成 (服务商 id, 模型名)。空串＝跟随设置。"""
        raw = (spec or "").strip()
        if not raw:
            return "", ""
        provider, model = parse_model_spec(raw)
        if provider:
            card = find_provider(self.cfg.llm, provider)
            if card is None:
                raise WBError(
                    "VALIDATION",
                    f"没有这个服务商：{provider}",
                    "在 设置 → 模型 里添加这家，或刷新页面重选",
                )
            return card.id, model
        return "", model

    def summary_models(self) -> dict[str, Any]:
        """列出总结能选的模型：所有服务商卡里的模型 + 当前的默认选择。"""
        items: list[dict[str, Any]] = []
        for card in self.cfg.llm.providers or []:
            for name in card.models or []:
                items.append({
                    "spec": f"{card.id}/{name}",
                    "provider": card.id,
                    "provider_name": card.name or card.id,
                    "model": name,
                })
        card_default, model_default = resolve_task_model(self.cfg.llm, "summary")
        default = ""
        if card_default is not None and model_default:
            default = f"{card_default.id}/{model_default}"
        return {"items": items, "default": default, "configured": bool(items)}
    def summary_start(self, project_id: str = "", role: str = "",
                      danmaku: list[str] | None = None,
                      model: str = "") -> dict[str, Any]:
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        row = self.repo.transcript_get(project_id)
        if not row or not (row.get("segments") or []):
            raise WBError(
                "CONFLICT",
                "这条视频还没有字幕，先转录",
                "总结只用素材里的信息；没有字幕就没有素材可用",
            )
        provider_id, model_name = self._pick_model(model, "summary")
        client = self._llm_client_for(provider_id, task="summary", model=model_name)
        if not client.configured:
            raise WBError(
                "LLM_NOT_CONFIGURED",
                "还没有配置大模型",
                "在 设置 → 模型 里填好地址与密钥再来",
            )
        # 角色取值收敛到唯一去处：显式提交的退役值/未知值在这里就抛，文案用权威层原话；
        # 配置里的退役值回落默认角色，回落原因随 payload 交给任务日志（RPC 这层没有日志）。
        # 原来这里跟着一段 `picked not in ROLE_KEYS → 没有这个角色：X` 的预校验，它排在
        # 适配层兜底前面，把「配置写着 standard」的用户直接挡在门外——那是死路，删掉。
        picked, role_notice = summarizer.resolve_role(role, self.cfg.llm.role)
        job = self.jobs.submit(
            "summary.run",
            project_id,
            {
                "project_id": project_id,
                "kind": "fresh",
                "role": picked,
                "role_notice": role_notice,
                "danmaku": [str(item) for item in (danmaku or [])][:400],
                "model": client.model,
                "provider": provider_id,
            },
        )
        return {"job": job, "role": picked, "model": client.model,
                "provider": provider_id, "segments": len(row.get("segments") or [])}

    def summary_get(self, id: str = "") -> dict[str, Any]:
        """读最新一版（``id`` 是项目 id）。没有总结时返回 ``found=False``。"""
        project_id = id
        if not project_id:
            raise WBError("VALIDATION", "缺少项目 id")
        latest = self.repo.summary_latest(project_id)
        versions = self.repo.summary_list(project_id, limit=50)
        return {
            "found": bool(latest),
            "summary": latest or {},
            "versions": [_version_row(item) for item in versions],
            "totals": _usage_totals(versions),
        }

    def summary_versions(self, id: str = "") -> dict[str, Any]:
        if not id:
            raise WBError("VALIDATION", "缺少项目 id")
        rows = self.repo.summary_list(id, limit=100)
        return {"versions": [_version_row(item) for item in rows]}

    def summary_refine(self, project_id: str = "", requirement: str = "",
                       role: str = "", summary_id: str = "",
                       model: str = "") -> dict[str, Any]:
        if not project_id:
            raise WBError("VALIDATION", "缺少 project_id")
        demand = (requirement or "").strip()
        if len(demand) < 4:
            raise WBError("VALIDATION", "说一下你想怎么改（至少几个字）")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        row = self.repo.transcript_get(project_id)
        if not row or not (row.get("segments") or []):
            raise WBError("CONFLICT", "这条视频还没有字幕，先转录")
        base = self.repo.summary_get(summary_id) if summary_id else self.repo.summary_latest(project_id)
        if not base:
            raise WBError("CONFLICT", "还没有总结，先生成一版再改")
        provider_id, model_name = self._pick_model(model, "summary")
        if not provider_id and not model_name:
            # 没指定就用这一版当初用的模型，别悄悄换成别的
            previous = str(base.get("model") or "")
            if previous:
                model_name = previous
        client = self._llm_client_for(provider_id, task="summary", model=model_name)
        if not client.configured:
            raise WBError("LLM_NOT_CONFIGURED", "还没有配置大模型")
        # 上一版存的 role 属于 fallback（那次不是用户此刻的选择）：退役就回落并留痕。
        picked, role_notice = summarizer.resolve_role(
            role, str(base.get("role") or "") or self.cfg.llm.role)
        job = self.jobs.submit(
            "summary.run",
            project_id,
            {
                "project_id": project_id,
                "kind": "refine",
                "role": str(picked),
                "role_notice": role_notice,
                "requirement": demand,
                "previous_id": str(base.get("id") or ""),
                "model": client.model,
                "provider": provider_id,
            },
        )
        return {"job": job, "requirement": demand, "base": base.get("version")}

    def summary_restore(self, project_id: str = "", summary_id: str = "") -> dict[str, Any]:
        """把旧版恢复成新的一版。**不删历史**——所以这不是回滚，是"照这版再存一份"。"""
        if not project_id or not summary_id:
            raise WBError("VALIDATION", "缺少 project_id 或 summary_id")
        old = self.repo.summary_get(summary_id)
        if not old:
            raise WBError("NOT_FOUND", "找不到这一版总结")
        if str(old.get("project_id") or "") != project_id:
            raise WBError("VALIDATION", "这一版不属于这个项目")
        content = dict(old.get("content") or {})
        content["revise_note"] = f"恢复自第 {old.get('version')} 版"
        saved = self.repo.summary_insert(
            project_id,
            content=content,
            content_md=summarizer.to_markdown(content),
            role=str(old.get("role") or ""),
            model=str(old.get("model") or ""),
            usage={"restored_from": old.get("id")},
            status="ready",
        )
        self._write_summary_files(project_id, saved)
        return {"summary": saved, "restored_from": old.get("version")}

    # ------------------------------------------------------- 合集总览（P2-08）
    #
    # 合集在库里没有"实体"：P1 只往 ``wb_project.collection_title`` 写了名字。
    # 所以这一层的做法是**按名字聚合，出总览时才建一行 ``wb_collection`` 当锚**——
    # 每集的 id 会变、集数会增加，只有名字是稳的。
    #
    # 三条边界，都是"宁可拦住也不给出半成品"：
    # 1. 少于两集不给总览（一集的东西就去看单集总结）；
    # 2. 有任何一集还没总结就不给（总览的输入就是子项总结，缺一块等于让模型编）；
    # 3. 总览**不覆盖子项**（写在 ``scope='collection'`` 的行里），失败也不碰子项。

    def _collection_children(self, title: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for project in self.repo.project_list_by_collection(title):
            latest = self.repo.summary_latest(str(project.get("id") or ""))
            out.append({"project": project, "summary": latest or {}})
        return out

    @staticmethod
    def _collection_root(children: list[dict[str, Any]]) -> str:
        """合集目录 = 所有子项根目录的公共父目录。

        取公共父目录而不是"第一集的上一级"：万一有子项的目录被用户挪过
        （``root_dir`` 是可以改的），拿第一集的会算到别的地方去。
        """
        import os

        dirs = [str((entry.get("project") or {}).get("root_dir") or "") for entry in children]
        dirs = [item for item in dirs if item]
        if not dirs:
            return ""
        try:
            common = os.path.commonpath(dirs)
        except ValueError:
            return os.path.dirname(dirs[0])
        # commonpath 在"只有一个目录"时会返回它自己，这时候要退到上一级
        if len(dirs) == 1 or common in dirs:
            return os.path.dirname(common)
        return common

    def collection_list(self) -> dict[str, Any]:
        """界面要的两个东西：有哪些合集、每个合集里各集准备好了没。"""
        rows = self.repo.collection_titles()
        items: list[dict[str, Any]] = []
        for row in rows:
            title = str(row.get("title") or "")
            if not title:
                continue
            collection_id = self.repo.collection_id_for(title)
            latest = self.repo.collection_summary_latest(collection_id)
            children = self._collection_children(title)
            items.append(
                {
                    "title": title,
                    "collection_id": collection_id,
                    "items": len(children),
                    "summarized": sum(1 for entry in children if entry.get("summary")),
                    "ready": len(children) >= overview_core.MIN_ITEMS
                    and all(entry.get("summary") for entry in children),
                    "has_overview": bool(latest),
                    "version": int((latest or {}).get("version") or 0),
                    "updated_at": (latest or {}).get("created_at") or "",
                    "children": [
                        {
                            "id": str((entry.get("project") or {}).get("id") or ""),
                            "title": str((entry.get("project") or {}).get("title") or ""),
                            "stage": str((entry.get("project") or {}).get("stage") or ""),
                            "bvid": str((entry.get("project") or {}).get("bvid") or ""),
                            "has_summary": bool(entry.get("summary")),
                            "one_line": str(((entry.get("summary") or {}).get("content") or {})
                                            .get("one_line") or ""),
                        }
                        for entry in children
                    ],
                }
            )
        return {"collections": items}

    def summary_collection_overview(self, collection: str = "", role: str = "",
                                    extra: str = "",
                                    model: str = "") -> dict[str, Any]:
        title = (collection or "").strip()
        if not title:
            raise WBError("VALIDATION", "缺少合集名")
        children = self._collection_children(title)
        if len(children) < overview_core.MIN_ITEMS:
            raise WBError(
                "CONFLICT",
                f"合集至少要有 {overview_core.MIN_ITEMS} 集才能出总览",
                f"「{title}」现在只有 {len(children)} 集",
            )
        missing = [str((entry.get("project") or {}).get("title") or "")
                   for entry in children if not entry.get("summary")]
        if missing:
            raise WBError(
                "CONFLICT",
                f"有 {len(missing)} 集还没有总结，先给它们各出一版",
                "缺总结的：" + "、".join(missing[:5]),
            )
        provider_id, model_name = self._pick_model(model, "collection_overview")
        client = self._llm_client_for(
            provider_id, task="collection_overview", model=model_name
        )
        if not client.configured:
            raise WBError("LLM_NOT_CONFIGURED", "还没有配置大模型")
        picked, role_notice = summarizer.resolve_role(role, self.cfg.llm.role)
        # ``wb_job.project_id`` 有外键指向 ``wb_project``。合集的 ``col_xxx`` 不是项目，
        # 当 project_id 传进去会直接 FOREIGN KEY constraint failed —— 任务表就挂在这一步。
        # 合集信息全在 payload 里，任务行不挂项目（也就不会被"删项目"顺带删掉）。
        job = self.jobs.submit(
            "summary.collection",
            None,
            {
                "collection": title,
                "role": picked,
                "role_notice": role_notice,
                "extra": (extra or "").strip()[:400],
                "model": client.model,
                "provider": provider_id,
            },
        )
        return {"job": job, "collection": title, "items": len(children),
                "model": client.model, "provider": provider_id, "role": picked}

    def summary_collection_get(self, id: str = "") -> dict[str, Any]:
        """``id`` 可以是合集名，也可以是 ``col_xxx``。界面两种都有。"""
        key = (id or "").strip()
        if not key:
            raise WBError("VALIDATION", "缺少合集名")
        row = self.repo.collection_get(key)
        title = str((row or {}).get("title") or key)
        collection_id = str((row or {}).get("id") or self.repo.collection_id_for(title))
        latest = self.repo.collection_summary_latest(collection_id)
        versions = self.repo.collection_summary_list(collection_id, limit=50)
        children = self._collection_children(title)
        return {
            "found": bool(latest),
            "collection": {"id": collection_id, "title": title,
                           "root_dir": str((row or {}).get("root_dir") or "")},
            "overview": latest or {},
            "versions": [_version_row(item) for item in versions],
            "children": [
                {
                    "id": str((entry.get("project") or {}).get("id") or ""),
                    "title": str((entry.get("project") or {}).get("title") or ""),
                    "has_summary": bool(entry.get("summary")),
                }
                for entry in children
            ],
        }

    def _collection_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("collection") or "").strip()
        children = self._collection_children(title)
        if len(children) < overview_core.MIN_ITEMS:
            raise WBError("CONFLICT", f"合集至少要有 {overview_core.MIN_ITEMS} 集")
        client = self._llm_client_for(
            str(payload.get("provider") or ""),
            task="collection_overview",
            model=str(payload.get("model") or ""),
        )
        if not client.configured:
            raise WBError("LLM_NOT_CONFIGURED", "还没有配置大模型")
        # job 重放读到的 role 是**已落库**的历史值，不是这次的选择 → 按合同算 fallback：
        # 退役就回落默认角色，并把原因写进任务日志（入口算出的说明随 payload 带过来）。
        role, notice = summarizer.resolve_role(
            "", str(payload.get("role") or "") or self.cfg.llm.role)
        notice = str(payload.get("role_notice") or "") or notice
        if notice:
            ctx.log(notice)
        ctx.log(f"用 {client.model} 出合集总览：{title}（{len(children)} 集）")
        result = overview_core.run(
            title,
            children,
            client=client,
            ctx=ctx,
            role=role,
            model=client.model,
            extra=str(payload.get("extra") or ""),
            # task-59：合集总览也要把配置交给 SummaryLLM，否则坏 JSON 的补救预算与
            # llm.max_tokens 无关（见 core/overview.py 的 run() docstring 与
            # tests/wb/test_summary_llm_cfg_wiring.py 那条锁）。
            cfg=self.cfg,
        )
        ctx.check_cancel()
        content = dict(result["content"])
        root = self._collection_root(children)
        record = self.repo.collection_upsert(
            title=title,
            kind="collection",
            up_name=str((children[0].get("project") or {}).get("up_name") or ""),
            up_mid=(children[0].get("project") or {}).get("up_mid"),
            root_dir=root,
        )
        collection_id = str(record.get("id") or self.repo.collection_id_for(title))
        saved = self.repo.collection_summary_insert(
            collection_id,
            content=content,
            content_md=overview_core.to_markdown(content),
            role=str(result.get("role") or role),
            model=str(result.get("model") or client.model),
            usage=result.get("usage") or {},
            status="ready",
        )
        paths = self._write_collection_files(root, record, saved)
        self.events.emit_now("library.changed", {"reason": "collection",
                                                "collection": title})
        return {
            "summary_id": saved.get("id"),
            "collection": title,
            "version": saved.get("version"),
            "items": len(children),
            "conclusions": len(content.get("conclusions") or []),
            "watch_order": len(content.get("watch_order") or []),
            "notes": result.get("notes") or [],
            "files": paths,
            "chars": saved.get("chars"),
        }

    def _write_collection_files(self, root: str, record: dict[str, Any],
                                row: dict[str, Any]) -> list[str]:
        """把总览写成 ``collection_overview.md`` / ``.json``（放在合集目录根上）。

        为什么不像单集那样 ``asset_upsert``：资产表挂在 ``project_id`` 上，合集不是
        项目。硬塞进某一集名下，用户删那一集就把总览带走了。
        """
        base = Path(root or str(record.get("root_dir") or ""))
        if not str(base):
            return []
        payload = dict(row.get("content") or {})
        payload.setdefault("version", row.get("version"))
        payload.setdefault("collection", record.get("title"))
        md_path = base / "collection_overview.md"
        json_path = base / "collection_overview.json"
        history = base / "collection_overview.v{}.json".format(int(row.get("version") or 1))
        try:
            base.mkdir(parents=True, exist_ok=True)
            md_path.write_text(str(row.get("content_md") or ""), encoding="utf-8")
            body = json.dumps(payload, ensure_ascii=False, indent=2)
            json_path.write_text(body, encoding="utf-8")
            history.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise WBError("IO", "合集总览写入失败", str(exc)) from exc
        return [str(md_path), str(json_path)]

    # --------------------------------------------------------- 发布草稿（P2-09）
    #
    # 这里**一个网络请求都没有**。方案书原则 7：AI 产出一律是草稿，
    # 发布入口在 Phase 5 —— 到那时才有"发出去撤不回"的代价需要门禁来兜。
    # 现在能保证的是：草稿的正文形态与字数限制和真实接口一致，
    # 用户在这里看到「80/400 字」是本机上限；平台侧可能更紧（见 12025 文案）。

    def _publish_content(self, project_id: str, summary_id: str = "") -> dict[str, Any]:
        """取一版总结的 content。没有总结就没得写草稿，直接说清楚。"""
        record = self.repo.summary_get(summary_id) if summary_id else self.repo.summary_latest(project_id)
        if not record:
            raise WBError("CONFLICT", "这个项目还没有总结，先去「总结」页出一版")
        if record.get("project_id") != project_id:
            raise WBError("VALIDATION", "这版总结不属于当前项目")
        return record

    def publish_preview(
        self, project_id: str, summary_id: str = "", confirmed: bool = False
    ) -> dict[str, Any]:
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        record = self._publish_content(project_id, summary_id)
        draft = publisher_core.build_draft(
            project,
            record.get("content") or {},
            max_summary_chars=self.cfg.publish.max_summary_chars,
            disclose=self.cfg.publish.disclose_ai,
        )
        draft["summary_id"] = record.get("id", "")
        draft["version"] = record.get("version", 1)
        note_row = self.repo.publish_for_project(project_id)
        draft["note"] = publisher_core.row_view(note_row)
        draft["gates"] = self._publish_gates(
            draft, project=project, note=note_row or {}, confirmed=confirmed
        )
        return draft

    def publish_note(
        self,
        project_id: str,
        note_pk: str = "",
        summary_id: str = "",
        title: str = "",
        summary: str = "",
        content: str = "",
    ) -> dict[str, Any]:
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "项目不存在")
        if note_pk:
            # 草稿行是按 id 更新的。id 若属于另一个项目，这次「保存」会把那个项目的
            # 草稿标题/摘要/正文整份覆盖掉（D1：跨项目覆盖）。
            target = self.repo.publish_get(note_pk)
            if target and str(target.get("project_id") or "") != str(project_id):
                raise WBError("CONFLICT", "这份草稿属于另一个视频，不能在这里覆盖")
        record = self._publish_content(project_id, summary_id)
        draft = publisher_core.build_draft(
            project,
            record.get("content") or {},
            max_summary_chars=self.cfg.publish.max_summary_chars,
            disclose=self.cfg.publish.disclose_ai,
            title=title,
            summary_text=summary,
        )
        # 用户手改过正文时以他的为准：Delta 是他能看到的最终形态，
        # 拿 Markdown 覆盖回去等于把他的编辑丢掉。
        use_content = content.strip() or draft["content"]
        draft["content"] = use_content
        draft["content_text"] = publisher_core.plain_text(use_content)
        draft["chars"] = publisher_core.count_chars(draft["content_text"])
        if not draft["content_text"].strip():
            draft["warnings"].append("正文是空的")
        publisher_core.check_draft(draft)
        row = self.repo.publish_note_upsert(
            project_id,
            bvid=str(project.get("bvid") or ""),
            aid=project.get("aid"),
            title=draft["title"],
            summary=draft["summary"],
            content=use_content,
            note_pk=note_pk,
        )
        self.events.emit_now("publish.updated", {"project_id": project_id, "status": "draft"})
        view = publisher_core.row_view(row)
        return {
            "note": view,
            "draft": draft,
            "warnings": draft["warnings"],
            "gates": self._publish_gates(view, project=project, note=row, confirmed=True),
        }

    def publish_records(self, limit: int = 50) -> dict[str, Any]:
        """发过 / 撤过 / 发失败的记录（草稿不算，草稿在草稿列表里）。"""
        rows = self.repo.publish_records(limit=max(1, min(200, int(limit))))
        records = [publisher_core.record_view(row) for row in rows]
        counts = {"published": 0, "withdrawn": 0, "failed": 0}
        for row in records:
            key = str((row or {}).get("status") or "")
            if key in counts:
                counts[key] += 1
        return {"records": [row for row in records if row], "counts": counts, "total": len(rows)}

    def publish_list(self, project_id: str = "") -> dict[str, Any]:
        """左栏「草稿」列表：**只回草稿**。

        发过 / 撤过的行属于发布记录（`publish.records`），混进草稿列表会让人
        以为它们还能改——点开一看正文早就发出去了，改它也只改本地。
        """
        rows = [
            row
            for row in self.repo.publish_list(project_id)
            if str(row.get("status") or "draft") == "draft"
        ]
        return {
            "notes": [publisher_core.row_view(row) for row in rows],
            "limits": self._publish_limits(),
        }

    def publish_get(self, id: str = "", confirmed: bool = False) -> dict[str, Any]:
        row = self.repo.publish_get(id)
        if not row:
            raise WBError("NOT_FOUND", "草稿不存在")
        project = self.repo.project_get(str(row.get("project_id", ""))) or {}
        view = publisher_core.row_view(row)
        view["project_title"] = project.get("title", "")
        view["up_name"] = project.get("up_name", "")
        return {
            "note": view,
            "limits": self._publish_limits(),
            "gates": self._publish_gates(view, project=project, note=row, confirmed=confirmed),
        }

    def publish_delete(self, id: str = "") -> dict[str, Any]:
        removed = self.repo.publish_delete(id)
        if not removed:
            raise WBError("NOT_FOUND", "草稿不存在")
        self.events.emit_now("publish.updated", {"project_id": "", "status": "deleted"})
        return {"removed": removed}

    def publish_publish(self, id: str, confirmed: bool = False) -> dict[str, Any]:
        """把草稿发成笔记。这是全应用唯一不可撤销的动作。

        并发保护：拿到锁才算数。非阻塞——第二个请求立刻拿到 CONFLICT，
        而不是排队等着把同一份草稿再发一遍。
        """
        if not self._publish_lock.acquire(blocking=False):
            raise WBError("CONFLICT", "上一次发布还没返回，等它结束再试")
        try:
            return self._publish_publish_locked(id, confirmed)
        finally:
            self._publish_lock.release()

    def _publish_publish_locked(self, id: str, confirmed: bool = False) -> dict[str, Any]:
        row = self.repo.publish_get(id)
        if not row:
            raise WBError("NOT_FOUND", "草稿不存在")
        status = str(row.get("status") or "draft")
        if status == "published":
            raise WBError(
                "CONFLICT",
                f"这条已经发过了（note_id={row.get('note_id') or '未知'}）",
            )
        if not confirmed:
            raise WBError("CONFLICT", "还没勾「我已阅读并确认」，这条不发布")

        project = self.repo.project_get(str(row.get("project_id", ""))) or {}
        view = publisher_core.row_view(row)
        gates = self._publish_gates(view, project=project, note=row, confirmed=True)
        if not gates.get("ready"):
            blocked = [g for g in gates.get("gates", []) if g.get("status") != "ok"]
            reason = "；".join(
                f"{g.get('label')}：{g.get('reason') or g.get('status')}" for g in blocked
            )
            raise WBError(
                "CONFLICT",
                f"门禁没过，先处理这几项：{reason}",
                detail=json.dumps(gates, ensure_ascii=False),
            )

        if not row.get("aid"):
            raise WBError("CONFLICT", "这条草稿没有 aid，定位不到视频")

        try:
            client = self._gateway.client()
        except WBError as exc:
            raise WBError(exc.code, f"登录态用不了：{exc.message}", detail=exc.detail) from exc

        try:
            out = publisher_core.post_note(
                client,
                {
                    "aid": row.get("aid"),
                    "title": row.get("title"),
                    "summary": row.get("summary"),
                    "content": row.get("content"),
                },
                on_retry=lambda msg: self.events.emit_now("app.toast", {"level": "info", "text": msg}),
            )
        except WBError as exc:
            # 失败**保留草稿**：用户改两个字就能重发，不该让他从总结重来一遍
            self.repo.publish_set_status(id, "failed", error=exc.message)
            self.events.emit_now(
                "publish.updated", {"id": id, "status": "failed", "error": exc.message}
            )
            raise

        self.repo.publish_set_status(id, "published", note_id=out.get("note_id") or "")
        bvid = str(row.get("bvid") or "")
        self.events.emit_now("publish.updated", {"id": id, "status": "published"})
        return {
            "note": publisher_core.row_view(self.repo.publish_get(id)),
            "note_id": out.get("note_id") or "",
            "attempts": out.get("attempts", 1),
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        }

    def publish_withdraw(self, id: str) -> dict[str, Any]:
        """撤回已发布的笔记（按 note_id 删）。"""
        row = self.repo.publish_get(id)
        if not row:
            raise WBError("NOT_FOUND", "草稿不存在")
        if str(row.get("status") or "") != "published":
            raise WBError("CONFLICT", "只有已发布的笔记才能撤回")
        if not row.get("note_id"):
            raise WBError("CONFLICT", "这条记录没有 note_id，撤不回来（可能是发布时没拿到）")

        try:
            client = self._gateway.client()
        except WBError as exc:
            raise WBError(exc.code, f"登录态用不了：{exc.message}", detail=exc.detail) from exc

        publisher_core.withdraw_note(client, aid=row.get("aid"), note_id=str(row["note_id"]))
        self.repo.publish_set_status(id, "withdrawn")
        self.events.emit_now("publish.updated", {"id": id, "status": "withdrawn"})
        return {
            "note": publisher_core.row_view(self.repo.publish_get(id)),
            "note_id": str(row.get("note_id")),
        }

    def publish_regenerate(self, id: str, part: str = "all") -> dict[str, Any]:
        """按当前项目的最新总结重算标题/摘要（**不落库**）。

        用户点它通常是因为"AI 给的标题不满意，但正文我想留着"——所以默认只换
        标题与摘要，正文仍然用存下来的那份，除非他明确要整份重来。
        """
        row = self.repo.publish_get(id)
        if not row:
            raise WBError("NOT_FOUND", "草稿不存在")
        project = self.repo.project_get(str(row.get("project_id", ""))) or {}
        record = self.repo.summary_latest(str(row.get("project_id", "")))
        if not record:
            raise WBError("CONFLICT", "这个项目还没有总结，重新生成也没有素材")
        fresh = publisher_core.build_draft(
            project,
            record.get("content") or {},
            max_summary_chars=self.cfg.publish.max_summary_chars,
            disclose=self.cfg.publish.disclose_ai,
        )
        keep_content = str(row.get("content", "") or "")
        draft = fresh
        if part != "all" and keep_content.strip():
            # 只换指定的一小块，其余保留用户已经编辑过的版本
            draft = dict(fresh)
            draft["title"] = row.get("title") if part == "summary" else fresh["title"]
            draft["summary"] = row.get("summary") if part == "title" else fresh["summary"]
            text = publisher_core.plain_text(keep_content)
            draft["content"] = keep_content
            draft["content_text"] = text
            draft["chars"] = publisher_core.count_chars(text)
            draft["title_chars"] = len(str(draft["title"] or ""))
            draft["summary_chars"] = len(str(draft["summary"] or ""))
        draft["note"] = publisher_core.row_view(row)
        draft["summary_id"] = record.get("id", "")
        draft["version"] = record.get("version", 1)
        return {
            "draft": draft,
            "part": part,
            "gates": self._publish_gates(draft, project=project, note=row),
        }

    def _publish_gates(
        self,
        draft: dict[str, Any],
        *,
        project: dict[str, Any] | None = None,
        note: dict[str, Any] | None = None,
        confirmed: bool = False,
        logged_in: bool | None = None,
    ) -> dict[str, Any]:
        """算三道门禁的当前状态。

        只做本地能判断的部分：登录态读本地凭证（不发请求），"视频还能不能访问"
        要发布那一刻才知道，所以它给 ``todo``，界面上照实写"发布时再确认"。
        """
        project = project or {}
        bvid = str(project.get("bvid", "") or (note or {}).get("bvid", "") or "")
        published = self.repo.publish_published(bvid) if bvid else []
        published_note = next(
            (row for row in published if str(row.get("id", "")) != str((note or {}).get("id", ""))),
            None,
        )
        # 限流按**账号**算，不按视频算：B站 的 60 秒间隔是防连发，
        # 只盯同一个 bvid 的话，换个视频就能绕过去——那个限制等于没做。
        # 重复检查仍然只看同一个 bvid（上面那份 published）。
        last_publish_at = ""
        for row in self.repo.publish_published():
            stamp = str(row.get("published_at", "") or "")
            if stamp > last_publish_at:
                last_publish_at = stamp
        if logged_in is None:
            logged_in = self._gateway.is_logged_in()
        return publisher_core.gate_report(
            draft,
            project=project,
            note=note or {},
            published_note=published_note,
            last_publish_at=last_publish_at,
            summary_limit=self.cfg.publish.max_summary_chars,
            min_interval_seconds=self.cfg.publish.min_interval_seconds,
            confirmed=confirmed,
            logged_in=logged_in,
        )

    def _publish_limits(self) -> dict[str, int]:
        return {"title": publisher_core.TITLE_MAX, "summary": self.cfg.publish.max_summary_chars}


    # ------------------------------------------------------- 切片候选（P3-01/02/03）

    def _clip_client(self) -> tuple[Any, str]:
        """给候选生成用的模型客户端。

        没配模型**不报错**：这一层有规则兜底（按弹幕峰值切窗口），用户至少能看到
        "这几个位置还行"。返回一个"一叫就失败"的桩，让 `clipper.generate` 走兜底，
        而不是把整个功能锁在配置页后面。
        """
        try:
            client = self._llm_client("clip_analyze")
            client.require()  # 不 require 的话，没配 key 会在第一批请求里才炸 401
            return client, str(self.cfg.llm.model or "")
        except WBError as exc:
            if exc.code != "LLM_NOT_CONFIGURED":
                raise
            return _UnconfiguredClient(), ""

    def _vision_client(self) -> tuple[Any, str]:
        """给 P6-01 画面精析用的 vision 客户端。

        与 `_clip_client` 不同，这里**没配就返回 (None, "")**、由上层直接跳过：
        "精析"是加分项，没配 vision 模型时它应该安静地不工作，而不是拿一个
        纯文本模型去猜画面（那只会给出比字幕层更差的分数）。
        """
        model = str(getattr(self.cfg.llm.tasks, "vision", "") or "").strip()
        if not model:
            return None, ""
        try:
            client = self._llm_client("vision")
            client.require()
            return client, model
        except WBError as exc:
            if exc.code != "LLM_NOT_CONFIGURED":
                raise
            return None, ""

    def clip_analyze(self, project_id: str = "", kinds: list[str] | None = None,
                     count: int = 12, refine: bool | None = None) -> dict[str, Any]:
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        if not self.repo.transcript_get(project_id):
            raise WBError("CONFLICT", "这个项目还没有字幕，先转录再挑切片")
        wanted = [kind for kind in (kinds or list(clipper.RANGES)) if kind in clipper.RANGES]
        job = self.jobs.submit(
            "clip.analyze",
            project_id,
            {
                "project_id": project_id,
                "kinds": wanted or list(clipper.RANGES),
                "count": max(1, min(24, int(count or 12))),
                # False 表示"这次不要精析"；None 表示"按配置来"
                "refine": refine,
            },
        )
        return {"job": job}

    def _analyze_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        transcript = self.repo.transcript_get(project_id)
        segments = list((transcript or {}).get("segments") or [])
        if not segments:
            raise WBError("CONFLICT", "这个项目还没有字幕，先转录再挑切片")
        duration_ms = self._clip_duration(project_id)
        client, model = self._clip_client()
        ctx.log("开始挑候选")
        out = clipper.generate(
            segments,
            client=client,
            duration_ms=duration_ms,
            kinds=list(payload.get("kinds") or list(clipper.RANGES)),
            count=int(payload.get("count") or 12),
            model=model,
            on_progress=ctx.log,
        )
        if ctx.should_cancel():
            ctx.check_cancel()

        # P6-01 画面精析：只改分数/标题/理由，不动起止时间（见 clip_refine 模块说明）。
        # 开关优先级：payload.refine（用户这次点的） > 配置里有没有 vision 模型。
        # 精析自己的 notes 放最前面：候选阶段的吸附/补长说明常常有七八条，
        # 「为什么没精析」要是排在它们后面，界面里就被截掉了。
        extra: list[str] = []
        refined = 0
        audio_used = 0
        vision_model = ""
        flag = payload.get("refine")
        want_vision = bool(str(getattr(self.cfg.llm.tasks, "vision", "") or "").strip())
        if flag is None:
            flag = want_vision
            if not want_vision:
                # 静默跳过会让人以为"精析跑过了但没改动"，必须在 notes 里留一句
                extra.append("没配 vision 模型，跳过画面精析（分数只按字幕与弹幕热度算）")
        elif not flag:
            extra.append("按本次请求跳过了画面精析")
        if flag:
            client_v, vision_model = self._vision_client()
            source = next(
                (a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"), {}
            )
            packed = clip_refine.refine_candidates(
                out["candidates"],
                client=client_v,
                segments=segments,
                ffmpeg=getattr(self, "_ffmpeg", None),
                source_path=str(source.get("path") or ""),
                work_dir=Path(self.cfg.cache_root) / "tmp" / ctx.job_id,
                model=vision_model,
                on_progress=ctx.log,
                should_cancel=ctx.should_cancel,
            )
            out["candidates"] = packed["candidates"]
            refined = packed["refined"]
            audio_used = packed["audio"]
            extra.extend(packed["notes"])
            ctx.log(f"画面精析：改了 {refined} 条（失败 {packed['failed']} 条）")
        notes = extra + list(out["notes"])

        # 旧的 AI 候选让位，`locked` 与手动的保留（P3-04：改过的不能被重跑冲掉）
        cleared = self.repo.candidate_clear(project_id)
        rows = clipper.to_rows(
            out["candidates"], source="ai", origin=str(out["source"])
        )
        created = [self.repo.candidate_insert(project_id, **row) for row in rows]
        ctx.log(f"清掉旧候选 {cleared} 条，写入 {len(created)} 条")
        self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "analyze"})
        return {
            "project_id": project_id,
            "added": len(created),
            "cleared": cleared,
            "source": out["source"],
            "dropped": len(out["dropped"]),
            "notes": notes[:12],
            "model": out.get("model") or model,
            "refined": refined,
            "vision_model": vision_model,
            "audio_analyzed": audio_used,
        }

    def clip_candidates(self, id: str = "") -> dict[str, Any]:
        project = self.repo.project_get(id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        transcript = self.repo.transcript_get(id) or {}
        segments = list(transcript.get("segments") or [])
        # 磁吸线：句首/句尾都算一条，前端拖动时就往这些点上贴。
        # 在这里算而不是让前端自己扫字幕，是为了和 P3-02 的吸附用同一份坐标。
        edges = sorted(
            {
                int(seg.get("start_ms") or 0)
                for seg in segments
            }
            | {
                int(seg.get("end_ms") or 0)
                for seg in segments
            }
        )
        decorated = self._decorate(project)
        sources = [item for item in self.repo.asset_list(id) if item.get("kind") == "source"]
        source = sources[0] if sources else {}
        frame_ms = self._clip_frame_ms(id)
        return {
            "project": decorated,
            "duration_ms": self._clip_duration(id),
            # 界面算 ±1 帧、后端对齐切点都用这一个值，别让两边各猜一次
            "frame_ms": frame_ms,
            "fps": round(1000.0 / frame_ms, 3) if frame_ms else 0.0,
            "has_transcript": bool(segments),
            "boundaries": edges,
            # 前端要拿这个路径喂 <video>；没有源文件就返回空，界面提示"先把片子下下来"
            "source_path": str(source.get("path") or ""),
            # 竖屏要用：安全区坐标给预览画遮罩，focus 给时间轴按点
            "safe_area": reframe.safe_area(),
            "focus": self._load_focus(id),
            "candidates": self.repo.candidate_list(id),
        }

    def _clip_frame_ms(self, project_id: str) -> int:
        """一帧多少毫秒。

        P3-04 的验收是"切点误差 ≤ 1 帧"，而帧长只有片源知道（25/30/29.97/60 都常见）。
        探不到就按 40ms（25fps）算——误差上限仍是一帧，界面上也会写清是估算值。
        """
        project = self.repo.project_get(project_id)
        if not project:
            return 40
        sources = [item for item in self.repo.asset_list(project_id) if item.get("kind") == "source"]
        path = str((sources[0] if sources else {}).get("path") or "")
        if not path:
            return 40
        try:
            info = self._ffmpeg.probe(path)
        except Exception:  # noqa: BLE001 - 探测失败不该让界面挂掉
            return 40
        fps = float(getattr(info, "fps", 0) or 0)
        if fps <= 0.1:
            return 40
        return max(1, int(round(1000.0 / fps)))

    @staticmethod
    def _snap_frame(value: int, frame_ms: int) -> int:
        if frame_ms <= 0:
            return int(value)
        return int(round(int(value) / float(frame_ms))) * frame_ms

    def _clip_duration(self, project_id: str) -> int:
        """片长（毫秒）。

        优先用源视频探测到的时长；探不到（比如只有字幕、源文件被挪走）就退回
        "字幕最后一句的结束时间"——比 0 好得多：0 会让所有切点夹取失效，
        用户能拖到片尾之外去。
        """
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        probe = int(round(float(self._decorate(project).get("duration_sec") or 0) * 1000))
        if probe:
            return probe
        transcript = self.repo.transcript_get(project_id) or {}
        endings = [int(seg.get("end_ms") or 0) for seg in transcript.get("segments") or []]
        return max(endings) if endings else 0

    def clip_update(self, id: str = "", start_ms: int | None = None,
                    end_ms: int | None = None, title: str | None = None,
                    score: int | None = None, status: str = "",
                    align: str = "") -> dict[str, Any]:
        current = self.repo.candidate_get(id)
        if not current:
            raise WBError("NOT_FOUND", "找不到这个候选")
        duration = self._clip_duration(str(current["project_id"]))
        start = int(current["start_ms"] if start_ms is None else start_ms)
        end = int(current["end_ms"] if end_ms is None else end_ms)
        if align:
            if align != "frame":
                raise WBError("VALIDATION", "align 只支持 frame")
            frame_ms = self._clip_frame_ms(str(current["project_id"]))
            start = self._snap_frame(start, frame_ms)
            end = self._snap_frame(end, frame_ms)
        start = max(0, start)
        if duration:
            start = min(start, max(0, duration - 400))
            end = min(end, duration)
        if end - start < 400:
            raise WBError("VALIDATION", "切片至少要 0.4 秒")
        fields: dict[str, Any] = {}
        if (start, end) != (int(current["start_ms"]), int(current["end_ms"])):
            fields["start_ms"] = start
            fields["end_ms"] = end
            notes = dict(current.get("notes") or {})
            notes["edited_ms"] = True
            fields["notes"] = notes
        if title is not None:
            fields["title"] = str(title).strip()[:80]
        if score is not None:
            fields["score"] = max(0, min(100, int(score)))
        if status:
            if status not in ("draft", "locked", "rejected"):
                raise WBError("VALIDATION", "状态只能是 draft / locked / rejected")
            fields["status"] = status
        row = self.repo.candidate_update(id, **fields) if fields else current
        self.events.emit_now("clip.updated", {"project_id": row["project_id"], "reason": "update"})
        return {"candidate": row}

    def clip_create(self, project_id: str = "", kind: str = "highlight", start_ms: int = 0,
                    end_ms: int = 0, title: str = "", align: str = "") -> dict[str, Any]:
        duration = self._clip_duration(project_id)
        if kind not in clipper.RANGES:
            raise WBError("VALIDATION", "类型只能是 " + " / ".join(clipper.RANGES))
        start = int(start_ms)
        end = int(end_ms)
        if align:
            if align != "frame":
                raise WBError("VALIDATION", "align 只支持 frame")
            frame_ms = self._clip_frame_ms(project_id)
            start = self._snap_frame(start, frame_ms)
            end = self._snap_frame(end, frame_ms)
        start = max(0, start)
        if duration:
            start = min(start, max(0, duration - 400))
            end = min(end, duration)
        if end - start < 400:
            raise WBError("VALIDATION", "切片至少要 0.4 秒")
        row = self.repo.candidate_insert(
            project_id,
            kind=kind,
            start_ms=start,
            end_ms=end,
            title=str(title).strip()[:80] or "手动切片",
            reason="手动新建",
            source="manual",
            notes={"raw_start_ms": start, "raw_end_ms": end, "boundary": "", "manual": True},
        )
        self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "create"})
        return {"candidate": row}

    def clip_delete(self, id: str = "") -> dict[str, Any]:
        current = self.repo.candidate_get(id)
        if not current:
            raise WBError("NOT_FOUND", "找不到这个候选")
        self.repo.candidate_delete(id)
        self.events.emit_now(
            "clip.updated", {"project_id": current["project_id"], "reason": "delete"}
        )
        return {"deleted": id}

    # ------------------------------------------------------------ 切片导出

    # ------------------------------------------------------- 切片文案与封面（P3-08）

    def _candidate_or_404(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.repo.candidate_get(candidate_id)
        if not candidate:
            raise WBError("NOT_FOUND", "找不到这个切片候选")
        return candidate

    def _candidate_segments(self, candidate: dict[str, Any]) -> list[dict[str, Any]]:
        """候选区间内的字幕。区间里一句都没有（切点落在空隙）时退回全片。

        文案只需要"这段在讲什么"，不需要逐字对齐，所以宁可用全片也不能空着。
        """
        project_id = str(candidate.get("project_id") or "")
        transcript = self.repo.transcript_get(project_id) or {}
        segments = list(transcript.get("segments") or [])
        start = int(candidate.get("start_ms") or 0)
        end = int(candidate.get("end_ms") or 0)
        inside = [
            seg
            for seg in segments
            if int(seg.get("end_ms") or 0) > start and int(seg.get("start_ms") or 0) < end
        ]
        return inside or segments

    def _source_file(self, project_id: str) -> str:
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            raise WBError("CONFLICT", "这个项目还没有源视频，封面抽不了帧")
        return str(sources[0]["path"])

    def clip_copy(self, candidate_id: str = "", regenerate: bool = False) -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        existing = dict(candidate.get("copy") or {})
        if existing.get("title") and not regenerate:
            # 已有文案就直接回：白烧一次请求换一个"差不多"的标题没有意义，
            # 要重写必须显式 regenerate（界面上就是"重新生成"按钮）。
            return {"candidate_id": candidate_id, "copy": existing, "reused": True}
        job = self.jobs.submit(
            "clip.copy",
            str(candidate.get("project_id") or ""),
            {"candidate_id": candidate_id, "regenerate": bool(regenerate)},
        )
        return {"candidate_id": candidate_id, "job": job, "reused": False}

    def _copy_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id") or "")
        candidate = self._candidate_or_404(candidate_id)
        project = self.repo.project_get(str(candidate.get("project_id") or "")) or {}
        segments = self._candidate_segments(candidate)
        client, model = self._clip_client()
        ctx.log("开始写文案")
        out = copywriter.generate(
            candidate,
            segments,
            client=client,
            project_title=str(project.get("title") or ""),
            model=model,
            on_progress=ctx.log,
        )
        if ctx.should_cancel():
            ctx.check_cancel()
        old = dict(candidate.get("copy") or {})
        copy = dict(out["copy"])
        copy.update(
            {
                "source": out["source"],
                "model": out.get("model") or "",
                "notes": list(out.get("notes") or [])[:8],
                "edited": False,
            }
        )
        # 封面不动：重写文案不该把用户挑好的那一帧弄丢
        if old.get("cover"):
            copy["cover"] = old["cover"]
        else:
            got = self._grab_cover(candidate, log=ctx.log)
            if got:
                copy["cover"] = got
        self.repo.candidate_update(candidate_id, copy=copy)
        self.events.emit_now(
            "clip.updated",
            {
                "project_id": candidate.get("project_id"),
                "candidate_id": candidate_id,
                "reason": "copy",
            },
        )
        ctx.log(f"文案好了（{out['source']}）")
        return {
            "candidate_id": candidate_id,
            "copy": copy,
            "source": out["source"],
            "attempts": out.get("attempts") or 1,
        }

    def _grab_cover(self, candidate: dict[str, Any], *, at_ms: int | None = None,
                    log: Any = None) -> dict[str, Any]:
        """抽一张封面。失败只记一笔——没有封面不该让文案任务整体失败。"""
        try:
            project_id = str(candidate.get("project_id") or "")
            project = self.repo.project_get(project_id)
            if not project:
                return {}
            info = cover_core.grab_frame(
                self._ffmpeg,
                self._source_file(project_id),
                candidate,
                project.get("root_dir") or "",
                at_ms=at_ms,
            )
            info["source"] = "frame"
            return info
        except WBError as exc:
            if log:
                log(f"封面没抽出来：{exc.message}")
            return {}
        except Exception as exc:  # noqa: BLE001 - 抽帧/探测都可能抛
            if log:
                log(f"封面没抽出来：{type(exc).__name__}")
            return {}

    def clip_copy_save(self, candidate_id: str = "", title: str = "", description: str = "",
                       tags: list[str] | None = None) -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        copy = dict(candidate.get("copy") or {})
        copy["title"] = copywriter.cut(title, copywriter.TITLE_MAX)
        copy["description"] = copywriter.cut(description, copywriter.DESC_MAX)
        copy["tags"] = copywriter.normalize_tags(tags)
        copy["edited"] = True
        problems: list[str] = []
        if len(copy["title"]) < 4:
            problems.append("标题太短，发布前再写具体一点")
        if len(copy["tags"]) < copywriter.TAGS_MIN:
            problems.append(f"标签只有 {len(copy['tags'])} 个，至少 {copywriter.TAGS_MIN} 个")
        self.repo.candidate_update(candidate_id, copy=copy)
        self.events.emit_now(
            "clip.updated",
            {
                "project_id": candidate.get("project_id"),
                "candidate_id": candidate_id,
                "reason": "copy",
            },
        )
        return {"candidate_id": candidate_id, "copy": copy, "problems": problems}

    def clip_cover(self, candidate_id: str = "", at_ms: int = -1) -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        info = self._grab_cover(candidate, at_ms=None if at_ms < 0 else int(at_ms))
        if not info:
            raise WBError("PROCESS_FAILED", "封面没抽出来，检查源视频是否还在")
        copy = dict(candidate.get("copy") or {})
        copy["cover"] = info
        self.repo.candidate_update(candidate_id, copy=copy)
        self.events.emit_now(
            "clip.updated",
            {
                "project_id": candidate.get("project_id"),
                "candidate_id": candidate_id,
                "reason": "cover",
            },
        )
        return {"candidate_id": candidate_id, "cover": info, "copy": copy}

    def clip_cover_frames(self, candidate_id: str = "", count: int = 4) -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        project = self.repo.project_get(str(candidate.get("project_id") or ""))
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        out = cover_core.frames_for(
            self._ffmpeg,
            self._source_file(str(candidate.get("project_id") or "")),
            candidate,
            project.get("root_dir") or "",
            count=max(2, min(8, int(count or 4))),
        )
        if not out["frames"]:
            raise WBError("PROCESS_FAILED", "一帧都没抽出来，检查源视频是否还在")
        return {"candidate_id": candidate_id, **out}

    def clip_cover_upload(self, candidate_id: str = "", path: str = "") -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        project = self.repo.project_get(str(candidate.get("project_id") or ""))
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        dest = cover_core.cover_path(project.get("root_dir") or "", candidate_id)
        cover_core.copy_image(path, dest)
        info = {
            "path": str(dest),
            "at_ms": -1,
            "bytes": dest.stat().st_size,
            "source": "upload",
        }
        copy = dict(candidate.get("copy") or {})
        copy["cover"] = info
        self.repo.candidate_update(candidate_id, copy=copy)
        self.events.emit_now(
            "clip.updated",
            {
                "project_id": candidate.get("project_id"),
                "candidate_id": candidate_id,
                "reason": "cover",
            },
        )
        return {"candidate_id": candidate_id, "cover": info, "copy": copy}

    def _clip_export_inputs(
        self,
        project_id: str,
        candidate: dict[str, Any],
        *,
        mode: str = "precise",
        subtitle_mode: str = "none",
        aspect: str = "16x9",
        vertical_mode: str = "",
    ) -> dict[str, Any]:
        """导出一段要准备的东西：源文件、字幕、输出路径、文件名。"""
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            raise WBError("CONFLICT", "这个项目还没有源视频，先下载或导入再导出")
        root = Path(str(project["root_dir"]))
        out_dir = root / "export"
        suffix = exporter.mode_suffix(mode, subtitle_mode)
        if aspect == "9x16":
            # 竖屏要看得出是哪种出法：居中/模糊/跟随三选一，事后从文件名能认出来
            suffix += "-" + (vertical_mode or "center")
        name = exporter.clip_filename(
            project_title=project.get("title"),
            candidate_title=candidate.get("title"),
            start_ms=int(candidate["start_ms"]),
            end_ms=int(candidate["end_ms"]),
            aspect=aspect,
            suffix=suffix,
        )
        # 同参数再导一次不覆盖：文件名已经带上模式，重名就加 " (2)"
        return {
            "project": project,
            "source": str(sources[0]["path"]),
            "output": str(unique_path(out_dir / name)),
            "dir": str(out_dir),
        }

    def _clip_style_ass(self, project_id: str, style_id: str, *, height: int = 0,
                        start_ms: int = 0, end_ms: int = 0) -> str:
        """按指定样式把当前字幕重写成一份 ASS，返回路径。

        高度按导出目标给（竖屏 1920、横屏跟源）；探不到就按 720 基准走——
        字号缩放错了只是大小不合适，比"没有字幕"好得多。

        `start_ms/end_ms` 是**切片在源视频里的窗口**。烧字幕时 `-ss` 只作用于视频
        输入，成片时间轴从 0 开始；ASS 里还写着源时间（比如 3000 秒），libass 一条
        都匹配不上——任务显示"烧字幕"成功，画面上却一行字都没有。所以切片段要裁出
        窗口并平移回 0，文件名带起点，不同切片互不覆盖。
        """
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        row = self.repo.transcript_get(project_id) or {}
        items = subtitles.normalize(row.get("segments") or [])
        if not items:
            raise WBError("CONFLICT", "这个项目还没有字幕，先转录再导出")
        want = int(height or 0)
        if want <= 0:
            sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
            if sources:
                try:
                    want = int(self._ffmpeg.probe(str(sources[0]["path"])).height or 0)
                except WBError:
                    want = 0
            want = want or subtitles.ASS_BASE_HEIGHT
        shift_ms = int(start_ms or 0)
        if shift_ms > 0:
            items = subtitles.window(items, shift_ms, int(end_ms or 0))
            if not items:
                raise WBError("CONFLICT", "这个切片的时间窗内没有字幕，换切点或去掉烧字幕")
        style, _builtin, _file = self._style_resolve(style_id)
        body = subtitles.format_ass(items, style=style, height=want)
        # 用毫秒而不是秒：两条切片起点只差半秒也会生成不同文件，不会互相覆盖
        name = (f"subtitle.{style['id']}.ass" if not shift_ms
                else f"subtitle.{style['id']}@{shift_ms}ms.ass")
        target = Path(str(project["root_dir"])) / "export" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return str(target)

    def clip_export(self, project_id: str = "", candidate_id: str = "", mode: str = "precise",
                    subtitle_mode: str = "none", aspect: str = "16x9",
                    vertical_mode: str = "center", crop_x: float = 0.5,
                    style_id: str = "", bgm: str = "", bgm_volume: float = -1,
                    bgm_duck: bool | None = None, fade_ms: int = -1) -> dict[str, Any]:
        candidate = self.repo.candidate_get(candidate_id)
        if not candidate:
            raise WBError("NOT_FOUND", "找不到这个候选")
        if project_id and str(candidate["project_id"]) != project_id:
            raise WBError("VALIDATION", "候选不属于这个项目")
        payload = self._clip_export_plan(
            str(candidate["project_id"]),
            candidate,
            mode=mode,
            subtitle_mode=subtitle_mode,
            aspect=aspect,
            vertical_mode=vertical_mode,
            crop_x=crop_x,
            style_id=style_id,
            bgm=bgm,
            bgm_volume=bgm_volume,
            bgm_duck=bgm_duck,
            fade_ms=fade_ms,
        )
        return {"job": self.jobs.submit("clip.export", str(candidate["project_id"]), payload)}

    def _clip_export_plan(
        self,
        project_id: str,
        candidate: dict[str, Any],
        *,
        mode: str = "precise",
        subtitle_mode: str = "none",
        aspect: str = "16x9",
        vertical_mode: str = "center",
        crop_x: float = 0.5,
        style_id: str = "",
        batch_id: str = "",
        bgm: str = "",
        bgm_volume: float = -1,
        bgm_duck: bool | None = None,
        fade_ms: int = -1,
    ) -> dict[str, Any]:
        """一段切片的导出计划（= 一个 ``clip.export`` 任务的 payload）。

        单条导出与批量导出走同一份，避免"批量那个忘了校验某个模式"这类分叉——
        计划错了，两个入口一起错，测试也只测一处。
        """
        if mode not in exporter.EXPORT_MODES:
            raise WBError("VALIDATION", "导出模式只能是 precise / fast")
        if subtitle_mode not in exporter.SUBTITLE_MODES:
            raise WBError("VALIDATION", "字幕处理只能是 none / hard / soft")
        try:
            # 用户写 9:16、界面写 9x16，两种都得认；往下只走规范值，
            # 文件名、数据库、ffmpeg 分支才不会再出现两套写法。
            aspect = exporter.normalize_aspect(aspect)
        except ValueError as exc:
            raise WBError("VALIDATION", str(exc)) from exc
        if vertical_mode not in reframe.VERTICAL_MODES:
            raise WBError("VALIDATION", "竖屏模式只能是 center / blur / follow")
        planned = self._clip_export_inputs(
            project_id,
            candidate,
            mode=mode,
            subtitle_mode=subtitle_mode,
            aspect=aspect,
            vertical_mode=vertical_mode,
        )
        if self._ffmpeg.ffmpeg is None:
            raise WBError("DEPENDENCY_MISSING", "缺少 ffmpeg，导出需要它")

        # P6-03：BGM 必须是真文件；路径不存在直接报错，不要等 ffmpeg 跑到一半才炸
        audio_path = str(bgm or "").strip()
        if audio_path:
            candidate_path = Path(audio_path).expanduser()
            if not candidate_path.is_file():
                raise WBError("NOT_FOUND", "找不到这个背景音乐文件", audio_path)
            audio_path = str(candidate_path.resolve())
        span_ms = max(1, int(candidate["end_ms"]) - int(candidate["start_ms"]))

        subtitle = ""
        if subtitle_mode != "none":
            subs = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "subtitle"]
            if subtitle_mode == "hard":
                # 硬烧一律**现生成 ASS**：样式（字号/描边/位置）要跟着设置走，
                # 拿上次导出的 subtitle.ass 凑数会烧出旧样式；而且 libass 读不了
                # srt/vtt——直接喂 ass= 报 "Could not create a libass track"。
                # 没有转录但有字幕文件的（导入视频 + 外部 srt）退化成原始文件，
                # 由 ffmpeg 的 subtitles= 滤镜处理。
                try:
                    subtitle = self._clip_style_ass(
                        project_id,
                        style_id,
                        height=1920 if aspect == "9x16" else 0,
                        start_ms=int(candidate["start_ms"]),
                        end_ms=int(candidate["end_ms"]),
                    )
                except WBError:
                    picks = [
                        a for a in subs
                        if str(a.get("path", "")).lower().endswith((".ass", ".ssa"))
                    ] or subs
                    subtitle = str(picks[0]["path"]) if picks else ""
                if not subtitle:
                    raise WBError("CONFLICT", "这个项目还没有字幕，先转录或导出字幕再烧录")
            else:
                # 软字幕优先 SRT（mov_text 不认 ASS 样式）
                picks = [a for a in subs if str(a.get("path", "")).lower().endswith(".srt")]
                picks = picks or subs
                if picks:
                    subtitle = str(picks[0]["path"])
                else:
                    # 和硬字幕一样，能从转录正文现写一份。导入视频 + 自己补转录的
                    # 项目磁盘上从来没有 srt，这里再要用户"先导出字幕"就多余了。
                    items = subtitles.window(
                        subtitles.normalize(
                            list((self.repo.transcript_get(project_id) or {}).get("segments") or [])),
                        int(candidate["start_ms"]), int(candidate["end_ms"]))
                    if not items:
                        raise WBError("CONFLICT", "这个项目还没有字幕，先转录或导出字幕再挂软字幕")
                    project = self.repo.project_get(project_id) or {}
                    target = (Path(str(project.get("root_dir") or "")) / "export"
                              / f"subtitle@{int(candidate['start_ms'])}ms.srt")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(format_srt(items), encoding="utf-8")
                    subtitle = str(target)

        payload = {
            "project_id": project_id,
            "candidate_id": str(candidate["id"]),
            "source": planned["source"],
            "output": planned["output"],
            "start_ms": int(candidate["start_ms"]),
            "end_ms": int(candidate["end_ms"]),
            "mode": mode,
            "subtitle": subtitle,
            "subtitle_mode": subtitle_mode,
            "aspect": aspect,
            "vertical_mode": vertical_mode,
            "crop_x": reframe.clamp_x(crop_x),
            "style_id": style_id,
            "bgm": audio_path,
            "bgm_volume": audio_mix.clamp_volume(
                bgm_volume if bgm_volume >= 0 else self.cfg.clip.bgm_volume
            ),
            "bgm_duck": bool(
                self.cfg.clip.bgm_duck if bgm_duck is None else bgm_duck
            ),
            "fade_ms": audio_mix.clamp_fade_ms(
                span_ms, fade_ms if fade_ms >= 0 else self.cfg.clip.fade_ms
            ),
        }
        if batch_id:
            payload["batch_id"] = batch_id
        return payload

    @staticmethod
    def _export_signature(job: dict[str, Any]) -> tuple[str, ...]:
        """判断"这条候选已经用同样的参数导过了"要看的字段。

        P6-03 加了 BGM 与淡入淡出：换了配乐就是另一版片子，不能被 skip_exported 当成
        "已经导过"直接跳过。
        """
        body = job.get("payload") or {}
        return (
            str(body.get("candidate_id") or ""),
            str(body.get("mode") or ""),
            str(body.get("subtitle_mode") or ""),
            str(body.get("aspect") or ""),
            str(body.get("vertical_mode") or ""),
            str(body.get("bgm") or ""),
            str(int(body.get("fade_ms") or 0)),
        )

    def _export_estimate(self, project_id: str, candidates: list[dict[str, Any]]) -> int:
        """粗估这批导出要写多少字节：源文件大小 × 时长占比 × 1.2。

        估不准是常态（crf 模式体积取决于画面复杂度），宁可略高——磁盘守卫是**拒绝**
        而不是警告，估低了会在导出中途写满盘，那才是真事故。
        """
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            return 0
        source = sources[0]
        size = int(source.get("bytes") or 0)
        try:
            total = int(self._ffmpeg.probe(str(source.get("path"))).duration_ms or 0)
        except WBError:
            total = 0
        if size <= 0 or total <= 0:
            return 0
        span = sum(max(0, int(c["end_ms"]) - int(c["start_ms"])) for c in candidates)
        return int(size * span / total * 1.2) + 1_000_000

    def clip_export_batch(self, project_id: str = "", candidate_ids: list[str] | None = None,
                          mode: str = "precise", subtitle_mode: str = "none",
                          aspect: str = "16x9", vertical_mode: str = "center",
                          crop_x: float = 0.5, style_id: str = "",
                          skip_exported: bool = False,
                          bgm: str = "", bgm_volume: float = -1,
                          bgm_duck: bool | None = None,
                          fade_ms: int = -1) -> dict[str, Any]:
        """多选切片一起导出：每条一个 ``clip.export`` 任务，共用同一个 ``batch_id``。

        并发靠 ``clip.export`` 的信号量（2），这里不排第二层队；一条失败不影响其他，
        界面按行重试。
        """
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        ids: list[str] = []
        for raw in candidate_ids or []:
            text = str(raw or "").strip()
            if text and text not in ids:  # 重复勾选不算两条
                ids.append(text)
        if not ids:
            raise WBError("VALIDATION", "先勾几条切片再批量导出")

        done = set()
        if skip_exported:
            for job in self.repo.job_list(limit=500, project_id=project_id):
                if job.get("type") != "clip.export" or job.get("status") != "succeeded":
                    continue
                path = str((job.get("result") or {}).get("path") or "")
                if path and Path(path).is_file():
                    done.add(self._export_signature(job))

        picked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for candidate_id in ids:
            candidate = self.repo.candidate_get(candidate_id)
            if not candidate:
                skipped.append({"candidate_id": candidate_id, "reason": "候选不存在"})
                continue
            if str(candidate["project_id"]) != project_id:
                skipped.append({"candidate_id": candidate_id, "reason": "候选不属于这个项目"})
                continue
            # 和计划函数保持同一口径：BGM 一律换算成解析后的绝对路径，
            # 否则"已经导过"永远匹配不上（计划里存的是绝对路径）
            bgm_sig = ""
            if str(bgm or "").strip():
                bgm_sig = str(Path(str(bgm).strip()).expanduser().resolve())
            sig = (
                candidate_id, mode, subtitle_mode, aspect, vertical_mode,
                bgm_sig,
                str(audio_mix.clamp_fade_ms(
                    max(1, int(candidate["end_ms"]) - int(candidate["start_ms"])),
                    self.cfg.clip.fade_ms if fade_ms < 0 else fade_ms,
                )),
            )
            if skip_exported and sig in done:
                skipped.append({"candidate_id": candidate_id, "reason": "这条已经导出过（同参数）"})
                continue
            picked.append(candidate)
        if not picked:
            return {
                "batch_id": "",
                "jobs": [],
                "skipped": skipped,
                "submitted": 0,
                "estimate_bytes": 0,
            }

        out_dir = Path(str(project["root_dir"])) / "export"
        # 磁盘守卫（方案书 §6.9）：2 倍预估 + 2GB，不够就不开工
        estimate = self._export_estimate(project_id, picked)
        need = estimate * 2 + 2 * 1024**3 if estimate else 2 * 1024**3
        ensure_free_space(out_dir, need)

        payloads: list[dict[str, Any]] = []
        batch_id = new_id("batch")
        for candidate in picked:
            payloads.append(
                self._clip_export_plan(
                    project_id,
                    candidate,
                    mode=mode,
                    subtitle_mode=subtitle_mode,
                    aspect=aspect,
                    vertical_mode=vertical_mode,
                    crop_x=crop_x,
                    style_id=style_id,
                    batch_id=batch_id,
                    bgm=bgm,
                    bgm_volume=bgm_volume,
                    bgm_duck=bgm_duck,
                    fade_ms=fade_ms,
                )
            )
        jobs = self.jobs.submit_batch("clip.export", project_id, payloads, batch_id=batch_id)
        return {
            "batch_id": batch_id,
            "jobs": [
                {
                    "candidate_id": str(job.get("payload", {}).get("candidate_id") or ""),
                    "job_id": job.get("id"),
                    "name": Path(str(job.get("payload", {}).get("output") or "")).name,
                }
                for job in jobs
            ],
            "skipped": skipped,
            "submitted": len(jobs),
            "estimate_bytes": estimate,
        }

    def clip_exports(self, project_id: str = "") -> dict[str, Any]:
        """这个项目的导出队列：**任务状态** + 产出文件是否还在。

        队列状态只有任务知道（排队/导出中/完成/失败/取消），资产表只记得"哪条文件
        登记过"。两边合起来才是一行完整的「导出记录」：模式、切点偏移、时长都在
        任务结果里。
        """
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        assets = {
            str(a.get("path") or ""): a
            for a in self.repo.asset_list(project_id)
            if a.get("kind") == "export"
        }
        out = []
        for job in self.repo.job_list(limit=200, project_id=project_id):
            if job.get("type") != "clip.export":
                continue
            result = job.get("result") or {}
            path = str(result.get("path") or "")
            asset = assets.get(path) or {}
            out.append(
                {
                    "job_id": job.get("id"),
                    "status": job.get("status"),
                    "candidate_id": str((job.get("payload") or {}).get("candidate_id") or ""),
                    "path": path,
                    "name": Path(path).name if path else "",
                    "bytes": int(result.get("bytes") or asset.get("bytes") or 0),
                    "duration_ms": int(result.get("duration_ms") or 0),
                    "expected_ms": int(result.get("expected_ms") or 0),
                    "offset_ms": int(result.get("offset_ms") or 0),
                    "mode": str(result.get("mode") or ""),
                    "subtitle_mode": str(result.get("subtitle_mode") or ""),
                    # 竖屏标记：队列里要能分清 16:9 和 9:16，别把竖屏当成原画幅
                    "aspect": str(
                        result.get("aspect")
                        or (job.get("payload") or {}).get("aspect")
                        or "16x9"
                    ),
                    "vertical_mode": str(
                        result.get("vertical_mode")
                        or (job.get("payload") or {}).get("vertical_mode")
                        or ""
                    ),
                    "note": str(result.get("note") or ""),
                    "error": str(job.get("error_message") or ""),
                    # 批量导出的一组任务共用一个 batch_id：界面据此显示"这批 3 条 · 2 成功 / 1 失败"
                    "batch_id": str((job.get("payload") or {}).get("batch_id") or ""),
                    "exists": bool(path) and Path(path).is_file(),
                    "created_at": job.get("created_at"),
                }
            )
        return {"exports": out}

    def _clip_export_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "precise")
        subtitle_mode = str(payload.get("subtitle_mode") or "none")
        source = str(payload.get("source") or "")
        output = str(payload.get("output") or "")
        start_ms = int(payload.get("start_ms") or 0)
        end_ms = int(payload.get("end_ms") or 0)
        if not Path(source).is_file():
            raise WBError("NOT_FOUND", "源视频文件不在了", source)
        expected = max(1, end_ms - start_ms)
        aspect = str(payload.get("aspect") or "16x9")
        vertical_mode = str(payload.get("vertical_mode") or "center")
        crop_x = reframe.clamp_x(payload.get("crop_x"))
        ctx.progress(0.1)
        line = ("精确重编码" if mode == "precise" else "快速切（关键帧对齐）")
        line += "，烧字幕" if subtitle_mode == "hard" else "，带软字幕" if subtitle_mode == "soft" else ""
        if aspect == "9x16":
            label = {"center": "居中", "blur": "模糊背景", "follow": "人脸跟随"}.get(
                vertical_mode, vertical_mode
            )
            line += "，竖屏·" + label
        ctx.log(line)
        if payload.get("bgm"):
            ctx.log(
                "混入背景音乐：" + Path(str(payload.get("bgm"))).name
                + ("（人声出现时自动压低）" if payload.get("bgm_duck", True) else "")
            )
        if int(payload.get("fade_ms") or 0) > 0:
            ctx.log("淡入淡出 " + str(int(payload.get("fade_ms"))) + " ms")

        src_crop = None
        focus: list[dict[str, Any]] = []
        focus_cuts: list[int] = []
        if aspect == "9x16":
            src_crop = self._src_crop(payload.get("project_id"), source, log=ctx.log)
            if vertical_mode == "follow":
                focus = self._load_focus(payload.get("project_id"))
                # 切点要一起带上：裁剪表达式靠它决定"哪儿该一瞬间换过去"
                meta = _load_json(self._focus_path(payload.get("project_id")))
                if isinstance(meta, dict):
                    for value in meta.get("cuts") or []:
                        try:
                            focus_cuts.append(int(value))
                        except (TypeError, ValueError):
                            continue
                if not focus:
                    # 没有轨迹就退回居中：宁可出一条正的片子，也不出偏的
                    ctx.log("没有跟随轨迹，这段按居中处理")
                    vertical_mode = "center"

        try:
            self._ffmpeg.export_clip(
                source,
                output,
                start_ms=start_ms,
                end_ms=end_ms,
                mode=mode,
                subtitle=str(payload.get("subtitle") or ""),
                subtitle_mode=subtitle_mode,
                aspect=aspect,
                vertical_mode=vertical_mode,
                crop_x=crop_x,
                focus=focus,
                duration_ms=expected,
                src_crop=src_crop,
                focus_cuts=focus_cuts,
                bgm=str(payload.get("bgm") or ""),
                bgm_volume=float(payload.get("bgm_volume") or audio_mix.BGM_VOLUME),
                bgm_duck=bool(payload.get("bgm_duck", True)),
                fade_ms=int(payload.get("fade_ms") or 0),
                **ctx.guard_kwargs(),
            )
        except WBError:
            # 被杀掉的 ffmpeg 会留下一个"半截 mp4"：播放器打不开、体积还不小，
            # 用户会以为导出成功了。取消/失败一律删掉，别让他去猜。
            try:
                Path(output).unlink(missing_ok=True)
            except OSError:
                pass
            raise
        ctx.progress(0.85)
        info = self._ffmpeg.probe(output)
        actual = int(getattr(info, "duration_ms", 0) or 0)
        delta = exporter.duration_delta_ms(expected, actual)
        note = exporter.offset_note(mode, delta)
        if note:
            ctx.log(note)
        size = Path(output).stat().st_size
        # wb_asset 没有 notes 列；模式/偏移这类信息存在任务结果里（clip.exports 合并两边）
        asset = self.repo.asset_upsert(
            str(payload.get("project_id") or ""),
            "export",
            output,
            bytes=size,
            status="ready",
            duration_ms=actual,
        )
        self.events.emit_now(
            "clip.updated", {"project_id": payload.get("project_id"), "reason": "export"}
        )
        return {
            "asset_id": asset.get("id") if isinstance(asset, dict) else "",
            "path": output,
            "name": Path(output).name,
            "bytes": size,
            "duration_ms": actual,
            "expected_ms": expected,
            "offset_ms": abs(delta),
            "delta_ms": delta,
            "mode": mode,
            "subtitle_mode": subtitle_mode,
            "aspect": aspect,
            "vertical_mode": vertical_mode,
            "crop_x": crop_x,
            "src_crop": src_crop,
            # 跟随退居中时 vertical_mode 已经改成 center，这里把"实际用了几个人脸点"写出来，
            # 队列与验收脚本才分得清"真跟随"和"没轨迹退回居中"
            "focus_points": len(focus),
            "note": note,
            "audio": audio_mix.describe(
                bgm_name=Path(str(payload.get("bgm") or "")).name,
                bgm=bool(payload.get("bgm")),
                volume=payload.get("bgm_volume"),
                duck=bool(payload.get("bgm_duck", True)),
                fade_ms=payload.get("fade_ms"),
                span_ms=expected,
            ),
        }

    # ---------------------------------------------------------------- 交付（P6-04）

    def nle_info(self) -> dict[str, Any]:
        """三个通道各是什么、本机有没有对应的软件。只读探测，不启动它们。"""
        apps = nle.detect_apps()
        notes = {
            "jianying": (
                "已检测到剪映草稿箱，可以直接放进去"
                if apps["jianying"]["found"]
                else "没检测到剪映草稿箱，草稿会写到项目的 export/jianying 下"
            ),
            "otio": (
                "已检测到达芬奇，用「文件 → 导入 → 时间线」打开 .otio"
                if apps["resolve"]["found"]
                else "没检测到达芬奇；.otio 是标准 OpenTimelineIO，别的剪辑软件也能读"
            ),
            "bundle": "素材 + 字幕 + 文案 + 说明，手动拖进任何剪辑软件",
        }
        return {
            "channels": [
                {
                    "key": key,
                    "label": nle.CHANNEL_LABELS[key],
                    "note": notes[key],
                    "ready": True,
                }
                for key in nle.CHANNELS
            ],
            "apps": apps,
        }

    def clip_deliver(self, project_id: str = "", candidate_id: str = "",
                     channel: str = "jianying",
                     place_in_app: bool = False) -> dict[str, Any]:
        candidate = self._candidate_or_404(candidate_id)
        if project_id and str(candidate["project_id"]) != project_id:
            raise WBError("VALIDATION", "候选不属于这个项目")
        target = nle.check_channel(channel)
        payload = {
            "project_id": str(candidate["project_id"]),
            "candidate_id": candidate_id,
            "channel": target,
            "place_in_app": bool(place_in_app),
        }
        return {
            "job": self.jobs.submit("clip.deliver", str(candidate["project_id"]), payload),
            "channel": target,
        }

    def _deliver_clip_file(self, candidate: dict[str, Any]) -> tuple[str, bool]:
        """降级包里放什么视频：优先这段已经导出的成片，没有就用源视频。

        返回 `(路径, 是否成片)`。成片是"按当前参数剪好、可能烧了字幕"的那一版；
        源视频是整段，长得多——所以降级包的说明里必须写清放的是哪一种。
        """
        candidate_id = str(candidate.get("id") or "")
        best = ""
        best_at = ""
        for job in self.repo.job_list(limit=300, project_id=str(candidate.get("project_id") or "")):
            if str(job.get("type") or "") != "clip.export":
                continue
            body = job.get("payload") or {}
            if str(body.get("candidate_id") or "") != candidate_id:
                continue
            result = job.get("result") or {}
            path = str(result.get("path") or "")
            if not path or not Path(path).is_file():
                continue
            stamp = str(job.get("finished_at") or job.get("created_at") or "")
            if not best or stamp >= best_at:
                best, best_at = path, stamp
        if best:
            return best, True
        sources = [
            a for a in self.repo.asset_list(str(candidate.get("project_id") or ""))
            if a.get("kind") == "source"
        ]
        return (str(sources[0]["path"]) if sources else ""), False

    def _subtitle_file(self, project_id: str) -> str:
        """降级包里那份字幕。没有 srt 资产但有转录正文时现写一份。

        正文是转录段，不是某个具体文件：导入视频 + 自己补转录的项目磁盘上从来没
        存过 srt，这时让包缺字幕，用户还得回应用里再导一次。
        """
        subs = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "subtitle"]
        picks = [a for a in subs if str(a.get("path", "")).lower().endswith(".srt")] or subs
        for item in picks:
            path = Path(str(item.get("path") or ""))
            if path.is_file():
                return str(path)
        segments = list((self.repo.transcript_get(project_id) or {}).get("segments") or [])
        if not segments:
            return ""
        root = str((self.repo.project_get(project_id) or {}).get("root_dir") or "")
        if not root:
            return ""
        dest = Path(root) / "transcript.srt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(format_srt(segments), encoding="utf-8")
        return str(dest)

    def _deliver_files(self, candidate: dict[str, Any]) -> list[tuple[str, str]]:
        """降级包要装的文件：视频 + 字幕 + 封面。文案单独写，说明单独写。"""
        files: list[tuple[str, str]] = []
        clip_path, _is_export = self._deliver_clip_file(candidate)
        if clip_path:
            suffix = Path(clip_path).suffix or ".mp4"
            files.append((clip_path, "片段" + suffix))
        sub_path = self._subtitle_file(str(candidate.get("project_id") or ""))
        if sub_path:
            files.append((sub_path, nle.BUNDLE_SUBTITLE))
        cover = (candidate.get("copy") or {}).get("cover") or {}
        cover_path = str(cover.get("path") or "") if isinstance(cover, dict) else ""
        if cover_path and Path(cover_path).is_file():
            files.append((cover_path, "封面" + (Path(cover_path).suffix or ".jpg")))
        return files

    def _clip_deliver_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        channel = nle.check_channel(payload.get("channel") or "jianying")
        project_id = str(payload.get("project_id") or "")
        candidate = self._candidate_or_404(str(payload.get("candidate_id") or ""))
        project = self.repo.project_get(project_id) or {}
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            raise WBError("CONFLICT", "这个项目还没有源视频，先下载或导入")
        source = str(sources[0]["path"])
        if not Path(source).is_file():
            raise WBError("NOT_FOUND", "源视频文件不在了", source)

        start_ms = int(candidate.get("start_ms") or 0)
        end_ms = int(candidate.get("end_ms") or 0)
        span = max(1, end_ms - start_ms)
        name = nle.safe_draft_name(
            f"{project.get('title') or '项目'}_{candidate.get('title') or '切片'}"
        )
        ctx.log(f"交付通道：{nle.CHANNEL_LABELS[channel]}")
        ctx.progress(0.1)

        info = self._ffmpeg.probe(source)
        width = int(getattr(info, "width", 0) or 0)
        height = int(getattr(info, "height", 0) or 0)
        duration_ms = int(getattr(info, "duration_ms", 0) or 0)
        fps = float(getattr(info, "fps", 0) or 0) or 30.0
        notes: list[str] = []
        result: dict[str, Any] = {
            "channel": channel,
            "label": nle.CHANNEL_LABELS[channel],
            "candidate_id": str(candidate.get("id") or ""),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "subtitle_style_followed": False,
        }

        if channel == "jianying":
            content_tpl = nle.load_template(nle.DRAFT_CONTENT_TEMPLATE)
            meta_tpl = nle.load_template(nle.DRAFT_META_TEMPLATE)
            root = nle.find_jianying_root()
            placed = bool(root and payload.get("place_in_app"))
            base = root if placed else Path(str(project.get("root_dir") or "")) / "export" / "jianying"
            folder = unique_path(Path(str(base)) / name)
            subtitles = self._candidate_segments(candidate)
            content = nle.jianying_document(
                content_tpl,
                name=folder.name,
                video_path=source,
                source_duration_ms=duration_ms,
                width=width,
                height=height,
                start_ms=start_ms,
                end_ms=end_ms,
                fps=fps,
                subtitles=subtitles,
            )
            ctx.progress(0.5)
            meta = nle.jianying_meta(
                meta_tpl, name=folder.name, folder=folder,
                created_ms=int(time.time() * 1000),
            )
            written = nle.write_jianying(folder, content, meta)
            if placed:
                notes.append(f"已放进剪映草稿箱：打开剪映就能看到「{folder.name}」")
            elif payload.get("place_in_app"):
                notes.append("没检测到剪映草稿箱，草稿放在本项目的 export/jianying 下，可手动拷进去")
            else:
                notes.append("草稿放在本项目的 export/jianying 下，拷进剪映草稿箱即能打开")
            notes.append("字幕样式不跟随：草稿里的字幕用中性样式，进剪映后自己调一次")
            result.update(
                {
                    "path": written["dir"],
                    "dir": written["dir"],
                    "files": written["files"],
                    "bytes": written["bytes"],
                    "placed_in_app": placed,
                    "jianying_root": str(root or ""),
                }
            )
        elif channel == "otio":
            out_dir = Path(str(project.get("root_dir") or "")) / "export" / "nle"
            target = unique_path(out_dir / f"{name}.otio")
            doc = nle.otio_document(
                name=target.stem,
                video_path=source,
                start_ms=start_ms,
                end_ms=end_ms,
                width=width,
                height=height,
                source_duration_ms=duration_ms,
            )
            ctx.progress(0.5)
            written = nle.write_otio(target, doc)
            notes.append("用达芬奇「文件 → 导入 → 时间线」选这个 .otio；素材按原路径引用，别挪源文件")
            notes.append("字幕样式不跟随：OTIO 只带画面与时长，字幕请在成片里烧好或单独导入")
            result.update(
                {"path": written["path"], "files": [written["path"]], "bytes": written["bytes"]}
            )
        else:
            files = self._deliver_files(candidate)
            clip_path, is_export = self._deliver_clip_file(candidate)
            folder = unique_path(
                Path(str(project.get("root_dir") or "")) / "export" / "bundle" / name
            )
            copy = dict(candidate.get("copy") or {})
            copy_text = copywriter.render_text(copy) if hasattr(copywriter, "render_text") else ""
            if not copy_text:
                lines = [str(copy.get("title") or "").strip()]
                if str(copy.get("description") or "").strip():
                    lines.append("")
                    lines.append(str(copy.get("description")).strip())
                tags = copy.get("tags") or []
                if tags:
                    lines.append("")
                    lines.append("标签：" + " ".join("#" + str(t) for t in tags))
                copy_text = "\n".join([line for line in lines if line is not None]).strip()
            note_text = nle.bundle_notes(
                project_title=str(project.get("title") or "项目"),
                clip_title=str(candidate.get("title") or "切片"),
                start_ms=start_ms,
                end_ms=end_ms,
                files=[dest for _src, dest in files],
            )
            ctx.progress(0.5)
            written = nle.write_bundle(
                folder, files=files, notes=note_text, copy_text=copy_text
            )
            if not is_export:
                notes.append("这段还没导出成片，包里放的是整段源视频（时长比切片长）")
            notes.append("按包里的「导入说明.txt」拖进剪映/达芬奇即可；字幕样式不跟随")
            result.update(
                {
                    "path": written["dir"],
                    "dir": written["dir"],
                    "files": written["files"],
                    "bytes": written["bytes"],
                    "source_is_export": is_export,
                }
            )

        ctx.progress(0.9)
        self.repo.asset_upsert(
            project_id,
            "export",
            str(result["path"]),
            bytes=int(result.get("bytes") or 0),
            status="ready",
            duration_ms=0 if channel != "otio" else span,
        )
        self.events.emit_now(
            "clip.updated", {"project_id": project_id, "reason": "deliver"}
        )
        for note in notes:
            ctx.log(note)
        result["notes"] = notes
        return result

    # ---------------------------------------------------------------- 竖屏

    def _project_root(self, project_id: str) -> Path:
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        return Path(str(project["root_dir"]))

    def _src_crop(self, project_id: str, source: str, *, log=None) -> dict[str, int] | None:
        """素材自带黑边要不要削。结果按"路径+大小+修改时间"缓存，探一次能用很多次。

        探测失败只是"不削"，不该拦住导出——所以任何异常都吞掉、记一行日志。
        缓存文件 `black_bars.json` 放在项目目录，删了会重新探。
        """
        ffmpeg_bin = self._ffmpeg.ffmpeg
        if not ffmpeg_bin:
            return None
        try:
            stat = Path(source).stat()
        except OSError:
            return None
        cache_path = self._project_root(project_id) / "black_bars.json"
        stamp = f"{Path(source).name}:{stat.st_size}:{int(stat.st_mtime)}"
        cached = _load_json(cache_path)
        if isinstance(cached, dict) and cached.get("stamp") == stamp:
            return reframe.crop_rect(cached.get("rect"))
        try:
            rect = reframe.detect_crop(source, ffmpeg_bin=ffmpeg_bin, duration_ms=3000)
        except Exception:  # noqa: BLE001 —— 探测失败不该挡住导出
            rect = None
        if rect and log:
            log(f"素材有黑边（{rect['w']}×{rect['h']}），已按内容区域裁切")
        _dump_json(cache_path, {"stamp": stamp, "rect": rect})
        return rect

    def _focus_path(self, project_id: str) -> Path:
        return self._project_root(project_id) / "focus.json"

    def _focus_subjects_path(self, project_id: str) -> Path:
        return self._project_root(project_id) / "focus.subjects.json"

    def _load_focus(self, project_id: str) -> list[dict[str, Any]]:
        return reframe.load_focus(self._focus_path(project_id))

    def _load_focus_state(self, project_id: str) -> dict[str, Any]:
        """多主体状态（各主体轨迹 + 切点）。切主体靠它重建，不必重跑检测。"""
        raw = _load_json(self._focus_subjects_path(project_id))
        return raw if isinstance(raw, dict) else {}

    def clip_focus(self, project_id: str = "") -> dict[str, Any]:
        """读跟随轨迹：检测出来的 + 手工点的，一起给界面画在时间轴上。"""
        root = self._project_root(project_id)
        path = self._focus_path(project_id)
        focus = reframe.load_focus(path)
        meta = _load_json(path)
        info = meta if isinstance(meta, dict) else {}
        state = self._load_focus_state(project_id)
        return {
            "project_id": project_id,
            "focus": focus,
            "detector": str(info.get("detector") or state.get("detector") or ""),
            "notes": info.get("notes") or state.get("notes") or [],
            "safe_area": reframe.safe_area(),
            "root": str(root),
            "subjects": state.get("subjects") or [],
            "subject": str(info.get("subject") or state.get("subject") or ""),
            "cuts": info.get("cuts") or state.get("cuts") or [],
            "enhance": bool(info.get("enhance", state.get("enhance", True))),
        }

    def clip_focus_save(
        self, project_id: str = "", focus: list[dict] = None, clear: bool = False
    ) -> dict[str, Any]:
        """手工关键帧：人工点的优先，检测出来的点保留（`manual=true` 标出来）。

        `clear=True` 是「删掉整条轨迹」——没有它就没法回到"没检测过"的状态，
        检测出来一条歪轨迹之后只能用新的覆盖，改不回去。
        """
        path = self._focus_path(project_id)
        if clear:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "focus"})
            return {"project_id": project_id, "focus": [], "manual": 0, "cleared": True,
                    "path": str(path)}
        points: list[dict[str, Any]] = []
        for item in (focus or []):
            if not isinstance(item, dict):
                continue
            try:
                t_ms = max(0, int(item.get("t_ms") or 0))
            except (TypeError, ValueError):
                continue
            points.append({"t_ms": t_ms, "x": reframe.clamp_x(item.get("x")), "manual": True})
        points.sort(key=lambda point: point["t_ms"])
        old = _load_json(path)
        keep = old if isinstance(old, dict) else {}
        detected = [
            point for point in reframe.load_focus(path) if point.get("t_ms") not in {p["t_ms"] for p in points}
        ]
        merged = sorted(detected + points, key=lambda point: point["t_ms"])
        meta: dict[str, Any] = {
            "detector": keep.get("detector") or "manual",
            "notes": list(keep.get("notes") or []) + ["手工关键帧已保存"],
            "updated_at": db_now_iso(),
        }
        # 切点与主体要跟着走：只存点会把"别在切点插值"这条信息丢掉
        for key in ("cuts", "subject", "enhance"):
            if key in keep:
                meta[key] = keep[key]
        reframe.save_focus(path, merged, meta=meta)
        self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "focus"})
        return {
            "project_id": project_id,
            "focus": merged,
            "manual": len(points),
            "path": str(path),
        }

    def clip_focus_analyze(
        self, project_id: str = "", enhance: bool | None = None, subject: str = ""
    ) -> dict[str, Any]:
        self._project_root(project_id)
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            raise WBError("CONFLICT", "这个项目还没有源视频，先下载或导入再检测")
        job = self.jobs.submit(
            "clip.focus",
            project_id,
            {"project_id": project_id, "enhance": enhance, "subject": str(subject or "")},
        )
        return {"job": job}

    def clip_focus_subject(self, project_id: str = "", subject: str = "") -> dict[str, Any]:
        """切换跟随对象：从 `focus.subjects.json` 里挑一条轨迹重建，不重跑检测。

        重跑一次的代价是整段抽帧（480 秒的片子约 1 分钟），而这里只是换一条已经算好的
        轨迹——用户点一下就该立刻换，不能让他等一分钟。
        """
        path = self._focus_path(project_id)
        state = self._load_focus_state(project_id)
        subjects = list(state.get("subjects") or [])
        if not subjects:
            raise WBError("CONFLICT", "还没做过多主体检测，先点「检测人脸」")
        chosen = str(subject or "")
        if not reframe.subject_by_id(subjects, chosen):
            if chosen:
                raise WBError("NOT_FOUND", "这个主体不在检测结果里", chosen)
            chosen = str(state.get("subject") or "") or reframe.pick_subject(subjects)
        span_ms = int(state.get("span_ms") or 0) or 1
        frame_width = int(state.get("frame_width") or reframe.WIDTH)
        cuts = [int(v) for v in (state.get("cuts") or [])]
        focus = reframe.rebuild_focus(
            subjects,
            subject_id=chosen,
            span_ms=span_ms,
            frame_width=frame_width,
            cuts=cuts,
        )
        if not focus:
            raise WBError("NOT_FOUND", "这条主体没有可用的轨迹", chosen)
        manual = [point for point in self._load_focus(project_id) if point.get("manual")]
        merged = reframe.merge_manual(focus, manual, span_ms=span_ms) if manual else focus
        found = reframe.subject_by_id(subjects, chosen) or {}
        notes = list(state.get("notes") or []) + [f"已换成跟{found.get('label') or chosen}"]
        reframe.save_focus(
            path,
            merged,
            meta={
                "detector": state.get("detector") or "haar",
                "notes": notes,
                "cuts": cuts,
                "subject": chosen,
                "enhance": bool(state.get("enhance", True)),
                "updated_at": db_now_iso(),
            },
        )
        state["subject"] = chosen
        self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "focus"})
        return {
            "project_id": project_id,
            "focus": merged,
            "subject": chosen,
            "label": str(found.get("label") or ""),
            "subjects": subjects,
            "cuts": cuts,
            "notes": notes,
            "path": str(path),
        }

    def _clip_focus_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        sources = [a for a in self.repo.asset_list(project_id) if a.get("kind") == "source"]
        if not sources:
            raise WBError("CONFLICT", "这个项目还没有源视频")
        source = str(sources[0]["path"])
        info = self._ffmpeg.probe(source)
        duration_ms = int(getattr(info, "duration_ms", 0) or 0)
        width = int(getattr(info, "width", 0) or 0)
        enhance = payload.get("enhance")
        if enhance is None:
            enhance = bool(getattr(self.cfg.clip, "follow_enhance", True))
        enhance = bool(enhance)
        if enhance:
            ctx.log("开始检测人脸（每 0.5 秒一帧，多人画面会分别跟）")
        else:
            ctx.log("开始检测人脸（跟随增强已关闭，只跟最大那张脸）")
        result = reframe.detect_focus(
            source,
            start_ms=0,
            end_ms=duration_ms or 1,
            models_root=str(self.cfg.storage.models_root),
            proxy=str(getattr(self.cfg.llm, "proxy", "") or ""),
            log=ctx.log,
            enhance=enhance,
            subject=str(payload.get("subject") or ""),
        )
        ctx.progress(0.7)
        span_ms = duration_ms or 1
        notes = list(result.get("notes") or [])
        subjects = list(result.get("subjects") or [])
        subject_id = str(result.get("subject") or "")
        cuts = [int(value) for value in (result.get("cuts") or [])]
        focus = list(result.get("focus") or [])
        vision_model = ""
        if enhance and len(subjects) >= 2:
            client, model = self._vision_client()
            if client is None:
                notes.append("没配 vision 模型，主体按出镜时长与脸的大小打分选")
            else:
                picked = reframe.vision_pick_subject(
                    client,
                    shots=result.get("thumbs") or [],
                    subjects=subjects,
                    model=model,
                    prompt=reframe.load_subject_prompt(),
                )
                if picked.get("subject"):
                    vision_model = model
                    if picked["subject"] != subject_id:
                        subject_id = str(picked["subject"])
                        focus = reframe.rebuild_focus(
                            subjects,
                            subject_id=subject_id,
                            span_ms=span_ms,
                            frame_width=max(1, width),
                            cuts=cuts,
                        )
                        label = (reframe.subject_by_id(subjects, subject_id) or {}).get("label")
                        notes.append(f"按画面判断该跟{label or subject_id}，已切换")
                    else:
                        notes.append("画面判断与自动打分一致")
                else:
                    notes.append("vision 没给出明确主体，沿用自动打分")
        ctx.progress(0.9)
        manual = [point for point in self._load_focus(project_id) if point.get("manual")]
        # 这里必须把 span_ms 传进去：merge_manual 的 span_ms 是必填关键字参数，
        # 漏了会在"有手工关键帧时再检测一次"这条路上直接 TypeError
        merged = reframe.merge_manual(focus, manual, span_ms=span_ms) if manual else focus
        path = self._focus_path(project_id)
        reframe.save_focus(
            path,
            merged,
            meta={
                "detector": result.get("detector") or "",
                "notes": notes,
                "cuts": cuts,
                "subject": subject_id,
                "enhance": enhance,
                "updated_at": db_now_iso(),
            },
        )
        _dump_json(
            self._focus_subjects_path(project_id),
            {
                "project_id": project_id,
                "enhance": enhance,
                "subject": subject_id,
                "cuts": cuts,
                "detector": result.get("detector") or "",
                "notes": notes,
                "span_ms": span_ms,
                "frame_width": width,
                "subjects": subjects,
                "vision_model": vision_model,
                "updated_at": db_now_iso(),
            },
        )
        self.events.emit_now("clip.updated", {"project_id": project_id, "reason": "focus"})
        return {
            "project_id": project_id,
            "detector": result.get("detector"),
            "notes": notes,
            "points": len(merged),
            "manual": len(manual),
            "hits": result.get("hits"),
            "frames": result.get("frames"),
            "subjects": subjects,
            "subject": subject_id,
            "cuts": len(cuts),
            "enhance": enhance,
            "vision_model": vision_model,
            "path": str(path),
        }

    def _require_llm(self, task: str = "summary") -> ChatClient:
        client = self._llm_client(task)
        if not client.configured:
            raise WBError(
                "LLM_NOT_CONFIGURED",
                "还没有配置大模型",
                "在 设置 → AI 里填好 base_url / api_key / model 再来",
            )
        return client

    def _summary_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        project = self.repo.project_get(project_id)
        if not project:
            raise WBError("NOT_FOUND", "找不到这个项目")
        row = self.repo.transcript_get(project_id)
        segments = (row or {}).get("segments") or []
        if not segments:
            raise WBError("CONFLICT", "这条视频还没有字幕")
        client = self._llm_client_for(
            str(payload.get("provider") or ""),
            task="summary",
            model=str(payload.get("model") or ""),
        )
        if not client.configured:
            raise WBError("LLM_NOT_CONFIGURED", "还没有配置大模型")
        # job 重放读到的 role 是**已落库**的历史值，不是这次的选择 → 按合同算 fallback：
        # 退役就回落默认角色，并把原因写进任务日志（入口算出的说明随 payload 带过来）。
        role, notice = summarizer.resolve_role(
            "", str(payload.get("role") or "") or self.cfg.llm.role)
        notice = str(payload.get("role_notice") or "") or notice
        if notice:
            ctx.log(notice)
        ctx.log(f"用 {client.model} 生成总结（角色：{role}）")

        if str(payload.get("kind")) == "refine":
            previous = self.repo.summary_get(str(payload.get("previous_id") or ""))
            if not previous:
                raise WBError("CONFLICT", "上一版总结不见了，重新生成一次")
            ctx.log(f"按你的要求改第 {previous.get('version')} 版："
                    f"{str(payload.get('requirement') or '')[:40]}")
            result = summarizer.refine(
                _bundle_project(project),
                segments,
                dict(previous.get("content") or {}),
                str(payload.get("requirement") or ""),
                client=client,
                cfg=self.cfg,
                ctx=ctx,
                role=role,
                model=client.model,
                cache=self.notes,
                task_key=project_id,
            )
        else:
            result = summarizer.run(
                _bundle_project(project),
                segments,
                client=client,
                cfg=self.cfg,
                ctx=ctx,
                role=role,
                danmaku=payload.get("danmaku") or None,
                model=client.model,
                cache=self.notes,
                task_key=project_id,
            )
        ctx.check_cancel()
        content = dict(result["summary"])
        saved = self.repo.summary_insert(
            project_id,
            content=content,
            content_md=summarizer.to_markdown(content),
            role=str(result.get("role") or role),
            model=str(result.get("model") or client.model),
            usage=result.get("usage") or {},
            status="ready",
        )
        self._write_summary_files(project_id, saved)
        stage = str(project.get("stage") or "new")
        # 只在"还没走到后面"时推进阶段：已经切过片的项目，重出一版总结不该把它
        # 打回 summarized——那会让切片台的数据看起来"还没做"。
        if stage in ("new", "downloaded", "subtitle", "transcribed"):
            self.repo.project_update(project_id, stage="summarized")
        self.events.emit_now("library.changed", {"project_id": project_id,
                                                "reason": "summary"})
        return {
            "summary_id": saved.get("id"),
            "version": saved.get("version"),
            "role": result.get("role"),
            "model": result.get("model"),
            "chars": saved.get("chars"),
            "one_line": content.get("one_line", ""),
            "calls": result.get("calls"),
            "notes": result.get("notes") or [],
        }

    def _write_summary_files(self, project_id: str, row: dict[str, Any]) -> list[str]:
        """把一版总结落成 ``summary.json`` + ``summary.md`` 并登记成资产。

        两个文件都写：``.json`` 给程序和版本恢复用，``.md`` 给人看/贴到别处用。
        只写一个的话，用户要么没法直接读，要么下次改版得从 md 反解结构。
        """
        project = self.repo.project_get(project_id) or {}
        root = Path(str(project.get("root_dir") or ""))
        if not str(root):
            return []
        json_path = root / "summary.json"
        md_path = root / "summary.md"
        payload = dict(row.get("content") or {})
        payload.setdefault("version", row.get("version"))
        payload.setdefault("model", row.get("model"))
        payload.setdefault("role", row.get("role"))
        # 版本另存一份，避免"再出一版把上一版覆盖掉"——磁盘上只有最新版是有意的，
        # 历史在库里；但每次覆盖前先把旧文件留个档，用户手动翻找时不至于全丢。
        history = root / "summary.v{}.json".format(int(row.get("version") or 1))
        try:
            root.mkdir(parents=True, exist_ok=True)
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(str(row.get("content_md") or ""), encoding="utf-8")
            history.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            raise WBError("IO", "总结写入失败", str(exc)) from exc
        paths = [str(md_path), str(json_path)]
        for path in (md_path, json_path):
            self.repo.asset_upsert(
                project_id, "summary", str(path),
                bytes=path.stat().st_size if path.exists() else None,
            )
        return paths

    def _model_download_job(self, ctx: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or self.cfg.transcribe.model or models_mod.DEFAULT_MODEL)
        found = models_mod.spec(name)

        def progress(ratio: float, text: str) -> None:
            ctx.progress(max(0.01, min(0.99, ratio)), stage=text)

        result = models_mod.download(
            self.cfg.models_root,
            found.name,
            progress=progress,
            # models.download 里的是"轮询线程"的取消；job 层的取消标志直接传进去
            cancel=ctx.should_cancel,
        )
        ctx.progress(1.0, stage="模型就绪")
        return result

    # ------------------------------------------------------- 文件

    # ------------------------------------------------------- 合并

    def merge_sources(self, project_id: str = "", query: str = "") -> dict[str, Any]:
        """可加进合并的来源：切片台的成片 + 素材库的源视频。

        本地文件不在这里列——那是系统选择框的事（`fs.pick` 拿到的路径直接进预检）。
        两个列表都带时长与体积，界面不用为了显示一行去逐个 probe。
        """
        needle = str(query or "").strip().lower()
        projects = self.repo.project_list(limit=500)
        if project_id:
            projects = [p for p in projects if p.get("id") == project_id]
        clips: list[dict[str, Any]] = []
        library: list[dict[str, Any]] = []
        for project in projects:
            title = str(project.get("title") or "")
            for asset in self.repo.asset_list(str(project.get("id"))):
                kind = str(asset.get("kind") or "")
                if kind not in ("export", "source"):
                    continue
                path = str(asset.get("path") or "")
                if not path:
                    continue
                name = Path(path).name
                if needle and needle not in name.lower() and needle not in title.lower():
                    continue
                row = {
                    "path": path,
                    "name": name,
                    "project_id": project.get("id"),
                    "project_title": title,
                    "bytes": int(asset.get("bytes") or 0),
                    "duration_ms": int(asset.get("duration_ms") or 0),
                    "exists": Path(path).is_file(),
                    "origin": "clip" if kind == "export" else "library",
                }
                (clips if kind == "export" else library).append(row)
        clips.sort(key=lambda r: (r["project_title"], r["name"]))
        library.sort(key=lambda r: (r["project_title"], r["name"]))
        return {"clips": clips, "library": library, "total": len(clips) + len(library)}

    def merge_preflight(
        self, paths: list[str] | None = None, width: int = 0, height: int = 0, fps: float = 0.0
    ) -> dict[str, Any]:
        """预检 + 两种模式的估算 + 默认可写路径。

        探测失败的文件**留在表里带错误**，不踢掉：用户得看见是哪一条坏。
        """
        wanted = [str(p) for p in (paths or []) if str(p or "").strip()]
        if not wanted:
            raise WBError("VALIDATION", "先往列表里加片段")
        result = merger.preflight(self._ffmpeg, wanted, width=width, height=height, fps=fps)
        usable = [i for i in result["items"] if i.get("ok")]
        out_dir = merger.default_output_dir(
            str(Path(usable[0]["path"]).parent) if usable else "", self.cfg.media_root
        )
        return {
            **result,
            "estimate": {
                "quick": merger.estimate(result, "quick"),
                "precise": merger.estimate(result, "precise"),
            },
            "output": {
                "dir": str(out_dir),
                "name": merger.output_name(usable),
            },
        }

    def merge_start(
        self,
        paths: list[str] | None = None,
        mode: str = "quick",
        out_dir: str = "",
        out_name: str = "",
        width: int = 0,
        height: int = 0,
        fps: float = 0.0,
    ) -> dict[str, Any]:
        """提交一次合并。

        **快速拼接在提交前就把关**：规格不一致直接 `CONFLICT` 并把差异写进 detail，
        不让一个注定失败的命令跑起来再失败——那会白占一次任务、还留下半成品。
        """
        wanted = [str(p) for p in (paths or []) if str(p or "").strip()]
        if not wanted:
            raise WBError("VALIDATION", "先往列表里加片段")
        if mode not in ("quick", "precise"):
            raise WBError("VALIDATION", "拼接模式只能是 quick / precise")

        result = merger.preflight(self._ffmpeg, wanted, width=width, height=height, fps=fps)
        usable = [i for i in result["items"] if i.get("ok")]
        # 预检里的"能不能快速拼接"只对 quick 是把关；precise 存在的意义就是兜住不一致，
        # 所以这里只拦"文件不够/读不出"，不拦"规格不一致"。
        reason = merger.block_reason(result, "quick" if mode == "quick" else "precise")
        if len(usable) < 2 or reason:
            raise WBError(
                "CONFLICT",
                reason or "至少要两个可用片段才能拼接",
                json.dumps(result.get("differences") or [], ensure_ascii=False),
            )

        directory = Path(out_dir) if out_dir else merger.default_output_dir(
            str(Path(usable[0]["path"]).parent), self.cfg.media_root
        )
        suffix = "_quick" if mode == "quick" else "_precise"
        name = out_name.strip() or merger.output_name(usable, suffix=suffix)
        if not name.lower().endswith(".mp4"):
            name = f"{name}.mp4"
        target = unique_path(directory / sanitize_name(name, max_len=120))

        estimate = merger.estimate(result, mode)
        # 守卫在提交前：不足就直接拒，不留半成品。估不准是常态，宁可略高。
        ensure_free_space(
            str(directory),
            int(estimate.get("size_bytes") or 0) * 2 + 2 * 1024**3,
        )

        record = self.repo.merge_insert(
            items=[i.get("path") for i in usable],
            mode=mode,
            preflight={
                "differences": result.get("differences") or [],
                "target": result.get("target") or {},
                "total_ms": result.get("total_ms") or 0,
                "count": result.get("count") or 0,
            },
            project_id=None,
            status="running",
            output_path=str(target),
        )
        payload = {
            "merge_id": record.get("id"),
            "paths": [i.get("path") for i in usable],
            "items": [
                {
                    "path": i.get("path"),
                    "has_audio": bool(i.get("has_audio")),
                    "duration_ms": int(i.get("duration_ms") or 0),
                }
                for i in usable
            ],
            "dest": str(target),
            "expected_ms": int(result.get("total_ms") or 0),
            "target": result.get("target") or {},
            "mode": mode,
        }
        job = self.jobs.submit("merge.run", None, payload)
        return {
            "job": job,
            "merge_id": record.get("id"),
            "output": str(target),
            "name": target.name,
            "count": len(usable),
            "expected_ms": payload["expected_ms"],
            "estimate": estimate,
        }

    def merge_list(self) -> dict[str, Any]:
        """合并历史（界面在 P4-04 用；先给验收一个能读的入口）。"""
        rows = self.repo.merge_list(limit=50)
        for row in rows:
            path = str(row.get("output_path") or "")
            row["exists"] = bool(path) and Path(path).is_file()
            row["name"] = Path(path).name if path else ""
            row["bytes"] = Path(path).stat().st_size if row["exists"] else 0
        return {"items": rows, "total": len(rows)}

    def _merge_job(self, ctx: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """合并本体：快速拼接优先，失败自动切精确重编码（只切一次），全程可取消。

        **为什么快速失败要自动切。** 预检判的是"规格一致"，但 `-c copy` 还会因为
        时间戳断层、moov 位置、非关键帧起点这些预检看不出来的原因失败。此时让用户
        自己发现、自己改模式、自己再点一次，纯属把技术细节甩给用户。切一次就够——
        精确也失败说明素材本身有问题，继续重试只是多等十分钟。
        """
        merge_id = str(payload.get("merge_id") or "")
        paths = [str(p) for p in (payload.get("paths") or [])]
        items = list(payload.get("items") or [])
        dest = Path(str(payload.get("dest") or ""))
        expected = int(payload.get("expected_ms") or 0)
        segments = [int(i.get("duration_ms") or 0) for i in items] or [0] * len(paths)
        target_spec = payload.get("target") or {}
        requested = str(payload.get("mode") or "quick")

        tmp = Path(str(self.cfg.cache_root)) / "tmp" / str(ctx.job_id)
        tmp.mkdir(parents=True, exist_ok=True)
        fallback_used = False
        note = ""

        def run_precise(out_path: Path) -> None:
            plan = merger.precise_plan(
                items,
                width=int(target_spec.get("width") or 1920),
                height=int(target_spec.get("height") or 1080),
                fps=float(target_spec.get("fps") or 30.0),
                sample_rate=int(target_spec.get("sample_rate") or 48000),
                channels=int(target_spec.get("channels") or 2),
            )
            ctx.log(
                f"精确重编码：{plan['count']} 段 → {plan['width']}×{plan['height']} "
                f"· {plan['fps']:g}fps · {plan['sample_rate']}Hz"
            )

            def report(progress: dict[str, Any]) -> None:
                ctx.progress(
                    0.1 + 0.85 * float(progress.get("fraction") or 0),
                    merger.progress_text(progress),
                )

            self._ffmpeg.merge_precise(
                inputs=paths,
                dest=out_path,
                plan=plan,
                segments_ms=segments,
                expected_ms=expected,
                on_progress=report,
                should_cancel=ctx.should_cancel,
            )

        try:
            if requested == "quick":
                listing = tmp / "concat.txt"
                listing.write_text(merger.concat_list_text(paths), encoding="utf-8")
                ctx.log(f"{len(paths)} 个片段，清单 {listing.name}")
                ctx.check_cancel()
                try:
                    ctx.progress(0.2, "拼接中")
                    self._ffmpeg.merge_copy(listing, dest, **ctx.guard_kwargs())
                except WBError as exc:
                    if exc.code == "CANCELED":
                        raise
                    fallback_used = True
                    note = merger.fallback_note()
                    ctx.log(f"{note}（原因：{exc.message}）")
                    # 失败那次的半成品先删掉，别和后面的成片混在一个目录里
                    try:
                        dest.unlink(missing_ok=True)
                    except OSError:
                        pass
                    dest = unique_path(dest.with_name(f"{dest.stem}_precise{dest.suffix}"))
                    if merge_id:
                        self.repo.merge_update(
                            merge_id,
                            mode="precise",
                            fallback_used=1,
                            output_path=str(dest),
                        )
                    ctx.progress(0.1, note)
                    run_precise(dest)
            else:
                ctx.progress(0.05, "准备精确重编码")
                ctx.check_cancel()
                run_precise(dest)

            ctx.progress(0.95, "校验成片")
            info = self._ffmpeg.probe(dest)
        except WBError as exc:
            if merge_id:
                status = "canceled" if exc.code == "CANCELED" else "failed"
                self.repo.merge_update(
                    merge_id,
                    status=status,
                    error="" if status == "canceled" else exc.message,
                    fallback_used=1 if fallback_used else 0,
                )
            raise
        finally:
            # 临时清单/中间文件不留：它们只对本次任务有用，留着只会占空间
            try:
                if tmp.is_dir():
                    for leftover in tmp.iterdir():
                        leftover.unlink(missing_ok=True)
                    tmp.rmdir()
            except OSError:
                pass

        actual = int(info.duration_ms or 0)
        delta = actual - expected
        mode_used = "precise" if (fallback_used or requested == "precise") else "quick"
        if merge_id:
            self.repo.merge_update(
                merge_id, status="succeeded", error="", mode=mode_used, output_path=str(dest)
            )
        self.events.emit_now("library.changed", {"reason": "merge"})
        ctx.progress(1.0, "完成")
        return {
            "merge_id": merge_id,
            "path": str(dest),
            "name": dest.name,
            "bytes": dest.stat().st_size if dest.is_file() else 0,
            "duration_ms": actual,
            "expected_ms": expected,
            "delta_ms": delta,
            "mode": mode_used,
            "requested_mode": requested,
            "fallback_used": fallback_used,
            "note": note,
            "count": len(paths),
            "target": target_spec,
        }

    def _sweep_tmp(self) -> list[str]:
        """开机清 `cache_root/tmp`：里面只有"某次任务自己建的临时目录"。

        `JobManager.__init__` 已经把上次没结束的任务标成失败，所以这些目录在没有
        对应运行中任务的情况下一定是垃圾。崩过一次之后不清理，磁盘上会攒下一堆
        `job_xxx` 空目录，用户翻缓存目录时只会以为程序在乱写东西。
        """
        root = Path(str(self.cfg.cache_root)) / "tmp"
        removed: list[str] = []
        if not root.is_dir():
            return removed
        for entry in root.iterdir():
            try:
                if entry.is_dir():
                    for child in entry.rglob("*"):
                        if child.is_file():
                            child.unlink()
                    for child in sorted(entry.rglob("*"), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    entry.rmdir()
                else:
                    entry.unlink()
                removed.append(entry.name)
            except OSError:
                continue
        return removed

    def _allowed_roots(self) -> list[str]:
        roots = [
            self.cfg.media_root,
            self.cfg.cache_root,
            self.cfg.models_root,
            os.path.dirname(self.cfg.db_path),
            # 日志目录：界面上「打开日志目录」要做 fs.reveal，不在白名单里会被拒
            self.logs["dir"],
        ]
        if self.cfg.source_path:
            roots.append(os.path.dirname(self.cfg.source_path))
        return [os.path.abspath(r) for r in roots if r]

    def fs_reveal(self, path: str) -> dict[str, Any]:
        """在资源管理器里定位一个文件。

        **只允许已存在的、落在已知根目录内的路径。** 这个接口的参数来自页面，
        不加约束的话它就是一个"打开任意路径"的原语——虽然只是打开，
        但足以用来探测本机目录结构。
        """
        from video_workbench.infra.files import is_within

        target = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
        if not any(is_within(root, target) for root in self._allowed_roots()):
            raise WBError("VALIDATION", "只允许打开工作区内的路径")
        if not os.path.exists(target):
            raise WBError("NOT_FOUND", "路径不存在", target)
        try:
            if os.name == "nt":
                # /select 会打开父目录并选中该文件，比单纯 open 更符合"定位到"的意图
                os.startfile(  # noqa: S606 - 本机 GUI 应用，非不可信输入
                    os.path.dirname(target) if os.path.isfile(target) else target
                )
            else:  # pragma: no cover - 非目标平台
                shutil.which("open") or shutil.which("xdg-open")
        except OSError as exc:
            raise WBError("PROCESS_FAILED", "无法打开资源管理器", str(exc)) from exc
        return {"path": target}

    # ------------------------------------------------------- 诊断

    def _diagnostics_sections(self) -> list[tuple[str, str]]:
        """诊断包的正文分段。全部纯文本，方便人直接读，不依赖任何工具。"""
        deps = {
            "ffmpeg": shutil.which("ffmpeg") or self._ffmpeg_in_config_dir() or "(未找到)",
            "ffprobe": shutil.which("ffprobe") or "(未找到)",
            "yt_dlp": "已装" if find_spec("yt_dlp") is not None else "未装",
            "faster_whisper": "已装" if find_spec("faster_whisper") is not None else "未装",
        }
        db_info = {
            "path": self.cfg.db_path,
            "exists": os.path.isfile(self.cfg.db_path),
            "schema_version": schema_version(self.conn),
        }
        errors = chr(10).join(f"{code}: {text}" for code, text in sorted(ERROR_CODES.items()))
        counts = {
            "projects": self.repo.project_count() if hasattr(self.repo, "project_count") else "(未统计)",
        }
        return [
            ("环境", chr(10).join(log_mod.environment_lines())),
            (
                "应用",
                chr(10).join(
                    [
                        f"name: {APP_NAME}",
                        f"version: {__version__}",
                        f"skin: {self.cfg.ui.skin}",
                        f"language: {self.cfg.app.language}",
                        f"media_server: {self.cfg.app.media_server}",
                        f"config: {self.cfg.source_path or '(默认)'}",
                        f"uptime_seconds: {round(time.time() - self.started_at, 1)}",
                    ]
                ),
            ),
            ("依赖", chr(10).join(f"{key}: {value}" for key, value in deps.items())),
            ("数据库", chr(10).join(f"{key}: {value}" for key, value in db_info.items())),
            ("目录", chr(10).join([f"media: {self.cfg.media_root}", f"cache: {self.cfg.cache_root}",
                                f"models: {self.cfg.models_root}", f"logs: {self.logs['dir']}"])),
            ("错误码表", errors),
            ("统计", chr(10).join(f"{key}: {value}" for key, value in counts.items())),
            ("启动恢复", json.dumps(getattr(self, "recovery", {}), ensure_ascii=False, indent=2)),
        ]

    # ------------------------------------------------------ 首次启动向导

    def app_onboard(self) -> dict[str, Any]:
        """五件事体检 + 是不是第一次来。

        纯只读：不改配置、不下载、不发请求（登录态读的是本地 key 文件）。
        """
        report = onboard_core.report(
            cfg=self.cfg,
            gateway=self._gateway,
            command_line=Path(getattr(__import__("sys"), "executable", "")).name or "python",
        )
        report["first_run"] = not bool(self.cfg.ui.onboarded)
        return report

    def app_onboard_done(self, skipped: bool = False) -> dict[str, Any]:
        """记下"看过了"。跳过一次也算看过——否则每次启动都弹窗，属于骚扰。"""
        self.cfg.apply({"ui": {"onboarded": True}})
        self.cfg.save()
        return {"onboarded": True, "skipped": bool(skipped)}

    def app_migration_notice(self) -> dict[str, Any]:
        """搬家一次性提示。没搬过家 / 旧家干净 / 已经确认过 → ``notice`` 为 None。

        这份数据只有路径与**键名**，没有任何值：值里可能有 ``api_key``，弹到屏幕上
        就是泄露。已确认后刻意返回 None 而不是「带 dismissed=True 的原件」——前端只需
        要一个判断，把决定留在后端（口径只有一个出处）。
        """
        notice = getattr(self.cfg.ui, "migration_notice", None)
        if not isinstance(notice, dict):
            return {"notice": None, "dismissed": False}
        if notice.get("dismissed") or not notice.get("keys"):
            return {"notice": None, "dismissed": bool(notice.get("dismissed"))}
        return {"notice": dict(notice), "dismissed": False}

    def app_migration_notice_dismiss(self) -> dict[str, Any]:
        """记下「这条我看过了」。弹窗的四条关闭路径（知道了 / ESC / 点遮罩 / ×）都走这一次。

        只改标记、不删事实：旧家那批键还在，事实不该跟着消失，只是不再打扰。
        ``save()`` 会整体重写 sidecar，所以这个标记必须熬得过之后任何一次无关保存
        （V5 验的就是这条链路）。
        """
        notice = getattr(self.cfg.ui, "migration_notice", None)
        if not isinstance(notice, dict):
            return {"dismissed": False, "reason": "没有需要确认的搬家提示"}
        notice["dismissed"] = True
        self.cfg.save()
        return {"dismissed": True}

    def app_diagnostics(self, path: str = "", log_lines: int = 400) -> dict[str, Any]:
        """导出一个诊断 zip：环境 + 脱敏配置 + 最近日志。

        不联网、不上传：包落在本地，用户自己去发。
        """
        target = str(path or "").strip()
        if target:
            resolved = Path(os.path.abspath(os.path.expanduser(target)))
            inside = any(is_within(root, str(resolved)) for root in self._allowed_roots())
            if not inside:
                raise WBError("VALIDATION", "诊断包只能写到媒体/缓存/日志目录里", str(resolved))
            if resolved.suffix.lower() != ".zip":
                resolved = resolved.with_suffix(".zip")
        else:
            folder = Path(self.cfg.media_root) / "diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = db_now_iso().replace(":", "").replace("-", "")[:15]
            resolved = Path(unique_path(str(folder / f"diagnostics-{stamp}.zip")))
        try:
            bundle = log_mod.write_bundle(
                resolved,
                title=f"{APP_NAME} 诊断包",
                sections=self._diagnostics_sections(),
                config_yaml=render_yaml(self.cfg),
                log_file=self.logs["file"],
                log_lines=max(20, min(5000, int(log_lines or 400))),
            )
        except OSError as exc:
            raise WBError("IO", "诊断包写不出去（目录可能没权限或满了）", str(exc)) from exc
        self.logger.info("诊断包已导出 %s（%s 字节）", bundle["path"], bundle["bytes"])
        return bundle

    def app_log_tail(self, lines: int = 200) -> dict[str, Any]:
        """最近若干行日志（已脱敏）。界面的"复制日志"用它。"""
        limit = max(1, min(2000, int(lines or 200)))
        rows = log_mod.tail_lines(self.logs["file"], limit)
        text = log_mod.redact(chr(10).join(rows))
        return {"file": self.logs["file"], "dir": self.logs["dir"], "lines": len(rows),
                "text": text, "files": log_mod.list_logs(self.logs["dir"])}

    def open_url(self, url: str) -> dict[str, Any]:
        """用系统浏览器打开一个链接（发布记录里的「打开视频」）。

        只放 http/https：这个接口的参数来自页面，放开 `file:` 之类就等于
        给了一个"打开本机任意东西"的原语。
        """
        from urllib.parse import urlparse

        target = str(url or "").strip()
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise WBError("VALIDATION", "只允许打开 http/https 链接")
        try:
            if os.name == "nt":
                os.startfile(target)  # noqa: S606 - 用户点出来的链接
            else:  # pragma: no cover - 非目标平台
                opener = shutil.which("open") or shutil.which("xdg-open")
                if not opener:
                    raise WBError("UNSUPPORTED", "这个系统没有可用的打开方式")
                subprocess.Popen([opener, target])  # noqa: S603
        except OSError as exc:
            raise WBError("PROCESS_FAILED", "打不开这个链接", str(exc)) from exc
        return {"url": target}

    def fs_media(self) -> dict[str, Any]:
        """媒体根目录。页面要知道"打开媒体目录"开的是哪儿，而不是自己拼。"""
        return {"path": self.cfg.media_root, "exists": os.path.isdir(self.cfg.media_root)}

    def fs_pick(self, kind: str = "dir") -> dict[str, Any]:
        """打开系统选择框。桌面层注入的 ``picker`` 才有实现。"""
        if self.picker is None:
            raise WBError("UNSUPPORTED", "当前运行方式不支持系统对话框")
        picked = self.picker(str(kind))
        return {"path": picked}

    # ------------------------------------------------------- 退出

    def close(self) -> None:
        self.jobs.shutdown()
        self.events.close()
        try:
            self.conn.close()
        except Exception:  # pragma: no cover
            pass


class _PickParams(BaseModel):
    kind: str = "dir"


class _UnconfiguredClient:
    """没配模型时的替身：一叫就抛，好让 `clipper.generate` 走规则兜底。

    直接抛 `LLM_NOT_CONFIGURED` 而不是返回假内容——兜底候选会明确标
    `source="rule"`，界面上写"位置可用，但没有内容含义"。
    """

    model = ""

    def chat(self, *_args: Any, **_kwargs: Any) -> str:
        raise ChatError("LLM_NOT_CONFIGURED", "还没有配置大模型")


def _load_json(path: Path) -> Any:
    """读侧车 json；不存在或坏了都返回 None——侧车只是缓存/辅助，不该抛。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dump_json(path: Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _bundle_project(project: dict[str, Any]) -> dict[str, Any]:
    """把 ``wb_project`` 的一行翻成总结器认的字段名。

    只做**键名映射**，不补内容：表里没有的（简介、标签、分区）一律留空，
    让提示词里的「（无）」出现。凭空填一个"未知"上去，模型会当成素材的一部分。
    """
    page = int(project.get("page_no") or 1)
    return {
        "bvid": str(project.get("bvid") or ""),
        "aid": int(project.get("aid") or 0),
        "cid": int(project.get("cid") or 0),
        "page": page,
        # 单集项目不知道总集数；写 1 比写 0 诚实——0 会让提示词出现「第 1 / 0 P」。
        "page_count": int(project.get("page_count") or 1),
        "title": str(project.get("title") or ""),
        "up_name": str(project.get("up_name") or ""),
        "up_mid": int(project.get("up_mid") or 0),
        "desc": str(project.get("desc") or ""),
        "tname": str(project.get("tname") or ""),
        "tags": list(project.get("tags") or []),
        "duration_sec": float(project.get("duration_sec") or 0),
    }


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """这条视频累计。

    按库里所有版本求和，不是「最后一次的账」——小村的问题是「这条视频一共花了多少」，
    打磨七八版之后只看最新一版会严重低估。缺字段的老版本按 0 计，不报错。
    """
    out = {"versions": len(rows), "calls": 0, "prompt_tokens": 0,
           "completion_tokens": 0, "total_tokens": 0, "seconds": 0.0}
    for row in rows:
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        for key in ("calls", "prompt_tokens", "completion_tokens"):
            try:
                out[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
        # 老版本只记了 calls，没有 total_tokens；能凑就用输入+输出
        # 兜出总量，凑不出就按 0——宁可少算，不凭空造数。
        try:
            given = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            given = 0
        if not given:
            try:
                given = int(usage.get("prompt_tokens") or 0) + int(
                    usage.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                given = 0
        out["total_tokens"] += given
        try:
            out["seconds"] = round(out["seconds"] + float(usage.get("seconds") or 0), 1)
        except (TypeError, ValueError):
            continue
    return out


def _version_row(row: dict[str, Any]) -> dict[str, Any]:
    """版本列表的一行。只挑界面要显示的，别把整篇正文塞进列表。"""
    content = dict(row.get("content") or {})
    return {
        "id": row.get("id"),
        "version": int(row.get("version") or 1),
        "role": row.get("role") or "",
        "model": row.get("model") or "",
        "chars": int(row.get("chars") or 0),
        "created_at": row.get("created_at") or "",
        "status": row.get("status") or "",
        # 合集总览没有 ``one_line``（它有一句 ``theme``）；版本列表只有一列显示摘要，
        # 让 ``one_line`` 兜到 ``theme`` 上，前端就不用为两种 scope 写两套渲染。
        "one_line": str(content.get("one_line") or content.get("theme") or ""),
        "theme": str(content.get("theme") or ""),
        "revise_note": str(content.get("revise_note") or ""),
        # 版本列表要能直接显示每版花了多少，不必再逐版回查。
        "usage": dict(row.get("usage") or {}),
    }
