"""按模型维度的请求量/Token/缓存命中/扣费统计收集器。

支持按小时分桶存储时间序列，并可在日/周维度动态聚合。
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from wb2api.models import iso, now_iso

DEFAULT_RETENTION_SECONDS = 30 * 86400.0  # 30 天


@dataclass
class ModelStats:
    """单个模型的累计统计。"""

    model: str = ""
    requests: int = 0
    success: int = 0
    failed: int = 0
    streaming: int = 0

    ttfb_sum_ms: int = 0
    ttfb_count: int = 0
    latency_sum_ms: int = 0
    latency_count: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_reported: int = 0

    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    credit_milli: int = 0
    gen_sum_ms: int = 0
    gen_count: int = 0

    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_seen"] = iso(self.first_seen) if self.first_seen > 0 else ""
        d["last_seen"] = iso(self.last_seen) if self.last_seen > 0 else ""
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelStats:
        d = dict(data)
        first_seen = d.get("first_seen", 0.0)
        if isinstance(first_seen, str):
            first_seen = _parse_iso_ts(first_seen)
        d["first_seen"] = float(first_seen or 0.0)

        last_seen = d.get("last_seen", 0.0)
        if isinstance(last_seen, str):
            last_seen = _parse_iso_ts(last_seen)
        d["last_seen"] = float(last_seen or 0.0)

        # 兼容字段
        valid_fields = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class Delta:
    """单个请求的观测值。"""

    model: str = ""
    stream: bool = False
    ok: bool = True
    ttfb_ms: float = 0.0
    latency_ms: float = 0.0
    has_usage: bool = False

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    credit: float = 0.0


def _parse_iso_ts(val: Any) -> float:
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _accumulate(m: ModelStats, d: Delta, now: float) -> None:
    m.last_seen = now
    if m.first_seen <= 0:
        m.first_seen = now

    m.requests += 1
    if d.ok:
        m.success += 1
    else:
        m.failed += 1

    if d.stream:
        m.streaming += 1

    if d.ttfb_ms > 0:
        m.ttfb_sum_ms += int(d.ttfb_ms)
        m.ttfb_count += 1

    if d.latency_ms > 0:
        lat = int(d.latency_ms)
        m.latency_sum_ms += lat
        m.latency_count += 1
        if d.ttfb_ms > 0 and d.latency_ms > d.ttfb_ms:
            m.gen_sum_ms += int(d.latency_ms - d.ttfb_ms)
            m.gen_count += 1

    if d.has_usage:
        m.usage_reported += 1
        m.prompt_tokens += d.prompt_tokens
        m.completion_tokens += d.completion_tokens
        m.total_tokens += d.total_tokens
        m.cache_hit_tokens += d.cache_hit_tokens
        m.cache_miss_tokens += d.cache_miss_tokens
        m.cache_write_tokens += d.cache_write_tokens
        m.cache_read_tokens += d.cache_read_tokens
        m.cache_creation_tokens += d.cache_creation_tokens

    if d.credit != 0:
        m.credit_milli += int(d.credit * 1000 + 0.5)


def _add_into(dst: ModelStats, src: ModelStats) -> None:
    dst.requests += src.requests
    dst.success += src.success
    dst.failed += src.failed
    dst.streaming += src.streaming
    dst.ttfb_sum_ms += src.ttfb_sum_ms
    dst.ttfb_count += src.ttfb_count
    dst.latency_sum_ms += src.latency_sum_ms
    dst.latency_count += src.latency_count
    dst.prompt_tokens += src.prompt_tokens
    dst.completion_tokens += src.completion_tokens
    dst.total_tokens += src.total_tokens
    dst.usage_reported += src.usage_reported
    dst.cache_hit_tokens += src.cache_hit_tokens
    dst.cache_miss_tokens += src.cache_miss_tokens
    dst.cache_write_tokens += src.cache_write_tokens
    dst.cache_read_tokens += src.cache_read_tokens
    dst.cache_creation_tokens += src.cache_creation_tokens
    dst.credit_milli += src.credit_milli
    dst.gen_sum_ms += src.gen_sum_ms
    dst.gen_count += src.gen_count

    if dst.first_seen <= 0 or (src.first_seen > 0 and src.first_seen < dst.first_seen):
        dst.first_seen = src.first_seen
    if src.last_seen > dst.last_seen:
        dst.last_seen = src.last_seen


def _bucket_key(ts: float) -> str:
    """本地时间格式化为 '2006-01-02T15'。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H")


def _parse_bucket_key(k: str) -> float:
    """解析本地时间桶键。"""
    return datetime.strptime(k, "%Y-%m-%dT%H").timestamp()


def _group_key(ts: float, interval: str) -> tuple[str, float, float]:
    """把时刻按粒度归组，返回组键与组的起止时间（本地时区）。"""
    dt = datetime.fromtimestamp(ts)
    if interval == "day":
        start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.strftime("%Y-%m-%d"), start.timestamp(), end.timestamp()
    elif interval == "week":
        offset = dt.weekday()  # 周一为 0
        start = (dt - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        iso_year, iso_week, _ = start.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}", start.timestamp(), end.timestamp()
    else:  # hour
        start = dt.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        return start.strftime("%Y-%m-%dT%H"), start.timestamp(), end.timestamp()


def derive(m: ModelStats) -> dict[str, Any]:
    """把累计统计换算成派生指标（与 Go Derived 字段保持一致）。"""
    cache_hit = m.cache_hit_tokens + m.cache_read_tokens
    cache_miss = m.cache_miss_tokens
    cache_write = m.cache_write_tokens + m.cache_creation_tokens
    credit = float(m.credit_milli) / 1000.0

    avg_ttfb_ms = float(m.ttfb_sum_ms) / float(m.ttfb_count) if m.ttfb_count > 0 else 0.0
    avg_latency_ms = (
        float(m.latency_sum_ms) / float(m.latency_count) if m.latency_count > 0 else 0.0
    )

    tokens_per_sec = 0.0
    if m.gen_count > 0 and m.gen_sum_ms > 0:
        tokens_per_sec = float(m.completion_tokens) / (float(m.gen_sum_ms) / 1000.0)
    elif m.latency_sum_ms > 0:
        tokens_per_sec = float(m.completion_tokens) / (float(m.latency_sum_ms) / 1000.0)

    cache_hit_rate = 0.0
    if cache_hit + cache_miss > 0:
        cache_hit_rate = float(cache_hit) / float(cache_hit + cache_miss)

    credit_per_req = credit / float(m.requests) if m.requests > 0 else 0.0
    last_seen_str = iso(m.last_seen) if m.last_seen > 0 else None

    return {
        "model": m.model,
        "requests": m.requests,
        "success": m.success,
        "failed": m.failed,
        "streaming": m.streaming,
        "avg_ttfb_ms": avg_ttfb_ms,
        "avg_latency_ms": avg_latency_ms,
        "tokens_per_sec": tokens_per_sec,
        "prompt_tokens": m.prompt_tokens,
        "completion_tokens": m.completion_tokens,
        "total_tokens": m.total_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_write_tokens": cache_write,
        "cache_hit_rate": cache_hit_rate,
        "credit": credit,
        "credit_per_req": credit_per_req,
        "last_seen": last_seen_str,
    }


class Collector:
    """线程安全的统计收集器。"""

    def __init__(self, state_file: str = "") -> None:
        self._lock = threading.Lock()
        self._models: dict[str, ModelStats] = {}
        self._series: dict[str, dict[str, ModelStats]] = {}
        self._since: float = time.time()
        self._state_file = state_file
        self._dirty = False
        self._flush_every = 20
        self._since_flush = 0
        self._retention = DEFAULT_RETENTION_SECONDS
        if self._state_file:
            self._load()

    def set_retention(self, seconds: float) -> None:
        """设置时间序列保留时长。"""
        with self._lock:
            self._retention = float(seconds)

    def record(self, delta: Delta) -> None:
        """记录一次请求。"""
        model = delta.model or "(unknown)"
        now = time.time()

        with self._lock:
            # 1) 累计
            m = self._models.get(model)
            if not m:
                m = ModelStats(model=model, first_seen=now)
                self._models[model] = m
            _accumulate(m, delta, now)

            # 2) 小时桶
            key = _bucket_key(now)
            bucket = self._series.get(key)
            if bucket is None:
                bucket = {}
                self._series[key] = bucket
            bm = bucket.get(model)
            if not bm:
                bm = ModelStats(model=model, first_seen=now)
                bucket[model] = bm
            _accumulate(bm, delta, now)

            # 3) 清理过期桶
            self._prune_locked(now)

            self._dirty = True
            self._since_flush += 1
            if self._state_file and self._since_flush >= self._flush_every:
                self._save_locked()

    def _prune_locked(self, now: float) -> None:
        ret = self._retention if self._retention > 0 else DEFAULT_RETENTION_SECONDS
        cutoff = now - ret
        to_del: list[str] = []
        for k in self._series:
            try:
                t = _parse_bucket_key(k)
                if t < cutoff:
                    to_del.append(k)
            except Exception:
                to_del.append(k)
        for k in to_del:
            self._series.pop(k, None)

    def prune(self) -> None:
        """手动清理过期桶。"""
        with self._lock:
            self._prune_locked(time.time())

    def series_buckets(self) -> int:
        """返回时间序列原始桶数。"""
        with self._lock:
            return len(self._series)

    def snapshot(self) -> dict[str, Any]:
        """返回当前累计统计快照。"""
        with self._lock:
            models_copy = {k: copy.deepcopy(v) for k, v in self._models.items()}
            total = ModelStats(model="(all)")
            for m in models_copy.values():
                _add_into(total, m)
            return {
                "models": models_copy,
                "total": total,
                "since": self._since,
                "now": time.time(),
            }

    def derived(self) -> dict[str, Any]:
        """生成面板派生视图。"""
        snap = self.snapshot()
        models_list: list[dict[str, Any]] = []
        for m in snap["models"].values():
            models_list.append(derive(m))

        models_list.sort(key=lambda d: (-d["requests"], d["model"]))
        now = snap["now"]
        since = snap["since"]
        uptime_sec = int(now - since) if since > 0 else 0

        return {
            "models": models_list,
            "total": derive(snap["total"]),
            "since": iso(since),
            "now": iso(now),
            "uptime_sec": uptime_sec,
        }

    def range_query(
        self,
        from_ts: float | str | None = None,
        to_ts: float | str | None = None,
        interval: str = "hour",
        model: str = "",
    ) -> dict[str, Any]:
        """按时间范围聚合查询。"""
        from_sec = _parse_iso_ts(from_ts)
        to_sec = _parse_iso_ts(to_ts)

        iv = interval.lower().strip()
        if iv in ("day", "daily", "d"):
            iv = "day"
        elif iv in ("week", "weekly", "w"):
            iv = "week"
        else:
            iv = "hour"

        with self._lock:
            groups: dict[str, dict[str, Any]] = {}
            model_set: set[str] = set()

            for key, bucket in self._series.items():
                try:
                    t = _parse_bucket_key(key)
                except Exception:
                    continue

                if from_sec > 0 and t < from_sec:
                    continue
                if to_sec > 0 and t >= to_sec:
                    continue

                for m_name, ms in bucket.items():
                    if model and m_name != model:
                        continue
                    model_set.add(m_name)
                    gk, gstart, gend = _group_key(t, iv)
                    g = groups.get(gk)
                    if not g:
                        g = {"start": gstart, "end": gend, "models": {}}
                        groups[gk] = g
                    dst = g["models"].get(m_name)
                    if not dst:
                        dst = ModelStats(model=m_name)
                        g["models"][m_name] = dst
                    _add_into(dst, ms)

            # 转换为有序数据点
            points: list[dict[str, Any]] = []
            total_stats = ModelStats(model="(all)")
            for gk, g in groups.items():
                merged = ModelStats(model="(all)")
                for ms in g["models"].values():
                    _add_into(merged, ms)
                _add_into(total_stats, merged)
                points.append(
                    {
                        "key": gk,
                        "start": iso(g["start"]),
                        "end": iso(g["end"]),
                        "start_ts": g["start"],
                        "end_ts": g["end"],
                        "stats": merged.to_dict(),
                        "derived": derive(merged),
                    }
                )

            points.sort(key=lambda p: p["start_ts"])
            start_str = points[0]["start"] if points else (iso(from_sec) if from_sec > 0 else "")
            end_str = points[-1]["end"] if points else (iso(to_sec) if to_sec > 0 else "")

            # 清除中间辅助属性
            for p in points:
                p.pop("start_ts", None)
                p.pop("end_ts", None)

            return {
                "interval": iv,
                "from": start_str,
                "to": end_str,
                "points": points,
                "total": derive(total_stats),
                "models": sorted(list(model_set)),
            }

    def reset(self) -> None:
        """清空统计。"""
        with self._lock:
            self._models.clear()
            self._series.clear()
            self._since = time.time()
            self._dirty = True
            self._save_locked()

    def flush(self) -> None:
        """强制落盘。"""
        with self._lock:
            if self._dirty:
                self._save_locked()

    def _save_locked(self) -> None:
        self._dirty = False
        self._since_flush = 0
        if not self._state_file:
            return

        doc = {
            "since": iso(self._since),
            "models": {k: v.to_dict() for k, v in self._models.items()},
            "series": {
                k: {m_k: m_v.to_dict() for m_k, m_v in b.items()} for k, b in self._series.items()
            },
            "retention_sec": int(self._retention),
        }
        raw = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        target = Path(self._state_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp")

        try:
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
        except Exception:
            pass

    def _load(self) -> None:
        if not self._state_file:
            return
        p = Path(self._state_file)
        if not p.exists():
            return
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            return

        if not isinstance(data, dict):
            return

        with self._lock:
            since = data.get("since")
            if since:
                self._since = _parse_iso_ts(since)

            models_data = data.get("models")
            if isinstance(models_data, dict):
                for k, v in models_data.items():
                    if isinstance(v, dict):
                        self._models[k] = ModelStats.from_dict(v)

            series_data = data.get("series")
            if isinstance(series_data, dict):
                for k, b in series_data.items():
                    if isinstance(b, dict):
                        bucket: dict[str, ModelStats] = {}
                        for m_k, m_v in b.items():
                            if isinstance(m_v, dict):
                                bucket[m_k] = ModelStats.from_dict(m_v)
                        self._series[k] = bucket

            self._prune_locked(time.time())
