# 4Pillar options book — web UI

A small local web app that replaces the three working tabs of
`../example_sheet.xlsx` — **Realised**, **pre session** and **Actionable Watchlist** — with a
persistent book, live option/underlying quotes, automatic month booking, and a three-tab
spreadsheet export.

- Backend: Python + FastAPI (single process, no database server)
- Storage: one JSON file, `data/portfolio.json` (plus timestamped backups)
- Frontend: static HTML/CSS/vanilla JS, no build step, no framework
- Market data: yfinance (free, ~15 min delayed)
- Export: `openpyxl`, three tabs written into a copy of `example_sheet.xlsx` itself

Everything the DeepSeek harness needs is either that JSON file or the REST API.

---

## Quick start

```bash
cd realised_presession_watchlist/portfolio-ui
./run.sh                      # http://127.0.0.1:8117
```

`run.sh` uses the workspace virtualenv at `../../.venv/bin/python` (it already has
`fastapi`, `uvicorn`, `yfinance`, `openpyxl`). Set `PORT=...` to move it.

In the browser:

| Control | What it does |
| --- | --- |
| **As of** date + **Today** | Recomputes month columns, booking months and valuations against any date. This is how you "set the dates". |
| **Pull live data** | One yfinance round-trip: underlying last, per-leg bid/ask/mid, next earnings date. |
| **Export .xlsx** | Downloads `options-book-<as-of>.xlsx`: your sheet's three tabs, same styling, current numbers. |
| **Watchlist cells** | Type a ticker in a cell and hit `Enter` to add it to that sector / side; `×` on a chip removes it (with Undo). `Notes: on/off` shows each candidate's sub-sector or comment. |
| **delete** on a position card (right-hand side) | Removes the position and all its legs, after a confirm. The red **del** in a leg row removes that one leg; the same buttons exist on closed trades in the Realised tab. |
| ⚙ | Opening balance, default multiplier, booking rule (`third_friday` / `calendar`), default as-of date. |

---

## How the book works

1. **Pre session** holds open positions. A position is a ticker with a category
   (`Hardware`, `Software`, `Real economy`, …), a bucket bias (`Long`/`Short`, which drives the
   Long/Short gross-market-value matrix in the header), and any number of legs — long calls, short
   puts, calendars, whatever the trade is.
2. Each leg carries side, type, strike, expiry, contracts, multiplier (default 100) and the premium
   **as traded** (always a positive number; a short leg books it as a credit). Bid/ask come from
   live quotes, with a manual `Mid override` available when a chain is untradeable.
3. **Bias is directional, not the leg's side.** A long put is a *bearish* position, so HPE
   (long 50 puts) is `Software / Short` even though its leg is `Long`. Each position stores
   `bias: "auto"` by default and the stance is derived from the legs — a long call or short put is
   `Long`, a long put or short call is `Short`, weighted by contracts × multiplier × premium so a
   debit spread resolves to the side that was paid for. Set `Long` / `Short` by hand in the
   position modal to pin it; a position whose label contradicts its legs shows a
   *"labelled X · legs say Y — fix"* chip that switches it back to auto.
4. **Close** a leg and it becomes a **Realised** trade (price in = the leg's premium, price out =
   what you exited at, dates, commission). **Close part of it and only that slice books.** The
   dialog's *Contracts to close* field opens at the whole leg; type a smaller number and those
   contracts are realised while the leg keeps the rest with its original premium in — three of six,
   then two more, then the last one, is three Realised trades and one leg that shrinks from 6 to 0.
   The basis follows the contract count exactly, because the sheet values a leg as
   `premium × contracts × multiplier`: half the contracts is half the position, not a new one. A
   partly-closed leg reads `3 /6` in the `Cont` column with its opening size on hover, and the
   dialog prints what this exit books, the basis it carries out and what is left open, before you
   commit. Realised P/L therefore always lands in the tab that shows it and flows straight back into
   the pre-session header.
5. **Realised** is the closed-trade ledger with the same KPI block as the sheet: win %, win/loss $,
   R multiple, Kelly %, closed P/L, average days — and it counts **one closed leg as one trade**, the
   same unit as a row on the sheet's Realised tab. A calendar that wins on one leg and loses on the
   other is one win and one loss, and the gross wins/losses are the sums of the leg rows (not the
   netted position totals); win %, R and Kelly follow from those units, so the dashboard and the
   workbook agree cent for cent. The ledger is displayed grouped **by trade**: one row per trade with
   a `Structure` chip and its legs folded underneath (toggle with *Legs: shown/hidden*) — a calendar
   is one decision to edit, but two ledger rows to the statistics. `+ Trade` creates a single option
   *or* a calendar in one
   form (leg 2 is optional); `+ leg` appends another leg to an existing trade and the structure
   re-derives itself. If you closed a calendar's legs one at a time (two separate singles), or one
   leg in slices (three singles of the same contract), tick
   them and hit **Merge selected** — the trades fold into one with the combined legs, the outer
   entry/exit dates, summed commission and de-duplicated notes; you choose which trade number
   survives. Slices of one contract merge back to a `Single`, so merging never turns one decision
   into a `Custom` two-leg trade. Date filters and presets (`This month`, `Last 90d`, `YTD`)
   recompute every KPI on the filtered set.
6. **Actionable Watchlist** is a spreadsheet, not a form: one row per sector block, the LONG
   candidates in one cell and the SHORT candidates in the next. Type a ticker into a cell and press
   `Enter` (commas, spaces or a pasted column work too), click the `×` on a chip to take a name out —
   a removal offers **Undo** for four seconds. Every sector row is always drawn, so a block with no
   candidates reads `empty` instead of disappearing. Nothing opens a dialog; the only other control
   is **Notes: on/off**, which shows each candidate's sub-sector or comment under its ticker (hover
   a chip for the full comment, catalyst, conviction and source). Candidates keep their comments,
   catalysts, conviction, source and `row_offset`, so rows written by the harness stay badged
   (blue dot) and still land on their original sheet row in the export; a block that outgrows its
   rows in the sheet gains a row and pushes the blocks below it down rather than dropping a
   candidate.

---

## The booking rule (the part the spreadsheet made painful)

> The **third Friday is the last day that books into the month**. A fourth (or fifth) Friday
> expiry or exit rolls into the **following** month.

Applied to the seeded book:

| Leg | Expiry | 3rd Friday of that month | Books in |
| --- | --- | --- | --- |
| BB short call 9 | 2026-11-27 (4th Friday) | 2026-11-20 | **Dec 2026** |
| CSX short put 50 | 2026-11-20 (3rd Friday) | 2026-11-20 | Nov 2026 |
| BB long call 9 | 2027-01-15 | 2027-01-15 | Jan 2027 |
| DGXX long/short calls | 2027-03-19 | 2027-03-19 | Mar 2027 |
| ORCL trade exits | 2026-09-24 (4th Friday of Sep) | 2026-09-18 | **Oct 2026** |

The monthly block then computes itself:

- **Known / worse case** = realised P/L booked into that month **+** `−total cost` for every leg
  whose booking month is that month. That is the sheet's own rule from `pre session!U1:U2`: *short
  the expiry → enter the credit as profit; long the expiry → enter the full loss*.
- **Actual** = realised booked that month + current open P/L.
- **Swing factor** = Actual − Known.
- **Cum. known** chains through the months (all-time realised + booked legs so far); **cum.
  balance after commissions** subtracts all-time commissions.
- A leg can override its booking month (`Book in month` on the leg form) — the cell gets a dot so a
  manual call is always visible rather than silently baked in.

### The exported Known / worse case is a live formula

In the workbook, each block's `Known / worse case` cell is a formula rather than a stamped number, so
editing the sheet behaves the way editing the app does. It sums `-P` over the block's own leg rows,
keeping only the legs whose expiry books into that month — the third-Friday rule expressed in Excel
as `DATE(YEAR(G),MONTH(G),1+MOD(4-WEEKDAY(DATE(YEAR(G),MONTH(G),1),3),7)+14)`, with anything later
rolling into the next month. Two consequences worth knowing:

- Change a premium (`L`), a contract count (`I`) or an expiry (`G`) and the month columns re-book
  themselves; `Actual`, `Swing factor` and the cumulative chain in row 10 follow, because they are
  formulas too.
- **Long legs are a debit, short legs a credit.** The direction comes from the side cell `E` alone and
  the size from `ABS(L)*I*K`, so a short entered as the credit received (a negative premium) credits
  the month exactly like one entered positive — the sheet cannot be pushed into the wrong sign by a
  data-entry convention. The app's own calculation takes the premium's magnitude for the same reason
  (`calc.leg_metrics`), so the two never disagree.
- Each month block's own cutoff date sits in **row 5**, above its `Known` column, formatted `dd mmm`
  (`U5` is December's third Friday, `Y5` January's, and so on). The formula reads that cell, so the
  months are data the same way the legs are. `3RD FRIDAY OF THE MONTH` in `V5` is the row's title.
- A leg whose booking month was **pinned by hand** cannot be expressed in the sheet (there is no
  override field), so that leg contributes its booked amount as a literal and the rest stay live.

Changing the definition itself — say worst case becomes "premium kept" instead of "full loss" —
is an edit of that one formula in the downloaded file, which is why it is written as `-P` terms
rather than a single opaque `SUM`.


### A position with three legs exports as one boxed block with a blank row under it

The sheet was drawn for **two** legs per position: a name row, two leg rows, a blank
row and the dark totals row. The blank row is not decorative -- it is what keeps the
totals band's black rule off the last leg. A three-leg position filled it, so the
totals row sat directly under the short leg and the block read as a box with a heavy
line drawn through it. Every block now **reserves one spare row**: a position with one
or two legs still takes five rows, one with three takes six, and the blank row is
always there.

Three things had to change to make that look right:

- **Rows are styled by side, not by position.** The first slot inherits the sheet's
  Long row (its tint, the row's fill) and the second its Short row -- which is why a
  long leg in the closing slot used to come out on the short row's white fill, and why
  the PUT chip looked wrong sitting on it: an orange chip on a white row is not the
  look the sheet uses. Each leg row now takes its own side's look, and only the *rules*
  are decided by position: first leg medium above and thin below, interior legs thin on
  both sides, last leg thin above and the medium rule below.
- **The spare row is blank.** It carries the sheet's empty-row look and no rules at
  all, so nothing draws a line between the last leg and the totals band.
- **A one-leg block closes on the row under the leg**, exactly as the sheet drew it --
  the closing rule sits below an empty row rather than under the leg itself.

The grammar comes from the sheet's **first block**, not from each block's own rows,
because the sheet disagrees with itself: its last block draws two opening rows with
nothing closing the box, and one middle block opens thin where the others open medium.
Copying each block's own rows would have carried that drift into every export.

**Trading action** reads `=-P` of the block's *totals* row, not its first leg, so a
multi-leg position quotes the net of all its legs; the totals cell is itself
`=SUM(P<first slot>:P<last slot>)` and always spans every leg row the position uses.

`scripts/parity.py` compares a block cell by cell wherever it has not moved, and for a
block that grew it asserts the structure instead -- the leg box, the blank spare row
with no rules, and the totals band's rule.

### Month columns generate and retire themselves

Columns are the union of: the month the book is currently booking into, every live leg's booking
month, every realised trade's booking month, and any month you pin with **+ month** — filtered to
**≥ the current booking month**, then **filled so that every month between the first and the last is
a column**. The fill is the point: an empty month is still a month you are working through, and a grid
that skipped it read as if the book jumped from November to February.

The floor follows the **same third-Friday cutoff as booking itself**, not the calendar: the as-of
month stays the live column until its third Friday passes, and after that every expiry and exit
already books into the following month, so the as-of month retires. September/October dropping off
was always this rule; it now applies within the month too. Before the 20 Nov 2026 cutoff the book
draws `Nov 2026 · Dec 2026 · Jan 2027 · Feb 2027 · Mar 2027`; on 2026-11-22 the November third Friday
has passed, so November is no longer a booking column and the grid starts at December:

```
Dec 2026 · Jan 2027 · Feb 2027 · Mar 2027
```

November's realised P/L is not lost — it stays in the all-time totals, the **Realised** KPI and the
Realised tab — it simply stops being a column that can still book. Move the as-of date to 2027-01-20
(past the 15 Jan cutoff) and the grid becomes `Feb 2027 · Mar 2027`; add a December expiry and a
column disappears from neither end. The exported workbook draws one `Known / Actual / Swing` block per
column, so the empty months are there as zero-known columns with the cumulative chain running flat
through them, not as missing months — and an export past the third Friday carries no closed month
either, because it draws the same columns the tab does.

---

## Storage

```
data/portfolio.json        # source of truth, pretty-printed, human/agent readable
data/backups/portfolio-<stamp>.json   # last 40 writes, written before each save
data/schema.json           # machine-readable contract + endpoint list (generated)
```

Writes are locked, written to a temp file, `fsync`ed and atomically renamed. A corrupt store
recovers the newest backup automatically. Each mutation appends to `audit` with actor, action and
detail (last 500 kept), so harness edits are attributable.

Top-level shape:

```jsonc
{
  "schema_version": 2,
  "meta":      { "opening_balance": 40000, "default_multiplier": 100,
                 "booking_rule": "third_friday", "as_of": null, "month_extras": [],
                 "updated_at": "...", "updated_by": "..." },
  "categories": ["Hardware", "Software", "Real economy"],
  "positions": [ { "id", "ticker", "company", "category", "bias", "next_earnings",
                   "underlying_price", "notes",
                   "legs": [ { "id", "side", "type", "strike", "expiry", "contracts",
                               "contracts_initial", "multiplier", "cost_premium", "bid",
                               "ask", "mid_override", "book_month_override", "opened",
                               "notes", "quote" } ] } ],
  "realised":  [ { "id", "trade_no", "ticker", "company",
                   "structure": "single | calendar | vertical | diagonal | roll | custom",
                   "entry_date", "exit_date", "commission", "notes",
                   "legs": [ { "id", "side", "type", "strike", "expiry", "contracts",
                               "multiplier", "price_in", "price_out", "notes" } ] } ],
  "watchlist": [ { "id", "ticker", "side", "sector", "sub_sector", "comment", "catalyst",
                   "conviction", "source", "updated_by", "updated_at" } ],
  "audit":     [ { "ts", "actor", "action", "detail" } ]
}
```

A **closed trade is one object with one or more legs.** A single option is `legs: [one]`; a
calendar (same strike, two expiries), a vertical (two strikes, one expiry) or a diagonal (both
differ) is the same object with two legs. `structure` is derived from the legs whenever it is not
set explicitly, and the realised statistics count **one closed leg as one trade** — the sheet's
Realised tab is a leg ledger and its KPI block reads the rows — so a calendar that wins on the
short leg and loses on the long one is one win and one loss. The trade object's P/L is still the
sum of its legs, which is what the month grid books; grouping never changes the net total.
Schema v1 files (one row per leg sharing a `trade_no`) are migrated on load by folding the rows into
`legs[]`; nothing is lost and no manual step is needed.

Derived numbers (P/L, booking months, month grid, KPIs) are **not** stored — they are recomputed
from these inputs, so the file never contains a stale total.

`contracts_initial` is the only stored field that exists purely to remember history: a partial close
sets it to the size the leg opened at, so `contracts` can fall to a fraction of the position without
the panel losing the fact that it was ever larger. It is `null` on a leg that has never been partly
closed, and the export keeps writing `contracts` (the live count) because that is what the sheet's
P/L formulas multiply.

---

## Access for the DeepSeek harness

Three ways in, no server required for the first two:

**1. Read the file.** `data/portfolio.json` is authoritative; `data/schema.json` describes it.

**2. Snapshot script** (one call, includes the computed book):

```bash
../../.venv/bin/python scripts/summary.py            # readable table
../../.venv/bin/python scripts/summary.py --json     # full JSON incl. months + swing factors
../../.venv/bin/python scripts/summary.py --json --as-of 2026-11-15
```

**3. REST API** while the app is running (`actor` is recorded in the audit trail):

```bash
# read model (positions with P/L, month grid, KPIs, watchlist)
curl -s 'http://127.0.0.1:8117/api/state?as_of=2026-10-25'

# rewrite the watchlist as the harness (mode: merge | replace_sector | replace_all)
curl -s -X POST 'http://127.0.0.1:8117/api/watchlist/bulk?actor=deepseek-harness' \
  -H 'Content-Type: application/json' -d '{
    "mode": "merge",
    "updated_by": "deepseek-harness",
    "items": [{
      "ticker": "MCHP", "side": "LONG", "sector": "Tech Hardware",
      "sub_sector": "Analog semis", "conviction": "high",
      "comment": "Industrial/auto recovery + Nov 5 earnings",
      "catalyst": "Oct 8 TSMC sales; Oct 27 TXN; Nov 5 MCHP earnings"
    }]
  }'

# other writes
POST   /api/positions            PATCH /api/positions/{id}        DELETE /api/positions/{id}
POST   /api/positions/{id}/legs  PATCH /api/positions/{id}/legs/{leg}
POST   /api/positions/{id}/legs/{leg}/close     # leg (or N of its contracts) -> Realised trade
POST   /api/realised             PATCH /api/realised/{id}         DELETE /api/realised/{id}
POST   /api/realised/{id}/legs    PATCH/DELETE /api/realised/{id}/legs/{leg_id}
POST   /api/realised/merge        {"ids": ["trd_a", "trd_b"], "keep_id": "trd_a"}   # fold 2+ trades
POST   /api/watchlist            PATCH /api/watchlist/{id}        DELETE /api/watchlist/{id}
PATCH  /api/meta                 POST  /api/months                POST /api/quotes/refresh
GET    /api/export/xlsx?as_of=YYYY-MM-DD&refresh=1  GET /api/schema   GET /api/health
```

Any mutation returns `{ ok, state, view }`, i.e. the fresh read model — no second round-trip.

---

## Spreadsheet export

`GET /api/export/xlsx` (or the button) returns **your workbook with today's numbers in it**: the
exporter opens `app/template.xlsx` — a byte-identical copy of `example_sheet.xlsx` — and writes
values and formulas into the cells the sheet already has. No style is ever invented: the export
only re-uses style records that are already in your workbook, so every font, fill, border, number
format, column width, row height, freeze pane, hidden gridline, tab colour, conditional-format rule
and data validation arrives exactly as you drew it. Where a row has to be created — a sector
holding more candidates than the sheet left room for — it is stamped with the sheet's own body-row
style instead of being left bare.

The pre session tab is a *pre session* sheet, so the export pulls quotes first: `GET
/api/export/xlsx` refreshes from yfinance before it builds (`?refresh=0` reuses whatever is stored),
which is what stops `BID`, `MID` and `ASK` arriving empty. How those three cells fill depends on
what the chain actually offers at the moment of the click — see [Live data](#live-data).

`app/template.xlsx` is the only thing that defines the look. Replace it and the next export
follows the new sheet:

```bash
cp ../example_sheet.xlsx app/template.xlsx      # or point it at any edited copy
curl -s -o data/export-check.xlsx localhost:8117/api/export/xlsx
python scripts/parity.py --exported data/export-check.xlsx   # still identical?
```

No restart is needed — the template is read on every export. The exporter also reads the sheet's
own geometry each time (where the month blocks start, how many the sheet drew, where the
`Trading action` column sits, and their widths), so a sheet with a different number of month blocks
works too: as many months as it has are reused as style donors and any further month clones the
last one. `scripts/parity.py` compares against `app/template.xlsx`, i.e. the new sheet, so it
answers "is the export still your sheet?" for whatever you drop in.

### What gets written where

| Tab | Cells |
| --- | --- |
| `Realised` | One row per leg from row 9, in trade-number order, each trade's number on its first leg (a calendar is still two rows, one trade). `M:P` keep the sheet's own formulas (`=IF(D9="Long",(J9-I9)*H9*100,(I9-J9)*H9*100)`), so `H2:O2` / `D1:E6` recalculate themselves. Per-leg entry/exit dates are written when the sheet recorded legs closing on different days. `R` carries the trade's commission. |
| `pre session` | `S5` opening balance, `S6`→`Realised!O2`, `S7` the sum of the block totals, `S8` commission, `S10` account balance; `G6:I10` the category × Long/Short gross-market-value matrix pointing at each block's `Q` cell; then one 5-row block per position from row 13 (name row, three leg slots, totals row) with the sheet's `P/Q/R/S` formulas, and per-month `Known / Actual / Swing` at each block's totals row. |
| `Actionable Watchlist` | The sheet's own sector blocks and row alignment are kept — `LONG candidates` in `H`, `SHORT candidates` in `I`, the sub-sector / comment text in `G` and `J`, each candidate back on the row it came from. See *The watchlist tab* below for how a block grows and how each cell is styled. |

### The call/put colour coding on `pre session`

A leg row is coded twice in colour by the sheet itself, and the export reproduces both from the
value it writes rather than from the row it borrowed:

| Cell | Column | `CALL` | `PUT` | `Long` | `Short` |
| --- | --- | --- | --- | --- | --- |
| `F` (TYPE) | 6 | dark blue, `333F4F` | the theme's orange accent (`theme 5`) | — | — |
| `E` (LONG / SHORT) | 5 | — | — | tinted (`theme 6`) | the sheet's plain white |

This matters because a row's style is copied wholesale from whichever template row it lands on:
before the code was restamped, a PUT booked into a slot the sheet had drawn for a CALL exported
**blue**, and a CALL landing on a PUT row exported **orange**. Now `leg_role_styles()` reads the
sheet's own look for each keyword — the fill and the white bold font that carries it — and only
those two travel: borders, alignment and number format stay as the row has them, because the sheet
draws a block's rules by position (the last leg slot has a medium rule under it, the slots above it
a thin one). `scripts/parity.py` checks those two columns against the keyword's coding instead of
against the same template cell, so the rule is enforced rather than assumed.

Swap the template and the colours follow *your* sheet: the exporter looks for the look each keyword
has in the new file, and a sheet that does not colour a keyword simply exports it unstyled.

### The watchlist tab

The tab is the book's candidate list written into the sheet's own sector blocks, so it is re-filled
rather than redrawn:

- **Every sector the sheet draws is always drawn**, in the sheet's order, with the sheet's own
  header wording — a block with no candidates keeps its band and its blank rows rather than
  vanishing. A sector the sheet never had is appended below the last block, in first-use order.
- **The sheet's row allocation is the floor.** A block keeps all the rows the sheet gave it, so a
  candidate stays on the row it came from (`row_offset`). A block only grows when the book holds
  more candidates than fit, and the blocks below it shift down by exactly that many rows.
- **One look per role.** Tickers and notes are stamped with the style your sheet uses *most* for
  that role — the bold Arial blue for a LONG, bold Arial red for a SHORT, bold Calibri for the
  sub-sector / comment — so the whole tab is consistent even where a candidate lands on a row the
  sheet left blank, and hand-picked one-off colours in the template give way to that one look.
  Every other cell goes back to the sheet's own empty-row style, so a colour never lingers on a row
  with no ticker. The sector band is written into **both** `H` and `I` on a header row.
- The exporter reads all of this from the template each time — block order, block heights, and the
  four role styles by majority vote over the sheet's own cells — so a differently laid out sheet
  is followed without code changes.

### The part that moves: months to the right

Everything left of column `T` is the sheet as drawn. From `U` rightwards the export re-emits the
month blocks (`Known / worse case`, `Actual`, `Swing factor`, 4-column stride) for **whatever
months the book currently needs**, then the `Trading action` column after them — so a 6-month book
carries six blocks and September simply stops existing when the calendar turns. Row 6 gets the
month label (`Jan 27` once the year rolls), row 7 keeps your `Realized` label with that month's
realised P/L beside it, and row 10 carries the cumulative chain (`=+S6+U17+…`, then `=+U10+Y17+…`).

Block styling is cloned from the sheet's own blocks: months 1-3 reuse `U:W`, `Y:AA` and `AC:AE`
exactly, and every later month clones the last one. Column widths follow the same 15.57 / 12.86 /
13.43 / 2.71 pattern and the action column keeps its 42.

### Proof it matches

`scripts/parity.py` compares an exported workbook with the original and fails on any difference in
sheet order, freeze panes, gridlines, tab colour, column widths, row heights, data validations,
conditional-format rules or **the style of every cell** (font, fill, border, alignment, number
format). On `Realised` it also compares *values*, because that tab is the sheet's own ledger.

`Actionable Watchlist` is the one tab checked **by role rather than by coordinate**, because it
holds the book's candidates rather than the sheet's own data: names come and go, and a block that
gains a candidate pushes the blocks below it down a row. Cell-for-cell comparison would fail there
for reasons that have nothing to do with the look, so instead the tab must satisfy: rows 1-6
(title, dateline, column headers) untouched; every sector block the sheet drew still drawn and in
the sheet's order; the sector band on both `H` and `I` of each header row; and on every row, every
empty cell carrying the sheet's own empty-row style and every cell with content carrying the
sheet's own style for its role. That is what makes a missing sector or a plain-black ticker a test
failure instead of a surprise in Excel.

    curl -s -o data/export-check.xlsx localhost:8117/api/export/xlsx
    python scripts/parity.py --exported data/export-check.xlsx
    cells compared: 28992
    PASSED: export matches the original workbook's styling and layout

`scripts/selfcheck.py` runs the same comparison as one of its checks, plus cell-by-cell assertions
about what landed where (trade numbers, block geometry, month block count, cumulative formulas,
category matrix, watchlist row alignment), and a **watchlist round-trip**: it seeds the book from
the sheet's own watchlist, exports it, and requires every sector, ticker and note to come back on
the same row — the strongest available statement of "this still looks like your sheet".

Two things the export adds on purpose, both inside cells the sheet already had: the dateline in
`Actionable Watchlist!G4` (`As of … · 35 candidates · 6 live positions`) and, per month block, the
realised amount beside the `Realized` label (the sheet left that cell empty).

---

## Live data

- `Pull live data` calls yfinance once per ticker: underlying `fast_info.last_price` (fallback:
  `info`, then 5-day close), option chain per distinct expiry, matched to your strike within
  0.25 %.
- **The midpoint is the plain average of bid and ask** — the spreadsheet's `N = (M+O)/2`. A zero
  bid is still a real quote, so `0.00 / 0.15` gives `0.075`, not `0.15`.
- **A book that exists is the midpoint.** The Pre session tab computes `(BID+ASK)/2` whenever both
  sides are real, so the `Mid` column can never contradict the `BID` and `ASK` printed on the same
  row, and `N` exports the sheet's own `(M+O)/2` formula beside them.
- `Mid override` is a **fallback, not a trump card**: it prices a leg the chain cannot quote at all
  — no running session, an expiry Yahoo has dropped, a strike that is not listed. It used to beat a
  live book, and that is exactly how three mids typed once on 6 Nov (BB `0.88`, HOOD `2.70`,
  XLV `5.95`) sat frozen on screen next to bid/ask that had since moved 10–30 %: the column showed
  `0.880` between `0.95` and `1.03` and was averaging nothing. A hand-typed mid on a leg that *does*
  have a book is now ignored for pricing and named in the badge's tooltip rather than silently
  dropped; the three stale values were cleared on 2026-11-09.
- The template is a copy of your own sheet, so it carries your example book in the same grid. Every
  row the grid could use is cleared before the real blocks are written, down to the lowest row the
  template fills — otherwise a book smaller than the example leaves a stranger's `HOOD 95.07` and
  literal `0.00` leg slots on the tab, quoting a spread that never existed.
- **`0.00 / 0.00` is not a quote, it is the absence of one.** Yahoo prices every contract that way
  whenever no session is running — measured 2026-11-06 07:30 ET, 0 of SPY's 162 nearest-dated calls
  carried a bid while every one of them carried a `lastPrice`. So a pull that comes back 0/0 caches
  nothing, writes no bid or ask, and prices the leg off the last trade. That is the bug that used to
  put a 0.00 spread and a 0.00 midpoint into the pre session tab of an early-morning export.
- Each leg keeps the last **two-sided book it actually saw** in `leg.book`, stamped with the time it
  was taken. A later pull that finds no book — pre-market, weekend, a chain Yahoo has zeroed — carries
  that book forward as `status: carried-book`, so `BID`/`ASK` stay real numbers with a known age rather
  than reverting to zero. The badge's tooltip names when the book was taken and when the pull ran. A
  leg that has never seen a book exports a midpoint with no spread; a leg whose strike or expiry is
  edited does not inherit the previous contract's book.
- The export in the pre session tab therefore fills as follows: live book → `BID`, `ASK` and the
  sheet's own `(M+O)/2`; carried book → the same three cells with the carried numbers; no book at
  all → `MID` from a manual override or the last trade, `BID`/`ASK` empty. `X-Quote-Summary` on the
  download response reports the split (`live` / `carried` / `no_book`), and the UI shows it as a
  toast after the save.
- Per-leg quote status is recorded and surfaced as a coloured dot: `ok`,
  `zero-bid` / `zero-ask` (one side is 0; the midpoint is still averaged), `ask-only` / `bid-only`
  (the other side is absent from the chain altogether), `carried-book`, `last-only`, `missing`,
  `strike-not-found` (nearest strike reported), `error`.
- A failing ticker leaves the previous numbers untouched and reports the error; nothing is
  invented or interpolated.
- `next_earnings` is refreshed from yfinance only when the returned date is today or later;
  otherwise your manual text is kept (`HPE` keeps `3rd Dec`, because yfinance still reports the
  September date).

---

## Files

```
portfolio-ui/
├── run.sh                     # uvicorn on 127.0.0.1:8117
├── app/
│   ├── main.py                # FastAPI routes, static mount, xlsx download
│   ├── calc.py                # leg/position maths, third-Friday rule, month grid, KPIs
│   ├── store.py               # JSON store, atomic writes, backups, normalisation
│   ├── quotes.py              # yfinance quote pull
│   ├── exporter.py            # writes our numbers into template.xlsx
│   ├── template.xlsx          # copy of example_sheet.xlsx (the export's look)
│   └── seed.py                # one-time import from ../example_sheet.xlsx
├── static/  index.html · app.js · styles.css
├── scripts/ selfcheck.py · summary.py · parity.py
└── data/    portfolio.json · backups/ · schema.json
```

Rebuild the store from the original spreadsheet (overwrites the book):

```bash
../../.venv/bin/python -m app.seed --force
```

Verify booking-rule fixtures, trade/calendar maths, store integrity, month-grid identities, the
export's cells, that its styling matches the original workbook, and that the sheet's own watchlist
round-trips back onto the same rows (181 checks):

```bash
../../.venv/bin/python scripts/selfcheck.py
```

---

## Decisions taken while porting (worth a look)

- **Seeded from the sheet, not invented.** `app/seed.py` reads positions, legs, trades and the
  watchlist directly from `example_sheet.xlsx` — the watchlist reader walks the sector blocks
  rather than a hand-typed list, so all 35 candidates and their row positions come across.
  Categories are read from the sheet's own
  header formulas (`H6=+Q17`, `I6=+Q22`, `H7=+Q27+Q32`, `I7=+Q37`, `H8=+Q42`, `I8=+Q8`), which
  means **CSX landed in `Software`** exactly as the spreadsheet had it. Change it in the UI if that
  was a typo.
- Empty leg slots in the sheet (RBLX short put, HPE short put, HOOD short call — strike 0, no
  contracts) were dropped rather than stored as phantom legs.
- **The sheet's Long/Short buckets were inconsistent with the positions themselves.** HPE and RBLX
  were filed under `Long` although both are long *puts* (bearish), and BB was filed under `Short`
  although it is a long call calendar (bullish debit). Those three now derive their bias from the
  legs — `Software / Short` for HPE and RBLX, `Hardware / Long` for BB — which is also what the
  exported `Category × Long/Short` matrix now sums (`Hardware: =Q17+Q22`, `Software: =Q27+Q32+Q37`).
  Every other position already matched its legs.
- The sheet's `less commission` cell pointed at a single cell (`=-Realised!R11`, which was blank).
  The app sums the whole `Commission` column instead:
  `Account balance = opening + realised − commissions + open P/L`.
- Premiums are per share, P/L = `(out − in) × contracts × multiplier` (`multiplier` defaults to 100
  and is editable per leg, so non-equity multipliers are possible).
- **A closed trade is modelled as a trade with legs, not as a row.** In the store that is one
  object (`legs[]`); in the UI it is one row per trade with its legs folded underneath and a
  `Structure` chip (Single / Calendar / Vertical / Diagonal / Roll / Custom); in the exported sheet
  it is one row per leg sharing a trade number, because that is how the original was written. So
  the same calendar reads as *one trade* for statistics and as *two rows* on paper — which is what
  the sheet's own `Closed trades = MAX(A:A)` was trying to express.
- The structure is derived from the legs (same strike + different expiry ⇒ calendar, and so on) and
  can be overridden per trade; adding a leg recomputes it.
- **Merging** exists because the broker closes legs separately: two singles on the same ticker fold
  into one trade in the UI (`Merge selected`) or via `POST /api/realised/merge`. It refuses mixed
  tickers (a calendar is one underlying) and refuses to shrink a trade to zero legs; leg count is
  preserved, so merging only changes how the trade is *counted*, never what it is worth.
- **Closing part of a leg is a smaller close, not a new position.** `contracts` on the close request
  decides how much realises; the leg keeps the rest at its original `cost_premium` and its
  `contracts_initial`, so a 6-lot closed 3/2/1 produces three single-leg trades whose P/L sums to
  the one trade the leg would have made. If those slices were really one decision, `Merge selected`
  folds them back into one trade — the same reason merging exists for calendars.
- Known limitations: no greeks/IV surface, single currency, and yfinance option data can be thin or
  delayed for illiquid strikes — those legs are flagged rather than guessed. A partial close has no
  separate realised-price-per-slice ledger: each exit is its own trade, so three slices of one leg
  are three trades of one leg each (merge them if they were one decision).

---

## Repository note

Source only. `data/` (the book itself) is gitignored, so a fresh clone starts with an empty
book rather than this one. The export's template is **not** included either: `app/template.xlsx`
is a copy of a personal workbook, so drop your own copy there to enable **Export .xlsx** — the
exporter reads whatever sheet is present and invents no styling.

```bash
./run.sh          # creates .venv on first run, serves http://127.0.0.1:8117
```
