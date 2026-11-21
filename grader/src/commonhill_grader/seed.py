"""The run's seed. On the platform, `AR_SEED` holds a secret seed that only the grader gets; it
must never be logged or written to the result, so nothing here prints it, returns an object
whose repr shows it, or puts it in an error. Locally `AR_SEED` is unset and every run uses the
public seed, so local runs are reproducible.

`rng(label)` gives an independent random stream per purpose (`rng("order")`, `rng("inputs")`),
each derived from the seed with HMAC-SHA256: the streams don't overlap, and knowing one stream's
numbers reveals neither the seed nor the other streams.
"""

import hashlib
import hmac
import os
import random

SEED_ENV = "AR_SEED"
PUBLIC_SEED = "commonhill-public-seed-v1"


def seed() -> str:
    """`AR_SEED` when set (and not empty), else `PUBLIC_SEED`."""
    return os.environ.get(SEED_ENV) or PUBLIC_SEED


def is_local() -> bool:
    """Whether this is a local run (no platform seed): only then are METRIC/ASI lines printed."""
    return not os.environ.get(SEED_ENV)


def rng(label: str) -> random.Random:
    """A `random.Random` for `label`, seeded with HMAC-SHA256(seed, label)."""
    digest = hmac.new(seed().encode(), label.encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(digest))
