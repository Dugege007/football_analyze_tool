"""双向自检：python -m pytest tests/test_water.py -q（或直接 python tests/test_water.py 打印表）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.water import is_censored_tier, tier_to_mid, water_to_tier  # noqa: E402

TIERS = [i / 2 for i in range(0, 21)]  # 0, 0.5, ..., 10
BOUNDARY = [0.60, 0.69, 0.70, 0.7124, 0.7125, 0.7126, 0.725, 0.7375, 0.925, 0.9374, 0.9375,
            0.95, 1.00, 1.1875, 1.19, 1.20, 1.21, 1.25]
EXPECT_BOUNDARY = {0.60: 0.0, 0.69: 0.0, 0.70: 0.0, 0.7124: 0.0, 0.7125: 0.5, 0.7126: 0.5,
                   0.725: 0.5, 0.7375: 1.0, 0.925: 4.5, 0.9374: 4.5, 0.9375: 5.0, 0.95: 5.0,
                   1.00: 6.0, 1.1875: 10.0, 1.19: 10.0, 1.20: 10.0, 1.21: 10.0, 1.25: 10.0}


def test_named_points():
    assert [tier_to_mid(t) for t in (4.5, 5, 5.5, 6, 6.5)] == [0.925, 0.95, 0.975, 1.0, 1.025]
    assert tier_to_mid(0) == 0.70 and tier_to_mid(10) == 1.20


def test_roundtrip_all_half_steps():
    for t in TIERS:
        assert water_to_tier(tier_to_mid(t)) == t, t


def test_boundaries():
    for w, t in EXPECT_BOUNDARY.items():
        assert water_to_tier(w) == t, (w, water_to_tier(w), t)


def test_censored():
    assert is_censored_tier(0) and is_censored_tier(10)
    assert not is_censored_tier(0.5) and not is_censored_tier(9.5)


if __name__ == "__main__":
    print("t -> mid -> t")
    for t in TIERS:
        m = tier_to_mid(t)
        print(f"  {t:>4} -> {m:.4f} -> {water_to_tier(m):>4}  {'censored' if is_censored_tier(t) else ''}")
    print("boundary water -> tier")
    for w in BOUNDARY:
        print(f"  {w:<7} -> {water_to_tier(w)}")
    for fn in (test_named_points, test_roundtrip_all_half_steps, test_boundaries, test_censored):
        fn()
    print("ALL OK")
