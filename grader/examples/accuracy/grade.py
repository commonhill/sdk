"""Accuracy-style example grader: how many held-out texts does the worker label correctly?

The evaluation file (`/eval/labels.jsonl` in a project; the public sample locally, the held-out
set on the platform) has one {"input": text, "label": "positive" | "negative"} per line. The
worker sees only the texts, in an order drawn from the run's seed; the labels never leave the
grader. The score is the fraction labelled correctly (`accuracy`, maximize). A wrong label just
lowers the score; an item the worker failed to answer at all counts as wrong, and if more than
5% failed, the run is invalid (the worker was broken, not just inaccurate).

    WORKER_URL=http://127.0.0.1:8080 python grade.py labels.jsonl [result.json]
"""

import json
import os
import sys
from pathlib import Path

from arh_grader import RESULT_PATH, Client, Value, invalid_result, rng, wait_ready, write_result

GRADER_VERSION = "accuracy-example-1"
BATCH_SIZE = 20
MAX_FAILED_FRACTION = 0.05  # the author's threshold for an invalid run
OUTPUT_SCHEMA = {"enum": ["positive", "negative"]}


def load(path: Path) -> list[dict[str, str]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(eval_items: Path, result: Path = RESULT_PATH) -> int:
    items: list[dict[str, str]] = []
    try:
        worker_url = os.environ["WORKER_URL"]
        items = load(eval_items)
        rng("order").shuffle(items)
        wait_ready(worker_url)  # runner.startup_timeout_s, from the platform (AR_STARTUP_TIMEOUT_S)
        evaluation = Client(worker_url, output_schema=OUTPUT_SCHEMA).evaluate(
            [item["input"] for item in items], BATCH_SIZE
        )
    except Exception as exc:  # the run ends with an (invalid) result whatever happens
        invalid_result(exc, metric="accuracy", n_items=len(items), grader_version=GRADER_VERSION, path=result)
        return 0
    correct = sum(output == item["label"] for output, item in zip(evaluation.outputs, items, strict=True))
    valid = evaluation.failures <= MAX_FAILED_FRACTION * len(items)
    write_result(
        primary=Value("accuracy", correct / len(items) if valid and items else None),
        n_items=len(items),
        failures=evaluation.failures,
        failure_kinds=evaluation.failure_kinds(),
        secondary={"wrong": float(len(items) - evaluation.failures - correct)},
        timings=evaluation.timings.as_result(),
        grader_version=GRADER_VERSION,
        path=result,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else RESULT_PATH))
