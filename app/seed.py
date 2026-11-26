"""One-time seeder: import the three tabs of example_sheet.xlsx into the store.

Reads the workbook directly (no hand transcription of numbers):

  * Realised              -> rows 9+ of the "Realised" sheet
  * pre session           -> the 5-row company blocks from row 13, with each
                             block's Long/Short bucket read from the H6/I6..H8/I8
                             formulas of the header summary
  * Actionable Watchlist  -> sector rows with LONG/SHORT candidate columns

Run:  .venv/bin/python -m app.seed  [--xlsx path] [--force]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import openpyxl

from . import calc, store

DEFAULT_XLSX = Path(__file__).resolve().parent.parent.parent / "example_sheet.xlsx"
BLOCK_START = 13
BLOCK_STRIDE = 5  # name row, 2 leg slots, blank, summary

# The watchlist tab is a two-column grid of sector blocks: the sector name sits
# in H and I, candidates below it, and columns G / J carry a sub-sector label or
# a free-text comment for the long / short side of that row. `read_watchlist`
# walks it rather than re-typing it, so nothing gets dropped or promoted to a
# sector by accident.

WL_SECTOR_FILL = "2D4D76"


def _fill_hex(cell) -> str:
    value = getattr(cell.fill.fgColor, "rgb", None)
    return value[-6:].upper() if isinstance(value, str) else ""


def _looks_like_label(text: str) -> bool:
    """'Drug Manufacturers' is a sub-sector; 'Potential Hedge when Bearish' is a note."""
    words = text.split()
    return bool(words) and len(words) <= 4 and all(word[:1].isupper() for word in words)


def read_watchlist(wb) -> list[dict]:
    ws = wb["Actionable Watchlist"]
    heads = [row for row in range(1, ws.max_row + 1)
             if any(_fill_hex(ws.cell(row=row, column=col)) == WL_SECTOR_FILL for col in (8, 9))]
    last_content = max((row for row in range(1, ws.max_row + 1)
                        if any(_text(ws.cell(row=row, column=col).value) for col in (7, 8, 9, 10))),
                       default=0)

    items: list[dict] = []
    for index, head in enumerate(heads):
        sector = next((_text(ws.cell(row=head, column=col).value) for col in (8, 9)
                       if _text(ws.cell(row=head, column=col).value)), "")
        end = heads[index + 1] - 1 if index + 1 < len(heads) else max(last_content, head)
        for row in range(head + 1, end + 1):
            long_ticker = _text(ws.cell(row=row, column=8).value)
            short_ticker = _text(ws.cell(row=row, column=9).value)
            long_note = _text(ws.cell(row=row, column=7).value)
            short_note = _text(ws.cell(row=row, column=10).value)
            for side, ticker, note in (("LONG", long_ticker, long_note),
                                       ("SHORT", short_ticker, short_note)):
                if not ticker:
                    continue
                sub = note if note and _looks_like_label(note) else ""
                comment = note if note and not sub else ""
                items.append({
                    "ticker": ticker.upper(), "side": side, "sector": sector,
                    "sub_sector": sub, "comment": comment,
                    "row_offset": row - head,     # the sheet aligns rows inside a block
                    "source": "manual", "updated_by": "seed",
                })
    return items


def _date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    parsed = calc.parse_date(value)
    return parsed.isoformat() if parsed else None


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _numbers(wb_formulas) -> dict:
    """Map summary-row -> {category, bias} from the H6/I6..H8/I8 formulas.

    Needs the formula view of the workbook (data_only=False): the cached values
    would just show numbers, not which Q-cell each bucket points at.
    """
    ws = wb_formulas["pre session"]
    mapping: dict[int, tuple[str, str]] = {}
    for row in range(5, 12):
        category = _text(ws.cell(row=row, column=7).value)
        for col, bias in ((8, "Long"), (9, "Short")):
            formula = _text(ws.cell(row=row, column=col).value)
            if not formula or "Q" not in formula:
                continue
            for token in formula.replace("+", " ").replace("=", " ").split():
                token = token.strip().upper()
                if token.startswith("Q") and token[1:].isdigit():
                    mapping[int(token[1:])] = (category, bias)
    return mapping


def read_positions(wb, wb_formulas=None) -> list[dict]:
    ws = wb["pre session"]
    buckets = _numbers(wb_formulas or wb)
    positions = []
    row = BLOCK_START
    while row < 200:
        ticker = _text(ws.cell(row=row, column=2).value).upper()
        summary_row = row + BLOCK_STRIDE - 1
        if not ticker:
            row += BLOCK_STRIDE
            continue
        category, sheet_bias = buckets.get(summary_row, ("Unclassified", "Long"))
        legs = []
        for leg_row in (row + 1, row + 2):
            side = _text(ws.cell(row=leg_row, column=5).value)
            kind = _text(ws.cell(row=leg_row, column=6).value).upper()
            expiry = _date(ws.cell(row=leg_row, column=7).value)
            strike = ws.cell(row=leg_row, column=8).value
            contracts = ws.cell(row=leg_row, column=9).value
            cost = ws.cell(row=leg_row, column=12).value
            if not side or not expiry or strike in (None, "", 0) or not contracts:
                continue
            legs.append({
                "side": "Short" if side.lower().startswith("s") else "Long",
                "type": "PUT" if kind.startswith("P") else "CALL",
                "strike": float(strike),
                "expiry": expiry,
                "contracts": int(contracts),
                "multiplier": int(ws.cell(row=leg_row, column=11).value or 100),
                "cost_premium": float(cost or 0),
                "bid": ws.cell(row=leg_row, column=13).value,
                "ask": ws.cell(row=leg_row, column=15).value,
            })
        if legs:
            positions.append({
                "ticker": ticker,
                "company": ticker,
                "category": category,
                "bias": "auto",          # stance derived from the legs, not the sheet label
                "bias_sheet": sheet_bias,
                "next_earnings": _text(ws.cell(row=row, column=4).value),
                "underlying_price": ws.cell(row=row, column=3).value,
                "legs": legs,
                "notes": "",
            })
        row += BLOCK_STRIDE
    return positions


def read_realised(wb) -> list[dict]:
    """Read the closed-trade block, folding leg rows that share a trade number
    into one trade (a calendar is two rows in the sheet, one trade in the store)."""
    ws = wb["Realised"]
    rows = []
    trade_no = None
    row = 9
    while row <= ws.max_row:
        ticker = _text(ws.cell(row=row, column=2).value).upper()
        if not ticker:
            row += 1
            continue
        if ws.cell(row=row, column=1).value not in (None, ""):
            trade_no = int(ws.cell(row=row, column=1).value)
        rows.append({
            "trade_no": trade_no,
            "ticker": ticker,
            "company": _text(ws.cell(row=row, column=3).value) or ticker,
            "side": _text(ws.cell(row=row, column=4).value),
            "type": _text(ws.cell(row=row, column=5).value).upper(),
            "expiry": _date(ws.cell(row=row, column=6).value),
            "strike": ws.cell(row=row, column=7).value,
            "contracts": ws.cell(row=row, column=8).value,
            "multiplier": 100,
            "price_in": ws.cell(row=row, column=9).value,
            "price_out": ws.cell(row=row, column=10).value,
            "entry_date": _date(ws.cell(row=row, column=11).value),
            "exit_date": _date(ws.cell(row=row, column=12).value),
            "commission": ws.cell(row=row, column=18).value or 0,
            "notes": "",
        })
        row += 1
        if row > ws.max_row:
            break
        # stop at a long run of empty rows
        if all(ws.cell(row=r, column=2).value in (None, "") for r in range(row, min(row + 5, ws.max_row + 1))):
            break
    return store.migrate_flat_trades(rows)


def build_state(xlsx: Path) -> dict:
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    wb_formulas = openpyxl.load_workbook(xlsx, data_only=False)
    state = store.default_state()
    state["meta"].update({
        "name": "4Pillar options book",
        "opening_balance": 40000.0,
        "updated_by": "seed",
    })
    state["positions"] = [store.normalise_position(p) for p in read_positions(wb, wb_formulas)]
    state["realised"] = [store.normalise_trade(t) for t in read_realised(wb)]
    state["watchlist"] = [store.normalise_watch(item) for item in read_watchlist(wb)]
    cats = []
    for pos in state["positions"]:
        if pos["category"] not in cats:
            cats.append(pos["category"])
    state["categories"] = cats
    store.log(state, "seed.import", f"imported {xlsx.name}", "seed")
    return store.normalise_state(state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    parser.add_argument("--force", action="store_true", help="overwrite an existing store")
    args = parser.parse_args(argv)

    if not args.xlsx.exists():
        print(f"error: {args.xlsx} not found", file=sys.stderr)
        return 1
    if store.STORE_PATH.exists() and not args.force:
        print(f"error: {store.STORE_PATH} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    state = build_state(args.xlsx)
    store.save(state, actor="seed")

    view = calc.build_view(state)
    print(f"wrote {store.STORE_PATH}")
    print(f"  positions : {len(state['positions'])}")
    sheet_labels = {p["ticker"]: p.get("bias_sheet") for p in state["positions"]}
    for pos in view["positions"]:
        note = ""
        if sheet_labels.get(pos["ticker"]) and sheet_labels[pos["ticker"]] != pos["bias"]:
            note = f"   (the sheet had {sheet_labels[pos['ticker']]})"
        print(f"      {pos['ticker']:<6} {pos['category']:<14} {pos['bias']:<6} "
              f"legs={pos['leg_count']} P/L={pos['position_pl']} book={pos['book_months']}{note}")
    print(f"  realised  : {view['kpis']['closed_trades']} trades / {view['kpis']['legs']} legs, "
          f"closed P/L = {view['kpis']['closed_pl']}, win {view['kpis']['win_rate']}")
    for t in view["realised"]:
        print(f"      #{t['trade_no']} {t['ticker']:<6} {t['structure_label']:<9} "
              f"{t['pl']:>9.2f}  {t['legs_label']}")
    print(f"  watchlist : {len(state['watchlist'])} items")
    print(f"  months    : {[m['label'] for m in view['months']['months']]}")
    print(f"  account balance = {view['summary']['account_balance']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
