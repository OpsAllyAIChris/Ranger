"""Tier 2 through the actual agent core.

The tools are exercised elsewhere in isolation. This drives them the way a real
turn does: core.turn -> provider asks for a tool -> registry runs it -> result
goes back -> core finishes. Only the model is scripted.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.core import Ranger
from ranger.events import Notice, TextDelta, ToolCalled, ToolFinished, TurnComplete
from ranger.testing import ScriptedProvider
from ranger.toolset import build_registry
from ranger.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"
TODAY = date(2026, 9, 7)


@pytest.fixture
def agent(vault_root, config):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    shutil.copy(FIXTURES / "_vault-build-report.md", vault_root / "_vault-build-report.md")

    vault = Vault(config.vault)

    def build(script):
        return Ranger(
            config=config,
            provider=ScriptedProvider(script),
            registry=build_registry(config, vault, today=lambda: TODAY),
            vault=vault,
        )

    return build


async def collect(ranger, text):
    return [event async for event in ranger.turn(text)]


async def test_a_recall_turn_end_to_end(agent):
    ranger = agent(
        [
            {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
            {"text": "Rod owes you confirmed volumes before you can price the changeover."},
        ]
    )
    events = await collect(ranger, "where are we on Illes Foods")

    assert [e.name for e in events if isinstance(e, ToolCalled)] == ["account_recall"]
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert finished.ok and "Illes Foods" in finished.summary

    reply = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert reply.startswith("Rod owes you")
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].tools_used == ("account_recall",)


async def test_the_digest_reaches_the_model_fenced_as_data(agent):
    ranger = agent(
        [
            {"tools": [{"name": "account_recall", "input": {"account": "Illes"}}]},
            {"text": "Answered."},
        ]
    )
    await collect(ranger, "where are we on Illes")

    tool_result = next(
        block
        for message in ranger.messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert "<untrusted_content" in tool_result["content"]


async def test_a_draft_turn_writes_into_the_vault(agent, config):
    ranger = agent(
        [
            {
                "tools": [
                    {
                        "name": "draft_and_hold",
                        "input": {
                            "title": "Follow up to Rusty on the SupplyBox timeline",
                            "body": "Rusty,\n\nAre you still aiming for October? Happy to "
                            "walk through it whenever suits.\n\nChris",
                            "account": "Rusty",
                        },
                    }
                ]
            },
            {"text": "Drafted and held. Have a look before you send it."},
        ]
    )
    events = await collect(ranger, "draft a follow up to Rusty about the SupplyBox timeline")

    written = list(config.vault.drafts.glob("*.md"))
    assert len(written) == 1
    assert "status: draft, not sent" in written[0].read_text(encoding="utf-8")
    reply = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "sent" not in reply.lower().replace("before you send", "")


async def test_a_quiet_turn_end_to_end(agent):
    ranger = agent(
        [
            {"tools": [{"name": "what_went_quiet", "input": {}}]},
            {"text": "Rusty Supply Co at 180 days and Pegasus Logistics at 97."},
        ]
    )
    events = await collect(ranger, "what went quiet")
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    # The default is the brief, not the full list: a morning has a size.
    assert "slipping" in finished.summary and "withheld" in finished.summary


async def test_the_model_is_told_to_ask_when_a_name_is_ambiguous(agent):
    ranger = agent(
        [
            {"tools": [{"name": "account_recall", "input": {"account": "Pegasus"}}]},
            {"text": "Two accounts match Pegasus. Packaging or Logistics?"},
        ]
    )
    events = await collect(ranger, "where are we on Pegasus")
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert "needs the operator" in finished.summary


async def test_a_rejected_draft_comes_back_for_a_rewrite(agent, config):
    """The dash rule is enforced in code, so the model gets a second go."""
    ranger = agent(
        [
            {
                "tools": [
                    {
                        "name": "draft_and_hold",
                        "input": {"title": "Follow up", "body": "Rusty — quick one."},
                    }
                ]
            },
            {
                "tools": [
                    {
                        "name": "draft_and_hold",
                        "input": {"title": "Follow up", "body": "Rusty, quick one."},
                    }
                ]
            },
            {"text": "Drafted."},
        ]
    )
    events = await collect(ranger, "draft a follow up to Rusty")

    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished[0].ok is False and "writing rules" in finished[0].summary
    assert finished[1].ok is True
    assert len(list(config.vault.drafts.glob("*.md"))) == 1


async def test_an_empty_knowledge_folder_is_silent(agent):
    """No warning on every startup. The prompt says so once instead."""
    ranger = agent([{"text": "ok"}])
    events = await collect(ranger, "hello")

    assert [e for e in events if isinstance(e, Notice)] == []
    assert "The Knowledge folder is empty" in ranger.system_prompt()


async def test_the_tools_are_described_in_the_system_prompt(agent):
    ranger = agent([{"text": "ok"}])
    prompt = ranger.system_prompt()
    for name in ("account_recall", "draft_and_hold", "what_went_quiet"):
        assert name in prompt
    assert "Never pick one yourself" in prompt
