"""独立复核发布包：不信 package.py 的自检，自己开包扫一遍。

    python scripts/verify-package.py [dist/bili-scribe-X.Y.Z.zip]

为什么要有这个脚本：`package.py` 结尾会自检一次，但**它是打包者自己的判据**。
"我这边的检查通过了"和"这个包干净"是两件事，所以这里用另一套判据再验一遍：
换一种扫描方式（逐条目解码搜文本）、换一批模式（本机路径、真实视频号）、
外加校验包内版本号与必备文件。

而且它是**可执行的**：之前这套检查写在文档里、靠人记得做，那等于没有——
文档里的注意事项不会自己执行，脚本会在发版前拦住你。

任何一项不过就退出码非 0，可以直接当发版闸门用。
"""

from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 敏感文件名：出现即失败。config 里可能有 api_key，库里是凭据，key.bin 是解密密钥。
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(config\.ya?ml(\..*)?|.*\.bak|.*\.tmp|.*\.db(-\w+)?|key\.bin)(/|$)"
    r"|\.venv/|__pycache__/|\.git/|\.pyc$"
)

# 内容模式：逐条目解码后搜。前四个是凭据，后两个是"不该外泄的本机/使用者信息"。
CONTENT_PATTERNS = {
    "SESSDATA 有值": r"SESSDATA\s*[:=]\s*[^\s'\"]",
    "api_key 有值": r"api_key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}",
    "GitHub 令牌": r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "OpenAI 风格密钥": r"\bsk-[A-Za-z0-9]{20,}",
    # ⚠️ 这条第一版写错了：正则里 `\\\\` 表示"两个反斜杠"，而真实的 Windows 路径只有
    #    一个，于是它永远匹配不上——检查显示"0 处 ✓"其实什么也没查。是"故意造一个脏包
    #    喂给它"那一步把它试出来的。所以这里用 `\\+`：一个或两个都认。
    #    另外：**注释里也别写出真实路径的样子**，否则这条检查会命中自己（已经命中过一次，
    #    而那次它是对的——把本机路径写进随包发布的文件里，本身就是轻微泄露）。
    "本机路径": r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.-]+",
    "bvid": r"BV[0-9A-Za-z]{10}",
}

# 允许出现在包里的 bvid：只有文档与示例里的占位号。
ALLOWED_BVIDS = {"BV1xx411c7mD"}
# 已知的假密钥（测试夹具），不算泄露。
ALLOWED_SECRETS = {"sk-test-abcdef123456"}


def verify(zip_path: Path) -> bool:
    if not zip_path.exists():
        print(f"找不到包：{zip_path}")
        return False

    z = zipfile.ZipFile(zip_path)
    names = z.namelist()
    top = sorted({n.split("/")[0] for n in names})
    print(f"包：{zip_path.name}  {zip_path.stat().st_size:,} 字节，{len(names)} 个条目")
    print(f"顶层目录：{top}")
    ok = True

    bad = [n for n in names if FORBIDDEN_NAMES.search(n)]
    print(f"① 敏感文件名：{bad or '无 ✓'}")
    ok &= not bad

    hits: dict[str, list[str]] = {k: [] for k in CONTENT_PATTERNS}
    bvids: dict[str, set[str]] = {}
    for n in names:
        if n.endswith("/"):
            continue
        text = z.read(n).decode("utf-8", "ignore")
        for label, pat in CONTENT_PATTERNS.items():
            for m in re.finditer(pat, text):
                found = m.group(0)
                if label == "api_key 有值" and any(s in found for s in ALLOWED_SECRETS):
                    continue
                if label == "bvid":
                    bvids.setdefault(found, set()).add(n)
                    continue
                hits[label].append(f"{n}: …{text[max(0, m.start()-20):m.end()+10]}…")

    for label, found in hits.items():
        if label == "bvid":
            continue  # bvid 单独在 ③ 里报（要区分"允许的占位号"和"真实视频号"）
        print(f"② [{label}] {len(found)} 处{'  ← 有问题！' if found else ' ✓'}")
        for line in found[:3]:
            print("     ", line.replace("\n", " ")[:100])
        ok &= not found

    unexpected = {b: f for b, f in bvids.items() if b not in ALLOWED_BVIDS}
    print(f"③ bvid：{sorted(bvids) or '无'}"
          f"{'  ← 有真实视频号！' if unexpected else ' ✓（只剩文档占位号）'}")
    ok &= not unexpected

    init = next((n for n in names if n.endswith("/bili_scribe/__init__.py")), None)
    if init is None:
        print("④ 包内找不到 bili_scribe/__init__.py  ← 有问题！")
        return False
    packed = re.search(r'__version__ = "([^"]+)"', z.read(init).decode("utf-8"))
    expected = re.search(
        r'__version__ = "([^"]+)"',
        (ROOT / "bili_scribe" / "__init__.py").read_text(encoding="utf-8"),
    )
    packed_v = packed.group(1) if packed else "?"
    expect_v = expected.group(1) if expected else "?"
    same = packed_v == expect_v
    print(f"④ 包内版本 {packed_v} / 工作区版本 {expect_v}：{'一致 ✓' if same else '不一致 ← 有问题！'}")
    ok &= same

    prefix = top[0] if len(top) == 1 else ""
    required = [
        f"{prefix}/bili_scribe/core/roles.py",
        f"{prefix}/bili_scribe/core/srt.py",
        f"{prefix}/bili_scribe/infra/uploads.py",
        f"{prefix}/README.md",
        f"{prefix}/requirements.txt",
        f"{prefix}/DISCLAIMER.md",
    ]
    missing = [p for p in required if p not in names]
    print(f"⑤ 必备文件：{'齐 ✓' if not missing else '缺 ' + str(missing)}")
    ok &= not missing

    if ok and prefix:
        roles = z.read(f"{prefix}/bili_scribe/core/roles.py").decode("utf-8")
        n_roles = roles.count("= Role(")
        print(f"⑥ 角色定义数：{n_roles} 个")

    print(f"\nsha256 = {hashlib.sha256(zip_path.read_bytes()).hexdigest()}")
    print(f"大小   = {zip_path.stat().st_size}")
    print("结论：" + ("干净 ✓（可以发）" if ok else "有问题 ✗（不要发）"))
    return ok


def main() -> int:
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        dist = ROOT / "dist"
        cands = sorted(dist.glob("bili-scribe-*.zip"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print("dist/ 里没有 bili-scribe-*.zip")
            return 1
        target = cands[-1]
    return 0 if verify(target) else 1


if __name__ == "__main__":
    raise SystemExit(main())
