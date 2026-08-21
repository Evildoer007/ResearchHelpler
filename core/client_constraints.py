"""客户条件的统一输入契约。

研究观点与客户条件是两层信息：前者只陈述市场状态；后者由客户明确给出或使用透明的
项目默认值，并单独交给 OptionHelper。这里不根据条件推导产品，更不修改价格或条款。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from . import config


@dataclass
class ClientConstraints:
    horizon: str = ""
    max_loss: str = ""
    principal_fluctuation: bool | None = None
    return_preference: str = ""

    def supplied(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.horizon:
            out["horizon"] = self.horizon
        if self.max_loss:
            out["max_loss"] = self.max_loss
        if self.principal_fluctuation is not None:
            out["principal_fluctuation"] = self.principal_fluctuation
        if self.return_preference:
            out["return_preference"] = self.return_preference
        return out

    def effective(self, base: Mapping[str, object] | None = None) -> dict[str, object]:
        """客户本次明确输入覆盖默认档案；收益偏好没有默认值。"""
        out = dict(base if base is not None else config.OPTIONHELPER_DEFAULT_CONSTRAINTS)
        out.update(self.supplied())
        return out

    def describe(self, base: Mapping[str, object] | None = None) -> str:
        value = self.effective(base)
        principal = "接受本金波动" if value.get("principal_fluctuation") else "不接受本金波动"
        parts = [f"期限 {value.get('horizon', '—')}", f"最大损失 {value.get('max_loss', '—')}", principal]
        if value.get("return_preference"):
            parts.append(f"收益偏好 {value['return_preference']}")
        return "；".join(parts)


def parse_cli(args: list[str]) -> tuple[list[str], ClientConstraints, str]:
    """消费客户条件参数，返回其余命令行；不改变原有 `-b` 文本的拼接行为。"""
    values = ClientConstraints()
    remaining: list[str] = []
    index = 0
    aliases = {
        "--horizon": "horizon",
        "--max-loss": "max_loss",
        "--return-preference": "return_preference",
        "--principal-fluctuation": "principal_fluctuation",
    }
    while index < len(args):
        key = args[index]
        field = aliases.get(key)
        if field is None:
            remaining.append(key)
            index += 1
            continue
        if index + 1 >= len(args):
            return remaining, values, f"{key} 缺少值"
        raw = args[index + 1].strip()
        index += 2
        if not raw:
            return remaining, values, f"{key} 不能为空"
        if field == "principal_fluctuation":
            normalized = raw.lower()
            if normalized in ("yes", "true", "1", "接受", "接受本金波动"):
                values.principal_fluctuation = True
            elif normalized in ("no", "false", "0", "不接受", "不接受本金波动"):
                values.principal_fluctuation = False
            else:
                return remaining, values, "--principal-fluctuation 只能是 yes 或 no"
        elif field == "max_loss":
            try:
                number = float(raw.rstrip("%"))
            except ValueError:
                return remaining, values, "--max-loss 应为百分比，例如 20%"
            if number < 0 or number > 100:
                return remaining, values, "--max-loss 必须在 0% 到 100% 之间"
            values.max_loss = f"{number:g}%"
        elif field == "horizon":
            values.horizon = raw[:40]
        else:
            values.return_preference = raw[:80]
    return remaining, values, ""
