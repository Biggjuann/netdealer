"""Unit tests for the 0DTE SPX EOD-Pin trade plan — pure logic, no network.

Levels are computed natively on the SPX chain (volume-driven), so the plan
takes a NetDealerResult already in SPX points — no SPY, no ×10 basis.
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.net_dealer import (NetDealerResult, StrikeRow, max_pain_volume,
                            peak_volume_strike)
from app.trade_plan import assemble_plan, eod_state

UTC = dt.timezone.utc
# 2026-09-22 is a normal trading Tuesday (EDT, UTC-4); 16:00 ET close = 20:00 UTC.
ARMED = dt.datetime(2026, 9, 22, 19, 50, tzinfo=UTC)    # 15:50 ET → 10 min to close
MIDDAY = dt.datetime(2026, 9, 22, 17, 0, tzinfo=UTC)     # 13:00 ET → not armed
AFTER = dt.datetime(2026, 9, 22, 20, 30, tzinfo=UTC)     # 16:30 ET → closed
WEEKEND = dt.datetime(2026, 9, 19, 19, 50, tzinfo=UTC)   # Saturday


def _spx_res(spx_spot=7765.0, c_target=7731.0, **over):
    """A native-SPX dealer read: C below spot (ceiling) → ATM PUT to ride down."""
    base = dict(
        ticker="$SPX", expiry="2026-09-22", spot=spx_spot, dte=0.0,
        c_target=c_target, c_netdir=0.0, max_pain=7730.0, call_wall=7775.0, put_wall=7700.0,
        long_avg=7788.0, short_avg=7748.0, total_call_oi=0.0, total_put_oi=0.0,
        total_call_volume=1e6, total_put_volume=1e6, volume_bias="DOWN",
        sweet_spot_low=min(c_target, 7748.0), sweet_spot_high=max(c_target, 7748.0),
        atm_iv=0.11, confidence=85.0,
    )
    base.update(over)
    return NetDealerResult(**base)


def _spx_rows(spx_spot=7765.0):
    rows = []
    for k in range(7690, 7811, 5):
        put_intr = max(k - spx_spot, 0.0)
        call_intr = max(spx_spot - k, 0.0)
        # Volume peaks at the wall strikes so the volume-wall helpers are testable.
        cv = 12000 if k == 7775 else 6000
        pv = 12000 if k == 7700 else 6000
        rows.append(StrikeRow(
            strike=float(k), call_oi=0.0, put_oi=0.0,
            call_volume=cv, put_volume=pv, call_iv=0.11, put_iv=0.11,
            call_ask=round(max(0.10, call_intr + 2.0), 2),
            put_ask=round(max(0.10, put_intr + 2.0), 2),
        ))
    return rows, spx_spot


def _plan(res=None, spx_spot=7765.0, now=ARMED):
    res = res or _spx_res(spx_spot=spx_spot)
    rows, s = _spx_rows(spx_spot)
    return assemble_plan(res, s, rows, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0, now=now)


def test_volume_walls_and_max_pain():
    rows, _ = _spx_rows()
    assert peak_volume_strike(rows, "call") == 7775.0     # matches the live chart
    assert peak_volume_strike(rows, "put") == 7700.0
    assert max_pain_volume(rows) is not None
    print("ok  volume walls: call 7775 / put 7700 (from live volume, not OI)")


def test_eod_state_arm_window():
    assert eod_state(ARMED)["armed"] is True
    assert eod_state(MIDDAY)["armed"] is False and eod_state(MIDDAY)["is_open"] is True
    assert eod_state(AFTER)["after_close"] is True
    assert eod_state(WEEKEND)["trading_day"] is False
    print("ok  arm window: armed@15:50, wait@13:00, closed@16:30, MARKET CLOSED Sat")


def test_native_levels_no_conversion():
    p = _plan()
    L = p["levels"]
    assert L["spot"] == 7765.0 and L["c"] == 7731.0     # straight from the SPX read
    assert L["call_wall"] == 7775.0 and L["put_wall"] == 7700.0
    assert "basis" not in L                              # no SPY→SPX conversion
    print(f"ok  native SPX levels: C={L['c']} call_wall={L['call_wall']} (no basis)")


def test_armed_c_below_spot_takes_atm_put():
    p = _plan()
    assert p["side"] == "PUT" and p["action"] == "BUY ATM PUT" and p["signal"] == "TAKE"
    assert p["plan"]["target"] == 7731.0 and p["plan"]["edge_pts"] > 0
    assert "whichever first" in p["plan"]["exit"]
    print(f"ok  armed C<spot → TAKE BUY ATM PUT, target C={p['plan']['target']}")


def test_wall_pin_matches_real_trade():
    # Real setup: spot at the call wall 7775, C at 7766 → the 7775 PUT, ride to C.
    p = _plan(res=_spx_res(spx_spot=7775.0, c_target=7766.0), spx_spot=7775.0)
    assert p["side"] == "PUT" and p["levels"]["c"] == 7766.0
    c = p["contracts"][0]
    assert c["strike"] == 7775.0 and c["symbol"].endswith("P7775")
    assert c["value_at_c"] == 9.0                        # 7775 − 7766
    print(f"ok  wall pin: {c['symbol']} valC={c['value_at_c']}")


def test_side_follows_c_even_if_volume_disagrees():
    # C below spot → PUT, regardless of a conflicting volume skew (flagged only).
    p = _plan(res=_spx_res(volume_bias="UP"))
    assert p["side"] == "PUT" and p["contracts"][0]["side"] == "PUT"
    assert any("against the pin" in w for w in p["warnings"])
    print("ok  side follows C-vs-spot (PUT); conflicting volume skew flagged")


def test_exactly_one_atm_contract():
    cs = _plan()["contracts"]
    assert len(cs) == 1 and cs[0]["role"].startswith("ATM")
    assert cs[0]["side"] == "PUT" and cs[0]["strike"] == 7765.0   # nearest spot
    assert cs[0]["value_at_c"] > 0 and cs[0]["symbol"].startswith(".SPXW")
    print(f"ok  strictly ATM: {cs[0]['symbol']} ask {cs[0]['premium']} valC {cs[0]['value_at_c']}")


def test_disarmed_outside_window():
    assert _plan(now=MIDDAY)["signal"] == "WAIT"
    assert _plan(now=AFTER)["signal"] == "CLOSED"
    assert _plan(now=WEEKEND)["signal"] == "MARKET CLOSED"
    print("ok  disarmed: WAIT midday, CLOSED after 4pm, MARKET CLOSED on Saturday")


def test_no_payoff_stands_down_when_armed():
    # C only ~1 pt from spot → ATM premium exceeds the gap; a perfect pin loses.
    p = _plan(res=_spx_res(c_target=7764.0))
    assert p["plan"]["payoff_at_c"] <= 0 and p["signal"] == "STAND DOWN"
    print(f"ok  premium > gap → STAND DOWN (payoff {p['plan']['payoff_at_c']})")


def test_c_above_spot_takes_atm_call():
    p = _plan(res=_spx_res(spx_spot=7735.0, c_target=7771.0, volume_bias="UP"),
              spx_spot=7735.0)
    assert p["side"] == "CALL" and p["action"] == "BUY ATM CALL" and p["signal"] == "TAKE"
    assert p["contracts"][0]["side"] == "CALL"
    print(f"ok  C>spot → TAKE BUY ATM CALL, target C={p['plan']['target']}")


def test_reachability_read():
    far = _plan(res=_spx_res(c_target=7731.0))            # ~34 pt to C
    assert far["reachable"] is False and any("reach C" in w for w in far["warnings"])
    assert far["signal"] == "TAKE"
    near = _plan(res=_spx_res(c_target=7751.0))           # ~14 pt to C
    assert near["reachable"] is True
    print(f"ok  reachability: far={far['reach_sigma']}σ (warn), near={near['reach_sigma']}σ (ok)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} trade-plan tests passed.")


if __name__ == "__main__":
    main()
