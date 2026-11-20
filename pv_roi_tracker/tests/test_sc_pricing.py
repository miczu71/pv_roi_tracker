"""zone_priced_self_consumption: autokonsumpcja PV wyceniana godzina po godzinie ceną strefy."""
from pv_roi_tracker.sc_pricing import zone_priced_self_consumption


def _price_map(prices):
    calls = []

    def price_at(hour):
        calls.append(hour)
        return prices.get(hour)

    return price_at, calls


def test_two_hours_different_zones_sum_kwh_times_price():
    produced = {'2026-07-01T10': 2.0, '2026-07-01T20': 1.0}
    exported = {'2026-07-01T10': 0.5, '2026-07-01T20': 0.0}
    price_at, _ = _price_map({'2026-07-01T10': 0.80, '2026-07-01T20': 0.60})
    kwh, pln = zone_priced_self_consumption(produced, exported, price_at)
    # 10h: 1.5 kWh × 0.80 = 1.20 ; 20h: 1.0 kWh × 0.60 = 0.60
    assert kwh == 2.5
    assert pln == 1.8


def test_export_greater_than_production_gives_zero_for_hour():
    produced = {'2026-07-01T12': 1.0}
    exported = {'2026-07-01T12': 3.0}
    price_at, calls = _price_map({'2026-07-01T12': 0.5})
    assert zone_priced_self_consumption(produced, exported, price_at) == (0.0, 0.0)
    assert calls == []


def test_monotonic_over_hour_prefixes():
    hours = [f'2026-07-01T{h:02d}' for h in range(24)]
    produced = {h: (1.0 if 8 <= int(h[-2:]) <= 16 else 0.0) for h in hours}
    exported = {h: 0.2 for h in hours}
    price_at, _ = _price_map({h: 0.7 for h in hours})
    last = -1.0
    for i in range(1, len(hours) + 1):
        prefix = hours[:i]
        p = {h: produced[h] for h in prefix}
        e = {h: exported[h] for h in prefix}
        result = zone_priced_self_consumption(p, e, price_at)
        assert result is not None
        assert result[1] >= last
        last = result[1]


def test_missing_price_for_hour_with_self_consumption_returns_none():
    produced = {'2026-07-01T10': 2.0, '2026-07-01T11': 2.0}
    exported = {'2026-07-01T10': 0.0, '2026-07-01T11': 0.0}
    price_at, _ = _price_map({'2026-07-01T10': 0.8})
    assert zone_priced_self_consumption(produced, exported, price_at) is None


def test_empty_produced_or_exported_returns_none():
    price_at, _ = _price_map({'2026-07-01T10': 0.8})
    assert zone_priced_self_consumption({}, {'2026-07-01T10': 0.0}, price_at) is None
    assert zone_priced_self_consumption({'2026-07-01T10': 1.0}, {}, price_at) is None


def test_hour_missing_in_exported_counts_as_zero_export():
    produced = {'2026-07-01T10': 2.0}
    exported = {'2026-07-01T09': 0.0}
    price_at, _ = _price_map({'2026-07-01T10': 0.5})
    assert zone_priced_self_consumption(produced, exported, price_at) == (2.0, 1.0)


def test_hours_without_self_consumption_do_not_call_price_at():
    produced = {'2026-07-01T10': 1.0, '2026-07-01T11': 1.0}
    exported = {'2026-07-01T10': 1.0, '2026-07-01T11': 5.0}
    price_at, calls = _price_map({})
    assert zone_priced_self_consumption(produced, exported, price_at) == (0.0, 0.0)
    assert calls == []
