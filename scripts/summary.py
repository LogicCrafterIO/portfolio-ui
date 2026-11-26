"""Compact snapshot of the book for the DeepSeek harness (no server needed).

Reads data/portfolio.json straight off disk and prints a text/JSON summary that
an analysing agent can consume in one shot.

    ../../.venv/bin/python scripts/summary.py            # human-readable
    ../../.venv/bin/python scripts/summary.py --json     # machine-readable
    ../../.venv/bin/python scripts/summary.py --json --as-of 2026-11-15
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import calc, store  # noqa: E402


def snapshot(as_of=None) -> dict:
    state = store.load()
    view = calc.build_view(state, calc.parse_date(as_of) if as_of else None)
    months = view["months"]
    return {
        "as_of": view["meta"]["as_of"],
        "booking_rule": view["meta"]["effective_rule"],
        "summary": view["summary"],
        "kpis": view["kpis"],
        "positions": [{
            "ticker": p["ticker"],
            "company": p["company"],
            "category": p["category"],
            "bias": p["bias"],
            "underlying_price": p["underlying_price"],
            "earnings_date": p["earnings_date"],
            "position_pl": p["position_pl"],
            "total_cost": p["total_cost"],
            "market_value": p["market_value"],
            "legs": [{
                "side": leg["side"], "type": leg["type"], "strike": leg["strike"],
                "expiry": leg["expiry"], "contracts": leg["contracts"],
                "cost_premium": leg["cost_premium"], "mid": leg["mid"],
                "position_pl": leg["position_pl"], "book_month": leg["effective_book_month"],
                "booked_amount": leg["booked_amount"], "quote_status": leg["quote_status"],
            } for leg in p["legs"]],
            "warnings": p["warnings"],
        } for p in view["positions"]],
        "months": [{
            "month": m["key"],
            "third_friday": m["third_friday"],
            "realised_booked": m["realised_pl"],
            "known_worst_case": m["known"],
            "actual": m["actual"],
            "swing_factor": m["swing"],
            "cumulative_booked": m["cumulative_booked"],
            "booked_legs": [{
                "ticker": l["ticker"], "side": l["side"], "type": l["type"],
                "strike": l["strike"], "expiry": l["expiry"], "booked": l["booked"],
            } for l in m["booked_legs"]],
        } for m in months["months"]],
        "realised": [{
            "trade_no": t["trade_no"], "ticker": t["ticker"], "company": t["company"],
            "structure": t["structure"], "leg_count": t["leg_count"],
            "legs_label": t["legs_label"],
            "entry_date": t["entry_date"], "exit_date": t["exit_date"], "days": t["days"],
            "pl": t["pl"], "pl_net": t["pl_net"], "pl_pct": t["pl_pct"],
            "book_month": t["book_month"], "commission": t["commission"],
            "legs": [{
                "side": l["side"], "type": l["type"], "strike": l["strike"],
                "expiry": l["expiry"], "contracts": l["contracts"],
                "price_in": l["price_in"], "price_out": l["price_out"], "pl": l["pl"],
            } for l in t["legs"]],
        } for t in view["realised"]],
        "watchlist": [{
            "ticker": w["ticker"], "side": w["side"], "sector": w["sector"],
            "sub_sector": w["sub_sector"], "conviction": w["conviction"],
            "comment": w["comment"], "catalyst": w["catalyst"],
            "source": w["source"], "updated_by": w["updated_by"], "updated_at": w["updated_at"],
        } for w in sorted(view["watchlist"], key=lambda w: (w["sector"], w["side"], w["ticker"]))],
        "recent_audit": view["audit"][-5:],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD")
    args = ap.parse_args(argv)

    snap = snapshot(args.as_of)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0

    s = snap["summary"]
    k = snap["kpis"]
    print(f"4Pillar options book — as of {snap['as_of']} (rule: {snap['booking_rule']})")
    print(f"  opening {s['opening_balance']:,.2f} | realised {s['realised_all_time']:,.2f} | "
          f"unrealised {s['unrealised']} | commission {s['commission']:,.2f} | "
          f"balance {s['account_balance']:,.2f}")
    print(f"  win rate {k['win_rate']} ({k['wins']}W/{k['losses']}L by trade), R {k['r_multiple']}, "
          f"Kelly {k['kelly']}, {k['closed_trades']} closed trades / {k['legs']} legs, "
          f"avg {k['avg_days']} days")
    print("\nOPEN POSITIONS")
    for p in snap["positions"]:
        print(f"  {p['ticker']:<6} {p['category']:<13} {p['bias']:<5} last {p['underlying_price']}  "
              f"P/L {p['position_pl']}")
        for leg in p["legs"]:
            print(f"      {leg['side']:<5} {leg['type']:<4} {leg['strike']:>7} {leg['expiry']} "
                  f"x{leg['contracts']:<4} cost {leg['cost_premium']:<6} mid {leg['mid']} "
                  f"P/L {leg['position_pl']} books {leg['book_month']} "
                  f"(worst {leg['booked_amount']})")
        for w in p["warnings"]:
            print(f"      ! {w}")
    print("\nCLOSED TRADES (a calendar is one trade, legs listed under it)")
    for t in snap["realised"]:
        print(f"  #{t['trade_no']:<3} {t['ticker']:<6} {t['structure']:<9} {t['entry_date']} → "
              f"{t['exit_date']}  P/L {t['pl']:>9,.2f}  ({t['leg_count']} leg"
              f"{'' if t['leg_count'] == 1 else 's'}, books {t['book_month']})")
        for leg in t["legs"]:
            print(f"        {leg['side']:<5} {leg['type']:<4} {leg['strike']:>7} {leg['expiry']} "
                  f"x{leg['contracts']:<4} {leg['price_in']} → {leg['price_out']}  "
                  f"P/L {leg['pl']:,.2f}")
    print("\nMONTHLY BOOKING")
    print(f"  {'month':<10}{'3rd Fri':<12}{'realised':>12}{'known':>12}{'actual':>12}{'swing':>12}{'cum':>12}")
    for m in snap["months"]:
        print(f"  {m['month']:<10}{m['third_friday']:<12}{m['realised_booked']:>12,.0f}"
              f"{m['known_worst_case']:>12,.0f}{(m['actual'] if m['actual'] is not None else float('nan')):>12,.0f}"
              f"{(m['swing_factor'] if m['swing_factor'] is not None else float('nan')):>12,.0f}"
              f"{m['cumulative_booked']:>12,.0f}")
    print("\nWATCHLIST")
    for w in snap["watchlist"]:
        note = f" — {w['comment']}" if w["comment"] else ""
        print(f"  {w['side']:<5} {w['ticker']:<6} {w['sector']}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
