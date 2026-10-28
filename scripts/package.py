"""打一个干净的发布包。

    python scripts/package.py

**包的内容以 `git ls-files` 为准**，不是遍历整个目录。原因很实际：这个仓库的
工作区里长期堆着调试脚本、抓下来的截图、跑测试留下的 json/txt——它们只对当下
这一次调试有意义，却会被 `rglob` 一起打进包里。v0.7.0 第一次就是这么打出来的：
386 个文件、57 MB，而上一版是 86 个文件、1.2 MB。发布包里混着调试中间产物，
对方解压后看到一堆 `poll_task.py`、`sub_body.txt`，只会觉得这东西没收拾干净。

排除两类东西，理由不同：

- **机器相关**：`.venv/`、`__pycache__/`、`dist/`、`build/`。它们只对生成它们的
  那台机器有意义，打进去只会把包撑大。
- **个人凭据**：`config.yaml`（里面有 api_key）、`*.db`、`key.bin`。这几样泄露出去
  就是直接泄露账号和模型额度。

打包结束后会重新打开压缩包自检一遍，确认敏感文件确实不在里面。宁可在这里报错，
也不要把一个装着凭据的包发出去。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".pytest_cache",
    "node_modules",
}

EXCLUDE_FILES = {
    "config.yaml",
    "config.yml",
    # 设置页保存时会把原配置备份成这个，里面可能有真实 api_key
    "config.yaml.bak",
    "config.yaml.tmp",
    "key.bin",
    "uvicorn.log",
}

EXCLUDE_SUFFIXES = {
    ".db",
    ".db-wal",
    ".db-shm",
    ".pyc",
    ".pyo",
    ".spec",
    ".zip",
    ".log",
    ".lnk",
    ".bak",
    ".tmp",
    ".orig",
}

# 打包后必须确认不在包里的文件名/后缀
FORBIDDEN_NAMES = {"config.yaml", "config.yaml.bak", "config.yml", "key.bin"}
FORBIDDEN_SUFFIXES = {".db", ".db-wal", ".db-shm", ".bin", ".bak", ".tmp"}


def should_skip(rel: Path) -> bool:
    if set(rel.parts) & EXCLUDE_DIRS:
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def tracked_files() -> list[Path]:
    """仓库里被 git 跟踪的文件，这才是发布包的内容边界。

    用 `git ls-files -z` 而不是 `rglob`：未跟踪的东西（调试脚本、抓图、临时 json）
    属于工作区噪声，不该出现在发给别人的包里。`-z` 是为了中文文件名不被转义。

    不在 git 仓库里跑时（比如对方拿到的 zip 解压后想重打一个包）直接报错，
    而不是悄悄退化成遍历目录——那正是打出一个 57 MB 脏包的路径。
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            "打包中止：这个目录不是 git 仓库（或 git 不可用），无法确定该打哪些文件。\n"
            f"  {exc}\n"
            "  发布包的内容以 git 跟踪清单为准；脱离仓库时请先把要发的东西整理成仓库。"
        ) from exc
    names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    return [ROOT / n for n in names]


def build(version: str, out_dir: Path) -> tuple[Path, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"bili-scribe-{version}.zip"
    prefix = f"bili-scribe-{version}"

    count = 0
    skipped: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(tracked_files()):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if should_skip(rel):
                skipped.append(rel.as_posix())
                continue
            zf.write(path, f"{prefix}/{rel.as_posix()}")
            count += 1

    # 自检：凭据类文件绝不能出现在包里
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    leaked = [
        n
        for n in names
        if Path(n).name in FORBIDDEN_NAMES or Path(n).suffix.lower() in FORBIDDEN_SUFFIXES
    ]
    if leaked:
        target.unlink(missing_ok=True)
        raise SystemExit(
            "打包中止：压缩包里出现了不该有的文件——\n  "
            + "\n  ".join(leaked)
            + "\n请检查 EXCLUDE 规则后再试。"
        )

    return target, count


def main() -> int:
    from bili_scribe import __version__

    parser = argparse.ArgumentParser(description="打一个干净的 bili-scribe 发布包")
    parser.add_argument("--version", default=__version__, help=f"版本号（默认 {__version__}）")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    args = parser.parse_args()

    target, count = build(args.version, Path(args.out))
    size_kb = target.stat().st_size / 1024

    print(f"已生成 {target}")
    print(f"  文件数 {count}，大小 {size_kb:.1f} KB")
    print("  已排除：.venv/ __pycache__/ config.yaml *.db key.bin dist/ .git/")
    print("  自检通过：包内不含任何凭据或数据库文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
