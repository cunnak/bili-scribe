"""ASR 模型的注册表、状态与下载（P2-01）。

**为什么模型不打包。** 方案书 §0 已经定了：`large-v3-turbo` 1.6GB，打进安装包等于让
每个只想剪 30 秒短视频的人都先下载 1.6GB。模型放在用户目录，第一次转录时再要。

**为什么状态判定看文件而不是看目录。** 下载中断会留下半截目录——目录存在但
``model.bin`` 只有 300MB，这种"看起来下过"的状态是最坑人的：用户点了转录，
faster-whisper 报一个 `Unable to open file` 之类的底层错误。这里要求必需文件
齐全才叫已下载。

**为什么下载要能取消且要报进度。** 1.6GB 在没有代理的机器上可能要十几分钟，
不能只给一个转圈。进度是轮询目录体积算出来的（huggingface_hub 的快照下载
不回调字节数），够用且不依赖库的内部实现。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..app.errors import WBError

# 少一个都不算下完。tokenizer.json 在小模型上可能不存在（tiny/base 用的是
# vocabulary.json），所以放在"至少有一个"的那组里单独判。
REQUIRED_ALWAYS = ("config.json", "model.bin")
REQUIRED_ANY = ("tokenizer.json", "vocabulary.json")

MB = 1024 * 1024


@dataclass(frozen=True)
class ModelSpec:
    name: str
    label: str
    repo: str
    approx_bytes: int
    note: str = ""

    @property
    def approx_label(self) -> str:
        if self.approx_bytes >= 1024 * MB:
            return f"{self.approx_bytes / (1024 * MB):.1f} GB"
        return f"{self.approx_bytes / MB:.0f} MB"


# 仓库 id 与体积按 CTranslate2 的 int8/fp16 转换版量级估的；体积只用于设置页
# 提前告诉用户"要下多大"，不参与任何判定。
SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "tiny", "tiny · 最快，准确率低", "Systran/faster-whisper-tiny", 75 * MB, "只适合试跑"
    ),
    ModelSpec("base", "base · 快", "Systran/faster-whisper-base", 145 * MB, ""),
    ModelSpec("small", "small · 均衡", "Systran/faster-whisper-small", 484 * MB, ""),
    ModelSpec("medium", "medium · 较准，较慢", "Systran/faster-whisper-medium", 1500 * MB, ""),
    ModelSpec(
        "large-v3", "large-v3 · 最准", "Systran/faster-whisper-large-v3", 3100 * MB, "CPU 上很慢"
    ),
    ModelSpec(
        "large-v3-turbo",
        "large-v3-turbo · 默认",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        1600 * MB,
        "准确率接近 large-v3，速度快数倍",
    ),
)

DEFAULT_MODEL = "large-v3-turbo"

_BY_NAME = {spec.name: spec for spec in SPECS}
_ALIASES = {
    # 用户可能在配置里写 faster-whisper 的别名
    "large-v3-turbo-ct2": "large-v3-turbo",
    "turbo": "large-v3-turbo",
    "large": "large-v3",
    "large-v2": "large-v3",
}


def spec(name: str) -> ModelSpec:
    key = str(name or "").strip().lower()
    key = _ALIASES.get(key, key)
    found = _BY_NAME.get(key)
    if found is None:
        raise WBError(
            "UNSUPPORTED",
            f"不认识的语音模型：{name}",
            "可选：" + "、".join(s.name for s in SPECS),
        )
    return found


def model_dir(models_root: str | os.PathLike[str], name: str) -> Path:
    return Path(models_root) / "faster-whisper" / spec(name).name


def _files_ok(path: Path) -> bool:
    if not path.is_dir():
        return False
    for required in REQUIRED_ALWAYS:
        target = path / required
        # model.bin 小于 20MB 一律当作下坏了的残留：最小的 tiny 也有 75MB。
        if not target.is_file() or (required == "model.bin" and target.stat().st_size < 20 * MB):
            return False
    return any((path / n).is_file() for n in REQUIRED_ANY)


def dir_size(path: str | os.PathLike[str]) -> int:
    total = 0
    root = Path(path)
    if not root.is_dir():
        return 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                continue
    return total


def is_downloaded(models_root: str | os.PathLike[str], name: str) -> bool:
    return _files_ok(model_dir(models_root, name))


def status(models_root: str | os.PathLike[str], name: str) -> dict[str, Any]:
    found = spec(name)
    path = model_dir(models_root, found.name)
    on_disk = dir_size(path)
    return {
        "name": found.name,
        "label": found.label,
        "note": found.note,
        "approx_bytes": found.approx_bytes,
        "approx_label": found.approx_label,
        "downloaded": _files_ok(path),
        "bytes_on_disk": on_disk,
        "path": str(path),
    }


def list_status(models_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return [status(models_root, s.name) for s in SPECS]


def remove(models_root: str | os.PathLike[str], name: str) -> dict[str, Any]:
    path = model_dir(models_root, name)
    if not path.exists():
        return {"name": spec(name).name, "removed": False, "bytes_freed": 0}
    freed = dir_size(path)
    shutil.rmtree(path, ignore_errors=True)
    # 上层目录空了就一起收掉，别在用户目录里留一串空壳
    parent = path.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return {"name": spec(name).name, "removed": True, "bytes_freed": freed}


Progress = Callable[[float, str], None]
Cancel = Callable[[], bool]


def _hf_endpoint() -> str:
    """国内直连 huggingface.co 基本必失败，但**不覆盖**用户显式设置。"""
    return os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com"


def _snapshot_download(repo: str, target: Path, timeout: float | None) -> None:
    from huggingface_hub import snapshot_download  # 延迟导入：没装时状态页还能用

    snapshot_download(
        repo_id=repo,
        local_dir=str(target),
        allow_patterns=[
            "config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.*",
            "preprocessor_config.json",
        ],
        max_workers=4,
        # 断点续传：huggingface_hub 默认就会绕过已完成的文件
        local_dir_use_symlinks=False,
    )


def download(
    models_root: str | os.PathLike[str],
    name: str,
    *,
    progress: Progress | None = None,
    cancel: Cancel | None = None,
    poll_seconds: float = 0.5,
    timeout: float | None = None,
) -> dict[str, Any]:
    """下载一个模型。同步阻塞，调用方负责放到 worker 线程里。

    返回 ``{"name", "downloaded", "bytes_on_disk", "path"}``；取消时抛 ``CANCELED``。
    """
    found = spec(name)
    target = model_dir(models_root, found.name)
    target.mkdir(parents=True, exist_ok=True)

    if _files_ok(target):
        if progress:
            progress(1.0, "已下载")
        return {
            "name": found.name,
            "downloaded": True,
            "bytes_on_disk": dir_size(target),
            "path": str(target),
            "cached": True,
        }

    if os.environ.get("HF_ENDPOINT") is None:
        os.environ["HF_ENDPOINT"] = _hf_endpoint()

    error: list[BaseException] = []
    done = threading.Event()

    def work() -> None:
        try:
            _snapshot_download(found.repo, target, timeout)
        except BaseException as exc:  # noqa: BLE001 —— 线程里必须兜住，否则异常静默丢失
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=work, name=f"model-dl-{found.name}", daemon=True)
    thread.start()

    started = time.time()
    while not done.wait(poll_seconds):
        on_disk = dir_size(target)
        ratio = min(0.99, on_disk / max(1, found.approx_bytes))
        if progress:
            progress(round(ratio, 3), f"下载中 {on_disk / MB:.0f}MB / 约 {found.approx_label}")
        if cancel and cancel():
            # 线程没法安全中断，只能不等它；残留文件下次下载会续传，不会白费。
            raise WBError("CANCELED", "已取消模型下载")
        if timeout and time.time() - started > timeout:
            raise WBError("NETWORK", "模型下载超时", f"已等待 {int(time.time() - started)} 秒")

    if error:
        exc = error[0]
        raise WBError(
            "NETWORK",
            f"模型下载失败：{type(exc).__name__}",
            f"{exc}\n可手动把模型放到：{target}\n或设置 HF_ENDPOINT 换镜像",
        )

    if not _files_ok(target):
        raise WBError(
            "NETWORK", "模型下载不完整", f"缺 {REQUIRED_ALWAYS} 或 {REQUIRED_ANY}，目录：{target}"
        )

    if progress:
        progress(1.0, "下载完成")
    return {
        "name": found.name,
        "downloaded": True,
        "bytes_on_disk": dir_size(target),
        "path": str(target),
        "cached": False,
    }


def resolve_for_use(models_root: str | os.PathLike[str], name: str) -> dict[str, Any]:
    """转录前调用：返回实际要交给 faster-whisper 的路径或仓库名。

    本地没有就报 ``DEPENDENCY_MISSING``，而不是让 faster-whisper 自己去连网——
    它的报错信息在离网机器上是超时，用户完全看不出"其实是模型没下"。
    """
    found = spec(name)
    path = model_dir(models_root, found.name)
    if _files_ok(path):
        return {"model": found.name, "path": str(path), "source": "local"}
    raise WBError(
        "DEPENDENCY_MISSING",
        f"语音模型 {found.name} 还没下载（约 {found.approx_label}）",
        f"到设置页点下载，或手动放到：{path}",
    )
