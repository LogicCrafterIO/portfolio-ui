"""All book arithmetic: leg/position valuation, the third-Friday booking rule,
realised trade stats and the monthly projection grid.

Nothing here touches disk or the network; it is pure functions over the store
dict, so it is trivially testable and safe for the DeepSeek harness to reuse.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

MONTH_KEYS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #

def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
                "%d %b %Y", "%d %B %Y", "%b %d %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def third_friday(year: int, month: int) -> date:
    """Return the third Friday of the given month (monthly option expiry)."""
    first_weekday, _ = calendar.monthrange(year, month)  # Monday == 0
    first_friday = 1 + (4 - first_weekday) % 7
    return date(year, month, first_friday + 14)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def booking_period(value, rule: str = "third_friday") -> tuple[int, int] | None:
    """Which booking month an expiry or exit date belongs to.

    The house rule: the third Friday is the last day that books into the month.
    A fourth (or fifth) Friday expiry/exit rolls into the following month --
    this is what the spreadsheet note above the monthly block was describing.

    rule="calendar" simply uses the calendar month.
    """
    d = parse_date(value)
    if d is None:
        return None
    if rule == "calendar":
        return (d.year, d.month)
    cutoff = third_friday(d.year, d.month)
    if d <= cutoff:
        return (d.year, d.month)
    return _next_month(d.year, d.month)


def current_booking_month(as_of: date, rule: str = "third_friday") -> tuple[int, int]:
    """The month the book is booking into *today*.

    Booking a date already uses the third-Friday cutoff (booking_period): an
    expiry or exit after the third Friday rolls into the following month. The
    month grid has to use the same cutoff for its floor, or it keeps drawing a
    month that can no longer receive anything. On 2026-11-22 the November
    third Friday (2026-11-20) has passed, so the current booking month is
    December -- a November column would be a closed month presented as live.

    rule="calendar" keeps the plain calendar month, where the cutoff does not
    exist and the month only turns on the 1st.
    """
    period = booking_period(as_of, rule)
    return period if period else (as_of.year, as_of.month)


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month_key(key: str) -> tuple[int, int]:
    year, month = str(key).split("-")
    return int(year), int(month)


def month_label(key: str, with_year: bool = True) -> str:
    year, month = parse_month_key(key)
    if with_year:
        return f"{MONTH_KEYS[month - 1]} {year}"
    return MONTH_KEYS[month - 1]


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


def today() -> date:
    return date.today()


def contiguous_months(keys: list[str]) -> list[str]:
    """Every month from the first key through the last, with no gaps.

    The month grid is a calendar, not a list of the months something happens to
    book into. It used to be built only from live legs, realised trades and pins,
    so a book whose next expiries were February and March exported as
    `Nov | Feb | Mar` -- December and January simply were not columns, and the
    reader could not tell the two empty months from two months that had been
    cut. Filling the interval makes the first and last month bound the grid and
    every month between them is drawn, carrying zeros if nothing lands in it.
    """
    keys = sorted(k for k in keys if k)
    if not keys:
        return []
    last = parse_month_key(keys[-1])
    year, month = parse_month_key(keys[0])
    out = []
    while (year, month) <= last:
        out.append(month_key(year, month))
        year, month = add_months(year, month, 1)
    return out


# --------------------------------------------------------------------------- #
# legs and positions
# --------------------------------------------------------------------------- #

def has_book(leg: dict) -> bool:
    """True when the leg carries a two-sided book: both Bid and Ask are real.

    `0.00 / 0.00` is the absence of a quote (see quotes.py), so it is not a book.
    One genuine zero -- `0.00 / 0.15` on a thin chain -- is.
    """
    bid, ask = leg.get("bid"), leg.get("ask")
    if bid is None or ask is None:
        return False
    return bool(bid or ask)


def leg_mid(leg: dict) -> float | None:
    """The midpoint the Pre session tab prints, always the average of the book beside it.

    A book that exists *is* the market's midpoint -- the sheet's `N = (M+O)/2` -- so
    it is computed first, and the Mid column can never contradict the Bid and Ask
    printed on the same row. `mid_override` is a **fallback**, not a trump card: it
    exists to price a leg the chain cannot quote at all (no running session, an
    expiry Yahoo has dropped, a strike that is not listed). It used to beat a live
    book, which is how three mids typed once in November froze on screen next to a
    Bid and an Ask that had moved 10-30% since, and the column stopped being an
    average of anything.
    """
    bid, ask = leg.get("bid"), leg.get("ask")
    if has_book(leg):
        return (float(bid) + float(ask)) / 2.0
    if leg.get("mid_override") is not None:
        return float(leg["mid_override"])
    quote = leg.get("quote") or {}
    if quote.get("mid") is not None:
        return float(quote["mid"])
    if bid is not None and bid:
        return float(bid)
    if ask is not None and ask:
        return float(ask)
    if quote.get("last") is not None:
        return float(quote["last"])
    return None


def leg_mid_source(leg: dict) -> str:
    """Which input the midpoint came from: quote | manual | override | last | none."""
    bid, ask = leg.get("bid"), leg.get("ask")
    if has_book(leg):
        # the average is computed from the book; "quote" says the book is a live pull,
        # "manual" that the pair was typed in by hand
        quote = leg.get("quote") or {}
        return "quote" if quote.get("mid") is not None else "manual"
    if leg.get("mid_override") is not None:
        return "override"
    quote = leg.get("quote") or {}
    if quote.get("mid") is not None:
        return "quote"
    if bid is not None or ask is not None:
        return "manual"
    if quote.get("last") is not None:
        return "last"
    return "none"


def leg_metrics(leg: dict, rule: str = "third_friday", as_of: date | None = None) -> dict:
    as_of = as_of or today()
    side = leg.get("side", "Long")
    sign = 1.0 if side == "Long" else -1.0
    # A premium is always quoted positive; a long pays it, a short receives it.
    # The magnitude is taken, not the typed sign, because the convention above is
    # what every formula here assumes: a short entered as a credit (a negative
    # number) used to flip its own worst case into a debit, and a long entered the
    # same way paid a negative premium. The side is the only thing that decides
    # direction, so the sign of the number is noise.
    contracts = float(leg.get("contracts") or 0)
    k = float(leg.get("multiplier") or 100)
    cost = abs(float(leg.get("cost_premium") or 0))
    mid = leg_mid(leg)

    total_cost = sign * cost * contracts * k          # spreadsheet column P
    market_value = None if mid is None else sign * mid * contracts * k   # Q
    option_pl_ps = None if mid is None else sign * (mid - cost)          # R
    position_pl = None if option_pl_ps is None else option_pl_ps * contracts * k  # S
    cost_basis = abs(total_cost)

    return {
        "id": leg.get("id"),
        "side": side,
        "type": leg.get("type", "CALL"),
        "strike": leg.get("strike"),
        "expiry": leg.get("expiry"),
        "contracts": contracts,
        # set when a leg has been partly closed: the count it opened with, so the
        # panel can read "3 of 6" instead of silently showing a smaller leg
        "contracts_initial": leg.get("contracts_initial"),
        "multiplier": k,
        "cost_premium": cost,
        "bid": leg.get("bid"),
        "ask": leg.get("ask"),
        "mid": mid,
        "mid_override": leg.get("mid_override"),
        "mid_source": leg_mid_source(leg),
        # a hand-typed mid that a real book has superseded: kept visible, never priced
        "mid_override_ignored": (leg.get("mid_override") is not None
                                 and leg_mid_source(leg) != "override"),
        "total_cost": round(total_cost, 2),
        "market_value": None if market_value is None else round(market_value, 2),
        "option_pl_ps": None if option_pl_ps is None else round(option_pl_ps, 4),
        "position_pl": None if position_pl is None else round(position_pl, 2),
        "pl_pct": None if position_pl is None or not cost_basis else round(position_pl / cost_basis, 4),
        "quote": leg.get("quote"),
        "quote_status": (leg.get("quote") or {}).get("status"),
        "quote_ts": (leg.get("quote") or {}).get("ts"),
        "book_month": month_key(*booking_period(leg.get("expiry"), rule)) if leg.get("expiry") else None,
        "book_month_override": leg.get("book_month_override"),
        "effective_book_month": (
            leg.get("book_month_override")
            or (month_key(*booking_period(leg.get("expiry"), rule)) if leg.get("expiry") else None)
        ),
        "booked_amount": round(-total_cost, 2),   # worst case: short keeps credit, long loses all
        "opened": leg.get("opened"),
        "notes": leg.get("notes", ""),
        "status": _leg_status(leg, as_of),
    }


def _leg_status(leg: dict, as_of: date) -> str:
    expiry = parse_date(leg.get("expiry"))
    if not leg.get("contracts"):
        return "empty"
    if expiry is None:
        return "no-expiry"
    if expiry < as_of:
        return "expired"
    if (expiry - as_of).days <= 7:
        return "expiring"
    return "open"


def derive_bias(legs: list[dict]) -> str:
    """Directional stance of a position, from what the legs actually do.

    A long call and a short put are bullish; a long put and a short call are bearish.
    Exposure is weighted by contracts x multiplier x premium so a debit spread resolves
    to the side that was paid for (e.g. long 20 x 0.65 calls vs short 20 x 0.35 calls
    is a bullish debit spread, not a tie).
    """
    net = 0.0
    for leg in legs or []:
        q = float(leg.get("contracts") or 0)
        if not q:
            continue
        k = float(leg.get("multiplier") or 100)
        premium = abs(float(leg.get("cost_premium") or 0)) or 1.0
        bullish = ((leg.get("side") == "Long") == (str(leg.get("type")).upper().startswith("C")))
        net += (1.0 if bullish else -1.0) * q * k * premium
    if net > 0:
        return "Long"
    if net < 0:
        return "Short"
    return "Long"


def position_metrics(pos: dict, rule: str = "third_friday", as_of: date | None = None) -> dict:
    as_of = as_of or today()
    legs = [leg_metrics(leg, rule, as_of) for leg in (pos.get("legs") or [])]
    live = [l for l in legs if l["contracts"]]

    total_cost = sum(l["total_cost"] for l in legs)
    mvs = [l["market_value"] for l in legs]
    market_value = None if any(v is None for v in mvs) else sum(mvs)
    pls = [l["position_pl"] for l in live]
    position_pl = None if any(v is None for v in pls) else sum(pls)

    long_mv = sum(l["market_value"] or 0 for l in legs if l["side"] == "Long")
    short_mv = sum(l["market_value"] or 0 for l in legs if l["side"] == "Short")
    basis = sum(abs(l["total_cost"]) for l in legs)
    stored_bias = str(pos.get("bias") or "Long")
    bias_derived = derive_bias([l for l in legs if l["contracts"]])
    bias_auto = stored_bias.strip().lower() in ("auto", "derive", "derived")
    bias = bias_derived if bias_auto else stored_bias

    return {
        "id": pos.get("id"),
        "ticker": pos.get("ticker"),
        "company": pos.get("company"),
        "category": pos.get("category"),
        "bias": bias,
        "bias_auto": bias_auto,
        "bias_labelled": stored_bias,
        "bias_derived": bias_derived,
        "bias_source": "auto" if bias_auto else "manual",
        "bias_mismatch": bool(live) and not bias_auto and bias != bias_derived,
        "next_earnings": pos.get("next_earnings"),
        "earnings_date": pos.get("earnings_date"),
        "notes": pos.get("notes", ""),
        "underlying_price": pos.get("underlying_price"),
        "underlying_quote": pos.get("underlying_quote"),
        "legs": legs,
        "leg_count": len(live),
        "total_cost": round(total_cost, 2),
        "market_value": None if market_value is None else round(market_value, 2),
        "position_pl": None if position_pl is None else round(position_pl, 2),
        "pl_pct": None if position_pl is None or not basis else round(position_pl / basis, 4),
        "long_mv": round(long_mv, 2),
        "short_mv": round(short_mv, 2),
        "book_months": sorted({l["effective_book_month"] for l in live if l["effective_book_month"]}),
        "warnings": _position_warnings(live, pos),
    }


def _position_warnings(legs: list[dict], pos: dict) -> list[str]:
    out = []
    if not any(l["contracts"] for l in legs):
        out.append("no live legs")
    for l in legs:
        if l["status"] == "expired":
            out.append(f"{l['side']} {l['type']} {l['strike']} expired {l['expiry']} — still on the book")
        if l["status"] == "no-expiry":
            out.append(f"{l['side']} {l['type']} leg has no expiry")
        if l["mid"] is None:
            out.append(f"{l['side']} {l['type']} {l['strike']} has no price (bid/ask missing)")
        if l.get("quote_status") in ("missing", "error"):
            out.append(f"{l['side']} {l['type']} {l['strike']}: live quote {l['quote_status']}")
    if not pos.get("underlying_price"):
        out.append("no underlying price")
    return out


# --------------------------------------------------------------------------- #
# booking a leg out -- all of it, or a slice of the contracts
# --------------------------------------------------------------------------- #

def _as_float(value) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def leg_close_plan(leg: dict, position: dict | None = None, *, price_out=None,
                   contracts=None, exit_date=None, entry_date=None,
                   commission=None, notes=None) -> dict:
    """Work out what booking all or part of an open leg into Realised does.

    A partial close is the same arithmetic on fewer contracts: the slice sold becomes
    a Realised trade carrying the leg's own premium in, and the leg keeps the rest, so
    the book's cost basis, market value and P/L all shrink by exactly that slice. That
    matters because the sheet values a leg as `premium x contracts x multiplier` -- sell
    half the contracts and the leg is half the position, not a new position.

    `contracts` is the number of contracts this exit covers; None/"" means all of them.
    Returns {"held", "closed", "remaining", "partial", "trade"} where `trade` is an
    un-normalised Realised trade (no id, no trade_no -- the store assigns those).

    Raises ValueError when the request cannot be booked: a missing exit premium, a
    non-numeric or non-positive count, or more contracts than the leg holds.
    """
    held = int(round(float(leg.get("contracts") or 0)))
    price = _as_float(price_out)
    if price is None:
        raise ValueError("price_out is required to close a leg")
    if held <= 0:
        raise ValueError("this leg has no contracts left to close")

    if contracts is None or contracts == "":
        closed = held
    else:
        count = _as_float(contracts)
        if count is None:
            raise ValueError("contracts to close must be a number")
        closed = int(round(count))
        if closed <= 0:
            raise ValueError("contracts to close must be at least 1")
        if closed > held:
            raise ValueError(f"only {held} contract{'' if held == 1 else 's'} held on this leg")

    remaining = held - closed
    partial = remaining > 0
    exit_day = str(exit_date or "").strip() or today().isoformat()
    entry_day = (str(leg.get("opened") or "").strip()
                 or str(entry_date or "").strip() or today().isoformat())

    side = leg.get("side", "Long")
    kind = leg.get("type", "CALL")
    text = (str(notes).strip() if notes not in (None, "")
            else f"closed from pre session ({side} {kind})")
    if partial:
        text = f"{text} · partial {closed} of {held}"

    trade = {
        "ticker": (position or {}).get("ticker"),
        "company": (position or {}).get("company") or (position or {}).get("ticker"),
        "structure": "single",
        "entry_date": entry_day,
        "exit_date": exit_day,
        "commission": commission or 0,
        "notes": text,
        "legs": [{
            "side": side, "type": kind, "strike": leg.get("strike"),
            "expiry": leg.get("expiry"), "contracts": closed,
            "multiplier": leg.get("multiplier") or 100,
            "price_in": leg.get("cost_premium"), "price_out": price,
        }],
    }
    return {"held": held, "closed": closed, "remaining": remaining,
            "partial": partial, "trade": trade}


# --------------------------------------------------------------------------- #
# realised trades + KPIs  (a ledger row = one closed leg; the sheet counts legs)
# --------------------------------------------------------------------------- #

STRUCTURE_LABELS = {
    "single": "Single",
    "calendar": "Calendar",
    "vertical": "Vertical",
    "diagonal": "Diagonal",
    "roll": "Roll",
    "custom": "Custom",
}


def derive_structure(legs: list[dict]) -> str:
    """Name the shape of a closed trade from its legs.

    single   -- one option
    calendar -- same strike and type, different expiries
    vertical -- same expiry, different strikes
    diagonal -- both strike and expiry differ
    """
    legs = [l for l in (legs or []) if isinstance(l, dict)]
    if len(legs) <= 1:
        return "single"
    expiries = {str(l.get("expiry")) for l in legs}
    strikes = {l.get("strike") for l in legs}
    types = {l.get("type") for l in legs}
    sides = {l.get("side") for l in legs}
    if len({(l.get("side"), l.get("type"), l.get("strike"), str(l.get("expiry")))
            for l in legs}) == 1:
        # the same contract booked more than once: a leg closed in partial slices
        return "single"
    if len(types) > 1:
        return "custom"
    if len(expiries) == 1 and len(strikes) > 1:
        return "vertical"
    if len(strikes) == 1 and len(expiries) > 1:
        return "calendar"
    if len(strikes) > 1 and len(expiries) > 1:
        return "diagonal"
    if len(sides) > 1:
        return "roll"
    return "custom"


def realised_leg_metrics(leg: dict) -> dict:
    side = leg.get("side", "Long")
    sign = 1.0 if side == "Long" else -1.0
    q = float(leg.get("contracts") or 0)
    k = float(leg.get("multiplier") or 100)
    pin = float(leg.get("price_in") or 0)
    pout = float(leg.get("price_out") or 0)
    pl = sign * (pout - pin) * q * k
    pct = (sign * (pout - pin) / pin) if pin else None
    basis = abs(pin * q * k)
    return {
        "id": leg.get("id"),
        "side": side,
        "type": leg.get("type", "CALL"),
        "strike": leg.get("strike"),
        "expiry": leg.get("expiry"),
        "contracts": q,
        "multiplier": k,
        "price_in": pin,
        "price_out": pout,
        "pl": round(pl, 2),
        "pl_pct": None if pct is None else round(pct, 4),
        "cost_basis": round(basis, 2),
        "win_score": 1 if pl > 0 else (-1 if pl < 0 else 0),
        "entry_date": leg.get("entry_date"),
        "exit_date": leg.get("exit_date"),
        "notes": leg.get("notes", ""),
        "label": _leg_label(leg),
    }


def _leg_label(leg: dict) -> str:
    side = str(leg.get("side", "")).upper()[:1] or "?"
    strike = leg.get("strike")
    strike_txt = f"{float(strike):g}" if isinstance(strike, (int, float)) else str(strike or "")
    expiry = parse_date(leg.get("expiry"))
    expiry_txt = expiry.strftime("%d-%b-%y") if expiry else "—"
    return f"{side} {strike_txt}{str(leg.get('type', ''))[:1]} {expiry_txt} x{leg.get('contracts') or 0:g}"


def trade_metrics(trade: dict, rule: str = "third_friday") -> dict:
    """Aggregate a closed trade.

    The trade-level P/L (`pl`) is the sum of its legs -- this is what the month
    grid books.  The KPI block is a different question: it counts one closed
    leg as one trade (see realised_kpis).  Each leg also carries its own `days`
    span (the sheet's M column), falling back to the trade's dates, so the
    per-leg statistics can average legs rather than positions.
    """
    legs = [realised_leg_metrics(l) for l in (trade.get("legs") or [])]
    commission = float(trade.get("commission") or 0)
    pl = round(sum(l["pl"] for l in legs), 2)
    basis = round(sum(l["cost_basis"] for l in legs), 2)
    pct = round(pl / basis, 4) if basis else None

    entry = parse_date(trade.get("entry_date"))
    exit_ = parse_date(trade.get("exit_date"))
    days = (exit_ - entry).days if entry and exit_ else None

    # a leg's own span, with the trade's dates as the fallback (the sheet's =L-K)
    for leg in legs:
        leg_entry = parse_date(leg.get("entry_date")) or entry
        leg_exit = parse_date(leg.get("exit_date")) or exit_
        leg["days"] = (leg_exit - leg_entry).days if leg_entry and leg_exit else 0

    book = booking_period(exit_ or entry, rule)

    structure = (trade.get("structure") or "").lower()
    if structure not in STRUCTURE_LABELS:
        structure = derive_structure(trade.get("legs") or [])

    return {
        "id": trade.get("id"),
        "trade_no": trade.get("trade_no"),
        "ticker": trade.get("ticker"),
        "company": trade.get("company") or trade.get("ticker"),
        "structure": structure,
        "structure_label": STRUCTURE_LABELS[structure],
        "entry_date": entry.isoformat() if entry else None,
        "exit_date": exit_.isoformat() if exit_ else None,
        "commission": round(commission, 2),
        "notes": trade.get("notes", ""),
        "legs": legs,
        "leg_count": len(legs),
        "legs_label": " · ".join(l["label"] for l in legs),
        "pl": pl,
        "pl_net": round(pl - commission, 2),
        "pl_pct": pct,
        "cost_basis": basis,
        "days": days,
        "win_score": 1 if pl > 0 else (-1 if pl < 0 else 0),
        "book_month": month_key(*book) if book else None,
        "currency_exposure": basis,
    }


def realised_kpis(trades: list[dict]) -> dict:
    """Realised statistics -- one closed leg is one trade.

    The sheet's Realised tab has one row per leg and its KPI block reads those
    rows (COUNTIF/SUMIF over columns O and P), so a spread that wins on one leg
    and loses on the other books one win *and* one loss, and the gross wins $
    and losses $ are the sums of the leg rows, not the netted position totals.
    Everything downstream -- win/loss %, R multiple, Kelly -- is computed from
    those units, which keeps the dashboard identical to the workbook.  The
    trade-level P/L that the month grid books is a separate calculation
    (trade_metrics); grouping legs into positions never changes the net total.
    """
    units = [leg for t in trades for leg in (t.get("legs") or [])]
    pls = [round(float(leg.get("pl") or 0), 2) for leg in units]
    n = len(pls)
    wins = [p for p in pls if p > 0]
    losses = [p for p in pls if p < 0]
    flat = [p for p in pls if p == 0]
    win_rate = (len(wins) / n) if n else None
    loss_rate = (len(losses) / n) if n else None
    win_dollars = sum(wins)
    loss_dollars = sum(losses)
    r_multiple = (win_dollars / abs(loss_dollars)) if loss_dollars else None
    kelly = None
    if win_rate is not None and r_multiple:
        kelly = win_rate - (1 - win_rate) / r_multiple
    days = [leg["days"] for leg in units if leg.get("days") is not None]
    structures: dict[str, int] = {}
    for t in trades:
        structures[t.get("structure", "single")] = structures.get(t.get("structure", "single"), 0) + 1
    return {
        "closed_trades": n,        # one closed leg = one trade (the sheet's unit)
        "legs": n,
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "win_rate": None if win_rate is None else round(win_rate, 4),
        "loss_rate": None if loss_rate is None else round(loss_rate, 4),
        "win_dollars": round(win_dollars, 2),
        "loss_dollars": round(loss_dollars, 2),
        "r_multiple": None if r_multiple is None else round(r_multiple, 3),
        "kelly": None if kelly is None else round(kelly, 4),
        "closed_pl": round(win_dollars + loss_dollars, 2),
        "commission": round(sum(t.get("commission") or 0 for t in trades), 2),
        "avg_days": round(sum(days) / len(days), 2) if days else None,
        "avg_win": round(win_dollars / len(wins), 2) if wins else None,
        "avg_loss": round(loss_dollars / len(losses), 2) if losses else None,
        "structures": structures,
    }


# --------------------------------------------------------------------------- #
# the monthly projection grid (spreadsheet columns U:AE)
# --------------------------------------------------------------------------- #

def build_month_view(state: dict, as_of: date | None = None) -> dict:
    """Booked (worst case) / actual / swing per month, auto-generated.

    Months come from:
      * the month the book is currently booking into (always present),
      * every live leg's booking month,
      * every realised trade's booking month,
      * any months the user pinned in meta.month_extras,
    and the span is then filled so that every month between the first and the
    last is a column -- an empty December between a November and a February book
    is still a month the reader is working through. Anything before the current
    booking month is dropped automatically -- this is what retires Sep/Oct style
    columns as the calendar turns.

    The floor is the *booking* month, not the calendar month: past the third
    Friday that month's expiry has already passed and every date now rolls into
    the next month (booking_period), so keeping it as a column showed a closed
    month as if it could still book. On 2026-11-22 the first column is
    December 2026, not November 2026.
    """
    meta = state.get("meta", {})
    rule = meta.get("booking_rule") or "third_friday"
    as_of = as_of or parse_date(meta.get("as_of")) or today()

    positions = [position_metrics(p, rule, as_of) for p in state.get("positions", [])]
    trades = [trade_metrics(t, rule) for t in state.get("realised", [])]

    current_key = month_key(*current_booking_month(as_of, rule))
    keys: set[str] = {current_key}
    for pos in positions:
        for leg in pos["legs"]:
            if leg["contracts"] and leg["effective_book_month"]:
                keys.add(leg["effective_book_month"])
    for t in trades:
        if t["book_month"]:
            keys.add(t["book_month"])
    for extra in meta.get("month_extras") or []:
        try:
            parse_month_key(extra)
            keys.add(extra)
        except (ValueError, TypeError):
            continue
    keys = {k for k in keys if k >= current_key}
    if not keys:
        keys = {current_key}
    months = contiguous_months(sorted(keys))

    realised_all_time = sum(t["pl"] for t in trades)
    commission_all_time = sum(t["commission"] for t in trades)
    open_pl = sum(p["position_pl"] or 0 for p in positions)
    unpriced = any(p["position_pl"] is None for p in positions)

    # realised booked into each month, using the third-Friday cutoff on exit date
    realised_by_month: dict[str, list[dict]] = {k: [] for k in months}
    for t in trades:
        if t["book_month"] in realised_by_month:
            realised_by_month[t["book_month"]].append(t)

    rows_by_month = []
    cumulative = realised_all_time
    for key in months:
        booked_legs, actual_legs = [], []
        company_map: dict[str, dict] = {}
        for pos in positions:
            c = company_map.setdefault(pos["id"], {
                "position_id": pos["id"], "ticker": pos["ticker"],
                "category": pos["category"], "bias": pos["bias"],
                "booked": 0.0, "actual": 0.0, "legs": [],
            })
            for leg in pos["legs"]:
                if not leg["contracts"]:
                    continue
                entry = {
                    "position_id": pos["id"], "leg_id": leg["id"], "ticker": pos["ticker"],
                    "side": leg["side"], "type": leg["type"], "strike": leg["strike"],
                    "expiry": leg["expiry"], "contracts": leg["contracts"],
                    "position_pl": leg["position_pl"],
                    "book_month": leg["effective_book_month"],
                    "overridden": bool(leg.get("book_month_override")),
                }
                if leg["effective_book_month"] == key:
                    booked_legs.append({**entry, "booked": leg["booked_amount"]})
                    c["booked"] = round(c["booked"] + leg["booked_amount"], 2)
                    c["legs"].append(entry)
                if leg["position_pl"] is not None:
                    c["actual"] = round(c["actual"] + leg["position_pl"], 2)
                actual_legs.append(entry)

        realised_month = realised_by_month.get(key, [])
        realised_pl = round(sum(t["pl"] for t in realised_month), 2)
        booked_legs_total = round(sum(l["booked"] for l in booked_legs), 2)
        known = round(realised_pl + booked_legs_total, 2)
        actual = None if unpriced else round(realised_pl + open_pl, 2)
        swing = None if actual is None else round(actual - known, 2)
        cumulative = round(cumulative + booked_legs_total, 2)

        companies = []
        for c in company_map.values():
            if not c["legs"] and not c["booked"]:
                continue
            companies.append({
                **c,
                "actual": None if c["actual"] is None else round(c["actual"], 2),
                "swing": None if c["actual"] is None else round(c["actual"] - c["booked"], 2),
            })

        rows_by_month.append({
            "key": key,
            "label": month_label(key),
            "short_label": month_label(key, with_year=False),
            "year": int(key[:4]),
            "realised_pl": realised_pl,
            "realised_trades": len(realised_month),
            "realised_detail": [{
                "id": t["id"], "trade_no": t["trade_no"], "ticker": t["ticker"],
                "structure": t["structure"], "leg_count": t["leg_count"],
                "legs_label": t["legs_label"], "pl": t["pl"], "exit_date": t["exit_date"],
            } for t in realised_month],
            "booked_legs": booked_legs,
            "booked": known,                     # "Known / worse case"
            "known": known,
            "actual": actual,
            "swing": swing,
            "cumulative_booked": cumulative,
            "cumulative_balance": round(cumulative - commission_all_time, 2),
            "companies": companies,
            "third_friday": third_friday(int(key[:4]), int(key[5:7])).isoformat(),
        })

    return {
        "as_of": as_of.isoformat(),
        "rule": rule,
        "months": rows_by_month,
        "realised_all_time": round(realised_all_time, 2),
        "commission_all_time": round(commission_all_time, 2),
        "open_pl": None if unpriced else round(open_pl, 2),
        "unpriced": unpriced,
        "totals": {
            "booked_current_month": rows_by_month[0]["booked"] if rows_by_month else 0.0,
            "swing_current_month": rows_by_month[0]["swing"] if rows_by_month else None,
        },
    }


# --------------------------------------------------------------------------- #
# portfolio summary / bucket matrix
# --------------------------------------------------------------------------- #

def build_summary(state: dict, as_of: date | None = None) -> dict:
    meta = state.get("meta", {})
    rule = meta.get("booking_rule") or "third_friday"
    as_of = as_of or parse_date(meta.get("as_of")) or today()

    positions = [position_metrics(p, rule, as_of) for p in state.get("positions", [])]
    trades = [trade_metrics(t, rule) for t in state.get("realised", [])]
    kpis = realised_kpis(trades)
    month_view = build_month_view(state, as_of)

    realised_all = kpis["closed_pl"]
    commission = kpis["commission"]
    unpriced = any(p["position_pl"] is None for p in positions)
    unrealised = None if unpriced else round(sum(p["position_pl"] or 0 for p in positions), 2)
    opening = float(meta.get("opening_balance") or 0)
    account_balance = round(opening + realised_all - commission + (unrealised or 0), 2)

    categories = list(state.get("categories") or [])
    for p in positions:
        if p["category"] not in categories:
            categories.append(p["category"])

    matrix = []
    for cat in categories:
        rows = [p for p in positions if p["category"] == cat]
        long_mv = sum(p["long_mv"] for p in rows)
        short_mv = sum(p["short_mv"] for p in rows)
        pl = sum(p["position_pl"] or 0 for p in rows)
        matrix.append({
            "category": cat,
            "tickers": [p["ticker"] for p in rows],
            "long_mv": round(long_mv, 2),
            "short_mv": round(short_mv, 2),
            "net_mv": round(long_mv + short_mv, 2),
            "position_pl": round(pl, 2),
            "exposure": round(sum(abs(p["total_cost"]) for p in rows), 2),
        })

    current = month_view["months"][0] if month_view["months"] else None
    return {
        "as_of": as_of.isoformat(),
        "rule": rule,
        "opening_balance": opening,
        "realised_all_time": realised_all,
        "realised_this_month": current["realised_pl"] if current else 0.0,
        "commission": commission,
        "unrealised": unrealised,
        "account_balance": account_balance,
        "booked_this_month": current["booked"] if current else 0.0,
        "swing_this_month": current["swing"] if current else None,
        "exposure": round(sum(abs(p["total_cost"]) for p in positions), 2),
        "gross_market_value": round(sum(p["market_value"] or 0 for p in positions), 2),
        "long_market_value": round(sum(p["long_mv"] for p in positions), 2),
        "short_market_value": round(sum(p["short_mv"] for p in positions), 2),
        "kpis": kpis,
        "by_category": matrix,
        "warnings": [w for p in positions for w in p["warnings"]],
        "unpriced": unpriced,
        "counts": {"positions": len(positions), "legs": sum(len(p["legs"]) for p in positions),
                   "realised": len(trades),
                   "watchlist": len(state.get("watchlist") or [])},
    }


def watchlist_groups(state: dict) -> list[dict]:
    items = list(state.get("watchlist") or [])
    order = []
    for w in items:
        if w["sector"] not in order:
            order.append(w["sector"])
    groups = []
    for sector in order:
        rows = [w for w in items if w["sector"] == sector]
        groups.append({
            "sector": sector,
            "long": [w for w in rows if w["side"] == "LONG"],
            "short": [w for w in rows if w["side"] == "SHORT"],
            "count": len(rows),
            "updated_at": max((w["updated_at"] for w in rows), default=None),
        })
    return groups


# --------------------------------------------------------------------------- #
# full read model handed to the UI / harness
# --------------------------------------------------------------------------- #

def build_view(state: dict, as_of: date | None = None) -> dict:
    meta = state.get("meta", {})
    rule = meta.get("booking_rule") or "third_friday"
    as_of = as_of or parse_date(meta.get("as_of")) or today()
    positions = [position_metrics(p, rule, as_of) for p in state.get("positions", [])]
    trades = sorted(
        [trade_metrics(t, rule) for t in state.get("realised", [])],
        key=lambda t: (t.get("exit_date") or "", t.get("entry_date") or "", t.get("trade_no") or 0),
        reverse=True,
    )
    return {
        "meta": {**meta, "as_of": as_of.isoformat(), "effective_rule": rule},
        "categories": state.get("categories", []),
        "summary": build_summary(state, as_of),
        "positions": positions,
        "realised": trades,
        "kpis": realised_kpis(trades),
        "months": build_month_view(state, as_of),
        "watchlist": state.get("watchlist", []),
        "watchlist_groups": watchlist_groups(state),
        "audit": state.get("audit", [])[-40:],
    }
