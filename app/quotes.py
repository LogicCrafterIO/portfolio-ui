"""Live-ish market data via yfinance.

Delayed quotes (~15 min, free) are good enough for a pre-session book and keep
the app dependency-light. Every leg gets a `quote` block recording exactly when
and from where the number came, so a stale or missing price is always visible
instead of being silently smoothed away.

Failure policy: a ticker that fails leaves the previous quote and recorded
numbers untouched, and the error is returned in the report. Nothing is invented.

The absent-book policy is the part worth knowing before reading a sheet. Yahoo
returns `bid: 0.0, ask: 0.0` for *every* contract whenever no session is running
-- measured 2026-11-06 07:30 ET, 0 of SPY's 162 nearest-dated calls carried a bid
while `lastPrice` was populated on all of them. A 0/0 row is therefore not a
quote at zero, it is the absence of a quote, and it must never reach the book:
that is what used to put a spread of 0.00/0.00 (and a midpoint of 0.00) into the
pre session tab of the export. So each leg keeps the last two-sided book it
actually saw in `leg["book"]`, stamped with the time it was taken, and a pull
that finds no live book carries that one forward with `status: carried-book`.
A leg that has never had a book keeps `bid`/`ask` empty and prices off `last`
instead, which the sheet shows as a midpoint with no spread rather than a lie.
"""

from __future__ import annotations

import logging
import warnings
from datetime import date, datetime, timezone

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)

SOURCE = "yfinance (delayed ~15m)"


def _parse(iso: str):
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value):
    try:
        if value is None:
            return None
        fv = float(value)
        if fv != fv:  # NaN
            return None
        return fv
    except (TypeError, ValueError):
        return None


def _last_price(ticker) -> tuple[float | None, str]:
    try:
        price = _f(getattr(ticker.fast_info, "last_price", None))
        if price:
            return price, "fast_info.last_price"
    except Exception:  # noqa: BLE001 - yfinance raises a zoo of exceptions
        pass
    try:
        info = ticker.info or {}
        for key in ("regularMarketPrice", "currentPrice", "previousClose"):
            price = _f(info.get(key))
            if price:
                return price, f"info.{key}"
    except Exception:  # noqa: BLE001
        pass
    try:
        hist = ticker.history(period="5d")
        if len(hist):
            price = _f(hist["Close"].iloc[-1])
            if price:
                return price, "history close"
    except Exception:  # noqa: BLE001
        pass
    return None, "unavailable"


def _match_strike(df, strike: float):
    if df is None or not len(df) or strike is None:
        return None, None
    strikes = df["strike"].tolist()
    best, best_diff = None, None
    for value in strikes:
        diff = abs(float(value) - float(strike))
        if best_diff is None or diff < best_diff:
            best, best_diff = float(value), diff
    if best is None:
        return None, None
    if best_diff <= max(0.005, abs(float(strike)) * 0.0025):
        row = df[df["strike"] == best].iloc[0]
        return row, best
    return None, best


def _book(bid, ask) -> dict | None:
    """The two-sided book in a chain row, or None when there is no book at all.

    Yahoo reports `0.0 / 0.0` on every contract outside a trading session (see the
    module docstring), so the pair is accepted only when at least one side is real.
    A row with one genuine zero -- `0.00 / 0.15`, a thin far-dated chain -- is still a
    quote and still averages to 0.075, exactly what the sheet's `N = (M+O)/2` does.
    """
    bid, ask = _f(bid), _f(ask)
    if bid is None or ask is None:
        return None
    if bid <= 0 and ask <= 0:
        return None
    return {"bid": bid, "ask": ask}


def _carried(leg: dict, contract: str | None = None) -> dict | None:
    """The last real book this leg saw, if it still belongs to the same contract.

    `leg["book"]` is written whenever a pull finds a real book, so a pre-market or
    weekend pull can carry real numbers forward instead of 0.00. The contract symbol
    is checked where both sides know it: an edited strike or expiry must not inherit
    the book of the leg it used to be.
    """
    book = leg.get("book") or {}
    pair = _book(book.get("bid"), book.get("ask"))
    if pair is None:
        return None
    if contract and book.get("contract") and contract != book["contract"]:
        return None
    pair["mid"] = _f(book.get("mid")) or round((pair["bid"] + pair["ask"]) / 2.0, 4)
    pair["ts"] = book.get("ts")
    pair["source"] = book.get("source") or SOURCE
    return pair


def _quote_from_row(row: dict) -> dict:
    bid = _f(row.get("bid"))
    ask = _f(row.get("ask"))
    last = _f(row.get("lastPrice"))
    mid = None
    status = "missing"
    if bid is not None and ask is not None:
        # Always the plain average -- the spreadsheet's N = (M+O)/2. A zero bid
        # is still a real quote (0.00 / 0.15 -> 0.075), not a missing side.
        mid = round((bid + ask) / 2.0, 4)
        if bid > 0 and ask > 0:
            status = "ok"
        elif bid == 0 and ask > 0:
            status = "zero-bid"   # thin chain: midpoint is still (0 + ask) / 2
        elif ask == 0 and bid > 0:
            status = "zero-ask"
        else:
            mid = None            # both sides zero: no usable quote
    elif ask is not None and ask > 0:
        mid = round(ask, 4)
        status = "ask-only"       # the other side of the book is absent entirely
    elif bid is not None and bid > 0:
        mid = round(bid, 4)
        status = "bid-only"
    if mid is None:
        if last:
            mid = round(last, 4)
            status = "last-only"
        else:
            status = "missing"
    return {
        "bid": bid,
        "ask": ask,
        "last": last,
        "mid": mid,
        "implied_vol": _f(row.get("impliedVolatility")),
        "volume": _f(row.get("volume")),
        "open_interest": _f(row.get("openInterest")),
        "contract_symbol": row.get("contractSymbol"),
        "status": status,
        "source": SOURCE,
        "ts": _now(),
    }


def refresh(state: dict, tickers: list[str] | None = None, include_earnings: bool = True) -> dict:
    """Pull quotes into state in place. Returns a report dict."""
    import yfinance as yf

    wanted = {t.strip().upper() for t in (tickers or []) if t and t.strip()}
    positions = state.get("positions", [])
    targets = [p for p in positions if not wanted or p["ticker"] in wanted]

    report = {"ts": _now(), "source": SOURCE, "positions": [], "errors": [], "skipped": []}

    for pos in targets:
        ticker_symbol = pos["ticker"]
        entry = {"ticker": ticker_symbol, "underlying": None, "legs": [], "errors": []}
        try:
            ticker = yf.Ticker(ticker_symbol)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"ticker": ticker_symbol, "error": f"ticker init failed: {exc}"})
            continue

        price, price_src = _last_price(ticker)
        if price:
            pos["underlying_price"] = round(price, 4)
            pos["underlying_quote"] = {"price": round(price, 4), "field": price_src,
                                       "source": SOURCE, "ts": _now()}
            entry["underlying"] = round(price, 4)
        else:
            entry["errors"].append("underlying price unavailable")
            report["errors"].append({"ticker": ticker_symbol, "error": "underlying price unavailable"})

        if include_earnings:
            try:
                cal = ticker.calendar or {}
                dates = cal.get("Earnings Date") or []
                if dates:
                    d = dates[0]
                    iso = d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
                    # yfinance sometimes hands back a stale/past date; keep the manual text then
                    if _parse(iso) and _parse(iso) >= date.today():
                        pos["earnings_date"] = iso
                        pos["next_earnings"] = _pretty_date(iso)
            except Exception:  # noqa: BLE001 - earnings calendar is a bonus
                pass

        chains: dict[str, object] = {}
        for leg in pos.get("legs", []):
            if not leg.get("contracts") or not leg.get("expiry") or leg.get("strike") in (None, ""):
                report["skipped"].append({"ticker": ticker_symbol, "leg": leg.get("id"), "reason": "incomplete leg"})
                continue
            expiry = str(leg["expiry"])[:10]
            if expiry not in chains:
                try:
                    chains[expiry] = ticker.option_chain(expiry)
                except Exception as exc:  # noqa: BLE001
                    chains[expiry] = None
                    msg = f"no option chain for {expiry}: {exc}"
                    entry["errors"].append(msg)
                    report["errors"].append({"ticker": ticker_symbol, "error": msg})
            chain = chains[expiry]
            if chain is None:
                leg.setdefault("quote", {})
                leg["quote"] = {**(leg.get("quote") or {}), "status": "missing",
                                "source": SOURCE, "ts": _now()}
                continue
            df = chain.calls if leg.get("type") == "CALL" else chain.puts
            row, nearest = _match_strike(df, leg.get("strike"))
            if row is None:
                quote = {**(leg.get("quote") or {}), "status": "strike-not-found", "source": SOURCE,
                         "ts": _now(), "nearest_strike": nearest, "mid": None}
                leg["quote"] = quote
                entry["legs"].append({"id": leg.get("id"), "strike": leg.get("strike"),
                                      "status": "strike-not-found", "nearest_strike": nearest})
                entry["errors"].append(
                    f"no {leg.get('type')} at strike {leg.get('strike')} for {expiry}"
                    + (f" (nearest {nearest})" if nearest else ""))
                continue
            quote = _quote_from_row(row.to_dict())
            pair = _book(quote["bid"], quote["ask"])
            if pair is not None:
                # a real book: record it, and remember it so the next pull outside
                # a session has something true to carry instead of a 0.00/0.00 pair
                leg["bid"], leg["ask"] = pair["bid"], pair["ask"]
                leg["book"] = {"bid": pair["bid"], "ask": pair["ask"], "mid": quote["mid"],
                               "ts": quote["ts"], "contract": quote.get("contract_symbol"),
                               "source": SOURCE}
                quote["book"] = "live"
            else:
                carried = _carried(leg, contract=quote.get("contract_symbol"))
                if carried is not None:
                    # no session running, or a chain Yahoo has priced at 0/0: keep the
                    # last two-sided quote this contract actually showed, and say when
                    # it was taken so the panel stops calling it live
                    leg["bid"], leg["ask"] = carried["bid"], carried["ask"]
                    quote.update({"bid": carried["bid"], "ask": carried["ask"],
                                  "mid": carried["mid"], "status": "carried-book",
                                  "book_ts": carried["ts"], "book_source": carried["source"],
                                  "book": "carried"})
                else:
                    # never had a book: a 0/0 row is not a quote. Price off the last
                    # trade and leave the spread empty rather than inventing one.
                    leg["bid"], leg["ask"] = None, None
                    quote.update({"bid": None, "ask": None, "book": "none"})
            leg["quote"] = quote
            entry["legs"].append({"id": leg.get("id"), "strike": leg.get("strike"),
                                  "bid": quote["bid"], "ask": quote["ask"], "mid": quote["mid"],
                                  "status": quote["status"], "book": quote["book"],
                                  "book_ts": quote.get("book_ts")})
        report["positions"].append(entry)

    legs = [leg for entry in report["positions"] for leg in entry["legs"]]
    report["summary"] = {
        "live": sum(1 for leg in legs if leg.get("book") == "live"),
        "carried": sum(1 for leg in legs if leg.get("book") == "carried"),
        "no_book": sum(1 for leg in legs if leg.get("book") == "none"),
        "legs": len(legs),
    }
    return report


def _pretty_date(iso: str) -> str:
    """2026-11-17 -> '17th Nov' (matches the spreadsheet's shorthand)."""
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return iso
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d.day}{suffix} {d.strftime('%b')}"
