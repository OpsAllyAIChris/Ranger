"""Filing what the operator said, and dating everything else.

The failure was real and was not invention. Asked to file "we are waiting on
their reply", Jarvis filed:

    Waiting on their reply. Countered board's 3,000 MOQ ask with 5,000 at 60/40
    terms; ball is in their court.

Every figure in that is true. None of it came from the operator, all of it came
from months earlier, and it is filed in the present tense with no date. **That
is worse than a wrong fact, because it is credible and it compounds**: the note
sits below the marker, is read every day, and becomes the input to everything
concluded later.

So: the note is what the operator said. Anything added is separate, dated and
sourced. And the enforceable half of that -- a figure in the note with no date
-- is refused rather than written, because a quantity came from somewhere and
somewhere had a date.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from ranger.timeblind import audit_notes
from ranger.toolset import build_registry
from ranger.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"

#: The note from the real run, verbatim.
FILED = (
    "Waiting on their reply. Countered board's 3,000 MOQ ask with 5,000 at "
    "60/40 terms; ball is in their court."
)


@pytest.fixture
def accounts(config, vault_root):
    for name in ("Accounts", "Knowledge"):
        shutil.rmtree(vault_root / name, ignore_errors=True)
        shutil.copytree(FIXTURES / name, vault_root / name)
    from ranger.marker import migrate

    migrate(None, config.vault.accounts)
    return config


@pytest.fixture
def registry(accounts):
    return build_registry(accounts, Vault(accounts.vault), today=lambda: date(2026, 9, 9))


def note_text(config, name="Illes Foods"):
    return (config.vault.accounts / f"{name}.md").read_text(encoding="utf-8")


# -- the note is what the operator said ------------------------------------


async def test_the_note_that_was_actually_filed_is_refused(registry, accounts):
    """**The real one, verbatim.** Two figures the operator never said."""
    result = await registry.run(
        "file_to_account", {"account": "Illes", "note": FILED}
    )

    assert not result.ok
    assert "3,000" in result.content and "5,000" in result.content
    assert "not dated" in result.content
    assert "`context`" in result.content, "and it says where they should have gone"
    assert "### 2026-09-09" not in note_text(accounts), "nothing was written"


async def test_what_the_operator_actually_asked_for_files_cleanly(registry, accounts):
    """One sentence in, one sentence filed. No elaboration to strip out."""
    result = await registry.run(
        "file_to_account",
        {"account": "Illes", "note": "Waiting on their reply.", "source": "Chris"},
    )

    assert result.ok
    text = note_text(accounts)
    assert "### 2026-09-09 | Waiting on their reply. | Chris" in text
    assert "MOQ" not in text


async def test_retrieved_history_goes_underneath_the_note_with_its_date(
    registry, accounts
):
    """**What the operator said and what Jarvis added are not one sentence.**
    A blended paragraph reads as a single statement from them, and the added
    half is the half nobody can check."""
    result = await registry.run("file_to_account", {
        "account": "Illes",
        "note": "Waiting on their reply.",
        "source": "Chris",
        "context": [{
            "what": "countered the 3,000 MOQ ask with 5,000 at 60/40 terms",
            "when": "June 2026",
            "source": "the account note",
        }],
    })

    assert result.ok
    lines = [line for line in note_text(accounts).splitlines() if line.strip()]
    said = lines[-2]
    added = lines[-1]
    assert said == "### 2026-09-09 | Waiting on their reply. | Chris"
    assert added == (
        "- June 2026: countered the 3,000 MOQ ask with 5,000 at 60/40 terms "
        "(the account note)"
    )


async def test_context_without_a_date_is_refused(registry, accounts):
    """A line with no date is read as the state of the account today."""
    result = await registry.run("file_to_account", {
        "account": "Illes",
        "note": "Waiting on their reply.",
        "context": [{"what": "the ball is in their court", "when": ""}],
    })

    assert not result.ok
    assert "has none" in result.content
    assert "leave it out" in result.content
    assert "### 2026-09-09" not in note_text(accounts)


@pytest.mark.parametrize(
    "note",
    [
        "Countered at 3,000 MOQ.",
        "They want 5,000 units.",
        "Held at $12,400 for the quarter.",
        "Margin is 24.5% on the last order.",
    ],
)
async def test_a_figure_in_the_note_is_always_refused(registry, accounts, note):
    result = await registry.run("file_to_account", {"account": "Illes", "note": note})
    assert not result.ok
    assert "not dated" in result.content


@pytest.mark.parametrize(
    "note",
    [
        "Waiting on their reply.",
        "Rod is chasing procurement this week.",
        "They asked for a revised quote.",
    ],
)
async def test_an_ordinary_note_is_not_refused(registry, accounts, note):
    """The guard has to be quiet on the notes the operator actually dictates,
    or it becomes something to work around."""
    result = await registry.run("file_to_account", {"account": "Illes", "note": note})
    assert result.ok, result.content


async def test_the_rule_is_the_same_in_the_window(registry, accounts):
    """This is a property of the tool, so it holds for every caller. The window
    files gate-free; it does not file undated figures."""
    from ranger.bridge import Session

    result = await registry.run("file_to_account", {"account": "Illes", "note": FILED})
    assert not result.ok, "the tool refuses it, whoever is calling"


# -- what the prompt says ---------------------------------------------------


def test_the_prompt_carries_the_rule_and_the_counterexample(config, vault):
    from ranger.core import Ranger
    from ranger.testing import ScriptedProvider

    prompt = Ranger(
        config=config, provider=ScriptedProvider([]),
        registry=build_registry(config, vault), vault=vault,
    ).system_prompt()
    flat = " ".join(prompt.split())

    assert "What the operator said is the note" in flat
    assert "Do not fill it out, round it up, or make it read better" in flat
    assert "Present tense only for what was observed now" in flat
    assert "If you cannot source it, leave it out" in flat
    # The counterexample, in the operator's own case.
    assert "In June they countered at 3,000 MOQ" in flat
    assert "Ball is in their court" in flat


def test_the_tool_description_no_longer_invites_elaboration(config, vault):
    """**The phrase that invited it**, quoted so it cannot come back:

        "Written for the operator to read in six months, not for you to read
        back."

    That is an instruction to add background so it makes sense later, which is
    exactly what produced a months-old fact filed as today's state.
    """
    tool = [t for t in build_registry(config, vault) if t.name == "file_to_account"][0]
    text = tool.description + str(tool.input_schema)

    assert "read in six months" not in text
    assert "Transcribe, do not elaborate" in tool.description
    assert "no figures they did not say" in text


# -- reading back what is already there ------------------------------------


def filed(config, entries):
    """Put entries below the marker by hand, as earlier runs would have."""
    path = config.vault.accounts / "Illes Foods.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n" + "\n".join(entries) + "\n", encoding="utf-8")


def test_the_audit_finds_the_note_that_started_this(accounts):
    filed(accounts, [f"### 2026-09-09 | {FILED} | Jarvis"])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert report.entries == 1
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.account == "Illes Foods"
    assert "3,000" in finding.figures
    assert "no date" in finding.reason


def test_the_audit_leaves_a_dated_line_alone(accounts):
    filed(accounts, [
        "### 2026-09-09 | In June 2026 they countered at 3,000 MOQ. | Jarvis",
    ])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert report.findings == [], "a figure with its date is the shape that is fine"


def test_the_audit_flags_jarvis_asserting_the_present_with_no_date(accounts):
    filed(accounts, ["### 2026-09-09 | The ball is in their court. | Jarvis"])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert len(report.findings) == 1
    assert "present tense" in report.findings[0].reason


def test_the_audit_does_not_flag_the_operator_saying_what_is_true_now(accounts):
    """**Attribution is what separates the two.** "Waiting on their reply",
    sourced to Chris, is a person saying what is true today and the entry's
    date covers it. An audit that flags that is noise, and noise is how an
    audit stops being read."""
    filed(accounts, ["### 2026-09-09 | Waiting on their reply. | Chris"])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert report.findings == []


def test_a_figure_with_no_date_is_flagged_whoever_said_it(accounts):
    filed(accounts, ["### 2026-09-09 | They countered at 3,000 MOQ. | Chris"])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert len(report.findings) == 1
    assert "3,000" in report.findings[0].figures


def test_the_audit_writes_nothing(accounts):
    """Delete-never, and edit-never: a correction is a new dated entry written
    by a person who knows what was actually true."""
    filed(accounts, [f"### 2026-09-09 | {FILED} | Jarvis"])
    path = accounts.vault.accounts / "Illes Foods.md"
    before = path.read_bytes()

    audit_notes(Vault(accounts.vault), accounts)

    assert path.read_bytes() == before


def test_the_audit_reports_what_it_looked_at(accounts):
    filed(accounts, [
        "### 2026-09-08 | Rod asked for a revised quote. | Chris",
        f"### 2026-09-09 | {FILED} | Jarvis",
    ])
    report = audit_notes(Vault(accounts.vault), accounts)

    assert "2 entries across 1 accounts" in report.summary()
    assert "1 worth looking at" in report.summary()


def test_the_cli_audit_says_nothing_was_changed(accounts, capsys):
    from ranger.cli import cmd_accounts_audit

    filed(accounts, [f"### 2026-09-09 | {FILED} | Jarvis"])

    class Args:
        accounts_command = "audit"

    assert cmd_accounts_audit(accounts, Args()) == 1
    out = capsys.readouterr().out

    assert "Illes Foods" in out
    assert "Nothing has been changed" in out
    assert "a new dated entry that" in out
