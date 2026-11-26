"""Self-check: booking-rule fixtures, store integrity, grid, and a real xlsx round-trip.

Run:  ../../.venv/bin/python scripts/selfcheck.py
Exit code 0 = everything passed.
"""

from __future__ import annotations

import copy
import io
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

from app import calc, exporter, quotes, seed, store  # noqa: E402
import parity  # noqa: E402  (also used by the export checks below)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, got, want) -> None:
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def check_true(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILS.append(f"{label} {detail}".strip())
        print(f"  FAIL {label} {detail}")
    else:
        print(f"  ok   {label}")


def test_third_friday() -> None:
    print("third-Friday fixtures")
    fixtures = [
        (date(2026, 9, 18), (2026, 9)),
        (date(2026, 10, 16), (2026, 10)),
        (date(2026, 10, 17), (2026, 11)),   # 3rd Saturday -> next month
        (date(2026, 11, 20), (2026, 11)),
        (date(2026, 11, 27), (2026, 12)),   # 4th Friday -> next month
        (date(2026, 12, 18), (2026, 12)),
        (date(2026, 12, 19), (2027, 1)),
        (date(2027, 1, 15), (2027, 1)),
        (date(2027, 1, 22), (2027, 2)),
        (date(2027, 3, 19), (2027, 3)),
    ]
    for value, want in fixtures:
        check(f"booking_period({value})", calc.booking_period(value), want)
    check("third_friday(2026,10)", calc.third_friday(2026, 10), date(2026, 10, 16))
    check("third_friday(2026,11)", calc.third_friday(2026, 11), date(2026, 11, 20))
    check("third_friday(2026,12)", calc.third_friday(2026, 12), date(2026, 12, 18))
    check("third_friday(2027,1)", calc.third_friday(2027, 1), date(2027, 1, 15))
    check("booking_period(calendar)", calc.booking_period(date(2026, 11, 27), "calendar"), (2026, 11))


def test_leg_math() -> None:
    print("leg maths")
    long_leg = {"side": "Long", "type": "CALL", "strike": 100, "expiry": "2027-01-15",
                "contracts": 2, "multiplier": 100, "cost_premium": 3.0,
                "quote": {"mid": 4.5}}
    m = calc.leg_metrics(long_leg)
    check("long total cost", m["total_cost"], 600.0)
    check("long market value", m["market_value"], 900.0)
    check("long position P/L", m["position_pl"], 300.0)
    check("long booked amount", m["booked_amount"], -600.0)
    check("long book month", m["effective_book_month"], "2027-01")

    short_leg = dict(long_leg, side="Short", cost_premium=1.5, quote={"mid": 0.5})
    m2 = calc.leg_metrics(short_leg)
    check("short total cost", m2["total_cost"], -300.0)
    check("short position P/L", m2["position_pl"], 200.0)
    check("short booked amount", m2["booked_amount"], 300.0)   # credit kept = worst case

    trade = {"ticker": "TEST", "structure": "single", "commission": 2.0,
             "entry_date": "2026-10-01", "exit_date": "2026-10-19",
             "legs": [{"side": "Long", "type": "CALL", "strike": 10, "expiry": "2027-01-15",
                       "contracts": 2, "multiplier": 100, "price_in": 4.0, "price_out": 6.0}]}
    t = calc.trade_metrics(trade)
    check("trade P/L", t["pl"], 400.0)
    check("trade P/L net", t["pl_net"], 398.0)
    check("trade days", t["days"], 18)
    check("trade books in (4th wk)", t["book_month"], "2026-11")
    check("single-leg structure", t["structure"], "single")

    calendar = {"ticker": "CAL", "commission": 0.0,
                "entry_date": "2026-09-20", "exit_date": "2026-09-24",
                "legs": [
                    {"side": "Long", "type": "CALL", "strike": 155, "expiry": "2026-12-18",
                     "contracts": 2, "multiplier": 100, "price_in": 13.24, "price_out": 8.90},
                    {"side": "Short", "type": "CALL", "strike": 155, "expiry": "2026-10-16",
                     "contracts": 1, "multiplier": 100, "price_in": 4.39, "price_out": 1.61},
                ]}
    cal = calc.trade_metrics(calendar)
    check("calendar derives structure", cal["structure"], "calendar")
    check("calendar P/L is the sum of legs", cal["pl"], -590.0)
    check("calendar leg count", cal["leg_count"], 2)
    # one closed leg is one trade: the winning leg is its own win, the losing
    # leg its own loss, and the gross sums are the sheet's own SUMIF over rows
    k = calc.realised_kpis([cal])
    check("closed trades count legs", k["closed_trades"], 2)
    check("leg-level wins", k["wins"], 1)
    check("leg-level losses", k["losses"], 1)
    check("leg-level win rate", k["win_rate"], 0.5)
    check("leg-level win dollars", k["win_dollars"], 278.0)
    check("leg-level loss dollars", k["loss_dollars"], -868.0)
    check("leg-level closed P/L", k["closed_pl"], -590.0)

    # ---- merging separately-closed legs back into one trade
    leg_a = {"side": "Long", "type": "CALL", "strike": 155, "expiry": "2026-12-18",
             "contracts": 2, "multiplier": 100, "price_in": 13.24, "price_out": 8.90}
    leg_b = {"side": "Short", "type": "CALL", "strike": 155, "expiry": "2026-10-16",
             "contracts": 1, "multiplier": 100, "price_in": 4.39, "price_out": 1.61}
    single_a = store.normalise_trade({"ticker": "ORCL", "trade_no": 11, "commission": 1.0,
                                      "entry_date": "2026-09-20", "exit_date": "2026-09-24",
                                      "notes": "long leg", "legs": [leg_a]})
    single_b = store.normalise_trade({"ticker": "ORCL", "trade_no": 12, "commission": 2.0,
                                      "entry_date": "2026-09-21", "exit_date": "2026-09-26",
                                      "notes": "short leg", "legs": [leg_b]})
    merged = store.merge_trades([single_a, single_b], keep_id=single_a["id"])
    check("merge keeps the chosen trade", merged["id"], single_a["id"])
    check("merge keeps legs", len(merged["legs"]), 2)
    check("merge derives structure", merged["structure"], "calendar")
    check("merge sums commission", merged["commission"], 3.0)
    check("merge widens entry", merged["entry_date"], "2026-09-20")
    check("merge widens exit", merged["exit_date"], "2026-09-26")
    check("merge keeps notes", merged["notes"], "long leg; short leg")
    check("merge P/L", calc.trade_metrics(merged)["pl"], -590.0)
    check("merge keeps trade number of kept trade", merged["trade_no"], 11)
    try:
        store.merge_trades([single_a, store.normalise_trade({"ticker": "MSFT", "legs": [leg_a]})])
        check("merge refuses mixed tickers", "no error", "ValueError")
    except ValueError:
        check("merge refuses mixed tickers", "ValueError", "ValueError")
    try:
        store.merge_trades([single_a])
        check("merge refuses a single trade", "no error", "ValueError")
    except ValueError:
        check("merge refuses a single trade", "ValueError", "ValueError")


def test_partial_close() -> None:
    print("partial closes")
    leg = {"side": "Long", "type": "CALL", "strike": 130, "expiry": "2026-12-18",
           "contracts": 6, "multiplier": 100, "cost_premium": 3.0,
           "opened": "2026-09-20", "quote": {"mid": 4.5}}
    pos = {"ticker": "ANET", "company": "Arista Networks"}

    full = calc.leg_close_plan(leg, pos, price_out=4.5, exit_date="2026-10-30")
    check("full close: all contracts", full["closed"], 6)
    check("full close: nothing remains", full["remaining"], 0)
    check("full close: not partial", full["partial"], False)
    check("full close: leg count", full["trade"]["legs"][0]["contracts"], 6)
    check("full close: premium in from the leg", full["trade"]["legs"][0]["price_in"], 3.0)
    check("full close: dates", (full["trade"]["entry_date"], full["trade"]["exit_date"]),
          ("2026-09-20", "2026-10-30"))
    check("full close: note", full["trade"]["notes"], "closed from pre session (Long CALL)")

    part = calc.leg_close_plan(leg, pos, price_out="4.50", contracts="2", exit_date="2026-10-30")
    check("partial: contracts closed", part["closed"], 2)
    check("partial: contracts left", part["remaining"], 4)
    check("partial: flagged", part["partial"], True)
    check("partial: exit premium parsed from a form string", part["trade"]["legs"][0]["price_out"], 4.5)
    check("partial: note records the slice", part["trade"]["notes"],
          "closed from pre session (Long CALL) · partial 2 of 6")

    # the slice's basis plus the remainder's basis must equal the untouched leg, and the
    # P/L must be counted once — not once for the exit and again on what stayed open
    before = calc.leg_metrics(leg)
    after = calc.leg_metrics(dict(leg, contracts=part["remaining"],
                                  contracts_initial=part["held"]))
    booked = calc.trade_metrics(store.normalise_trade({"trade_no": 1, **part["trade"]}))
    check("partial: basis splits without loss",
          round(abs(after["total_cost"]) + booked["legs"][0]["cost_basis"], 2),
          round(abs(before["total_cost"]), 2))
    check("partial: P/L counted once",
          round(booked["pl"] + after["position_pl"], 2), round(before["position_pl"], 2))
    check("partial: remainder keeps the premium in", after["cost_premium"], 3.0)
    check("partial: remainder remembers its size", after["contracts_initial"], 6)

    for label, kwargs in [
        ("more contracts than held", {"price_out": 4.5, "contracts": 7}),
        ("zero contracts", {"price_out": 4.5, "contracts": 0}),
        ("negative contracts", {"price_out": 4.5, "contracts": -2}),
        ("no exit premium", {"price_out": None}),
        ("an unreadable contract count", {"price_out": 4.5, "contracts": "two"}),
    ]:
        try:
            calc.leg_close_plan(leg, pos, **kwargs)
            check(f"refuses {label}", "no error", "ValueError")
        except ValueError:
            check(f"refuses {label}", "ValueError", "ValueError")

    try:
        calc.leg_close_plan(dict(leg, contracts=0), pos, price_out=1.0)
        check("refuses a leg with no contracts", "no error", "ValueError")
    except ValueError:
        check("refuses a leg with no contracts", "ValueError", "ValueError")

    check("contracts_initial survives normalisation",
          store.normalise_leg({"contracts": 4, "contracts_initial": "6"})["contracts_initial"], 6)
    check("contracts_initial absent by default",
          store.normalise_leg({"contracts": 4})["contracts_initial"], None)

    # two slices of one leg merge back into a "single", not a "custom" two-leg trade
    slice_a = store.normalise_trade({"ticker": "ANET", "trade_no": 21, "entry_date": "2026-09-20",
                                     "exit_date": "2026-10-30",
                                     "legs": [dict(part["trade"]["legs"][0], contracts=2)]})
    slice_b = store.normalise_trade({"ticker": "ANET", "trade_no": 22, "entry_date": "2026-09-20",
                                     "exit_date": "2026-11-02",
                                     "legs": [dict(part["trade"]["legs"][0], contracts=4)]})
    rejoined = store.merge_trades([slice_a, slice_b], keep_id=slice_a["id"])
    check("slices merge to one contract", rejoined["structure"], "single")
    check("slices merge to the full size", sum(l["contracts"] for l in rejoined["legs"]), 6)
    check("slices merge to one P/L", calc.trade_metrics(rejoined)["pl"],
          calc.trade_metrics(store.normalise_trade({
              "ticker": "ANET", "trade_no": 1, "entry_date": "2026-09-20", "exit_date": "2026-10-30",
              "legs": [dict(part["trade"]["legs"][0], contracts=6)]}))["pl"])
    check("a real two-leg spread is still not single",
          calc.derive_structure([{"side": "Long", "type": "CALL", "strike": 130, "expiry": "2026-12-18"},
                                 {"side": "Long", "type": "CALL", "strike": 140,
                                  "expiry": "2026-12-18"}]), "vertical")


def test_bias() -> None:
    print("directional bias")
    put = [{"side": "Long", "type": "PUT", "contracts": 3, "multiplier": 100, "cost_premium": 4.15}]
    check("long put is bearish", calc.derive_bias(put), "Short")
    call = [{"side": "Long", "type": "CALL", "contracts": 1, "multiplier": 100, "cost_premium": 11.16}]
    check("long call is bullish", calc.derive_bias(call), "Long")
    short_put = [{"side": "Short", "type": "PUT", "contracts": 2, "multiplier": 100, "cost_premium": 0.96}]
    check("short put is bullish", calc.derive_bias(short_put), "Long")
    short_call = [{"side": "Short", "type": "CALL", "contracts": 7, "multiplier": 100, "cost_premium": 0.48}]
    check("short call is bearish", calc.derive_bias(short_call), "Short")
    debit_spread = [
        {"side": "Long", "type": "CALL", "contracts": 20, "multiplier": 100, "cost_premium": 0.65},
        {"side": "Short", "type": "CALL", "contracts": 20, "multiplier": 100, "cost_premium": 0.35},
    ]
    check("debit call spread is bullish", calc.derive_bias(debit_spread), "Long")
    ratio_put = [
        {"side": "Long", "type": "PUT", "contracts": 6, "multiplier": 100, "cost_premium": 2.0},
        {"side": "Short", "type": "PUT", "contracts": 2, "multiplier": 100, "cost_premium": 0.96},
    ]
    check("put ratio is bearish", calc.derive_bias(ratio_put), "Short")
    check("no legs defaults to Long", calc.derive_bias([]), "Long")

    pos = store.normalise_position({"ticker": "HPE", "bias": "auto", "legs": put})
    check("auto bias stores auto", pos["bias"], "auto")
    check("auto bias source", pos["bias_source"], "auto")
    metrics = calc.position_metrics(pos)
    check("auto bias resolves to Short", metrics["bias"], "Short")
    check("auto bias is not a mismatch", metrics["bias_mismatch"], False)
    manual = calc.position_metrics(store.normalise_position(
        {"ticker": "HPE", "bias": "Long", "legs": put}))
    check("manual label wins", manual["bias"], "Long")
    check("manual contradiction is flagged", manual["bias_mismatch"], True)
    check("mismatch keeps the derived value", manual["bias_derived"], "Short")


def test_midpoint() -> None:
    print("bid/ask midpoint")
    q = quotes._quote_from_row({"bid": 0.0, "ask": 0.15, "lastPrice": 0.05})
    check("0.00 / 0.15 mid", q["mid"], 0.075)
    check("0.00 / 0.15 status", q["status"], "zero-bid")
    q = quotes._quote_from_row({"bid": 1.04, "ask": 1.16, "lastPrice": 1.1})
    check("1.04 / 1.16 mid", q["mid"], 1.1)
    check("1.04 / 1.16 status", q["status"], "ok")
    q = quotes._quote_from_row({"bid": 0.72, "ask": 0.0, "lastPrice": 0.6})
    check("0.72 / 0.00 mid", q["mid"], 0.36)
    check("0.72 / 0.00 status", q["status"], "zero-ask")
    q = quotes._quote_from_row({"bid": 0.0, "ask": 0.0, "lastPrice": 0.12})
    check("both zero falls back to last", q["mid"], 0.12)
    check("both zero status", q["status"], "last-only")
    q = quotes._quote_from_row({"bid": None, "ask": 0.4})
    check("absent bid uses ask", q["mid"], 0.4)
    check("absent bid status", q["status"], "ask-only")
    # the manual path and the quote path must agree
    manual = calc.leg_metrics({"side": "Short", "type": "CALL", "contracts": 20, "multiplier": 100,
                              "cost_premium": 0.35, "bid": 0.0, "ask": 0.15})
    from_quote = calc.leg_metrics({"side": "Short", "type": "CALL", "contracts": 20, "multiplier": 100,
                                   "cost_premium": 0.35,
                                   "quote": quotes._quote_from_row({"bid": 0.0, "ask": 0.15})})
    check("manual bid/ask mid", manual["mid"], 0.075)
    check("quote mid matches manual", from_quote["mid"], manual["mid"])
    check("short leg P/L at 0.075 mid", from_quote["position_pl"], 550.0)
    # a book that exists is the midpoint: the Mid column must always be the average of
    # the Bid and Ask printed beside it. A hand-typed mid only prices a leg the chain
    # cannot quote, so it must not outrank a live pair.
    book_with_override = calc.leg_metrics({
        "side": "Long", "type": "CALL", "contracts": 1, "multiplier": 100,
        "cost_premium": 1.0, "bid": 0.96, "ask": 1.06, "mid_override": 1.25,
        "quote": {"mid": 1.01, "status": "ok"}})
    check("live book beats a stale override", book_with_override["mid"], 1.01)
    check("live book: source is the quote", book_with_override["mid_source"], "quote")
    check("stale override is still visible",
          book_with_override["mid_override_ignored"], True)
    typed_book = calc.leg_metrics({"side": "Long", "type": "CALL", "contracts": 1,
                                   "multiplier": 100, "cost_premium": 1.0,
                                   "bid": 0.5, "ask": 2.0, "mid_override": 1.25})
    check("a typed book still averages", typed_book["mid"], 1.25)
    check("typed book source", typed_book["mid_source"], "manual")
    no_book = calc.leg_metrics({"side": "Long", "type": "CALL", "contracts": 1,
                                "multiplier": 100, "cost_premium": 1.0,
                                "mid_override": 1.25})
    check("override prices a leg with no book", no_book["mid"], 1.25)
    check("no book: source is the override", no_book["mid_source"], "override")


def test_store() -> None:
    print("store")
    state = store.load()
    check_true("store file exists", store.STORE_PATH.exists(), str(store.STORE_PATH))
    check_true("has positions", len(state["positions"]) > 0, str(len(state["positions"])))
    check_true("has realised trades", len(state["realised"]) > 0)
    check_true("has watchlist", len(state["watchlist"]) > 0)
    for pos in state["positions"]:
        for leg in pos["legs"]:
            check_true(f'{pos["ticker"]} leg side valid', leg["side"] in ("Long", "Short"), leg["side"])
            check_true(f'{pos["ticker"]} leg type valid', leg["type"] in ("CALL", "PUT"), leg["type"])
            check_true(f'{pos["ticker"]} leg expiry parses', calc.parse_date(leg["expiry"]) is not None,
                       str(leg["expiry"]))
    check_true("audit trail present", len(state["audit"]) > 0)


def test_grid(state: dict) -> None:
    print("monthly grid")
    view = calc.build_view(state)
    months = view["months"]["months"]
    check_true("months present", len(months) > 0)
    as_of = calc.parse_date(view["months"]["as_of"])
    current_key = calc.month_key(*calc.current_booking_month(as_of, view["months"]["rule"]))
    check("first month is the current booking month", months[0]["key"], current_key)
    if view["months"]["rule"] != "calendar":
        # past the third Friday the calendar month has already closed, so the
        # grid must not still be drawing it as the live column
        if as_of > calc.third_friday(as_of.year, as_of.month):
            expected = calc.month_key(*calc.add_months(as_of.year, as_of.month, 1))
        else:
            expected = calc.month_key(as_of.year, as_of.month)
        check("floor follows the third-Friday cutoff", months[0]["key"], expected)
        check_true("no month before the current booking month",
                   all(m["key"] >= current_key for m in months),
                   str([m["key"] for m in months]))
    check_true("months sorted", [m["key"] for m in months] == sorted(m["key"] for m in months))
    for m in months:
        check_true(f'{m["label"]} known = realised + booked legs',
                   round(m["known"], 2) == round(m["realised_pl"] + sum(l["booked"] for l in m["booked_legs"]), 2))
        if m["actual"] is not None:
            check_true(f'{m["label"]} swing = actual - known',
                       round(m["swing"], 2) == round(m["actual"] - m["known"], 2))
    print(f"  months: {[m['label'] for m in months]}")
    print(f"  known:  {[m['known'] for m in months]}")
    print(f"  swing:  {[m['swing'] for m in months]}")


# The export is the user's own workbook with our numbers written into it, so the
# tests check the cells that must carry data and hand the styling question to
# scripts/parity.py, which compares every cell's style against the original.
def test_export(state: dict) -> None:
    print("xlsx export (template-driven)")
    view = calc.build_view(state)
    wb = exporter.build_workbook(state)
    check("sheet names", wb.sheetnames, ["Realised", "pre session", "Actionable Watchlist"])

    target = store.DATA_DIR / "_selfcheck_export.xlsx"
    wb.save(target)
    try:
        reloaded = openpyxl.load_workbook(target)
        rl = reloaded["Realised"]
        legs = sum(len(t["legs"]) for t in view["realised"])
        first_rows = [9 + offset for offset, (_, _, first) in enumerate(
            [(t, leg, index == 0) for t in sorted(view["realised"], key=lambda t: t["trade_no"])
             for index, leg in enumerate(t["legs"])]) if first]
        check("realised trade rows", len(first_rows), len(view["realised"]))
        check("closed units are legs", legs, view["kpis"]["closed_trades"])
        check("realised trade numbers on first leg rows", [rl.cell(row=r, column=1).value for r in first_rows],
              [t["trade_no"] for t in sorted(view["realised"], key=lambda t: t["trade_no"])])
        check("realised leg count", sum(1 for r in range(9, 9 + legs)
                                        if rl.cell(row=r, column=2).value), legs)
        check("realised side casing", rl.cell(row=9, column=4).value,
              _title_case(rl.cell(row=9, column=4).value))
        check("realised days formula", rl["M9"].value, "=L9-K9")
        check("average-days range follows the data", rl["M2"].value, f"=average(M9:M{8 + legs})")
        check("closed-trades cell counts legs", rl["L2"].value, "=COUNTA(B$9:B$1048572)")
        check("realised KPI formulas untouched", rl["E1"].value, "=J2/L2")
        check("realised wins formula untouched", rl["H2"].value, '=SUMIF(O$8:O$1048572,">0")')

        ps = reloaded["pre session"]
        months = view["months"]["months"]
        blocks = exporter._position_blocks(view["positions"])
        check("position block count", len(blocks), len(view["positions"]))
        check("block grid starts at row 13", blocks[0]["ticker_row"], 13)
        check("block stride", blocks[1]["ticker_row"] - blocks[0]["ticker_row"], 5)
        # A block is a name row, its leg slots, one spare row and the totals row, so a
        # position with three or more legs takes one row more than the sheet's five --
        # the spare is what keeps the totals rule off the last leg
        expected_totals, row = [], 13
        for block in blocks:
            slots = max(3, len(block["legs"]) + 1)
            expected_totals.append(row + 1 + slots)
            row += slots + 2
        check("totals rows follow the leg counts", [b["total_row"] for b in blocks], expected_totals)
        check("first block totals", ps["P17"].value, "=SUM(P14:P16)")
        check("opening balance", ps["S5"].value, float(state["meta"]["opening_balance"]))
        check("realised link", ps["S6"].value, "=+Realised!O2")
        check("unrealised link", ps["S7"].value, "=+" + "+".join(f"S{b['total_row']}" for b in blocks))
        check("balance formula", ps["S10"].value, "=+S5+S6+S7-S8")

        labels = [ps.cell(row=6, column=21 + 4 * index + 1).value for index in range(len(months))]
        check("one month block per month", len(labels), len(months))
        check_true("month labels written", all(labels), str(labels))
        check("month block headers", [ps.cell(row=12, column=21 + 4 * index).value for index in range(len(months))],
              ["Known /worse case"] * len(months))
        check("first cumulative starts from realised", ps["U10"].value, "=+S6+" + "+".join(
            f"U{b['total_row']}" for b in blocks))
        check("next cumulative chains", ps["Y10"].value, "=+U10+" + "+".join(
            f"Y{b['total_row']}" for b in blocks))
        action_col = 21 + 4 * len(months)
        check("trading action column", ps.cell(row=12, column=action_col).value, "Trading action")
        # the trading action is the block's net cost, so it must reference the totals
        # row (itself SUM over every leg) -- a multi-leg position must not quote the
        # first leg's cost and contradict the total beside it
        check("trading action formula", ps.cell(row=blocks[0]["total_row"], column=action_col).value,
              f"=-P{blocks[0]['total_row']}")
        check("leg cost formula", ps["P14"].value, '=IF(E14="Long",L14*I14*K14,-L14*I14*K14)')
        # M/N/O follow whether the leg actually has a book: the sheet's own
        # formula when it does, the quote mid when it does not (see test_quote_fill,
        # which pins all three cases without needing the live store or a network)
        first_leg = blocks[0]["legs"][0]
        has_book = (first_leg.get("bid") is not None and first_leg.get("ask") is not None
                    and (first_leg["bid"] > 0 or first_leg["ask"] > 0))
        check("mid formula when the leg has a book", ps["N14"].value,
              "=+(M14+O14)/2" if has_book else first_leg.get("mid"))

        # ---- the call/put and long/short colour coding follows the value written,
        #      not the template row the leg happened to land on
        donors = parity.leg_role_styles(openpyxl.load_workbook(exporter.TEMPLATE)["pre session"])
        check("template codes CALL dark blue", donors[(6, "CALL")][1][1], "FF333F4F")
        check("template codes PUT the theme orange", donors[(6, "PUT")][1][1], "theme5:0.0")
        check_true("CALL and PUT look different", donors[(6, "CALL")][1] != donors[(6, "PUT")][1])
        wrong = []
        for block in blocks:
            for row, leg in zip(block["slot_rows"], block["legs"]):
                for col, key in ((5, str(leg["side"]).upper()), (6, str(leg["type"]).upper())):
                    want = donors.get((col, key))
                    sig = parity.style_of(ps.cell(row=row, column=col))
                    if want is None or (sig[0], sig[1]) != (want[0], want[1]):
                        wrong.append(f"{parity.get_column_letter(col)}{row} {key}")
        check("leg side/type colours follow the value", wrong, [])

        # retyping a leg must repaint its TYPE cell, whatever row it sits on: this is
        # the case that used to keep the donor row's colour (a PUT exported as blue)
        flipped = copy.deepcopy(state)
        first = next(pos for pos in flipped["positions"] if pos["legs"])
        leg0 = first["legs"][0]
        leg0["type"] = "PUT" if str(leg0["type"]).upper() == "CALL" else "CALL"
        ps2 = exporter.build_workbook(flipped)["pre session"]
        row2 = exporter._position_blocks(calc.build_view(flipped)["positions"])[0]["slot_rows"][0]
        want2 = donors[(6, str(leg0["type"]).upper())]
        got2 = parity.style_of(ps2.cell(row=row2, column=6))
        check("a retyped leg repaints its TYPE cell", (got2[0], got2[1]), (want2[0], want2[1]))

        # A position's booked cost must land in *its* month column, whichever month
        # that is. This check used to name a ticker from the book it was written
        # against, so it raised StopIteration -- and took every check after it down
        # with it -- the moment that position was closed. It now uses whatever leg
        # actually carries a book, and compares against the same sum the exporter
        # wrote, so the assertion means the same thing on any book.
        booked_leg = next(((block, leg) for block in blocks for leg in block["legs"]
                           if leg.get("booked_amount")), None)
        check_true("the book has a leg carrying a booked amount", booked_leg is not None)
        if booked_leg:
            block, leg = booked_leg
            mk = leg["effective_book_month"]
            col = 21 + 4 * [m["key"] for m in months].index(mk)
            ticker = block["position"]["ticker"]
            # The cell is a live formula, not a stamped number: it must sum one
            # signed term per leg of the block and read that month's cutoff anchor
            # in row 5. The numbers it produces are checked against calc by the
            # verification scripts, which recalculate the workbook.
            cell = ps.cell(row=block["total_row"], column=col)
            anchor = ps.cell(row=5, column=col).coordinate
            formula = cell.value if isinstance(cell.value, str) else ""
            check_true(f"{ticker} known {mk} is a live formula",
                       formula.startswith("=") and "ABS(" in formula and anchor in formula,
                       repr(formula)[:90])
            # the Known cell is that position's own contribution -- the cumulative
            # row above it sums every position -- so it must name exactly this
            # block's leg rows, each gated on the month its expiry books into
            missing = [f"L{row}" for row in block["slot_rows"][:len(block["legs"])]
                       if f"L{row}" not in formula]
            check(f"{ticker} known {mk} covers its legs", missing, [])

        # The sheet draws three category buckets; the book may use one, two or three of
        # them, so the rows and their SUM references are derived from the book instead
        # of assuming all three are occupied and that positions landed on fixed rows.
        template_ps = openpyxl.load_workbook(exporter.TEMPLATE)["pre session"]
        template_cats = [str(template_ps.cell(row=r, column=7).value or "").strip() for r in (6, 7, 8)]
        cat_rows = [ps.cell(row=r, column=7).value for r in (6, 7, 8)]
        used = [label for label in template_cats if label in cat_rows]
        check("category rows keep the sheet's order", cat_rows, used + [None] * (len(cat_rows) - len(used)))

        def cat_refs(label, bias):
            # the exporter buckets by casefolded label (the sheet's spelling wins), so
            # the expectation has to fold the same way
            refs = [b["total_row"] for b in blocks
                    if str(b["position"]["category"]).casefold() == label.casefold()
                    and b["position"]["bias"] == bias]
            return "=+" + "+".join(f"Q{row}" for row in refs) if refs else 0

        for row, label in zip((6, 7, 8), cat_rows):
            if label:
                check(f"{label} long formula", ps.cell(row=row, column=8).value, cat_refs(label, "Long"))
                check(f"{label} short formula", ps.cell(row=row, column=9).value, cat_refs(label, "Short"))
        filled_cats = [row for row in (6, 7, 8) if ps.cell(row=row, column=7).value]
        check("gross market value row", ps["H10"].value,
              "=+" + "+".join(f"H{row}" for row in filled_cats) if filled_cats else "=0")

        # the template is a copy of the user's own sheet and carries *their* example
        # book in the same grid (HOOD at 95.07 with a real 8.60/8.75, and 0.00 leg
        # slots). None of it may survive an export: only this book's blocks belong on
        # the tab, or the sheet shows bid/ask that never came from yfinance.
        block_rows = ({b["ticker_row"] for b in blocks}
                      | {row for b in blocks for row in b["slot_rows"]}
                      | {b["total_row"] for b in blocks})
        tickers = [ps.cell(row=row, column=2).value for row in range(13, 90)
                   if ps.cell(row=row, column=2).value not in (None, "")]
        check("only this book's tickers on the grid", tickers,
              [b["position"]["ticker"] for b in blocks])
        stray = [row for row in range(13, 90) if row not in block_rows
                 and ps.cell(row=row, column=8).value not in (None, "")]
        check("no template sample leg rows left behind", stray, [])
        stray_quotes = [row for row in range(13, 90) if row not in block_rows
                        and (ps.cell(row=row, column=13).value not in (None, "")
                             or ps.cell(row=row, column=15).value not in (None, ""))]
        check("no template sample bid/ask left behind", stray_quotes, [])

        wl = reloaded["Actionable Watchlist"]
        check("watchlist title untouched", wl["G2"].value, "ACTIONABLE WATCHLIST")
        check("watchlist headers untouched", [wl.cell(row=6, column=c).value for c in (7, 8, 9, 10)],
              ["Comment", "LONG candidates", "SHORT candidates", "Comment"])

        # every sector the sheet draws is still drawn, in the sheet's order, and
        # no candidate is lost or duplicated -- the export is a re-fill of the
        # tab, not a redraw, so a block only gains rows when the book needs them
        template_blocks = parity._watchlist_blocks(openpyxl.load_workbook(exporter.TEMPLATE)["Actionable Watchlist"])
        blocks_now = parity._watchlist_blocks(wl)
        check("watchlist sector rows kept", [b["name"] for b in blocks_now][:len(template_blocks)],
              [b["name"] for b in template_blocks])

        drawn = {}
        for block in blocks_now:
            for row in [block["header_row"], *block["rows"]]:
                for col in (8, 9):
                    value = wl.cell(row=row, column=col).value
                    if isinstance(value, str) and value.strip() and row != block["header_row"]:
                        drawn.setdefault(value.strip(), []).append((block["name"], row, col))
        wanted = {(item["ticker"], item["sector"]) for item in view["watchlist"]}
        got = {(ticker, sector) for ticker, places in drawn.items() for sector, _, _ in places}
        check("watchlist candidates placed", sorted(got), sorted(wanted))
        check("watchlist candidates not duplicated",
              {ticker: places for ticker, places in drawn.items() if len(places) > 1}, {})

        # a ticker keeps the row it came from when its block still has that row
        by_name = {_sector_key(b["name"]): b for b in blocks_now}
        for item in view["watchlist"]:
            offset = item.get("row_offset")
            block = by_name.get(_sector_key(item["sector"]))
            if not isinstance(offset, int) or block is None or offset > len(block["rows"]):
                continue
            column = 8 if item["side"] == "LONG" else 9
            check(f'watchlist row alignment kept ({item["ticker"]})',
                  wl.cell(row=block["rows"][offset - 1], column=column).value, item["ticker"])

        # the sub-sector / comment text rides along in G (LONG side) and J (SHORT side)
        notes_left = {wl.cell(row=row, column=7).value for row in range(7, 80)}
        notes_right = {wl.cell(row=row, column=10).value for row in range(7, 80)}
        wanted_left = {item["sub_sector"] or item["comment"] for item in view["watchlist"]
                       if (item["sub_sector"] or item["comment"]) and item["side"] == "LONG"}
        wanted_right = {item["sub_sector"] or item["comment"] for item in view["watchlist"]
                        if (item["sub_sector"] or item["comment"]) and item["side"] == "SHORT"}
        check("watchlist LONG notes kept", sorted(wanted_left - notes_left), [])
        check("watchlist SHORT notes kept", sorted(wanted_right - notes_right), [])

        # One look per role: every ticker carries the sheet's LONG/SHORT look, every
        # note its note look, and every blank cell the *same* plain look as every
        # other blank -- no hand-picked one-off colours, and no tint left behind on a
        # row whose ticker the book dropped. The blank look is asserted as consistent
        # rather than compared to a majority vote, because once the book fills most
        # rows the majority is a ticker's colour and every blank row would "fail".
        styles = parity._watchlist_styles(wl, blocks_now)
        wrong = []
        blanks: dict[int, set] = {}
        for block in blocks_now:
            for row in block["rows"]:
                for col, role in ((7, "note_left"), (8, "long"), (9, "short"), (10, "note_right")):
                    cell = wl.cell(row=row, column=col)
                    value = cell.value
                    if isinstance(value, str) and value.strip():
                        if parity.style_of(cell) != styles[role]:
                            wrong.append(f"{parity.get_column_letter(col)}{row} {value!r} mis-styled")
                    else:
                        blanks.setdefault(col, set()).add(parity.style_of(cell))
        for col, looks in sorted(blanks.items()):
            if len(looks) > 1:                     # blank cells are per column, one look each
                wrong.append(f"{parity.get_column_letter(col)}: {len(looks)} looks on blank rows")
        check("watchlist ticker styling consistent", wrong, [])
    finally:
        target.unlink(missing_ok=True)

    # the whole point: byte-level styling must match the original workbook
    check("styling parity with example_sheet.xlsx", parity.run(wb)[1:], [])

    # and the tab must survive its own data: seed the book from the sheet's own
    # watchlist, export it, and every sector, ticker, note and row must come back
    test_watchlist_roundtrip()


def _title_case(text) -> str:
    return (text or "")[:1].upper() + (text or "")[1:].lower()


def _sector_key(text: str) -> str:
    return " ".join((text or "").lower().replace("/", " ").split())


def test_watchlist_roundtrip() -> None:
    """Seed from the sheet's own watchlist and export it straight back.

    This is the strongest statement of "it looks like the template": the tab
    the user drew, run through the app and out again, must land on the same
    rows with the same sectors, tickers and notes. Only the normalisations the
    export makes on purpose may differ -- a consistent LONG blue / SHORT red
    where the sheet had hand-picked one-offs, and the sector name in both band
    cells.
    """
    print("watchlist round-trip (sheet -> store -> export)")
    template = openpyxl.load_workbook(exporter.TEMPLATE)
    want = template["Actionable Watchlist"]
    export = exporter.build_workbook(seed.build_state(exporter.TEMPLATE))
    got = openpyxl.load_workbook(io.BytesIO(_bytes(export)))["Actionable Watchlist"]

    want_blocks = parity._watchlist_blocks(want)
    got_blocks = parity._watchlist_blocks(got)
    check("round-trip: sector blocks", [b["name"] for b in got_blocks],
          [b["name"] for b in want_blocks])

    def placements(ws, blocks):
        """Every ticker on the body rows, keyed by the cell it sits in."""
        return {(row, col): ws.cell(row=row, column=col).value
                for block in blocks for row in block["rows"] for col in (8, 9)
                if ws.cell(row=row, column=col).value not in (None, "")}

    check("round-trip: every ticker back on its row",
          placements(got, got_blocks), placements(want, want_blocks))

    notes = {f"{parity.get_column_letter(col)}{row}": want.cell(row=row, column=col).value
             for block in want_blocks for row in block["rows"] for col in (7, 10)
             if isinstance(want.cell(row=row, column=col).value, str)
             and want.cell(row=row, column=col).value.strip()}
    check("round-trip: every note back on its row",
          {ref: got[ref].value for ref in notes}, notes)

    # the one thing the export normalises on the header rows: the sector name
    # goes in both band cells, so a block is labelled the same way as the rest
    single = [f"{parity.get_column_letter(col)}{block['header_row']}"
              for block in got_blocks for col in (8, 9)
              if got.cell(row=block["header_row"], column=col).value != block["name"]]
    check("round-trip: sector band in both cells", single, [])


def _bytes(workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_degenerate() -> None:
    print("degenerate states")

    empty = store.default_state()
    v = calc.build_view(empty)
    check("empty book: one month column", len(v["months"]["months"]), 1)
    check("empty book: realised zero", v["summary"]["realised_all_time"], 0.0)
    check("empty book: balance = opening", v["summary"]["account_balance"],
          float(empty["meta"]["opening_balance"]))
    check("empty book: export sheets", exporter.build_workbook(empty).sheetnames,
          ["Realised", "pre session", "Actionable Watchlist"])

    unpriced = store.default_state()
    unpriced["positions"] = [store.normalise_position({
        "ticker": "ZZZ", "legs": [{"side": "Long", "type": "CALL", "strike": 10,
                                   "expiry": "2026-12-18", "contracts": 1,
                                   "cost_premium": 1.0}],
    })]
    v2 = calc.build_view(unpriced)
    check("unpriced leg: no position P/L", v2["positions"][0]["position_pl"], None)
    check_true("unpriced leg: flagged", v2["months"]["unpriced"])
    dec = [m for m in v2["months"]["months"] if m["key"].endswith("-12")][0]
    check("unpriced leg: booked still computed", dec["known"], -100.0)
    check("unpriced leg: actual unavailable", dec["actual"], None)
    check_true("unpriced leg: export builds",
               exporter.build_workbook(unpriced).sheetnames[1] == "pre session")

    legacy = store.default_state()  # an old client may POST partial state
    legacy.pop("watchlist")
    legacy["positions"] = [{"ticker": "abc", "legs": [{"strike": "5", "contracts": "2",
                                                       "side": "short", "type": "put"}]}]
    normalised = store.normalise_state(legacy)
    check("normalise: ticker upper-cased", normalised["positions"][0]["ticker"], "ABC")
    check("normalise: side", normalised["positions"][0]["legs"][0]["side"], "Short")
    check("normalise: type", normalised["positions"][0]["legs"][0]["type"], "PUT")
    check("normalise: contracts from string", normalised["positions"][0]["legs"][0]["contracts"], 2)


def test_quote_fill() -> None:
    """What lands in pre session M / N / O for the three states a leg can be in.

    Yahoo prices every contract 0.00 / 0.00 outside a session, so a 0/0 row is the
    *absence* of a book, not a spread of zero: it must export as a midpoint and no
    bid/ask, never as 0.00/0.00/0.00. One genuine zero (0.00 / 0.15, a thin chain)
    is still a quote and still exports as the sheet's own (M+O)/2.
    """
    print("pre session quote fill")
    check("0/0 is not a book", quotes._book(0.0, 0.0), None)
    check("0/0.15 is a book", quotes._book(0.0, 0.15), {"bid": 0.0, "ask": 0.15})
    check("a missing side is not a book", quotes._book(None, 0.4), None)
    check("an absent book is not carried", quotes._carried({"book": None}), None)
    check("a 0/0 cache is not carried", quotes._carried({"book": {"bid": 0.0, "ask": 0.0}}), None)
    carried = quotes._carried({"book": {"bid": 0.96, "ask": 1.06, "mid": 1.01,
                                        "ts": "2026-11-05T20:00:00+00:00",
                                        "contract": "BB270115C00009000"}},
                              contract="BB270115C00009000")
    check("a real book is carried", (carried["bid"], carried["ask"], carried["mid"]),
          (0.96, 1.06, 1.01))
    check("a book does not follow an edited contract",
          quotes._carried({"book": {"bid": 0.96, "ask": 1.06,
                                    "contract": "BB270115C00009000"}},
                          contract="BB270115C00010000"), None)

    def leg(**quote):
        return {"side": "Long", "type": "CALL", "strike": 9, "expiry": "2027-01-15",
                "contracts": 1, "multiplier": 100, "cost_premium": 1.0, **quote}

    state = store.default_state()
    state["positions"] = [store.normalise_position({
        "ticker": "TEST", "category": "Hardware",
        "legs": [leg(bid=None, ask=None, quote={"mid": 0.07, "status": "last-only"}),   # no book
                 leg(bid=0.96, ask=1.06, quote={"mid": 1.01, "status": "ok"}),        # live book
                 leg(bid=0.0, ask=0.15, quote={"mid": 0.075, "status": "zero-bid"})],  # one real side
    })]
    ps = exporter.build_workbook(state)["pre session"]
    rows = exporter._position_blocks(calc.build_view(state)["positions"])[0]["slot_rows"]

    check("no book: bid cell empty", ps.cell(row=rows[0], column=13).value, None)
    check("no book: ask cell empty", ps.cell(row=rows[0], column=15).value, None)
    check("no book: mid is the last trade", ps.cell(row=rows[0], column=14).value, 0.07)
    check("live book: bid", ps.cell(row=rows[1], column=13).value, 0.96)
    check("live book: ask", ps.cell(row=rows[1], column=15).value, 1.06)
    check("live book: mid formula", ps.cell(row=rows[1], column=14).value,
          f"=+(M{rows[1]}+O{rows[1]})/2")
    check("zero bid: kept as a quote", ps.cell(row=rows[2], column=13).value, 0.0)
    check("zero bid: ask", ps.cell(row=rows[2], column=15).value, 0.15)
    check("zero bid: mid formula", ps.cell(row=rows[2], column=14).value,
          f"=+(M{rows[2]}+O{rows[2]})/2")

    # a leg with a live book exports the sheet's own midpoint formula even when a hand
    # typed mid is sitting on the leg -- otherwise the sheet's N and its M/O neighbours
    # disagree, which is the bug that froze 0.88 between a 0.95 bid and a 1.03 ask
    manual = store.default_state()
    manual["positions"] = [store.normalise_position({
        "ticker": "TEST", "category": "Hardware",
        "legs": [leg(bid=0.96, ask=1.06, mid_override=1.25,
                     quote={"mid": 1.01, "status": "ok"})],
    })]
    ps2 = exporter.build_workbook(manual)["pre session"]
    row = exporter._position_blocks(calc.build_view(manual)["positions"])[0]["slot_rows"][0]
    check("live book wins the midpoint", ps2.cell(row=row, column=14).value,
          f"=+(M{row}+O{row})/2")
    check("stale override: bid still exported", ps2.cell(row=row, column=13).value, 0.96)
    check("stale override: ask still exported", ps2.cell(row=row, column=15).value, 1.06)
    # with no book the typed mid is the only price there is, and it exports as a value
    # because there is no M/O pair for the sheet to average
    unquoted = store.default_state()
    unquoted["positions"] = [store.normalise_position({
        "ticker": "TEST2", "category": "Hardware",
        "legs": [leg(bid=None, ask=None, mid_override=1.25, quote={"status": "missing"})],
    })]
    ps3 = exporter.build_workbook(unquoted)["pre session"]
    row3 = exporter._position_blocks(calc.build_view(unquoted)["positions"])[0]["slot_rows"][0]
    check("override prices an unquoted leg", ps3.cell(row=row3, column=14).value, 1.25)
    check("override: N is a value when there is no book",
          isinstance(ps3.cell(row=row3, column=14).value, float), True)


def test_quote_carry() -> None:
    """A pull that finds no book must carry the last real one, never write 0.00.

    Runs against a stubbed yfinance, so the whole policy -- cache a live book, carry
    it when the chain goes to 0/0, refuse to carry it onto a different contract -- is
    exercised without a network call or a market being open.
    """
    print("quote pull: live book, carried book, no book")
    import types
    import pandas as pd

    def chain(rows):
        return types.SimpleNamespace(calls=pd.DataFrame(rows), puts=pd.DataFrame(rows))

    def stub(rows):
        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol
                self.fast_info = types.SimpleNamespace(last_price=8.37)
                self.calendar = {}

            def option_chain(self, expiry):
                return chain(rows)

        module = types.ModuleType("yfinance")
        module.Ticker = FakeTicker
        return module

    row = {"strike": 9.0, "bid": 0.0, "ask": 0.0, "lastPrice": 0.85,
           "contractSymbol": "BB270115C00009000", "impliedVolatility": 0.5,
           "volume": 1, "openInterest": 10}

    state = store.default_state()
    state["positions"] = [store.normalise_position({
        "ticker": "BB", "category": "Hardware",
        "legs": [{"side": "Long", "type": "CALL", "strike": 9, "expiry": "2027-01-15",
                  "contracts": 1, "cost_premium": 1.0}],
    })]
    real = sys.modules.get("yfinance")
    try:
        # 1) no session running: the chain is priced 0/0, and a 0/0 pair is not a quote
        sys.modules["yfinance"] = stub([row])
        quotes.refresh(state, include_earnings=False)
        leg = state["positions"][0]["legs"][0]
        check("no book: no bid stored", leg["bid"], None)
        check("no book: no ask stored", leg["ask"], None)
        check("no book: mid from the last trade", leg["quote"]["mid"], 0.85)
        check("no book: status", leg["quote"]["status"], "last-only")
        check("no book: nothing cached", leg.get("book"), None)
        check("no book: report counts it", quotes.refresh(state, include_earnings=False)["summary"]["no_book"], 1)

        # 2) the session opens: a real two-sided book lands and is cached
        sys.modules["yfinance"] = stub([dict(row, bid=0.96, ask=1.06)])
        report = quotes.refresh(state, include_earnings=False)
        check("live book: bid stored", leg["bid"], 0.96)
        check("live book: ask stored", leg["ask"], 1.06)
        check("live book: mid is the average", leg["quote"]["mid"], 1.01)
        check("live book: cached with its time", bool(leg["book"]["ts"]), True)
        check("live book: report counts it", report["summary"]["live"], 1)

        # 3) the session closes again: carry the book rather than reverting to 0.00
        sys.modules["yfinance"] = stub([row])
        report = quotes.refresh(state, include_earnings=False)
        check("carried: bid kept", leg["bid"], 0.96)
        check("carried: ask kept", leg["ask"], 1.06)
        check("carried: mid kept", leg["quote"]["mid"], 1.01)
        check("carried: status", leg["quote"]["status"], "carried-book")
        check("carried: stamps when the book was taken", leg["quote"]["book_ts"], leg["book"]["ts"])
        check("carried: report counts it", report["summary"]["carried"], 1)

        # 4) the leg is edited onto another strike: the old book must not follow it
        leg["strike"] = 11
        sys.modules["yfinance"] = stub([dict(row, strike=11.0, contractSymbol="BB270115C00011000")])
        quotes.refresh(state, include_earnings=False)
        check("edited contract does not inherit a book", (leg["bid"], leg["ask"]), (None, None))
    finally:
        if real is None:
            sys.modules.pop("yfinance", None)
        else:
            sys.modules["yfinance"] = real


def write_schema() -> None:
    """Keep data/schema.json in sync with the API contract."""
    import json
    from app.main import get_schema

    target = store.DATA_DIR / "schema.json"
    target.write_text(json.dumps(get_schema(), indent=2) + "\n", encoding="utf-8")
    print(f"schema written: {target}")


def main() -> int:
    test_third_friday()
    test_leg_math()
    test_partial_close()
    test_midpoint()
    test_quote_fill()
    test_quote_carry()
    test_store()
    state = store.load()
    test_grid(state)
    test_export(state)
    test_bias()
    test_degenerate()
    write_schema()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} of {CHECKS} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASSED: all {CHECKS} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
