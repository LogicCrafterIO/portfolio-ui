"""Durable JSON store for the options book.

The store is deliberately a single, human-readable JSON file so that the
DeepSeek harness (or any analyst) can read and write it directly:

    data/portfolio.json

Every write is:
  * serialised through a process-local lock,
  * written to a temp file and atomically renamed (no half-written state),
  * preceded by a timestamped copy in data/backups/ (last 40 kept).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STORE_PATH = DATA_DIR / "portfolio.json"
BACKUP_DIR = DATA_DIR / "backups"
MAX_BACKUPS = 40

SCHEMA_VERSION = 2

_lock = threading.RLock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def default_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "name": "4Pillar options book",
            "currency": "USD",
            "opening_balance": 40000.0,
            "default_multiplier": 100,
            "booking_rule": "third_friday",
            "as_of": None,
            "month_extras": [],
            "updated_at": utcnow(),
            "updated_by": "system",
        },
        "categories": ["Hardware", "Software", "Real economy"],
        "positions": [],
        "realised": [],
        "watchlist": [],
        "audit": [],
    }


# --------------------------------------------------------------------------- #
# migration / normalisation
# --------------------------------------------------------------------------- #

def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _int(v, default=None):
    n = _num(v)
    return default if n is None else int(round(n))


def normalise_leg(raw: dict) -> dict:
    leg = dict(raw or {})
    leg["id"] = leg.get("id") or new_id("leg")
    leg["side"] = "Short" if str(leg.get("side", "Long")).strip().lower().startswith("s") else "Long"
    leg["type"] = "PUT" if str(leg.get("type", "CALL")).strip().upper().startswith("P") else "CALL"
    leg["strike"] = _num(leg.get("strike"))
    leg["contracts"] = _int(leg.get("contracts"), 0) or 0
    # contracts the leg opened with; set by a partial close so the panel can show
    # "3 of 6" and the export's contract count still reads as the leg's own size
    leg["contracts_initial"] = _int(leg.get("contracts_initial"))
    leg["multiplier"] = _int(leg.get("multiplier"), 100) or 100
    leg["cost_premium"] = _num(leg.get("cost_premium")) or 0.0
    leg["bid"] = _num(leg.get("bid"))
    leg["ask"] = _num(leg.get("ask"))
    leg["mid_override"] = _num(leg.get("mid_override"))
    leg["expiry"] = (leg.get("expiry") or None)
    leg["opened"] = leg.get("opened") or None
    leg["notes"] = leg.get("notes") or ""
    leg["book_month_override"] = leg.get("book_month_override") or None
    leg["quote"] = leg.get("quote") or None
    return leg


def normalise_position(raw: dict) -> dict:
    pos = dict(raw or {})
    pos["id"] = pos.get("id") or new_id("pos")
    pos["ticker"] = (pos.get("ticker") or "").strip().upper()
    pos["company"] = (pos.get("company") or pos["ticker"]).strip()
    pos["category"] = (pos.get("category") or "Unclassified").strip()
    pos["legs"] = [normalise_leg(leg) for leg in (pos.get("legs") or [])]

    raw_bias = str(pos.get("bias") or "").strip().lower()
    if raw_bias in ("", "auto", "derive", "derived"):
        # the stance follows the legs: a long put is a bearish position
        pos["bias"] = "auto"
        pos["bias_source"] = "auto"
    else:
        pos["bias"] = "Short" if raw_bias.startswith("s") else "Long"
        pos["bias_source"] = "manual"

    pos["next_earnings"] = pos.get("next_earnings") or ""
    pos["earnings_date"] = pos.get("earnings_date") or None
    pos["notes"] = pos.get("notes") or ""
    pos["underlying_price"] = _num(pos.get("underlying_price"))
    pos["underlying_quote"] = pos.get("underlying_quote") or None
    return pos


def normalise_realised_leg(raw: dict) -> dict:
    """One leg of a closed trade: a calendar/vertical has two or more."""
    leg = dict(raw or {})
    leg["id"] = leg.get("id") or new_id("trl")
    leg["side"] = "Short" if str(leg.get("side", "Long")).strip().lower().startswith("s") else "Long"
    leg["type"] = "PUT" if str(leg.get("type", "CALL")).strip().upper().startswith("P") else "CALL"
    leg["strike"] = _num(leg.get("strike"))
    leg["expiry"] = leg.get("expiry") or None
    leg["contracts"] = _int(leg.get("contracts"), 0) or 0
    leg["multiplier"] = _int(leg.get("multiplier"), 100) or 100
    leg["price_in"] = _num(leg.get("price_in")) or 0.0
    leg["price_out"] = _num(leg.get("price_out")) or 0.0
    for key in ("entry_date", "exit_date"):
        # optional: the sheet records when each leg of a calendar actually closed
        leg[key] = str(leg.get(key)).strip() or None if leg.get(key) else None
    leg["notes"] = leg.get("notes") or ""
    return leg


from .calc import derive_bias, derive_structure  # noqa: E402  (pure functions)


def normalise_trade(raw: dict) -> dict:
    """A closed trade: 1 leg (single option) or many (calendar/vertical/diagonal).

    v1 stores held one row per leg with a shared trade_no; those rows are still
    accepted here and folded into a legs[] array (see migrate_flat_trades).
    """
    t = dict(raw or {})
    t["id"] = t.get("id") or new_id("trd")
    t["ticker"] = (t.get("ticker") or "").strip().upper()
    t["company"] = (t.get("company") or t["ticker"]).strip()
    t["commission"] = _num(t.get("commission")) or 0.0
    t["trade_no"] = _int(t.get("trade_no"), None)
    t["notes"] = t.get("notes") or ""

    raw_legs = t.get("legs")
    if raw_legs is None:
        # legacy flat row -> single-leg trade
        raw_legs = [{
            "side": t.get("side"), "type": t.get("type"), "strike": t.get("strike"),
            "expiry": t.get("expiry"), "contracts": t.get("contracts"),
            "multiplier": t.get("multiplier"), "price_in": t.get("price_in"),
            "price_out": t.get("price_out"), "notes": t.get("notes"),
        }]
    t["legs"] = [normalise_realised_leg(l) for l in raw_legs if isinstance(l, dict)]

    for key in ("side", "type", "strike", "expiry", "contracts", "multiplier",
                "price_in", "price_out"):
        t.pop(key, None)

    if not t.get("ticker"):
        t["ticker"] = ""

    structure = (t.get("structure") or "").strip().lower()
    allowed = {"single", "calendar", "vertical", "diagonal", "roll", "custom"}
    t["structure"] = structure if structure in allowed else derive_structure(t["legs"])
    t["entry_date"] = t.get("entry_date") or None
    t["exit_date"] = t.get("exit_date") or None
    return t


def _contract_key(leg: dict) -> tuple:
    return (leg["side"], leg["type"], leg["strike"], leg["expiry"], leg["multiplier"])


def _blend_price(a: float, qa: float, b: float, qb: float) -> float:
    """Contract-weighted average, or the surviving side when the other is unpriced."""
    total = qa + qb
    if total <= 0:
        return a or b
    if not a:
        return b
    if not b:
        return a
    return round((a * qa + b * qb) / total, 6)


def coalesce_legs(legs: list[dict]) -> tuple[list[dict], list[str]]:
    """Fold legs that describe the *same contract* into one row.

    Merging two trades that were the same position closed in pieces (2 of 3, then the
    last 1) produced two legs identical in side, type, strike, expiry and multiplier,
    which the realised tab then showed as two rows of one economic trade. A contract is
    one line in the book: contracts add, and the in/out prices become contract-weighted
    averages, so the leg's P/L is unchanged to the cent. Legs that differ in anything but
    size stay separate -- that is what makes a calendar or a vertical a multi-leg trade.

    Returns (legs, blends) where blends describes, in words, every price that was
    averaged away, so the caller can keep the detail in the trade's notes.
    """
    out: list[dict] = []
    first_by_key: dict[tuple, dict] = {}
    blends: list[str] = []
    for raw in legs or []:
        leg = normalise_realised_leg(raw)
        key = _contract_key(leg)
        first = first_by_key.get(key)
        if first is None:
            first_by_key[key] = leg
            out.append(leg)
            continue

        qa, qb = float(first["contracts"]), float(leg["contracts"])
        before = first["price_out"]
        first["price_in"] = _blend_price(first["price_in"], qa, leg["price_in"], qb)
        first["price_out"] = _blend_price(before, qa, leg["price_out"], qb)
        first["contracts"] = first["contracts"] + leg["contracts"]
        strike = leg.get("strike")
        strike_txt = f"{float(strike):g}" if isinstance(strike, (int, float)) else "?"
        blends.append(
            f"{qb:g} of {float(first['contracts']):g} {leg['side'].upper()[:1]} "
            f"{strike_txt}{leg['type'][:1]} {leg['expiry']} out {float(leg['price_out'] or 0):.2f}"
            f" blended to {float(first['price_out'] or 0):.2f}")
        for field, pick in (("entry_date", min), ("exit_date", max)):
            a, b = first.get(field), leg.get(field)
            if a and b:
                first[field] = pick(a, b)
            elif b:
                first[field] = b
        note = (leg.get("notes") or "").strip()
        if note and note not in (first.get("notes") or ""):
            first["notes"] = "; ".join(x for x in (first.get("notes"), note) if x)
    return out, blends


def merge_trades(trades: list[dict], keep_id: str | None = None) -> dict:
    """Fold two or more closed trades into one (e.g. two legs closed separately).

    Legs are kept in entry order, dates widen to the outer entry/exit, commission sums,
    notes are de-duplicated and the structure is re-derived. Legs of the *same* contract
    are coalesced into one row (see coalesce_legs) so a position closed in pieces reads as
    one line. Raises ValueError when the merge does not make sense (fewer than two trades,
    or more than one ticker).
    """
    trades = [t for t in (trades or []) if isinstance(t, dict)]
    if len(trades) < 2:
        raise ValueError("merging needs at least two trades")
    tickers = sorted({(t.get("ticker") or "").upper() for t in trades})
    if len(tickers) > 1:
        raise ValueError(f"only trades on the same ticker can merge (got {', '.join(tickers)})")

    ordered = sorted(trades, key=lambda t: (t.get("entry_date") or "", t.get("trade_no") or 0))
    keep = next((t for t in ordered if t.get("id") == keep_id), None) or ordered[0]

    legs: list[dict] = []
    for trade in ordered:
        for leg in trade.get("legs") or []:
            legs.append(normalise_realised_leg(leg))
    legs, blends = coalesce_legs(legs)

    entries = [t["entry_date"] for t in trades if t.get("entry_date")]
    exits = [t["exit_date"] for t in trades if t.get("exit_date")]
    notes: list[str] = []
    for trade in ordered:
        note = (trade.get("notes") or "").strip()
        if note and note not in notes:
            notes.append(note)
    if blends:
        # the averaged fills stay legible: one row on screen, the tape in the note
        notes.append("merged fills: " + "; ".join(blends))

    merged = {
        **keep,
        "ticker": (keep.get("ticker") or "").upper(),
        "company": keep.get("company") or keep.get("ticker"),
        "entry_date": min(entries) if entries else None,
        "exit_date": max(exits) if exits else None,
        "commission": round(sum(_num(t.get("commission")) or 0.0 for t in trades), 2),
        "notes": "; ".join(notes),
        "legs": legs,
        "structure": derive_structure(legs),
    }
    merged.pop("structure_locked", None)
    return normalise_trade(merged)


def migrate_flat_trades(rows: list[dict]) -> list[dict]:
    """Fold v1's one-row-per-leg realised list into trade objects, by trade_no."""
    groups: list[dict] = []
    index: dict = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("legs"), list):
            groups.append(row)          # already trade-shaped
            continue
        row = dict(row or {})
        key = row.get("trade_no")
        if key in (None, "") or key not in index:
            group = {
                "trade_no": key,
                "ticker": row.get("ticker"),
                "company": row.get("company"),
                "entry_date": row.get("entry_date"),
                "exit_date": row.get("exit_date"),
                "commission": 0.0,
                "notes": "",
                "legs": [],
            }
            groups.append(group)
            if key not in (None, ""):
                index[key] = group
        else:
            group = index[key]
        group["commission"] = (_num(group.get("commission")) or 0.0) + (_num(row.get("commission")) or 0.0)
        for date_key, pick in (("entry_date", min), ("exit_date", max)):
            values = [v for v in (group.get(date_key), row.get(date_key)) if v]
            group[date_key] = pick(values) if values else None
        if row.get("notes"):
            group["notes"] = (group["notes"] + " " + str(row["notes"])).strip()
        group["legs"].append({
            "side": row.get("side"), "type": row.get("type"), "strike": row.get("strike"),
            "expiry": row.get("expiry"), "contracts": row.get("contracts"),
            "multiplier": row.get("multiplier"), "price_in": row.get("price_in"),
            "price_out": row.get("price_out"),
        })
    return groups


def normalise_watch(raw: dict) -> dict:
    w = dict(raw or {})
    w["id"] = w.get("id") or new_id("wl")
    w["ticker"] = (w.get("ticker") or "").strip().upper()
    side = str(w.get("side") or "LONG").strip().lower()
    w["side"] = "SHORT" if side.startswith("s") else "LONG"
    w["sector"] = (w.get("sector") or "Unclassified").strip()
    w["sub_sector"] = (w.get("sub_sector") or "").strip()
    w["comment"] = w.get("comment") or ""
    w["catalyst"] = w.get("catalyst") or ""
    w["conviction"] = (w.get("conviction") or "").strip()
    offset = w.get("row_offset")
    w["row_offset"] = int(offset) if isinstance(offset, (int, float)) and offset >= 0 else None
    w["source"] = (w.get("source") or "manual").strip()
    w["updated_by"] = w.get("updated_by") or w["source"]
    w["updated_at"] = w.get("updated_at") or utcnow()
    return w


def category_key(label) -> str:
    """Identity of a category label: case and repeated spaces do not count."""
    return " ".join(str(label or "").split()).casefold()


def canonical_category(label, known) -> str:
    """The book's own spelling of `label`, matched case-insensitively.

    `category` is a free-text field in the UI, so "real economy", "Real  Economy"
    and "REAL ECONOMY" are all the bucket the book already has, and the book's
    spelling is the one that gets stored. A name it has never seen is kept as
    typed -- that really is a new bucket, and `pre session` has room for three.
    """
    text = " ".join(str(label or "").split())
    key = text.casefold()
    for candidate in known:
        if category_key(candidate) == key:
            return candidate
    return text


def normalise_state(raw: dict) -> dict:
    state = default_state()
    raw = raw or {}
    base = state["meta"]
    meta = dict(base)
    meta.update({k: v for k, v in (raw.get("meta") or {}).items() if k in {
        "name", "currency", "opening_balance", "default_multiplier", "booking_rule",
        "as_of", "month_extras", "updated_at", "updated_by",
    }})
    meta["opening_balance"] = _num(meta.get("opening_balance")) or 0.0
    meta["default_multiplier"] = _int(meta.get("default_multiplier"), 100) or 100
    meta["month_extras"] = [m for m in (meta.get("month_extras") or []) if isinstance(m, str)]
    state["meta"] = meta

    cats = raw.get("categories") or state["categories"]
    state["categories"] = [str(c).strip() for c in cats if str(c).strip()]

    state["positions"] = [normalise_position(p) for p in (raw.get("positions") or [])]
    flat = raw.get("realised") or []
    if any(not isinstance(t, dict) or "legs" not in t for t in flat):
        flat = migrate_flat_trades(flat)
    state["realised"] = [normalise_trade(t) for t in flat]
    state["watchlist"] = [normalise_watch(w) for w in (raw.get("watchlist") or [])]
    state["audit"] = list(raw.get("audit") or [])[-500:]

    # One bucket per spelling, case- and space-insensitive. The registry's own
    # spelling wins, so retyping "real economy" lands on whatever the book
    # already calls that bucket instead of creating a near-duplicate that the
    # third sheet row cannot hold -- that is what used to 500 the xlsx export.
    folded: dict[str, str] = {}
    for category in state["categories"]:
        folded.setdefault(category_key(category), category)
    for position in state["positions"]:
        position["category"] = folded.setdefault(
            category_key(position["category"]), position["category"])
    state["categories"] = list(folded.values())
    return state


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #

def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _backup() -> None:
    if not STORE_PATH.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"portfolio-{stamp}.json"
    try:
        shutil.copy2(STORE_PATH, dest)
    except OSError:
        return
    backups = sorted(BACKUP_DIR.glob("portfolio-*.json"))
    for old in backups[:-MAX_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


def load() -> dict:
    with _lock:
        if not STORE_PATH.exists():
            state = default_state()
            save(state, actor="bootstrap")
            return state
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # corrupt store: recover newest backup if possible
            backups = sorted(BACKUP_DIR.glob("portfolio-*.json"))
            if backups:
                raw = json.loads(backups[-1].read_text(encoding="utf-8"))
                _atomic_write(STORE_PATH, json.dumps(raw, indent=2, ensure_ascii=False))
            else:
                raw = default_state()
        return normalise_state(raw)


def save(state: dict, actor: str = "ui") -> dict:
    with _lock:
        state = normalise_state(state)
        state["meta"]["updated_at"] = utcnow()
        state["meta"]["updated_by"] = actor
        _backup()
        _atomic_write(STORE_PATH, json.dumps(state, indent=2, ensure_ascii=False))
        return state


def mutate(fn, actor: str = "ui") -> dict:
    """Load, let fn(state) edit in place, persist, return state."""
    with _lock:
        state = load()
        result = fn(state)
        if isinstance(result, dict):
            state = result
        state = save(state, actor=actor)
        return state


def log(state: dict, action: str, detail: str = "", actor: str = "ui") -> None:
    state.setdefault("audit", []).append({
        "ts": utcnow(),
        "actor": actor,
        "action": action,
        "detail": detail[:1000],
    })
    state["audit"] = state["audit"][-500:]
