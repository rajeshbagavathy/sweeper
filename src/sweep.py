from __future__ import annotations

import itertools
import random
from typing import Any

from src.config import SweepConfig


def keep_combination(combo: dict[str, Any], exclude_expressions: list[str]) -> bool:
    """True if combo survives every exclude rule.

    Each expression is evaluated with only the combination's own keys in scope - a
    restricted eval (no builtins) per the spec, so exclude rules can't do anything
    beyond reading combination values.
    """
    for expr in exclude_expressions:
        try:
            if eval(expr, {"__builtins__": {}}, dict(combo)):
                return False
        except Exception:
            # An exclude rule referencing a key this combo doesn't have (or any other
            # eval error) shouldn't take down the whole sweep - just don't exclude on it.
            continue
    return True


def expand(sweep: SweepConfig) -> list[dict[str, Any]]:
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]

    combos = []
    for values in itertools.product(*value_lists):
        combo = dict(sweep.fixed)
        combo.update(zip(keys, values))
        combos.append(combo)

    combos = [c for c in combos if keep_combination(c, sweep.exclude)]

    if sweep.shuffle:
        random.shuffle(combos)
    if sweep.limit is not None:
        combos = combos[: sweep.limit]

    return combos
