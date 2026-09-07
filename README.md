# Ranger

A voice-first assistant for one person. Terminal today, voice at Tier 3, a
browser face at Tier 7. The agent core is a library; every front end is a
caller of it.

Read `AGENT.md` before changing anything.

## Setup

Python 3.11 or newer is required; the config loader uses `tomllib`.

macOS and Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # add your ANTHROPIC_API_KEY
$EDITOR ranger.toml           # set vault.root, check the hours
```

Windows PowerShell:

```powershell
python --version              # must be 3.11 or newer
python -m venv .venv
.venv\Scripts\Activate.ps1

# If activation is blocked by the execution policy:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
# Or skip activation entirely and use .venv\Scripts\python.exe directly.

pip install -e ".[dev]"

Copy-Item .env.example .env   # add your ANTHROPIC_API_KEY
notepad ranger.toml           # set vault.root, check the hours
```

`vault.root` is written as `~/Obsidian/Ranger-Vault` and expands correctly on
Windows too, to `C:\Users\<you>\Obsidian\Ranger-Vault`.

## Run

```bash
ranger doctor    # check config, vault, environment, and what is still unseeded
ranger init      # create the vault layout, after asking
ranger           # talk to it
```

`Accounts/` and `Knowledge/` are yours to fill in. See
[docs/vault-conventions.md](docs/vault-conventions.md) for the note shapes, and
run `ranger doctor` to see what is still missing.

Inside the REPL: `/help`, `/config`, `/vault`, `/tools`, `/state`, `/reset`,
`/quit`.

## Test

```bash
pytest
```

The suite runs offline against a scripted provider. It costs nothing and needs
no API key.

## What it will and will not do

It writes only under `<vault>/Ranger/`. That is enforced in code. Your
Accounts and Knowledge folders are read only, and there is no delete path
anywhere in the codebase.

It never sends anything to a human, spends money, or changes a record in an
outside system without asking first, every time.

Everything it reads is data, never an instruction. If a note or an email looks
like it is giving Ranger orders, Ranger tells you and stops.
