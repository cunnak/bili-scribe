/* ============================================================================
 * mock.js —— 浏览器里的假后端
 *
 * 只有在 `?mock=1` / `#mock=1`，或者根本没有 `pywebview` 时才生效。
 * 存在的理由有三个，都不是"好看"：
 *   1. 方案书 8.x 的视觉验收要在浏览器里跑完"两套皮肤 × 全部页面"，
 *      没后端就跑不了，而每次都要开桌面窗口太慢。
 *   2. jsdom 测试不能起 Python，界面逻辑的回归只能对着假数据做。
 *   3. 结构必须和真后端**一致**：字段名照抄 `Service.dispatch` 的返回，
 *      不一致就等于给界面喂了一种真后端不会产生的形状，测了也白测。
 * ========================================================================= */

(function () {
  "use strict";

  var WB = (window.WB = window.WB || {});

  // 这一份直接抄 `WorkbenchConfig.settings_view()` 的真实输出（字段名、层级、
  // 默认值都照抄），只在 api_key 上填一个假值，方便看清"已填写"的样式。
  var SETTINGS = {
    app: { language: "zh-CN", media_server: "auto" },
    ui: { skin: "ark", rail_collapsed: false },
    server: { host: "127.0.0.1", port: 8818 },
    llm: {
      base_url: "https://api.deepseek.com/v1",
      api_key: "sk-mock-0000000000000000",
      model: "deepseek-chat",
      temperature: 0.3,
      max_tokens: 4096,
      max_input_chars: 24000,
      chunk_size: 6000,
      chunk_overlap: 400,
      thinking: "auto",
      timeout_seconds: 300,
      proxy: "",
      role: "wengao",
      // 附加提示词开关（与后端 config.LlmConfig.extra_prompt 同形状，默认关）。
      extra_prompt: false,
      user_agent: "",
      extra_headers: {},
      // 服务商列表：跟后端 config.LlmProviderConfig 同形状。mock 里给两家，
      // 是为了让"多服务商"的界面路径（切换当前、任务映射下拉分组）真的有东西可渲染，
      // 而不是只画一张卡糊弄过去。
      providers: [
        {
          id: "deepseek",
          name: "DeepSeek 官方",
          protocol: "openai",
          base_url: "https://api.deepseek.com/v1",
          api_key: "sk-mock-0000000000000000",
          models: ["deepseek-chat", "deepseek-reasoner"]
        },
        {
          id: "moonshot",
          name: "Kimi",
          protocol: "openai",
          base_url: "https://api.moonshot.cn/v1",
          api_key: "",
          models: ["moonshot-v1-8k"]
        }
      ],
      active_provider: "deepseek",
      tasks: {
        summary: "",
        collection_overview: "",
        translate: "",
        clip_analyze: "",
        copy: "",
        refine: "",
        vision: ""
      }
    },
    storage: {
      db_path: "C:\\Users\\demo\\.bili-scribe\\bili-scribe.db",
      key_path: "C:\\Users\\demo\\.bili-scribe\\key.bin",
      media_root: "%APPDATA%/VideoWorkbench/media",
      cache_root: "%APPDATA%/VideoWorkbench/cache",
      models_root: "%APPDATA%/VideoWorkbench/models",
      ffmpeg_dir: "%APPDATA%/VideoWorkbench/bin",
      retention: { source_days: 30, audio_days: 3, frames_days: 7, subtitle_days: 0, export_days: 0 }
    },
    collect: {
      prefer_subtitle_lang: ["zh-CN", "zh-Hans", "zh-Hant", "ai-zh"],
      include_platform_summary: true,
      danmaku: { enabled: true, also_with_subtitle: false, max_items: 800, hot_items: 200, sample_items: 200 },
      frames: { enabled: true, max_frames: 12, quality: 85 }
    },
    download: {
      quality: "1080p",
      codec: "h264",
      concurrency: 2,
      cookies_from: "bili_scribe",
      dir_template: "{up}/{collection}/{title}/{page_title}"
    },
    transcribe: {
      engine: "faster-whisper",
      model: "large-v3-turbo",
      device: "auto",
      compute_type: "auto",
      language: "auto",
      vad: true,
      word_timestamps: true
    },
    subtitle: { default_style: "default", optimize: true, translate: { target: "auto", bilingual: true, glossary: [] } },
    clip: {
      highlight_min_seconds: 15,
      highlight_max_seconds: 90,
      segment_min_seconds: 120,
      segment_max_seconds: 480,
      candidate_count: 12,
      snap_tolerance_ms: 2000,
      vertical_mode: "center"
    },
    publish: {
      require_confirm: true,
      min_interval_seconds: 60,
      max_comment_chars: 900,
      disclose_ai: true,
      disclosure_text: "本条总结由 AI 依据视频字幕生成。",
      max_summary_chars: 400,
      auto_comment: true
    }
  };

  var INFO = {
    name: "VideoWorkbench",
    version: "0.1.0",
    config_path: "C:\\Users\\demo\\.bili-scribe\\config.yaml",
    db_path: "C:\\Users\\demo\\.bili-scribe\\bili-scribe.db",
    media_root: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media",
    skin: "ark",
    rail_collapsed: false,
    log_dir: "C:\Users\demo\AppData\Roaming\VideoWorkbench\logs",
    log_file: "C:\Users\demo\AppData\Roaming\VideoWorkbench\logs\wb-20261030.log",
    uptime: 12.4,
    // 演示数据按"装好了"给：缺依赖是状态提示，不该混进样张里干扰判断。
    deps: { ffmpeg: true, yt_dlp: true, faster_whisper: true },
    // 截图默认不弹向导：向导另有 #/onboard 路由可单独拍
    onboarded: true
  };

  // 项目列表的形状 = `Service._decorate()` 的输出，逐字段照抄：
  // stage 是流程阶段（new/downloaded/subtitle/transcribed/summary/clipped…），
  // missing 是"磁盘上已经不在了"的绝对路径数组，has_thumb/thumb 指向 frames/thumb.jpg。
  // 这三个都是磁盘现状，不落库，所以假数据也必须按真形状摆，否则界面测了也白测。
  // 向导体检的演示数据：三态各来一条，两套皮肤都能看清样式差异。
  // 路径里的反斜杠用 chr(92) 拼进来的原因：这里直接写 "C:fmpeg" 会被
  // 多层转义吃掉（P3-06 踩过），写成单反斜杠又会被 JS 当转义符吞。
  function MOCK_ONBOARD() {
    var bs = String.fromCharCode(92);
    var steps = [
      { id: "webview2", title: "窗口运行库（WebView2）", status: "ok",
        detail: "已装 WebView2 120.0.2210.144", hint: "", action: null },
      { id: "ffmpeg", title: "FFmpeg", status: "ok",
        detail: "C:" + bs + "ffmpeg" + bs + "bin" + bs + "ffmpeg.exe", hint: "", action: null },
      { id: "model", title: "语音模型", status: "todo",
        detail: "large-v3-turbo 还没下（约 1.6 GB）",
        hint: "下载在后台跑，可以先去别的页面。下完不用重启。",
        action: { kind: "rpc", method: "transcribe.model.download", params: { name: "large-v3-turbo" }, label: "开始下载" } },
      { id: "bili", title: "B 站登录", status: "missing",
        detail: "没有可用的登录态",
        hint: "复用 bili-scribe 的登录态；没登录也能把本地视频拖进素材库。",
        action: { kind: "note", label: "知道了" } },
      { id: "llm", title: "大模型", status: "ok",
        detail: "deepseek-chat · key 已配置", hint: "", action: null }
    ];
    var okCount = steps.filter(function (s) { return s.status === "ok"; }).length;
    return {
      steps: steps, ok_count: okCount, total: steps.length,
      ready: false, blocking: ["model"], missing: ["bili"],
      first_run: true, platform: "win32 / python 3.14.0（演示数据）",
      done: false, command_line: "VideoWorkbench.exe"
    };
  }

  var PROJECTS = [
    {
      id: "p_demo_1",
      title: "【硬核】三十分钟讲透注意力机制",
      up_name: "示例UP",
      bvid: "BV1xx411c7mD",
      source_url: "https://www.bilibili.com/video/BV1xx411c7mD",
      stage: "transcribed",
      root_dir: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_1",
      duration_sec: 1832.4,
      bytes: 486113280,
      asset_count: 4,
      kinds: ["source", "subtitle", "summary"],
      has_thumb: false,
      has_transcript: true,
      thumb: "",
      missing: [],
      collection_title: "深度学习导论 · 2026",
      created_at: "2026-10-28 21:04:11",
      updated_at: "2026-10-28 22:10:02"
    },
    {
      id: "p_demo_2",
      title: "线性代数期末速通（第一讲）",
      up_name: "示例UP",
      bvid: "BV1yy411c7mE",
      source_url: "https://www.bilibili.com/video/BV1yy411c7mE",
      stage: "transcribed",
      root_dir: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_2",
      duration_sec: 2711.0,
      bytes: 712003584,
      asset_count: 3,
      kinds: ["source", "thumb", "subtitle"],
      has_thumb: false,
      has_transcript: true,
      thumb: "",
      missing: [],
      collection_title: "深度学习导论 · 2026",
      created_at: "2026-10-28 20:31:55",
      updated_at: "2026-10-28 20:59:40"
    },
    {
      id: "p_demo_3",
      title: "本地素材：讲座录像 2026-10-27.mp4",
      up_name: "本地文件",
      bvid: "",
      source_url: "",
      stage: "new",
      root_dir: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_3",
      duration_sec: 5402.2,
      bytes: 1288490188,
      asset_count: 1,
      kinds: ["source"],
      has_thumb: false,
      has_transcript: false,
      thumb: "",
      missing: ["C:\\Users\\demo\\Videos\\讲座录像 2026-10-27.mp4"],
      collection_title: "",
      created_at: "2026-10-27 19:02:00",
      updated_at: "2026-10-27 19:06:12"
    },
    {
      id: "p_demo_4",
      title: "【第三讲】残差连接为什么好用",
      up_name: "示例UP",
      bvid: "BV1zz411c7mF",
      source_url: "https://www.bilibili.com/video/BV1zz411c7mF",
      stage: "summarized",
      root_dir: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_4",
      duration_sec: 2410.5,
      bytes: 604110848,
      asset_count: 4,
      kinds: ["source", "subtitle", "summary"],
      has_thumb: false,
      has_transcript: true,
      thumb: "",
      missing: [],
      collection_title: "深度学习导论 · 2026",
      created_at: "2026-10-28 19:10:22",
      updated_at: "2026-10-28 20:02:31"
    }
  ];

  var ASSETS = {
    p_demo_1: [
      { id: "a1", kind: "source", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_1\\source.mp4", bytes: 486113280, duration_ms: 1832400, exists: true },
      { id: "a2", kind: "subtitle", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_1\\subtitle.srt", bytes: 48211, exists: true },
      { id: "a3", kind: "transcript", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_1\\transcript.json", bytes: 91304, exists: true },
      { id: "a4", kind: "summary", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_1\\summary.md", bytes: 5120, exists: true }
    ],
    p_demo_2: [
      { id: "b1", kind: "source", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_2\\source.mp4", bytes: 712003584, duration_ms: 2711000, exists: true },
      { id: "b2", kind: "thumb", path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\p_demo_2\\frames\\thumb.jpg", bytes: 40211, exists: true }
    ],
    p_demo_3: [
      { id: "c1", kind: "source", path: "C:\\Users\\demo\\Videos\\讲座录像 2026-10-27.mp4", bytes: 1288490188, duration_ms: 5402200, exists: false }
    ]
  };

  var RESOLVE = {
    is_batch: true,
    kind: "collection",
    title: "深度学习导论 · 2026 秋季",
    up_name: "示例UP",
    cover: "",
    collection_title: "深度学习导论 · 2026 秋季",
    note: "合集共 12 集，时长为估算值",
    items: [
      { bvid: "BV1aa411c7m1", cid: 1011, page: 1, title: "01 从线性回归说起", part_title: "01 从线性回归说起", duration_ms: 1_842_000 },
      { bvid: "BV1aa411c7m2", cid: 1012, page: 2, title: "02 反向传播", part_title: "02 反向传播", duration_ms: 2_016_000 },
      { bvid: "BV1aa411c7m3", cid: 1013, page: 3, title: "03 注意力机制", part_title: "03 注意力机制", duration_ms: 2_540_000 }
    ]
  };

  var JOBS = [
    {
      id: "j_demo_1",
      kind: "download",
      type: "download.item",
      status: "running",
      progress: 0.37,
      stage: "下载视频流 1080P",
      project_id: "p_demo_2",
      created_at: "2026-10-28 20:59:00",
      updated_at: "2026-10-28 21:00:12",
      attempts: 1,
      error: null
    },
    {
      id: "j_demo_2",
      kind: "transcribe",
      type: "transcribe",
      status: "queued",
      progress: 0,
      stage: "排队中",
      project_id: "p_demo_2",
      created_at: "2026-10-28 20:59:01",
      updated_at: "2026-10-28 20:59:01",
      attempts: 0,
      error: null
    },
    // P1-08：一个"有失败项"的批次。新建任务页的「用 BBDown 重试」靠它出现。
    {
      id: "j_demo_batch_1",
      kind: "download",
      type: "download.batch",
      status: "failed",
      progress: 1,
      stage: "下载中 2/3",
      project_id: null,
      created_at: "2026-10-28 20:40:00",
      updated_at: "2026-10-28 20:52:30",
      attempts: 1,
      error: "有 1 集失败",
      result: {
        total: 3,
        ok: 2,
        failed: 1,
        items: [
          { bvid: "BV1xx411c7mD", title: "第 1 集：开场", page: 1, status: "succeeded" },
          { bvid: "BV1xx411c7mD", title: "第 2 集：主体", page: 2, status: "succeeded" },
          { bvid: "BV1xx411c7mD", title: "第 3 集：大会员专享", page: 3, status: "failed" }
        ]
      }
    }
  ];

  // 下载后端探测：mock 里装成"yt-dlp 有、BBDown 有"，这样降级路径也有东西可点。
  var MOCK_BACKENDS = {
    default: "ytdlp",
    ytdlp: { found: true, path: "C:\\mock\\yt-dlp.exe", name: "yt-dlp.exe", module: false, configured: "", hint: "" },
    bbdown: { found: true, path: "C:\\mock\\tools\\BBDown.exe", name: "BBDown.exe", configured: "", hint: "" }
  };

  // 优化任务：mock 里永远"已完成"，省掉前端轮询的等待
  var OPT_JOB = { id: "j_mock_opt", type: "subtitle.optimize", status: "succeeded", progress: 1,
                  stage: "已完成", result: { changes: [], failed: [], model: "deepseek-chat" } };

  // 导出任务：mock 里永远"已完成"，产物是一份编出来的视频路径
  var EXPO_JOB = { id: "j_mock_export", type: "video.export", status: "succeeded", progress: 1,
                   stage: "已完成", result: { output: "", bytes: 0, mode: "none" } };

  // 翻译任务与草稿：形状照真后端 wb_subtitle(kind="translation") 的行。
  var TRANS_JOB = { id: "j_mock_trans", type: "subtitle.translate", status: "succeeded", progress: 1,
                    stage: "已完成", result: { failed: [] } };  var TRANS = {};   // project_id -> { target, status, updated_at, segments }
  // 假词典只为让"双语版式/术语命中"在浏览器里肉眼可验，不参与真实翻译逻辑。
  var PHRASES = {
    "We're no strangers to love": "我们对爱都不是陌生人",
    "You know the rules and so do I": "你懂规矩，我也懂",
    "A full commitment's what I'm thinking of": "我想要的是一份全然的承诺",
    "This is a deliberately overlong subtitle line that blows past the two-line character budget": "这是一行刻意超长的字幕，已经超过两行的字数预算",
    "Yeah": "是啊"
  };
  var GLOSSARY = ["AI 芯片=accelerator", "transformer=Transformer 模型"];

  // 总结（P2-07）。字段照后端 wb_summary 的行 + core/summarizer.to_markdown 的分节，
  // 假数据跟真实现长得不一样，截图就会替后端撒谎。
  var SUM_JOB = { id: "j_mock_summary", type: "summary.run", status: "succeeded", progress: 1,
                  stage: "已完成", result: { version: 1 } };
  // 假总结的正文要跟着**项目标题**走。写死一段 MV 文案的话，截图里左边项目叫
  // 「讲透注意力机制」、右边却在讲 Rick Astley——审图的人第一眼就会怀疑数据是假的，
  // 这种"看起来在撒谎"的截图比没截图更糟。
  var SUMMARY_TEXT_FALLBACK = "这条视频";

  function summaryContentFor(projectId) {
    var project = null;
    for (var i = 0; i < PROJECTS.length; i++) {
      if (PROJECTS[i].id === projectId) project = PROJECTS[i];
    }
    var title = project && project.title ? project.title : SUMMARY_TEXT_FALLBACK;
    var md = [
      "## 这段在讲什么",
      "",
      "围绕「" + title + "」这条主线展开，**先给结论再补细节**，中间穿插了两处现场演示。",
      "主讲人反复强调的一点：结论本身不新鲜，值钱的是把结论推到能动手的那几步。",
      "",
      "## 怎么论证的",
      "",
      "- 先摆一个反例，说明直觉为什么不可靠",
      "- 再把变量拆开，逐个控制住做对比",
      "- 最后给出可复现的操作顺序",
      "",
      "> 注意：素材来自 AI 转录字幕，专有名词与数字建议回视频核对。"
    ].join("\n");
    return {
      one_line: title + "：把抽象结论拆成能照做的步骤",
      summary_md: md,
      key_points: [
        "先证伪直觉，再给方法",
        "把变量拆开逐个对比",
        "最后落到可复现的操作顺序"
      ],
      timeline: [
        { t: "00:12", text: "抛出反例，说明直觉不可靠" },
        { t: "01:05", text: "拆变量做第一组对比" },
        { t: "03:33", text: "给出操作顺序与两个坑" }
      ],
      caution: "素材来自 AI 转录字幕，没有人声以外的画面描述；个别数字属于口播估算。",
      comment_short: "一句话：这条视频的价值不在结论，而在它把结论推到能动手的步骤——先证伪直觉，再把变量拆开逐个对比，最后给出一份能照做的顺序。最容易踩的两个坑也在片尾点到了。",
      chunks: 1,
      model: "deepseek-chat",
      role: "wengao",
      frames: 0,
      revise_note: ""
    };
  }

  // 切片候选（P3-03）。形状照 `Service.clip_candidates`：
  // notes 里的 origin/boundary/raw_start_ms/edited_ms 界面都要读，
  // boundaries 是磁吸线，与 P3-02 吸附用的是同一份坐标。
  var CLIP_JOB = { id: "j_mock_clip", type: "clip.analyze", status: "succeeded",
                   progress: 1, stage: "已完成", result: {} };
  // 文案任务（P3-08）：mock 里第一次轮询就置成功，并把文案写到那条候选上
  var COPY_JOB = { id: "j_mock_copy", type: "clip.copy", status: "succeeded",
                   progress: 1, stage: "已完成", result: {} };
  var MOCK_PENDING_COPY = "";
  var COPY_SEED = {
    title: "他为什么坚持不做低价",
    description: "同一款产品他把价格翻了三倍，这段讲清定价不是成本加法，而是你敢不敢下判断。",
    tags: ["定价策略", "商业判断", "产品思维"],
    source: "llm",
    edited: false,
    cover: { path: "", at_ms: 13_400, bytes: 184_320, source: "frame" }
  };
  // 跟随关键帧：p_demo_1 有一条检测轨迹 + 一个手工点，界面能看见也能改
  var FOCUS = {
    p_demo_1: [
      { t_ms: 0, x: 0.42, manual: false },
      { t_ms: 4000, x: 0.55, manual: false },
      { t_ms: 8000, x: 0.48, manual: true }
    ]
  };
  var FOCUS_DETECTOR = { p_demo_1: "haar" };
  // P6-02：多主体检测结果（截图与前端测试要能看到选择器与切点提示）
  var FOCUS_SUBJECTS = {
    p_demo_1: [
      {
        id: "sub_a",
        label: "左边那位",
        score: 78,
        track: [
          { t_ms: 0, x: 0.42 },
          { t_ms: 4000, x: 0.55 },
          { t_ms: 8000, x: 0.48 }
        ]
      },
      {
        id: "sub_b",
        label: "右边那位",
        score: 41,
        track: [
          { t_ms: 0, x: 0.74 },
          { t_ms: 4000, x: 0.7 },
          { t_ms: 8000, x: 0.66 }
        ]
      }
    ]
  };
  var FOCUS_SUBJECT = { p_demo_1: "sub_a" };
  var FOCUS_CUTS = { p_demo_1: [6000] };
  var FOCUS_ENHANCE = { p_demo_1: true };
  // 人脸检测任务（P6-02）：mock 里立刻成功，好让选择器出现在截图里
  // P6-04 三通道交付：mock 里也装成"本机装了剪映、没装达芬奇"，界面才知道要不要
  // 显示"放进草稿箱"那个勾。路径用 String.fromCharCode(92) 拼，避免反斜杠被吃掉。
  var NLE_DELIVER_JOB = { id: "j_mock_deliver", type: "clip.deliver", status: "queued", progress: 0.2 };
  var MOCK_DELIVER = { channel: "jianying", place_in_app: false };

  function MOCK_NLE_INFO() {
    var bs = String.fromCharCode(92);
    return {
      apps: {
        jianying: { found: true, root: "C:" + bs + "Users" + bs + "demo" + bs + "JianyingPro" },
        resolve: { found: false, root: "" }
      },
      channels: [
        { key: "jianying", label: "剪映草稿", ready: true,
          note: "已检测到剪映草稿箱；勾上「放进草稿箱」就直接出现在剪映里" },
        { key: "otio", label: "达芬奇 OTIO", ready: true,
          note: "没检测到达芬奇也能导，自己拿 .otio 进去" },
        { key: "bundle", label: "降级包", ready: true,
          note: "素材 + 字幕 + 导入说明，什么剪辑软件都能用" }
      ]
    };
  }

  function MOCK_DELIVER_RESULT(channel, placeInApp) {
    var bs = String.fromCharCode(92);
    var base = "C:" + bs + "Users" + bs + "demo" + bs + "media" + bs + "UP" + bs + "标题" + bs + "export";
    if (channel === "otio") {
      return {
        channel: "otio", label: "达芬奇 OTIO", path: base + bs + "nle" + bs + "先证伪直觉.otio",
        files: [base + bs + "nle" + bs + "先证伪直觉.otio"], bytes: 2048,
        placed_in_app: false, subtitle_style_followed: false,
        notes: ["字幕样式不跟随：OTIO 里字幕按达芬奇默认样式走，要定制请回本应用导出硬字幕"]
      };
    }
    if (channel === "bundle") {
      return {
        channel: "bundle", label: "降级包", path: base + bs + "bundle" + bs + "先证伪直觉",
        files: ["片段.mp4", "字幕.srt", "导入说明.txt", "文案.txt"], bytes: 8192,
        placed_in_app: false, source_is_export: false, subtitle_style_followed: false,
        notes: ["包里放的是整段源视频（这段还没导出成片）", "字幕样式不跟随：包里的 srt 不带样式"]
      };
    }
    return {
      channel: "jianying", label: "剪映草稿",
      path: placeInApp
        ? "C:" + bs + "Users" + bs + "demo" + bs + "JianyingPro" + bs + "先证伪直觉"
        : base + bs + "jianying" + bs + "先证伪直觉",
      files: ["draft_content.json", "draft_meta_info.json"], bytes: 5120,
      placed_in_app: Boolean(placeInApp),
      jianying_root: "C:" + bs + "Users" + bs + "demo" + bs + "JianyingPro",
      subtitle_style_followed: false,
      notes: [
        placeInApp ? "已放进剪映草稿箱（UP 标题）" : "草稿放在本项目的 export/jianying 下",
        "字幕样式不跟随：草稿里字幕是白字 5 号，要定制请回本应用导出硬字幕"
      ]
    };
  }

  var FOCUS_JOB = { id: "j_mock_focus", type: "clip.focus", status: "succeeded", progress: 1,
                    stage: "已完成", result: { subjects: [], subject: "", cuts: 1, enhance: true } };

  var MOCKEXPDIR = "C:\\mock\\export\\";

  var CANDIDATES = {};
  // 导出队列（P3-05）：job.get 要能查到，所以单独存一份任务表
  function mergeStamp() {
    return new Date().toISOString().slice(0, 16).replace("T", " ");
  }

  var MERGE_SEQ = 0;
  var MERGE_JOB = null;
  var MERGES = [];

  var EXPORTS = [];
  var EXPORT_JOBS = {};

  function clipRows(projectId) {
    if (!CANDIDATES[projectId]) CANDIDATES[projectId] = [];
    return CANDIDATES[projectId];
  }

  function clipSeed(projectId) {
    if (CANDIDATES[projectId]) return CANDIDATES[projectId];
    if (projectId !== "p_demo_1") {
      CANDIDATES[projectId] = [];
      return CANDIDATES[projectId];
    }
    // 60 秒一类的短片去重后剩两三条是正常的：三条重叠加一条长段，
    // 界面要能看出"分数、理由、兜底标记、锁定状态"四种表达。
    CANDIDATES[projectId] = [
      { id: "cand_000000000001", kind: "highlight", start_ms: 12_400, end_ms: 34_800,
        score: 86, title: "先证伪直觉，再给方法",
        reason: "这一段的论证顺序完整：反例 → 拆变量 → 给顺序，剪出来能独立成立。",
        quote: "你得先承认直觉是错的，否则后面每一步都在给直觉打补丁。",
        tags: ["论证"], status: "draft", source: "ai",
        notes: { origin: "llm", boundary: "sentence", raw_start_ms: 12_400,
                 raw_end_ms: 34_800, snap_notes: [], silence_trimmed_ms: 0 } },
      { id: "cand_000000000002", kind: "highlight", start_ms: 41_200, end_ms: 58_900,
        score: 78, title: "把结论推到能动手的步骤",
        reason: "片尾那五步是全片唯一可复现的部分，适合当独立短视频。",
        quote: "所以别问我结论，问顺序。",
        tags: ["方法"], status: "locked", source: "ai",
        notes: { origin: "llm", boundary: "sentence", raw_start_ms: 41_000,
                 raw_end_ms: 58_900, snap_notes: ["起点贴到句边界"], silence_trimmed_ms: 200,
                 edited_ms: true },
        copy: { title: "别问我结论，问顺序", description: "片尾那五步是全片唯一能照着做的部分。这段把顺序讲清了，剪出来能单独用。",
                tags: ["方法", "复现步骤", "表达"], source: "llm", edited: true,
                cover: { path: "", at_ms: 42_200, bytes: 176_400, source: "frame" } } },
      { id: "cand_000000000003", kind: "highlight", start_ms: 96_500, end_ms: 121_000,
        score: 62, title: "注意力其实就是加权求和",
        reason: "概念定义段，单独看略干，但配图讲得最清楚。",
        quote: "注意力没那么玄，它就是一次加权求和。",
        tags: ["概念"], status: "draft", source: "ai",
        notes: { origin: "rule", boundary: "", raw_start_ms: 96_100,
                 raw_end_ms: 121_000, snap_notes: ["起点不在句边界上，已保留原值"],
                 silence_trimmed_ms: 0 } },
      { id: "cand_000000000004", kind: "segment", start_ms: 148_000, end_ms: 271_000,
        score: 71, title: "从直觉到三条可复现步骤的完整推导",
        reason: "长段适合放到课程合集里，独立发会显得拖。",
        quote: "",
        tags: ["完整推导"], status: "draft", source: "ai",
        notes: { origin: "llm", boundary: "sentence", raw_start_ms: 148_000,
                 raw_end_ms: 271_000, snap_notes: [], silence_trimmed_ms: 0 } }
    ];
    return CANDIDATES[projectId];
  }

  function clipBoundaries(projectId) {
    var rows = clipRows(projectId);
    var set = {};
    rows.forEach(function (row) {
      set[row.start_ms] = true;
      set[row.end_ms] = true;
    });
    return Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
  }

  function clipFind(id) {
    var ids = Object.keys(CANDIDATES);
    for (var i = 0; i < ids.length; i++) {
      var rows = CANDIDATES[ids[i]];
      for (var j = 0; j < rows.length; j++) {
        if (rows[j].id === id) return { row: rows[j], rows: rows };
      }
    }
    return null;
  }

  function clipSource(projectId) {
    var list = ASSETS[projectId] || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].kind === "source") return list[i].path;
    }
    return "";
  }

  // 合集总览（P2-08）。字段照 core/overview.to_markdown 的分节：
  // theme / conclusions / watch_order / items / overlap，少一个，截图就和后端不一样。
  var OV_JOB = { id: "j_mock_overview", type: "summary.collection", status: "succeeded",
                 progress: 1, stage: "已完成", result: { version: 2 } };
  var COLS = {};        // collection title -> { overview, versions }

  function collectionChildren(title) {
    return PROJECTS.filter(function (item) {
      return item.collection_title === title;
    }).map(function (item) {
      return {
        id: item.id, title: item.title, stage: item.stage, bvid: item.bvid,
        has_summary: !!item.has_transcript,
        one_line: summaryContentFor(item.id).one_line
      };
    });
  }

  function collectionTitles() {
    var seen = {};
    var out = [];
    PROJECTS.forEach(function (item) {
      var title = item.collection_title || "";
      if (!title || seen[title]) return;
      seen[title] = true;
      out.push(title);
    });
    return out;
  }

  function overviewContentFor(title) {
    var kids = collectionChildren(title);
    var names = kids.map(function (item) { return item.title; });
    return {
      theme: "「" + title + "」三讲连起来看，是一条「先把直觉证伪、再给可复现步骤」的主线；单集看会漏掉第三讲对前两讲的反驳。",
      conclusions: [
        "三讲共用同一套论证骨架：反例 → 拆变量 → 给顺序，差别只在拆的粒度。",
        "第二讲的结论被第三讲部分推翻：残差不是「更好」，而是「让深层网络还能训得动」。",
        "真正能落地的只有第三讲末尾那五步；前两讲是理解它为什么成立的前提。",
        "重复最多的提醒是「别信直觉」，三讲里出现了四次，每次配一个反例。"
      ],
      watch_order: [
        "先看第一讲建立词汇（注意力到底是什么）",
        "再看第三讲，拿到能照做的步骤与两个坑",
        "最后回看第二讲，补上第一讲到第三讲之间的推导"
      ],
      items: kids.map(function (item, index) {
        return {
          title: item.title,
          // 合集里的"一句话"是这一集在合集里的定位，不是单集总结的复读
          one_line: ["建立词汇与直觉", "把直觉换成可计算的写法", "指出前两讲的边界"][index] || "同一主线的另一集",
          url: item.bvid ? "https://www.bilibili.com/video/" + item.bvid : "",
          bvid: item.bvid,
          minutes: [31, 41, 40][index] || 30
        };
      }),
      overlap: [
        "第一讲与第二讲都用「加权求和」举例，第二讲讲得更细，可只看第二讲。",
        "第二讲说「层数越多越好」，第三讲明确否掉，以第三讲为准。",
        "缺口：三讲都没给数据集与训练超参，复现要自己补。"
      ],
      notes: []
    };
  }

  function seedCollection(title) {
    if (COLS[title]) return COLS[title];
    var content = overviewContentFor(title);
    var first = {
      id: "sum_col_1", version: 1, role: "wengao", model: "deepseek-chat",
      chars: 980, created_at: "2026-10-29 21:12:04", status: "ready",
      one_line: content.theme, revise_note: "", theme: content.theme, content: content
    };
    var second = {
      // task-31 之前这一行是 role: "xueshu"。学术分析已删、视频总结合并进说人话，
      // 演示数据里不放退役 key：mock 不承载 retired 那个判定结果（那来自真后端
      // summary.roles.retired），所以留一个退役 key 会让这一行显示成「未知角色」——
      // 那句是假话。合并的继任者就是 renhua，用它。
      id: "sum_col_2", version: 2, role: "renhua", model: "deepseek-chat",
      chars: 1120, created_at: "2026-10-30 09:41:22", status: "ready",
      one_line: content.theme, revise_note: "", theme: content.theme, content: content
    };
    COLS[title] = { overview: second, versions: [second, first] };
    return COLS[title];
  }

  var SUMMARIES = {};   // project_id -> { summary, versions }
  var DRAFTS = [];      // 发布草稿（P2-09）：一个项目最多一份，真后端也一样
  var DRAFT_SEQ = 0;
  // 两条历史记录：截图与手工验收要看到"用过一阵子"的样子，
  // 否则记录卡永远是空态，看不出它到底会不会显示状态与时间。
  function seedRecord(projectId, title, status, noteId, publishedAt, withdrawnAt, error) {
    var proj = projectById(projectId) || {};
    DRAFTS.push({
      id: "draft_seed_" + DRAFTS.length,
      project_id: projectId,
      project_title: proj.title || "",
      up_name: proj.up_name || "",
      bvid: proj.bvid || "",
      aid: proj.aid || 0,
      title: title,
      summary: "一句话摘要。",
      // 真库里 content 是 Delta 的 JSON 字符串，不是数组：mock 存成数组会让
      // mockPlain 解析失败 → 门禁判「正文是空的」，界面上就变成一句谎话。
      content: '[{"insert":"这一段是已经发出去的正文。"}]',
      status: status,
      note_id: noteId || "",
      error: error || "",
      // ISO 时间：门禁要拿去和「现在」相减，本地化格式算不出来
      published_at: publishedAt || "",
      withdrawn_at: withdrawnAt || "",
      created_at: "2026-10-28T21:11:00+08:00",
      updated_at: withdrawnAt || publishedAt || "2026-10-28T21:11:00+08:00"
    });
  }
  seedRecord("p_demo_2", "【拆解】把结论推到能动手的步骤", "published", "note_mock_1",
    "2026-10-29T09:24:00+08:00", "", "");
  seedRecord("p_demo_3", "注意力到底在算什么", "withdrawn", "note_mock_2",
    "2026-10-27T19:02:00+08:00", "2026-10-28T10:40:00+08:00", "");

  function projectById(id) {
    for (var i = 0; i < PROJECTS.length; i += 1) {
      if (PROJECTS[i].id === id) return PROJECTS[i];
    }
    return null;
  }

  function mockPlain(delta) {
    // Delta JSON → 纯文本。正面只为了字数和预览，不需要还原加粗。
    try {
      var ops = JSON.parse(delta || "[]");
      return ops.map(function (op) { return (op && op.insert) || ""; }).join("");
    } catch (err) {
      return "";
    }
  }

  function mockChars(text) {
    return Array.from(String(text || "").replace(/\s+/g, "")).length;
  }

  function mockDraftView(row) {
    var text = mockPlain(row.content);
    return {
      id: row.id, project_id: row.project_id, bvid: row.bvid, aid: row.aid,
      title: row.title, summary: row.summary, content: row.content,
      status: row.status, created_at: row.created_at,
      updated_at: row.updated_at || row.created_at,
      note_id: row.note_id || "",
      published_at: row.published_at || "",
      withdrawn_at: row.withdrawn_at || "",
      error: row.error || "",
      content_text: text, chars: mockChars(text),
      title_chars: Array.from(String(row.title || "")).length,
      summary_chars: Array.from(String(row.summary || "")).length,
      project_title: (projectById(row.project_id) || {}).title || "",
      up_name: (projectById(row.project_id) || {}).up_name || ""
    };
  }

  var GATE_INTERVAL = 60;
  var NOTE_SEQ = 0;
  // 演示环境里当成「扫码登录过」：真后端读的是本地登录态（service.py 里 _gateway.is_logged_in），
  // mock 没有登录流程，写死成已登录才走得到「三关全绿」那一步。
  var MOCK_LOGGED_IN = true;

  /** 三道门禁的当前状态（与后端同语义：能本地判断的算，判不了的标 todo）。 */
  function mockGates(draft, project, note, opts) {
    var o = opts || {};
    var title = String(draft.title || "");
    var summary = String(draft.summary || "");
    var body = String(draft.content_text || "").trim();
    var items = [
      { text: "标题 " + Array.from(title).length + " / 80 字", ok: !!title && Array.from(title).length <= 80 },
      { text: "摘要 " + Array.from(summary).length + " / 400 字", ok: Array.from(summary).length <= 400 },
      { text: "正文 " + mockChars(body) + " 字", ok: !!body },
      { text: "已阅读并确认标题/摘要/正文", ok: !!o.confirmed }
    ];
    var contentStatus = !title || !body ? "blocked" : (items[0].ok && items[1].ok ? (o.confirmed ? "ok" : "todo") : "blocked");

    var bvid = (project && project.bvid) || (note && note.bvid) || "";
    var accountItems = [
      { text: "视频 BV 号 " + (bvid || "（没有）"), ok: !!bvid },
      { text: "草稿指向的是同一个视频", ok: !!bvid },
      { text: "登录状态：" + (o.loggedIn === false ? "未登录" : o.loggedIn ? "已登录" : "发布时再确认"), ok: o.loggedIn !== false }
    ];
    var accountStatus = !bvid ? "blocked" : (o.loggedIn === false ? "blocked" : (o.loggedIn ? "ok" : "todo"));

    var paceItems = [
      { text: "距上次发布：还没发过", ok: true },
      { text: "同一视频没有未撤回的笔记", ok: true }
    ];
    var paceStatus = body ? "ok" : "blocked";

    return {
      ready: contentStatus === "ok" && accountStatus === "ok" && paceStatus === "ok",
      limits: { summary: 400, interval: GATE_INTERVAL },
      gates: [
        { id: "content", label: "内容确认", status: contentStatus,
          reason: contentStatus === "blocked" ? "标题或正文是空的" : (contentStatus === "todo" ? "发布前要勾一下「我已阅读并确认」" : ""),
          items: items },
        { id: "account", label: "账号与视频", status: accountStatus,
          reason: accountStatus === "blocked" ? "这个项目没有 BV 号，发不了笔记" : (accountStatus === "todo" ? "发布时会再核一次登录态与视频可访问性" : ""),
          items: accountItems },
        { id: "pace", label: "频率与重复", status: paceStatus,
          reason: paceStatus === "ok" ? "" : "正文是空的",
          items: paceItems }
      ]
    };
  }

  function mockDelta(text) {
    // 与真后端一样：一段话 + 换行，别搞出平台不认的属性
    return JSON.stringify([{ insert: String(text || "") + "\n" }]);
  }

  function mockPreview(projectId, summaryId) {
    var project = projectById(projectId);
    if (!project) return null;
    var box = SUMMARIES[projectId] || seedSummary(projectId);
    if (!box) return null;
    var content = box.summary.content || {};
    var lines = [];
    if (content.one_line) lines.push("**" + content.one_line + "**", "");
    if (content.summary_md) lines.push(content.summary_md, "");
    if ((content.key_points || []).length) {
      lines.push("## 要点");
      content.key_points.forEach(function (point) { lines.push("- " + point); });
      lines.push("");
    }
    if ((content.timeline || []).length) {
      lines.push("## 时间轴");
      content.timeline.forEach(function (item) {
        lines.push("- `" + (item.t || "") + "` " + (item.text || ""));
      });
      lines.push("");
    }
    if (content.caution) lines.push("注意：" + content.caution);
    lines.push("（本总结由 AI 依据视频字幕生成，发布前请自己过一遍）");
    var markdown = lines.join("\n");
    var text = markdown
      .replace(/\*\*/g, "")
      .replace(/^#{1,6}\s*/gm, "")
      .replace(/`/g, "");
    return {
      project_id: projectId,
      project_title: project.title,
      title: project.title,
      summary: content.comment_short || content.one_line || "",
      content: mockDelta(text),
      content_text: text + "\n",
      markdown: markdown,
      chars: mockChars(text),
      title_chars: Array.from(project.title).length,
      summary_chars: Array.from(content.comment_short || content.one_line || "").length,
      limits: { title: 80, summary: 400 },
      warnings: [],
      summary_id: box.summary.id || "",
      version: box.summary.version || 1,
      note: null
    };
  }


  // 用量样例：两版加起来刻意等于真实两小时视频的量级
  // （21 次调用 / 输入 241,242 / 输出 34,879），跟 bili-scribe 面板同一数量级，
  // 截图时一眼能看出这卡在说什么。
  var SUMMARY_USAGE = {
    1: { calls: 9, prompt_tokens: 120000, completion_tokens: 18000,
         total_tokens: 138000, seconds: 1089, chunks: 8 },
    2: { calls: 12, prompt_tokens: 121242, completion_tokens: 16879,
         total_tokens: 138121, seconds: 1092, chunks: 8 }
  };

  function summaryRow(version, content, role, note) {
    return {
      id: "sum_mock_" + version,
      version: version,
      role: role,
      model: "deepseek-chat",
      chars: String(content.summary_md || "").replace(/\s+/g, "").length,
      created_at: "2026-10-2" + (version === 1 ? "8" : "9") + " 2" + version + ":11:07",
      status: "ready",
      one_line: content.one_line,
      revise_note: note || "",
      usage: JSON.parse(JSON.stringify(SUMMARY_USAGE[version] || { calls: 1 })),
      content: content
    };
  }

  function usageTotals(rows) {
    var out = { versions: (rows || []).length, calls: 0, prompt_tokens: 0,
                completion_tokens: 0, total_tokens: 0, seconds: 0 };
    (rows || []).forEach(function (row) {
      var u = row.usage || {};
      out.calls += u.calls || 0;
      out.prompt_tokens += u.prompt_tokens || 0;
      out.completion_tokens += u.completion_tokens || 0;
      out.total_tokens += u.total_tokens || ((u.prompt_tokens || 0) + (u.completion_tokens || 0));
      out.seconds += u.seconds || 0;
    });
    out.seconds = Math.round(out.seconds * 10) / 10;
    return out;
  }

  function seedSummary(projectId) {
    if (SUMMARIES[projectId]) return SUMMARIES[projectId];
    var content = summaryContentFor(projectId);
    var first = summaryRow(1, content, "wengao", "");
    var second = summaryRow(2, content, "wengao", "要点压到三条，去掉形容词");
    SUMMARIES[projectId] = {
      summary: second,
      versions: [second, first]
    };
    return SUMMARIES[projectId];
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function ok(data) {
    return { ok: true, data: clone(data === undefined ? null : data), request_id: "mock" };
  }

  // ---------------------------------------------------------------- 字幕

  // 一条样例字幕：三段英文 + 一段中文，够撑出"段/时间/质检"三列的样子
  var SUBTITLES = {
    p_demo_1: [
      { id: "s1", start_ms: 0, end_ms: 4640, text: "We're no strangers to love", words: [] },
      { id: "s2", start_ms: 4640, end_ms: 9180, text: "You know the rules and so do I", words: [] },
      { id: "s3", start_ms: 9180, end_ms: 12640, text: "A full commitment's what I'm thinking of", words: [] },
      { id: "s4", start_ms: 12640, end_ms: 15900, text: "你不会从任何其他人那里得到这份爱", words: [] },
      // 后两段刻意留两条质检问题：一段太长（ASR 常见的"一大坨"）、一段太短。
      // 质检列不能只有"全绿"这一种样子，否则那条规则等于没验过。
      { id: "s5", start_ms: 15900, end_ms: 18400, text: "This is a deliberately overlong subtitle line that blows past the two-line character budget", words: [] },
      { id: "s6", start_ms: 18400, end_ms: 18600, text: "Yeah", words: [] }
    ]
  };

  // 波形用一条"说话-停顿-说话"的假包络，够看出画布是活的
  var PEAKS = (function () {
    var out = [];
    for (var i = 0; i < 240; i++) {
      var block = Math.floor(i / 40);
      var height = block % 2 === 0 ? 40 + ((i * 37) % 170) : 6 + ((i * 13) % 18);
      out.push(height);
    }
    return out;
  })();

  function speech(qcItems, segments) {
    var ids = {};
    (qcItems || []).forEach(function (item) {
      (item.segment_ids || []).forEach(function (id) { ids[id] = true; });
    });
    return ids;
  }

  function subtitleStats(segments) {
    var chars = 0;
    (segments || []).forEach(function (seg) { chars += String(seg.text || "").length; });
    var span = segments && segments.length
      ? segments[segments.length - 1].end_ms - segments[0].start_ms
      : 0;
    return {
      count: (segments || []).length,
      chars: chars,
      span_ms: span,
      chars_per_minute: span ? Math.round((chars / (span / 60000)) * 10) / 10 : 0,
      first_ms: segments && segments.length ? segments[0].start_ms : 0,
      last_ms: segments && segments.length ? segments[segments.length - 1].end_ms : 0
    };
  }

  // 与后端 core/subtitles.py 的 qc() 逐条对齐：常量一致、优先级一致
  // （negative 盖住 too_short），相邻段既查重叠也查大空洞。
  // 假数据的规则如果和真实现不一样，界面上"质检 N 条"就是假证据。
  var QC = {
    MAX_CHARS_PER_LINE: 22,
    MAX_SEGMENT_MS: 12000,
    MIN_SEGMENT_MS: 400,
    OVERLAP_WARN_MS: 50,
    GAP_WARN_MS: 30000
  };

  function subtitleQc(segments) {
    var items = (segments || []).slice().sort(function (a, b) { return a.start_ms - b.start_ms; });
    var out = [];
    function add(kind, level, ids, message) {
      out.push({ kind: kind, level: level, segment_ids: ids, message: message });
    }
    items.forEach(function (seg, index) {
      var text = String(seg.text == null ? "" : seg.text);
      var span = seg.end_ms - seg.start_ms;
      if (!text.trim()) add("empty", "error", [seg.id], "字幕内容为空");
      if (seg.end_ms <= seg.start_ms) {
        add("negative", "error", [seg.id], "结束时间不大于开始时间");
      } else if (span < QC.MIN_SEGMENT_MS) {
        add("too_short", "warn", [seg.id], "时长只有 " + span + "ms，一眼看不清");
      } else if (span > QC.MAX_SEGMENT_MS) {
        add("too_long", "warn", [seg.id], "时长 " + (span / 1000).toFixed(1) + "s，建议分割");
      }
      if (text.length > QC.MAX_CHARS_PER_LINE * 2) {
        add("too_many_chars", "warn", [seg.id], text.length + " 字，一行放不下");
      }
      if (index) {
        var prev = items[index - 1];
        var gap = seg.start_ms - prev.end_ms;
        if (gap < -QC.OVERLAP_WARN_MS) {
          add("overlap", "error", [prev.id, seg.id], "与上一条重叠 " + (-gap) + "ms");
        } else if (gap > 60000) {
          add("big_gap", "info", [prev.id, seg.id], "中间空了 " + Math.round(gap / 1000) + "s，可能有漏句");
        }
      }
    });
    return out;
  }

  function subtitleGet(projectId) {
    var segments = SUBTITLES[projectId] || [];
    var project = null;
    for (var i = 0; i < PROJECTS.length; i++) {
      if (PROJECTS[i].id === projectId) project = PROJECTS[i];
    }
    return {
      project_id: projectId || "",
      title: project ? project.title : "",
      has_transcript: segments.length > 0,
      language: "en",
      model: "large-v3-turbo",
      segments: clone(segments),
      qc: subtitleQc(segments),
      stats: subtitleStats(segments)
    };
  }

  function subtitleSave(projectId, segments) {
    SUBTITLES[projectId] = segments;
    return { project_id: projectId, count: segments.length,
             qc: subtitleQc(segments), stats: subtitleStats(segments) };
  }

  function findIndex(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return i;
    return -1;
  }

  /** mock 里只实现编辑器会发的几种，语义与后端 core/subtitles.py 保持一致。 */
  function subtitleOp(p) {
    var list = clone(SUBTITLES[p.project_id] || []);
    var hits = 0;
    if (p.op === "update") {
      var at = findIndex(list, p.segment_id);
      if (at < 0) return fail("NOT_FOUND", "找不到这一段");
      list[at].text = String(p.text || "");
    } else if (p.op === "split") {
      var si = findIndex(list, p.segment_id);
      if (si < 0) return fail("NOT_FOUND", "找不到这一段");
      var seg = list[si];
      if (!(p.at_ms > seg.start_ms && p.at_ms < seg.end_ms)) return fail("VALIDATION", "切点不在段内");
      var right = { id: seg.id + "b", start_ms: p.at_ms, end_ms: seg.end_ms,
                    text: seg.text.slice(Math.ceil(seg.text.length / 2)), words: [] };
      seg.end_ms = p.at_ms;
      seg.text = seg.text.slice(0, Math.ceil(seg.text.length / 2));
      list.splice(si + 1, 0, right);
    } else if (p.op === "merge") {
      var ids = p.segment_ids || [];
      if (ids.length < 2) return fail("VALIDATION", "至少要选两段");
      var idx = ids.map(function (id) { return findIndex(list, id); }).sort(function (a, b) { return a - b; });
      if (idx[0] < 0) return fail("NOT_FOUND", "找不到这一段");
      if (idx[idx.length - 1] - idx[0] !== idx.length - 1) return fail("VALIDATION", "只能合并相邻的段");
      var block = list.slice(idx[0], idx[idx.length - 1] + 1);
      list.splice(idx[0], block.length, {
        id: block[0].id,
        start_ms: block[0].start_ms,
        end_ms: block[block.length - 1].end_ms,
        text: block.map(function (s) { return s.text; }).join(" "),
        words: []
      });
    } else if (p.op === "delete") {
      var drop = p.segment_ids || [];
      list = list.filter(function (seg) { return drop.indexOf(seg.id) < 0; });
    } else if (p.op === "shift") {
      var offset = Number(p.offset_ms) || 0;
      var only = p.segment_ids || [];
      list = list.map(function (seg) {
        if (only.length && only.indexOf(seg.id) < 0) return seg;
        return { id: seg.id, start_ms: Math.max(0, seg.start_ms + offset),
                 end_ms: Math.max(1, seg.end_ms + offset), text: seg.text, words: seg.words || [] };
      });
    } else if (p.op === "replace") {
      var only2 = p.segment_ids || [];
      var pattern = String(p.pattern || "");
      if (!pattern) return fail("VALIDATION", "查找内容不能为空");
      list = list.map(function (seg) {
        if (only2.length && only2.indexOf(seg.id) < 0) return seg;
        var next = seg.text;
        if (p.regex) {
          try {
            var re = new RegExp(pattern, p.case_sensitive === false ? "gi" : "g");
            if (re.test(seg.text)) hits += 1;
            next = seg.text.replace(re, p.replacement || "");
          } catch (err) {
            next = seg.text;
          }
        } else {
          if (seg.text.indexOf(pattern) >= 0) hits += 1;
          next = seg.text.split(pattern).join(p.replacement || "");
        }
        return { id: seg.id, start_ms: seg.start_ms, end_ms: seg.end_ms, text: next, words: seg.words || [] };
      });
    } else if (p.op === "insert") {
      var item = p.segment || {};
      var newSeg = {
        id: "s_new_" + (list.length + 1),
        start_ms: Number(item.start_ms) || 0,
        end_ms: Number(item.end_ms) || ((Number(item.start_ms) || 0) + 1000),
        text: String(item.text || ""),
        words: []
      };
      var where = 0;
      while (where < list.length && list[where].start_ms <= newSeg.start_ms) where += 1;
      list.splice(where, 0, newSeg);
    } else {
      return fail("VALIDATION", "不认识的字幕操作：" + p.op);
    }

    SUBTITLES[p.project_id] = list;
    return { project_id: p.project_id, op: p.op, hits: hits, segments: clone(list),
             qc: subtitleQc(list), stats: subtitleStats(list) };
  }

  function fail(code, message) {
    return { ok: false, error: { code: code, message: message, detail: "", retryable: false }, request_id: "mock" };
  }

  /** 按白名单响应；未知方法返回 NOT_FOUND —— 与真后端一致。 */
  function call(method, params) {
    var p = params || {};
    switch (method) {
      case "app.onboard": {
        return ok(MOCK_ONBOARD());
      }
      case "app.onboard.done": {
        return ok({ onboarded: true, skipped: !!(p && p.skipped) });
      }
      case "app.migration_notice": {
        // 演示态**没有真搬家这回事**，所以这里只给「无提示」的空形状：
        // 前端拿到 notice=null 就不弹。真机上这份由 load() 从旧家现读出来。
        // app.js 的 maybeMigrationNotice 里还有一道 isMock 闸，两道闸说的是同一件事：
        // 这条提示只讲真实发生过的事，不进演示。
        return ok({ notice: null, dismissed: false });
      }
      case "app.migration_notice.dismiss": {
        return ok({ dismissed: true });
      }
      case "app.open_url": {
        var url = String((p && p.url) || "");
        if (!/^https?:/i.test(url)) return fail("VALIDATION", "只允许 http/https 地址");
        // 演示模式没有系统浏览器可开：留痕给测试与手工验收核对（真机走 os.startfile）
        window.__wbOpenedUrls = window.__wbOpenedUrls || [];
        window.__wbOpenedUrls.push(url);
        return ok({ opened: true, url: url });
      }
      case "app.diagnostics": {
        // 与后端同形状：路径 + 字节 + 包内条目。
        var diagPath = (p.path || MOCKEXPDIR + "diagnostics\diagnostics-20261030-211500.zip");
        if (diagPath.slice(-4).toLowerCase() !== ".zip") diagPath += ".zip";
        return ok({ path: diagPath, bytes: 8421, entries: ["report.txt", "config.redacted.yaml", "log.txt"], log_lines: 400 });
      }
      case "app.log_tail": {
        var lines = [
          "2026-10-30 21:14:02 INFO    video_workbench ==== 会话开始 pid=16172 python=3.14.0 ====",
          "2026-10-30 21:14:02 INFO    video_workbench.service 配置 C:\Users\demo\.bili-scribe\config.yaml",
          "2026-10-30 21:14:09 INFO    video_workbench.job [job_9f2a] [demo] 3s / 10s",
          "2026-10-30 21:14:12 INFO    video_workbench.job [job_9f2a] [demo] 完成",
          "2026-10-30 21:14:20 WARNING video_workbench.job [job_7c11] 快速拼接失败，已切精确重编码",
          "2026-10-30 21:14:33 INFO    video_workbench.service 诊断包已导出（8421 字节）"
        ].slice(-Math.max(1, Math.min(2000, (p && p.lines) || 200)));
        return ok({
          file: INFO.log_file,
          dir: INFO.log_dir,
          lines: lines.length,
          text: lines.join(String.fromCharCode(10)),
          files: [{ name: "wb-20261030.log", path: INFO.log_file, bytes: 48210, mtime: 1793387640 }]
        });
      }
      case "app.info":
        return ok(INFO);
      case "app.bootstrap":
        return ok({ info: INFO, settings: SETTINGS, library: listProjects("", 50, 0), jobs: JOBS,
                    styles: STYLES.map(function (row) { return styleRow(row.config, row.builtin); }) });
      case "app.settings.get":
        return ok(SETTINGS);
      case "app.settings.put":
        merge(SETTINGS, p.values || {});
        return ok({ settings: SETTINGS, saved: SETTINGS.storage.db_path, restart: [] });
      case "app.skin.set":
        SETTINGS.ui.skin = p.skin;
        INFO.skin = p.skin;
        return ok({ skin: p.skin });
      case "app.rail.set":
        SETTINGS.ui.rail_collapsed = !!p.collapsed;
        return ok({ collapsed: !!p.collapsed });
      case "library.list":
        return ok(listProjects(p.query || "", p.limit || 50, p.offset || 0));
      case "library.get":
        for (var i = 0; i < PROJECTS.length; i++) {
          if (PROJECTS[i].id === p.id) {
            var detail = clone(PROJECTS[i]);
            detail.assets = clone(ASSETS[p.id] || []);
            return ok({ project: detail });
          }
        }
        return fail("NOT_FOUND", "项目不存在");
      case "library.delete":
        for (var d = 0; d < PROJECTS.length; d++) {
          if (PROJECTS[d].id === p.id) {
            PROJECTS.splice(d, 1);
            return ok({ id: p.id, removed: true });
          }
        }
        return fail("NOT_FOUND", "项目不存在");
      case "download.resolve":
        if (!String(p.url || "").trim()) return fail("VALIDATION", "链接不能为空");
        return ok(RESOLVE);
      case "download.start":
        return ok({ count: (p.items || []).length, project_ids: ["p_demo_1"], job: { id: "j_mock_batch", type: "download.batch" } });
      case "download.retry_failed":
        // 记住上一次用哪个后端重试，前端测试据此断言"换后端有没有传下去"。
        window.__wbRetried = { id: p.id, backend: p.backend || "" };
        return ok({ job: { id: "j_mock_retry", type: "download.batch", status: "queued" }, count: 1 });
      case "download.backends":
        return ok(MOCK_BACKENDS);
      case "import.local":
        return ok({ total: (p.paths || []).length, project_ids: ["p_demo_3"] });
      case "subtitle.get":
        return ok(subtitleGet(p.project_id));
      case "subtitle.save":
        return ok(subtitleSave(p.project_id, p.segments || []));
      case "subtitle.op":
        return ok(subtitleOp(p));
            case "llm.models": {
        // 永远比卡里已配的多两个：弹窗要能演示「选中还没加的模型」
        var pid = String(p.provider || "").toLowerCase();
        var catalog = {
          deepseek: ["deepseek-chat", "deepseek-reasoner", "deepseek-coder", "deepseek-vl2"],
          moonshot: ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-k2-0905-preview"]
        };
        // 本机 magpie 网关：地址里带 3425 就当它，回一份混装清单（真网关是 30 个模型的聚合）
        var list = catalog[pid] || ["gpt-4o-mini", "gpt-4o", "o4-mini"];
        var probeBase = String((p && p.base_url) || "");
        var provs0 = (SETTINGS.llm && SETTINGS.llm.providers) || [];
        var hit0 = null;
        for (var qi = 0; qi < provs0.length; qi++) {
          if (!p.provider || provs0[qi].id === p.provider) { hit0 = provs0[qi]; break; }
        }
        var mBase = String((p && p.base_url) || (hit0 && hit0.base_url) || "");
        var mKey = String((p && p.api_key) || (hit0 && hit0.api_key) || "");
        if (!mBase || !mKey) {
          return fail("LLM_NOT_CONFIGURED", "还缺接口地址或 API Key");
        }
        if (probeBase.indexOf("127.0.0.1:3425") >= 0 || mBase.indexOf("127.0.0.1:3425") >= 0) {
          list = ["auto", "hy4-preview", "gpt-5.1", "claude-sonnet-4.5", "gemini-2.5-pro", "deepseek-v4.1"];
        }
        return ok({
          provider: p.provider || "",
          base_url: mBase,
          models: list.map(function (id) {
            return { id: id, owned_by: "" };
          }),
          count: list.length
        });
      }
      case "llm.test": {
        // 按 provider/model 回显：界面要能看出"测的是哪一家"，不然两张卡的反馈长得一样。
        var llmCfg = SETTINGS.llm || {};
        var provs = llmCfg.providers || [];
        var hit = null;
        var ref = String((p && p.provider) || "");
        for (var pi = 0; pi < provs.length; pi++) {
          if (!ref || provs[pi].id === ref || provs[pi].name === ref) { hit = provs[pi]; break; }
        }
        if (!hit) hit = provs[0] || { id: "", name: "", base_url: llmCfg.base_url || "", models: [] };
        var chosen = (p && p.model) || (hit.models && hit.models[0]) || llmCfg.model || "";
        // 参数优先：真后端允许把界面里还没保存的地址/密钥传上来（见 _llm_client_for）。
        var effBase = String((p && p.base_url) || hit.base_url || "");
        var effKey = String((p && p.api_key) || hit.api_key || "");
        if (!effBase || !effKey) {
          return fail("LLM_NOT_CONFIGURED", (hit.name || hit.id || "这家服务商") + "：缺少接口地址或 API Key");
        }
        return ok({ ok: true, model: chosen, base_url: effBase, provider: hit.id,
                    latency_ms: 218, reply: "OK" });
      }
      case "subtitle.optimize": {
        var segs0 = clone(SUBTITLES[p.project_id] || []);
        OPT_JOB.status = "succeeded";
        OPT_JOB.result = {
          model: "deepseek-chat",
          segments: segs0.length,
          batches: 1,
          failed: [],
          changes: segs0.map(function (seg) {
            var text = String(seg.text || "");
            var next = text.replace(/^(嗯|啊|那个|然后)/, "").trim();
            if (!next) return null;
            if (!/[。！？，]$/.test(next)) next += "。";
            if (next === text) return null;
            return { id: seg.id, start_ms: seg.start_ms, end_ms: seg.end_ms, before: text, after: next, kind: "text" };
          }).filter(Boolean)
        };
        OPT_JOB.result.changed = OPT_JOB.result.changes.length;
        return ok({ job: OPT_JOB, segments: segs0.length, model: "deepseek-chat" });
      }
      case "subtitle.optimize.apply": {
        var list = clone(SUBTITLES[p.project_id] || []);
        var picked = {};
        (p.accepted_ids || []).forEach(function (id) { picked[id] = true; });
        var byId = {};
        (p.changes || []).forEach(function (c) { byId[c.id] = c; });
        var kept = [];
        list.forEach(function (seg) {
          var change = picked[seg.id] ? byId[seg.id] : null;
          if (!change) { kept.push(seg); return; }
          if (change.after === "") {
            if (kept.length) kept[kept.length - 1].end_ms = seg.end_ms;
            return;
          }
          kept.push(Object.assign({}, seg, { text: change.after }));
        });
        SUBTITLES[p.project_id] = kept;
        return ok({ project_id: p.project_id, applied: (p.accepted_ids || []).length,
                    segments: kept, qc: subtitleQc(kept), stats: subtitleStats(kept),
                    snapshot: "transcript.prev.json" });
      }
      case "subtitle.rollback": {
        var back = clone(SUBTITLES[p.id] || []);
        return ok({ project_id: p.id, segments: back, qc: subtitleQc(back),
                    stats: subtitleStats(back), restored_at: "2026-10-29T22:00:00" });
      }
      case "subtitle.translate": {
        var rows = clone(SUBTITLES[p.project_id] || []);
        var target = p.target === "auto" || !p.target ? "zh" : p.target;
        var failed = [];
        var draft = rows.map(function (seg, index) {
          var line = clone(seg);
          // 每第四条塞一个失败，让"单条重试"这条逻辑在假后端也有东西可点
          if ((index + 1) % 4 === 0) {
            failed.push({ ids: [seg.id], code: "LLM_ERROR", message: "假后端模拟的批次失败", detail: "" });
            line.translated_text = "";
            return line;
          }
          var text = PHRASES[seg.text] || ("[" + target + "] " + seg.text);
          GLOSSARY.forEach(function (entry) {
            var kv = String(entry).split("=");
            if (kv.length === 2 && kv[0]) text = text.split(kv[0]).join(kv[1]);
          });
          line.translated_text = text;
          return line;
        });
        TRANS[p.project_id] = { target: target, status: "draft",
                                updated_at: "2026-10-30T10:00:00", segments: draft };
        TRANS_JOB.status = "succeeded";
        TRANS_JOB.result = { model: "deepseek-chat", target: target, total: rows.length,
                             translated: rows.length - failed.length, batches: Math.ceil(rows.length / 10),
                             glossary: GLOSSARY.slice(), failed: failed };
        return ok({ job: TRANS_JOB, segments: rows.length, target: target });
      }
      case "subtitle.translation": {
        var saved = TRANS[p.project_id];
        if (!saved) return ok({ found: false, target: "", status: "", segments: [] });
        if (p.target && p.target !== "auto" && p.target !== saved.target) {
          return ok({ found: false, target: p.target, status: "", segments: [] });
        }
        return ok({ found: true, target: saved.target, status: saved.status,
                    updated_at: saved.updated_at, segments: clone(saved.segments) });
      }
      case "subtitle.translation.apply": {
        var cur = TRANS[p.project_id];
        if (!cur) return fail("NOT_FOUND", "没有译文草稿，请先做机器翻译");
        // 手改的译文先合进草稿，再谈接受/拒绝——和真后端同一顺序
        var edits = p.overrides || {};
        cur.segments.forEach(function (seg) {
          if (Object.prototype.hasOwnProperty.call(edits, seg.id)) seg.translated_text = String(edits[seg.id] || "");
        });
        var picked = {};
        (p.accepted_ids || []).forEach(function (id) { picked[id] = true; });
        var keepAll = !(p.accepted_ids && p.accepted_ids.length);
        var merged = cur.segments.map(function (seg) {
          var line = clone(seg);
          if (!keepAll && !picked[seg.id]) line.translated_text = "";
          return line;
        });
        cur.status = "confirmed";
        cur.segments = merged;
        var code = cur.target;
        return ok({ project_id: p.project_id, target: code,
                    applied: keepAll ? merged.length : Object.keys(picked).length,
                    srt: "subtitle." + code + ".srt", ass: "subtitle.bilingual.ass",
                    segments: clone(merged) });
      }
      case "subtitle.export": {
        var formats = (p.formats && p.formats.length) ? p.formats : ["srt", "ass", "vtt"];
        var root = "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\索尼音乐中国\\【官方 MV】Never Gonna Give You Up - Rick Astley";
        var files = formats.map(function (f) {
          // 目录与文件名跟后端一致（root\export\subtitle.orig.srt）：
          // 假数据和真实现长得不一样，截图就会替后端撒谎。
          return { format: f, path: root + "\\export\\subtitle.orig." + f,
                   bytes: f === "ass" ? 39704 : (f === "vtt" ? 12880 : 13560) };
        });
        var mode = p.mode || "none";
        if (mode === "none") {
          return ok({ project_id: p.project_id, mode: "none", height: 720, files: files, output: "" });
        }
        EXPO_JOB.status = "succeeded";
        EXPO_JOB.progress = 1;
        EXPO_JOB.result = {
          output: root + "\\export\\source.1080p." + mode + ".mp4",
          bytes: mode === "soft" ? 49807360 : 61236838, mode: mode
        };
        return ok({ project_id: p.project_id, mode: mode,
                    height: Number(p.height) || 1080, files: files,
                    job_id: EXPO_JOB.id, output: "" });
      }
      case "glossary.get":
        return ok({ entries: GLOSSARY.slice() });
      case "glossary.put":
        GLOSSARY = (p.entries || []).slice();
        return ok({ count: GLOSSARY.length, entries: GLOSSARY.slice() });
      case "glossary.import_csv":
        GLOSSARY = GLOSSARY.concat(["gpu=显卡"]);
        return ok({ count: 1, total: GLOSSARY.length, entries: GLOSSARY.slice(),
                    path: p.path || "glossary.csv" });
      case "glossary.export_csv":
        return ok({ count: GLOSSARY.length,
                    path: p.path || "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media\\glossary.csv" });
      case "collection.list": {
        var list = collectionTitles().map(function (title) {
          var kids = collectionChildren(title);
          var summarized = kids.filter(function (item) { return item.has_summary; }).length;
          var box = COLS[title] || null;
          return {
            title: title,
            collection_id: "col_" + title.length + "_mock",
            items: kids.length,
            summarized: summarized,
            ready: summarized >= 2 && summarized === kids.length,
            has_overview: !!box,
            version: box ? box.overview.version : 0,
            updated_at: box ? box.overview.created_at : "",
            children: kids
          };
        });
        return ok({ collections: list });
      }
      case "summary.collection.overview": {
        var title = p.collection || "";
        var kids2 = collectionChildren(title);
        if (!kids2.length) return fail("CONFLICT", "这个合集里没有子项");
        if (kids2.length < 2) return fail("CONFLICT", "合集里至少要有两条视频才能出总览");
        var missing = kids2.filter(function (item) { return !item.has_summary; });
        if (missing.length) {
          return fail("CONFLICT", "有 " + missing.length + " 集还没有总结，先生成它们的总结");
        }
        var bin = seedCollection(title);
        var fresh = {
          id: "sum_col_" + (bin.versions.length + 1),
          version: bin.versions.length + 1,
          role: p.role || "wengao",
          model: "deepseek-chat",
          chars: 1050,
          created_at: "2026-10-30 12:03:18",
          status: "ready",
          one_line: bin.overview.theme,
          revise_note: "",
          theme: bin.overview.theme,
          content: overviewContentFor(title)
        };
        bin.overview = fresh;
        bin.versions = [fresh].concat(bin.versions);
        OV_JOB.result = { version: fresh.version };
        return ok({ job: OV_JOB, title: title, items: kids2.length,
                    role: fresh.role, model: "deepseek-chat" });
      }
      case "summary.collection.get": {
        var wanted = p.id || "";
        var box2 = COLS[wanted] || (wanted && collectionChildren(wanted).length ? seedCollection(wanted) : null);
        var rows = collectionChildren(wanted);
        if (!rows.length) return ok({ found: false, overview: null, versions: [], children: [] });
        return ok({
          found: !!box2,
          collection: { title: wanted, items: rows.length },
          overview: box2 ? box2.overview : null,
          versions: box2 ? box2.versions.map(function (row) {
            return { id: row.id, version: row.version, role: row.role, model: row.model,
                     chars: row.chars, created_at: row.created_at, status: row.status,
                     one_line: row.one_line, revise_note: row.revise_note };
          }) : [],
          children: rows
        });
      }
      // 判据：mock 只承载形状，不承载会变的判定结果（configured_unrecognized/configured_hint 之类不写死）。
      // 角色真源三个字段同理：只补**键的形状**，值留空——哪个角色吃哪个 txt、多少字节、
      // sha 前 8 位都由后端 core/prompt_source.py 现算，写死在这里等于第二份映射表，
      // 用户改一篇 txt 就得跟着漂。
      case "summary.roles":
        return ok({
          roles: [
            // 顺序照后端 ROLE_KEYS。task-31：xueshu（学术分析）已删，shipin（视频总结）
            // 合并进 renhua——所以 renhua 这一条的 tagline 就是 resources/prompts/
            // 视频总结.txt 实测出来的那四条原则，不是照记忆编的。
            { key: "renhua", name: "人话", tagline: "锚点撑全文·推理外显·变形替代定义·体感优先", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" },
            { key: "shensi", name: "深思", tagline: "追问前提与反例", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" },
            { key: "wengao", name: "文稿", tagline: "可直接朗读的稿子", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" },
            { key: "xiezuo", name: "写作", tagline: "重结构与节奏", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" },
            { key: "fenxi", name: "分析", tagline: "拆机制与因果", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" },
            { key: "jinglian", name: "精炼", tagline: "压到最短", passes: 3, prompt_source: null, prompt_bytes: 0, prompt_sha8: "" }
          ],
          // 这里**不写 retired 名单**：退役角色只有一个出处（权威层 RETIRED_ROLES），
          // mock 里再抄一份就是第二个 owner，而 summary.js:234 本来就按「读不到当空」处理。
          default: "wengao"
        });
      case "summary.models": {
        // 与真后端同构：把配置里的服务商展开成「服务商id/模型名」
        var cards = (SETTINGS.llm.providers || []);
        var out = [];
        cards.forEach(function (card) {
          (card.models || []).forEach(function (name) {
            out.push({ spec: card.id + "/" + name, provider: card.id,
                       provider_name: card.name || card.id, model: name });
          });
        });
        var act = cards.filter(function (c) { return c.id === (SETTINGS.llm.active_provider || ""); })[0] || cards[0];
        return ok({ items: out,
                    default: act && (act.models || []).length ? (act.id + "/" + act.models[0]) : "",
                    configured: out.length > 0 });
      }
      case "summary.start":
      case "summary.refine": {
        var pid = p.project_id || "p_demo_1";
        var box = seedSummary(pid);
        if (method === "summary.refine") {
          var next = summaryRow(box.versions.length + 1, summaryContentFor(p.project_id), box.summary.role,
                                p.requirement || "按你的要求改过");
          box.summary = next;
          box.versions = [next].concat(box.versions);
        } else {
          // 「重新生成」也是一版：真后端每次 submit 都 summary_insert，历史只增不减。
          // mock 少加一版，截图上就会显示成「重出没有产生新版本」——那是假象。
          var fresh = summaryRow(box.versions.length + 1, summaryContentFor(p.project_id),
                                 p.role || box.summary.role, "");
          if (p.model) fresh.model = String(p.model).split("/").pop();
          box.summary = fresh;
          box.versions = [fresh].concat(box.versions);
        }
        SUM_JOB.result = { version: box.summary.version };
        return ok({ job: SUM_JOB, role: box.summary.role, model: box.summary.model || "deepseek-chat" });
      }
      case "summary.get": {
        // 首次进页面就给一份现成的（含两版历史），这样截图和手工验收看到的是
        // 「用过一阵子」的样子，而不是永远的空态。空态另有专门断言覆盖。
        var got = SUMMARIES[p.id] || (p.id ? seedSummary(p.id) : null);
        if (!got) {
          return ok({ found: false, summary: {}, versions: [] });
        }
        return ok({ found: true, summary: got.summary,
                    versions: got.versions.map(function (row) {
                      return {
                        id: row.id, version: row.version, role: row.role, model: row.model,
                        chars: row.chars, created_at: row.created_at, status: row.status,
                        one_line: row.one_line, revise_note: row.revise_note,
                        usage: JSON.parse(JSON.stringify(row.usage || {}))
                      };
                    }),
                    totals: usageTotals(got.versions) });
      }
      case "summary.versions": {
        var box2 = SUMMARIES[p.id];
        return ok({ versions: box2 ? box2.versions.map(function (row) {
          return { id: row.id, version: row.version, role: row.role, model: row.model,
                   chars: row.chars, created_at: row.created_at, status: row.status,
                   one_line: row.one_line, revise_note: row.revise_note,
                   usage: JSON.parse(JSON.stringify(row.usage || {})) };
        }) : [] });
      }
      case "summary.restore": {
        var bin = SUMMARIES[p.project_id];
        if (!bin) return fail("NOT_FOUND", "找不到这一版总结");
        var old = null;
        bin.versions.forEach(function (row) { if (row.id === p.summary_id) old = row; });
        if (!old) return fail("NOT_FOUND", "找不到这一版总结");
        var copy = JSON.parse(JSON.stringify(old));
        copy.id = "sum_mock_" + (bin.versions.length + 1);
        copy.version = bin.versions.length + 1;
        copy.revise_note = "恢复自第 " + old.version + " 版";
        bin.summary = copy;
        bin.versions = [copy].concat(bin.versions);
        return ok({ summary: copy, restored_from: old.version });
      }
      case "publish.preview": {
        var draft = mockPreview(p.project_id);
        if (!draft) return fail("CONFLICT", "这个项目还没有总结，先去「总结」页出一版");
        var existing = null;
        DRAFTS.forEach(function (row) { if (row.project_id === p.project_id) existing = row; });
        draft.note = existing ? mockDraftView(existing) : null;
        draft.gates = mockGates(draft, projectById(p.project_id), existing, {
          confirmed: !!p.confirmed,
          loggedIn: MOCK_LOGGED_IN
        });
        return ok(draft);
      }
      case "publish.note": {
        var project = projectById(p.project_id);
        if (!project) return fail("NOT_FOUND", "项目不存在");
        var built = mockPreview(p.project_id);
        if (!built) return fail("CONFLICT", "这个项目还没有总结，先去「总结」页出一版");
        var target = null;
        DRAFTS.forEach(function (row) {
          if (p.note_pk ? row.id === p.note_pk : row.project_id === p.project_id) target = row;
        });
        var title = String(p.title || built.title).trim();
        var summaryText = String(p.summary || built.summary).trim();
        var content = String(p.content || built.content);
        var plain = mockPlain(content);
        if (!title) return fail("VALIDATION", "标题不能为空");
        if (!plain.trim()) return fail("VALIDATION", "正文不能为空，先给这个项目出一版总结");
        if (Array.from(title).length > 80) return fail("VALIDATION", "标题超过 80 字");
        if (Array.from(summaryText).length > 400) return fail("VALIDATION", "摘要超过 400 字");
        if (target) {
          target.title = title;
          target.summary = summaryText;
          target.content = content;
        } else {
          DRAFT_SEQ += 1;
          target = {
            id: "draft_mock_" + DRAFT_SEQ,
            project_id: p.project_id,
            bvid: project.bvid || "",
            aid: 1000 + DRAFT_SEQ,
            note_id: null,
            title: title,
            summary: summaryText,
            content: content,
            status: "draft",
            created_at: new Date().toISOString().slice(0, 16).replace("T", " "),
            published_at: null,
            withdrawn_at: null
          };
          DRAFTS.unshift(target);
        }
        return ok({
          note: mockDraftView(target),
          draft: built,
          warnings: [],
          gates: mockGates(mockDraftView(target), project, target, { confirmed: true })
        });
      }
      case "publish.list":
        // 只回 draft：发过 / 撤过的进「发布记录」，混进草稿列表会让人以为还能改
        return ok({
          notes: DRAFTS.filter(function (row) {
            if ((row.status || "draft") !== "draft") return false;
            return !p.project_id || row.project_id === p.project_id;
          }).map(mockDraftView),
          limits: { title: 80, summary: 400 }
        });
      case "publish.get": {
        var hit = null;
        DRAFTS.forEach(function (row) { if (row.id === p.id) hit = row; });
        if (!hit) return fail("NOT_FOUND", "草稿不存在");
        return ok({
          note: mockDraftView(hit),
          limits: { title: 80, summary: 400 },
          gates: mockGates(mockDraftView(hit), projectById(hit.project_id), hit, {
            confirmed: !!p.confirmed,
            loggedIn: MOCK_LOGGED_IN
          })
        });
      }
      case "publish.regenerate": {
        var hit2 = null;
        DRAFTS.forEach(function (row) { if (row.id === p.id) hit2 = row; });
        if (!hit2) return fail("NOT_FOUND", "草稿不存在");
        var fresh = mockPreview(hit2.project_id);
        if (!fresh) return fail("CONFLICT", "这个项目还没有总结，重新生成也没有素材");
        var merged = JSON.parse(JSON.stringify(fresh));
        if (p.part !== "all") {
          merged.title = p.part === "summary" ? hit2.title : fresh.title;
          merged.summary = p.part === "title" ? hit2.summary : fresh.summary;
          merged.content = hit2.content;
          merged.content_text = mockPlain(hit2.content);
          merged.chars = mockChars(merged.content_text);
        }
        merged.note = mockDraftView(hit2);
        return ok({
          draft: merged,
          part: p.part || "summary",
          gates: mockGates(merged, projectById(hit2.project_id), hit2, {})
        });
      }
      case "publish.publish": {
        var target2 = null;
        DRAFTS.forEach(function (row) { if (row.id === p.id) target2 = row; });
        if (!target2) return fail("NOT_FOUND", "草稿不存在");
        if (target2.status === "published") {
          return fail("CONFLICT", "这条已经发过了（note_id=" + (target2.note_id || "未知") + "）");
        }
        if (!p.confirmed) return fail("CONFLICT", "还没勾「我已阅读并确认」，这条不发布");
        var view2 = mockDraftView(target2);
        // 勾了确认 + 已登录：与真后端一致，三关都按 ok 算
        var report = mockGates(view2, projectById(target2.project_id), target2, {
          confirmed: true,
          loggedIn: MOCK_LOGGED_IN
        });
        if (!report.ready) {
          var bad = report.gates
            .filter(function (g) { return g.status !== "ok"; })
            .map(function (g) { return g.label + "：" + (g.reason || g.status); })
            .join("；");
          return fail("CONFLICT", "门禁没过，先处理这几项：" + bad, JSON.stringify(report));
        }
        NOTE_SEQ += 1;
        target2.status = "published";
        target2.note_id = "note_mock_" + NOTE_SEQ;
        target2.published_at = new Date().toISOString().slice(0, 19).replace("T", " ");
        return ok({
          note: mockDraftView(target2),
          note_id: target2.note_id,
          attempts: 1,
          url: "https://www.bilibili.com/video/" + (target2.bvid || "")
        });
      }
      case "publish.records": {
        var STATUS_TEXT = { published: "已发布", withdrawn: "已撤回", failed: "发布失败" };
        var rows = DRAFTS.filter(function (row) {
          return row.status === "published" || row.status === "withdrawn" || row.status === "failed";
        }).map(function (row) {
          var proj = projectById(row.project_id) || {};
          var bvid = row.bvid || proj.bvid || "";
          return {
            id: row.id,
            project_id: row.project_id,
            project_title: proj.title || row.project_title || "未知项目",
            up_name: proj.up_name || "",
            bvid: bvid,
            note_id: row.note_id || "",
            title: row.title || "",
            status: row.status,
            status_text: STATUS_TEXT[row.status] || row.status,
            error: row.error || "",
            published_at: row.published_at || "",
            withdrawn_at: row.withdrawn_at || "",
            updated_at: row.updated_at || row.created_at || "",
            title_chars: Array.from(String(row.title || "")).length,
            url: bvid ? "https://www.bilibili.com/video/" + bvid : ""
          };
        });
        var counts = { published: 0, withdrawn: 0, failed: 0 };
        rows.forEach(function (row) { if (counts[row.status] !== undefined) counts[row.status] += 1; });
        return ok({ records: rows, counts: counts, total: rows.length });
      }
      case "app.open_url": {
        // 只记下来，不真开：mock 里"打开浏览器"会把 jsdom 之外的窗口拉起来
        window.__wbOpenedUrls = window.__wbOpenedUrls || [];
        window.__wbOpenedUrls.push(p.url);
        return ok({ url: p.url });
      }
      case "publish.withdraw": {
        var target3 = null;
        DRAFTS.forEach(function (row) { if (row.id === p.id) target3 = row; });
        if (!target3) return fail("NOT_FOUND", "草稿不存在");
        if (target3.status !== "published") return fail("CONFLICT", "只有已发布的笔记才能撤回");
        if (!target3.note_id) return fail("CONFLICT", "这条记录没有 note_id，撤不回来");
        target3.status = "withdrawn";
        target3.withdrawn_at = new Date().toISOString().slice(0, 19).replace("T", " ");
        return ok({ note: mockDraftView(target3), note_id: target3.note_id });
      }
      case "publish.delete": {
        var before = DRAFTS.length;
        DRAFTS = DRAFTS.filter(function (row) { return row.id !== p.id; });
        if (DRAFTS.length === before) return fail("NOT_FOUND", "草稿不存在");
        return ok({ removed: 1 });
      }
      case "clip.focus":
        return ok({
          project_id: p.project_id,
          focus: FOCUS[p.project_id] || [],
          detector: FOCUS_DETECTOR[p.project_id] || "",
          subjects: FOCUS_SUBJECTS[p.project_id] || [],
          subject: FOCUS_SUBJECT[p.project_id] || "",
          cuts: FOCUS_CUTS[p.project_id] || [],
          enhance: FOCUS_ENHANCE[p.project_id] !== false,
          notes: [],
          safe_area: { top: 120, bottom: 260, width: 1080, height: 1920 },
          root: ""
        });
      case "clip.focus.subject": {
        var picked = null;
        (FOCUS_SUBJECTS[p.project_id] || []).forEach(function (row) {
          if (row.id === p.subject) picked = row;
        });
        if (!picked) return fail("NOT_FOUND", "没有这个主体");
        var rebuilt = (picked.track || []).map(function (point) {
          return { t_ms: point.t_ms, x: point.x };
        });
        FOCUS[p.project_id] = rebuilt;
        FOCUS_SUBJECT[p.project_id] = picked.id;
        return ok({
          project_id: p.project_id,
          focus: rebuilt,
          subject: picked.id,
          label: picked.label,
          subjects: FOCUS_SUBJECTS[p.project_id] || [],
          cuts: FOCUS_CUTS[p.project_id] || [],
          notes: [],
          path: ""
        });
      }
      case "clip.focus.save": {
        var manualPoints = (p.focus || []).map(function (point) {
          return { t_ms: Number(point.t_ms) || 0, x: Number(point.x), manual: true };
        });
        var kept = (FOCUS[p.project_id] || []).filter(function (point) {
          return !manualPoints.some(function (next) {
            return next.t_ms === point.t_ms;
          });
        });
        var merged = kept.concat(manualPoints).sort(function (a, b) {
          return a.t_ms - b.t_ms;
        });
        FOCUS[p.project_id] = merged;
        return ok({ project_id: p.project_id, focus: merged, manual: manualPoints.length, path: "" });
      }
      case "clip.focus.analyze":
        return ok({ job: { id: "j_mock_focus", type: "clip.focus", status: "running" } });
      case "clip.analyze": {
        var rows0 = clipSeed(p.project_id);
        CLIP_JOB.status = "succeeded";
        CLIP_JOB.progress = 1;
        CLIP_JOB.result = {
          rows: rows0.length,
          // 与真后端同形：refined / vision_model / audio_analyzed / notes。
          // mock 里假装配了 vision、精析改了 2 条，这样那句
          // 「候选已更新（画面精析改了 N 条）」在截图里也能被看到。
          refined: 2,
          vision_model: "mock-vl",
          audio_analyzed: 2,
          notes: []
        };
        return ok({ job: CLIP_JOB });
      }
      case "clip.candidates": {
        var proj = null;
        for (var q = 0; q < PROJECTS.length; q++) {
          if (PROJECTS[q].id === p.id) proj = PROJECTS[q];
        }
        if (!proj) return fail("NOT_FOUND", "项目不存在");
        var rows1 = clipSeed(p.id);
        return ok({
          project: proj,
          duration_ms: Math.round((proj.duration_sec || 0) * 1000),
          has_transcript: !!proj.has_transcript,
          frame_ms: 40,
          fps: 25,
          boundaries: clipBoundaries(p.id),
          source_path: clipSource(p.id),
          safe_area: { top: 120, bottom: 260, width: 1080, height: 1920 },
          focus: FOCUS[p.id] || [],
          candidates: rows1
        });
      }
      case "nle.info":
        return ok(MOCK_NLE_INFO());
      case "clip.deliver": {
        MOCK_DELIVER.channel = p.channel || "jianying";
        MOCK_DELIVER.place_in_app = Boolean(p.place_in_app);
        NLE_DELIVER_JOB.status = "queued";
        NLE_DELIVER_JOB.progress = 0.2;
        NLE_DELIVER_JOB.result = null;
        return ok({ job: NLE_DELIVER_JOB, channel: MOCK_DELIVER.channel });
      }
      case "clip.copy": {
        var hit = clipFind(p.candidate_id);
        if (!hit) return fail("NOT_FOUND", "候选不存在");
        if (hit.row.copy && hit.row.copy.title && !p.regenerate) {
          return ok({ candidate_id: p.candidate_id, copy: hit.row.copy, reused: true });
        }
        COPY_JOB.status = "running";
        COPY_JOB.result = null;
        MOCK_PENDING_COPY = p.candidate_id;
        return ok({ candidate_id: p.candidate_id, job: COPY_JOB, reused: false });
      }
      case "clip.copy.save": {
        var saved = clipFind(p.candidate_id);
        if (!saved) return fail("NOT_FOUND", "候选不存在");
        var copyRow = saved.row.copy || (saved.row.copy = {});
        copyRow.title = String(p.title || "").slice(0, 30);
        copyRow.description = String(p.description || "").slice(0, 120);
        copyRow.tags = (p.tags || []).slice(0, 5);
        copyRow.edited = true;
        returned = { candidate_id: p.candidate_id, copy: copyRow, problems: [] };
        if (copyRow.title.length < 4) returned.problems.push("标题太短，发布前再写具体一点");
        if (copyRow.tags.length < 3) {
          returned.problems.push("标签只有 " + copyRow.tags.length + " 个，至少 3 个");
        }
        return ok(returned);
      }
      case "clip.cover": {
        var covHit = clipFind(p.candidate_id);
        if (!covHit) return fail("NOT_FOUND", "候选不存在");
        var at = p.at_ms === undefined || p.at_ms < 0 ? covHit.row.start_ms + 1000 : Number(p.at_ms);
        var info = { path: "", at_ms: at, bytes: 184320, source: "frame" };
        covHit.row.copy = covHit.row.copy || {};
        covHit.row.copy.cover = info;
        return ok({ candidate_id: p.candidate_id, cover: info, copy: covHit.row.copy });
      }
      case "clip.cover.frames": {
        var fHit = clipFind(p.candidate_id);
        if (!fHit) return fail("NOT_FOUND", "候选不存在");
        var span = fHit.row.end_ms - fHit.row.start_ms;
        var frames = [];
        for (var f = 0; f < 4; f += 1) {
          frames.push({ path: "", at_ms: fHit.row.start_ms + Math.round((span * (f + 1)) / 5) });
        }
        return ok({ candidate_id: p.candidate_id, frames: frames, count: frames.length });
      }
      case "clip.cover.upload": {
        var uHit = clipFind(p.candidate_id);
        if (!uHit) return fail("NOT_FOUND", "候选不存在");
        var up = { path: "", at_ms: -1, bytes: 245760, source: "upload" };
        uHit.row.copy = uHit.row.copy || {};
        uHit.row.copy.cover = up;
        return ok({ candidate_id: p.candidate_id, cover: up, copy: uHit.row.copy });
      }
      case "clip.update": {
        var found = clipFind(p.id);
        if (!found) return fail("NOT_FOUND", "候选不存在");
        var row = found.row;
        var notes = row.notes || (row.notes = {});
        if (p.status) {
          if (["draft", "locked", "rejected"].indexOf(p.status) < 0) {
            return fail("VALIDATION", "状态不合法");
          }
          row.status = p.status;
        }
        if (p.title !== undefined) row.title = String(p.title);
        if (p.score !== undefined) row.score = Number(p.score) || 0;
        if (p.start_ms !== undefined || p.end_ms !== undefined) {
          var startMs = p.start_ms === undefined ? row.start_ms : Number(p.start_ms);
          var endMs = p.end_ms === undefined ? row.end_ms : Number(p.end_ms);
          if (p.align === "frame") {
            startMs = Math.round(startMs / 40) * 40;
            endMs = Math.round(endMs / 40) * 40;
          }
          if (!(endMs - startMs >= 400)) return fail("VALIDATION", "切片至少要 0.4 秒");
          if (!notes.edited_ms) {
            notes.raw_start_ms = row.start_ms;
            notes.raw_end_ms = row.end_ms;
          }
          notes.edited_ms = true;
          row.start_ms = startMs;
          row.end_ms = endMs;
        }
        return ok({ candidate: row });
      }
      case "clip.create": {
        var rows2 = clipSeed(p.project_id);
        var row2 = {
          id: "cand_new_" + (rows2.length + 1), kind: p.kind || "highlight",
          start_ms: Number(p.start_ms) || 0, end_ms: Number(p.end_ms) || 20_000,
          score: 0, title: p.title || "手动切片", reason: "", quote: "",
          tags: [], status: "draft", source: "manual", notes: { origin: "manual" }
        };
        rows2.push(row2);
        return ok({ candidate: row2 });
      }
      case "clip.export": {
        var cand3 = clipFind(p.candidate_id);
        if (!cand3) return fail("NOT_FOUND", "候选不存在");
        if (["precise", "fast"].indexOf(p.mode) < 0) {
          return fail("VALIDATION", "导出模式不合法");
        }
        var fast = p.mode === "fast";
        var vertical = p.aspect === "9x16";
        var vmode = vertical ? (p.vertical_mode || "center") : "";
        var expId = "j_mock_export_" + (EXPORTS.length + 1);
        var offset = fast ? 480 : 0;
        var expDuration = cand3.row.end_ms - cand3.row.start_ms - offset;
        var expJob = {
          id: expId, type: "clip.export", status: "running", progress: 0.2,
          stage: "导出中", result: {}
        };
        EXPORT_JOBS[expId] = expJob;
        EXPORTS.push({
          job_id: expId, status: "running", candidate_id: p.candidate_id,
          path: "", name: "", bytes: 0, duration_ms: 0, expected_ms: 0,
          offset_ms: 0, mode: p.mode, subtitle_mode: p.subtitle_mode || "none",
          aspect: p.aspect || "16x9", vertical_mode: vmode,
          note: "", error: "", exists: false, batch_id: p.batch_id || ""
        });
        window.setTimeout(function () {
          expJob.status = "succeeded";
          expJob.progress = 1;
          expJob.stage = "已完成";
          var expName =
            (cand3.row.title || "切片") +
            (vertical ? "_9x16_" + vmode : "_16x9") +
            (fast ? "_fast" : "_precise") +
            ".mp4";
          expJob.result = {
            path: MOCKEXPDIR + expName,
            bytes: 2_400_000, duration_ms: expDuration, expected_ms:
              cand3.row.end_ms - cand3.row.start_ms, offset_ms: offset, mode: p.mode,
            aspect: p.aspect || "16x9", vertical_mode: vmode
          };
          for (var e = 0; e < EXPORTS.length; e += 1) {
            if (EXPORTS[e].job_id !== expId) continue;
            EXPORTS[e].status = "succeeded";
            EXPORTS[e].path = expJob.result.path;
            EXPORTS[e].name = expName;
            EXPORTS[e].bytes = expJob.result.bytes;
            EXPORTS[e].duration_ms = expDuration;
            EXPORTS[e].expected_ms = expJob.result.expected_ms;
            EXPORTS[e].offset_ms = offset;
            // 与后端 offset_note 同语义：成片更短=片尾吸到关键帧，更长=开头多带
            EXPORTS[e].note = fast
              ? "快速模式：成片比要求短 " + offset + " ms（片尾吸到关键帧）"
              : "";
            EXPORTS[e].exists = true;
          }
        }, 400);
        return ok({ job: expJob });
      }
      case "clip.export.batch": {
        var batchIds = p.candidate_ids || [];
        if (!batchIds.length) return fail("VALIDATION", "先勾几条切片再批量导出");
        if (["precise", "fast"].indexOf(p.mode) < 0) {
          return fail("VALIDATION", "导出模式不合法");
        }
        var batchKey = "batch_mock_" + (EXPORTS.length + 1);
        var batchJobs = [];
        var batchSkipped = [];
        var fast2 = p.mode === "fast";
        var vertical2 = p.aspect === "9x16";
        var vmode2 = vertical2 ? (p.vertical_mode || "center") : "";
        // 故意让第二条失败：批量导出的关键语义是"部分成功"，全成功的假数据测不出这条
        batchIds.forEach(function (cid, index) {
          var hit4 = clipFind(cid);
          if (!hit4) {
            batchSkipped.push({ candidate_id: cid, reason: "候选不存在" });
            return;
          }
          var jobId = "j_mock_export_" + (EXPORTS.length + 1);
          var job4 = {
            id: jobId, type: "clip.export", status: "running", progress: 0.2,
            stage: "导出中", result: {}
          };
          EXPORT_JOBS[jobId] = job4;
          EXPORTS.push({
            job_id: jobId, status: "running", candidate_id: cid,
            path: "", name: "", bytes: 0, duration_ms: 0, expected_ms: 0,
            offset_ms: 0, mode: p.mode, subtitle_mode: p.subtitle_mode || "none",
            aspect: p.aspect || "16x9", vertical_mode: vmode2,
            note: "", error: "", exists: false, batch_id: batchKey
          });
          batchJobs.push({ candidate_id: cid, job_id: jobId, name: "" });
          (function (job5, row5, cand5, failThis) {
            window.setTimeout(function () {
              var expName =
                (cand5.row.title || "切片") +
                (vertical2 ? "_9x16_" + vmode2 : "_16x9") +
                (fast2 ? "_fast" : "_precise") +
                ".mp4";
              if (failThis) {
                job5.status = "failed";
                job5.stage = "失败";
                job5.error_code = "PROCESS_FAILED";
                job5.error_message = "ffmpeg 退出码 1：这一条故意失败的样例";
                row5.status = "failed";
                row5.error = job5.error_message;
                return;
              }
              job5.status = "succeeded";
              job5.progress = 1;
              job5.stage = "已完成";
              job5.result = {
                path: MOCKEXPDIR + expName,
                bytes: 2_400_000,
                duration_ms: cand5.row.end_ms - cand5.row.start_ms - (fast2 ? 480 : 0),
                expected_ms: cand5.row.end_ms - cand5.row.start_ms,
                offset_ms: fast2 ? 480 : 0, mode: p.mode,
                aspect: p.aspect || "16x9", vertical_mode: vmode2
              };
              row5.status = "succeeded";
              row5.path = job5.result.path;
              row5.name = expName;
              row5.bytes = job5.result.bytes;
              row5.duration_ms = job5.result.duration_ms;
              row5.expected_ms = job5.result.expected_ms;
              row5.note = fast2
                ? "快速模式：成片比要求短 480 ms（片尾吸到关键帧）"
                : "";
              row5.exists = true;
            }, 400 + index * 120);
          })(job4, EXPORTS[EXPORTS.length - 1], hit4, index === 1);
        });
        return ok({
          batch_id: batchKey,
          jobs: batchJobs,
          skipped: batchSkipped,
          submitted: batchJobs.length,
          estimate_bytes: 4_800_000
        });
      }

  // 合并来源（P4-01）。切片成片取 EXPORTS 里已经成功的行，素材库取 ASSETS 里 kind=source。
  function mergeSpecOf(path) {
    var name = String(path || "");
    // 素材库源片在真库里是 1280×720/25（short_60s 就是），混进合并要能看见差异
    var small =
      name.indexOf("short_60s") >= 0 ||
      name.toLowerCase().slice(-10) === "source.mp4";
    return {
      width: small ? 1280 : 1920,
      height: small ? 720 : 1080,
      fps: small ? 25 : 30,
      vcodec: "h264",
      pix_fmt: "yuv420p",
      acodec: "aac",
      sample_rate: 48000,
      channels: 2
    };
  }

  function mergeSources(query) {
    var needle = String(query || "").toLowerCase();
    var clips = [];
    for (var i = 0; i < EXPORTS.length; i += 1) {
      var row = EXPORTS[i];
      if (row.status !== "succeeded" || !row.path) continue;
      if (needle && row.path.toLowerCase().indexOf(needle) < 0) continue;
      clips.push({
        path: row.path,
        name: row.name,
        project_id: "p_demo_1",
        project_title: "【硬核】三十分钟讲透注意力机制",
        bytes: row.bytes || 2_400_000,
        duration_ms: row.duration_ms || 22_400,
        exists: true,
        origin: "clip"
      });
    }
    var library = [];
    function projectTitle(pid) {
      for (var k = 0; k < PROJECTS.length; k += 1) {
        if (PROJECTS[k].id === pid) return PROJECTS[k].title;
      }
      return pid;
    }
    Object.keys(ASSETS).forEach(function (pid) {
      (ASSETS[pid] || []).forEach(function (asset) {
        if (asset.kind !== "source") return;
        if (needle && String(asset.path || "").toLowerCase().indexOf(needle) < 0) return;
        library.push({
          path: asset.path,
          name: String(asset.path || "").split(/[\/]/).pop(),
          project_id: pid,
          project_title: projectTitle(pid),
          bytes: asset.bytes || 49_600_000,
          duration_ms: asset.duration_ms || 59_480,
          exists: true,
          origin: "library"
        });
      });
    });
    if (!clips.length && !needle) {
      clips = [
        {
          path: MOCKEXPDIR + "先证伪直觉，再给方法_16x9_precise.mp4",
          name: "先证伪直觉，再给方法_16x9_precise.mp4",
          project_id: "p_demo_1",
          project_title: projectTitle("p_demo_1"),
          bytes: 2_400_000,
          duration_ms: 22_400,
          exists: true,
          origin: "clip"
        },
        {
          path: MOCKEXPDIR + "把结论推到能动手的步骤_16x9_precise.mp4",
          name: "把结论推到能动手的步骤_16x9_precise.mp4",
          project_id: "p_demo_1",
          project_title: projectTitle("p_demo_1"),
          bytes: 1_900_000,
          duration_ms: 17_700,
          exists: true,
          origin: "clip"
        }
      ];
    }
    return { clips: clips, library: library, total: clips.length + library.length };
  }

  function mergePreflight(p) {
    var paths = (p.paths || []).filter(Boolean);
    if (!paths.length) return fail("VALIDATION", "先往列表里加片段");
    var items = paths.map(function (path, index) {
      var spec = mergeSpecOf(path);
      var known = null;
      for (var i = 0; i < EXPORTS.length; i += 1) {
        if (EXPORTS[i].path === path) known = EXPORTS[i];
      }
      var seeded = String(path).indexOf("把结论推到能动手的步骤") >= 0 ? 17_700 : 0;
      return {
        path: path,
        name: String(path).split(/[\/]/).pop(),
        origin: "mock",
        exists: true,
        ok: true,
        duration_ms: known
          ? known.duration_ms
          : seeded || (spec.width === 1280 ? 59_480 : 22_400),
        fps: spec.fps,
        vcodec: spec.vcodec,
        pix_fmt: spec.pix_fmt,
        acodec: spec.acodec,
        sample_rate: spec.sample_rate,
        channels: spec.channels,
        width: spec.width,
        height: spec.height,
        bytes: known ? known.bytes : 2_400_000,
        error: "",
        no: index + 1
      };
    });
    var first = items[0];
    var diffs = [];
    var fields = [
      ["width", "宽度"], ["height", "高度"], ["fps", "帧率"], ["vcodec", "视频编码"],
      ["pix_fmt", "像素格式"], ["acodec", "音频编码"], ["sample_rate", "采样率"], ["channels", "声道数"]
    ];
    fields.forEach(function (pair) {
      var same = items.every(function (row) { return row[pair[0]] === first[pair[0]]; });
      if (same) return;
      diffs.push({
        field: pair[0],
        label: pair[1],
        values: items.map(function (row) {
          var v = row[pair[0]];
          var text = pair[0] === "fps" ? v + " fps"
            : pair[0] === "sample_rate" ? v + " Hz"
            : pair[0] === "channels" ? v + " 声道" : String(v);
          return { name: row.name, value: text };
        })
      });
    });
    var totalMs = items.reduce(function (sum, row) { return sum + row.duration_ms; }, 0);
    var totalBytes = items.reduce(function (sum, row) { return sum + row.bytes; }, 0);
    var canQuick = items.length >= 2 && diffs.length === 0;
    var targetW = Number(p.width) || first.width;
    var targetH = Number(p.height) || first.height;
    var targetFps = Number(p.fps) || first.fps;
    var minutes = totalMs / 60000;
    return ok({
      items: items,
      count: items.length,
      missing: [], broken: [],
      total_ms: totalMs,
      total_text: Math.floor(totalMs / 60000) + ":" + ("0" + (Math.floor(totalMs / 1000) % 60)).slice(-2),
      total_bytes: totalBytes,
      total_bytes_text: (totalBytes / 1024 / 1024).toFixed(1) + " MB",
      consistent: diffs.length === 0,
      can_quick: canQuick,
      differences: diffs,
      target: { width: targetW, height: targetH, fps: targetFps, sample_rate: 48000, channels: 2 },
      warnings: canQuick ? [] : diffs.length ? ["规格不一致，只能精确重编码"] : ["至少要两个片段才能合并"],
      estimate: {
        quick: { mode: "quick", size_bytes: totalBytes, size_text: (totalBytes / 1024 / 1024).toFixed(1) + " MB", seconds: Math.round(minutes * 1.2), seconds_text: "约 " + Math.max(1, Math.round(minutes * 1.2)) + " 秒" },
        precise: { mode: "precise", size_bytes: Math.round(totalBytes * 0.42), size_text: (totalBytes * 0.42 / 1024 / 1024).toFixed(1) + " MB", seconds: Math.round(minutes * 45), seconds_text: "约 " + Math.max(1, Math.round(minutes * 45)) + " 秒" }
      },
      output: { dir: MOCKEXPDIR + "merge", name: "合并_" + items.length + "段_20261030-170000.mp4" },
      checked_at: Math.floor(Date.now() / 1000)
    });
  }

      case "merge.sources":
        return ok(mergeSources(p.query));
      case "merge.preflight":
        return mergePreflight(p);
      case "merge.start": {
        if (!p.paths || !p.paths.length) return fail("VALIDATION", "先往列表里加片段");
        // 与后端同语义：precise 已支持（P4-03），只有"片段不够"和"quick 但规格不一致"才是 CONFLICT。
        var pre = mergePreflight({ paths: p.paths, width: p.width, height: p.height, fps: p.fps });
        if (!pre.ok) return pre;
        if (pre.data.count < 2 || (p.mode === "quick" && !pre.data.can_quick)) {
          return fail("CONFLICT", p.mode === "quick" && !pre.data.can_quick
            ? "规格不一致：帧率 / 高度"
            : "至少要两个可用片段才能拼接", JSON.stringify(pre.data.differences || []));
        }
        var mergeId = "mg_mock_" + (MERGE_SEQ += 1);
        var outName = (p.out_name || "").trim() || pre.data.output.name;
        if (outName.slice(-4).toLowerCase() !== ".mp4") outName += ".mp4";
        MERGE_JOB = {
          id: "j_mock_merge",
          type: "merge.run",
          status: "running",
          progress: 0,
          payload: { merge_id: mergeId, paths: p.paths.slice(), mode: p.mode || "quick" },
          result: null,
          error_code: "",
          error_message: "",
          created_at: mergeStamp(),
          started_at: mergeStamp(),
          finished_at: ""
        };
        MERGES.unshift({
          id: mergeId,
          mode: p.mode || "quick",
          status: "running",
          output_path: (p.out_dir || pre.data.output.dir) + "\\" + outName,
          created_at: mergeStamp()
        });
        setTimeout(function () {
          var row = MERGES[0];
          row.status = "succeeded";
          MERGE_JOB.status = "succeeded";
          MERGE_JOB.progress = 1;
          MERGE_JOB.finished_at = mergeStamp();
          MERGE_JOB.result = {
            merge_id: mergeId,
            path: row.output_path,
            name: outName,
            bytes: pre.data.total_bytes,
            duration_ms: pre.data.total_ms,
            expected_ms: pre.data.total_ms,
            delta_ms: 0,
            mode: row.mode,
            mode_used: row.mode,
            requested_mode: row.mode,
            fallback_used: false,
            note: "",
            count: pre.data.count
          };
        }, 400);
        return ok({ job: MERGE_JOB, merge_id: mergeId, output: MERGES[0].output_path, name: outName, count: pre.data.count });
      }
      case "merge.list": {
        return ok({
          items: MERGES.map(function (row) {
            return Object.assign({}, row, {
              name: String(row.output_path || "").split(/[\\/]/).pop(),
              exists: true,
              bytes: 4608000
            });
          }),
          total: MERGES.length
        });
      }

      case "clip.exports": {
        if (!p.project_id) return fail("NOT_FOUND", "项目不存在");
        return ok({ exports: EXPORTS.slice() });
      }
      case "clip.delete": {
        var hit = clipFind(p.id);
        if (!hit) return fail("NOT_FOUND", "候选不存在");
        var left = hit.rows.filter(function (item) { return item.id !== p.id; });
        hit.rows.length = 0;
        left.forEach(function (item) { hit.rows.push(item); });
        return ok({ removed: 1 });
      }
      case "audio.peaks":
        return ok({ project_id: p.project_id, peaks: PEAKS, buckets: PEAKS.length,
                    duration_ms: 213000, cached: true });
      case "style.list":
        return ok(STYLES.map(function (row) { return styleRow(row.config, row.builtin); }));
      case "style.get": {
        var hit = styleFind(p.id || "default");
        if (!hit) return fail("NOT_FOUND", "样式不存在", p.id);
        var canvas = hit.config.preview.orientation === "portrait"
          ? { width: 720, height: 1280 } : { width: 1280, height: 720 };
        return ok({ style: JSON.parse(JSON.stringify(hit.config)), builtin: !!hit.builtin,
                    warnings: [], file: "C:\mock\styles\\" + hit.id + ".json", canvas: canvas });
      }
      case "style.save": {
        if (p.id && (styleFind(p.id) || {}).builtin) return fail("CONFLICT", "内置样式不可修改，请先复制一份");
        if (!p.config) return fail("VALIDATION", "缺少样式内容");
        var conf = JSON.parse(JSON.stringify(p.config));
        var id = p.id || "sty_mock_" + (STYLES.length + 1);
        conf.id = id;
        conf.name = p.name || conf.name || "新样式";
        var size = Number((conf.primary || {}).size || 48);
        if (size < 12 || size > 120) return fail("VALIDATION", "主字幕字号要在 12–120");
        var existing = styleFind(id);
        if (existing) {
          existing.config = conf;
          existing.name = conf.name;
          existing.mode = conf.mode;
          existing.size = size;
          existing.font = conf.primary.font;
        } else {
          STYLES.push({ id: id, name: conf.name, mode: conf.mode, builtin: false,
                        size: size, font: conf.primary.font, layout: conf.layout, config: conf });
        }
        return ok({ style: conf, builtin: false, warnings: [],
                    file: "C:\mock\styles\\" + id + ".json",
                    row: { id: id, name: conf.name, mode: conf.mode } });
      }
      case "style.delete": {
        var target = styleFind(p.id);
        if (!target) return fail("NOT_FOUND", "样式不存在");
        if (target.builtin) return fail("CONFLICT", "内置样式不可删除");
        STYLES = STYLES.filter(function (row) { return row.id !== target.id; });
        return ok({ id: target.id, removed: true });
      }
      case "style.import": {
        // mock 里没有文件选择器：造一个与浏览器里"导入 JSON"等价的副本
        var imported = preset("sty_mock_import", "导入的样式", "ass", 56);
        STYLES.push({ id: imported.id, name: imported.name, mode: imported.mode,
                      builtin: false, size: 56, font: imported.primary.font,
                      layout: imported.layout, config: imported });
        return ok({ style: imported, builtin: false, warnings: [], file: "C:\mock\styles\\" + imported.id + ".json" });
      }
      case "style.export":
        return ok({ path: "C:\mock\styles\\" + (p.id || "default") + ".json", bytes: 812,
                    builtin: true, style: (styleFind(p.id || "default") || {}).config });
      case "style.fonts":
        return ok({ fonts: ["Microsoft YaHei", "SimHei", "Microsoft JhengHei", "Consolas",
                            "Source Han Sans SC", "Segoe UI"], count: 6, note: "" });
      case "style.render":
        // mock 里没有真 PNG 可读：给空 path + 一句说明。
        // 之前瞎给一个 C:\mock\... 路径，界面就挂出破图（只显示 alt 文字），
        // 看截图的人会以为"真实渲染坏了"。
        return ok({ path: "", width: 1280, height: 720,
                    style_id: (p.id || "default"), warnings: [], ass: "",
                    bytes: 48120,
                    note: "预览示意：mock 模式下真实渲染不产图，真机由 FFmpeg 出 PNG" });
      case "job.list":
        return ok(JOBS);
      case "job.get":
        if (p.id === FOCUS_JOB.id) {
          FOCUS_JOB.status = "succeeded";
          FOCUS_JOB.progress = 1;
          FOCUS_JOB.result = {
            project_id: p.project_id || "p_demo_1",
            subjects: FOCUS_SUBJECTS[p_demo_1] || [],
            subject: FOCUS_SUBJECT[p_demo_1] || "",
            cuts: 1,
            enhance: true
          };
          return ok(FOCUS_JOB);
        }
        if (p.id === NLE_DELIVER_JOB.id) {
          NLE_DELIVER_JOB.status = "succeeded";
          NLE_DELIVER_JOB.progress = 1;
          NLE_DELIVER_JOB.result = MOCK_DELIVER_RESULT(
            MOCK_DELIVER.channel,
            MOCK_DELIVER.place_in_app
          );
          return ok(NLE_DELIVER_JOB);
        }
        if (p.id === OPT_JOB.id) return ok(OPT_JOB);
        if (p.id === TRANS_JOB.id) return ok(TRANS_JOB);
        if (p.id === EXPO_JOB.id) return ok(EXPO_JOB);
        if (p.id === SUM_JOB.id) return ok(SUM_JOB);
        if (p.id === OV_JOB.id) return ok(OV_JOB);
        if (p.id === CLIP_JOB.id) return ok(CLIP_JOB);
        if (p.id === COPY_JOB.id) {
          if (MOCK_PENDING_COPY) {
            var pend = clipFind(MOCK_PENDING_COPY);
            if (pend) {
              pend.row.copy = JSON.parse(JSON.stringify(COPY_SEED));
            }
            MOCK_PENDING_COPY = "";
          }
          COPY_JOB.status = "succeeded";
          COPY_JOB.progress = 1;
          return ok(COPY_JOB);
        }
        if (MERGE_JOB && p.id === MERGE_JOB.id) return ok(MERGE_JOB);
        if (EXPORT_JOBS[p.id]) return ok(EXPORT_JOBS[p.id]);
        for (var j = 0; j < JOBS.length; j++) {
          if (JOBS[j].id === p.id) return ok(JOBS[j]);
        }
        return fail("NOT_FOUND", "任务不存在");
      case "job.cancel":
      case "job.retry":
        // 导出任务不在 JOBS 里（它们记在 EXPORT_JOBS + EXPORTS），重试要单独收
        if (EXPORT_JOBS[p.id]) {
          var retryJob = EXPORT_JOBS[p.id];
          retryJob.status = "succeeded";
          retryJob.progress = 1;
          retryJob.stage = "已完成";
          retryJob.error_code = "";
          retryJob.error_message = "";
          retryJob.result = retryJob.result || {};
          retryJob.result.path = retryJob.result.path || MOCKEXPDIR + "重试成功.mp4";
          retryJob.result.bytes = retryJob.result.bytes || 2_400_000;
          for (var r = 0; r < EXPORTS.length; r += 1) {
            if (EXPORTS[r].job_id !== p.id) continue;
            EXPORTS[r].status = "succeeded";
            EXPORTS[r].error = "";
            EXPORTS[r].path = retryJob.result.path;
            EXPORTS[r].name = retryJob.result.path.split("\\\\").pop();
            EXPORTS[r].bytes = retryJob.result.bytes;
            EXPORTS[r].exists = true;
          }
          return ok(retryJob);
        }
        for (var k = 0; k < JOBS.length; k++) {
          if (JOBS[k].id === p.id) {
            JOBS[k].status = method === "job.cancel" ? "canceled" : "queued";
            JOBS[k].progress = 0;
            return ok(JOBS[k]);
          }
        }
        return fail("NOT_FOUND", "任务不存在");
      // 码表只抄常用 8 项 + 3 个长尾：前端从不依赖全集（真源是 infra/languages.py），
      // 这里只负责让 mock 模式能真的把选择弹窗打开、选出值。
      case "transcribe.languages": {
        var langCommon = [
          { code: "", label: "自动检测" },
          { code: "zh", label: "中文" },
          { code: "en", label: "英语" },
          { code: "ja", label: "日语" },
          { code: "yue", label: "粤语" },
          { code: "ko", label: "韩语" },
          { code: "ru", label: "俄语" },
          { code: "de", label: "德语" }
        ];
        var langAll = [
          { code: "fr", label: "法语" },
          { code: "es", label: "西班牙语" },
          { code: "pt", label: "葡萄牙语" }
        ];
        return ok({ common: langCommon, all: langAll, current: "", count: 100 });
      }
      case "transcribe.start": {
        // 真后端会把 language 归一化后写进 job.payload；mock 里也挂在 job 上，
        // 前端测试才能断言"用户选的确实是这个码"，而不是只看弹窗关没关。
        var picked = String((p && p.language) || "");
        var madeJobs = [];
        ((p && p.project_ids) || []).forEach(function (pid) {
          var trJob = {
            id: "j_mock_tr_" + (JOBS.length + 1),
            kind: "transcribe",
            type: "transcribe",
            status: "queued",
            progress: 0,
            stage: picked ? "排队中 · " + picked : "排队中 · 自动检测",
            language: picked,
            project_id: pid,
            created_at: "2026-11-02 21:00:00",
            updated_at: "2026-11-02 21:00:00",
            attempts: 0,
            error: null
          };
          JOBS.unshift(trJob);
          madeJobs.push(trJob);
        });
        return ok({ jobs: madeJobs, count: madeJobs.length, skipped: [] });
      }
      case "job.demo.submit":
        return ok({ id: "j_mock_demo", kind: "dev.demo", status: "queued", progress: 0, stage: "排队中" });
      case "fs.reveal":
        return ok({ path: p.path });
      case "fs.pick":
        return ok({ path: "C:\\Users\\demo\\AppData\\Roaming\\VideoWorkbench\\media" });
      default:
        return fail("NOT_FOUND", "未知接口：" + method);
    }
  }

  function listProjects(query, limit, offset) {
    var q = String(query || "").trim().toLowerCase();
    var rows = PROJECTS.filter(function (row) {
      if (!q) return true;
      return (
        String(row.title).toLowerCase().indexOf(q) >= 0 ||
        String(row.up_name).toLowerCase().indexOf(q) >= 0 ||
        String(row.bvid).toLowerCase().indexOf(q) >= 0
      );
    });
    return {
      items: rows.slice(offset, offset + limit),
      total: rows.length,
      limit: limit,
      offset: offset
    };
  }

  // ------------------------------------------------------------ 字幕样式（P3-07）
  //
  // 结构与后端一致（primary/secondary/rounded/preview）。**不要**只给 {id,name}：
  // 样式页会照着 config 建表单，字段缺一个就渲染出一个空格子，看起来像 bug。
  /** 读 tokens.css 的变量：mock 里的样式样例色不写死（check_colors 会拦）。 */
  function capToken(name) {
    try {
      var value = window.getComputedStyle(document.documentElement).getPropertyValue(name);
      return (value || "").trim();
    } catch (error) {
      return "";
    }
  }

  function face(size) {
    return {
      font: "Microsoft YaHei", size: size, spacing: 0,
      color: capToken("--cap-fg"), outline_color: capToken("--cap-stroke"),
      outline_width: 2, bold: false
    };
  }

  function preset(id, name, mode, size) {
    return {
      id: id, name: name, mode: mode, layout: "original_top",
      primary: face(size),
      secondary: face(Math.max(12, size - 12)),
      rounded: {
        font: "Microsoft YaHei", size: 40, text_color: capToken("--cap-fg"),
        bg_color: capToken("--cap-bg"),
        bg_alpha: 0.6, corner_radius: 8, padding_h: 16, padding_v: 8,
        margin_bottom: 120, line_spacing: 4, letter_spacing: 0,
        primary_box: true, secondary_box: true
      },
      preview: { text_length: "medium", orientation: "landscape", background: "dark" }
    };
  }

  var STYLES = [
    { id: "default", name: "默认", mode: "ass", builtin: true, size: 48,
      font: "Microsoft YaHei", layout: "original_top", config: preset("default", "默认", "ass", 48) },
    { id: "rounded-default", name: "圆角默认", mode: "rounded", builtin: true, size: 40,
      font: "Microsoft YaHei", layout: "original_top",
      config: preset("rounded-default", "圆角默认", "rounded", 48) }
  ];

  function styleFind(id) {
    for (var i = 0; i < STYLES.length; i += 1) {
      if (STYLES[i].id === id || STYLES[i].name === id) return STYLES[i];
    }
    return null;
  }

  function styleRow(config, builtin) {
    return {
      id: config.id, name: config.name, mode: config.mode, layout: config.layout,
      builtin: !!builtin, size: config.primary.size, font: config.primary.font,
      config: JSON.parse(JSON.stringify(config))
    };
  }

  /** 深合并：设置页提交的永远是"改了的叶子"，用浅合并会把 llm.tasks 整块打掉。 */
  function merge(target, patch) {
    Object.keys(patch).forEach(function (key) {
      var value = patch[key];
      if (value && typeof value === "object" && !Array.isArray(value) && target[key] && typeof target[key] === "object") {
        merge(target[key], value);
      } else {
        target[key] = value;
      }
    });
    return target;
  }

  WB.mock = {
    call: call,
    settings: function () {
      return clone(SETTINGS);
    },
    /** mock 下没有真事件，切皮肤这类"服务端广播"由这里补一条，界面路径才一致。 */
    emit: function (name, payload) {
      if (window.__wbEvent) window.__wbEvent(name, JSON.stringify(payload || {}));
    }
  };
})();
