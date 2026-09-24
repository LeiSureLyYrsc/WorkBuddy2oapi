"""账号池单测：覆盖加权挑号、LRU 兜底、全冷却兜底、熔断退避、软退避、模型级豁免、在途租约、状态持久化等。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from wb2api.models import Account
from wb2api.pool import AccountPool


class FakeClock:
    """可步进的测试时钟。"""

    def __init__(self, start: float = 1700000000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_add_and_sync_from_dir(tmp_path: Path) -> None:
    clock = FakeClock()
    state_file = str(tmp_path / "state.json")
    pool = AccountPool(state_file=state_file, now_fn=clock)

    acc1 = Account(uid="u1", nickname="Alice", access_token="t1")
    acc2 = Account(uid="u2", nickname="Bob", access_token="t2")
    pool.add(acc1)
    pool.add(acc2)

    assert pool.auth_by_uid("u1") is acc1
    assert pool.peek_by_uid("u2") is acc2
    assert pool.available_uids() == ["u1", "u2"]

    # sync_from_dir: 删除 u1，保留 u2，新增 u3
    acc3 = Account(uid="u3", nickname="Charlie", access_token="t3")
    pool.sync_from_dir([acc2, acc3])
    assert pool.auth_by_uid("u1") is None
    assert pool.auth_by_uid("u2") is not None
    assert pool.auth_by_uid("u3") is not None
    assert pool.available_uids() == ["u2", "u3"]


def test_pick_weighted_top5_and_lru() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(now_fn=clock, min_pick_gap=0.1)
    # 创建 5 个账号，u5 积分最高，u1 积分最低
    for i in range(1, 6):
        uid = f"u{i}"
        pool.add(Account(uid=uid, nickname=f"User{i}"))
        pool.set_credits(uid, i * 10)

    # 强制让 rand_fn 选第 0 个（最大权重）
    pool.set_random_source(lambda n: 0)

    # 首次抽取，全员未使用过（idle 满分），但 u5 credits 最高，所以权重第一
    chosen = pool.pick()
    assert chosen is not None
    assert chosen.uid == "u5"
    st5 = pool.status("u5")
    assert st5 is not None
    assert st5.uid == "u5"

    # 依次抽取剩余账号，分别记录 last_used
    for expected in ["u4", "u3", "u2", "u1"]:
        c = pool.pick()
        assert c is not None
        assert c.uid == expected

    # 现在 5 个账号都刚刚被用过（last_used 都在 1700000000.0，且 now - last_used < 0.1s）
    # 推进时钟 0.01s（仍 < 0.1s），eligible 集合为空，触发 Top5 内的 LRU 兜底
    # 由于 u5 最早被 pick（最早被赋予 last_used），其 last_used <= 其他账号
    # LRU 兜底应选出 u5
    clock.advance(0.01)
    chosen_lru = pool.pick()
    assert chosen_lru is not None
    assert chosen_lru.uid == "u5"


def test_earliest_expiry_fallback() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(now_fn=clock)

    acc1 = Account(uid="u1")
    acc2 = Account(uid="u2")
    acc3 = Account(uid="u3_hard")
    acc4 = Account(uid="u4_disabled")

    pool.add(acc1)
    pool.add(acc2)
    pool.add(acc3)
    pool.add(acc4)

    # u1: soft cooldown 300s
    pool.cooldown("u1", "soft_rate", 300, "soft 429")
    # u2: soft cooldown 100s (最早恢复)
    pool.cooldown("u2", "soft_rate", 100, "soft 429")
    # u3: hard_credit cooldown 50s (硬冷却不得兜底)
    pool.cooldown("u3_hard", "hard_credit", 50, "balance 0")
    # u4: disabled
    pool.disable("u4_disabled", "session dead")

    # 没有 healthy 候选，触发 earliest expiry 兜底
    # 应该选 u2 (exp = now + 100s)，跳过 u3_hard 和 u4_disabled
    chosen = pool.pick()
    assert chosen is not None
    assert chosen.uid == "u2"

    # 如果 tried 排除 u2，则兜底选 u1
    chosen_tried = pool.pick_excluding({"u2"})
    assert chosen_tried is not None
    assert chosen_tried.uid == "u1"

    # 如果排除 u1 和 u2，兜底找不到任何合法账号，返回 None
    assert pool.pick_excluding({"u1", "u2"}) is None


def test_soft_cooldown_exponential_backoff_and_cap() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(now_fn=clock, soft_rate_max=3600.0)
    pool.add(Account(uid="u1"))

    base = 600.0
    # 第 1 次 soft 冷却：streak=1 -> 600s
    pool.cooldown("u1", "soft_rate", base, "rate 1")
    st = pool.status("u1")
    assert st is not None
    assert st.cooling is True
    assert st.soft_streak == 1
    assert st.cool_remaining_sec == 600

    # 第 2 次 soft 冷却：streak=2 -> 1200s
    pool.cooldown("u1", "soft_rate", base, "rate 2")
    st = pool.status("u1")
    assert st is not None
    assert st.soft_streak == 2
    assert st.cool_remaining_sec == 1200

    # 第 3 次 soft 冷却：streak=3 -> 2400s
    pool.cooldown("u1", "soft_rate", base, "rate 3")
    st = pool.status("u1")
    assert st is not None
    assert st.soft_streak == 3
    assert st.cool_remaining_sec == 2400

    # 第 4 次 soft 冷却：streak=4 -> 4800s，超过 soft_rate_max 3600s，封顶 3600s
    pool.cooldown("u1", "soft_rate", base, "rate 4")
    st = pool.status("u1")
    assert st is not None
    assert st.soft_streak == 4
    assert st.cool_remaining_sec == 3600


def test_breaker_exponential_backoff() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(
        now_fn=clock,
        breaker_threshold=3,
        breaker_cooldown=1800.0,
        breaker_cooldown_max=7200.0,
    )
    pool.add(Account(uid="u1"))

    # 连续 2 次错误，未达阈值
    pool.note_error("u1")
    pool.note_error("u1")
    st = pool.status("u1")
    assert st is not None
    assert st.breaker_fails == 2
    assert st.breaker_until == ""
    assert st.cooling is False

    # 第 3 次错误，触发熔断：退避 1800s
    pool.note_error("u1")
    st = pool.status("u1")
    assert st is not None
    assert st.breaker_fails == 0
    assert st.cooling is True
    assert st.breaker_until != ""

    # 再来 3 次错误：第 2 次熔断，退避 3600s
    pool.note_error("u1")
    pool.note_error("u1")
    pool.note_error("u1")
    assert pool._by_uid["u1"].retry_count == 2
    assert pool._by_uid["u1"].breaker_until == clock.current + 3600.0

    # 再来 3 次错误：退避 7200s (达上限)
    pool.note_error("u1")
    pool.note_error("u1")
    pool.note_error("u1")
    assert pool._by_uid["u1"].breaker_until == clock.current + 7200.0

    # 再来 3 次错误：封顶 7200s
    pool.note_error("u1")
    pool.note_error("u1")
    pool.note_error("u1")
    assert pool._by_uid["u1"].breaker_until == clock.current + 7200.0


def test_hard_cooldown_to_4am() -> None:
    # 构造一个本地时间凌晨 2 点的时间戳
    now_dt = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0)
    clock = FakeClock(now_dt.timestamp())
    pool = AccountPool(now_fn=clock)
    pool.add(Account(uid="u1"))

    pool.cooldown_until_tomorrow_4am("u1", "credit exhausted")
    st = pool.status("u1")
    assert st is not None
    assert st.cooling is True
    assert st.cool_kind == "hard_credit"
    # 当天 2 点到当天 4 点，差 2 小时 = 7200 秒
    assert st.cool_remaining_sec == 7200

    # 构造一个本地时间上午 10 点的时间戳
    now_dt2 = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    clock2 = FakeClock(now_dt2.timestamp())
    pool2 = AccountPool(now_fn=clock2)
    pool2.add(Account(uid="u2"))

    pool2.cooldown_until_tomorrow_4am("u2", "credit exhausted")
    st2 = pool2.status("u2")
    assert st2 is not None
    # 上午 10 点到次日 4 点，差 18 小时 = 18 * 3600 = 64800 秒
    assert st2.cool_remaining_sec == 18 * 3600


def test_session_dead_12153_threshold() -> None:
    pool = AccountPool()
    pool.add(Account(uid="u1"))

    assert pool.note_session_dead("u1") is False
    st = pool.status("u1")
    assert st is not None and st.disabled is False

    assert pool.note_session_dead("u1") is False
    st = pool.status("u1")
    assert st is not None and st.disabled is False

    # 达到 3 次阈值，判定 session 死亡，禁用账号
    assert pool.note_session_dead("u1") is True
    st = pool.status("u1")
    assert st is not None
    assert st.disabled is True
    assert st.disabled_reason == "12153 session dead"

    # 手工复活
    pool.revive_disabled("u1")
    st = pool.status("u1")
    assert st is not None
    assert st.disabled is False
    assert st.disabled_reason == ""

    # 测试 clear_session_dead 中途清零
    pool.note_session_dead("u1")
    pool.note_session_dead("u1")
    pool.clear_session_dead("u1")
    # 清零后再来一次不会触发禁用
    assert pool.note_session_dead("u1") is False
    st = pool.status("u1")
    assert st is not None and st.disabled is False


def test_reenable_if_credits() -> None:
    clock = FakeClock()
    pool = AccountPool(now_fn=clock)
    pool.add(Account(uid="u1"))

    pool.cooldown("u1", "hard_credit", 3600, "out of credits")
    st = pool.status("u1")
    assert st is not None
    assert st.cooling is True

    # 余额仍为 0 时不解冻
    pool.reenable_if_credits("u1", 0)
    st = pool.status("u1")
    assert st is not None
    assert st.cooling is True
    assert st.credits == 0

    # 余额 > 0 时解冻冷却
    pool.reenable_if_credits("u1", 100)
    st = pool.status("u1")
    assert st is not None
    assert st.cooling is False
    assert st.credits == 100

    # 如果已被禁用，即使有余额也不解冻
    pool.disable("u1", "banned")
    pool.cooldown("u1", "hard_credit", 3600, "out of credits")
    pool.reenable_if_credits("u1", 500)
    st = pool.status("u1")
    assert st is not None
    assert st.disabled is True
    assert st.credits == 500


def test_note_success_clears_failures_and_streaks() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(now_fn=clock)
    pool.add(Account(uid="u1"))

    # 累加各种失败状态
    pool.note_error("u1")
    pool.note_error("u1")
    pool.note_error("u1")  # 触发熔断
    pool.cooldown("u1", "soft_rate", 600, "429")  # soft_streak 增加
    pool.note_session_dead("u1")  # sessionDeadFails 增加

    entry = pool._by_uid["u1"]
    assert entry.retry_count > 0
    assert entry.soft_streak > 0
    assert entry.session_dead_fails > 0
    assert entry.breaker_until > 0

    # 成功请求清除熔断与连续失败
    pool.note_success("u1")
    assert entry.success_count == 1
    assert entry.fails == 0
    assert entry.retry_count == 0
    assert entry.breaker_until == 0.0
    assert entry.soft_streak == 0
    assert entry.session_dead_fails == 0


def test_model_level_soft_cooldown_exemption() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(now_fn=clock)
    pool.set_random_source(lambda n: 0)
    pool.add(Account(uid="u1"))
    pool.set_credits("u1", 100)
    pool.add(Account(uid="u2"))
    pool.set_credits("u2", 10)

    # u1 触发针对 model-A 的 429 冷却（带 reset_at）
    pool.cooldown_soft_for_model(
        uid="u1",
        base_seconds=600,
        reset_at_epoch=clock.current + 300,
        model="model-A",
        reason="upstream 6004",
    )

    m, is_active = pool.model_soft_cooldown("u1")
    assert is_active is True
    assert m == "model-A"

    # 对 model-A：u1 处于软冷却不可选，只能选出 u2
    picked_a = pool.pick_excluding_for_model(None, "model-A")
    assert picked_a is not None
    assert picked_a.uid == "u2"

    # 对 model-B：u1 获得软冷却豁免！且 u1 积分高于 u2，选出 u1
    picked_b = pool.pick_excluding_for_model(None, "model-B")
    assert picked_b is not None
    assert picked_b.uid == "u1"

    # 但如果账号被熔断或禁用，模型豁免不得穿透
    pool.disable("u1", "manual disable")
    picked_after_disable = pool.pick_excluding_for_model(None, "model-B")
    assert picked_after_disable is not None
    assert picked_after_disable.uid == "u2"


def test_in_flight_lease() -> None:
    pool = AccountPool(max_in_flight=2)
    pool.add(Account(uid="u1"))

    assert pool.acquire("u1") is True  # 1
    assert pool.acquire("u1") is True  # 2
    assert pool.acquire("u1") is False  # 满载，拒绝

    # 满载账号不参与 pick
    assert pool.pick() is None
    assert pool.servable_now() is False

    # 释放一个名额
    pool.release("u1")
    assert pool.servable_now() is True
    picked = pool.pick()
    assert picked is not None
    assert picked.uid == "u1"

    # 多次 release 幂等底限为 0
    pool.release("u1")
    pool.release("u1")
    pool.release("u1")
    st_u1 = pool.status("u1")
    assert st_u1 is not None and st_u1.in_flight == 0


def test_state_persist_and_reload(tmp_path: Path) -> None:
    state_file = str(tmp_path / "state.json")
    clock = FakeClock(1700000000.0)

    pool1 = AccountPool(state_file=state_file, now_fn=clock)
    acc = Account(uid="u1", nickname="Tester")
    pool1.add(acc)
    pool1.set_credits("u1", 1234)
    pool1.cooldown("u1", "soft_rate", 500, "rate limit")
    pool1.note_error("u1")
    pool1.flush()
    pool1.stop_flusher()

    # 读取 json 内容校验
    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "u1" in data["accounts"]
    raw_u1 = data["accounts"]["u1"]
    assert raw_u1["credits"] == 1234
    assert raw_u1["cool_kind"] == "soft_rate"
    assert raw_u1["err_total"] == 1
    assert raw_u1["soft_streak"] == 1

    # 新建 pool2 恢复
    pool2 = AccountPool(state_file=state_file, now_fn=clock)
    st = pool2.status("u1")
    assert st is not None
    assert st.credits == 1234
    assert st.cool_kind == "soft_rate"
    assert st.err_total == 1
    assert st.soft_streak == 1
    pool2.stop_flusher()


def test_legacy_state_file_migration(tmp_path: Path) -> None:
    state_file = tmp_path / "legacy_state.json"
    legacy_doc = {
        "accounts": {
            "u_legacy_0": {
                "credits": 50,
                "cool_kind": 0,  # 0 -> hard_credit
                "err_count": 8,  # err_count > err_total (2)
                "err_total": 2,
                "until": "2099-01-01T00:00:00Z",
            },
            "u_legacy_1": {
                "credits": 80,
                "cool_kind": 1,  # 1 -> soft_rate
                "err_count": 3,
                "err_total": 5,
                "until": "2099-01-01T00:00:00Z",
            },
        }
    }
    state_file.write_text(json.dumps(legacy_doc), encoding="utf-8")

    pool = AccountPool(state_file=str(state_file))
    st0 = pool.status("u_legacy_0")
    assert st0 is not None
    assert st0.cooling is True
    assert st0.cool_kind == "hard_credit"
    assert st0.err_total == 8
    assert pool._by_uid["u_legacy_0"].cool_kind == "hard_credit"

    st1 = pool.status("u_legacy_1")
    assert st1 is not None
    assert st1.cooling is True
    assert st1.cool_kind == "soft_rate"
    assert st1.err_total == 5
    assert pool._by_uid["u_legacy_1"].cool_kind == "soft_rate"
    pool.stop_flusher()


def test_counts_detailed_and_list() -> None:
    clock = FakeClock(1700000000.0)
    pool = AccountPool(max_in_flight=1, now_fn=clock)
    pool.add(Account(uid="u1"))
    pool.add(Account(uid="u2"))
    pool.add(Account(uid="u3"))
    pool.add(Account(uid="u4"))

    # u2: disabled
    pool.disable("u2", "banned")
    # u3: cooling
    pool.cooldown("u3", "soft_rate", 600, "rate")
    # u1: healthy but in-flight full
    pool.acquire("u1")

    # total=4, healthy=2 (u1, u4), cooling=1 (u3), disabled=1 (u2), in_flight_full=1 (u1)
    total, healthy, cooling, disabled, in_flight_full = pool.counts_detailed()
    assert total == 4
    assert healthy == 2
    assert cooling == 1
    assert disabled == 1
    assert in_flight_full == 1

    # list() 按 UID 排序
    st_list = pool.list()
    assert [s.uid for s in st_list] == ["u1", "u2", "u3", "u4"]
    assert pool.available_uids() == ["u4"]
