"""FastAPI app: REST API + static single-page UI + xlsx export.

Run with:  ./run.sh      (== uvicorn app.main:app --port 8117)

Everything the DeepSeek harness needs is plain JSON over this API, or the
data/portfolio.json file itself. Mutating calls accept ?actor=... (or the
X-Actor header) so writes from the harness are attributed in the audit trail.
"""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import calc, exporter, quotes, store

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="4Pillar options book", version="1.0.0",
              description="Pre-session / realised / actionable-watchlist book")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1", "http://localhost"],
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _actor(request: Request, actor: str | None) -> str:
    header = request.headers.get("x-actor")
    return (actor or header or "ui").strip()[:40]


def _as_of(value: str | None, state: dict) -> date:
    if value:
        parsed = calc.parse_date(value)
        if parsed is None:
            raise HTTPException(400, f"unreadable as_of date: {value!r}")
        return parsed
    return calc.parse_date(state["meta"].get("as_of")) or calc.today()


def _view(state: dict, as_of: date | None = None) -> dict:
    return calc.build_view(state, as_of)


def _ok(state: dict, as_of: date | None = None, **extra) -> dict:
    payload = {"ok": True, "state": _state_for_client(state), "view": _view(state, as_of)}
    payload.update(extra)
    return payload


def _state_for_client(state: dict) -> dict:
    return {
        "meta": state["meta"],
        "categories": state["categories"],
        "positions": state["positions"],
        "realised": state["realised"],
        "watchlist": state["watchlist"],
    }


def _find_position(state: dict, pid: str) -> dict:
    for pos in state["positions"]:
        if pos["id"] == pid:
            return pos
    raise HTTPException(404, f"position {pid} not found")


def _find_leg(pos: dict, lid: str) -> dict:
    for leg in pos["legs"]:
        if leg["id"] == lid:
            return leg
    raise HTTPException(404, f"leg {lid} not found on {pos['ticker']}")


def _merge(target: dict, patch: dict, allowed: set[str]) -> None:
    for key, value in (patch or {}).items():
        if key in allowed:
            target[key] = value


LEG_FIELDS = {"side", "type", "strike", "expiry", "contracts", "multiplier", "cost_premium",
              "bid", "ask", "mid_override", "book_month_override", "opened", "notes"}
POSITION_FIELDS = {"ticker", "company", "category", "bias", "next_earnings", "earnings_date",
                   "notes", "underlying_price"}
TRADE_FIELDS = {"trade_no", "ticker", "company", "structure", "entry_date", "exit_date",
                "commission", "notes"}
REALISED_LEG_FIELDS = {"side", "type", "strike", "expiry", "contracts", "multiplier",
                       "price_in", "price_out", "notes"}
WATCH_FIELDS = {"ticker", "side", "sector", "sub_sector", "comment", "catalyst", "conviction",
                "source", "updated_by"}


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #

@app.get("/api/health")
def health() -> dict:
    state = store.load()
    return {
        "ok": True,
        "store": str(store.STORE_PATH),
        "exists": store.STORE_PATH.exists(),
        "positions": len(state["positions"]),
        "realised": len(state["realised"]),
        "watchlist": len(state["watchlist"]),
        "updated_at": state["meta"].get("updated_at"),
        "server_date": calc.today().isoformat(),
    }


@app.get("/api/state")
def get_state(as_of: str | None = None) -> dict:
    state = store.load()
    return {"ok": True, "state": _state_for_client(state), "view": _view(state, _as_of(as_of, state))}


@app.get("/api/schema")
def get_schema() -> dict:
    """Machine-readable contract for external writers (e.g. the DSH harness)."""
    return {
        "store_file": str(store.STORE_PATH),
        "backups": str(store.BACKUP_DIR),
        "note": ("The JSON store is the source of truth. Any writer may PUT /api/state, or use the "
                 "granular endpoints. booking_rule=third_friday assigns an expiry/exit to the month "
                 "of its own third Friday, or the next month for 4th/5th-Friday dates."),
        "state": {
            "meta": {"opening_balance": "number", "default_multiplier": "int",
                     "booking_rule": "third_friday | calendar", "as_of": "YYYY-MM-DD | null",
                     "month_extras": ["YYYY-MM"]},
            "positions[]": {"id": "str", "ticker": "str", "company": "str", "category": "str",
                            "bias": "Long | Short | auto (auto = derived from the legs)",
                            "next_earnings": "str",
                            "underlying_price": "number", "notes": "str",
                            "legs[]": {"id": "str", "side": "Long | Short", "type": "CALL | PUT",
                                       "strike": "number", "expiry": "YYYY-MM-DD",
                                       "contracts": "number",
                                       "multiplier": "number",
                                       "contracts_initial": "number|null (size the leg opened "
                                                            "at; set by a partial close)",
                                       "cost_premium": "number", "bid": "number|null",
                                       "ask": "number|null", "book_month_override": "YYYY-MM|null",
                                       "quote": "server-managed"}},
            "realised[]": {"id": "str", "trade_no": "int", "ticker": "str", "company": "str",
                           "structure": "single | calendar | vertical | diagonal | roll | custom",
                           "entry_date": "YYYY-MM-DD", "exit_date": "YYYY-MM-DD",
                           "commission": "number", "notes": "str",
                           "legs[]": {"id": "str", "side": "Long | Short",
                                      "type": "CALL | PUT", "strike": "number",
                                      "expiry": "YYYY-MM-DD", "contracts": "number",
                                      "multiplier": "number", "price_in": "number",
                                      "price_out": "number", "notes": "str"}},
            "watchlist[]": {"id": "str", "ticker": "str", "side": "LONG | SHORT", "sector": "str",
                            "sub_sector": "str", "comment": "str", "catalyst": "str",
                            "conviction": "str", "source": "manual | deepseek"},
        },
        "endpoints": {
            "GET /api/state?as_of=YYYY-MM-DD": "full read model",
            "POST /api/state": "replace the whole state (body: state dict)",
            "PATCH /api/meta": "opening balance, as_of, booking rule, month extras",
            "POST|PATCH|DELETE /api/positions[/{id}]": "position CRUD",
            "POST|PATCH|DELETE /api/positions/{id}/legs[/{leg_id}]": "leg CRUD",
            "POST /api/positions/{id}/legs/{leg_id}/close": "book all or some contracts into "
                                                            "Realised {price_out, contracts?"
                                                            " (defaults to all), exit_date?,"
                                                            " commission?, keep_leg?, notes?}",
            "POST|PATCH|DELETE /api/realised[/{id}]": "closed trade CRUD (trade = 1+ legs)",
            "POST /api/realised/merge": "fold 2+ trades into one {ids: [...], keep_id?}",
            "POST|PATCH|DELETE /api/realised/{id}/legs[/{leg_id}]": "legs of a closed trade",
            "POST|PATCH|DELETE /api/watchlist[/{id}]": "watchlist item CRUD",
            "POST /api/watchlist/bulk": "{items:[], mode:merge|replace_sector|replace_all}",
            "POST /api/quotes/refresh": "yfinance quote pull for open legs",
            "GET /api/export/xlsx?as_of=YYYY-MM-DD&refresh=1": "3-tab spreadsheet download, pulls fresh yfinance quotes first (refresh=0 reuses the stored ones)",
            "POST /api/months": "{month:'YYYY-MM'} pin an extra month column",
        },
    }


# --------------------------------------------------------------------------- #
# whole-state write
# --------------------------------------------------------------------------- #

@app.post("/api/state")
def put_state(payload: dict = Body(...), request: Request = None,
              actor: str | None = Query(None), as_of: str | None = None) -> dict:
    body = payload.get("state", payload) if isinstance(payload, dict) else payload
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a state object")
    body = dict(body)
    body.setdefault("meta", {})
    store.log(body, "state.replace", f"{len(body.get('positions') or [])} positions", _actor(request, actor))
    state = store.save(body, actor=_actor(request, actor))
    return _ok(state, as_of)


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #

@app.patch("/api/meta")
def patch_meta(patch: dict = Body(...), request: Request = None,
               actor: str | None = Query(None), as_of: str | None = None) -> dict:
    def apply(state: dict) -> dict:
        meta = state["meta"]
        _merge(meta, patch, {"name", "currency", "opening_balance", "default_multiplier",
                             "booking_rule", "as_of", "month_extras"})
        if meta.get("booking_rule") not in (None, "third_friday", "calendar"):
            raise HTTPException(400, "booking_rule must be 'third_friday' or 'calendar'")
        store.log(state, "meta.patch", str(list(patch.keys())), _actor(request, actor))
        return state
    state = store.mutate(apply, actor=_actor(request, actor))
    return _ok(state, as_of)


@app.post("/api/months")
def add_month(payload: dict = Body(...), request: Request = None,
              actor: str | None = Query(None)) -> dict:
    month = str(payload.get("month") or "").strip()
    try:
        calc.parse_month_key(month)
    except (ValueError, TypeError):
        raise HTTPException(400, "month must be YYYY-MM")

    def apply(state: dict) -> dict:
        extras = state["meta"].setdefault("month_extras", [])
        if month not in extras:
            extras.append(month)
        store.log(state, "month.add", month, _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/months/{month}")
def remove_month(month: str, request: Request = None, actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        extras = state["meta"].get("month_extras") or []
        state["meta"]["month_extras"] = [m for m in extras if m != month]
        store.log(state, "month.remove", month, _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


# --------------------------------------------------------------------------- #
# positions and legs
# --------------------------------------------------------------------------- #

@app.post("/api/positions")
def create_position(payload: dict = Body(...), request: Request = None,
                    actor: str | None = Query(None)) -> dict:
    if not (payload.get("ticker") or "").strip():
        raise HTTPException(400, "ticker is required")

    def apply(state: dict) -> dict:
        pos = store.normalise_position(payload)
        pos["legs"] = [store.normalise_leg(l) for l in (payload.get("legs") or [])]
        state["positions"].append(pos)
        if pos["category"] not in state["categories"]:
            state["categories"].append(pos["category"])
        store.log(state, "position.create", f"{pos['ticker']} ({pos['id']})", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.patch("/api/positions/{pid}")
def patch_position(pid: str, patch: dict = Body(...), request: Request = None,
                   actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        _merge(pos, patch, POSITION_FIELDS)
        state["positions"][state["positions"].index(pos)] = store.normalise_position(pos)
        if pos["category"] not in state["categories"]:
            state["categories"].append(pos["category"])
        store.log(state, "position.patch", f"{pos['ticker']}: {list(patch.keys())}", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/positions/{pid}")
def delete_position(pid: str, request: Request = None, actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        state["positions"] = [p for p in state["positions"] if p["id"] != pid]
        store.log(state, "position.delete", pos["ticker"], _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.post("/api/positions/{pid}/legs")
def create_leg(pid: str, payload: dict = Body(...), request: Request = None,
               actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        leg = store.normalise_leg(payload)
        pos["legs"].append(leg)
        store.log(state, "leg.create", f"{pos['ticker']} {leg['side']} {leg['type']} {leg['strike']}",
                  _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.patch("/api/positions/{pid}/legs/{lid}")
def patch_leg(pid: str, lid: str, patch: dict = Body(...), request: Request = None,
              actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        leg = _find_leg(pos, lid)
        _merge(leg, patch, LEG_FIELDS)
        if patch.get("mid_override") in ("", None) and "mid_override" in patch:
            leg["mid_override"] = None
        idx = pos["legs"].index(leg)
        pos["legs"][idx] = store.normalise_leg(leg)
        store.log(state, "leg.patch", f"{pos['ticker']} {lid}: {list(patch.keys())}", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/positions/{pid}/legs/{lid}")
def delete_leg(pid: str, lid: str, request: Request = None,
               actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        leg = _find_leg(pos, lid)
        pos["legs"] = [l for l in pos["legs"] if l["id"] != lid]
        store.log(state, "leg.delete", f"{pos['ticker']} {lid}", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.post("/api/positions/{pid}/legs/{lid}/close")
def close_leg(pid: str, lid: str, payload: dict = Body(...), request: Request = None,
              actor: str | None = Query(None)) -> dict:
    """Book a leg out -- all of it, or a slice of its contracts -- into Realised.

    `contracts` says how many contracts this exit covers. Omit it (or pass the whole
    count) to close the leg; pass fewer to realise that slice and leave the remainder
    open on the book, with `contracts_initial` recording the size it opened at.
    `keep_leg` only applies to a full close: it leaves the row behind with 0 contracts.
    """
    def apply(state: dict) -> dict:
        pos = _find_position(state, pid)
        leg = _find_leg(pos, lid)
        if payload.get("price_out") in (None, ""):
            raise HTTPException(400, "price_out is required to close a leg")
        try:
            plan = calc.leg_close_plan(
                leg, pos,
                price_out=payload.get("price_out"),
                contracts=payload.get("contracts", payload.get("contracts_out")),
                exit_date=payload.get("exit_date"),
                entry_date=payload.get("entry_date"),
                commission=payload.get("commission"),
                notes=payload.get("notes"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        numbers = [t.get("trade_no") or 0 for t in state["realised"]]
        trade = store.normalise_trade({**plan["trade"],
                                       "trade_no": (max(numbers) if numbers else 0) + 1})
        state["realised"].append(trade)

        if plan["remaining"]:
            leg["contracts"] = plan["remaining"]
            leg["contracts_initial"] = plan["held"]
        elif payload.get("keep_leg"):
            leg["contracts"] = 0
            leg["contracts_initial"] = plan["held"]
        else:
            pos["legs"] = [l for l in pos["legs"] if l["id"] != lid]

        what = (f"{plan['closed']} of {plan['held']} contracts, {plan['remaining']} left open"
                if plan["partial"] else "the whole leg")
        store.log(state, "leg.close", f"{pos['ticker']} -> trade {trade['trade_no']}: {what}",
                  _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


# --------------------------------------------------------------------------- #
# realised
# --------------------------------------------------------------------------- #

@app.post("/api/realised")
def create_trade(payload: dict = Body(...), request: Request = None,
                 actor: str | None = Query(None)) -> dict:
    """Create a closed trade: one leg (single) or several (calendar/vertical/diagonal).

    Accepts either {"legs": [...]} or legacy top-level leg fields.
    """
    if not (payload.get("ticker") or "").strip():
        raise HTTPException(400, "ticker is required")
    if not (payload.get("legs") or payload.get("price_in") not in (None, "")):
        raise HTTPException(400, "at least one leg is required")

    def apply(state: dict) -> dict:
        body = dict(payload)
        if body.get("trade_no") in (None, ""):
            numbers = [t.get("trade_no") or 0 for t in state["realised"]]
            body["trade_no"] = (max(numbers) if numbers else 0) + 1

        if not body.get("legs"):
            body = {**body, "legs": [{
                "side": body.get("side"), "type": body.get("type"), "strike": body.get("strike"),
                "expiry": body.get("expiry"), "contracts": body.get("contracts"),
                "multiplier": body.get("multiplier"), "price_in": body.get("price_in"),
                "price_out": body.get("price_out"),
            }]}
        # a calendar/vertical trade may be posted without explicit dates: derive them
        if not body.get("entry_date") or not body.get("exit_date"):
            body["entry_date"] = body.get("entry_date") or calc.today().isoformat()
            body["exit_date"] = body.get("exit_date") or calc.today().isoformat()

        trade = store.normalise_trade(body)
        state["realised"].append(trade)
        store.log(state, "trade.create",
                  f"{trade['ticker']} #{trade['trade_no']} {trade['structure']} "
                  f"({len(trade['legs'])} leg{'s' if len(trade['legs']) != 1 else ''})",
                  _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.patch("/api/realised/{tid}")
def patch_trade(tid: str, patch: dict = Body(...), request: Request = None,
                actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        trade = _find_trade(state, tid)
        _merge(trade, patch, TRADE_FIELDS)
        if "structure" in patch:
            trade["structure_locked"] = True
        idx = state["realised"].index(trade)
        state["realised"][idx] = store.normalise_trade(trade)
        store.log(state, "trade.patch", f"#{trade['trade_no']} {trade['ticker']}: {list(patch.keys())}",
                  _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.post("/api/realised/merge")
def merge_realised(payload: dict = Body(...), request: Request = None,
                   actor: str | None = Query(None)) -> dict:
    """Merge two or more closed trades into one trade (e.g. legs closed separately)."""
    ids = [i for i in (payload.get("ids") or []) if i]
    if len(set(ids)) < 2:
        raise HTTPException(400, "select at least two trades to merge")

    def apply(state: dict) -> dict:
        chosen = [t for t in state["realised"] if t["id"] in set(ids)]
        if len(chosen) != len(set(ids)):
            raise HTTPException(404, "one of the selected trades no longer exists")
        try:
            merged = store.merge_trades(chosen, payload.get("keep_id"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        dropped = {t["id"] for t in chosen if t["id"] != merged["id"]}
        state["realised"] = [t for t in state["realised"] if t["id"] not in dropped]
        index = next(i for i, t in enumerate(state["realised"]) if t["id"] == merged["id"])
        state["realised"][index] = merged
        store.log(state, "trade.merge",
                  f"#{merged['trade_no']} {merged['ticker']} merged {len(chosen)} trades into "
                  f"{merged['structure']} ({len(merged['legs'])} legs)", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


def _find_trade(state: dict, tid: str) -> dict:
    for trade in state["realised"]:
        if trade["id"] == tid:
            return trade
    raise HTTPException(404, f"trade {tid} not found")


def _refresh_structure(trade: dict) -> None:
    if not trade.get("structure_locked"):
        trade["structure"] = store.derive_structure(trade.get("legs") or [])


@app.post("/api/realised/{tid}/legs")
def create_trade_leg(tid: str, payload: dict = Body(...), request: Request = None,
                     actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        trade = _find_trade(state, tid)
        leg = store.normalise_realised_leg(payload)
        trade.setdefault("legs", []).append(leg)
        if not payload.get("structure"):
            trade["structure"] = store.derive_structure(trade["legs"])
        store.log(state, "trade.leg.create", f"#{trade['trade_no']} {trade['ticker']} + {leg['side']} "
                  f"{leg['type']} {leg['strike']} {leg['expiry']}", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.patch("/api/realised/{tid}/legs/{lid}")
def patch_trade_leg(tid: str, lid: str, patch: dict = Body(...), request: Request = None,
                    actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        trade = _find_trade(state, tid)
        for idx, leg in enumerate(trade.get("legs") or []):
            if leg["id"] == lid:
                merged = {**leg, **{k: v for k, v in (patch or {}).items() if k in REALISED_LEG_FIELDS}}
                trade["legs"][idx] = store.normalise_realised_leg(merged)
                if not patch.get("structure"):
                    trade["structure"] = store.derive_structure(trade["legs"])
                store.log(state, "trade.leg.patch", f"#{trade['trade_no']} {trade['ticker']} "
                          f"{lid}: {list(patch.keys())}", _actor(request, actor))
                return state
        raise HTTPException(404, f"leg {lid} not found on trade {trade.get('trade_no')}")
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/realised/{tid}/legs/{lid}")
def delete_trade_leg(tid: str, lid: str, request: Request = None,
                     actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        trade = _find_trade(state, tid)
        before = len(trade.get("legs") or [])
        trade["legs"] = [l for l in (trade.get("legs") or []) if l["id"] != lid]
        if len(trade["legs"]) == before:
            raise HTTPException(404, f"leg {lid} not found on trade {trade.get('trade_no')}")
        if not trade["legs"]:
            raise HTTPException(400, "a trade needs at least one leg — delete the trade instead")
        trade["structure"] = store.derive_structure(trade["legs"])
        store.log(state, "trade.leg.delete", f"#{trade['trade_no']} {trade['ticker']} {lid}",
                  _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/realised/{tid}")
def delete_trade(tid: str, request: Request = None, actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        before = len(state["realised"])
        state["realised"] = [t for t in state["realised"] if t["id"] != tid]
        if len(state["realised"]) == before:
            raise HTTPException(404, f"trade {tid} not found")
        store.log(state, "trade.delete", tid, _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


# --------------------------------------------------------------------------- #
# watchlist
# --------------------------------------------------------------------------- #

@app.post("/api/watchlist")
def create_watch(payload: dict = Body(...), request: Request = None,
                 actor: str | None = Query(None)) -> dict:
    if not (payload.get("ticker") or "").strip():
        raise HTTPException(400, "ticker is required")

    def apply(state: dict) -> dict:
        body = dict(payload)
        body.setdefault("updated_by", _actor(request, actor))
        item = store.normalise_watch(body)
        state["watchlist"].append(item)
        store.log(state, "watch.create", f"{item['side']} {item['ticker']}", _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.patch("/api/watchlist/{wid}")
def patch_watch(wid: str, patch: dict = Body(...), request: Request = None,
                actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        for idx, item in enumerate(state["watchlist"]):
            if item["id"] == wid:
                _merge(item, patch, WATCH_FIELDS)
                item["updated_at"] = store.utcnow()
                item["updated_by"] = patch.get("updated_by") or _actor(request, actor)
                state["watchlist"][idx] = store.normalise_watch(item)
                store.log(state, "watch.patch", f"{item['ticker']}: {list(patch.keys())}",
                          _actor(request, actor))
                return state
        raise HTTPException(404, f"watchlist item {wid} not found")
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.delete("/api/watchlist/{wid}")
def delete_watch(wid: str, request: Request = None, actor: str | None = Query(None)) -> dict:
    def apply(state: dict) -> dict:
        before = len(state["watchlist"])
        state["watchlist"] = [w for w in state["watchlist"] if w["id"] != wid]
        if len(state["watchlist"]) == before:
            raise HTTPException(404, f"watchlist item {wid} not found")
        store.log(state, "watch.delete", wid, _actor(request, actor))
        return state
    return _ok(store.mutate(apply, actor=_actor(request, actor)))


@app.post("/api/watchlist/bulk")
def bulk_watchlist(payload: dict = Body(...), request: Request = None,
                   actor: str | None = Query(None)) -> dict:
    """Bulk upsert used by the DeepSeek harness.

    mode=merge          add or update matching (ticker, side) rows
    mode=replace_sector wipe each sector present in the payload, then insert
    mode=replace_all    wipe the whole watchlist, then insert
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    mode = payload.get("mode") or "merge"
    if mode not in ("merge", "replace_sector", "replace_all"):
        raise HTTPException(400, "mode must be merge | replace_sector | replace_all")
    who = payload.get("updated_by") or _actor(request, actor)

    def apply(state: dict) -> dict:
        incoming = []
        for raw in items:
            raw = dict(raw)
            raw.setdefault("source", "deepseek")
            raw.setdefault("updated_by", who)
            raw["updated_at"] = store.utcnow()
            incoming.append(store.normalise_watch(raw))

        if mode == "replace_all":
            state["watchlist"] = []
        elif mode == "replace_sector":
            sectors = {w["sector"] for w in incoming}
            state["watchlist"] = [w for w in state["watchlist"] if w["sector"] not in sectors]

        by_key = {(w["ticker"], w["side"]): w for w in state["watchlist"]}
        for item in incoming:
            existing = by_key.get((item["ticker"], item["side"]))
            if existing:
                existing.update({k: v for k, v in item.items() if k != "id"})
                existing["updated_at"] = store.utcnow()
                existing["updated_by"] = who
            else:
                state["watchlist"].append(item)
                by_key[(item["ticker"], item["side"])] = item
        store.log(state, f"watch.bulk.{mode}", f"{len(incoming)} items by {who}", who)
        return state
    return _ok(store.mutate(apply, actor=who))


# --------------------------------------------------------------------------- #
# quotes + export
# --------------------------------------------------------------------------- #

@app.post("/api/quotes/refresh")
def refresh_quotes(payload: dict | None = Body(None), request: Request = None,
                   actor: str | None = Query(None)) -> dict:
    payload = payload or {}
    tickers = payload.get("tickers") or None
    include_earnings = bool(payload.get("include_earnings", True))
    report: dict[str, Any] = {}

    def apply(state: dict) -> dict:
        nonlocal report
        report = quotes.refresh(state, tickers=tickers, include_earnings=include_earnings)
        store.log(state, "quotes.refresh",
                  f"{len(report.get('positions', []))} tickers, {len(report.get('errors', []))} errors",
                  _actor(request, actor))
        return state
    state = store.mutate(apply, actor=_actor(request, actor))
    return _ok(state, report=report)


@app.get("/api/export/xlsx")
def export_xlsx(as_of: str | None = None, refresh: bool = True) -> Response:
    """The workbook, carrying quotes pulled at the moment of the export.

    The pre session tab is a *pre session* sheet, so M/N/O have to be as fresh as the
    click: the export refreshes from yfinance first (`?refresh=0` skips it) and then
    writes whatever the pull could establish -- a live book, the last two-sided book
    the contract showed (carried, stamped on the leg with when it was taken), or the
    last trade as the midpoint when the chain has never offered a spread. A refresh
    that fails for one ticker still produces the workbook; why it failed travels in
    the `X-Quote-Summary` header and in the audit log.
    """
    quote_report: dict[str, Any] | None = None
    if refresh:
        def apply(state: dict) -> dict:
            nonlocal quote_report
            try:
                quote_report = quotes.refresh(state, include_earnings=False)
                summary = quote_report.get("summary", {})
                store.log(state, "quotes.refresh.export",
                          f"{summary.get('live', 0)} live / {summary.get('carried', 0)} carried / "
                          f"{summary.get('no_book', 0)} no book, "
                          f"{len(quote_report.get('errors', []))} errors", "export")
            except Exception as exc:  # noqa: BLE001 - an export must still happen
                quote_report = {"error": f"{type(exc).__name__}: {exc}"}
            return state
        state = store.mutate(apply, actor="export")
    else:
        state = store.load()
    try:
        wb = exporter.build_workbook(state, _as_of(as_of, state))
    except ValueError as exc:
        # the sheet has fixed room for some things (three category buckets);
        # say which, so the button can tell the reader instead of the browser
        # showing a bare "Internal Server Error". `detail` is FastAPI's own
        # error key, which is what the UI's api() helper reads.
        return JSONResponse({"detail": str(exc)}, status_code=409)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    stamp = (calc.parse_date(as_of) or calc.today()).isoformat()
    filename = f"options-book-{stamp}.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"',
               "Access-Control-Expose-Headers": "X-Quote-Summary"}
    if quote_report is not None:
        summary = quote_report.get("summary") or {"error": quote_report.get("error", "")}
        headers["X-Quote-Summary"] = json.dumps(summary)
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# static UI
# --------------------------------------------------------------------------- #

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "static/index.html missing"}, status_code=500)
    return FileResponse(index_file)


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)
