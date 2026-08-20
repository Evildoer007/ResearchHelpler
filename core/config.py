"""全局配置与运行环境处理。

关键约束（实测踩坑）：
本机常开着本地代理（如 Clash，127.0.0.1:7897）。akshare 的数据源
（东财 push2.eastmoney.com、新浪等）都是国内站点，经该代理转发到境外
出口时会 ProxyError 失败。因此所有取数操作前必须清掉代理环境变量。
打包成 exe 后用户若开着代理，同样会踩这个坑，故在此统一处理。
"""

from __future__ import annotations

import os
from contextlib import contextmanager

# akshare / 国内数据源默认直连，不走系统代理
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def no_proxy():
    """临时清空代理环境变量，退出时恢复。用于包裹 akshare 调用。"""
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        os.environ.pop("NO_PROXY", None)


# ---- 凭证加载：环境变量优先，其次本地 config.local.json（已被 .gitignore 忽略）----
import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CONFIG_PATH = _PROJECT_ROOT / "config.local.json"


def _load_local_config() -> dict:
    try:
        with open(_LOCAL_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_LOCAL = _load_local_config()


def _cred(key: str, default: str = "") -> str:
    """取凭证：环境变量优先，其次本地文件。"""
    return os.environ.get(key) or _LOCAL.get(key, default)


# ---- DeepSeek / LLM 配置 ----
DEEPSEEK_API_KEY = _cred("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# ⚠ 别写回 "deepseek-chat"：实测那是旧别名，会落到 **deepseek-v4-flash**（低档模型）。
# 项目此前一直在用 flash 跑，而 writer 那些"正文太薄、漏写逻辑"的毛病正出在这一环。
# 账号可用模型可随时复查：GET {BASE_URL}/models
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# ---- iFinD（同花顺 quant API）配置 ----
# 数据源主力。注意：账户有周度取数上限，静态/慢变数据须缓存到本地（见 data_cache/）。
IFIND_ACCOUNT = _cred("IFIND_ACCOUNT")
IFIND_PASSWORD = _cred("IFIND_PASSWORD")

# 本地数据缓存目录（静态数据取一次存这里，规避配额）
DATA_CACHE_DIR = _PROJECT_ROOT / "data_cache"


def has_llm() -> bool:
    """是否配置了可用的 DeepSeek key。未配置时 planner/writer 直接返回错误提示配置，
    绝不用假数据兜底（防幻觉原则：宁可不出，也不造假）。"""
    return bool(DEEPSEEK_API_KEY)


def has_ifind() -> bool:
    """是否配置了 iFinD 账户。未配置时取数层回退到 akshare。"""
    return bool(IFIND_ACCOUNT and IFIND_PASSWORD)


# ---- OptionHelper（衍生品推荐/定价/回测/报告完整版）集成配置 ----
# OptionHelper 是独立项目（不在本仓库内），锁定了 numpy/pandas/scipy/numba
# 的精确版本，与本项目自己的环境混装有版本冲突风险，必须用独立解释器隔离
# 调用（子进程，见 core/optionhelper_bridge.py），故这里只记路径，不 import。
OPTIONHELPER_ROOT = _cred("OPTIONHELPER_ROOT")   # option-helper 项目根目录（含 scripts/tool_entry.py）
# 默认指向随本项目一起建的隔离 venv（.optionhelper_venv，装了其 requirements.lock）；
# 换机器/换环境时用 OPTIONHELPER_PYTHON 环境变量或 config.local.json 覆盖。
OPTIONHELPER_PYTHON = _cred(
    "OPTIONHELPER_PYTHON", str(_PROJECT_ROOT / ".optionhelper_venv" / "Scripts" / "python.exe"),
)
# OptionHelper 自己的 iFind 凭证体系是 Refresh Token（HTTP API），
# 与本项目 IFIND_ACCOUNT/PASSWORD（同花顺 iFinD 桌面端账户）不是同一套，不能互相代替。
OPTIONHELPER_IFIND_REFRESH_TOKEN = _cred("IFIND_REFRESH_TOKEN")


def has_optionhelper() -> bool:
    """OptionHelper 完整版三项前置是否都已配置：项目路径、独立解释器、iFind Token。
    模型网关复用本项目已有的 DeepSeek key，不算在内（见 has_llm）。"""
    return bool(
        OPTIONHELPER_ROOT
        and Path(OPTIONHELPER_PYTHON).exists()
        and OPTIONHELPER_IFIND_REFRESH_TOKEN
    )
