"""受控事件材料导入：只提取原文候选，绝不替分析师生成事实或传导关系。

事件型报告不能把 LLM 的概括当成海外公司业绩、公告或产业链因果。本模块只读取
分析师显式选择的本地材料（或其手动输入的直链下载件），按页保留可回查的原文片段。
GUI 中必须由分析师选定片段，并明确其是“事件事实”还是“传导证据”，才会写入观点包。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MATERIALS_DIR = Path(__file__).resolve().parent.parent / "output" / "materials"
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".docx"}


@dataclass(frozen=True)
class MaterialCandidate:
    """一个可直接回查到原文件页码的原文片段。"""

    content: str
    source: str
    reference: str
    page: int = 0


def _read_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .docs import read_pages
        return read_pages(path)
    if suffix in {".txt", ".md"}:
        return [path.read_text(encoding="utf-8", errors="replace")]
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError as error:
            raise ValueError("读取 DOCX 需要安装 python-docx；请改用 PDF/TXT，或安装该依赖。") from error
        document = Document(str(path))
        return ["\n".join(p.text for p in document.paragraphs)]
    raise ValueError("仅支持 PDF、TXT、MD 或 DOCX 材料。")


def _query_tokens(query: str) -> set[str]:
    latin = {word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{1,}", query)}
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    # 中文没有空格，取相邻双字以提高“与本次需求相关”的排序，但不把未命中的句子丢掉。
    return latin | {chinese[i:i + 2] for i in range(max(0, len(chinese) - 1))}


def _split_snippets(page: str) -> list[str]:
    compact = re.sub(r"[\t \u3000]+", " ", page).strip()
    raw = re.split(r"(?<=[。！？!?；;])\s*|\n{2,}", compact)
    snippets: list[str] = []
    for part in raw:
        part = part.strip()
        if len(part) < 24:
            continue
        # 很长的 PDF 段落只展示原文开头；不追加省略号或做改写，确保被导入的内容本身
        # 仍是材料中的逐字片段。分析师可回到页码查看未展示的后半段。
        snippets.append(part[:520])
    return snippets


def extract_candidates(path: Path, *, query: str = "", limit: int = 60,
                       reference: str | None = None) -> list[MaterialCandidate]:
    """从一份材料返回按相关度排序的原文候选，候选内容从不经过 LLM。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"找不到材料：{path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError("仅支持 PDF、TXT、MD 或 DOCX 材料。")
    tokens = _query_tokens(query)
    candidates: list[tuple[int, int, MaterialCandidate]] = []
    ref = reference or str(path.resolve())
    for page_no, page in enumerate(_read_pages(path), 1):
        for position, text in enumerate(_split_snippets(page)):
            lower = text.lower()
            matches = sum(1 for token in tokens if token.lower() in lower)
            # 数字、业绩/公告字样只能影响排序，不能充当真伪判断或自动分类。
            factual_hint = int(bool(re.search(r"\d", text))) + int(bool(re.search(
                r"业绩|营收|收入|利润|出货|指引|公告|同比|环比|revenue|earnings|guidance|shipment",
                text, re.I)))
            candidate = MaterialCandidate(
                content=text,
                source=f"{path.name} p{page_no}",
                reference=ref,
                page=page_no,
            )
            candidates.append((matches * 10 + factual_hint, -position, candidate))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in candidates[:limit]]


def download_direct_material(url: str) -> Path:
    """下载分析师手动提供的 HTTPS 直链材料，拒绝网页和过大文件。

    仅供 GUI 中用户点击“从直链获取”使用；不根据需求自动联网，也不会抓取网页或重定向后的
    任意内容。下载件保存到已忽略的 output/materials，保证本次引用仍能被本机复核。
    """
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("只接受 HTTPS 的 PDF/TXT/MD/DOCX 直链，不抓取普通网页。")
    request = Request(url, headers={"User-Agent": "ResearchHelper/1.0"})
    try:
        with urlopen(request, timeout=20) as response:
            final_url = response.geturl()
            final = urlparse(final_url)
            if final.scheme != "https" or not final.netloc:
                raise ValueError("下载跳转后的地址不是 HTTPS，已拒绝。")
            content_type = (response.headers.get("Content-Type") or "").lower()
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                raise ValueError("材料超过 25MB 限制。")
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - 需要把网络库异常转成 GUI 可读错误
        raise ValueError(f"无法下载材料：{type(error).__name__}: {error}") from error
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("材料超过 25MB 限制。")
    guessed = Path(final.path).suffix.lower()
    if data[:4] == b"%PDF":
        suffix = ".pdf"
    elif guessed in {".txt", ".md", ".docx"} and ("html" not in content_type):
        suffix = guessed
    else:
        raise ValueError("链接未返回可解析的 PDF/TXT/MD/DOCX 原件；请下载原件后选择本地文件。")
    MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    target = MATERIALS_DIR / f"material-{digest}{suffix}"
    if not target.exists():
        target.write_bytes(data)
    return target
