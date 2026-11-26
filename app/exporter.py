"""Export the book back into a copy of the original workbook.

The three tabs are *the user's own spreadsheet*, not a redraw of it: this module
opens `template.xlsx` (a byte-for-byte copy of `example_sheet.xlsx`) and writes
our numbers into the cells that already exist. Everything the sheet carries --
fonts, fills, borders, number formats, column widths, row heights, freeze panes,
hidden gridlines, tab colours, conditional colouring, data validations and the
defined names -- comes along untouched, because we never create a style.

What gets written where:

  Realised
      Rows 9+ one row per leg (a calendar shares one trade number, exactly as
      the sheet did). Columns M..P keep the sheet's own formulas, the KPI block
      in D1:E6 / H2:O2 is left alone so it recalculates from column O.

  pre session
      S5 opening balance, S6:S10 the balance stack, G6:I10 the category x
      Long/Short market-value matrix, then one 5-row block per position from
      row 13 (name row, three leg slots, totals row) with the sheet's formulas.
      To the right, one 3-column block per month (Known / Actual / Swing, plus
      its cumulative) is re-emitted for whatever months the book currently
      needs -- this is the part that moves; the trading-action column rides
      along after them. **Known / worse case is a live formula, not a number**:
      it sums, over the block's own leg rows, `-P` for every leg whose expiry
      books into that month (see _known_worse_formula). Change a leg's premium,
      contracts or expiry in Excel and the month re-books itself; the month's
      third-Friday cutoff sits in row 5 above its Known column as the anchor the
      formula reads.
      A leg row is colour-coded by the sheet's own rule: E tinted by side, F by
      type -- CALL dark blue, PUT orange. Those two cells are restamped from
      the keyword actually written (see leg_role_styles), because the row
      otherwise inherits the look of whichever template row it landed on.

  Actionable Watchlist
      The template's sector blocks, filled from our candidates: LONG tickers in
      H, SHORT in I, sub-sector / comment text in G and J. Every sector the
      sheet drew keeps its header row and its row allocation; a block only
      grows when the book has more candidates than the sheet left room for.
      Each value is stamped with the sheet's own cell look for its role (the
      LONG blue, the SHORT red, the bold comment), taken by majority vote over
      the sheet's own body cells -- so every ticker on the tab is styled the
      same way the sheet styles the rest of them, and rows the sheet left blank
      do not export as plain black Calibri.

Only *values* and *formulas* are written here; styles are copied from the
template when a row has to be created (more legs / months / candidates than the
sheet already had room for).
"""

from __future__ import annotations

import copy
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from . import calc

TEMPLATE = Path(__file__).with_name("template.xlsx")

# ---------------------------------------------------------------- geometry --
REALISED_FIRST_ROW = 9
REALISED_LAST_COL = 18            # R = Commission is the last column the sheet uses
REALISED_TEMPLATE_ROWS = 20       # rows 9..20 carry the sheet's own formatting

PS_BLOCK_START = 13               # first position block
PS_SLOT_MIN = 3                   # the sheet always leaves three leg slots
PS_MONTH_START = 21               # column U = first month block
PS_MONTH_WIDTHS = (15.57, 12.86, 13.43, 2.71)   # known, actual, swing, spacer
PS_ACTION_WIDTH = 42.0
PS_ACTION_TRAIL = 2.71
PS_REF_TICKER_ROW = 13            # style donors inside the template
PS_REF_SLOT_ROWS = (14, 15, 16)
PS_REF_TOTAL_ROW = 17
PS_TEMPLATE_SLOTS = 3             # leg slots in each of the sheet's own blocks
LEG_SIDE_COL = 5                  # E: Long / Short
LEG_TYPE_COL = 6                  # F: CALL / PUT -- the sheet colours these two
# the month blocks and the trading-action column are read from the template
# itself (see _template_layout), so any sheet can be dropped in.

WL_BAND_FILL = "2D4D76"           # the sector-header band the sheet paints in H and I
WL_SCAN_ROWS = 1000              # the sheet's used range stops at row 1000
WL_TABLE_COLS = range(1, 19)     # A..R: the block table plus the sheet's column strip


# ------------------------------------------------------------------ helpers --
def _copy_style(src, dst) -> None:
    dst._style = copy.copy(src._style)


from openpyxl.styles import Border, Side  # noqa: E402  (border surgery below)


def _set_border_sides(ws: Worksheet, row: int, last_col: int,
                      top: str | None = None, bottom: str | None = None) -> None:
    """Rewrite a row's top and/or bottom rule, leaving its other sides alone.

    A leg row's look comes from the sheet's Long or Short row, which carries whatever
    rules *that* row happened to need; the rules a row needs are decided by its role in
    the box, so they are set here rather than inherited. Sides left as None are kept as
    they are, which is what keeps the left edge (the thick ticker rule) intact.
    """
    for col in range(1, last_col + 1):
        cell = ws.cell(row=row, column=col)
        border = cell.border
        top_side = border.top
        bottom_side = border.bottom
        cell.border = Border(
            left=border.left, right=border.right,
            top=(Side(style=top, color=getattr(top_side, "color", None)) if top else border.top),
            bottom=(Side(style=bottom, color=getattr(bottom_side, "color", None)) if bottom else border.bottom),
        )


def _stamp_code(cell, code) -> None:
    """Adopt the sheet's colour code: another cell's (font, fill), nothing else.

    Only those two travel. Borders, alignment and the number format stay as the row
    already has them, because the template draws those by position inside a block (the
    last leg slot carries a medium rule underneath, the slots above it a thin one), and
    stealing a donor's borders there would break the block's outline. The font comes
    along so the keyword lands white on its colour instead of inheriting an empty
    row's dark text.
    """
    font, fill = code
    cell._style = copy.copy(cell._style)
    cell.fill = copy.copy(fill)
    cell.font = copy.copy(font)


def _blank(value) -> bool:
    return value in (None, "")

def _rgb(cell) -> str:
    """Lower-case hex of a cell's fill, or '' when it has none."""
    value = getattr(cell.fill.fgColor, "rgb", None)
    return value[-6:].upper() if isinstance(value, str) else ""


def _num(value) -> float | None:
    return None if value is None else float(value)


def _short_date(value: date | datetime | None):
    if value is None:
        return None
    return datetime(value.year, value.month, value.day)


def _title(text: str | None, default: str = "") -> str:
    text = (text or default).strip()
    return text[:1].upper() + text[1:].lower() if text else ""


def _drop_width(ws: Worksheet, col: int) -> None:
    """Forget a column width the template set (openpyxl needs the entry removed)."""
    letter = _letter(col)
    if letter in ws.column_dimensions:
        del ws.column_dimensions[letter]


def _clear(ws: Worksheet, row: int, cols) -> None:
    for col in cols:
        ws.cell(row=row, column=col).value = None


def _last_used_row(ws: Worksheet, first_row: int, last_col: int, cap: int = 250) -> int:
    """The lowest row of the template's own grid that still holds a value.

    Read before we write, so `_write_presession` knows how far down the sheet's example
    book reaches and can clear all of it. Bounded by `cap` rows and never past the
    sheet's used range, because touching cells below it would materialise empty ones.
    """
    last = first_row - 1
    limit = min(ws.max_row, first_row + cap)
    for row in range(first_row, limit + 1):
        if any(not _blank(ws.cell(row=row, column=col).value) for col in range(1, last_col + 1)):
            last = row
    return last


# ------------------------------------------------------------------ Realised --
def _write_realised(ws: Worksheet, view: dict) -> None:
    # the sheet lists trades in the order they were numbered, oldest first
    trades = sorted(view["realised"], key=lambda t: (t.get("trade_no") or 0, t.get("entry_date") or ""))

    # rows 9..20 are the sheet's own hand-formatted block (the first trade is
    # tinted, the rest are white with grey calculation columns). Below them the
    # sheet repeats the white/grey pattern, so grow from row 20 and never
    # repaint a row the template already styled.
    last_styled = REALISED_TEMPLATE_ROWS

    rows = [(trade, leg, index == 0) for trade in trades
            for index, leg in enumerate(trade["legs"])]
    last_row = REALISED_FIRST_ROW + max(len(rows), 1) - 1

    # wipe values (never the M..P formulas of rows we do not use)
    for row in range(REALISED_FIRST_ROW, max(last_styled, last_row) + 6):
        _clear(ws, row, list(range(1, 13)) + [REALISED_LAST_COL])

    for offset, (trade, leg, is_first) in enumerate(rows):
        row = REALISED_FIRST_ROW + offset
        if row > last_styled:                        # borrow the sheet's own look
            for col in range(1, REALISED_LAST_COL + 1):
                _copy_style(ws.cell(row=last_styled, column=col), ws.cell(row=row, column=col))

        ws.cell(row=row, column=1).value = trade["trade_no"] if is_first else None
        ws.cell(row=row, column=2).value = trade["ticker"]
        ws.cell(row=row, column=3).value = trade.get("company") or trade["ticker"]
        ws.cell(row=row, column=4).value = _title(leg["side"])
        ws.cell(row=row, column=5).value = _title(leg["type"])
        ws.cell(row=row, column=6).value = _short_date(calc.parse_date(leg["expiry"]))
        ws.cell(row=row, column=7).value = float(leg["strike"])
        ws.cell(row=row, column=8).value = float(leg["contracts"])
        ws.cell(row=row, column=9).value = float(leg["price_in"])
        ws.cell(row=row, column=10).value = float(leg["price_out"])
        ws.cell(row=row, column=11).value = _short_date(
            calc.parse_date(leg.get("entry_date") or trade["entry_date"]))
        ws.cell(row=row, column=12).value = _short_date(
            calc.parse_date(leg.get("exit_date") or trade["exit_date"]))
        ws.cell(row=row, column=13).value = f"=L{row}-K{row}"
        ws.cell(row=row, column=14).value = f'=IF(D{row}="Long",(J{row}-I{row})/I{row},-(J{row}-I{row})/I{row})'
        ws.cell(row=row, column=15).value = (
            f'=IF(D{row}="Long",(J{row}-I{row})*H{row}*100,(I{row}-J{row})*H{row}*100)')
        ws.cell(row=row, column=16).value = f'=IF(O{row}>0,1,IF(O{row}<0,-1,0))'
        if is_first:
            ws.cell(row=row, column=REALISED_LAST_COL).value = trade.get("commission") or 0

    # the sheet's own average-days formula stops wherever its data stopped
    ws.cell(row=2, column=13).value = f"=average(M{REALISED_FIRST_ROW}:M{last_row})"


# -------------------------------------------------------------- pre session --
def _template_block_rows(index: int) -> dict:
    """Where the sheet drew block `index`: name row, three leg slots, totals row."""
    ticker = PS_BLOCK_START + index * (PS_TEMPLATE_SLOTS + 2)
    slots = [ticker + step for step in range(1, PS_TEMPLATE_SLOTS + 1)]
    return {"ticker": ticker, "slots": slots, "total": slots[-1] + 1}


def _drawn_template_blocks(ws: Worksheet) -> int:
    """How many position blocks the template actually drew.

    The sheet's own example book stops somewhere -- this template draws five. Any
    position past that lands on rows the sheet never styled, and copying a look
    from them yields a block with no box, no band and no totals rule. Counting the
    ticker rows (column B) is how the drawn blocks are found; everything beyond
    them borrows the first block's look instead.
    """
    drawn = 0
    limit = min(ws.max_row, PS_BLOCK_START + 60 * (PS_TEMPLATE_SLOTS + 2))
    for row in range(PS_BLOCK_START, limit + 1):
        if not _blank(ws.cell(row=row, column=2).value):
            drawn += 1
    return drawn


def _position_blocks(positions: list[dict]) -> list[dict]:
    """One block per position: name row, leg slots, one spare row, totals row.

    The sheet's block is a name row, two leg rows, a *blank* row and the dark totals
    row -- the blank one is what keeps the totals rule off the last leg. A three-leg
    position fills the spare if it is not reserved, which is why a three-leg export put
    the totals row straight under the short leg; every block therefore keeps one slot
    free, so a position with three legs takes six rows rather than five. Blocks with
    one or two legs are unchanged.
    """
    blocks, row = [], PS_BLOCK_START
    for position in positions:
        legs = [leg for leg in position["legs"] if leg["contracts"]]
        slots = max(PS_SLOT_MIN, len(legs) + 1)
        blocks.append({
            "position": position,
            "legs": legs,
            "ticker_row": row,
            "slot_rows": list(range(row + 1, row + 1 + slots)),
            "spare_rows": list(range(row + 1 + len(legs), row + 1 + slots)),
            # the sheet closes a one-leg box on the empty row beneath the leg
            "close_rows": [row + 2] if len(legs) == 1 else [],
            "total_row": row + 1 + slots,
        })
        row += slots + 2
    return blocks


def leg_role_styles(ws: Worksheet) -> dict:
    """The sheet's own fill for a leg's side cell and its type cell.

    The workbook codes a leg twice in colour: E is tinted by side, and F by type --
    CALL is the dark slate blue, PUT the theme's orange accent. Reading those looks
    off the sheet's own leg rows is what keeps them true: a row inherits the style of
    whatever template row it lands on, so a PUT booked into a slot the sheet drew for
    a CALL would otherwise come out blue. Only the two cells that carry the code are
    restamped; every other column of the row keeps its inherited style.

    Returns {(column, "CALL"|"PUT"|"LONG"|"SHORT"): (font, fill)}. A sheet that does not
    colour a keyword simply gets no entry, and that cell keeps the row's look.
    """
    styles: dict[tuple[int, str], tuple] = {}
    used_range = min(ws.max_row, 500)
    for row in range(PS_BLOCK_START, used_range + 1):
        type_cell = ws.cell(row=row, column=LEG_TYPE_COL)
        side_cell = ws.cell(row=row, column=LEG_SIDE_COL)
        kind = str(type_cell.value or "").strip().upper()
        side = str(side_cell.value or "").strip().upper()
        if kind not in ("CALL", "PUT") or side not in ("LONG", "SHORT"):
            continue
        if type_cell.fill is not None and type_cell.fill.patternType:
            styles.setdefault((LEG_TYPE_COL, kind),
                              (copy.copy(type_cell.font), copy.copy(type_cell.fill)))
        if side_cell.fill is not None and side_cell.fill.patternType:
            styles.setdefault((LEG_SIDE_COL, side),
                              (copy.copy(side_cell.font), copy.copy(side_cell.fill)))
    return styles


def _template_layout(ws: Worksheet) -> dict:
    """Read the month-block geometry out of the template before we touch it.

    Whatever sheet you drop in as `template.xlsx` decides where the months start,
    how many blocks the sheet already drew (each one is a style donor) and where
    the trading-action column sits.
    """
    month_starts: list[int] = []
    action_col = None
    # stay inside the sheet's used range: touching ws.cell() beyond it would
    # materialise empty cells and inflate the workbook's dimensions
    for col in range(1, min(ws.max_column, 250) + 1):
        head = ws.cell(row=12, column=col).value
        if _blank(head):
            continue
        text = str(head).strip().lower()
        if text.startswith("known"):
            month_starts.append(col)
        elif text.startswith("trading action"):
            action_col = col
        elif text.startswith("actual") or text.startswith("swing"):
            continue
    month_starts = month_starts or [PS_MONTH_START]
    if action_col is None:
        action_col = month_starts[-1] + 3

    def width(col: int, fallback: float) -> float:
        value = ws.column_dimensions.get(_letter(col))
        got = getattr(value, "width", None)
        return float(got) if isinstance(got, (int, float)) else fallback

    widths = []
    for base in month_starts:
        widths.append([width(base + offset, PS_MONTH_WIDTHS[offset]) for offset in range(4)])
    return {
        "month_starts": month_starts,
        "widths": widths,
        "action_col": action_col,
        "action_width": width(action_col, PS_ACTION_WIDTH),
        "trail_width": width(action_col + 1, PS_ACTION_TRAIL),
        "leg_styles": leg_role_styles(ws),
    }


def _leg_booking_key(row: int, rule: str) -> str:
    """Excel for `calc.booking_period`: which month a leg's expiry books into.

    The house rule (third Friday) is expressed with the sheet's own date
    arithmetic: the third Friday of the expiry's month is the last day that books
    there, and anything later rolls into the following month. The result is a
    month key (`YEAR*12+MONTH`) so two months can be compared with `=` instead of
    a string. A blank expiry yields 0, which matches no real month.
    """
    d = f"G{row}"
    year_month = f"YEAR({d})*12+MONTH({d})"
    if rule == "calendar":
        return f'IF({d}="",0,{year_month})'
    third_friday = (f"DATE(YEAR({d}),MONTH({d}),"
                    f"1+MOD(4-WEEKDAY(DATE(YEAR({d}),MONTH({d}),1),3),7)+14)")
    return (f'IF({d}="",0,IF({d}<={third_friday},{year_month},{year_month}+1))')


def _known_worse_formula(block: dict, month: dict, rule: str, anchor: str) -> str:
    """The `Known / worse case` cell as a formula the sheet recalculates itself.

    The number is the same one `calc` computes -- the book's worst case for the
    month: every leg that books into it is assumed to expire worthless, so a **short
    keeps the credit** (a positive contribution) and a **long loses the premium it
    paid** (a negative one). The direction comes from the side cell alone (`E`), and
    the magnitude from `ABS(L)*I*K`, so a premium typed as a credit (negative) or as
    a debit prices the same way -- the sheet cannot be pushed into the wrong sign by
    a data-entry convention.

    Why a formula rather than a stamped value: the premium, contracts and expiry
    are all editable cells on this tab, and a static number silently disagrees
    with them the moment one is typed. `anchor` is the cell holding the month's
    own cutoff date (row 5 above the Known column), so the months stay data too --
    the formula reads it rather than hard-coding a date.

    A leg whose booking month was pinned by hand (`book_month_override`) cannot be
    expressed this way -- the sheet has no field for the override -- so that leg
    contributes its booked amount as a literal, and the rest stay live.
    """
    column_key = f"(YEAR({anchor})*12+MONTH({anchor}))"
    legs_by_row = dict(zip(block["slot_rows"], block["legs"]))
    terms: list[str] = []
    for row in block["slot_rows"]:
        leg = legs_by_row.get(row)
        if leg is not None and leg.get("book_month_override"):
            if leg.get("effective_book_month") == month["key"]:
                terms.append(f"{float(leg['booked_amount']):.2f}")
            continue
        terms.append(
            f'IF({_leg_booking_key(row, rule)}={column_key},'
            f'IF(E{row}="Long",-ABS(L{row})*I{row}*K{row},ABS(L{row})*I{row}*K{row}),0)')
    return "=" + "+".join(terms) if terms else "=0"


def _write_month_anchors(ws: Worksheet, months: list[dict], month_cols: list[int],
                         rule: str) -> list[str]:
    """Stamp each month block's cutoff date into row 5, above its Known column.

    The template titles that row `3RD FRIDAY OF THE MONTH` but leaves it empty, so
    the dates belong there: for the third-Friday rule the cell holds the month's
    third Friday itself (the last day that books into it), for the calendar rule
    the first of the month. The `Known / worse case` formula reads these cells,
    which is what lets a user insert or re-date a month block without editing any
    formula. Returns the addresses, in month order.
    """
    anchors: list[str] = []
    for index, base in enumerate(month_cols):
        month = months[index]
        try:
            cutoff = datetime.fromisoformat(str(month["third_friday"]))
        except (KeyError, ValueError):
            cutoff = datetime(month["year"], int(str(month["key"])[5:7]), 1)
        if rule == "calendar":
            cutoff = datetime(cutoff.year, cutoff.month, 1)
        cell = ws.cell(row=5, column=base)
        cell.value = cutoff
        cell.number_format = "dd mmm"
        anchors.append(cell.coordinate)
    return anchors


def _write_presession(ws: Worksheet, view: dict) -> None:
    layout = _template_layout(ws)
    template_months = len(layout["month_starts"])
    positions = view["positions"]
    months = view["months"]["months"]
    blocks = _position_blocks(positions)
    last_block_row = blocks[-1]["total_row"] if blocks else PS_BLOCK_START + 4
    month_cols = [PS_MONTH_START + 4 * index for index in range(len(months))]
    # the booking rule decides which month a leg books into, and the formulas below
    # have to express the same rule the app used or the sheet would disagree with it
    rule = (view.get("meta") or {}).get("effective_rule") or "third_friday"
    action_col = PS_MONTH_START + 4 * len(months)
    region_end = action_col + 1
    template_action = layout["action_col"]
    template_end = min(ws.max_column, 250)   # the template's own right-hand edge
    # Counted *before* the wipe: the sheet's drawn blocks are identified by their
    # ticker names in column B, and wiping clears them. Counting afterwards said
    # zero and sent every block to the first block's look, which is how a native
    # block's own rules (the medium separator above row 33, the format of a bid
    # cell) went missing.
    drawn_blocks = _drawn_template_blocks(ws)

    # ---- wipe the whole right-hand region plus the position grid -----------
    # The template is a copy of the user's own sheet, so it carries *their* example
    # book in the same grid -- HOOD at 95.07 with a real 8.60/8.75, and empty leg
    # slots holding literal 0.00 bid/ask/mid. Clearing only the rows our blocks
    # occupy left that sample data underneath a smaller book, which is how an export
    # could show a bid/ask pair that never came from yfinance at all. Every row the
    # grid could use is cleared instead, down to the lowest row the template fills.
    wipe_to = max(last_block_row, 60, _last_used_row(ws, PS_BLOCK_START, 20))
    for row in range(5, wipe_to + 1):
        for col in range(PS_MONTH_START, max(region_end, template_end) + 1):
            ws.cell(row=row, column=col).value = None
    for row in range(PS_BLOCK_START, wipe_to + 1):
        _clear(ws, row, range(1, 21))

    # ---- restyle the month region ----------------------------------------
    # the sheet drew three blocks; reuse each of them as its own style donor so
    # the first three months look exactly like the original, and clone the last
    # one for any further month the book needs
    region_rows = range(5, max(last_block_row, 60) + 1)
    for index, base in enumerate(month_cols):
        donor_index = min(index, template_months - 1)
        donor = layout["month_starts"][donor_index]
        donor_widths = layout["widths"][donor_index]
        for offset in range(4):
            src, dst = donor + offset, base + offset
            if src != dst:
                for row in region_rows:
                    _copy_style(ws.cell(row=row, column=src), ws.cell(row=row, column=dst))
        for offset in range(4):
            dst = base + offset
            ws.column_dimensions[_letter(dst)].width = donor_widths[offset]
    if action_col != template_action:
        for row in region_rows:
            _copy_style(ws.cell(row=row, column=template_action),
                        ws.cell(row=row, column=action_col))
        ws.column_dimensions[_letter(action_col)].width = layout["action_width"]
        ws.column_dimensions[_letter(action_col + 1)].width = layout["trail_width"]

    # drop the widths of columns the template used but this book no longer needs
    for col in range(action_col + 2, max(template_action + 12, action_col + 12)):
        _drop_width(ws, col)
    fallback_widths = layout["widths"][template_months - 1]
    for col in range(PS_MONTH_START, action_col + 2):
        dim = ws.column_dimensions.get(_letter(col))
        if dim is None or dim.width is None:
            ws.column_dimensions[_letter(col)].width = fallback_widths[(col - PS_MONTH_START) % 4]

    # ---- position grid ----------------------------------------------------
    # the cutoff dates the Known / worse case formulas read, written after the
    # wipe above so they are not cleared with the rest of the right-hand region
    anchors = _write_month_anchors(ws, months, month_cols, rule)

    span = range(1, region_end + 2)

    def snapshot(row: int) -> dict:
        return {col: copy.copy(ws.cell(row=row, column=col)._style) for col in span}

    # The sheet draws a fixed number of example blocks. A book with more positions
    # than that runs off the bottom of them -- and those rows carry no styling at
    # all, so an extra position used to export as bare rows with no box, no band and
    # no totals rule. The sheet's *first* block is the reference for every block it
    # never drew, which is what PS_REF_* points at. Every look is snapshotted here,
    # before any block is written: the donors are often the destination rows
    # themselves, and restyling row N would otherwise become row N+1's donor.
    ref_ticker_look = snapshot(PS_REF_TICKER_ROW)
    ref_slot_looks = [snapshot(row) for row in PS_REF_SLOT_ROWS]
    ref_total_look = snapshot(PS_REF_TOTAL_ROW)

    looks: list[dict] = []
    for index in range(len(blocks)):
        if index < drawn_blocks:
            rows = _template_block_rows(index)
            looks.append({"ticker": snapshot(rows["ticker"]),
                          "slots": [snapshot(row) for row in rows["slots"]],
                          "total": snapshot(rows["total"])})
        else:
            looks.append({"ticker": ref_ticker_look, "slots": ref_slot_looks,
                          "total": ref_total_look})

    for index, block in enumerate(blocks):
        position, legs = block["position"], block["legs"]
        ticker_row, total_row = block["ticker_row"], block["total_row"]
        look = looks[index]

        if ticker_row != (PS_REF_TICKER_ROW if index >= drawn_blocks
                          else _template_block_rows(index)["ticker"]):
            for col in span:
                ws.cell(row=ticker_row, column=col)._style = copy.copy(look["ticker"][col])
        if total_row != (PS_REF_TOTAL_ROW if index >= drawn_blocks
                         else _template_block_rows(index)["total"]):
            for col in span:
                ws.cell(row=total_row, column=col)._style = copy.copy(look["total"][col])
        # Each leg row takes the sheet's look for *its own side* -- the Long row's when
        # the leg is long, the Short row's when it is short -- and only the rules are
        # decided by position. Styling by position instead put a Long leg on the Short
        # row's white fill whenever it happened to land in the closing slot, which is
        # what made a three-leg block read as though its legs did not belong together.
        #
        # The box itself: first leg gets the medium rule above and a thin one below,
        # interior legs thin on both sides, the last leg thin above and the medium rule
        # below. Then come the block's spare rows -- blank, carrying the sheet's empty
        # row look and *no* rules, so the totals row's dark line is not the next thing
        # under the short leg.
        long_look = ref_slot_looks[0]
        short_look = ref_slot_looks[1] if len(ref_slot_looks) > 1 else ref_slot_looks[0]
        spare_look = ref_slot_looks[-1] if len(ref_slot_looks) > 1 else ref_slot_looks[0]
        close_rows = set(block.get("close_rows") or ())
        close_slots = {len(legs)} if close_rows else set()
        for slot_index, row in enumerate(block["slot_rows"]):
            if slot_index >= len(legs):
                # The sheet's own blank rows -- its third slot, and the row it closes a
                # one-leg box on -- are left exactly as it drew them. A row that is a
                # spare only because this block grew (a third leg pushed it down) has to
                # be repainted, or it keeps whatever the sheet drew there: the dark
                # totals band, which read as a second black line under the short leg.
                sheet_drew_it = index < drawn_blocks and (slot_index == 2 or slot_index in close_slots)
                if sheet_drew_it:
                    continue
                for col in span:
                    ws.cell(row=row, column=col)._style = copy.copy(spare_look[col])
                continue
            leg = legs[slot_index]
            row_look = (long_look if str(leg.get("side") or "").lower().startswith("long")
                        else short_look)
            for col in span:
                ws.cell(row=row, column=col)._style = copy.copy(row_look[col])
            first, last = slot_index == 0, slot_index == len(legs) - 1
            _set_border_sides(ws, row, region_end + 1,
                              top="medium" if first else "thin",
                              bottom="medium" if (last and len(legs) > 1) else "thin")
        # A block with one leg draws its box across the row below the leg -- the sheet
        # does this on its own one-leg blocks, closing the rule under an empty row. On a
        # block the sheet drew, that row is its own artwork and is left exactly as it is;
        # only a block that ran past the drawn ones borrows the spare look for it.
        if index >= drawn_blocks:
            for row in close_rows:
                for col in span:
                    ws.cell(row=row, column=col)._style = copy.copy(spare_look[col])
                _set_border_sides(ws, row, region_end + 1, top="thin", bottom="medium")

        ws.cell(row=ticker_row, column=2).value = position["ticker"]
        ws.cell(row=ticker_row, column=3).value = _num(position.get("underlying_price"))
        ws.cell(row=ticker_row, column=4).value = position.get("next_earnings") or None
        if block is blocks[0]:
            ws.cell(row=ticker_row, column=PS_MONTH_START + 2).value = "V-U"

        for row, leg in zip(block["slot_rows"], legs):
            ws.cell(row=row, column=LEG_SIDE_COL).value = _title(leg["side"])
            ws.cell(row=row, column=LEG_TYPE_COL).value = (leg["type"] or "").upper()
            # E and F carry the sheet's own coding (side tint, CALL blue / PUT orange).
            # The row borrowed a donor's style above, so restamp those two from the
            # sheet's look for the value we just wrote -- otherwise a PUT keeps the
            # blue of whichever CALL row donated the style.
            for col, key in ((LEG_SIDE_COL, str(leg.get("side") or "").strip().upper()),
                             (LEG_TYPE_COL, str(leg.get("type") or "").strip().upper())):
                style = layout["leg_styles"].get((col, key))
                if style is not None:
                    _stamp_code(ws.cell(row=row, column=col), style)
            ws.cell(row=row, column=7).value = _short_date(calc.parse_date(leg["expiry"]))
            ws.cell(row=row, column=8).value = float(leg["strike"])
            ws.cell(row=row, column=9).value = float(leg["contracts"])
            ws.cell(row=row, column=10).value = f"=+C{ticker_row}"
            ws.cell(row=row, column=11).value = float(leg["multiplier"] or 100)
            ws.cell(row=row, column=12).value = float(leg["cost_premium"] or 0)
            bid, ask, mid = leg.get("bid"), leg.get("ask"), leg.get("mid")
            # A book exists when at least one side is real. Yahoo prices every
            # contract 0.00 / 0.00 outside a session, and that pair is the *absence*
            # of a quote: writing it would put a 0.00 midpoint on the sheet and the
            # leg's own mid would never be reached. One genuine zero (`0.00 / 0.15`,
            # a thin chain) is still a quote and still exports as the sheet's own
            # `(M+O)/2`.
            has_book = bid is not None and ask is not None and (bid > 0 or ask > 0)
            if has_book:
                # BID and ASK are observed data and always go out when they exist --
                # a manual mid must never be the reason two real columns export blank.
                ws.cell(row=row, column=13).value = float(bid)
                ws.cell(row=row, column=15).value = float(ask)
                # the sheet's own midpoint, as a formula, exactly as the pre session tab
                # shows it: leg_mid() computes (M+O)/2 whenever a book exists, so N and
                # its neighbours can no longer disagree
                ws.cell(row=row, column=14).value = f"=+(M{row}+O{row})/2"
            else:                                    # no two-sided quote: use our mid
                # this is the only place a hand-typed mid survives, and it survives as a
                # value: there is no M/O pair for the sheet to average
                ws.cell(row=row, column=14).value = None if mid is None else float(mid)
            ws.cell(row=row, column=16).value = f'=IF(E{row}="Long",L{row}*I{row}*K{row},-L{row}*I{row}*K{row})'
            ws.cell(row=row, column=17).value = f'=IF(E{row}="Long",N{row}*I{row}*K{row},-N{row}*I{row}*K{row})'
            ws.cell(row=row, column=18).value = f'=IF(E{row}="Long",N{row}-L{row},L{row}-N{row})'
            ws.cell(row=row, column=19).value = (
                f'=IF(OR(R{row}="",I{row}="",K{row}=""),"",R{row}*I{row}*K{row})')

        first_slot, last_slot = block["slot_rows"][0], block["slot_rows"][-1]
        for col in (16, 17, 18, 19):
            letter = _letter(col)
            ws.cell(row=total_row, column=col).value = f"=SUM({letter}{first_slot}:{letter}{last_slot})"
        # The trading action is the block's *net* cost, not the first leg's. It used
        # to read `-P{first_slot}`, which for a multi-leg position subtracted one leg
        # and quoted a number the block total beside it contradicted.
        ws.cell(row=total_row, column=action_col).value = (
            f"=-P{total_row}" if legs else None)

        # Known / Actual / Swing per month
        for index, base in enumerate(month_cols):
            known, actual, swing = (ws.cell(row=total_row, column=base + offset).coordinate
                                    for offset in range(3))
            # live, not stamped: the legs' own premium/contracts/expiry cells feed it
            ws[known].value = _known_worse_formula(block, months[index], rule, anchors[index])
            ws[actual].value = f"=+S{total_row}"
            ws[swing].value = f"=+{actual}-{known}"

    # ---- month headers, realised-per-month, cumulative known chain ---------
    ws.cell(row=5, column=PS_MONTH_START + 1).value = "3RD FRIDAY OF THE MONTH"
    previous = "S6"
    as_of_date = calc.parse_date(view["summary"]["as_of"])
    for index, base in enumerate(month_cols):
        month = months[index]
        label = month["short_label"]
        if as_of_date and month["year"] != as_of_date.year:
            label = f"{label} {str(month['year'])[2:]}"      # the sheet only showed one year
        ws.cell(row=6, column=base + 1).value = label
        ws.cell(row=7, column=base + 1).value = "Realized"
        ws.cell(row=7, column=base + 2).value = float(month["realised_pl"] or 0)
        ws.cell(row=12, column=base).value = "Known /worse case"
        ws.cell(row=12, column=base + 1).value = "Actual"
        ws.cell(row=12, column=base + 2).value = "Swing factor"
        known_cells = "+".join(ws.cell(row=block["total_row"], column=base).coordinate
                               for block in blocks) if blocks else "0"
        ws.cell(row=10, column=base).value = f"=+{previous}+{known_cells}"
        previous = f"{_letter(base)}10"
    ws.cell(row=12, column=action_col).value = "Trading action"

    # ---- left-hand summary block -----------------------------------------
    summary = view["summary"]
    ws.cell(row=5, column=19).value = float(summary["opening_balance"])
    ws.cell(row=6, column=19).value = "=+Realised!O2"
    total_rows = [block["total_row"] for block in blocks]
    ws.cell(row=7, column=19).value = "=+" + "+".join(f"S{row}" for row in total_rows) if total_rows else "=0"
    ws.cell(row=8, column=19).value = "=-SUM(Realised!R9:R400)"
    ws.cell(row=10, column=19).value = "=+S5+S6+S7-S8"

    _write_categories(ws, positions, blocks)


def _write_categories(ws: Worksheet, positions: list[dict], blocks: list[dict]) -> None:
    """G6:H10 -- category x Long/Short gross market value, pointing at block Q cells.

    A category lands in a bucket by *label*, not by string equality: the sheet
    draws three buckets and names them itself, so the sheet's spelling wins and
    `"Real Economy"` is the same bucket as `"Real economy"`. The category field
    is free text in the UI, so a capital letter typed once must not become a
    fourth bucket and take the export down with it. Only a genuinely distinct
    label is a new bucket -- and a fourth one cannot be drawn, which is an error
    worth returning as a message rather than a traceback.
    """
    rows = [6, 7, 8]                                    # the sheet holds three buckets
    template = [str(ws.cell(row=row, column=7).value or "").strip() for row in rows]

    def bucket_for(position: dict) -> str:
        """The sheet's own label for this position's category."""
        category = (position.get("category") or "").strip()
        for label in template:
            if label and label.casefold() == category.casefold():
                return label                           # the sheet's spelling wins
        return category or "Unclassified"

    present: list[str] = []
    for position in positions:
        label = bucket_for(position)
        if label not in present:
            present.append(label)
    ordered = [label for label in template if label in present]
    ordered += [label for label in present if label not in ordered]
    if len(ordered) > len(rows):
        raise ValueError(
            f"pre session has room for three category buckets, this book uses "
            f"{len(ordered)}: " + ", ".join(ordered)
            + ". Merge two of them in the book, or give the tab another bucket.")

    for offset, row in enumerate(rows):
        category = ordered[offset] if offset < len(ordered) else None
        ws.cell(row=row, column=7).value = category
        for col, bias in ((8, "Long"), (9, "Short")):
            cells = [block["total_row"] for block in blocks
                     if category is not None
                     and bucket_for(block["position"]) == category
                     and block["position"]["bias"] == bias]
            ws.cell(row=row, column=col).value = "=+" + "+".join(f"Q{row_}" for row_ in cells) if cells else 0

    filled = rows[:len(ordered)]
    for col in (8, 9):
        letter = _letter(col)
        ws.cell(row=10, column=col).value = "=+" + "+".join(f"{letter}{row}" for row in filled) if filled else "=0"


# --------------------------------------------------------------- watchlist --
def _style_of(cell) -> object:
    """A copy of a cell's style -- enough to stamp it onto another cell."""
    return copy.copy(cell._style)


def _stamp(cell, style) -> None:
    cell._style = copy.copy(style)


def _majority(cells):
    """The style most of these cells share (the sheet's own look for that role)."""
    buckets: dict[tuple, list] = {}
    for cell in cells:
        buckets.setdefault(tuple(cell._style), []).append(cell)
    if not buckets:
        return None
    return _style_of(max(buckets.values(), key=len)[0])


def _watchlist_styles(ws: Worksheet, body_rows: list[int], header_rows: list[int]) -> dict:
    """The looks the tab uses: row filler, sector band, LONG, SHORT, comment.

    Each is the style the sheet itself draws most often for that role, so the
    export never invents a style and never inherits a blank row's default --
    that is what used to leave `DLTR` plain black Calibri while `HPE` beside it
    was bold dark red.
    """
    def cell(row: int, column: int):
        return ws.cell(row=row, column=column)

    fallback = _style_of(ws.cell(row=1, column=1))
    filler = {}
    for col in WL_TABLE_COLS:
        style = _majority([cell(row, col) for row in body_rows])
        filler[col] = style if style is not None else fallback
    band = _majority([cell(row, col) for row in header_rows for col in (8, 9)
                      if _rgb(cell(row, col)) == WL_BAND_FILL]) or fallback

    def written(column: int):
        cells = [cell(row, column) for row in body_rows
                 if isinstance(cell(row, column).value, str) and cell(row, column).value.strip()]
        return _majority(cells) or filler[column]

    return {"filler": filler, "band": band,
            "long": written(8), "short": written(9),
            "note_left": written(7), "note_right": written(10)}


def _watchlist_layout(ws: Worksheet) -> dict:
    """Read the sheet's sector blocks and the styles that define their look.

    The block order, each block's row allocation and every style reused below
    come out of the template itself, so a differently laid out sheet still
    works: its blocks are the ones drawn, in the order it draws them.
    """
    headers: list[int] = []
    last_content = 0
    for row in range(1, min(ws.max_row, WL_SCAN_ROWS) + 1):
        if any(_rgb(ws.cell(row=row, column=col)) == WL_BAND_FILL for col in (8, 9)):
            headers.append(row)
        if any(not _blank(ws.cell(row=row, column=col).value) for col in (7, 8, 9, 10)):
            last_content = row

    blocks = []
    for index, row in enumerate(headers):
        end = (headers[index + 1] - 1 if index + 1 < len(headers)
               else max(last_content, row + 1))
        name = next((ws.cell(row=row, column=col).value for col in (8, 9)
                     if not _blank(ws.cell(row=row, column=col).value)), "") or ""
        blocks.append({"name": str(name).strip(), "key": _key(str(name)),
                       "header_row": row, "rows": list(range(row + 1, end + 1))})
    return {"blocks": blocks,
            "first_row": blocks[0]["header_row"] if blocks else 7,
            "last_content": last_content,
            "styles": _watchlist_styles(ws, [r for block in blocks for r in block["rows"]],
                                        headers)}


def _watchlist_plan(layout: dict, items: list[dict]) -> list[dict]:
    """Where every block and every candidate lands, once, before anything moves.

    A block keeps the rows the sheet gave it and only grows when the book has
    more candidates than fit; growing shifts the blocks below it down, so the
    shift is carried forward here instead of being rediscovered mid-write.
    """
    pools: dict[str, dict] = {}
    for item in items:
        key = _key(item["sector"])
        bucket = pools.setdefault(key, {"name": item["sector"], "LONG": [], "SHORT": []})
        bucket["SHORT" if str(item["side"]).upper().startswith("S") else "LONG"].append(item)

    plan: list[dict] = []
    shift = 0
    for block in layout["blocks"]:
        bucket = pools.pop(block["key"], None)
        wanted = max(len(bucket["LONG"]), len(bucket["SHORT"])) if bucket else 0
        grow = max(0, wanted - len(block["rows"]))
        header = block["header_row"] + shift
        plan.append({"name": block["name"], "bucket": bucket, "header_row": header,
                     "rows": [header + 1 + step for step in range(len(block["rows"]) + grow)],
                     "grow": grow, "template_rows": list(block["rows"]), "appended": False})
        shift += grow

    # sectors the sheet never drew: appended at the bottom, in first-use order
    bottom = plan[-1]["rows"][-1] if plan and plan[-1]["rows"] else layout["first_row"]
    for bucket in pools.values():
        header = bottom + 2
        rows = [header + 1 + step
                for step in range(max(len(bucket["LONG"]), len(bucket["SHORT"]), 1))]
        plan.append({"name": bucket["name"], "bucket": bucket, "header_row": header, "rows": rows,
                     "grow": 0, "template_rows": [], "appended": True})
        bottom = rows[-1]
    return plan


def _grow_watchlist(ws: Worksheet, plan: list[dict], styles: dict) -> None:
    """Insert the rows a grown block needs, bottom block first.

    openpyxl moves the rows below an insert with their styles intact but
    leaves the inserted rows bare, so each new row is stamped with the sheet's
    own body-row look -- otherwise a grown block exports as unformatted rows
    with no background and no table border.
    """
    for block in sorted(plan, key=lambda b: b["header_row"], reverse=True):
        if block["appended"] or block["grow"] <= 0:
            continue
        at = block["template_rows"][-1] + 1
        ws.insert_rows(at, block["grow"])
        for offset in range(block["grow"]):
            for col in WL_TABLE_COLS:
                _stamp(ws.cell(row=at + offset, column=col), styles["filler"][col])


def _write_watchlist(ws: Worksheet, view: dict) -> None:
    items = view["watchlist"]
    layout = _watchlist_layout(ws)
    styles = layout["styles"]
    plan = _watchlist_plan(layout, items)
    _grow_watchlist(ws, plan, styles)

    # wipe the table so a candidate the book has dropped does not survive as a
    # stale ticker under its old block; the cells go back to the sheet's own
    # empty-row look, so a ticker colour never lingers on a row with no ticker
    last = max([block["rows"][-1] for block in plan if block["rows"]] + [layout["last_content"]])
    for row in range(layout["first_row"], last + 1):
        for col in range(7, 12):
            ws.cell(row=row, column=col).value = None
        for col in range(7, 11):
            _stamp(ws.cell(row=row, column=col), styles["filler"][col])

    for block in plan:
        if block["appended"]:                       # rows the sheet never drew
            for row in [block["header_row"], *block["rows"]]:
                for col in WL_TABLE_COLS:
                    _stamp(ws.cell(row=row, column=col), styles["filler"][col])
        for col in range(7, 12):                    # the block's own row look
            _stamp(ws.cell(row=block["header_row"], column=col), styles["filler"][col])
        for col in (8, 9):                          # the sector band spans both cells
            cell = ws.cell(row=block["header_row"], column=col)
            _stamp(cell, styles["band"])
            cell.value = block["name"]

        taken = {8: set(), 9: set()}
        for column, side, note_col in ((8, "LONG", 7), (9, "SHORT", 10)):
            ticker_style = styles["long"] if side == "LONG" else styles["short"]
            note_style = styles["note_left"] if side == "LONG" else styles["note_right"]
            for item in (block["bucket"][side] if block["bucket"] else []):
                target = None
                offset = item.get("row_offset")
                if isinstance(offset, int) and 1 <= offset <= len(block["rows"]):
                    if block["rows"][offset - 1] not in taken[column]:
                        target = block["rows"][offset - 1]
                if target is None:                 # no pinned row, or it is taken
                    target = next((row for row in block["rows"] if row not in taken[column]), None)
                if target is None:
                    continue
                taken[column].add(target)
                cell = ws.cell(row=target, column=column)
                _stamp(cell, ticker_style)
                cell.value = item["ticker"]
                note = (item.get("sub_sector") or item.get("comment") or "").strip()
                if note:
                    note_cell = ws.cell(row=target, column=note_col)
                    _stamp(note_cell, note_style)
                    note_cell.value = note

    as_of = view["summary"]["as_of"]
    ws.cell(row=4, column=7).value = (
        f"As of {as_of} · {len(items)} candidates · {len(view['positions'])} live positions")


def _key(name: str) -> str:
    return " ".join((name or "").lower().replace("/", " ").split())


def _letter(col: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(col)


# -------------------------------------------------------------------- entry --
def build_workbook(state: dict, as_of: date | None = None) -> Workbook:
    """Fill the original workbook with this book's numbers."""
    if not TEMPLATE.exists():
        raise FileNotFoundError(f"missing export template: {TEMPLATE}")

    view = calc.build_view(state, as_of=as_of)
    wb = load_workbook(TEMPLATE)
    _write_realised(wb["Realised"], view)
    _write_presession(wb["pre session"], view)
    _write_watchlist(wb["Actionable Watchlist"], view)
    wb.calculation.fullCalcOnLoad = True        # formulas have no cached values
    return wb
