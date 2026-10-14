"""Import card / bank statement CSVs into ``transactions``.

Banks disagree on headers and on sign conventions. Each ``Profile`` below describes one
export format; ``detect`` picks the profile whose signature headers are all present.
Amounts are normalised to the app convention: negative = money out, positive = in.

Dedupe: ``external_id`` is the bank's reference when the export has one, otherwise a
hash of (date, amount, description) plus a per-file sequence so that two identical
lines on one day both import, while re-importing an overlapping file skips them.
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..models import Transaction


@dataclass(frozen=True)
class Profile:
    name: str
    signature: tuple[str, ...]            # headers that must all be present to auto-detect
    date: tuple[str, ...]                 # candidate date columns, priority order
    description: tuple[str, ...]
    amount: tuple[str, ...] = ()          # single signed column ...
    debit: tuple[str, ...] = ()           # ... or separate debit / credit columns
    credit: tuple[str, ...] = ()
    credit_sign: int = 1                  # -1 when the credit column reports credits as negatives
    sign: int = 1                         # multiply amount by this; -1 when positive means a charge
    ref: tuple[str, ...] = ()             # bank transaction id, if any
    extra: dict = field(default_factory=dict)


PROFILES: list[Profile] = [
    Profile("chase", ("Transaction Date", "Post Date", "Description", "Amount"),
            date=("Transaction Date", "Post Date"), description=("Description",),
            amount=("Amount",), sign=1),                                   # purchases negative
    Profile("amex", ("Date", "Description", "Amount", "Card Member"),
            date=("Date",), description=("Description",),
            amount=("Amount",), sign=-1, ref=("Reference",)),              # purchases positive
    Profile("capitalone", ("Transaction Date", "Posted Date", "Description", "Debit", "Credit"),
            date=("Transaction Date", "Posted Date"), description=("Description",),
            debit=("Debit",), credit=("Credit",)),
    Profile("discover", ("Trans. Date", "Post Date", "Description", "Amount"),
            date=("Trans. Date", "Post Date"), description=("Description",),
            amount=("Amount",), sign=-1),                                  # purchases positive
    Profile("citi", ("Date", "Description", "Debit", "Credit"),
            date=("Date",), description=("Description",),
            debit=("Debit",), credit=("Credit",), credit_sign=-1),  # Citi: credits negative
    Profile("bofa", ("Posted Date", "Reference Number", "Payee", "Amount"),
            date=("Posted Date",), description=("Payee",),
            amount=("Amount",), sign=1, ref=("Reference Number",)),
    # Last resort: any CSV with a date, a description and a signed amount.
    Profile("generic", ("Date", "Description", "Amount"),
            date=("Date",), description=("Description",), amount=("Amount",), sign=1),
]
PROFILE_BY_NAME = {p.name: p for p in PROFILES}


def detect(headers: list[str]) -> Profile:
    hs = {h.strip() for h in headers}
    for p in PROFILES:
        if set(p.signature) <= hs:
            return p
    raise ValueError(
        f"unrecognised statement format; headers: {headers}. "
        f"Pass --format {{{','.join(PROFILE_BY_NAME)}}} or extend PROFILES."
    )


def _pick(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v.strip()
    return None


def _money(s: str | None) -> float | None:
    if not s:
        return None
    s = s.strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    v = float(s)
    return -v if neg else v


def _date(s: str | None) -> str:
    if not s:
        raise ValueError("missing date")
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {s!r}")


def _amount(row: dict, p: Profile) -> float:
    if p.amount:
        v = _money(_pick(row, p.amount))
        if v is None:
            raise ValueError("missing amount")
        return v * p.sign
    debit = _money(_pick(row, p.debit)) or 0.0
    credit = _money(_pick(row, p.credit)) or 0.0
    return credit * p.credit_sign - debit


def content_key(date: str, amount: float, description: str) -> str:
    return hashlib.sha1(f"{date}|{amount:.2f}|{description.lower()}".encode()).hexdigest()[:16]


@dataclass
class ImportResult:
    profile: str
    new: int = 0
    skipped: int = 0


def import_csv(conn: sqlite3.Connection, path: Path, account_id: int,
               fmt: str | None = None, flip_sign: bool = False) -> ImportResult:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        p = PROFILE_BY_NAME[fmt] if fmt else detect(headers)
        res = ImportResult(profile=p.name)
        seen: dict[str, int] = {}
        for row in reader:
            row = {k.strip(): v for k, v in row.items() if k}
            date = _date(_pick(row, p.date))
            desc = _pick(row, p.description) or ""
            amount = round(_amount(row, p) * (-1 if flip_sign else 1), 2)
            ref = _pick(row, p.ref)
            if ref:
                ext = f"{p.name}:{ref}"
            else:
                base = content_key(date, amount, desc)
                seq = seen.get(base, 0)
                seen[base] = seq + 1
                ext = f"{base}#{seq}" if seq else base
            t = Transaction(account_id=account_id, posted_date=date, amount=amount,
                            description=desc, external_id=ext, source=f"csv-{p.name}")
            if t.insert(conn) is not None:
                res.new += 1
            else:
                res.skipped += 1
    conn.commit()
    return res
