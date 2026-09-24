"""官方价格表计算测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wb2api.pricing import ModelPrice, PricingTable, Usage, normalize


def test_normalize() -> None:
    assert normalize("DeepSeek-V4.1-Flash") == "deepseekv41flash"
    assert normalize("deepseek_v4_pro") == "deepseekv4pro"
    assert normalize(" deepseek . flash ") == "deepseekflash"


def test_default_pricing_table() -> None:
    table = PricingTable()
    price, ok = table.resolve("deepseek-v4.1-flash")
    assert ok
    assert price.cached_input == 0.04
    assert price.miss_input == 2.0
    assert price.output == 8.0
    assert price.off_peak_ratio == 0.5

    pro_price, ok = table.resolve("deepseek-v4-pro")
    assert ok
    assert pro_price.cached_input == 0.30
    assert pro_price.miss_input == 9.0
    assert pro_price.output == 27.0
    assert pro_price.off_peak_ratio == 0.5


def test_resolve_normalized_matching() -> None:
    table = PricingTable()
    # 变体名字也能解析
    p1, ok1 = table.resolve("DeepSeek-V4.1-Flash")
    assert ok1
    assert p1.cached_input == 0.04

    p2, ok2 = table.resolve("deepseek_v4.1_flash")
    assert ok2

    p3, ok3 = table.resolve("non-existent-model")
    assert not ok3
    assert not p3.priced()


def test_compute_peak_and_offpeak() -> None:
    table = PricingTable()
    # 1,000,000 cached hit, 1,000,000 miss, 1,000,000 output
    usage = Usage(
        prompt_tokens=2_000_000,
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        completion_tokens=1_000_000,
    )

    # 高峰期：
    # cached_input: 1M * 0.04 = 0.04
    # miss_input: 1M * 2.0 = 2.00
    # output: 1M * 8.0 = 8.00
    # total: 10.04
    cost_peak = table.compute("deepseek-v4.1-flash", usage, mode="peak")
    assert cost_peak.priced
    assert cost_peak.cached_input_tokens == 1_000_000
    assert cost_peak.miss_input_tokens == 1_000_000
    assert cost_peak.output_tokens == 1_000_000
    assert pytest.approx(cost_peak.cached_input_cost) == 0.04
    assert pytest.approx(cost_peak.miss_input_cost) == 2.00
    assert pytest.approx(cost_peak.output_cost) == 8.00
    assert pytest.approx(cost_peak.total) == 10.04

    # 空闲期（半价）：
    # cached_input: 0.02
    # miss_input: 1.00
    # output: 4.00
    # total: 5.02
    cost_offpeak = table.compute("deepseek-v4.1-flash", usage, mode="offpeak")
    assert cost_offpeak.priced
    assert pytest.approx(cost_offpeak.cached_input_cost) == 0.02
    assert pytest.approx(cost_offpeak.miss_input_cost) == 1.00
    assert pytest.approx(cost_offpeak.output_cost) == 4.00
    assert pytest.approx(cost_offpeak.total) == 5.02


def test_compute_miss_fallback() -> None:
    table = PricingTable()
    # 上游未明确上报 miss（cache_miss_tokens=0），但 prompt=10,000, hit=3,000
    # 回退值 = prompt - hit = 7,000 > miss(0)
    usage = Usage(
        prompt_tokens=10_000,
        cache_hit_tokens=3_000,
        cache_miss_tokens=0,
        completion_tokens=1_000,
    )
    cost = table.compute("deepseek-flash", usage, mode="peak")
    assert cost.cached_input_tokens == 3_000
    assert cost.miss_input_tokens == 7_000
    assert cost.output_tokens == 1_000

    # 3,000 / 1M * 0.04 = 0.00012
    # 7,000 / 1M * 2.0 = 0.014
    # 1,000 / 1M * 8.0 = 0.008
    # total = 0.02212
    assert pytest.approx(cost.cached_input_cost) == 0.00012
    assert pytest.approx(cost.miss_input_cost) == 0.014
    assert pytest.approx(cost.output_cost) == 0.008
    assert pytest.approx(cost.total) == 0.02212


def test_table_set_delete_and_persistence(tmp_path: Path) -> None:
    pricing_file = tmp_path / "pricing.json"
    table = PricingTable(str(pricing_file))

    # 添加自定义模型
    table.set(
        "custom-model",
        ModelPrice(
            cached_input=0.1,
            miss_input=1.0,
            output=3.0,
            off_peak_ratio=0.8,
            note="自定义测试模型",
        ),
    )
    p, ok = table.resolve("custom-model")
    assert ok
    assert p.cached_input == 0.1

    # 保存到磁盘
    table.save()
    assert pricing_file.exists()

    # 重新加载
    table2 = PricingTable(str(pricing_file))
    p2, ok2 = table2.resolve("custom-model")
    assert ok2
    assert p2.cached_input == 0.1
    assert p2.note == "自定义测试模型"

    # 删除
    table2.delete("custom-model")
    _, ok_deleted = table2.resolve("custom-model")
    assert not ok_deleted

    # unpriced
    unpriced_list = table2.unpriced(["custom-model", "deepseek-flash"])
    assert unpriced_list == ["custom-model"]
