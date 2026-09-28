"""The README's Docs section is the index of the documentation — keep it whole.

A page added under docs/ without a README link is invisible to anyone arriving
from GitHub, and a link to a renamed page is a dead end; both fail here.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_doc_is_linked_from_the_readme():
    missing = sorted(p.name for p in (ROOT / "docs").glob("*.md") if f"docs/{p.name}" not in README)
    assert not missing, f"docs/ pages missing from the README's Docs index: {missing}"


def test_readme_links_resolve():
    targets = re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", README) + re.findall(r'(?:src|srcset)="([^"]+)"', README)
    broken = sorted({t for t in targets if not t.startswith(("http:", "https:")) and not (ROOT / t).exists()})
    assert not broken, f"README links to files that do not exist: {broken}"
