"""源语言代码表。单一真源。

**为什么需要这个模块。**
faster-whisper 只认 OpenAI Whisper 那 100 个裸码（全小写，``haw`` / ``yue``
是三字母）。任何别的写法——``zh-CN``、``ZH``、``中文``、带空格的 ``" auto "``,
甚至空字符串——都会在 ``faster_whisper.tokenizer.Tokenizer`` 的构造函数里抛
``ValueError``。

那个校验发生得太晚：父进程先跑完 VAD、把模型加载进显存，worker 才开始建
tokenizer。也就是说用户敲错一个字母，代价是整台机器白跑一次（CPU 上
large-v3-turbo 按分钟计）。所以归一化和拒绝都必须发生在 ``jobs.submit`` 之前。

**为什么不用 faster_whisper 现成的常量。**
``faster_whisper.tokenizer._LANGUAGE_CODES`` 是下划线开头的私有名，而且
``app/service.py`` 有意不在启动路径上 import faster_whisper（用 ``find_spec``
探测依赖就是想保持这个边界）。这里复制一份，用测试断言两边集合相等——
上游加语言我们会红，而不是静默放行一个后端不认的码。
"""

from __future__ import annotations

from typing import Any

from ..app.errors import WBError

#: ``faster_whisper.tokenizer._LANGUAGE_CODES`` 的镜像，100 个，全小写。
#: 改这里必须同时改 ``tests/wb/test_languages.py`` 的对账用例。
CODES: frozenset[str] = frozenset(
    """
    af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr
    gl gu ha haw he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv
    mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn
    so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo yue zh
    """.split()
)

#: 中文显示名。缺项时前端回退显示 code 本身，不会因为漏翻译而空白。
NAMES: dict[str, str] = {
    "af": "南非荷兰语", "am": "阿姆哈拉语", "ar": "阿拉伯语", "as": "阿萨姆语",
    "az": "阿塞拜疆语", "ba": "巴什基尔语", "be": "白俄罗斯语", "bg": "保加利亚语",
    "bn": "孟加拉语", "bo": "藏语", "br": "布列塔尼语", "bs": "波斯尼亚语",
    "ca": "加泰罗尼亚语", "cs": "捷克语", "cy": "威尔士语", "da": "丹麦语",
    "de": "德语", "el": "希腊语", "en": "英语", "es": "西班牙语",
    "et": "爱沙尼亚语", "eu": "巴斯克语", "fa": "波斯语", "fi": "芬兰语",
    "fo": "法罗语", "fr": "法语", "gl": "加利西亚语", "gu": "古吉拉特语",
    "ha": "豪萨语", "haw": "夏威夷语", "he": "希伯来语", "hi": "印地语",
    "hr": "克罗地亚语", "ht": "海地克里奥尔语", "hu": "匈牙利语", "hy": "亚美尼亚语",
    "id": "印尼语", "is": "冰岛语", "it": "意大利语", "ja": "日语",
    "jw": "爪哇语", "ka": "格鲁吉亚语", "kk": "哈萨克语", "km": "高棉语",
    "kn": "卡纳达语", "ko": "韩语", "la": "拉丁语", "lb": "卢森堡语",
    "ln": "林加拉语", "lo": "老挝语", "lt": "立陶宛语", "lv": "拉脱维亚语",
    "mg": "马达加斯加语", "mi": "毛利语", "mk": "马其顿语", "ml": "马拉雅拉姆语",
    "mn": "蒙古语", "mr": "马拉地语", "ms": "马来语", "mt": "马耳他语",
    "my": "缅甸语", "ne": "尼泊尔语", "nl": "荷兰语", "nn": "新挪威语",
    "no": "挪威语", "oc": "奥克语", "pa": "旁遮普语", "pl": "波兰语",
    "ps": "普什图语", "pt": "葡萄牙语", "ro": "罗马尼亚语", "ru": "俄语",
    "sa": "梵语", "sd": "信德语", "si": "僧伽罗语", "sk": "斯洛伐克语",
    "sl": "斯洛文尼亚语", "sn": "修纳语", "so": "索马里语", "sq": "阿尔巴尼亚语",
    "sr": "塞尔维亚语", "su": "巽他语", "sv": "瑞典语", "sw": "斯瓦希里语",
    "ta": "泰米尔语", "te": "泰卢固语", "tg": "塔吉克语", "th": "泰语",
    "tk": "土库曼语", "tl": "他加禄语", "tr": "土耳其语", "tt": "鞑靼语",
    "uk": "乌克兰语", "ur": "乌尔都语", "uz": "乌兹别克语", "vi": "越南语",
    "yi": "意第绪语", "yo": "约鲁巴语", "yue": "粤语", "zh": "中文",
}

#: 弹窗与设置页的置顶项。``""`` 代表自动检测——它在 faster-whisper 侧就是
#: ``language=None``。放在最前面是因为它是"我不知道"的正当答，但不是默认答。
COMMON: tuple[str, ...] = ("", "zh", "en", "ja", "yue", "ko", "ru", "de")

_AUTO_ALIASES = frozenset({"", "auto", "自动", "自动检测", "-", "none", "null"})


def normalize(value: Any, *, field: str = "源语言") -> str:
    """把任意用户输入归一成 faster-whisper 认的裸码；``""`` 表示自动检测。

    容忍大小写与首尾空格（``" ZH "`` → ``"zh"``），拒绝一切不在 ``CODES``
    里的值。**抛 WBError 而不是返回原值**：原样透传的后果是在 tokenizer 里
    炸，那时已经付过 VAD 和模型加载的代价了。
    """
    text = str(value if value is not None else "").strip()
    lowered = text.lower()
    if lowered in _AUTO_ALIASES:
        return ""
    if lowered not in CODES:
        raise WBError(
            "VALIDATION",
            f"{field}不支持「{text}」。请从列表里选，例如 zh、en、ja。",
            "faster-whisper accepts 100 bare ISO codes (lowercase, e.g. zh/en/yue)",
        )
    return lowered


def label(code: str) -> str:
    """显示名。``""`` → 「自动检测」，未知码原样返回。"""
    if not code:
        return "自动检测"
    return NAMES.get(code, code)


def catalog() -> dict[str, list[dict[str, str]]]:
    """给前端的两段式选项：常用置顶 + 其余按字母序折叠。"""
    common = [{"code": c, "label": label(c)} for c in COMMON]
    rest = [
        {"code": c, "label": NAMES.get(c, c)}
        for c in sorted(CODES)
        if c not in COMMON
    ]
    return {"common": common, "all": rest}
