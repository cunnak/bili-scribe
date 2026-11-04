"""对账档位 fixture 的**唯一**去处（task-9）。

`WB_SUMMARY_PARITY_STRICT` 在 task-8 之后**默认就是严格档**，未设＝严格，
只有 `0` / `false` / `off` 才是宽松档。以前散落在各文件里写死的
`os.environ` / `"WB_SUMMARY_PARITY_STRICT"` 字符串，就是这次 16 条红的根源：
产品把默认翻过来，测试却各自记着「不设就是宽松」。所以档位语义只在这里定义一次，
用例只管选档，不再自己拼字符串。

三个档位（fixture 名即用参名）：

* `default_env` —— **未设这个变量**。这是用户的常态，也是**严格档**：加固关掉、
  坏 JSON 一次就失败、`max_tokens` 按配置原样。想钉「用户实际拿到什么」就用它。
* `strict_env`  —— 显式 `=1`。与未设同档；它单独存在的价值是钉住
  *任何非 OFF 值都算严格*（`1`/`true`/`2`/拼错值都算），以及给「同档两条路必须一致」
  的对账留一个显式锚点。
* `loose_env`   —— 显式 `=0`。WB 的三处加固（①时间线补密、②输出预算抬到
  12288/翻倍/封顶 16384、③坏 JSON 的修复→续写→拆分→重问阶梯）**只在这一档生效**。

**选错档的两种失败都要防**：把加固用例留在默认档，它会红（产品没错，用例站错边）；
更糟的是把「默认档什么都不做」写成永远绿的断言——那条看起来在守复刻，其实一个字节都没守。
所以每条用了这三个 fixture 的用例都在 docstring 里写明自己守哪一档、为什么。
"""

from __future__ import annotations

import importlib

import pytest


def adapter_module():
    """适配层模块本体（档位开关、预算、阶梯、①都在这里）。"""
    return importlib.import_module("video_workbench.core.summarizer")


def env_name() -> str:
    """开关名从产品侧读，不在测试里硬编码字符串——产品改名时这里跟着改一次就行。"""
    return str(getattr(adapter_module(), "PARITY_ENV", "WB_SUMMARY_PARITY_STRICT"))


def off_values() -> frozenset:
    """产品认可的「关掉」写法集合；测试拿它反向核对，不自己抄一份。"""
    return frozenset(getattr(adapter_module(), "PARITY_OFF_VALUES", frozenset()))


def is_strict() -> bool:
    return bool(adapter_module()._parity_strict())


@pytest.fixture
def default_env(monkeypatch):
    """未设＝**严格档（用户常态）**。

    必须显式 delenv，不能「什么都不做」：`tests/wb/conftest.py` 的 `_isolate_env`
    清了 WB_LLM_API_KEY 之类，却没清这个变量。从设过它的 shell 里跑测试时，
    「什么都不做」会让本该钉默认档的用例静默跑到宽松档上、断言照样通过。
    """
    monkeypatch.delenv(env_name(), raising=False)
    yield


@pytest.fixture
def strict_env(monkeypatch):
    """显式 `=1` 的严格档，与 `default_env` 同档（等价性由档位取值表那条用例守）。"""
    monkeypatch.setenv(env_name(), "1")
    yield


@pytest.fixture
def loose_env(monkeypatch):
    """宽松档：WB 的三处加固在这一档生效，加固类用例钉这里。"""
    monkeypatch.setenv(env_name(), "0")
    yield
