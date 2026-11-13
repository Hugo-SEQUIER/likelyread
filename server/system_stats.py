"""Statistics for systems: intervals, tests, the effect of each value tried and the
correlation matrix, plus the per-email CSV export. Pure functions on email summaries.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from itertools import combinations

# A winner needs at least this many emails on both sides of the comparison.
MIN_EMAILS = 10
Z_95 = 1.959964
Z_80 = 0.841621
# Values of different categories this correlated always went out together.
CONFOUNDED_R = 0.8


def wilson(k: int, n: int) -> list[float] | None:
    """95% Wilson interval of k successes in n: honest even for small n and 0 or 100%."""
    if n == 0:
        return None
    p = k / n
    centre = (p + Z_95**2 / (2 * n)) / (1 + Z_95**2 / n)
    half = Z_95 * math.sqrt(p * (1 - p) / n + Z_95**2 / (4 * n * n)) / (1 + Z_95**2 / n)
    return [max(0.0, centre - half), min(1.0, centre + half)]


def two_proportion_p(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided p-value of a two-proportion z-test (pooled)."""
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    return math.erfc(abs(k1 / n1 - k2 / n2) / se / math.sqrt(2))


def emails_per_value(p1: float, p2: float) -> int | None:
    """Emails each value needs to tell p1 from p2 (5% significance, 80% power)."""
    if p1 == p2:
        return None
    return math.ceil((Z_95 + Z_80) ** 2 * (p1 * (1 - p1) + p2 * (1 - p2)) / (p1 - p2) ** 2)


def pearson(x: list[int], y: list[int]) -> float | None:
    """Correlation of two 0/1 columns (the phi coefficient); None when one never varies."""
    n = len(x)
    sx, sy = sum(x), sum(y)
    sxy = sum(a * b for a, b in zip(x, y))
    den = math.sqrt((n * sx - sx * sx) * (n * sy - sy * sy))
    return None if den == 0 else (n * sxy - sx * sy) / den


def outcomes(summary: dict) -> dict[str, int]:
    return {"opened": int(summary["opens"] > 0), "replied": int(summary["replied_at"] is not None)}


def stats(goal: str, count: str, categories: list, rows: list[tuple[dict, dict]]) -> dict:
    """What moves the goal: each value's rate against the other values of its category,
    and how values and outcomes go together. rows are (email summary, its experiment)."""
    results = [outcomes(s) for s, _ in rows]
    features = []  # (category row, value, 0/1 column)
    for c in categories:
        seen: dict[str, int] = {}
        for _, e in rows:
            v = e["values"].get(c["id"])
            if v is not None:
                seen[v] = seen.get(v, 0) + 1
        for v in sorted(seen, key=lambda v: (-seen[v], v)):
            features.append((c, v, [int(e["values"].get(c["id"]) == v) for _, e in rows]))

    effects = []
    for c, v, column in features:
        with_ = [r[count] for r, has in zip(results, column) if has]
        without = [
            r[count]
            for r, (_, e) in zip(results, rows)
            if e["values"].get(c["id"]) not in (None, v)
        ]
        rate = sum(with_) / len(with_)
        rate_without = sum(without) / len(without) if without else None
        p = two_proportion_p(sum(with_), len(with_), sum(without), len(without)) if without else None
        effects.append(
            {
                "category_id": c["id"],
                "category": c["name"],
                "value": v,
                "emails": len(with_),
                "rate": rate,
                "rate_without": rate_without,
                "diff": None if rate_without is None else rate - rate_without,
                "p_value": p,
                "significant": p is not None and p < 0.05 and min(len(with_), len(without)) >= MIN_EMAILS,
            }
        )
    effects.sort(key=lambda e: -(e["diff"] if e["diff"] is not None else -2))

    labels = [{"kind": "value", "category_id": c["id"], "category": c["name"], "value": v} for c, v, _ in features]
    labels += [{"kind": "outcome", "name": name} for name in ("opened", "replied")]
    columns = [col for _, _, col in features] + [[r["opened"] for r in results], [r["replied"] for r in results]]
    r = [[pearson(a, b) for b in columns] for a in columns]

    confounded = []
    for i, j in combinations(range(len(features)), 2):
        if features[i][0]["id"] != features[j][0]["id"] and r[i][j] is not None and r[i][j] >= CONFOUNDED_R:
            confounded.append(
                {
                    "a": {"category": features[i][0]["name"], "value": features[i][1]},
                    "b": {"category": features[j][0]["name"], "value": features[j][1]},
                    "r": r[i][j],
                }
            )
    return {
        "goal": goal,
        "emails": len(rows),
        "effects": effects,
        "matrix": {"labels": labels, "r": r},
        "confounded": confounded,
    }


def _cell(value) -> object:
    """Text a spreadsheet will not run as a formula (CSV injection)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _iso(ts: float | None) -> str:
    return "" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def export_csv(categories: list, rows: list[tuple[dict, dict]]) -> str:
    """One row per email: its experiment, its value in each category and what happened."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(
        ["email_id", "sent_at", "campaign", "test", *(c["name"] for c in categories),
         "opened", "opens", "replied", "replied_at", "hours_to_reply"]
    )
    for s, e in sorted(rows, key=lambda row: row[0]["sent_at"] or 0):
        replied = s["replied_at"]
        hours = "" if replied is None or s["sent_at"] is None else round((replied - s["sent_at"]) / 3600, 2)
        w.writerow(
            [_cell(v) for v in (
                s["id"], _iso(s["sent_at"]), s["campaign"], e["idea"],
                *(e["values"].get(c["id"], "") for c in categories),
                int(s["opens"] > 0), s["opens"], int(replied is not None), _iso(replied), hours,
            )]
        )
    return out.getvalue()
