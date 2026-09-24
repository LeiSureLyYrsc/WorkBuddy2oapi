"""官方 API 价格表：把网关的 token 用量换算成官方 API 花费。

计价口径分三档（缓存命中输入 / 缓存未命中输入 / 输出），支持高峰与空闲时段折算。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
DEEPSEEK_UPDATED = "2026-09-14"


@dataclass
class ModelPrice:
    """单个模型的官方单价（单位：元 / 百万 token）。"""

    cached_input: float = 0.0
    miss_input: float = 0.0
    output: float = 0.0
    off_peak_ratio: float = 0.0  # 空闲时段倍数（DeepSeek 为 0.5）
    note: str = ""

    def priced(self) -> bool:
        """报告该条目是否已配置有效价格（任一 > 0）。"""
        return self.cached_input > 0 or self.miss_input > 0 or self.output > 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "cached_input": self.cached_input,
            "miss_input": self.miss_input,
            "output": self.output,
        }
        if self.off_peak_ratio:
            d["off_peak_ratio"] = self.off_peak_ratio
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class Usage:
    """计价所需的 token 用量。"""

    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class Cost:
    """单个模型的换算结果。"""

    model: str
    priced: bool = False
    note: str = ""
    cached_input_cost: float = 0.0
    miss_input_cost: float = 0.0
    output_cost: float = 0.0
    total: float = 0.0
    cached_input_tokens: int = 0
    miss_input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "model": self.model,
            "priced": self.priced,
            "cached_input_cost": self.cached_input_cost,
            "miss_input_cost": self.miss_input_cost,
            "output_cost": self.output_cost,
            "total": self.total,
            "cached_input_tokens": self.cached_input_tokens,
            "miss_input_tokens": self.miss_input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.note:
            d["note"] = self.note
        return d


def normalize(s: str) -> str:
    """归一化模型名用于宽松匹配。"""
    s = s.strip().lower()
    for ch in (".", "-", "_", " "):
        s = s.replace(ch, "")
    return s


class PricingTable:
    """官方价格表。线程安全。"""

    def __init__(self, path: str = "") -> None:
        self._lock = threading.RLock()
        self._path = path
        self._models: dict[str, ModelPrice] = self.default()
        self.source = DEEPSEEK_SOURCE
        self.updated_at = DEEPSEEK_UPDATED
        if self._path:
            self.load()

    @staticmethod
    def default() -> dict[str, ModelPrice]:
        """内置默认价格表（DeepSeek 官方价）。"""
        flash = ModelPrice(
            cached_input=0.04,
            miss_input=2.0,
            output=8.0,
            off_peak_ratio=0.5,
            note="DeepSeek-V4.1-Flash（高峰价；空闲时段为半价）",
        )
        pro = ModelPrice(
            cached_input=0.30,
            miss_input=9.0,
            output=27.0,
            off_peak_ratio=0.5,
            note="DeepSeek-V4-Pro-0813（高峰价；空闲时段为半价）",
        )
        m: dict[str, ModelPrice] = {}
        for name in (
            "deepseek-flash",
            "deepseek-v4-flash",
            "deepseek-v4.1-flash",
            "deepseek-v4-flash-vision-exp",
        ):
            m[name] = ModelPrice(
                cached_input=flash.cached_input,
                miss_input=flash.miss_input,
                output=flash.output,
                off_peak_ratio=flash.off_peak_ratio,
                note=flash.note,
            )
        for name in ("deepseek-v4-pro", "deepseek-v4-pro-0813"):
            m[name] = ModelPrice(
                cached_input=pro.cached_input,
                miss_input=pro.miss_input,
                output=pro.output,
                off_peak_ratio=pro.off_peak_ratio,
                note=pro.note,
            )
        return m

    def load(self) -> None:
        """从磁盘加载价格表并合并（文件条目覆盖内置条目）。"""
        if not self._path:
            return
        p = Path(self._path)
        if not p.exists():
            return
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            return

        with self._lock:
            models_data = data.get("models")
            if isinstance(models_data, dict):
                for name, item in models_data.items():
                    if isinstance(item, dict):
                        self._models[name] = ModelPrice(
                            cached_input=float(item.get("cached_input", 0.0) or 0.0),
                            miss_input=float(item.get("miss_input", 0.0) or 0.0),
                            output=float(item.get("output", 0.0) or 0.0),
                            off_peak_ratio=float(item.get("off_peak_ratio", 0.0) or 0.0),
                            note=str(item.get("note", "") or ""),
                        )
            if data.get("source"):
                self.source = str(data["source"])
            if data.get("updated_at"):
                self.updated_at = str(data["updated_at"])

    def save(self) -> None:
        """原子写回价格表（0600 权限）。"""
        if not self._path:
            raise ValueError("未配置价格表路径")

        with self._lock:
            doc = {
                "models": {k: v.to_dict() for k, v in self._models.items()},
                "source": self.source,
                "updated_at": self.updated_at,
            }

        raw = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        target = Path(self._path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")

        with self._lock:
            tmp.write_text(raw, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

    def resolve(self, model: str) -> tuple[ModelPrice, bool]:
        """查找模型单价。先精确匹配，再做别名归一化匹配。"""
        with self._lock:
            if model in self._models and self._models[model].priced():
                return self._models[model], True
            norm = normalize(model)
            for name, p in self._models.items():
                if normalize(name) == norm and p.priced():
                    return p, True
        return ModelPrice(), False

    def compute(self, model: str, usage: Usage, mode: str = "peak") -> Cost:
        """按价格表把用量换算成官方 API 花费。

        mode: "peak" | "offpeak"
        miss fallback = prompt_tokens - hit，若大于上报 miss 则取 fallback。
        """
        c = Cost(model=model)
        p, ok = self.resolve(model)
        if not ok:
            return c

        c.priced = True
        c.note = p.note

        hit = max(0, usage.cache_hit_tokens)
        miss = max(0, usage.cache_miss_tokens)
        fallback = max(0, usage.prompt_tokens - hit)
        if fallback > miss:
            miss = fallback
        out = max(0, usage.completion_tokens)

        ratio = 1.0
        if mode.lower() == "offpeak" and p.off_peak_ratio > 0:
            ratio = p.off_peak_ratio

        per_million = 1_000_000
        c.cached_input_tokens = hit
        c.miss_input_tokens = miss
        c.output_tokens = out
        c.cached_input_cost = (hit / per_million) * p.cached_input * ratio
        c.miss_input_cost = (miss / per_million) * p.miss_input * ratio
        c.output_cost = (out / per_million) * p.output * ratio
        c.total = c.cached_input_cost + c.miss_input_cost + c.output_cost
        return c

    def set(self, model: str, price: ModelPrice) -> None:
        """更新单个模型的单价。"""
        with self._lock:
            self._models[model] = price

    def delete(self, model: str) -> None:
        """删除单个模型的价格。"""
        with self._lock:
            self._models.pop(model, None)

    def models_copy(self) -> dict[str, ModelPrice]:
        """返回当前价格表的副本。"""
        with self._lock:
            return {
                k: ModelPrice(
                    cached_input=v.cached_input,
                    miss_input=v.miss_input,
                    output=v.output,
                    off_peak_ratio=v.off_peak_ratio,
                    note=v.note,
                )
                for k, v in self._models.items()
            }

    def unpriced(self, models: list[str]) -> list[str]:
        """返回价格表中没有单价的模型名。"""
        with self._lock:
            out = [m for m in models if not self.resolve(m)[1]]
        out.sort()
        return out
