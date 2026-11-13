/* ============================================================================
 * pages/settings.js —— 设置
 *
 * 结构照方案书 5.7：左边分区，右边表单，底部一条常驻的保存条。
 *
 * **表单是声明式生成的，不是手写八段 HTML。** 理由：设置项有 50 多个，
 * 手写会立刻出现"界面上有、配置里没有"（或反过来）的漂移；
 * 声明表（SECTIONS）同时是文档——加一个配置项就来这里加一行。
 *
 * **只提交改过的字段。** 后端 apply 是"按段落合并 + 校验 + 全有或全无"，
 * 整份提交会把「用户没动过的段落」也一起写回，一旦某段正好被新版本改过默认值，
 * 用户点一次保存就被旧值覆盖了。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});
  var ui = WB.ui;

  // ------------------------------------------------------------ 声明表
  //
  // path 用点号对应 config.yaml 的层级；type 决定控件；help 是"为什么要有这个"。

  var SECTIONS = [
    {
      key: "general",
      title: "常规",
      hint: "语言与媒体服务方式。改了「媒体服务」需要重开程序。",
      fields: [
        { path: "app.language", label: "界面语言", type: "select", options: [["zh-CN", "简体中文"], ["en-US", "English"]] },
        {
          path: "app.media_server",
          label: "媒体服务",
          type: "select",
          options: [
            ["auto", "自动（优先本地文件，播放器不支持时回退 HTTP）"],
            ["file", "始终本地文件"],
            ["http", "始终 HTTP"]
          ],
          help: "HTTP 只监听 127.0.0.1，用于浏览器内核不支持直接播放的编码。"
        },
        { path: "ui.skin", label: "皮肤", type: "select", options: [["ark", "ark（深色）"], ["exhibition", "exhibition（亮色）"]], help: "等同于顶栏的皮肤开关。" },
        { path: "ui.rail_collapsed", label: "左栏默认折叠", type: "switch" },
        { path: "server.port", label: "媒体服务端口", type: "number", help: "被占用时会自动顺延，这里只是偏好。" }
      ]
    },
    {
      key: "llm",
      title: "模型",
      hint: "一张卡 ＝ 一个服务商（接口地址 + 密钥 + 模型列表）。任务用哪一家在下面「任务模型」里指定。密钥只存在本机 config.yaml，不随设置页外传。",
      fields: [
        // 服务商列表不是普通输入框，由 renderProviders 画卡片（DSH 那种一卡一家）。
        { path: "llm.providers", label: "服务商", type: "providers" },
        {
          path: "llm.role",
          label: "默认角色",
          type: "select",
          group: "advanced",
          // 这张字面量表是「summary.roles 读不到时页面照常可用」的兜底（reject 那条需求），
          // 正常路径下会被后端返回的名单整表换掉（见 roleOptionsFrom / applyRoleIssue）。
          // 两边必须同步：parity 有一条常驻门把这张表的 key 序列与 ROLE_KEYS 逐字比对
          // （tests/wb/test_settings_role_warning_ui.py 的
          // test_the_option_table_in_settings_js_is_still_a_copy_of_the_backend_list）。
          // 顺序照 ROLE_KEYS，别在这儿排序。task-31 起 xueshu（学术分析）已删、
          // shipin（视频总结）合并进 renhua，两者都退出清单；历史行仍显示「（已停用）」，
          // 那条口径在后端 summary.roles 的 retired 里，前端不写死任何 key。
          options: [["renhua", "renhua（人话）"], ["shensi", "shensi（深思）"], ["wengao", "wengao（文稿）"], ["xiezuo", "xiezuo（写作）"], ["fenxi", "fenxi（分析）"], ["jinglian", "jinglian（精炼）"]],
          // 轮数由角色的 passes 决定（判定在 core/summary/summarizer.py:572 那个门），
          // 选项标签里的「三轮 / 单轮」是后端注入的，别在文案里数角色数——
          // 数一次就得多改一处前端，下次新增或退役角色必然对不上。
          help: "表里这些角色都走 生成→自我批判→修订 三轮，耗时约为单轮的 3 倍（轮数以选项标签为准，它按角色的 passes 注入）。配置里那个值不能直接用的时候，这一栏下面会出现一行说明（内容来自后端，不在这里点名具体角色，免得下次退役或新增就得多改一处前端）。"
        },
        {
          // 附加提示词：把 resources/prompts/大概率管用 .txt 全文接在角色提示词后面。
          // 默认关——那份是对抗性双角色提示词（Guardian / Maverick 两段必出），和总结
          // 要的 JSON 契约正面冲突，开着会拖垮产出。要不要吃它交给人，判定与拼接都在
          // core/prompt_source.py，这里只是一个开关。
          path: "llm.extra_prompt",
          label: "附加提示词（大概率管用）",
          type: "switch",
          group: "advanced",
          help: "打开后把「大概率管用 .txt」全文接在当前角色的 txt 真源之后、输出契约之前。与 JSON 契约正面冲突，默认关。"
        },
        {
          // 坏 JSON 抢救（task-5）：以前只能靠环境变量 WB_SUMMARY_PARITY_STRICT=0
          // 放行，而那个开关连输出预算兜底一起放行，开一下就是两件事。
          // 这个开关只放行修复与重问（修 JSON → 截断续写 → 正文/时间线分批 → 严格重问），
          // 预算闸不跟着开；判定与止损都在 core/summarizer.py，这里只是一个开关。
          path: "llm.rescue_json",
          label: "坏 JSON 抢救",
          type: "switch",
          group: "advanced",
          help: "默认关：模型输出不是合法 JSON 时直接报错，不重试。打开后会尝试修复它、截断了接着写、正文与时间线分两次要、或换严格要求重问一次；抢救失败就停手，不会反复重试烧钱。不改变输出预算。"
        },
        { path: "llm.temperature", label: "温度", type: "number", group: "advanced", help: "0–2。总结建议 0.2–0.4。" },
        { path: "llm.max_tokens", label: "单次最大输出", type: "number", group: "advanced" },
        // 下面三项后端早就在用（ChatClient 与分块摘要都读），界面一直没暴露，
        // 等于"配置里有、界面上没有"——只能手改 YAML。这次一起补上。
        //
        // 「深度思考」这一栏原来在这里，已删（task-26）。删的是控件不是键：
        // WB 自己**不发送** enable_thinking——`video_workbench/**/*.py` 里对 thinking
        // 只有一个命中，就是 config.py 那句取值校验（thinking 走 `_enum(...)` 白名单），
        // 没有任何出网路径读它；而 bili-scribe 会读、会真发这个参数，搬家也带着它走，
        // 所以键必须留在 LlmConfig / render_yaml / mock 里。留着按钮等于让用户以为
        // WB 的总结会按这个值走，那是空控件假承诺。真要接这条路，先接出网再放控件。
        {
          path: "llm.max_input_chars",
          label: "单次输入上限（字）",
          type: "number",
          group: "advanced",
          help: "超过就按下面的分块大小切开分别摘要，最后合稿。"
        },
        { path: "llm.chunk_size", label: "分块大小（字）", type: "number", group: "advanced" },
        { path: "llm.chunk_overlap", label: "分块重叠（字）", type: "number", group: "advanced" },
        { path: "llm.timeout_seconds", label: "请求超时（秒）", type: "number", group: "advanced", help: "长文摘要与思考模型把这里放宽。" },
        { path: "llm.proxy", label: "代理", type: "text", group: "advanced", placeholder: "http://127.0.0.1:7897", help: "只作用于模型请求；yt-dlp 用下面的下载代理。" },
        {
          path: "llm.user_agent",
          label: "请求 UA",
          group: "advanced",
          type: "text",
          placeholder: "留空用内置浏览器 UA",
          help: "内置的是浏览器 UA——Cloudflare 的 1010 规则按客户端指纹拦人，填成程序名会被 403。一般不用改。"
        },
        {
          path: "llm.extra_headers",
          label: "额外请求头",
          type: "headers",
          group: "advanced",
          placeholder: "x-opencode-session: 任意字符串",
          help: "每行一个「名字: 值」。有些网关要求私有头：opencode zen 的对话接口必须带 x-opencode-session（这里已自动补），Azure 风格要 api-version。"
        },
        // 任务 → 服务商/模型 的映射，同样不是普通输入框（每行一个下拉）。
        { path: "llm.tasks", label: "任务模型", type: "taskmap", group: "advanced" }
      ]
    },
    {
      key: "storage",
      title: "存储",
      hint: "路径支持 %APPDATA% 等环境变量，留空用默认位置。",
      fields: [
        { path: "storage.media_root", label: "媒体根目录", type: "path", kind: "dir" },
        { path: "storage.cache_root", label: "缓存目录", type: "path", kind: "dir" },
        { path: "storage.models_root", label: "模型目录", type: "path", kind: "dir", help: "faster-whisper 模型权重放这里，换机器时整目录拷走即可。" },
        { path: "storage.ffmpeg_dir", label: "ffmpeg 目录", type: "path", kind: "dir", help: "不填则用 PATH 里的 ffmpeg。" },
        { path: "storage.retention.source_days", label: "源视频保留天数", type: "number", help: "0 = 不自动清理。" },
        { path: "storage.retention.audio_days", label: "音频保留天数", type: "number" },
        { path: "storage.retention.frames_days", label: "截帧保留天数", type: "number" }
      ]
    },
    {
      key: "collect",
      title: "采集",
      hint: "字幕、弹幕、截帧。",
      fields: [
        { path: "collect.prefer_subtitle_lang", label: "优先字幕语言", type: "list", help: "逗号分隔，按顺序命中。全部落空才转录音频。" },
        { path: "collect.include_platform_summary", label: "收集平台简介", type: "switch" },
        { path: "collect.danmaku.enabled", label: "采集弹幕", type: "switch" },
        { path: "collect.danmaku.max_items", label: "弹幕上限", type: "number" },
        { path: "collect.frames.enabled", label: "截帧", type: "switch", help: "总结时附上画面：PPT、图表、代码里的字能进正文。关掉省图片 token，也少一次本地抽帧。" },
        { path: "collect.frames.max_frames", label: "每稿最多截帧", type: "number", help: "按视频时长等距抽取，越多越贵。" }
      ]
    },
    {
      key: "process",
      title: "下载与转录",
      hint: "并发是硬约束：下载最多 2，转录固定 1（显存排队反而更慢）。",
      fields: [
        { path: "download.quality", label: "画质上限", type: "select", options: [["4k", "4K"], ["1080p", "1080p"], ["720p", "720p"], ["480p", "480p"]] },
        { path: "download.codec", label: "编码偏好", type: "select", options: [["h264", "h264（兼容优先）"], ["hevc", "hevc（体积小）"], ["av1", "av1"]] },
        { path: "download.concurrency", label: "下载并发", type: "number" },
        { path: "download.dir_template", label: "目录模板", type: "text", help: "可用 {up} {collection} {title} {page_title}。" },
        { path: "download.backend", label: "下载后端", type: "select", options: [["ytdlp", "yt-dlp（默认）"], ["bbdown", "BBDown"]], help: "BBDown 是兜底：默认仍走 yt-dlp，失败后在任务上点「用 BBDown 重试」即可，不会自动切换。" },
        { path: "download.bbdown_path", label: "BBDown 路径", type: "path", kind: "file", help: "留空则自动找 PATH、tools/ 与当前目录。" },
        { path: "download.bbdown_args", label: "BBDown 附加参数", type: "text", placeholder: "--dfn 1080P", help: "原样追加到命令行；不填也能用。" },
        { path: "transcribe.model", label: "转录模型", type: "select", options: [["large-v3-turbo", "large-v3-turbo"], ["large-v3", "large-v3"], ["medium", "medium"], ["small", "small"], ["base", "base"]] },
        { path: "transcribe.device", label: "推理设备", type: "select", options: [["auto", "自动"], ["cuda", "CUDA"], ["cpu", "CPU"]] },
        { path: "transcribe.compute_type", label: "计算精度", type: "select", options: [["auto", "自动"], ["float16", "float16"], ["int8_float16", "int8_float16"], ["int8", "int8"]] },
        { path: "transcribe.vad", label: "静音过滤（VAD）", type: "switch" },
        // 这里给的是「默认源语言」，只影响 CLI 与无前端确认的路径——素材库点
        // 「开始转录」时一定会再问一次，那里能选全部 100 种，所以下拉只放常用项。
        // 以前是自由文本框，随手写个 zh-CN 就会在 tokenizer 里炸（它只认 ISO 639-1）。
        { path: "transcribe.language", label: "源语言", type: "select", options: [["auto", "自动检测"], ["zh", "中文"], ["en", "英语"], ["ja", "日语"], ["yue", "粤语"], ["ko", "韩语"], ["ru", "俄语"], ["de", "德语"]], help: "auto 会先做语言探测，只看开头一小段，容易判错；已知语种时选死更稳。" }
      ]
    },
    {
      key: "subtitle",
      title: "字幕与翻译",
      hint: "AI 优化补标点、机器翻译出双语。译文永远是草稿——要在字幕编辑器里逐条确认才会写进文件。",
      fields: [
        { path: "subtitle.optimize", label: "允许 AI 优化字幕", type: "switch", help: "关掉后字幕编辑器不显示「AI 优化」。" },
        { path: "subtitle.translate.target", label: "默认目标语言", type: "select", options: [["auto", "自动（中文→英文，其余→中文）"], ["zh", "中文"], ["en", "英文"], ["ja", "日文"]] },
        { path: "subtitle.translate.layout", label: "双语版式", type: "select", options: [["original_top", "原文在上"], ["translated_top", "译文在上"], ["original_only", "仅原文"], ["translated_only", "仅译文"]] },
        { path: "subtitle.translate.bilingual", label: "生成双语 ASS", type: "switch", help: "关掉就只出单语 SRT。" }
      ]
    },
    {
      key: "clip",
      title: "切片",
      hint: "候选时长区间与数量。长段用于「合集拆条」，高光用于「抽爆点」。",
      fields: [
        { path: "clip.highlight_min_seconds", label: "高光最短（秒）", type: "number" },
        { path: "clip.highlight_max_seconds", label: "高光最长（秒）", type: "number" },
        { path: "clip.segment_min_seconds", label: "长段最短（秒）", type: "number" },
        { path: "clip.segment_max_seconds", label: "长段最长（秒）", type: "number" },
        { path: "clip.candidate_count", label: "候选数量", type: "number" },
        { path: "clip.vertical_mode", label: "竖版裁切", type: "select", options: [["center", "居中"], ["face", "跟随人脸"]] }
      ]
    },
    {
      key: "publish",
      title: "发布",
      hint: "默认每一步都要人确认。笔记只发标题与正文，摘要不再发。",
      fields: [
        { path: "publish.require_confirm", label: "发布前确认", type: "switch", help: "关掉意味着点了就发。建议保持开启。" },
        { path: "publish.max_comment_chars", label: "评论长度上限", type: "number" },
        { path: "publish.disclose_ai", label: "标注 AI 生成", type: "switch" },
        { path: "publish.disclosure_text", label: "标注文案", type: "text" },
        { path: "publish.min_interval_seconds", label: "最小发布间隔（秒）", type: "number" }
      ]
    },
    {
      key: "about",
      title: "关于",
      hint: "出问题时要给人看的东西都在这一页：日志在哪、诊断包里有什么。",
      fields: []
    }
  ];

  // ------------------------------------------------------------ 工具

  function getPath(obj, path) {
    return path.split(".").reduce(function (acc, key) {
      if (acc === null || acc === undefined) return undefined;
      return acc[key];
    }, obj);
  }

  /**
   * 编辑用的副本：数组/对象必须深拷一份。
   *
   * 直接引用 base 里的对象会踩坑——卡片改模型是就地改（`p.models = [...]`），
   * 而标脏靠 `JSON.stringify(current) !== JSON.stringify(base)`：两边指向同一个
   * 对象时永远相等，于是「改过但保存按钮还是灰的」。深拷一次，脏判定才有意义。
   */
  function copyValue(value) {
    if (value === null || value === undefined) return value;
    if (typeof value !== "object") return value;
    return JSON.parse(JSON.stringify(value));
  }

  /** 把「改过的字段」按段落归拢成后端认识的嵌套结构。 */
  function buildPatch(dirty, values) {
    var patch = {};
    Object.keys(dirty).forEach(function (path) {
      if (!dirty[path]) return;
      var bits = path.split(".");
      var cursor = patch;
      for (var i = 0; i < bits.length - 1; i++) {
        cursor[bits[i]] = cursor[bits[i]] || {};
        cursor = cursor[bits[i]];
      }
      cursor[bits[bits.length - 1]] = values[path];
    });
    return patch;
  }

  // ------------------------------------------------------------ 页面

  function render(ctx) {
    var params = (ctx && ctx.params) || {};
    var activeKey = params.section || SECTIONS[0].key;
    var page = ui.h("section", { class: "page" });
    var body = ui.h("div", { id: "settings-body" });
    page.appendChild(
      ui.h("div", { class: "page__head" }, [
        ui.h("h2", { class: "page__title", text: "设置" }),
        ui.h("p", { class: "page__hint", text: "改动即时生效的项不需要重开程序；标注「需重启」的会提示。" })
      ])
    );
    page.appendChild(body);
    load(body, activeKey);
    return page;
  }

  // 配置里那个「默认角色」到底能不能直接用，判定只在后端一处（core/summarizer.resolve_role，
  // 退役与拼错的措辞也在那里定）。这里只当读者：summary.roles 带下 configured_hint 就挂一行，
  // 不带就什么都不显示。前端不再自己列退役角色名单——那会变成第二个 owner，下次退役就得改这里。
  var roleWarnNode = null;
  var roleIssueText = "";
  var roleSrcNode = null;        // 角色真源清单那一行（每个角色当前吃哪个 txt）
  var roleSourceText = "";
  var roleAdvNode = null;        // 「上下文与长素材」那个折叠区，默认角色就在里面
  var roleIssueData = null;      // 后端最近一次的结论，paint 之后要重新贴一次
  var roleDefault = "";          // 后端 summary.roles 给的 default：配置不可用时下拉框选它
  var roleOptionList = null;     // 后端 summary.roles 给的名单（顺序/名称/轮数），null＝还没拿到

  // 配置里那个角色不在选项中时，下拉框必须选后端给的那个 default，而不是列表第一项。
  // 「退到第一项」是 DOM 的副作用：`selected: String(value) === String(opt[0])` 在所有
  // 选项都不匹配时，浏览器就把第一项当成选中态显示出来——于是红字说「本次按默认角色
  // 『文稿编辑』」，框里却亮着列表第一项，用户顺手一保存，配置就被改成他没选过的角色。
  // （当年亮着的是「学术」；那个角色后来被删了，所以这里不点名，免得下一个读代码的人
  // 去角色表里找一个已经不存在的 key。）
  // 这里不写 "wengao" 字面量：default 由后端 resolve_role 定（配置干净时它等于配置值本身，
  // 脏时就是它真正会用的兜底），抄一份进来等于多了第二个 owner，下次换默认角色必漂。
  // 角色名单的唯一权威是后端（video_workbench/core/summary/__init__.py 的 ALL_ROLES）：
  // 顺序、显示名、几轮，全从 summary.roles 那一次返回里现取，不在前端再数一遍。
  // 读到就用这份，读不到才退回上面那张字面量兜底表——reject 时页面必须照常可用，
  // 这条需求钉在 test_roles_endpoint_failure_does_not_break_the_settings_page。
  // 标签里的「三轮/单轮」按 passes 推，不点名任何角色：task-31 合并掉 shipin 之后
  // 清单里全是三轮，但轮数是角色的属性、不是名单的属性——写死任何一边，
  // 下次加/退役角色都会把它变成假话。
  function roleOptionsFrom(roles) {
    var list = roles || [];
    if (!list.length) return null;
    return list.map(function (item) {
      var label = String(item.name || item.key);
      return [String(item.key), label + (Number(item.passes) >= 3 ? "（三轮）" : "（单轮）")];
    });
  }

  // 这一栏当前该用哪份选项：后端名单优先，没有就用字段自带的兜底表。
  function roleOptionsOf(field) {
    return roleOptionList || (field && field.options) || [];
  }

  function roleSelectValue(field, configured) {
    var keys = roleOptionsOf(field).map(function (opt) {
      return String(opt[0]);
    });
    var want = String(configured || "");
    if (keys.indexOf(want) >= 0) return want;            // 配置可用：照配置选，行为不变
    var fallback = String(roleDefault || "");
    // 读不到后端结论时（summary.roles 失败）退回配置值本身：宁可不选，也不自己编一个。
    return keys.indexOf(fallback) >= 0 ? fallback : want;
  }

  // 折叠区里有两个「用户需要看得见」的东西：脏角色的红字，和附加提示词这个开关本身。
  // 「附加提示词」默认关，而它就躺在这组里：不展开等于这个开关不存在（task-17 的 V3
  // 要求用户在 20 秒内能点到它）。所以**本会话第一次进设置页**、且它还是关着的时候，
  // 把这一组展开一次；开着的人显然已经找到用过它，不展开。用户随后自己收起，我们不再
  // 插手——`extraGroupForced` 在整个页面生命周期里只放行一次，重复进出页面不会反复强开。
  var extraGroupForced = false;

  function expandExtraGroup(current) {
    if (extraGroupForced || !roleAdvNode) return;
    if (!current || current["llm.extra_prompt"]) return;   // 开着 = 已经找到它，别抢展开
    extraGroupForced = true;
    roleAdvNode.open = true;
  }

  function expandRoleGroup() {
    if (!roleIssueText || !roleAdvNode) return;
    roleAdvNode.open = true;
  }

  function applyRoleIssue(data) {
    // 两个字段是一对：configured_unrecognized 是那个脏值本身，configured_hint 是说明。
    // 只认 hint 的话，脏值就没人读了；这里要求两者同时非空才显示——旧后端只带 hint
    // 时也不会挂出一行没有对象的提示。
    roleIssueData = data || null;
    // default 顺手记下来：它是后端 resolve_role 的结论，配置值不可用时下拉框就选它。
    // 放在早退之前——第一次进页面时提示行还不存在，但结论得先存住，paint 之后才用得上。
    roleDefault = String((data && data.default) || "");
    // 名单同样在这里存：选项的顺序/显示名/轮数都跟随后端，加角色不必再改前端。
    // applyRoleIssue 在 paint 之前会先跑一次，所以第一次画就已经是后端的名单；
    // 读失败时 roleOptionsFrom 给 null，roleOptionsOf 自动退回那张字面量兜底表。
    roleOptionList = roleOptionsFrom(data && data.roles);
    // 真源清单也在这里存：它是同一次 summary.roles 的返回，不值得为它多发一次请求。
    roleSourceText = describeRoleSources(data && data.roles);
    var dirty = (data && data.configured_unrecognized) || "";
    roleIssueText = dirty ? ((data && data.configured_hint) || "") : "";
    if (roleSrcNode) {
      roleSrcNode.textContent = roleSourceText;
      roleSrcNode.style.display = roleSourceText ? "" : "none";
    }
    if (!roleWarnNode) return;
    roleWarnNode.textContent = roleIssueText;
    roleWarnNode.style.display = roleIssueText ? "" : "none";
    expandRoleGroup();
  }

  // 一行一个角色：有 txt 真源的写文件名 + 字节数 + sha 前 8 位，没有的写清楚用的是
  // 内置压缩版。为什么要把 sha 摆出来：用户改完 txt 想确认「生效的是不是这一份」，
  // 看哈希比看文件名可靠；而字节数只有后端知道（prompt_source 现算，不存副本）。
  function describeRoleSources(roles) {
    var list = roles || [];
    if (!list.length) return "";
    var lines = [];
    list.forEach(function (item) {
      if (!item || !item.key) return;
      if (item.prompt_source) {
        lines.push(item.key + " ← " + item.prompt_source +
          " · " + (item.prompt_bytes || 0) + " B" +
          (item.prompt_sha8 ? " · " + item.prompt_sha8 : ""));
      } else {
        lines.push(item.key + " ← 无 txt 真源，用内置压缩版");
      }
    });
    return lines.join("\n");
  }

  function refreshRoleIssue() {
    return WB.rpc("summary.roles", {}, { silent: true }).then(applyRoleIssue, function () {
      applyRoleIssue(null);   // 读不到就当没提示：这一页的主功能不能因此打不开
    });
  }

  function load(body, activeKey) {
    body.textContent = "";
    body.appendChild(ui.h("div", { class: "empty" }, [ui.h("div", { class: "empty__hint", text: "正在读取设置…" })]));

    Promise.all([WB.rpc("app.settings.get"), refreshRoleIssue()])
      .then(function (done) {
        paint(body, done[0], activeKey);
        // 后端结论到得比表单早，那时提示行和折叠区都还没被建出来；画完之后按同一条结论补一次，
        // 脏的时候这一组才会被展开。
        applyRoleIssue(roleIssueData);
      })
      .catch(function (err) {
        body.textContent = "";
        body.appendChild(
          ui.empty("读不到设置", (err && err.message) || "配置文件不可读", ui.h("button", { class: "btn", text: "重试", onclick: function () { load(body, activeKey); } }))
        );
      });
  }

  function paint(body, settings, activeKey) {
    var base = settings;
    var current = {};
    var dirty = {};

    // 用一份副本承载编辑中的值，保存成功后再把它变成新的 base
    SECTIONS.forEach(function (section) {
      section.fields.forEach(function (field) {
        current[field.path] = copyValue(getPath(base, field.path));
      });
    });

    body.textContent = "";

    var nav = ui.h("div", { class: "settings__nav" });
    var form = ui.h("div", { class: "settings__form" });
    var sectionTitle = ui.h("h3", { class: "card__title", text: "" });
    var sectionHint = ui.h("p", { class: "page__hint", text: "" });
    var fieldsHost = ui.h("div", { class: "col", style: "gap:18px" });

    var saveBtn = ui.h("button", {
      class: "btn btn--primary",
      text: "保存",
      disabled: true,
      onclick: function () {
        save();
      }
    });
    var resetBtn = ui.h("button", {
      class: "btn",
      text: "放弃改动",
      disabled: true,
      onclick: function () {
        Object.keys(current).forEach(function (path) {
          current[path] = getPath(base, path);
        });
        dirty = {};
        showSection(activeSection.key);
        refreshBar();
      }
    });
    var dirtyLabel = ui.h("span", { class: "muted", text: "没有改动" });

    var bar = ui.h("div", { class: "settings__bar" }, [dirtyLabel, ui.h("div", { class: "row", style: "gap:8px" }, [resetBtn, saveBtn])]);

    var activeSection = SECTIONS[0];

    function refreshBar() {
      var count = Object.keys(dirty).filter(function (k) {
        return dirty[k];
      }).length;
      saveBtn.disabled = count === 0;
      resetBtn.disabled = count === 0;
      dirtyLabel.textContent = count === 0 ? "没有改动" : count + " 项待保存";
      dirtyLabel.className = count === 0 ? "muted" : "muted accent";
    }

    function markDirty(path, value) {
      current[path] = value;
      dirty[path] = JSON.stringify(value) !== JSON.stringify(getPath(base, path));
      refreshBar();
    }

    function showSection(key) {
      var section = SECTIONS.filter(function (s) {
        return s.key === key;
      })[0] || SECTIONS[0];
      activeSection = section;
      sectionTitle.textContent = section.title;
      sectionHint.textContent = section.hint || "";
      fieldsHost.textContent = "";
      // group:"advanced" 的字段不进主表单，收进折叠区（模型分区的「上下文与长素材」）。
      var advanced = [];
      section.fields.forEach(function (field) {
        if (field.type === "providers") {
          fieldsHost.appendChild(renderProviders(current, markDirty));
          return;
        }
        if (field.group === "advanced") {
          advanced.push(field);
          return;
        }
        fieldsHost.appendChild(renderField(field, current[field.path], function (value) {
          markDirty(field.path, value);
        }));
      });
      if (advanced.length) fieldsHost.appendChild(renderAdvanced(section.key, advanced, current, markDirty));
      // 字幕分区带术语表：它不是普通配置项，是一份要编辑的表。
      if (section.key === "subtitle") {
        fieldsHost.appendChild(renderGlossary());
        fieldsHost.appendChild(renderStylesLink());
      }
      // 关于分区没有配置项，全是动作按钮。
      if (section.key === "about") fieldsHost.appendChild(renderAbout());
      // 下载分区多一条后端体检：BBDown 装没装，只有真探测一次才知道。
      if (section.key === "process") fieldsHost.appendChild(renderBackends());
      var items = nav.querySelectorAll(".settings__nav-item");
      for (var i = 0; i < items.length; i++) {
        if (items[i].dataset.key === key) items[i].setAttribute("aria-current", "true");
        else items[i].removeAttribute("aria-current");
      }
      // 记住当前分区，切页回来还在原地（file:// 下 replaceState 偶发拒绝，不影响功能）
      if (WB.router.current() && WB.router.current().route === "settings") {
        try {
          history.replaceState(null, "", "#/settings?section=" + key);
        } catch (_err) {
          /* 忽略：地址栏没记住而已，不阻断渲染 */
        }
      }
    }

    function save() {
      var patch = buildPatch(dirty, current);
      if (!Object.keys(patch).length) return;
      saveBtn.disabled = true;
      saveBtn.textContent = "保存中…";
      WB.rpc("app.settings.put", { values: patch }, { silent: true })
        .then(function (data) {
          base = (data && data.settings) || base;
          SECTIONS.forEach(function (section) {
            section.fields.forEach(function (field) {
              // 这里也必须深拷：回填成同一个对象引用，下一次就地改就再也标不出脏了
              // （症状是「保存过一次之后，再改模型列表就不提示待保存」）。
              current[field.path] = copyValue(getPath(base, field.path));
            });
          });
          dirty = {};
          saveBtn.textContent = "保存";
          refreshBar();
          // 角色改完立刻回查一次：改对了那行提示就该消失，还不对就换一句新的说明。
          // 提示的内容始终来自后端，不在这里做判断。
          refreshRoleIssue();
          // 皮肤/折叠这类即时项要让外壳跟着变
          if (patch.ui && patch.ui.skin && WB.applySkin) WB.applySkin(data.settings.ui.skin);
          if (patch.ui && typeof patch.ui.rail_collapsed === "boolean" && WB.applyRail) WB.applyRail(patch.ui.rail_collapsed);
        })
        .catch(function (err) {
          saveBtn.textContent = "保存";
          refreshBar();
          // 失败时不清 dirty：让用户能改完再试，而不是重新填一遍
          WB.toast((err && err.message) || "保存失败", "error");
        });
    }

    SECTIONS.forEach(function (section) {
      nav.appendChild(
        ui.h("button", {
          class: "settings__nav-item",
          type: "button",
          dataset: { key: section.key },
          text: section.title,
          onclick: function () {
            showSection(section.key);
          }
        })
      );
    });

    form.appendChild(ui.h("div", { class: "card" }, [
      ui.h("div", { class: "card__head" }, [ui.h("div", { class: "col", style: "gap:4px" }, [sectionTitle, sectionHint])]),
      ui.h("div", { class: "card__body" }, [fieldsHost])
    ]));

    body.appendChild(ui.h("div", { class: "settings" }, [nav, form]));
    body.appendChild(bar);
    showSection(activeKey);
    refreshBar();
  }

  // ------------------------------------------------------------ 样式页入口
  //
  // 样式（字号/描边/圆角条）不是"一个配置值"，是一整套预设 + 预览，塞进设置页会让
  // 9 个分区的声明式表单破功。这里只放入口，编辑在独立页面（方案书 5.11）。
  function renderStylesLink() {
    var node = ui.h("div", { class: "settings__row" }, [
      ui.h("span", { class: "field__label", text: "全局字幕样式" }),
      ui.h("div", { class: "settings__row-actions" }, [
        ui.h("span", { class: "muted",
                       text: "这里改的是全局默认；单个视频可以在字幕页单独覆盖" }),
        (function () {
          var btn = ui.h("button", {
            class: "btn btn--ghost",
            text: "打开全局样式页",
            attrs: { type: "button" }
          });
          btn.addEventListener("click", function () {
            // 不带 project_id = 全局作用域。带了就变成改某个视频，入口不能含糊。
            WB.router.go("styles", {});
          });
          return btn;
        })()
      ])
    ]);
    return node;
  }

  /** 「关于」：版本、日志目录、打开日志、导出诊断包、复制最近日志。 */
  function renderAbout() {
    var info = { log_dir: "", log_file: "" };
    var versionText = ui.h("span", { class: "muted", text: "读取中…" });
    var dirText = ui.h("span", { class: "muted", text: "—" });
    var note = ui.h("span", { class: "muted", text: "" });
    var exportBtn = ui.h("button", { class: "btn btn--ghost", type: "button", text: "导出诊断包" });
    var revealBtn = ui.h("button", { class: "btn btn--ghost", type: "button", text: "打开日志目录" });
    var copyBtn = ui.h("button", { class: "btn btn--ghost", type: "button", text: "复制最近日志" });
    // 体检报告是排障第一站：换机器、换 FFmpeg 路径之后用户得能重看一遍
    var onboardBtn = ui.h("button", { class: "btn btn--ghost", type: "button", text: "运行环境体检" });
    onboardBtn.addEventListener("click", function () {
      if (WB.router) WB.router.go("onboard", {});
    });

    WB.rpc("app.info", {}, { silent: true })
      .then(function (data) {
        info = data || info;
        versionText.textContent = (data.name || "VideoWorkbench") + " " + (data.version || "");
        dirText.textContent = data.log_dir || "（尚未生成）";
      })
      .catch(function () {
        versionText.textContent = "读不到版本信息";
      });

    revealBtn.addEventListener("click", function () {
      if (!info.log_dir) {
        note.textContent = "还没有日志目录（先随便跑一个任务）";
        return;
      }
      WB.rpc("fs.reveal", { path: info.log_dir }, { silent: true }).then(function () {
        note.textContent = "";
      }).catch(function (err) {
        note.textContent = "打不开：" + (err && err.message ? err.message : "未知原因");
      });
    });

    exportBtn.addEventListener("click", function () {
      exportBtn.disabled = true;
      exportBtn.textContent = "打包中…";
      note.textContent = "";
      WB.rpc("app.diagnostics", { log_lines: 400 }, { silent: true })
        .then(function (data) {
          note.textContent = "已导出：" + data.path + "（" + Math.round((data.bytes || 0) / 1024) + " KB）";
          WB.toast("诊断包已导出", "success");
          if (data.path) WB.rpc("fs.reveal", { path: data.path }, { silent: true }).catch(function () {});
        })
        .catch(function (err) {
          note.textContent = "导出失败：" + (err && err.message ? err.message : "未知原因");
        })
        .then(function () {
          exportBtn.disabled = false;
          exportBtn.textContent = "导出诊断包";
        });
    });

    copyBtn.addEventListener("click", function () {
      note.textContent = "";
      WB.rpc("app.log_tail", { lines: 200 }, { silent: true })
        .then(function (data) {
          var text = String(data.text || "");
          if (navigator.clipboard && navigator.clipboard.writeText) {
            return navigator.clipboard.writeText(text).then(function () {
              note.textContent = "已复制 " + (data.lines || 0) + " 行（凭据已抹掉）";
            });
          }
          note.textContent = "这个环境不让写剪贴板，去日志目录里看吧";
          return null;
        })
        .catch(function (err) {
          note.textContent = "复制失败：" + (err && err.message ? err.message : "未知原因");
        });
    });

    return ui.h("div", { class: "settings__about" }, [
      ui.h("div", { class: "settings__row" }, [
        ui.h("span", { class: "field__label", text: "版本" }),
        ui.h("div", { class: "settings__row-actions" }, [versionText])
      ]),
      ui.h("div", { class: "settings__row" }, [
        ui.h("span", { class: "field__label", text: "日志目录" }),
        ui.h("div", { class: "settings__row-actions" }, [dirText])
      ]),
      ui.h("div", { class: "settings__row" }, [
        ui.h("span", { class: "field__label", text: "排障" }),
        ui.h("div", { class: "settings__row-actions" }, [
          ui.h("span", { class: "muted", text: "诊断包 = 环境 + 脱敏配置 + 最近日志；不发到任何服务器，落在本地" })
        ])
      ]),
      ui.h("div", { class: "settings__row" }, [
        ui.h("span", { class: "field__label", text: "" }),
        ui.h("div", { class: "settings__row-actions" }, [onboardBtn, revealBtn, exportBtn, copyBtn])
      ]),
      ui.h("div", { class: "settings__row" }, [
        ui.h("span", { class: "field__label", text: "" }),
        ui.h("div", { class: "settings__row-actions" }, [note])
      ])
    ]);
  }

  // ------------------------------------------------------------ 单字段

  /** 「测试连接」：真打一次模型。失败的三种情况要分得清，否则用户只会以为坏了。 */
  /**
   * 下载后端体检（P1-08）。两行：yt-dlp 与 BBDown。
   *
   * 只显示"找没找到 + 在哪"——不显示版本，是因为探测不为读版本去起进程；
   * 真的跑起来报错时错误信息里自然会带版本。
   */
  function renderBackends() {
    var box = ui.h("div", { class: "settings__backends col", style: "gap:8px" });
    var note = ui.h("span", { class: "muted", text: "" });

    function row(label, data) {
      var ok = !!(data && data.found);
      var text = ok ? (data.name || data.path || "已就绪") : ((data && data.hint) || "没检测到");
      return ui.h("div", { class: "settings__backend row", style: "gap:10px;align-items:center" }, [
        ui.h("span", { class: "settings__backend-dot" + (ok ? " is-ok" : " is-bad") }),
        ui.h("strong", { text: label }),
        ui.h("span", { class: "muted", text: text })
      ]);
    }

    function paint(data) {
      box.innerHTML = "";
      box.appendChild(row("yt-dlp", data && data.ytdlp));
      box.appendChild(row("BBDown", data && data.bbdown));
      note.textContent = "默认后端：" + ((data && data.default) || "ytdlp") + "。BBDown 只在失败项上手动重试时使用。";
      box.appendChild(note);
    }

    var btn = ui.h("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "重新检测",
      onclick: function () {
        btn.disabled = true;
        WB.rpc("download.backends", {}, { silent: true })
          .then(paint)
          .catch(function (err) {
            note.className = "muted";
            note.textContent = (err && err.message) || "检测失败";
          })
          .then(function () {
            btn.disabled = false;
          });
      }
    });

    WB.rpc("download.backends", {}, { silent: true }).then(paint).catch(function () {});
    return ui.h("div", { class: "col", style: "gap:8px" }, [box, btn]);
  }

  // ------------------------------------------------------------ 服务商卡片
  //
  // 一张卡 ＝ 一个服务商（地址 + 密钥 + 模型列表）。为什么要多张卡：
  // 小村的实际用法是"便宜模型跑切片分析、贵模型跑总结"，单一 base_url 做不到；
  // 而 bili_scribe 只读扁平的 llm.base_url/api_key/model，所以后端把当前这张卡
  // 镜像回那三个字段（config.sync_providers），界面这边只管 providers 这一份真相。
  //
  // 输入框就地改模型、不整段重绘——重绘会把光标从正在打的字里踢出去。
  // 只有结构性变化（加卡、删卡、改模型标签、切当前）才 repaint。

  // 本机 magpie 网关：默认监听 3425，OpenAI 兼容，key 填什么都能过（它只信任本机回环请求）。
  // 之所以做成按钮而不是让人手抄地址：这三项（地址 / 密钥 / 模型名）手抄最容易错，
  // 而 magpie 的模型列表每次开都可能变。
  var MAGPIE_BASE = "http://127.0.0.1:3425/v1";
  var MAGPIE_KEY = "magpie";

  function magpieRow(arr, current, markDirty, repaint) {
    return ui.h("div", { class: "providers__extra" }, [
      ui.h("button", {
        class: "btn btn--ghost",
        type: "button",
        text: "接入本机 magpie",
        title: "探测 127.0.0.1:3425 上的 magpie 网关：建一张卡、设为当前、拉回它的模型列表",
        onclick: function () {
          var card = null;
          for (var i = 0; i < arr.length; i++) {
            if (String(arr[i].base_url || "").indexOf("127.0.0.1:3425") >= 0) {
              card = arr[i];
              break;
            }
          }
          if (!card) {
            card = {
              id: "magpie",
              name: "本机 magpie",
              protocol: "openai",
              base_url: MAGPIE_BASE,
              api_key: MAGPIE_KEY,
              models: []
            };
            arr.push(card);
          }
          card.base_url = card.base_url || MAGPIE_BASE;
          card.api_key = card.api_key || MAGPIE_KEY;
          current["llm.active_provider"] = card.id;
          markDirty("llm.providers", arr);
          markDirty("llm.active_provider", card.id);
          repaint();

          WB.rpc(
            "llm.models",
            { provider: card.id, base_url: card.base_url, api_key: card.api_key },
            { silent: true }
          )
            .then(function (data) {
              var ids = ((data && data.models) || []).map(function (m) {
                return m.id;
              });
              var merged = (card.models || []).slice();
              ids.forEach(function (id) {
                if (merged.indexOf(id) < 0) merged.push(id);
              });
              card.models = merged;
              // 模型没选过、或选的那个这次没出现，就落到第一个——否则点「生成总结」会报模型不存在
              if (!current["llm.model"] || merged.indexOf(current["llm.model"]) < 0) {
                if (merged.length) {
                  current["llm.model"] = merged[0];
                  markDirty("llm.model", merged[0]);
                }
              }
              markDirty("llm.providers", arr);
              repaint();
              ui.toast("已接入本机 magpie：" + merged.length + " 个模型", "ok");
            })
            .catch(function (err) {
              repaint();
              var why = (err && err.message) || "连不上";
              ui.toast("没连上本机 magpie（" + MAGPIE_BASE + "）：" + why, "error");
            });
        }
      }),
      ui.h("span", {
        class: "muted",
        text: "同一台机器上跑着 magpie 时用这个，自动填地址与密钥"
      })
    ]);
  }

  function renderProviders(current, markDirty) {
    var box = ui.h("div", { class: "providers" });

    function list() {
      var arr = current["llm.providers"];
      if (!Array.isArray(arr)) {
        arr = [];
        current["llm.providers"] = arr;
      }
      return arr;
    }

    function repaint() {
      box.textContent = "";
      var arr = list();
      if (!arr.length) {
        box.appendChild(ui.h("div", { class: "muted", text: "还没有服务商。底下这张空卡先填起来。" }));
      }
      arr.forEach(function (p, i) {
        box.appendChild(providerCard(p, i, arr, current, markDirty, repaint));
      });
      box.appendChild(
        ui.h("button", {
          class: "btn btn--ghost",
          type: "button",
          text: "添加服务商",
          onclick: function () {
            arr.push({
              id: "custom-" + Date.now().toString(36),
              name: "新服务商",
              protocol: "openai",
              base_url: "",
              api_key: "",
              models: []
            });
            markDirty("llm.providers", arr);
            repaint();
          }
        })
      );
      box.appendChild(magpieRow(arr, current, markDirty, repaint));
    }

    repaint();
    return box;
  }

  function providerCard(p, idx, arr, current, markDirty, repaint) {
    var activeId = current["llm.active_provider"] || (arr[0] && arr[0].id) || "";
    var isActive = p.id === activeId;

    // 就地改：不动数组引用，只改元素，然后标脏。
    function touch(key, value) {
      p[key] = value;
      markDirty("llm.providers", arr);
    }

    var head = ui.h("div", { class: "provider-card__head" }, [
      ui.h("input", {
        class: "input provider-card__name",
        value: p.name || "",
        placeholder: "服务商名称",
        title: "点这里可以改名称",
        oninput: function () {
          touch("name", this.value);
        }
      }),
      ui.h(
        "select",
        {
          class: "select",
          onchange: function () {
            touch("protocol", this.value);
          }
        },
        [ui.h("option", { value: "openai", text: "OpenAI 兼容", selected: (p.protocol || "openai") === "openai" })]
      ),
      isActive
        ? ui.h("span", { class: "chip chip--on", text: "当前使用" })
        : ui.h("button", {
            class: "btn btn--ghost",
            type: "button",
            text: "设为当前",
            onclick: function () {
              markDirty("llm.active_provider", p.id);
              current["llm.active_provider"] = p.id;
              repaint();
            }
          }),
      arr.length > 1
        ? ui.h("button", {
            class: "icon-btn",
            type: "button",
            title: "删除这家服务商",
            html: ui.svg("trash"),
            onclick: function () {
              var next = arr.slice(0, idx).concat(arr.slice(idx + 1));
              current["llm.providers"] = next;
              markDirty("llm.providers", next);
              if (p.id === (current["llm.active_provider"] || "")) {
                current["llm.active_provider"] = next[0] ? next[0].id : "";
                markDirty("llm.active_provider", current["llm.active_provider"]);
              }
              repaint();
            }
          })
        : null
    ]);

    var url = ui.h("input", {
      class: "input mono",
      value: p.base_url || "",
      placeholder: "https://api.deepseek.com/v1",
      oninput: function () {
        touch("base_url", this.value);
      }
    });

    var key = ui.h("input", {
      class: "input mono",
      type: "password",
      value: p.api_key || "",
      placeholder: "未配置",
      autocomplete: "off",
      oninput: function () {
        touch("api_key", this.value);
      }
    });
    var eye = ui.h("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "显示",
      onclick: function () {
        var hidden = key.type === "password";
        key.type = hidden ? "text" : "password";
        eye.textContent = hidden ? "隐藏" : "显示";
      }
    });

    var models = ui.h("div", { class: "chips" });
    (p.models || []).forEach(function (m, mi) {
      models.appendChild(
        ui.h("span", { class: "chip" }, [
          ui.h("span", { class: "mono", text: m }),
          ui.h("button", {
            class: "chip__x",
            type: "button",
            title: "移除这个模型",
            text: "×",
            onclick: function () {
              p.models = (p.models || []).slice(0, mi).concat((p.models || []).slice(mi + 1));
              touch("models", p.models);
              renderModelChips();
            }
          })
        ])
      );
    });
    var addModel = ui.h("input", {
      class: "input input--sm mono",
      placeholder: "模型名，回车添加",
      onkeydown: function (ev) {
        if (ev.key !== "Enter") return;
        var name = addModel.value.trim();
        if (!name) return;
        p.models = (p.models || []).concat([name]);
        touch("models", p.models);
        addModel.value = "";
        renderModelChips();
      }
    });

    function renderModelChips() {
      models.textContent = "";
      (p.models || []).forEach(function (m, mi) {
        models.appendChild(
          ui.h("span", { class: "chip" }, [
            ui.h("span", { class: "mono", text: m }),
            ui.h("button", {
              class: "chip__x",
              type: "button",
              title: "移除这个模型",
              text: "×",
              onclick: function () {
                p.models = (p.models || []).slice(0, mi).concat((p.models || []).slice(mi + 1));
                touch("models", p.models);
                renderModelChips();
              }
            })
          ])
        );
      });
      models.appendChild(addModel);
    }
    renderModelChips();

    // 「获取可用模型」：问这家密钥能用哪些模型，勾选后一次加进 chips。
    // 为什么要有：手打模型名是这类设置页最容易出错的一步（大小写、带不带日期后缀、
    // 网关前缀），而 /models 是标准 OpenAI 路径，能列就直接列。
    var fetchBtn = ui.h("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "获取可用模型",
      onclick: function () {
        openModelPicker();
      }
    });

    function openModelPicker() {
      // 外层放吸顶条、内层滚动放行：原来是同一个容器，条压在滚动区里
      // 把第一行的勾选框盖住了（截图上第一行只剩半截）。
      var box = ui.h("div", { class: "model-pick__wrap" });
      var status = ui.h("div", { class: "muted", text: "正在问这家密钥能用哪些模型…" });
      box.appendChild(status);

      var addBtn = ui.h("button", { class: "btn", type: "button", text: "保存所选", disabled: true });
      var cancel = ui.h("button", { class: "btn btn--ghost", type: "button", text: "取消" });
      var dlg = ui.modal({
        title: "可用模型 · " + (p.name || "服务商"),
        body: box,
        footer: [cancel, addBtn]
      });
      cancel.onclick = function () {
        dlg.close();
      };
      addBtn.onclick = function () {
        var picked = [];
        Array.prototype.forEach.call(box.querySelectorAll("input[type=checkbox]"), function (cb) {
          if (cb.checked) picked.push(cb.value);
        });
        // 勾选框表示「这家能用哪些」，所以勾选集合就是最终列表——取消勾选等于移除。
        // 只增不减（原来的行为）会让弹窗与卡片各说一套：卡片里明明有，弹窗里取消不掉。
        var kept = (p.models || []).filter(function (id) {
          return picked.indexOf(id) >= 0;
        });
        picked.forEach(function (id) {
          if (kept.indexOf(id) < 0) kept.push(id);
        });
        if (!kept.length && (p.models || []).length) {
          dlg.close();
          ui.confirm(
            "清空模型列表？",
            "这家服务商将没有任何模型，相关任务会报「未配置模型」。",
            { confirmText: "清空", danger: true }
          ).then(function (yes) {
            if (!yes) return;
            p.models = [];
            touch("models", []);
            renderModelChips();
          });
          return;
        }
        p.models = kept;
        touch("models", kept);
        renderModelChips();
        dlg.close();
      };

      WB.rpc(
        "llm.models",
        // 带上卡片里**当前**的值：用户常常是刚敲完地址和密钥就点这里，还没保存。
        { provider: p.id || "", base_url: p.base_url || "", api_key: p.api_key || "" },
        { silent: true }
      )
        .then(function (data) {
          var list = (data && data.models) || [];
          var have = p.models || [];
          box.textContent = "";
          var rows = ui.h("div", { class: "model-pick" });
          var bar = ui.h("div", { class: "model-pick__bar" }, [
            ui.h("span", { class: "muted", text: "共 " + list.length + " 个可用模型 · 勾选即这家可用，取消勾选会移除" }),
            ui.h("span", { class: "topbar__spacer" })
          ]);
          var all = ui.h("button", { class: "btn btn--ghost", type: "button", text: "全选" });
          var none = ui.h("button", { class: "btn btn--ghost", type: "button", text: "全不选" });
          bar.appendChild(all);
          bar.appendChild(none);
          box.appendChild(bar);

          list.forEach(function (item) {
            var id = typeof item === "string" ? item : item.id;
            var owned = (typeof item === "object" && item.owned_by) || "";
            var cb = ui.h("input", { type: "checkbox", value: id });
            cb.checked = have.indexOf(id) >= 0;
            rows.appendChild(
              ui.h("label", { class: "model-pick__row" }, [
                cb,
                ui.h("span", { class: "model-pick__id mono", text: id }),
                owned ? ui.h("span", { class: "muted", text: owned }) : null
              ])
            );
          });
          box.appendChild(rows);
          all.onclick = function () {
            Array.prototype.forEach.call(rows.querySelectorAll("input[type=checkbox]"), function (cb) {
              cb.checked = true;
            });
          };
          none.onclick = function () {
            Array.prototype.forEach.call(rows.querySelectorAll("input[type=checkbox]"), function (cb) {
              cb.checked = false;
            });
          };
          addBtn.disabled = false;
          probeResult.className = "muted accent";
          probeResult.textContent = "列出 " + list.length + " 个模型";
        })
        .catch(function (err) {
          var code = (err && err.code) || "";
          var why =
            code === "LLM_NOT_CONFIGURED"
              ? "先填好这家的 API 地址和密钥再获取"
              : (err && err.message) || "获取失败";
          box.textContent = "";
          box.appendChild(ui.h("div", { class: "muted danger", text: why }));
          if (err && err.detail) box.appendChild(ui.h("div", { class: "muted", text: String(err.detail).slice(0, 300) }));
          probeResult.className = "muted danger";
          probeResult.textContent = why;
        });
    }

    var probeResult = ui.h("span", { class: "muted", text: "" });
    var probeBtn = ui.h("button", {
      class: "btn btn--ghost",
      type: "button",
      text: "测试连接",
      onclick: function () {
        probeBtn.disabled = true;
        probeBtn.textContent = "测试中…";
        probeResult.className = "muted";
        probeResult.textContent = "";
        WB.rpc(
          "llm.test",
          {
            provider: p.id || "",
            model: (p.models || [])[0] || "",
            base_url: p.base_url || "",
            api_key: p.api_key || ""
          },
          { silent: true }
        )
          .then(function (data) {
            probeResult.className = "muted accent";
            probeResult.textContent = "连上了：" + (data.model || "") + " · " + (data.latency_ms || 0) + "ms";
          })
          .catch(function (err) {
            var code = (err && err.code) || "";
            probeResult.className = "muted";
            probeResult.textContent =
              code === "LLM_NOT_CONFIGURED" ? "这家还没填完地址 / 密钥 / 模型" : (err && err.message) || "连接失败";
          })
          .then(function () {
            probeBtn.disabled = false;
            probeBtn.textContent = "测试连接";
          });
      }
    });

    return ui.h("div", { class: "provider-card" + (isActive ? " is-active" : "") }, [
      head,
      ui.h("div", { class: "field" }, [ui.h("label", { class: "field__label", text: "API 地址" }), url]),
      ui.h("div", { class: "field" }, [
        ui.h("label", { class: "field__label", text: "API Key" }),
        ui.h("div", { class: "field__row" }, [key, eye]),
        ui.h("span", {
          class: "field__help",
          text: p.api_key ? "密钥只写进本机 config.yaml，界面上不回显全文。" : "留空则这家不可用，相关任务会明确报「未配置模型」。"
        })
      ]),
      ui.h("div", { class: "field" }, [ui.h("label", { class: "field__label", text: "模型" }), models]),
      ui.h("div", { class: "row", style: "gap:10px;align-items:center" }, [fetchBtn, probeBtn, probeResult])
    ]);
  }

  // ------------------------------------------------------------ 上下文与长素材（折叠区）

  function renderAdvanced(sectionKey, fields, current, markDirty) {
    var body = ui.h("div", { class: "adv__body" });
    fields.forEach(function (field) {
      if (field.type === "taskmap") {
        body.appendChild(renderTaskMap(current, markDirty));
        return;
      }
      body.appendChild(
        renderField(field, current[field.path], function (value) {
          markDirty(field.path, value);
        })
      );
    });
    var details = ui.h("details", { class: "adv" }, [
      ui.h("summary", { class: "adv__summary" }, [
        ui.h("span", { class: "adv__title", text: "上下文与长素材" }),
        ui.h("span", {
          class: "muted",
          text: "默认值就能用；长视频超时、切片分析想换便宜模型时再来改。"
        })
      ]),
      body
    ]);
    roleAdvNode = details;   // 脏结论到达时要把这一组展开（expandRoleGroup）
    expandRoleGroup();
    expandExtraGroup(current);   // 附加提示词默认关时，本会话第一次进页面替用户展开一次
    return details;
  }

  // ------------------------------------------------------------ 任务 → 服务商/模型

  var TASK_LABELS = [
    ["summary", "总结"],
    ["collection_overview", "合集总览"],
    ["translate", "翻译"],
    ["clip_analyze", "切片分析"],
    ["copy", "文案"],
    ["refine", "字幕优化"],
    ["vision", "画面精析"]
  ];

  function renderTaskMap(current, markDirty) {
    var providers = Array.isArray(current["llm.providers"]) ? current["llm.providers"] : [];
    var tasks = current["llm.tasks"] || {};
    if (!Object.keys(tasks).length) current["llm.tasks"] = tasks;

    var options = [["", "跟随当前服务商"]];
    providers.forEach(function (p) {
      (p.models || []).forEach(function (m) {
        options.push([p.id + "/" + m, (p.name || p.id) + " · " + m]);
      });
    });

    var body = ui.h("div", { class: "taskmap" });
    TASK_LABELS.forEach(function (pair) {
      var key = pair[0];
      var sel = ui.h(
        "select",
        {
          class: "select",
          onchange: function () {
            tasks[key] = sel.value;
            markDirty("llm.tasks", tasks);
          }
        },
        options.map(function (opt) {
          return ui.h("option", { value: opt[0], text: opt[1], selected: (tasks[key] || "") === opt[0] });
        })
      );
      body.appendChild(
        ui.h("div", { class: "taskmap__row" }, [ui.h("span", { class: "taskmap__label", text: pair[1] }), sel])
      );
    });

    return ui.h("div", { class: "field" }, [
      ui.h("label", { class: "field__label", text: "任务模型" }),
      body,
      ui.h("span", {
        class: "field__help",
        text: "没指定的任务用当前服务商与默认模型。取值是「服务商 id/模型名」，保存后由后端解析。"
      })
    ]);
  }
  /**
   * 术语表编辑器。一行一条 `原文=译文`，也可导入 CSV（表头 source,target）。
   * 它单独走 glossary.* 接口而不塞进 settings.patch：术语表要即时生效、要能
   * 导入导出，用"改完一起保存"的模式会把它的反馈周期拖长。
   */
  function renderGlossary() {
    var box = ui.h("textarea", {
      class: "textarea",
      rows: "8",
      placeholder: "AI 芯片=accelerator\r\ntransformer=Transformer 模型",
      text: "加载中…"
    });
    var note = ui.h("span", { class: "muted", text: "" });

    function paint(entries) {
      box.value = (entries || []).join("\n");
      note.textContent = (entries || []).length + " 条";
    }

    WB.rpc("glossary.get", {}, { silent: true })
      .then(function (data) { paint((data && data.entries) || []); })
      .catch(function () { box.value = ""; note.textContent = "读不到术语表"; });

    function busy(btn, text) {
      btn.disabled = true;
      btn.textContent = text;
      return function () {
        btn.disabled = false;
        btn.textContent = text === "保存中…" ? "保存" : (text === "导入中…" ? "导入 CSV" : "导出 CSV");
      };
    }

    var saveBtn = ui.h("button", {
      class: "btn btn--primary", type: "button", text: "保存",
      onclick: function () {
        var done = busy(saveBtn, "保存中…");
        WB.rpc("glossary.put", { entries: String(box.value || "").split(/\r?\n/) }, { silent: true })
          .then(function (data) {
            paint((data && data.entries) || []);
            WB.toast("术语表已保存（" + ((data && data.count) || 0) + " 条）", "success");
          })
          .catch(function (err) { WB.toast((err && err.message) || "保存失败", "error"); })
          .then(done);
      }
    });

    var importBtn = ui.h("button", {
      class: "btn btn--ghost", type: "button", text: "导入 CSV",
      onclick: function () {
        WB.rpc("fs.pick", { kind: "file" }, { silent: true })
          .then(function (data) {
            var path = (data && data.path) || "";
            if (!path) return null;
            var done = busy(importBtn, "导入中…");
            return WB.rpc("glossary.import_csv", { path: path, merge: true }, { silent: true })
              .then(function (res) {
                paint((res && res.entries) || []);
                WB.toast("新增 " + ((res && res.count) || 0) + " 条，同名源词已覆盖", "success");
              })
              .catch(function (err) { WB.toast((err && err.message) || "导入失败", "error"); })
              .then(done);
          })
          .catch(function () { /* 用户取消选择 */ });
      }
    });

    var exportBtn = ui.h("button", {
      class: "btn btn--ghost", type: "button", text: "导出 CSV",
      onclick: function () {
        var done = busy(exportBtn, "导出中…");
        WB.rpc("glossary.export_csv", {}, { silent: true })
          .then(function (data) { WB.toast("已导出到 " + ((data && data.path) || "媒体目录"), "success"); })
          .catch(function (err) { WB.toast((err && err.message) || "导出失败", "error"); })
          .then(done);
      }
    });

    return ui.h("div", { class: "field" }, [
      ui.h("span", { class: "field__label", text: "术语表（强制替换，一行一条）" }),
      box,
      ui.h("div", { class: "row", style: "gap:10px;align-items:center" }, [saveBtn, importBtn, exportBtn, note]),
      ui.h("span", {
        class: "field__help",
        text: "模型没译、把源词原样带出来时按这里替换；模型自己换了写法就管不了，那靠提示词。"
      })
    ]);
  }

  function renderField(field, value, onChange) {
    var control;

    if (field.type === "switch") {
      var caption = ui.h("span", { class: "muted", text: value ? "开启" : "关闭" });
      var box = ui.h("button", {
        class: "switch",
        type: "button",
        role: "switch",
        "aria-checked": value ? "true" : "false",
        onclick: function () {
          var next = box.getAttribute("aria-checked") !== "true";
          box.setAttribute("aria-checked", next ? "true" : "false");
          caption.textContent = next ? "开启" : "关闭";
          onChange(next);
        }
      }, [ui.h("span", { class: "switch__knob" })]);
      control = ui.h("div", { class: "row" }, [box, caption]);
      return row(field, control);
    }

    if (field.type === "segmented") {
      var wrap = ui.h("div", { class: "seg" });
      var buttons = [];
      (field.options || []).forEach(function (opt) {
        var b = ui.h("button", {
          class: "seg__item" + (String(value) === String(opt[0]) ? " is-on" : ""),
          type: "button",
          text: opt[1],
          onclick: function () {
            buttons.forEach(function (x) {
              x.className = "seg__item";
            });
            b.className = "seg__item is-on";
            onChange(opt[0]);
          }
        });
        buttons.push(b);
        wrap.appendChild(b);
      });
      return row(field, wrap);
    }

    if (field.type === "select") {
      // 「默认角色」这一栏在配置值不可用时不能退回列表第一项（DOM 的副作用），要选后端
      // 给的 default，见 roleSelectValue。选项本身也走后端名单（roleOptionsOf），
      // 其它下拉框行为不变。
      var picked = field.path === "llm.role" ? roleSelectValue(field, value) : value;
      var select = ui.h(
        "select",
        {
          class: "select",
          onchange: function () {
            onChange(select.value);
          }
        },
        (field.path === "llm.role" ? roleOptionsOf(field) : (field.options || [])).map(function (opt) {
          return ui.h("option", { value: opt[0], text: opt[1], selected: String(picked) === String(opt[0]) });
        })
      );
      control = select;
      return row(field, control);
    }

    if (field.type === "number") {
      // 必须 oninput，不能只靠 onchange：onchange 要等失焦才触发，而用户的动作是
      // 「改完数字直接去点保存」——点之前光标还在框里，脏标记还没起来，保存按钮就是灰的
      // （症状：改数字必须先点一下别的地方，"选中"了才算数）。边打边标脏。
      var num = ui.h("input", {
        class: "input mono",
        type: "number",
        value: value === null || value === undefined ? "" : String(value),
        oninput: function () {
          var raw = num.value;
          if (raw === "") {
            onChange(null);
            return;
          }
          var parsed = Number(raw);
          // 打到一半（"-"、"1e"）先不报；别把中间态写进脏表，否则保存下去是 NaN
          if (isNaN(parsed)) return;
          onChange(parsed);
        },
        onchange: function () {
          var raw = num.value;
          if (raw === "") {
            onChange(null);
            return;
          }
          var parsed = Number(raw);
          onChange(isNaN(parsed) ? null : parsed);
        }
      });
      control = num;
      return row(field, control);
    }

    if (field.type === "password") {
      var pw = ui.h("input", {
        class: "input mono",
        type: "password",
        value: value || "",
        placeholder: field.placeholder || "",
        autocomplete: "off",
        oninput: function () {
          onChange(pw.value);
        }
      });
      var eye = ui.h("button", {
        class: "btn btn--ghost",
        text: "显示",
        type: "button",
        onclick: function () {
          var hidden = pw.type === "password";
          pw.type = hidden ? "text" : "password";
          eye.textContent = hidden ? "隐藏" : "显示";
        }
      });
      control = ui.h("div", { class: "field__row" }, [pw, eye]);
      return row(field, control);
    }

    if (field.type === "list") {
      var listInput = ui.h("input", {
        class: "input",
        value: Array.isArray(value) ? value.join(", ") : "",
        oninput: function () {
          onChange(
            listInput.value
              .split(",")
              .map(function (s) {
                return s.trim();
              })
              .filter(Boolean)
          );
        }
      });
      control = listInput;
      return row(field, control);
    }

    if (field.type === "path") {
      var pathInput = ui.h("input", {
        class: "input mono",
        value: value || "",
        oninput: function () {
          onChange(pathInput.value);
        }
      });
      var pick = ui.h("button", {
        class: "btn",
        text: "选择…",
        type: "button",
        onclick: function () {
          pick.disabled = true;
          WB.rpc("fs.pick", { kind: field.kind || "dir" }, { silent: true })
            .then(function (data) {
              pick.disabled = false;
              if (data && data.path) {
                pathInput.value = data.path;
                onChange(data.path);
              }
            })
            .catch(function () {
              pick.disabled = false;
              WB.toast("当前运行方式不支持系统对话框，请手动填写路径", "warning");
            });
        }
      });
      control = ui.h("div", { class: "field__row" }, [pathInput, pick]);
      return row(field, control);
    }

    var text = ui.h("input", {
      class: "input",
      value: value === null || value === undefined ? "" : String(value),
      placeholder: field.placeholder || "",
      oninput: function () {
        onChange(text.value);
      }
    });
    if (field.type === "headers") {
      // 界面上是一行一个「名字: 值」，配置里是对象。两边都要能转。
      text = ui.h("textarea", {
        class: "input",
        rows: 3,
        placeholder: field.placeholder || "",
        value: headersToText(value),
        oninput: function () {
          onChange(textToHeaders(text.value));
        }
      });
      return row(field, text);
    }
    if (field.type === "textarea") {
      text = ui.h("textarea", {
        class: "input",
        rows: 4,
        value: value || "",
        oninput: function () {
          onChange(text.value);
        }
      });
    }
    return row(field, text);
  }

  function headersToText(value) {
    if (!value || typeof value !== "object") return "";
    return Object.keys(value)
      .map(function (k) { return k + ": " + value[k]; })
      .join("\n");
  }

  function textToHeaders(text) {
    // 换行符用 charCode 拼，避免在源码里出现转义序列（本仓库多次被两层转义吃掉）。
    var NL = String.fromCharCode(13) + String.fromCharCode(10);
    var out = {};
    String(text || "")
      .split(new RegExp("[" + NL + "]+"))
      .forEach(function (line) {
        var trimmed = line.trim();
        if (!trimmed || trimmed.charAt(0) === "#") return;
        var at = trimmed.indexOf(":");
        if (at <= 0) return;
        var name = trimmed.slice(0, at).trim();
        var val = trimmed.slice(at + 1).trim();
        if (name) out[name] = val;
      });
    return out;
  }

  function row(field, control) {
    var warn = null;
    var src = null;
    if (field.path === "llm.role") {
      // 配置里那个值不能直接用时才显示（文案由后端给，前端不判断）。留在这里是因为
      // 用户必须在这张表单上看得见问题——不然他保存完才发现总结跑不起来。
      warn = ui.h("span", {
        class: "warn-line",
        text: roleIssueText,
        style: "display:" + (roleIssueText ? "" : "none")
      });
      roleWarnNode = warn;
      // 每个角色当前吃的是哪份提示词。文件名/字节数/sha 全部来自后端 summary.roles，
      // 前端不写映射表——映射的唯一出处是 core/prompt_source.py 的 MAP。
      src = ui.h("span", {
        class: "field__help",
        text: roleSourceText,
        style: "white-space:pre-line;display:" + (roleSourceText ? "" : "none")
      });
      roleSrcNode = src;
    }
    return ui.h("div", { class: "field" }, [
      ui.h("label", { class: "field__label", text: field.label }),
      control,
      field.help ? ui.h("span", { class: "field__help", text: field.help }) : null,
      src,
      warn
    ]);
  }

  WB.pages = WB.pages || {};
  WB.pages.settings = { render: render, sections: SECTIONS };

  WB.router.define("settings", {
    title: "设置",
    crumb: "模型 / 存储 / 采集 / 切片 / 发布",
    icon: "gear",
    nav: true,
    render: render
  });
})();
