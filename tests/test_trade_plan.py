"""Unit tests for the 0DTE SPX trade plan — pure logic, no network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.net_dealer import NetDealerResult, StrikeRow
from app.trade_plan import SPX_MULT, assemble_plan, to_spx


def _spy_map(**over):
    """A DOWN-crush SPY map: C below spot, calls expensive → BUY PUTS toward C."""
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
    """SPX ladder with explicit asks so winners are deterministic. OTM puts are
    cheap (flat 2.0); ITM puts carry intrinsic + 2. Volume is high (SPX 0DTE has
    ~zero OI, so the plan ranks on volume)."""
    rows = []
    for k in range(7690, 7811, 5):
        put_intr = max(k - spx_spot, 0.0)
        call_intr = max(spx_spot - k, 0.0)
        rows.append(StrikeRow(
            strike=float(k), call_oi=0.0, put_oi=0.0,
            call_volume=8000, put_volume=8000,
            call_iv=0.11, put_iv=0.11,
            call_ask=round(max(0.10, call_intr + 2.0), 2),
            put_ask=round(max(0.10, put_intr + 2.0), 2),
        ))
    return rows, spx_spot


def test_down_crush_makes_a_put_plan():
    spy = _spy_map()
    spx_rows, spx_spot = _spx_rows()
    plan = assemble_plan(spy, spy.spot, spx_rows, spx_spot, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0)
    assert plan["side"] == "PUT" and plan["action"] == "BUY PUTS"
    # basis = spx_spot - spy_spot*10; C mapped onto the SPX ladder.
    basis = round(spx_spot - spy.spot * SPX_MULT, 2)
    assert plan["levels"]["c"] == to_spx(770.0, basis)
    assert plan["levels"]["c"] < spx_spot                  # room to fall
    assert plan["plan"]["edge_pts"] > 0
    assert plan["plan"]["target1"] == plan["levels"]["c"]
    # Stop sits at the call wall (a reclaim through it invalidates a short).
    assert plan["plan"]["stop"] == plan["levels"]["call_wall"]
    print(f"ok  down-crush → BUY PUTS: C={plan['levels']['c']} spot={spx_spot} "
          f"edge={plan['plan']['edge_pts']} R:R={plan['plan']['rr']}")


def test_contracts_finish_itm_and_are_labelled():
    spy = _spy_map()
    spx_rows, spx_spot = _spx_rows()
    plan = assemble_plan(spy, spy.spot, spx_rows, spx_spot, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0)
    cs = plan["contracts"]
    assert 1 <= len(cs) <= 3
    c_spx = plan["levels"]["c"]
    for c in cs:
        assert c["side"] == "PUT"
        assert c["strike"] > c_spx                         # a put finishes ITM at C
        assert c["value_at_c"] > 0
        assert c["symbol"].startswith(".SPXW") and c["symbol"].endswith(str(int(c["strike"])))
        assert "P" in c["symbol"]
    roles = " / ".join(c["role"].split(" ")[0] for c in cs)
    print(f"ok  contracts ({len(cs)}): {roles}; best %={max(x['est_gain_pct'] for x in cs)*100:.0f}")


def test_strong_setup_is_GO():
    spy = _spy_map(confidence=88.0)
    spx_rows, spx_spot = _spx_rows()
    plan = assemble_plan(spy, spy.spot, spx_rows, spx_spot, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0)
    assert plan["signal"] == "GO", plan["warnings"]
    print(f"ok  strong agreeing setup → {plan['signal']} (conf {plan['confidence']})")


def test_edge_exhausted_stands_down():
    # DOWN crush but C is already at/above spot → no room left to fall. The SPX
    # edge is (spy_spot − C)×10, so exhaust it on the SPY map itself.
    spy = _spy_map(c_target=774.0)      # C above spot on a DOWN crush
    spx_rows, spx_spot = _spx_rows()
    plan = assemble_plan(spy, spy.spot, spx_rows, spx_spot, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0)
    assert plan["plan"]["edge_pts"] <= 0
    assert plan["signal"] == "STAND DOWN"
    print(f"ok  edge exhausted → {plan['signal']} (edge {plan['plan']['edge_pts']})")


def test_up_crush_makes_a_call_plan():
    spy = _spy_map(crush_direction="UP", expensive_side="PUT", trade_side="CALL",
                   c_target=777.0, volume_bias="UP",
                   sweet_spot_low=773.4, sweet_spot_high=777.0)
    spx_rows, spx_spot = _spx_rows(spx_spot=7735.0)   # below C≈7736*... room to rise
    plan = assemble_plan(spy, spy.spot, spx_rows, spx_spot, "2026-09-22",
                         dte=0.0, t_years=0.0008, sessions=0)
    assert plan["side"] == "CALL" and plan["action"] == "BUY CALLS"
    for c in plan["contracts"]:
        assert c["side"] == "CALL" and c["strike"] < plan["levels"]["c"]
    print(f"ok  up-crush → BUY CALLS: C={plan['levels']['c']} spot={spx_spot}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} trade-plan tests passed.")


if __name__ == "__main__":
    main()
