"""Unit tests for the 0DTE SPX EOD-Pin trade plan — pure logic, no network."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.net_dealer import NetDealerResult, StrikeRow
from app.trade_plan import SPX_MULT, assemble_plan, eod_state, to_spx

UTC = dt.timezone.utc
# 2026-09-22 is a normal trading Tuesday (EDT, UTC-4); 16:00 ET close = 20:00 UTC.
ARMED = dt.datetime(2026, 9, 22, 19, 50, tzinfo=UTC)    # 15:50 ET → 10 min to close
MIDDAY = dt.datetime(2026, 9, 22, 17, 0, tzinfo=UTC)     # 13:00 ET → not armed
AFTER = dt.datetime(2026, 9, 22, 20, 30, tzinfo=UTC)     # 16:30 ET → closed
WEEKEND = dt.datetime(2026, 9, 19, 19, 50, tzinfo=UTC)   # Saturday
HALFDAY = dt.datetime(2026, 11, 27, 17, 50, tzinfo=UTC)  # Fri after Thanksgiving, 12:50 ET (EST)


def _spy_map(**over):
    """A DOWN-crush SPY map: C below spot, calls expensive → BUY ATM PUT."""
    base = dict(
        ticker="SPY", expiry="2026-09-22", spot=773.4, dte=0.0,
        c_target=770.0, c_netdir=0.0, max_pain=770.0, call_wall=776.0, put_wall=765.0,
        long_avg=774.0, short_avg=767.0, total_call_oi=1e6, total_put_oi=1e6,
        crush_direction="DOWN", expensive_side="CALL", trade_side="PUT",
        sweet_spot_low=767.0, sweet_spot_high=770.0, direction_agree=True,
        volume_bias="DOWN", volume_agree=True, pin_steepness=0.8,
        atm_iv=0.11, confidence=85.0,
    )
    base.update(over)
    return NetDealerResult(**base)


def _spx_rows(spx_spot=7765.0):
    rows = []
    for k in range(7690, 7811, 5):
        put_intr = max(k - spx_spot, 0.0)
        call_intr = max(spx_spot - k, 0.0)
        rows.append(StrikeRow(
            strike=float(k), call_oi=0.0, put_oi=0.0,
            call_volume=8000, put_volume=8000, call_iv=0.11, put_iv=0.11,
            call_ask=round(max(0.10, call_intr + 2.0), 2),
            put_ask=round(max(0.10, put_intr + 2.0), 2),
        ))
    return rows, spx_spot


def _plan(spy=None, spx_spot=7765.0, now=ARMED):
    spy = spy or _spy_map()
    rows, s = _spx_rows(spx_spot)
    return assemble_plan(spy, spy.spot, rows, s, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0, now=now)


def test_eod_state_arm_window():
    assert eod_state(ARMED)["armed"] is True
    assert eod_state(MIDDAY)["armed"] is False and eod_state(MIDDAY)["is_open"] is True
    assert eod_state(AFTER)["after_close"] is True
    assert eod_state(WEEKEND)["trading_day"] is False
    hd = eod_state(HALFDAY)                       # half-day close is 13:00 ET
    assert hd["armed"] is True and "half day" in hd["close_et"]
    print(f"ok  arm window: armed@15:50, wait@13:00, closed@16:30, half-day {hd['close_et']}")


def test_armed_c_below_spot_takes_atm_put():
    p = _plan()
    assert p["side"] == "PUT" and p["action"] == "BUY ATM PUT"
    assert p["signal"] == "TAKE", p["warnings"]
    assert p["plan"]["target"] == to_spx(770.0, p["levels"]["basis"])
    assert p["plan"]["edge_pts"] > 0
    assert "whichever first" in p["plan"]["exit"]
    print(f"ok  armed C<spot → TAKE BUY ATM PUT, target C={p['plan']['target']}")


def test_side_follows_c_not_crush():
    # The bug: SPY's crush read says UP, but C is BELOW spot. The pin target is
    # C, so the side must be a PUT (ride DOWN to C) — never a CALL. The crush
    # disagreement is flagged as a warning, not obeyed.
    spy = _spy_map(crush_direction="UP", expensive_side="PUT", volume_bias="UP",
                   c_target=770.0)                       # C below spot 773.4
    p = _plan(spy=spy)
    assert p["side"] == "PUT" and p["action"] == "BUY ATM PUT"
    assert p["contracts"][0]["side"] == "PUT"
    assert any("against the pin" in w for w in p["warnings"])
    print("ok  side follows C-vs-spot (PUT) even when crush says UP; disagreement flagged")


def test_wall_pin_matches_real_trade():
    # Real setup: spot at the call wall 7775, C at 7766 → buy the 7775 PUT, ride
    # to C, close at C. value at C = 7775 − 7766 = 9.
    spy = _spy_map(c_target=772.5)                        # → c_spx 7766 at spx_spot 7775
    p = _plan(spy=spy, spx_spot=7775.0)
    assert p["side"] == "PUT" and p["levels"]["c"] == 7766.0
    c = p["contracts"][0]
    assert c["strike"] == 7775.0 and c["symbol"].endswith("P7775")
    assert c["value_at_c"] == 9.0
    assert "whichever first" in p["plan"]["exit"]
    print(f"ok  wall pin: {c['symbol']} valC={c['value_at_c']} exit='{p['plan']['exit']}'")


def test_exactly_one_atm_contract():
    p = _plan()
    cs = p["contracts"]
    assert len(cs) == 1 and cs[0]["role"].startswith("ATM")
    assert cs[0]["side"] == "PUT" and cs[0]["strike"] == 7765.0   # nearest to spot
    assert cs[0]["value_at_c"] > 0 and cs[0]["symbol"].startswith(".SPXW")
    print(f"ok  strictly ATM: {cs[0]['symbol']} ask {cs[0]['premium']} valC {cs[0]['value_at_c']}")


def test_disarmed_outside_window():
    assert _plan(now=MIDDAY)["signal"] == "WAIT"
    assert _plan(now=AFTER)["signal"] == "CLOSED"
    assert _plan(now=WEEKEND)["signal"] == "MARKET CLOSED"
    print("ok  disarmed: WAIT midday, CLOSED after 4pm, MARKET CLOSED on Saturday")


def test_no_payoff_stands_down_when_armed():
    # C only ~1 pt from spot → the ATM premium exceeds the intrinsic captured at
    # C, so even a perfect pin loses → STAND DOWN.
    p = _plan(spy=_spy_map(c_target=773.3))               # c_spx ≈ 7764, gap ≈ 1 pt
    assert p["plan"]["payoff_at_c"] <= 0 and p["signal"] == "STAND DOWN"
    print(f"ok  premium > gap → STAND DOWN (payoff {p['plan']['payoff_at_c']})")


def test_c_above_spot_takes_atm_call():
    spy = _spy_map(crush_direction="UP", expensive_side="PUT", trade_side="CALL",
                   c_target=777.0, volume_bias="UP",
                   sweet_spot_low=773.4, sweet_spot_high=777.0)
    p = _plan(spy=spy, spx_spot=7735.0)
    assert p["side"] == "CALL" and p["action"] == "BUY ATM CALL" and p["signal"] == "TAKE"
    assert p["contracts"][0]["side"] == "CALL"
    print(f"ok  C>spot → TAKE BUY ATM CALL, target C={p['plan']['target']}")


def test_reachability_read():
    # Far C (big ceiling) → unreachable in the minutes left, warned but still armed.
    far = _plan(spy=_spy_map(c_target=770.0))            # ~34 pt to C
    assert far["reachable"] is False and any("reach C" in w for w in far["warnings"])
    assert far["signal"] == "TAKE"                        # strict rule still fires
    # Near C (small gap, but big enough to beat premium) → reachable.
    near = _plan(spy=_spy_map(c_target=772.0))            # ~14 pt to C
    assert near["reachable"] is True
    print(f"ok  reachability: far={far['reach_sigma']}σ (warn), near={near['reach_sigma']}σ (ok)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} trade-plan tests passed.")


if __name__ == "__main__":
    main()
