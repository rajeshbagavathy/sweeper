from __future__ import annotations

import itertools
import random
from typing import Any, Iterator

from src.config import SweepConfig

# A raw (pre-exclude) Cartesian product above this size means expand() would hang the
# caller for a long time materializing a list that large - fail fast instead. Filtering
# only ever shrinks the count, so checking the raw product first is a safe, cheap
# upper-bound check before doing any real work.
# Was 200_000 - too tight for legitimately wide sweeps whose exclude rules shrink the
# raw product a lot (the check runs before exclude filtering, so a config that ends up
# with a modest final count could still get rejected here). Raised 10x; still cheap to
# check and still catches truly runaway grids.
MAX_COMBINATIONS = 2_000_000

# Above this many raw combinations, expand()'s eval()-based exclude filtering (see
# keep_combination) is the actual performance problem, not a hypothetical one -
# confirmed live via benchmark: ~14s of pure eval() time for 3,024,000 raw combos with
# a single exclude rule, and a real config has several. That's tens of seconds of
# blocking work on every Preview click and every Start click before anything else can
# happen. Below this threshold expand()'s exact, eager pass is already sub-second and
# there's no reason to give up precision for it - see estimate_survival/
# iter_shuffled_combos, which are the large-sweep alternative to expand().
EXACT_COUNT_THRESHOLD = 50_000


class TooManyCombinations(ValueError):
    pass


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


def raw_count(value_lists: list[list[Any]]) -> int:
    """Size of the Cartesian product of value_lists, before any exclude filtering -
    O(number of parameters), never touches an actual combination."""
    n = 1
    for values in value_lists:
        n *= max(len(values), 1)
    return n


def _check_cap(n: int) -> None:
    if n > MAX_COMBINATIONS:
        raise TooManyCombinations(
            f"This configuration would produce {n:,} combinations before exclude "
            f"filtering (limit is {MAX_COMBINATIONS:,}). Narrow a range or increase "
            f"an interval on one or more parameters."
        )


def unrank(keys: list[str], value_lists: list[list[Any]], fixed: dict[str, Any], index: int) -> dict[str, Any]:
    """The combo at position `index` of itertools.product(*value_lists) (0-based),
    decoded directly via mixed-radix arithmetic - same value as zipping `keys` with
    `list(itertools.product(*value_lists))[index]`, but without materializing every
    combo before it (see test_unrank_matches_product_order). This is what lets
    estimate_survival/iter_shuffled_combos below touch a huge combination space
    without ever building the whole thing."""
    per_key: list[Any] = [None] * len(keys)
    remainder = index
    for pos in range(len(value_lists) - 1, -1, -1):
        values = value_lists[pos]
        remainder, i = divmod(remainder, len(values))
        per_key[pos] = values[i]
    combo = dict(fixed)
    combo.update(zip(keys, per_key))
    return combo


def expand(sweep: SweepConfig) -> list[dict[str, Any]]:
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]
    _check_cap(raw_count(value_lists))

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


def estimate_survival(
    sweep: SweepConfig, sample_size: int = 5_000, seed: int | None = None
) -> tuple[int, list[dict[str, Any]]]:
    """How many of sweep's raw combinations would survive exclude filtering, estimated
    from a random sample instead of checked one by one - the exact count is what makes
    expand() slow on a huge sweep (see EXACT_COUNT_THRESHOLD), so this exists to answer
    "roughly how many" in well under a second regardless of how large the raw space is.

    Returns (estimated survivor count, up to 10 sample survivors) - the estimate is a
    simple extrapolation (survival rate in the sample x raw size) and gets noisier the
    smaller/rarer the surviving fraction is, but is more than good enough for a Preview
    number or an in-progress total that only needs to be in the right ballpark."""
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]
    n = raw_count(value_lists)
    _check_cap(n)
    if n == 0:
        return 0, []

    rng = random.Random(seed)
    size = min(sample_size, n)
    survivors = 0
    sample: list[dict[str, Any]] = []
    for idx in rng.sample(range(n), size):
        combo = unrank(keys, value_lists, sweep.fixed, idx)
        if keep_combination(combo, sweep.exclude):
            survivors += 1
            if len(sample) < 10:
                sample.append(combo)
    estimated = round(n * survivors / size)
    if sweep.limit is not None:
        estimated = min(estimated, sweep.limit)
    return estimated, sample


def count_or_estimate(sweep: SweepConfig, sample_size: int = 5_000, seed: int | None = None) -> dict[str, Any]:
    """The right-sized answer to "how many combinations, and what do a few look like"
    for either a small sweep (expand() is already fast - exact) or a huge one
    (estimate_survival - fast but approximate). Used by both Preview and, for the
    in-progress "total" a huge run reports while it's still walking iter_shuffled_combos
    below, since neither can afford expand()'s exact pass at that size."""
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]
    n = raw_count(value_lists)
    _check_cap(n)
    if n <= EXACT_COUNT_THRESHOLD:
        combos = expand(sweep)
        return {"raw_count": n, "count": len(combos), "estimated": False, "sample": combos[:10]}
    estimated, sample = estimate_survival(sweep, sample_size=sample_size, seed=seed)
    return {"raw_count": n, "count": estimated, "estimated": True, "sample": sample}


def iter_shuffled_combos(sweep: SweepConfig, seed: int | None = None) -> Iterator[dict[str, Any]]:
    """Yields every combo surviving sweep.exclude, across the full raw combination
    space exactly once, in random order - the large-sweep alternative to expand().

    Unlike expand(), never materializes more than one combo (plus a compact list of
    raw indices) at a time, so a several-hundred-thousand-combo sweep no longer means
    a slow eval() pass and a huge list of dicts sitting in memory before a single
    browser action can even start.

    The random order is what makes this safe to consume in chunks without bias - any
    contiguous run of N yielded combos is an unbiased sample across every swept
    parameter (never clustered the way raw itertools.product order would be, e.g. a
    long run of combos sharing the same slow-varying keys), and the full stream covers
    the whole space exactly once with no repeats and no gaps. A caller that processes
    this incrementally (see run_sweep_multiprocess) is therefore effectively working
    through the sweep in nicely-shuffled batches without this function - or the
    caller - ever needing to think in terms of explicit batch boundaries."""
    keys = list(sweep.vary.keys())
    value_lists = [sweep.vary[k] for k in keys]
    n = raw_count(value_lists)
    _check_cap(n)

    order = list(range(n))
    random.Random(seed).shuffle(order)

    yielded = 0
    for idx in order:
        combo = unrank(keys, value_lists, sweep.fixed, idx)
        if keep_combination(combo, sweep.exclude):
            yield combo
            yielded += 1
            if sweep.limit is not None and yielded >= sweep.limit:
                return
