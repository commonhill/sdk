"""Oracle-style example grader: how few probes does the worker need to find hidden numbers?

Each item hides a number in [0, 2**20), drawn from the run's seed. The worker gets the item's
oracle session and the range's bits, never the number; it asks the grader's oracle
(`AR_ORACLE_URL`) to compare a probe with it: `{"probe": p}` is answered `{"cmp": c}`, -1 if the
number is below p, 0 at it, 1 above. Its output is the number. The score is the mean number of
calls per item (`probes`, minimize), counted by the grader (the SDK's `Oracle`): the worker can
only lower it by asking less, never by saying so. A wrong or failed item (a session over its cap
of MAX_CALLS calls fails its item) makes the run invalid. The per-item counts go to items.json,
the bytes moved to the secondary metrics.

    WORKER_URL=http://127.0.0.1:8080 python grade.py [result.json]

The oracle listens on ORACLE_PORT (8081); the worker finds it in AR_ORACLE_URL
(`http://grader:8081` in a project's compose.yaml).
"""

import os
import sys
from pathlib import Path
from typing import cast

from arh_grader import (
    ORACLE_PORT,
    RESULT_PATH,
    Client,
    Oracle,
    Value,
    invalid_result,
    rng,
    wait_ready,
    write_result,
)

GRADER_VERSION = "oracle-example-1"
N_ITEMS = 50
BITS = 20
MAX_CALLS = 64  # per item: a binary search needs BITS, a scan 2**BITS
BATCH_SIZE = 5
OUTPUT_SCHEMA = {"type": "integer", "minimum": 0, "maximum": 2**BITS - 1}


def compare(hidden: int, query: object) -> dict[str, int]:
    """The oracle's answer about `hidden`: where it is against the probe. An invalid query is a
    ValueError, answered 400 (and counted)."""
    probe = cast(dict[str, object], query).get("probe") if isinstance(query, dict) else None
    if not isinstance(probe, int) or isinstance(probe, bool) or not 0 <= probe < 2**BITS:
        raise ValueError("a probe is an integer in range")
    return {"cmp": (hidden > probe) - (hidden < probe)}


def main(result: Path = RESULT_PATH, port: int = ORACLE_PORT) -> int:
    try:
        worker_url = os.environ["WORKER_URL"]
        numbers = rng("numbers")
        hidden = [numbers.randrange(2**BITS) for _ in range(N_ITEMS)]
        wait_ready(worker_url)  # runner.startup_timeout_s, from the platform (AR_STARTUP_TIMEOUT_S)
        with Oracle(compare, max_calls=MAX_CALLS, port=port) as oracle:
            sessions = [oracle.session(number, key=index) for index, number in enumerate(hidden)]
            items = [{"oracle": session.token, "bits": BITS} for session in sessions]
            client = Client(worker_url, output_schema=OUTPUT_SCHEMA)
            evaluation = client.evaluate(items, BATCH_SIZE, sessions=sessions)
    except Exception as exc:  # the run ends with an (invalid) result whatever happens
        invalid_result(exc, metric="probes", n_items=N_ITEMS, grader_version=GRADER_VERSION, path=result)
        return 0
    outputs = zip(evaluation.outputs, hidden, strict=True)
    wrong = sum(output is not None and output != number for output, number in outputs)
    failures = evaluation.failures + wrong
    calls = [float(session.calls) for session in sessions]
    valid = failures == 0
    write_result(
        primary=Value("probes", sum(calls) / N_ITEMS if valid else None),
        n_items=N_ITEMS,
        failures=failures,
        failure_kinds=evaluation.failure_kinds() | ({"wrong": wrong} if wrong else {}),
        secondary={
            "query_bytes": float(sum(session.query_bytes for session in sessions)),
            "answer_bytes": float(sum(session.answer_bytes for session in sessions)),
        },
        item_scores=calls if valid else None,
        grader_version=GRADER_VERSION,
        path=result,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else RESULT_PATH))
