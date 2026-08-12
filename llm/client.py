"""DeepSeek 客户端（用 requests 直连 REST API，不依赖 openai 包）。

选择直连 REST 的原因：
  - 少一个依赖，打包 exe 更干净（也避开了 pip 装 openai 时的代理/SSL 问题）；
  - api.deepseek.com 是国内站，直连更稳，需绕过本地代理（同 akshare）。

用法：
    cli = DeepSeekClient()
    res = cli.chat_json(system="...", user="...")   # 强制 JSON 输出并解析
    if res.ok: plan = res.data

多用户 key 策略：key 从各用户本机 config.local.json/环境变量读，绝不打进 exe（DESIGN §13）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import requests

from core import config
from core.config import no_proxy


@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    data: Any = None                       # json 模式下解析出的对象
    usage: dict = field(default_factory=dict)
    error: str = ""


class DeepSeekClient:
    def __init__(self, model: str | None = None, timeout: int = 120) -> None:
        self.api_key = config.DEEPSEEK_API_KEY
        self.base_url = config.DEEPSEEK_BASE_URL.rstrip("/")
        self.model = model or config.DEEPSEEK_MODEL
        self.timeout = timeout
        self.total_tokens = 0   # 累计 token，成本监控（同 iFinD 的 dataVol 记账）

    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = True,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        if not self.available():
            return ChatResult(False, error="未配置 DEEPSEEK_API_KEY")

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens

        try:
            with no_proxy():
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            if r.status_code != 200:
                return ChatResult(False, error=f"HTTP {r.status_code}: {r.text[:200]}")
            d = r.json()
            content = d["choices"][0]["message"]["content"]
            usage = d.get("usage", {}) or {}
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)

            data = None
            if json_mode:
                try:
                    data = json.loads(content)
                except json.JSONDecodeError as e:
                    return ChatResult(False, content=content, usage=usage,
                                      error=f"JSON 解析失败: {e}")
            return ChatResult(True, content=content, data=data, usage=usage)
        except Exception as e:  # noqa: BLE001
            return ChatResult(False, error=f"{type(e).__name__}: {str(e)[:200]}")

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2,
                  max_tokens: int | None = None) -> ChatResult:
        """便捷：系统+用户两段提示，强制 JSON 输出并解析。

        max_tokens 留给输出量大的环节显式加码（如 writer 要为每条逻辑写 120~200 字，
        三条逻辑加核心结论、图表规格，默认额度可能不够）。
        """
        return self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )


if __name__ == "__main__":  # python -m llm.client
    cli = DeepSeekClient()
    print("available:", cli.available(), "| model:", cli.model)
    res = cli.chat_json("你是测试助手，只输出JSON。", '返回 {"pong": true}')
    print("ok:", res.ok, "| data:", res.data, "| error:", res.error)
    print("累计 tokens:", cli.total_tokens)
