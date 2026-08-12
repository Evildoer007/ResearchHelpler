"""上市公司公告：查最新公告并下载到 sources/，供研报文档通道一并抽取。

为什么公告值得单独接一条通道（而不是塞进 docs.py）：
  公告是上市公司依法向交易所提交的**监管披露文件**，不是媒体或研报的原创表达——
  引用它和引用研报/新闻是完全不同性质的合规问题，风险低得多（详见 DESIGN §7.5）。
  数据源是交易所官方 URL（sse.com.cn / szse.cn），不是转载。

⚠ **`sources/` 仍是"用完即清"的工作台，这里不改变这个定位**（见 DESIGN #64）。
下载下来的公告 PDF 与分析师手动放入的研报待遇完全一样：都是"这次要用的材料"，
分析师用完后一并清空，不具备任何跨会话的持久性。

接口边界（实测确认，见 DESIGN #64）：
  - `THS_iResearch`（研报）/ `THS_iEvent`（事件）：SDK 里有函数，但账号未开通该模块权限
    （errorcode=0 但 errmsg="account type is not supported"）。
  - 公告改走 `THS_iwencai`（自然语言查询，权限已通），返回结构里带 PageRawTitle/URL/
    PublishTime/Channel_，Channel_="pubnote" 即公司公告。
  - **不管怎么措辞（"近30天全部公告"/"最近20条"），返回数量固定在 1~2 条**，
    不受自然语言里的数量词控制——这是接口本身的限制，不是我们查询方式的问题。
    故这条通道只适合"看最新一条有没有新公告"，不适合批量拉取历史公告。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import config

SOURCES_DIR = Path(__file__).resolve().parent.parent / "sources"


@dataclass
class Filing:
    标题: str
    日期: str          # YYYY-MM-DD
    url: str
    uid: str = ""
    是否PDF: bool = True


def _query_latest(code: str, provider=None) -> list[Filing]:
    """查某标的最新公告。провайдер 保留位供未来切换，当前直连 iFinD。"""
    from iFinDPy import THS_iwencai

    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()

    r = THS_iwencai(f"{code} 最新公告标题", "stock")
    if r.get("errorcode") != 0:
        return []
    tables = r.get("tables") or []
    if not tables:
        return []
    t = tables[0].get("table", {})
    key = next((k for k in t if "资讯" in k), None)
    if not key or not t[key]:
        return []
    try:
        items = json.loads(t[key][0])
    except (json.JSONDecodeError, IndexError, TypeError):
        return []

    out = []
    for it in items:
        if it.get("Channel_") != "pubnote":     # 只要公司公告，滤掉资讯/研报等其它频道
            continue
        title = str(it.get("PageRawTitle", "")).strip()
        date = str(it.get("PublishTime", "")).strip()
        url = str(it.get("URL", "")).strip()
        if not (title and date and url):
            continue
        out.append(Filing(标题=title, 日期=date, url=url, uid=str(it.get("UID", ""))))
    return out


_BAD = re.compile(r'[\\/:*?"<>|]')


def _safe_title(s: str, limit: int = 60) -> str:
    """标题里的股票简称常带冒号（"兆易创新：兆易创新关于…"），
    与文件系统不允许的字符一并清掉，避免存盘失败。"""
    s = _BAD.sub("", s).strip()
    return s[:limit] if len(s) > limit else s


def download_latest(code: str, *, dest: Path | None = None,
                    provider=None, max_n: int = 3) -> list[Path]:
    """把某标的最新公告下载到 sources/，命名匹配 `docs.parse_filename` 的
    `YYYYMMDD-机构-标题` 格式，落盘后即被文档通道当作一篇待抽取的材料。

    去重靠**文件名**，不靠 UID：同一份公告重复调用只会产出同一个文件名，
    第二次直接跳过下载。⚠ 这只解决**同一次工作过程内**（改需求重跑、--pick 反复
    调整选择等）反复打网络请求的效率问题，不是持久化——`sources/` 仍要求用完即清，
    分析师清空后再跑，会重新下载，这是预期行为。
    """
    dest = dest or SOURCES_DIR
    dest.mkdir(parents=True, exist_ok=True)

    filings = _query_latest(code, provider)[:max_n]
    saved = []
    for f in filings:
        date8 = f.日期.replace("-", "")
        fname = f"{date8}-交易所公告-{_safe_title(f.标题)}.pdf"
        path = dest / fname
        if path.exists():             # 已存过，不重复下载
            saved.append(path)
            continue
        try:
            import requests

            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "Referer": "https://www.sse.com.cn/",
            }
            with config.no_proxy():
                resp = requests.get(f.url, headers=headers, timeout=20)
            resp.raise_for_status()
            # ⚠ 必须校验拿到的确实是 PDF，不能只看 HTTP 状态码。
            # 实测 sse.com.cn 的静态文件服务器挂着反爬 JS 挑战（阿里云 WAF），
            # 简单 GET 会拿到 200 状态 + 一段执行 JS 才能过关的 HTML 挑战页，
            # 内容是 `<html><script>...var arg1=...`，不是真实 PDF。
            # 若不校验，会把这段 HTML 当 PDF 存下——文件名看着正常（日期/标题都对），
            # 内容却是假的，比"下载失败什么都不做"更危险：分析师会以为公告已经取到，
            # 而后续 docs.read_pages() 解析这个"PDF"会直接报错崩掉整条抽取流程。
            if resp.content[:4] != b"%PDF":
                continue
            path.write_bytes(resp.content)
            saved.append(path)
        except Exception:
            continue                  # 单条下载失败不影响其它公告，也不影响报告生成
    return saved


if __name__ == "__main__":  # python -m core.filings <代码>
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "600030.SH"
    paths = download_latest(code)
    print(f"{code} 下载 {len(paths)} 份公告:")
    for p in paths:
        print("  ", p.name)
