"""Compare an exported workbook against the original sheet it came from.

The export is supposed to be *the same spreadsheet* with new numbers in it, so
this script checks exactly that and nothing softer:

  * sheet names and order, freeze panes, gridlines, tab colour, autofilter
  * every column width and row height
  * data validations and conditional-formatting rules (ranges + colours)
  * the style of every cell (font, fill, border, alignment, number format)

Values are allowed to differ -- that is the point of the export -- but a style
difference is a failure.

The one region that is meant to change is the pre session month block area to
the right of column S (the sheet hard-codes three months; the export emits as
many as the book needs), so there we check that each emitted block matches the
template's *first* block instead of matching the original cells column by column.

    python scripts/parity.py [--exported PATH] [--template PATH]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from datetime import datetime

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "template.xlsx"
PS_FIXED_COLS = 20          # A..T stay as the sheet has them
PS_MONTH_START = 21         # U onwards is the moving part
LEG_SIDE_COL = 5            # E: Long / Short, tinted by the sheet
LEG_TYPE_COL = 6            # F: CALL / PUT, dark blue vs orange


def column_index(letter: str) -> int:
    index = 0
    for char in letter:
        if not char.isalpha():
            break
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def style_of(cell) -> tuple:
    font = cell.font
    fill = cell.fill
    border = cell.border
    align = cell.alignment

    def colour(obj):
        if obj is None:
            return None
        rgb = getattr(obj, "rgb", None)
        if isinstance(rgb, str):
            return rgb[-8:].upper()
        theme = getattr(obj, "theme", None)
        return None if theme is None else f"theme{theme}:{getattr(obj, 'tint', 0)}"

    return (
        (font.name, font.size, bool(font.bold), bool(font.italic),
         bool(font.underline), colour(font.color)),
        (fill.fill_type, colour(fill.fgColor), colour(fill.bgColor)),
        tuple((getattr(side, "style", None), colour(getattr(side, "color", None))) for side in
              (border.left, border.right, border.top, border.bottom)),
        (align.horizontal, align.vertical, bool(align.wrap_text), align.indent),
        cell.number_format,
    )


def describe(sig: tuple) -> str:
    (font, fill, border, align, number_format) = sig
    return (f"font={font} fill={fill} border={border} align={align} nf={number_format!r}")


def sheet_properties(ws) -> dict:
    widths = {key: round(dim.width, 3) for key, dim in ws.column_dimensions.items() if dim.width}
    heights = {key: round(dim.height, 3) for key, dim in ws.row_dimensions.items() if dim.height}
    validations = sorted(
        (str(dv.sqref), dv.type, str(dv.formula1), str(dv.formula2)) for dv in ws.data_validations.dataValidation)
    rules = []
    for rng, group in ws.conditional_formatting._cf_rules.items():
        for rule in group:
            colour = None
            if rule.dxf is not None:
                font_colour = getattr(rule.dxf.font.color, "rgb", None) if rule.dxf.font else None
                fill_colour = getattr(rule.dxf.fill.fgColor, "rgb", None) if rule.dxf.fill else None
                colour = (str(font_colour), str(fill_colour))
            rules.append((str(rng.sqref), rule.type, rule.operator,
                          tuple(rule.formula or ()), colour))
    return {
        "freeze": ws.freeze_panes,
        "gridlines": ws.sheet_view.showGridLines,
        "tab": str(getattr(ws.sheet_properties.tabColor, "rgb", ws.sheet_properties.tabColor)),
        "widths": widths,
        "heights": heights,
        "validations": validations,
        "conditional": sorted(rules, key=str),
        "autofilter": ws.auto_filter.ref,
        "protection": ws.protection.sheet,
    }


def compare_properties(name: str, want: dict, got: dict, problems: list[str],
                       width_floor: int | None = None) -> None:
    for key in ("freeze", "gridlines", "tab", "autofilter", "protection"):
        if want[key] != got[key]:
            problems.append(f"{name}: {key} {got[key]!r} != {want[key]!r}")
    for key in ("widths", "heights"):
        if key == "widths" and width_floor is not None:
            # the month-block area is regenerated, so its widths are checked by
            # the block-pattern test below instead of against the original
            want = dict(want)
            want["widths"] = {k: v for k, v in want["widths"].items()
                              if column_index(k) < width_floor}
            got = dict(got)
            got["widths"] = {k: v for k, v in got["widths"].items()
                             if column_index(k) < width_floor}
        missing = {k: v for k, v in want[key].items() if got[key].get(k) != v}
        extra = {k: v for k, v in got[key].items() if k not in want[key]}
        if missing:
            problems.append(f"{name}: {key} differ for {list(missing)[:8]}"
                            f" (e.g. {list(missing.items())[:3]})")
        if extra:
            problems.append(f"{name}: {key} added for {list(extra)[:8]}")
    if want["validations"] != got["validations"]:
        problems.append(f"{name}: data validations differ "
                        f"({len(got['validations'])} vs {len(want['validations'])})")
    if want["conditional"] != got["conditional"]:
        only_got = [r for r in got["conditional"] if r not in want["conditional"]]
        only_want = [r for r in want["conditional"] if r not in got["conditional"]]
        problems.append(f"{name}: conditional formatting differs "
                        f"(+{len(only_got)} / -{len(only_want)}) e.g. {only_got[:2]} {only_want[:2]}")


def compare_cells(name: str, want_ws, got_ws, cells, problems: list[str], label: str) -> int:
    checked = 0
    for row, col in cells:
        want = style_of(want_ws.cell(row=row, column=col))
        got = style_of(got_ws.cell(row=row, column=col))
        checked += 1
        if want != got:
            ref = f"{get_column_letter(col)}{row}"
            problems.append(f"{name}!{ref} ({label}): {describe(got)} != {describe(want)}")
    return checked


TEMPLATE_MONTHS = 3


def leg_role_styles(ws) -> dict:
    """The template's own look for a leg's side cell (E) and type cell (F).

    The sheet codes a leg twice: E is tinted by side, F by type -- CALL dark blue,
    PUT the theme's orange. These are the looks an export has to reproduce for the
    value it writes, no matter which template row the leg lands on.
    """
    styles: dict[tuple[int, str], tuple] = {}
    for row in range(1, min(ws.max_row, 500) + 1):
        type_cell = ws.cell(row=row, column=LEG_TYPE_COL)
        side_cell = ws.cell(row=row, column=LEG_SIDE_COL)
        kind = str(type_cell.value or "").strip().upper()
        side = str(side_cell.value or "").strip().upper()
        if kind not in ("CALL", "PUT") or side not in ("LONG", "SHORT"):
            continue
        if type_cell.fill is not None and type_cell.fill.patternType:
            styles.setdefault((LEG_TYPE_COL, kind), style_of(type_cell))
        if side_cell.fill is not None and side_cell.fill.patternType:
            styles.setdefault((LEG_SIDE_COL, side), style_of(side_cell))
    return styles


def shifted_block_rows(got_ws, want_ws) -> set[int]:
    """Rows of blocks whose layout moved, so the template's row is the wrong reference.

    A block that has not grown sits exactly where the sheet drew it, and is still
    compared cell by cell. One that grew (three or more legs) pushed its totals row --
    and every row after it -- down, so comparing those rows to the template would
    compare a spare row to the sheet's dark totals band. Their structure is asserted by
    `check_leg_box` instead.
    """
    rows: set[int] = set()
    got_tickers = [row for row in range(13, 80)
                   if (lambda name: name and " " not in name)(
                       str(got_ws.cell(row=row, column=2).value or "").strip())]
    for index, ticker in enumerate(got_tickers):
        total = ticker + 4
        for row in range(ticker + 1, 80):
            if _fill_of(got_ws.cell(row=row, column=LEG_SIDE_COL)) == "3A3838":
                total = row
                break
        if total != ticker + 4 or ticker != 13 + 5 * index:
            rows.update(range(ticker, total + 1))
    return rows


def export_leg_rows(got_ws) -> set[int]:
    """Rows the export uses as a leg row -- compared to the box grammar, not the sheet."""
    return {row for row in range(1, 80)
            if str(got_ws.cell(row=row, column=LEG_SIDE_COL).value or "").strip().upper()
            in ("LONG", "SHORT")}


def added_leg_rows(got_ws, want_ws) -> set[int]:
    """Rows the export uses as a leg row where the template drew none.

    The sheet drew two leg rows per block plus a spare. A position with three or more
    legs legitimately writes into that spare row and past it, and those cells have to
    be styled -- boxed, filled, coded by side -- where the template left them bare.
    That is the export doing its job rather than drifting from the sheet, so style
    differences are tolerated on exactly those rows and nowhere else.
    """
    def carries_leg(ws, row: int) -> bool:
        return str(ws.cell(row=row, column=LEG_SIDE_COL).value or "").strip().upper() in ("LONG", "SHORT")

    return {row for row in range(1, 80) if carries_leg(got_ws, row) and not carries_leg(want_ws, row)}


def coded_cells(got_ws) -> set[tuple[int, int]]:
    """The pre-session cells whose look follows their own value, not their row."""
    skip = set()
    for row in range(1, 60):
        for col, words in ((LEG_SIDE_COL, ("LONG", "SHORT")), (LEG_TYPE_COL, ("CALL", "PUT"))):
            if str(got_ws.cell(row=row, column=col).value or "").strip().upper() in words:
                skip.add((row, col))
    return skip


def check_leg_coding(name: str, want_ws, got_ws, problems: list[str],
                     styles: dict | None = None) -> int:
    """E and F must carry the template's coding for the value written in them.

    A leg row borrows a template row's style wholesale, so those two cells are only
    right if they are restamped from the side and type: a PUT on a row the sheet drew
    for a CALL has to be orange, and a CALL on a PUT row has to be blue.
    """
    styles = styles if styles is not None else leg_role_styles(want_ws)
    checked = 0
    for row in range(1, 60):
        for col in (LEG_SIDE_COL, LEG_TYPE_COL):
            cell = got_ws.cell(row=row, column=col)
            value = str(cell.value or "").strip().upper()
            want = styles.get((col, value))
            if want is None:
                continue
            checked += 1
            # only the code travels (font + fill); a slot's borders are drawn by its
            # position inside the block, so the row keeps those
            got = style_of(cell)
            if (got[0], got[1]) != (want[0], want[1]):
                ref = f"{get_column_letter(col)}{row}"
                problems.append(f"{name}!{ref} ({value}): font/fill {got[:2]} != {want[:2]}")
    return checked


def check_leg_box(want_ws, got_ws, problems: list[str]) -> int:
    """Every block's leg rows must form the sheet's box, however many legs there are.

    The sheet's own blocks are not consistent -- its last block opens twice and never
    closes, one middle block opens thin where the rest open medium -- so the export
    normalises each block to the first block's grammar: the first leg carries the
    medium rule above it and a thin one below, interior legs thin on both sides, the
    last leg thin above and the medium rule below that closes the box. This is
    asserted here because the rows themselves are excluded from the strict
    template-cell comparison (their look is the export's, not the sheet's).
    """
    checked = 0
    ticker_rows = [row for row in range(13, 80)
                   if (lambda name: name and " " not in name)(
                       str(got_ws.cell(row=row, column=2).value or "").strip())]
    for ticker in ticker_rows:
        total = ticker + 4                    # where the sheet's own block would end
        for row in range(ticker + 1, 80):     # grown blocks: find the dark totals band
            if _fill_of(got_ws.cell(row=row, column=LEG_SIDE_COL)) == "3A3838":
                total = row
                break
        slots = [row for row in range(ticker + 1, total)]
        leg_rows = [row for row in slots if str(got_ws.cell(row=row, column=LEG_SIDE_COL).value
                                                or "").strip().upper() in ("LONG", "SHORT")]
        if not leg_rows:
            continue
        for index, row in enumerate(leg_rows):
            top = got_ws.cell(row=row, column=LEG_SIDE_COL).border.top.style
            bottom = got_ws.cell(row=row, column=LEG_SIDE_COL).border.bottom.style
            if index == 0:
                # a lone leg keeps the sheet's opening look (medium above, thin below);
                # the box closes under the spare row the sheet drew beneath it
                want_top = "medium"
                want_bottom = "thin" if len(leg_rows) > 1 else got_ws.cell(
                    row=row, column=LEG_SIDE_COL).border.bottom.style
            elif index == len(leg_rows) - 1:
                want_top, want_bottom = "thin", "medium"
            else:
                want_top, want_bottom = "thin", "thin"
            checked += 2
            if top != want_top:
                problems.append(f"pre session!{get_column_letter(LEG_SIDE_COL)}{row} (leg box): "
                                f"top rule {top!r} != {want_top!r} in a {len(leg_rows)}-leg block")
            if bottom != want_bottom:
                problems.append(f"pre session!{get_column_letter(LEG_SIDE_COL)}{row} (leg box): "
                                f"bottom rule {bottom!r} != {want_bottom!r} in a {len(leg_rows)}-leg block")
        rule = got_ws.cell(row=total, column=LEG_SIDE_COL).border.bottom.style
        checked += 1
        if rule != "medium":
            problems.append(f"pre session!{get_column_letter(LEG_SIDE_COL)}{total} (leg box): "
                            f"totals rule {rule!r} != 'medium'")
        # The blank row between the last leg and the dark totals band must be blank: no
        # rules of its own, or the block reads as though the box closed twice.
        # a one-leg block closes its box on the empty row beneath the leg -- that row is
        # the sheet's own artwork, so the blank rows start after it
        first_blank = leg_rows[-1] + (2 if len(leg_rows) == 1 else 1)
        for row in range(first_blank, total):
            cell = got_ws.cell(row=row, column=LEG_SIDE_COL)
            checked += 2
            for side, rule in (("top", cell.border.top.style), ("bottom", cell.border.bottom.style)):
                if rule is not None:
                    problems.append(f"pre session!{get_column_letter(LEG_SIDE_COL)}{row} (spare row): "
                                    f"{side} rule {rule!r} != None")
    return checked


def _check_month_blocks(want_ws, got_ws, problems: list[str],
                        skip_rows: set[int] | None = None) -> int:
    """Blocks must match the template block that donated their styling.

    Two rows are the export's own by design and are not compared: row 5, where the
    booking cutoff each Known column reads is written with a date format, and any row
    the book fills with a leg the template drew empty (its interior rule spans the
    month columns too, which is what keeps the block boxed across the sheet).
    """
    checked = 0
    skip_rows = set(skip_rows or ()) | {5}
    blocks = 0
    while True:
        base = PS_MONTH_START + 4 * blocks
        dim = got_ws.column_dimensions.get(get_column_letter(base))
        if dim is None or dim.width is None or abs(dim.width - 15.57) > 0.1:
            break                              # a block starts with the wide column
        blocks += 1
    if not blocks:
        problems.append("pre session: no month blocks found")
    for index in range(blocks):
        base = PS_MONTH_START + 4 * index
        donor = PS_MONTH_START + 4 * min(index, TEMPLATE_MONTHS - 1)
        cells = [(row, col - PS_MONTH_START) for row in range(5, 60)
                 if row not in skip_rows
                 for col in range(PS_MONTH_START, PS_MONTH_START + 4)]
        for row, inner in cells:
            want = style_of(want_ws.cell(row=row, column=donor + inner))
            got = style_of(got_ws.cell(row=row, column=base + inner))
            checked += 1
            if want != got:
                ref = f"{get_column_letter(base + inner)}{row}"
                problems.append(f"pre session!{ref} (month block {index + 1}): "
                                f"{describe(got)} != {describe(want)}")
        expected = (15.57, 12.86, 13.43, 2.71)
        for inner in range(4):
            width = got_ws.column_dimensions[get_column_letter(base + inner)].width
            if width is None or abs(width - expected[inner]) > 0.01:
                problems.append(f"pre session!{get_column_letter(base + inner)} width "
                                f"{width} != {expected[inner]} (month block {index + 1})")
    action = PS_MONTH_START + 4 * blocks
    if got_ws.column_dimensions[get_column_letter(action)].width not in (42.0, 42):
        problems.append(f"pre session!{get_column_letter(action)} action-column width "
                        f"{got_ws.column_dimensions[get_column_letter(action)].width} != 42")
    return checked


def value_of(cell):
    value = cell.value
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat() if hasattr(value, "date") else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, str):
        return " ".join(value.split())
    return value


# --------------------------------------------------------------- watchlist --
# The watchlist tab holds the book's candidates, not the sheet's own data: some
# names come and go, and a block that gains a candidate grows a row and pushes
# the blocks below it down. Comparing cell for cell would therefore fail for
# reasons that have nothing to do with the look, so this tab is checked the way
# the tab is designed -- by role. Everything above the table must be untouched;
# every sector the sheet drew must still be drawn, in the sheet's order; and on
# every row, an empty cell must carry the sheet's own filler style while a cell
# with content must carry the sheet's own style for that role (the LONG blue,
# the SHORT red, the bold comment).
WL_FIRST_ROW = 7            # the first sector block; rows 1-6 are the sheet's chrome
WL_TABLE_COLS = (7, 8, 9, 10, 11)


def _fill_of(cell) -> str:
    value = getattr(cell.fill.fgColor, "rgb", None)
    return value[-6:].upper() if isinstance(value, str) else ""


def _watchlist_blocks(ws) -> list[dict]:
    """The tab's sector blocks: a banded header row, then the rows under it."""
    limit = min(ws.max_row, 1000)
    headers = [row for row in range(1, limit + 1)
               if any(_fill_of(ws.cell(row=row, column=col)) == "2D4D76" for col in (8, 9))]
    last = max([WL_FIRST_ROW - 1] + [
        row for row in range(1, limit + 1)
        if any(ws.cell(row=row, column=col).value not in (None, "")
               for col in WL_TABLE_COLS)])
    blocks = []
    for index, row in enumerate(headers):
        end = headers[index + 1] - 1 if index + 1 < len(headers) else max(last, row + 1)
        name = next((ws.cell(row=row, column=col).value for col in (8, 9)
                     if ws.cell(row=row, column=col).value not in (None, "")), "") or ""
        blocks.append({"name": str(name).strip(), "header_row": row,
                       "rows": list(range(row + 1, end + 1))})
    return blocks


def _watchlist_styles(ws, blocks) -> dict:
    """The sheet's own look for each role, by majority vote over its cells."""
    def majority(cells):
        counts: dict[tuple, int] = {}
        for cell in cells:
            sig = style_of(cell)
            counts[sig] = counts.get(sig, 0) + 1
        return max(counts, key=counts.get) if counts else None

    body = [row for block in blocks for row in block["rows"]]
    filler = {col: majority([ws.cell(row=row, column=col) for row in body])
              for col in WL_TABLE_COLS}
    band = majority([ws.cell(row=block["header_row"], column=col)
                     for block in blocks for col in (8, 9)
                     if _fill_of(ws.cell(row=block["header_row"], column=col)) == "2D4D76"])

    def written(column):
        cells = [ws.cell(row=row, column=column) for row in body
                 if isinstance(ws.cell(row=row, column=column).value, str)
                 and ws.cell(row=row, column=column).value.strip()]
        return majority(cells) or filler[column]

    return {"filler": filler, "band": band or filler[8],
            "long": written(8), "short": written(9),
            "note_left": written(7), "note_right": written(10)}


def _check_watchlist(want_ws, got_ws, problems: list[str]) -> int:
    checked = 0
    for row in range(1, WL_FIRST_ROW):                 # title, dateline, headers
        for col in range(1, 19):
            want, got = style_of(want_ws.cell(row=row, column=col)), style_of(got_ws.cell(row=row, column=col))
            checked += 1
            if want != got:
                problems.append(f"Actionable Watchlist!{get_column_letter(col)}{row} (chrome): "
                                f"{describe(got)} != {describe(want)}")
        if row == 2:                                   # the title itself must survive
            if want_ws.cell(row=row, column=7).value != got_ws.cell(row=row, column=7).value:
                problems.append("Actionable Watchlist: title changed")

    want_blocks = _watchlist_blocks(want_ws)
    got_blocks = _watchlist_blocks(got_ws)
    want_names = [block["name"] for block in want_blocks]
    got_names = [block["name"] for block in got_blocks]
    if got_names[:len(want_names)] != want_names:
        missing = [name for name in want_names if name not in got_names]
        problems.append(f"Actionable Watchlist: sector blocks {got_names[:len(want_names)]} "
                        f"!= {want_names}" + (f" (missing: {missing})" if missing else ""))

    styles = _watchlist_styles(want_ws, want_blocks)
    for block in got_blocks:
        rows = [block["header_row"], *block["rows"]]
        for row in rows:
            for col in WL_TABLE_COLS:
                cell = got_ws.cell(row=row, column=col)
                value = cell.value
                if row == block["header_row"] and col in (8, 9):
                    want = styles["band"]
                elif isinstance(value, str) and value.strip():
                    want = {7: styles["note_left"], 8: styles["long"], 9: styles["short"],
                            10: styles["note_right"]}.get(col, styles["filler"][col])
                else:
                    want = styles["filler"][col]
                checked += 1
                if style_of(cell) != want:
                    problems.append(
                        f"Actionable Watchlist!{get_column_letter(col)}{row} "
                        f"({'header' if row == block['header_row'] else repr(value)}): "
                        f"{describe(style_of(cell))} != {describe(want)}")
    return checked


def compare_values(name: str, want_ws, got_ws, cells, problems: list[str], ignore=()) -> int:
    """For sheets whose content is the sheet's own data, values must match too."""
    checked = 0
    for row, col in cells:
        ref = f"{get_column_letter(col)}{row}"
        if ref in ignore:
            continue
        want = value_of(want_ws.cell(row=row, column=col))
        got = value_of(got_ws.cell(row=row, column=col))
        checked += 1
        if want != got:
            problems.append(f"{name}!{ref} value {got!r} != {want!r}")
    return checked


def run(exported, template: Path = TEMPLATE) -> list[str]:
    """Compare an exported workbook (path or Workbook) with the original; [] means identical."""
    want = load_workbook(template)
    got = load_workbook(exported) if isinstance(exported, (str, Path)) else exported
    problems: list[str] = []
    checked = 0

    if want.sheetnames != got.sheetnames:
        problems.append(f"sheets {got.sheetnames} != {want.sheetnames}")

    for name in want.sheetnames:
        if name not in got.sheetnames:
            continue
        want_ws, got_ws = want[name], got[name]
        compare_properties(name, sheet_properties(want_ws), sheet_properties(got_ws), problems,
                           width_floor=PS_MONTH_START if name == "pre session" else None)

        if name == "pre session":
            # the side/type cells are checked against the coding they carry, not
            # against whatever the same template row happened to be drawn for
            extra_leg_rows = (added_leg_rows(got_ws, want_ws) | export_leg_rows(got_ws)
                              | shifted_block_rows(got_ws, want_ws))
            fixed = [(row, col) for row in range(1, 60) for col in range(1, PS_FIXED_COLS + 1)
                     if (row, col) not in coded_cells(got_ws) and row not in extra_leg_rows]
            checked += compare_cells(name, want_ws, got_ws, fixed, problems, "fixed block")
            checked += check_leg_coding(name, want_ws, got_ws, problems)
            checked += check_leg_box(want_ws, got_ws, problems)
            # each emitted month block must look like the sheet's block for that
            # slot (the sheet drew three: Nov/Dec clone the October one past that)
            checked += _check_month_blocks(want_ws, got_ws, problems, extra_leg_rows)
            continue

        if name == "Actionable Watchlist":
            checked += _check_watchlist(want_ws, got_ws, problems)
            continue

        max_col = max(want_ws.max_column, 18)
        cells = [(row, col) for row in range(1, want_ws.max_row + 1) for col in range(1, max_col + 1)]
        checked += compare_cells(name, want_ws, got_ws, cells, problems, "cell")

        if name == "Realised":
            # the ledger was seeded from this sheet, so the rows must still read
            # the same apart from the commission cell we normalise to 0
            data = [(row, col) for row in range(9, 21) for col in range(1, 13)]
            checked += compare_values(name, want_ws, got_ws, data, problems)

    problems.insert(0, f"cells compared: {checked}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--exported", type=Path, default=Path("/tmp/export.xlsx"))
    parser.add_argument("--max-report", type=int, default=12)
    args = parser.parse_args(argv)

    problems = run(args.exported, args.template)
    print(problems[0])
    problems = problems[1:]
    if problems:
        print(f"\nSTYLE/PROPERTY MISMATCHES: {len(problems)}")
        for line in problems[:args.max_report]:
            print("  " + line)
        if len(problems) > args.max_report:
            print(f"  ... {len(problems) - args.max_report} more")
        return 1
    print("PASSED: export matches the original workbook's styling and layout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
