"""Search-quality baseline: recall@5 / MRR for relay's *existing* FTS5 search
against a golden query set, scored on the real vault (see conftest.py).

This is a measurement tool, not a gate — relay post #253's own framing is that
a low score is itself the useful result (evidence for building hybrid search),
not a test failure. No threshold is asserted beyond "the run produced valid
numbers". Report is keyed by search mode so a later phase adding
semantic/hybrid modes extends this without a rewrite — only "keyword" exists
today.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from relay import service

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


@pytest.mark.asyncio
@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason="tests/eval/golden.yaml missing (gitignored, personal) — copy golden.example.yaml",
)
async def test_fts5_baseline_recall(live_snapshot):
    golden = yaml.safe_load(GOLDEN_PATH.read_text())
    db = live_snapshot

    recalls: list[float] = []
    reciprocal_ranks: list[float] = []
    print("\n--- eval: keyword (FTS5) mode ---")
    for entry in golden:
        query = entry["query"]
        expected = set(entry["expect"])
        result = await service.list_posts(db, search=query, limit=5, summary=True)
        retrieved = [item.id for item in result.items]

        recall = _recall_at_5(expected, retrieved)
        rr = _reciprocal_rank(expected, retrieved)
        recalls.append(recall)
        reciprocal_ranks.append(rr)
        print(f"  recall@5={recall:.2f} rr={rr:.2f}  {query!r} expected={sorted(expected)} got={retrieved}")

    mean_recall = sum(recalls) / len(recalls)
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
    print(f"--- keyword: mean recall@5={mean_recall:.3f}  MRR={mrr:.3f} over {len(golden)} queries ---")

    assert 0.0 <= mean_recall <= 1.0
    assert 0.0 <= mrr <= 1.0
