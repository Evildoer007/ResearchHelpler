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
import time
from threading import Event, Thread
from dataclasses import dataclass, field
from typing import Any

import requests

from core import config
from core.config import no_proxy
from core.run_tracker import record_external


class _WaitHeartbeat:
    """仅在受 RunTracker 管理的正式运行中提示长时 LLM 等待。"""

    def __init__(self, *, label: str, started: float, enabled: bool) -> None:
        self.label = label
        self.started = started
        self.enabled = enabled
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        print(f"  · [llm] {self.label} 请求已发出，正在等待响应…")
        self._thread = Thread(target=self._run, name="research-helper-llm-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(15):
            elapsed = int(time.perf_counter() - self.started)
            print(f"  · [llm] {self.label} 仍在响应，已等待 {elapsed}s…")

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)


@dataclass
class ChatResult:
    ok: bool
    content: str = ""
    data: Any = None                       # json 模式下解析出的对象
    usage: dict = field(default_factory=dict)
    error: str = ""
    # 上游给的结束原因。**"stop"=正常写完，"length"=被 max_tokens 掐断**。
    # 必须留着：截断的 JSON 与空响应的报错长得一样（都是"JSON 解析失败"），
    # 但成因相反——空响应是瞬时抖动、重试即好；被掐断是额度不够、
    # 重试多少次都一样掐在同一处，只会白烧几倍 token（实测连撞 3 次）。
    finish_reason: str = ""


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
        _attempt: int = 1,
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

        started = time.perf_counter()
        # 先记录“运行中”，供终端和 GUI 在 requests.post 阻塞期间显示真实状态；
        # 每个现有的完成/失败 record_external 会原位收束该条记录。
        record_external("DeepSeek chat/completions", status="running", duration_seconds=0,
                        attempt=_attempt, detail=f"model={self.model}，等待响应")
        from core.run_tracker import current
        heartbeat = _WaitHeartbeat(label="DeepSeek", started=started, enabled=current() is not None)
        heartbeat.start()
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
                error = f"HTTP {r.status_code}: {r.text[:200]}"
                record_external("DeepSeek chat/completions", status="failed",
                                duration_seconds=time.perf_counter() - started, attempt=_attempt,
                                detail=f"HTTP {r.status_code}", error=error)
                return ChatResult(False, error=error)
            d = r.json()
            choice = d["choices"][0]
            content = choice["message"]["content"]
            fr = str(choice.get("finish_reason") or "")
            usage = d.get("usage", {}) or {}
            self.total_tokens += int(usage.get("total_tokens", 0) or 0)

            data = None
            if json_mode:
                try:
                    data = json.loads(content)
                except json.JSONDecodeError as e:
                    if fr == "length":
                        # 额度不够，不是坏运气。把推理 token 一并报出来——
                        # 推理模型（deepseek-v4-pro）的思考过程也计入 max_tokens，
                        # 实测 reasoning 能占掉 2500~7000，正文再长就写不完了。
                        rt = (usage.get("completion_tokens_details") or {}).get(
                            "reasoning_tokens", 0)
                        error = (f"输出被 max_tokens 掐断（completion "
                                 f"{usage.get('completion_tokens')} 其中推理 {rt}）"
                                 f"——需加大额度或压缩输出要求，重试无效")
                        record_external("DeepSeek chat/completions", status="failed",
                                        duration_seconds=time.perf_counter() - started, attempt=_attempt,
                                        detail="finish_reason=length", error=error)
                        return ChatResult(
                            False, content=content, usage=usage, finish_reason=fr,
                            error=error)
                    error = f"JSON 解析失败: {e}"
                    record_external("DeepSeek chat/completions", status="failed",
                                    duration_seconds=time.perf_counter() - started, attempt=_attempt,
                                    detail=f"finish_reason={fr or 'unknown'}", error=error)
                    return ChatResult(False, content=content, usage=usage,
                                      finish_reason=fr, error=error)
            record_external("DeepSeek chat/completions", status="completed",
                            duration_seconds=time.perf_counter() - started, attempt=_attempt,
                            detail=f"tokens={usage.get('total_tokens', 0)}")
            return ChatResult(True, content=content, data=data, usage=usage,
                              finish_reason=fr)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:200]}"
            record_external("DeepSeek chat/completions", status="failed",
                            duration_seconds=time.perf_counter() - started, attempt=_attempt,
                            error=error)
            return ChatResult(False, error=error)
        finally:
            heartbeat.close()

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2,
                  max_tokens: int | None = None, retries: int = 2) -> ChatResult:
        """便捷：系统+用户两段提示，强制 JSON 输出并解析。**自带瞬时故障重试**。

        max_tokens 留给输出量大的环节显式加码（如 writer 要为每条逻辑写 160~260 字，
        三条逻辑加核心结论、图表规格，默认额度可能不够）。

        ## 为什么重试放在这一层（D6）

        实测反复撞到三种**与提示词内容无关**的瞬时故障：
          · 空字符串响应        → `JSON 解析失败: Expecting value: line 1 column 1`
          · 响应被截断          → `JSON 解析失败: Unterminated string starting at ...`
          · 上游连接/超时抖动    → `ConnectionError` / `ReadTimeout`
        隔离测试证实与 prompt 无关：同一个输入连调 4 次，4 次全成——纯服务端抖动。
        但一次失败就让整份报告失败、要分析师手动重跑（还得重新花掉前面所有取数与
        规划的时间和额度），代价完全不对称。

        `docs.extract()` 早就自己加了"空结果重试一次"，而 `planner.plan()` /
        `writer.write()` / `selection.propose()` 都没有——**同一个毛病在每个调用点
        各修一次是错的**，故统一放在这里，所有调用方自动获得防护。

        只重试**瞬时**故障，不重试确定性失败：未配 key、HTTP 4xx（鉴权/参数错）、
        以及**被 max_tokens 掐断**（`finish_reason == "length"`）。

        ⚠ 最后一条是踩出来的：截断的 JSON 与空响应报的错长得一模一样
        （都是"JSON 解析失败"），但成因相反。把额度不够也当瞬时故障去重试，
        只会在同一处掐断三次、白烧三倍 token——实测正是如此（连撞 3 次，
        截断位置都在 char 3300 附近）。故改用 `finish_reason` 区分，
        不靠错误字符串猜。
        """
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last = ChatResult(False, error="未执行")
        for attempt in range(retries + 1):
            last = self.chat(msgs, json_mode=True, temperature=temperature,
                             max_tokens=max_tokens, _attempt=attempt + 1)
            if last.ok:
                if attempt:
                    last.error = f"（第 {attempt + 1} 次尝试成功，前 {attempt} 次瞬时失败）"
                return last
            if not self._retryable(last):
                return last
            # 仅对已判定的瞬时故障退避重试，避免同一时刻连续撞到上游抖动。
            if attempt < retries:
                time.sleep(min(2 ** attempt, 4))
        # DeepSeek 官方已知：JSON Output 偶尔会在 finish_reason=stop 时返回空 content。
        # 连续重试仍为空时，再发一次普通文本请求，让提示词继续约束“只输出 JSON”，
        # 然后在本地严格解析。只对“空响应”启用；非空坏 JSON、长度截断和 4xx 不绕过。
        if not (last.content or "").strip() and "JSON 解析失败" in (last.error or ""):
            plain = self.chat(msgs, json_mode=False, temperature=temperature,
                              max_tokens=max_tokens, _attempt=retries + 2)
            if plain.ok:
                try:
                    plain.data = self._parse_json_text(plain.content)
                    plain.error = f"（JSON Output 连续 {retries + 1} 次空响应，普通文本模式兜底成功）"
                    return plain
                except json.JSONDecodeError as error:
                    plain.ok = False
                    plain.error = f"普通文本兜底仍非合法 JSON: {error}"
            return plain
        return last

    @staticmethod
    def _parse_json_text(content: str):
        """解析普通模式返回的 JSON；只剥代码围栏，不容忍 JSON 外的解释文字。"""
        text = (content or "").strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        return json.loads(text)

    @staticmethod
    def _retryable(res: "ChatResult") -> bool:
        """这次失败值不值得重试。默认不重试——只放行确认过的瞬时故障签名。"""
        # 被 max_tokens 掐断：确定性失败，重试只会掐在同一处，先判掉
        if res.finish_reason == "length":
            return False
        e = res.error or ""
        if "未配置" in e or e.startswith("HTTP 4"):
            return False
        return any(k in e for k in (
            "JSON 解析失败",          # 此处只剩"空响应/坏 JSON"，截断的已在上面排除
            "ConnectionError", "Timeout", "ChunkedEncoding",
            "RemoteDisconnected", "ProtocolError",
            "HTTP 5",                  # 上游 5xx
        ))


if __name__ == "__main__":  # python -m llm.client
    cli = DeepSeekClient()
    print("available:", cli.available(), "| model:", cli.model)
    res = cli.chat_json("你是测试助手，只输出JSON。", '返回 {"pong": true}')
    print("ok:", res.ok, "| data:", res.data, "| error:", res.error)
    print("累计 tokens:", cli.total_tokens)
