"""Every question a person asks on the Ask tab, and the answer they got.

A person sees only their own history. An answer is kept as returned, with the
company scope it was computed under; if that person can no longer see one of
those companies, the stored figures are withheld and they are asked to run
the question again under the scope they have now.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from fpa_project.governance import engine

SCHEMA = "fpa_governance"
log = logging.getLogger(__name__)


def record(user_id: str, question: str, provider: str, mode: str, status: str,
           companies: frozenset[str] | set[str], response: dict[str, Any]) -> int | None:
    """Keep one question and its answer. Best effort: a failure here never fails the answer."""
    try:
        with engine().begin() as conn:
            return conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.ask_history (user_id, question, provider, mode, status, companies, response) "
                    "VALUES (:u, :q, :p, :m, :s, :c, CAST(:r AS jsonb)) RETURNING ask_id"
                ),
                {"u": user_id, "q": question, "p": provider, "m": mode, "s": status,
                 "c": sorted(companies), "r": json.dumps(response, default=str)},
            ).scalar_one()
    except Exception:  # noqa: BLE001 - history is a convenience; the answer already stands
        log.exception("could not record ask history for %s", user_id)
        return None


def _withheld(companies: list[str], visible: frozenset[str]) -> bool:
    return not set(companies or []) <= set(visible)


def list_for(user_id: str, visible: frozenset[str], search: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """The person's questions, newest first, without the answers."""
    params: dict[str, Any] = {"u": user_id, "limit": limit}
    where = ""
    if search.strip():
        where, params["q"] = " AND question ILIKE :q", f"%{search.strip()}%"
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT ask_id, question, status, mode, companies, created_at FROM {SCHEMA}.ask_history "
                f"WHERE user_id = :u{where} ORDER BY created_at DESC LIMIT :limit"
            ),
            params,
        ).mappings().all()
    return [
        {"ask_id": r["ask_id"], "question": r["question"], "status": r["status"], "mode": r["mode"],
         "created_at": r["created_at"], "withheld": _withheld(r["companies"], visible)}
        for r in rows
    ]


def load_for(user_id: str, ask_id: int, visible: frozenset[str]) -> dict[str, Any] | None:
    """One question and its answer, if it is this person's. None otherwise."""
    with engine().connect() as conn:
        row = conn.execute(
            text(
                f"SELECT ask_id, question, provider, mode, status, companies, response, created_at "
                f"FROM {SCHEMA}.ask_history WHERE ask_id = :id AND user_id = :u"
            ),
            {"id": ask_id, "u": user_id},
        ).mappings().first()
    if row is None:
        return None
    withheld = _withheld(row["companies"], visible)
    return {
        "ask_id": row["ask_id"], "question": row["question"], "provider": row["provider"],
        "mode": row["mode"], "status": row["status"], "created_at": row["created_at"],
        "withheld": withheld,
        # Figures computed under a scope the person no longer has are not shown again
        "response": None if withheld else row["response"],
    }
