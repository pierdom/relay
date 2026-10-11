"""The TUI's tag-config modal (relay #198, B-2): it opens on the tag's current
config — saving replaces all of it, so an empty field would clear a rule the
user never touched — and carries keep_last."""
from __future__ import annotations

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from textual.app import App
from textual.widgets import Button, Input

from relay_tui import api
from relay_tui.widgets.modals import TagConfigModal


async def _run(current: api.Tag | None, edits: dict[str, str]) -> dict | None:
    result: dict = {}

    class Host(App):
        def on_mount(self) -> None:
            self.push_screen(TagConfigModal("digest", current), callback=lambda r: result.setdefault("r", r))

    async with Host().run_test() as pilot:
        await pilot.pause()
        modal = pilot.app.screen
        for field, value in edits.items():
            modal.query_one(f"#{field}", Input).value = value
        modal.query_one("#save-btn", Button).press()
        await pilot.pause()
    return result.get("r")


@pytest.mark.asyncio
async def test_the_modal_opens_on_the_current_config_and_keeps_what_it_did_not_touch():
    current = api.Tag(name="digest", count=9, ttl_hours=48, keep_last=7)
    assert await _run(current, {"ttl-input": "24"}) == {"ttl_hours": 24, "keep_last": 7, "expires_at": None}


@pytest.mark.asyncio
async def test_clearing_every_field_removes_the_config():
    current = api.Tag(name="digest", count=9, keep_last=7)
    assert await _run(current, {"keep-input": ""}) == {"ttl_hours": None, "keep_last": None, "expires_at": None}


@pytest.mark.asyncio
async def test_keep_last_must_be_a_positive_whole_number():
    assert await _run(None, {"keep-input": "0"}) is None   # refused: the modal stays open
    assert await _run(None, {"keep-input": "5"}) == {"ttl_hours": None, "keep_last": 5, "expires_at": None}


def test_tags_carry_their_config_from_the_api():
    tags = api._tags({"tags": [{"tag": "digest", "count": 3, "keep_last": 7}, {"tag": "plain", "count": 1}]})
    assert (tags[0].keep_last, tags[1].keep_last) == (7, None)
