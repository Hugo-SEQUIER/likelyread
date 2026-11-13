"""Systems: what the user tries in their outreach, and what works.

A system is a recurring outreach effort ("Cold emails to game founders") with a goal
metric (reply rate or open rate) and a target. Its experiments (tests, in the dashboard)
are the things the user tries, each tested by the emails of one campaign, with a note.
The user names the categories the system varies (Hook, Link, Length...), and each
experiment has a value in them, so the values of a category can be compared and a
winner found by the statistics, not by hand. The numbers are
computed from the tracked emails at read time, like everything else; only the user's
words are stored.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from app import DB, Principal, Store, _clean_campaign, need, rates
import system_stats
from system_stats import MIN_EMAILS, emails_per_value, two_proportion_p, wilson

Goal = Literal["reply_rate", "open_rate"]
# The count behind each goal rate.
GOAL_COUNT = {"reply_rate": "replied", "open_rate": "opened"}


def _one_line(v: str | None) -> str | None:
    return None if v is None else " ".join(v.split())


def _clean_values(v: dict[str, str | None] | None) -> dict[str, str] | None:
    """Trimmed values; an empty one drops its category."""
    if v is None:
        return None
    out = {}
    for category_id, value in v.items():
        value = " ".join((value or "").split())
        if len(value) > 100:
            raise ValueError("a value is at most 100 characters")
        if value:
            out[category_id] = value
    return out


class SystemIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    goal: Goal = "reply_rate"
    target: float | None = Field(default=None, ge=0, le=1)
    notes: str = Field(default="", max_length=5000)

    _name = field_validator("name")(_one_line)


class SystemPatch(BaseModel):
    """Only the fields sent change; target null clears it."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    goal: Goal | None = None
    target: float | None = Field(default=None, ge=0, le=1)
    notes: str | None = Field(default=None, max_length=5000)

    _name = field_validator("name")(_one_line)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    _name = field_validator("name")(_one_line)


class ExperimentIn(BaseModel):
    idea: str = Field(min_length=1, max_length=500)
    campaign: str | None = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=5000)
    values: dict[str, str | None] = Field(default_factory=dict, max_length=50)

    _idea = field_validator("idea")(_one_line)
    _campaign = field_validator("campaign")(_clean_campaign)
    _values = field_validator("values")(_clean_values)


class ExperimentPatch(BaseModel):
    """Only the fields sent change; campaign null unlinks it; values replaces the map."""

    idea: str | None = Field(default=None, min_length=1, max_length=500)
    campaign: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=5000)
    values: dict[str, str | None] | None = Field(default=None, max_length=50)

    _idea = field_validator("idea")(_one_line)
    _campaign = field_validator("campaign")(_clean_campaign)
    _values = field_validator("values")(_clean_values)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


def compare(buckets: list[dict], count: str) -> dict:
    """The leader of a category's values and whether its lead is clear."""
    tried = sorted((b for b in buckets if b["value"] is not None and b["emails"]), key=lambda b: -b[count] / b["emails"])
    if len(tried) < 2:
        return {"status": "not_enough", "leader": None, "runner_up": None, "p_value": None, "emails_needed": None}
    a, b = tried[0], tried[1]
    p = two_proportion_p(a[count], a["emails"], b[count], b["emails"])
    if p < 0.05 and min(a["emails"], b["emails"]) >= MIN_EMAILS:
        a["winner"] = True
        status, needed = "clear", None
    else:
        status = "unclear"
        n = emails_per_value(a[count] / a["emails"], b[count] / b["emails"])
        needed = None if n is None else max(MIN_EMAILS, n) - min(a["emails"], b["emails"])
        needed = None if needed is None else max(1, needed)
    return {"status": status, "leader": a["value"], "runner_up": b["value"], "p_value": p, "emails_needed": needed}


# ---- systems -----------------------------------------------------------------


def parse_filters(raw: list[str]) -> dict[str, set[str | None]]:
    """value=<category id>:<value> pairs; an empty value means "not set"."""
    out: dict[str, set[str | None]] = defaultdict(set)
    for item in raw:
        category_id, sep, value = item.partition(":")
        if not sep:
            raise HTTPException(status_code=422, detail="value must be <category id>:<value>")
        out[category_id].add(value or None)
    return out


def matching(experiments: list[dict], filters: dict[str, set[str | None]]) -> list[dict]:
    """Experiments with one of the chosen values in every filtered category."""
    return [e for e in experiments if all(e["values"].get(cid) in chosen for cid, chosen in filters.items())]


class Systems:
    """Systems, their categories and experiments, with their numbers, for one account."""

    def __init__(self, db: DB, store: Store) -> None:
        self.db = db
        self.store = store

    # ---- reading -------------------------------------------------------

    def _load(self, p: Principal, only: str | None = None):
        """The account's systems (or one), with their experiments, categories and the
        email summaries of every campaign an experiment points to."""
        need(p, "systems")
        where, args = "account_id IS ?", [p.account_id]
        if only is not None:
            where, args = where + " AND id = ?", [*args, only]
        systems = self.db.query(f"SELECT * FROM systems WHERE {where} ORDER BY created_at DESC, rowid DESC", args)
        experiments = self.db.query(
            "SELECT * FROM experiments WHERE account_id IS ? ORDER BY created_at DESC, rowid DESC", (p.account_id,)
        )
        categories = self.db.query(
            "SELECT * FROM categories WHERE account_id IS ? ORDER BY created_at, rowid", (p.account_id,)
        )
        by_campaign: dict[str, list[dict]] = defaultdict(list)
        if any(e["campaign"] for e in experiments):
            for s, _ in self.store.summaries(p.account_id):
                if s["campaign"]:
                    by_campaign[s["campaign"]].append(s)
        exps: dict[str, list[dict]] = defaultdict(list)
        for e in experiments:
            exps[e["system_id"]].append({**dict(e), "values": json.loads(e["category_values"] or "{}")})
        cats: dict[str, list] = defaultdict(list)
        for c in categories:
            cats[c["system_id"]].append(c)
        return systems, exps, cats, by_campaign

    def list(self, p: Principal, only: str | None = None, filters: dict | None = None) -> list[dict]:
        systems, exps, cats, by_campaign = self._load(p, only)
        return [self._system(s, exps[s["id"]], cats[s["id"]], by_campaign, filters or {}) for s in systems]

    def emails(self, p: Principal, system_id: str, filters: dict) -> tuple:
        """(system row, categories, [(email summary, experiment)]) for one system: each email
        of its filtered experiments' campaigns once, with the experiment that tests it."""
        systems, exps, cats, by_campaign = self._load(p, system_id)
        if not systems:
            raise _not_found()
        seen: set[str] = set()
        rows = []
        for e in matching(exps[system_id], filters):
            for s in by_campaign.get(e["campaign"], []) if e["campaign"] else []:
                if s["id"] not in seen:
                    seen.add(s["id"])
                    rows.append((s, e))
        return systems[0], cats[system_id], rows

    def get(self, p: Principal, system_id: str, filters: dict | None = None) -> dict:
        found = self.list(p, system_id, filters)
        if not found:
            raise _not_found()
        return found[0]

    def _experiment(self, p: Principal, experiment_id: str) -> dict:
        system_id = self._owned("experiments", experiment_id, p)["system_id"]
        return next(e for e in self.get(p, system_id)["experiments"] if e["id"] == experiment_id)

    def _category(self, p: Principal, category_id: str) -> dict:
        system_id = self._owned("categories", category_id, p)["system_id"]
        return next(c for c in self.get(p, system_id)["categories"] if c["id"] == category_id)

    @staticmethod
    def _system(row, experiments: list[dict], categories: list, by_campaign: dict, filters: dict) -> dict:
        def emails_of(exps: list[dict]) -> list[dict]:
            campaigns = {e["campaign"] for e in exps if e["campaign"]}
            return [s for c in campaigns for s in by_campaign.get(c, [])]

        experiments = matching(experiments, filters)
        goal = row["goal"]
        count = GOAL_COUNT[goal]
        numbers = rates(emails_of(experiments))
        current = numbers[goal] if numbers["emails"] else None
        if current is None or row["target"] is None:
            health = "not_measured"
        else:
            health = "on_target" if current >= row["target"] else "below_target"

        columns = []
        for c in categories:
            groups: dict[str | None, list[dict]] = defaultdict(list)
            for e in experiments:
                groups[e["values"].get(c["id"])].append(e)
            buckets = []
            for value, exps in groups.items():
                n = rates(emails_of(exps))
                buckets.append(
                    {"value": value, "experiments": len(exps), **n, "ci": wilson(n[count], n["emails"]), "winner": False}
                )
            result = compare(buckets, count)
            buckets.sort(key=lambda b: (b["value"] is None, -(b[count] / b["emails"] if b["emails"] else -1)))
            columns.append(
                {"id": c["id"], "name": c["name"], "created_at": c["created_at"], "values": buckets, "result": result}
            )

        return {
            "id": row["id"],
            "name": row["name"],
            "goal": goal,
            "target": row["target"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            **numbers,
            "current": current,
            "health": health,
            "categories": columns,
            "experiments": [
                {
                    "id": e["id"],
                    "system_id": e["system_id"],
                    "idea": e["idea"],
                    "campaign": e["campaign"],
                    "note": e["note"],
                    "values": e["values"],
                    "created_at": e["created_at"],
                    **rates(emails_of([e])),
                }
                for e in experiments
            ],
        }

    def _owned(self, table: str, row_id: str, p: Principal):
        need(p, "systems")
        rows = self.db.query(f"SELECT * FROM {table} WHERE id = ? AND account_id IS ?", (row_id, p.account_id))
        if not rows:
            raise _not_found()
        return rows[0]

    def _checked_values(self, system_id: str, values: dict[str, str]) -> str:
        known = {r["id"] for r in self.db.query("SELECT id FROM categories WHERE system_id = ?", (system_id,))}
        if set(values) - known:
            raise HTTPException(status_code=422, detail="values: a category is not in this system")
        return json.dumps(values)

    def _unique_name(self, system_id: str, name: str, other_than: str | None = None) -> None:
        if self.db.query(
            "SELECT 1 FROM categories WHERE system_id = ? AND lower(name) = lower(?) AND id IS NOT ?",
            (system_id, name, other_than),
        ):
            raise HTTPException(status_code=409, detail="this system already has that category")

    # ---- writing -------------------------------------------------------

    def create(self, p: Principal, body: SystemIn) -> dict:
        need(p, "systems")
        system_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO systems(id, account_id, name, goal, target, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (system_id, p.account_id, body.name, body.goal, body.target, body.notes, time.time()),
        )
        return self.get(p, system_id)

    def update(self, p: Principal, system_id: str, body: SystemPatch) -> dict:
        self._owned("systems", system_id, p)
        changes = body.model_dump(include=body.model_fields_set)
        for key in ("name", "goal", "notes"):
            if changes.get(key, "") is None:  # these cannot be cleared
                del changes[key]
        self._update("systems", system_id, changes)
        return self.get(p, system_id)

    def delete(self, p: Principal, system_id: str) -> dict:
        self._owned("systems", system_id, p)
        self.db.execute("DELETE FROM experiments WHERE system_id = ?", (system_id,))
        self.db.execute("DELETE FROM categories WHERE system_id = ?", (system_id,))
        self.db.execute("DELETE FROM systems WHERE id = ?", (system_id,))
        return {"ok": True}

    def add_category(self, p: Principal, system_id: str, body: CategoryIn) -> dict:
        self._owned("systems", system_id, p)
        self._unique_name(system_id, body.name)
        category_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO categories(id, account_id, system_id, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (category_id, p.account_id, system_id, body.name, time.time()),
        )
        return self._category(p, category_id)

    def rename_category(self, p: Principal, category_id: str, body: CategoryIn) -> dict:
        row = self._owned("categories", category_id, p)
        self._unique_name(row["system_id"], body.name, category_id)
        self.db.execute("UPDATE categories SET name = ? WHERE id = ?", (body.name, category_id))
        return self._category(p, category_id)

    def delete_category(self, p: Principal, category_id: str) -> dict:
        row = self._owned("categories", category_id, p)
        with self.db.lock:
            for e in self.db.query("SELECT id, category_values FROM experiments WHERE system_id = ?", (row["system_id"],)):
                values = json.loads(e["category_values"] or "{}")
                if values.pop(category_id, None) is not None:
                    self.db.execute("UPDATE experiments SET category_values = ? WHERE id = ?", (json.dumps(values), e["id"]))
            self.db.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        return {"ok": True}

    def add_experiment(self, p: Principal, system_id: str, body: ExperimentIn) -> dict:
        self._owned("systems", system_id, p)
        values = self._checked_values(system_id, body.values)
        experiment_id = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO experiments(id, account_id, system_id, idea, campaign, note, category_values, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (experiment_id, p.account_id, system_id, body.idea, body.campaign, body.note, values, time.time()),
        )
        return self._experiment(p, experiment_id)

    def update_experiment(self, p: Principal, experiment_id: str, body: ExperimentPatch) -> dict:
        row = self._owned("experiments", experiment_id, p)
        changes = body.model_dump(include=body.model_fields_set)
        for key in ("idea", "note"):
            if changes.get(key, "") is None:
                del changes[key]
        if "values" in changes:
            values = changes.pop("values")
            changes["category_values"] = self._checked_values(row["system_id"], values or {})
        self._update("experiments", experiment_id, changes)
        return self._experiment(p, experiment_id)

    def delete_experiment(self, p: Principal, experiment_id: str) -> dict:
        self._owned("experiments", experiment_id, p)
        self.db.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
        return {"ok": True}

    def _update(self, table: str, row_id: str, changes: dict) -> None:
        if changes:  # keys come from the pydantic models above, never from the request
            sets = ", ".join(f"{k} = ?" for k in changes)
            self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), row_id))


def register_routes(app: FastAPI, principal, systems: Systems) -> None:
    @app.get("/api/systems")
    def list_systems(p: Principal = Depends(principal)):
        return systems.list(p)

    @app.post("/api/systems")
    def create_system(body: SystemIn, p: Principal = Depends(principal)):
        return systems.create(p, body)

    @app.get("/api/systems/{system_id}")
    def get_system(
        system_id: str,
        p: Principal = Depends(principal),
        value: list[str] = Query(default=[], max_length=50, description="<category id>:<value> filters"),
    ):
        return systems.get(p, system_id, parse_filters(value))

    @app.get("/api/systems/{system_id}/stats")
    def system_stats_route(
        system_id: str,
        p: Principal = Depends(principal),
        value: list[str] = Query(default=[], max_length=50),
    ):
        row, categories, rows = systems.emails(p, system_id, parse_filters(value))
        return system_stats.stats(row["goal"], GOAL_COUNT[row["goal"]], categories, rows)

    @app.get("/api/systems/{system_id}/export.csv")
    def system_csv(
        system_id: str,
        p: Principal = Depends(principal),
        value: list[str] = Query(default=[], max_length=50),
    ):
        row, categories, rows = systems.emails(p, system_id, parse_filters(value))
        slug = re.sub(r"[^a-z0-9]+", "-", row["name"].lower()).strip("-") or "system"
        return Response(
            content=system_stats.export_csv(categories, rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="likelyread-{slug}.csv"'},
        )

    @app.patch("/api/systems/{system_id}")
    def update_system(system_id: str, body: SystemPatch, p: Principal = Depends(principal)):
        return systems.update(p, system_id, body)

    @app.delete("/api/systems/{system_id}")
    def delete_system(system_id: str, p: Principal = Depends(principal)):
        return systems.delete(p, system_id)

    @app.post("/api/systems/{system_id}/categories")
    def add_category(system_id: str, body: CategoryIn, p: Principal = Depends(principal)):
        return systems.add_category(p, system_id, body)

    @app.patch("/api/categories/{category_id}")
    def rename_category(category_id: str, body: CategoryIn, p: Principal = Depends(principal)):
        return systems.rename_category(p, category_id, body)

    @app.delete("/api/categories/{category_id}")
    def delete_category(category_id: str, p: Principal = Depends(principal)):
        return systems.delete_category(p, category_id)

    @app.post("/api/systems/{system_id}/experiments")
    def add_experiment(system_id: str, body: ExperimentIn, p: Principal = Depends(principal)):
        return systems.add_experiment(p, system_id, body)

    @app.patch("/api/experiments/{experiment_id}")
    def update_experiment(experiment_id: str, body: ExperimentPatch, p: Principal = Depends(principal)):
        return systems.update_experiment(p, experiment_id, body)

    @app.delete("/api/experiments/{experiment_id}")
    def delete_experiment(experiment_id: str, p: Principal = Depends(principal)):
        return systems.delete_experiment(p, experiment_id)
