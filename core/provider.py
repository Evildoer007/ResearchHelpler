"""数据提供者抽象层。

把取数的"怎么调某个数据源"隔离在这里，上层 fetcher 只面向统一接口，
数据源可切换/降级（iFinD 用满配额或缺字段时回退 akshare），换源不动上层。

分层：
  provider（本文件）= 源相关的低层取数：给"源生指标代码 + 标的"，返回规范化数值。
  fetcher（下一层）  = 把 schema 的业务字段映射到某个 provider 的指标代码并调用。

注意：
- iFinD 走自己的网络栈，无需绕本地代理；只有 akshare 需要 no_proxy()。
- iFinD 有配额，provider 累计 dataVol 便于监控消耗。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import time
from typing import Any

from . import config
from .run_tracker import record_external


@dataclass
class FetchResult:
    """一次取数的规范化结果。

    data 结构：{ 标的代码: { 指标代码: 值或列表 } }
    单个标的单指标时可用 .value 便捷取第一个值。
    """

    ok: bool
    source: str
    data: dict[str, dict[str, Any]] = field(default_factory=dict)
    data_vol: int = 0
    error: str = ""

    @property
    def value(self) -> Any:
        """便捷取第一个标的的第一个指标的第一个值（点查场景）。"""
        for _code, inds in self.data.items():
            for _ind, val in inds.items():
                if isinstance(val, list):
                    return val[0] if val else None
                return val
        return None


class DataProvider(ABC):
    """数据源统一接口。指标代码为"源生"代码，字段映射由 fetcher 负责。"""

    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """该源当前是否可用（有凭证/依赖）。"""

    @abstractmethod
    def get_basic(self, codes: list[str], indicators: list[str], params: str = "") -> FetchResult:
        """时点基础数据：一批标的 × 一批指标。"""

    def get_history(self, code: str, indicator: str, start: str, end: str) -> FetchResult:
        """历史序列（算分位、事件研究用）。默认未实现，子类按需覆盖。"""
        raise NotImplementedError(f"{self.name} 未实现 get_history")

    def close(self) -> None:  # 可选清理
        pass


# ============================================================
# iFinD（主力，已实测可用）
# ============================================================
class iFinDProvider(DataProvider):
    name = "iFinD"

    def __init__(self) -> None:
        self._logged_in = False
        self.total_data_vol = 0  # 累计消耗，监控配额

    def available(self) -> bool:
        if not config.has_ifind():
            return False
        try:
            import iFinDPy  # noqa: F401
            return True
        except Exception:
            return False

    def _ensure_login(self) -> None:
        """惰性登录，进程内只登一次。"""
        if self._logged_in:
            return
        import iFinDPy as ths

        ret = ths.THS_iFinDLogin(config.IFIND_ACCOUNT, config.IFIND_PASSWORD)
        if ret in (0, -201):  # 0=成功, -201=已登录
            self._logged_in = True
        else:
            raise RuntimeError(f"iFinD 登录失败，返回码 {ret}")

    @staticmethod
    def _parse_basic(d: dict) -> dict[str, dict[str, Any]]:
        """解析 THS_BasicData 返回：d['tables'][i] = {thscode, table:{ind:[vals]}}。"""
        out: dict[str, dict[str, Any]] = {}
        for t in d.get("tables", []) or []:
            code = t.get("thscode", "")
            table = t.get("table", {}) or {}
            out[code] = {ind: vals for ind, vals in table.items()}
        return out

    @staticmethod
    def _join_params(indicators: list[str], params) -> str:
        """拼接参数串。

        iFinD 规则（实测）：多指标时参数必须用 `;` 分隔且**个数与指标一一对应**，
        否则报 -209。例如 3 个无参指标要传 ";;"，不能传 ""。
        params 可为 list（逐指标对应）或 str（所有指标共用该参数）。
        """
        if isinstance(params, (list, tuple)):
            plist = list(params) + [""] * (len(indicators) - len(params))
        else:
            plist = [params or ""] * len(indicators)
        return ";".join(plist[: len(indicators)])

    @staticmethod
    def _retryable_error(error: str) -> bool:
        """只为明确的瞬时连接故障重试，业务口径/权限错误必须直接返回。"""
        text = (error or "").lower()
        return any(token in text for token in (
            "timeout", "timed out", "connection", "reset", "temporary", "unavailable",
            "网络", "连接", "超时", "服务暂",
        ))

    def get_basic(self, codes: list[str], indicators: list[str], params="") -> FetchResult:
        for attempt in range(1, 3):
            started = time.perf_counter()
            try:
                self._ensure_login()
                import iFinDPy as ths

                code_str = ",".join(codes)
                ind_str = ";".join(indicators)
                d = ths.THS_BasicData(code_str, ind_str, self._join_params(indicators, params))
                if d.get("errorcode", -1) != 0:
                    error = f"errorcode={d.get('errorcode')} {d.get('errmsg')}"
                    record_external("iFinD THS_BasicData", status="failed",
                                    duration_seconds=time.perf_counter() - started, attempt=attempt,
                                    detail=f"codes={len(codes)}, indicators={len(indicators)}", error=error)
                    if attempt < 2 and self._retryable_error(error):
                        time.sleep(1)
                        continue
                    return FetchResult(False, self.name, error=error)
                vol = int(d.get("dataVol", 0) or 0)
                self.total_data_vol += vol
                record_external("iFinD THS_BasicData", status="completed",
                                duration_seconds=time.perf_counter() - started, attempt=attempt,
                                detail=f"codes={len(codes)}, indicators={len(indicators)}, dataVol={vol}")
                return FetchResult(True, self.name, data=self._parse_basic(d), data_vol=vol)
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {str(e)[:200]}"
                record_external("iFinD THS_BasicData", status="failed",
                                duration_seconds=time.perf_counter() - started, attempt=attempt,
                                detail=f"codes={len(codes)}, indicators={len(indicators)}", error=error)
                if attempt < 2 and self._retryable_error(error):
                    time.sleep(1)
                    continue
                return FetchResult(False, self.name, error=error)
        return FetchResult(False, self.name, error="iFinD 基础数据重试耗尽")

    def get_history(self, code: str, indicator: str, start: str, end: str) -> FetchResult:
        for attempt in range(1, 3):
            started = time.perf_counter()
            try:
                self._ensure_login()
                import iFinDPy as ths

                d = ths.THS_HistoryQuotes(code, indicator, "", start, end)
                if d.get("errorcode", -1) != 0:
                    error = f"errorcode={d.get('errorcode')} {d.get('errmsg')}"
                    record_external("iFinD THS_HistoryQuotes", status="failed",
                                    duration_seconds=time.perf_counter() - started, attempt=attempt,
                                    detail=f"indicator={indicator}", error=error)
                    if attempt < 2 and self._retryable_error(error):
                        time.sleep(1)
                        continue
                    return FetchResult(False, self.name, error=error)
                vol = int(d.get("dataVol", 0) or 0)
                self.total_data_vol += vol
                record_external("iFinD THS_HistoryQuotes", status="completed",
                                duration_seconds=time.perf_counter() - started, attempt=attempt,
                                detail=f"indicator={indicator}, dataVol={vol}")
                return FetchResult(True, self.name, data=self._parse_basic(d), data_vol=vol)
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {str(e)[:200]}"
                record_external("iFinD THS_HistoryQuotes", status="failed",
                                duration_seconds=time.perf_counter() - started, attempt=attempt,
                                detail=f"indicator={indicator}", error=error)
                if attempt < 2 and self._retryable_error(error):
                    time.sleep(1)
                    continue
                return FetchResult(False, self.name, error=error)
        return FetchResult(False, self.name, error="iFinD 历史行情重试耗尽")

    def close(self) -> None:
        if not self._logged_in:
            return
        try:
            import iFinDPy as ths

            logout = getattr(ths, "THS_iFinDLogout", None)
            if callable(logout):
                logout()
        except Exception:
            pass
        finally:
            self._logged_in = False


# ============================================================
# akshare（免费补充/降级）—— 先占位，随 fetcher 需要再填实
# ============================================================
class AkshareProvider(DataProvider):
    name = "akshare"

    def available(self) -> bool:
        try:
            import akshare  # noqa: F401
            return True
        except Exception:
            return False

    def get_basic(self, codes: list[str], indicators: list[str], params: str = "") -> FetchResult:
        # TODO: 按需实现——akshare 无统一"标的×指标"接口，需按指标分派到具体函数。
        # 用 config.no_proxy() 包裹实际调用（akshare 数据源需绕代理）。
        return FetchResult(
            False, self.name,
            error="AkshareProvider.get_basic 待实现（按 fetcher 需要的字段逐个接）",
        )


# ============================================================
# 工厂：优先 iFinD，不可用则降级 akshare
# ============================================================
def get_provider(prefer: str = "iFinD") -> DataProvider:
    ifind = iFinDProvider()
    ak = AkshareProvider()
    order = [ifind, ak] if prefer == "iFinD" else [ak, ifind]
    for p in order:
        if p.available():
            return p
    raise RuntimeError("没有可用的数据源（iFinD 未配置且 akshare 未安装）")


if __name__ == "__main__":  # 实测：python -m core.provider
    p = get_provider()
    print(f"选用数据源: {p.name}  可用: {p.available()}")
    r = p.get_basic(["600519.SH", "000001.SZ"], ["ths_stock_short_name_stock"])
    print(f"ok={r.ok}  source={r.source}  dataVol={r.data_vol}  error={r.error}")
    print("data:", r.data)
    print("便捷 .value:", r.value)
    if isinstance(p, iFinDProvider):
        print("累计 dataVol:", p.total_data_vol)
        p.close()
