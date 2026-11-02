"""Does a simple calibration of the model's mood scale to your own ratings help?

    python -m enrich.evaluate mood         # saves the pairs (your rating, the model's) - numbers only, no text
    python -m enrich.evaluate calibrate    # fits   your rating ~ a + b * model   and checks it by leave-one-out

Every entry is predicted from a fit on all OTHER entries, so the figures are not flattered by the fit
itself. Plain Python, no dependencies.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEAD_ZONE = 0.5   # a calibrated value within +-0.5 counts as "zero"
MIN_PAIRS = 10


def _mean(v: list[float]) -> float:
    return sum(v) / len(v)


def fit(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares y = a + b x. A constant x gives b = 0 and the mean of y."""
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def pearson(xs: list[float], ys: list[float]) -> float | None:
    mx, my = _mean(xs), _mean(ys)
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy) ** 0.5


def ranks(v: list[float]) -> list[float]:
    """Average ranks (ties share the mean of their positions)."""
    order = sorted(range(len(v)), key=lambda i: v[i])
    out = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def sign(v: float, dead: float = 0.0) -> int:
    return (v > dead) - (v < -dead)


@dataclass
class Calibration:
    n: int
    pearson: float | None
    spearman: float | None
    a: float
    b: float
    raw_error: float
    raw_same_sign: float
    loo_error: float
    loo_same_sign: float

    def __str__(self) -> str:
        corr = lambda r: "-" if r is None else f"{r:.2f}"  # noqa: E731
        verdict = ("the model's scale is too narrow" if self.b > 1.15 else
                   "the model's scale is about right" if self.b > 0.85 else "the model's scale is too wide")
        return "\n".join([
            f"pairs: {self.n}",
            f"correlation between the model and your ratings: Pearson {corr(self.pearson)}, "
            f"Spearman {corr(self.spearman)}",
            f"fit: your rating = {self.a:+.2f} + {self.b:.2f} x model  ({verdict})",
            f"without calibration:        mean error {self.raw_error:.2f}, same sign {100 * self.raw_same_sign:.0f} %",
            f"with calibration (leave-one-out): mean error {self.loo_error:.2f}, "
            f"same sign {100 * self.loo_same_sign:.0f} % (calibrated values within +-{DEAD_ZONE} count as zero)",
        ])


def calibrate(pairs: list[tuple[float, float]]) -> Calibration:
    """pairs: (your rating, the model's value)."""
    if len(pairs) < MIN_PAIRS:
        raise ValueError(f"Only {len(pairs)} pairs; at least {MIN_PAIRS} are needed.")
    ys, xs = [p[0] for p in pairs], [p[1] for p in pairs]
    a, b = fit(xs, ys)
    loo_err, loo_same = 0.0, 0
    for i in range(len(pairs)):
        ai, bi = fit(xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:])
        c = ai + bi * xs[i]
        loo_err += abs(c - ys[i])
        loo_same += sign(c, DEAD_ZONE) == sign(ys[i])
    n = len(pairs)
    return Calibration(
        n, pearson(xs, ys), pearson(ranks(xs), ranks(ys)), a, b,
        sum(abs(x - y) for x, y in zip(xs, ys)) / n,
        sum(sign(x) == sign(y) for x, y in zip(xs, ys)) / n,
        loo_err / n, loo_same / n)


def save_pairs(path: Path, model: str, version: str, rows: list[tuple[int, int, int, int]]) -> None:
    """rows: (entry id, your rating, the model's value, entry length). Numbers only - no text."""
    path.write_text(json.dumps({"model": model, "version": version,
                                "pairs": [list(r) for r in rows]}), encoding="utf-8")


def load_pairs(path: Path) -> tuple[dict, list[tuple[float, float]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, [(float(r[1]), float(r[2])) for r in data["pairs"]]
