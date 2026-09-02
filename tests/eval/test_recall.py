"""Search-quality: recall@5 / MRR per mode for relay's search, scored on the
real vault (see conftest.py). ``keyword`` was relay #253 phase 1's baseline
(v1.0.0: recall@5=0.540, MRR=0.414). ``semantic``/``hybrid`` are phases 2-4's
proof of concept — this is the number that answers the post's own success
criterion: did hybrid clearly beat keyword?

This is a measurement tool, not a gate. No threshold is asserted beyond "the
run produced valid numbers" — a hybrid mode that doesn't clearly beat keyword
is a real result (the post's own framing), not a test failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from relay import database, service

pytestmark = pytest.mark.eval

GOLDEN_PATH = Path(__file__).parent / "golden.yaml"


def _recall_at_5(expected: set[int], retrieved: list[int]) -> float:
    top5 = retrieved[:5]
    return len(expected & set(top5)) / len(expected)


def _reciprocal_rank(expected: set[int], retrieved: list[int]) -> float:
    for rank, post_id in enumerate(retrieved[:5], start=1):
        if post_id in expected:
            return 1 / rank
    return 0.0


async def _score_mode(db, golden: list[dict], mode: str) -> tuple[float, float]:
    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    print(f"\n--- eval: {mode} mode ---")
    for entry in golden:
        query = entry["query"]
        expected = set(entry["expect"])
        result = await service.list_posts(db, search=query, limit=5, summary=True, mode=mode)
        retrieved = [item.id for item in result.items]

        recall = _recall_at_5(expected, retrieved)
        rr = _reciprocal_rank(expected, retrieved)
        recalls.append(recall)
        reciprocal_ranks.append(rr)
        print(f"  recall@5={recall:.2f} rr={rr:.2f}  {query!r} expected={sorted(expected)} got={retrieved}")

    mean_recall = sum(recalls) / len(recalls)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    print(f"--- {mode}: mean recall@5={mean_recall:.3f}  MRR={mrr:.3f} over {len(golden)} queries ---")
    return mean_recall, mrr


@pytest.mark.asyncio
@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="tests/eval/golden.yaml missing (gitignored, personal) — copy golden.example.yaml",
)
async def test_recall_per_mode(live_snapshot):
    golden = yaml.safe_load(GOLDEN_PATH.read_text())
    db = live_snapshot

    scores: dict[str, tuple[float, float]] = {}
    scores["keyword"] = await _score_mode(db, golden, "keyword")

    if database.VEC_ENABLED:
        scores["semantic"] = await _score_mode(db, golden, "semantic")
        scores["hybrid"] = await _score_mode(db, golden, "hybrid")
    else:
        print("\n--- sqlite-vec unavailable in this environment — semantic/hybrid skipped ---")

    print("\n--- summary ---")
    for mode, (recall, mrr) in scores.items():
        print(f"  {mode:9s} recall@5={recall:.3f}  MRR={mrr:.3f}")

    for recall, mrr in scores.values():
        assert 0.0 <= recall <= 1.0
        assert 0.0 <= mrr <= 1.0
