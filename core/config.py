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
