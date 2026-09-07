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

cp .env.example .env                          # add your ANTHROPIC_API_KEY
cp ranger.local.toml.example ranger.local.toml # your settings, git-ignored
$EDITOR ranger.local.toml                      # vault.root, voice, devices
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

Copy-Item .env.example .env
Copy-Item ranger.local.toml.example ranger.local.toml
notepad .env                  # add your ANTHROPIC_API_KEY
notepad ranger.local.toml     # your voice, your devices. Git-ignored
```

**Put your own settings in `ranger.local.toml`, not `ranger.toml`.**
`ranger.toml` is tracked and changes as Ranger is built, so editing it means a
merge conflict on every pull. The local file is git-ignored and overrides the
tracked one key by key; the rest of each section still comes from `ranger.toml`.
`ranger doctor` lists which values it overrode.

`vault.root` is written as `~/Obsidian/Ranger-Vault` and expands correctly on
Windows too, to `C:\Users\<you>\Obsidian\Ranger-Vault`.

## Pulling an update

Most of the time a `git pull` is the whole job. The install is editable, so
Python code, the front end under `ranger/web/` and the vendored assets are all
read straight from the working tree.

Re-run `pip install -e ".[dev]"` only when one of these changed:

- the dependencies or the optional dev extras
- the `[project.scripts]` entry point
- the Python version the venv was built against

**Stop `ranger ui` before you reinstall.** A running server holds
`.venv\Scripts\ranger.exe` open, and pip deletes the old package before it
writes the new one. On Windows that fails with `WinError 32, the process cannot
access the file because it is being used by another process` **after** the
uninstall step, which leaves no package installed at all and makes the next
`pytest` die with `ModuleNotFoundError: No module named 'ranger'`.

If that has already happened: stop the server, then run the same install again.
It succeeds the second time and nothing is lost.

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

## Voice (Tier 3a)

```powershell
pip install sounddevice numpy soundfile pynput   # nothing here compiles
ranger audio devices     # what PortAudio sees, and which ones Ranger will use
ranger audio check       # record 3 seconds, measure it, play it back
ranger audio check --hold --keep out.wav
```

Do **not** install PyAudio: it has no wheel for Python 3.14 and builds from
source. `sounddevice` bundles PortAudio in its platform wheel.

Set `voice.input_device` to part of a device name rather than an index.
Indices move when a USB microphone or a headset connects.

On Windows, if the recording comes back as pure silence, the cause is almost
always Settings, Privacy and security, Microphone, "Let desktop apps access
your microphone".

## Transcription (Tier 3b)

```powershell
ranger audio check --keep out.wav
ranger transcribe out.wav
ranger transcribe out.wav --expect "where are we on Illes Foods"
ranger transcribe out.wav --no-hints --expect "where are we on Illes Foods"
```

`--expect` prints exactly which words came back wrong and a word error rate, so
mishearing is visible rather than inferred. Running the same file with and
without `--no-hints` is how you find out whether `stt.keyterms` is earning its
keep.

Vocabulary hints live in `ranger.toml` under `[stt] keyterms`. The parameter
Deepgram wants depends on the model, and the code picks it: `keyterm` for
nova-3, `keywords` for nova-2 and earlier. Change `stt.model`, not the
parameter.

## Speech (Tier 3c)

```powershell
ranger voices                 # what the account has
# paste an id into tts.voice_id in ranger.toml
ranger say "Rod owes you confirmed volumes before you can price the changeover."
ranger say "..." --voice <other-id> --keep sample.wav
```

`ranger keyterms` shows the vocabulary hints that would be sent to Deepgram,
ranked, with whatever did not fit the cap.

Ranger touches exactly two ElevenLabs endpoints, so a key scoped to Text to
Speech plus Voices read only is enough.

## The full loop (Tier 3d)

```powershell
ranger --voice
```

Hold the key, talk, release. It shows what it heard, answers, and speaks while
the rest of the reply is still being written. Press again while it is talking
to cut in; that same press starts your next turn.

`ranger` with no flags is still the typed REPL, and always will be.

## Memory (Tier 4)

```powershell
ranger memory      # what is remembered, and the budget split
```

Plain markdown in `Ranger/memory/facts.md`. Edit or delete a line in Obsidian
and Ranger reads the change on the next turn. Facts about companies belong in
the account note, not here.

## The heartbeat (Tier 5)

```powershell
ranger heartbeat            # the loop. Ctrl-C to stop
ranger heartbeat --once     # one pass, saying why each check did or did not run
ranger heartbeat --once --force morning   # run it now, whatever the clock says
ranger inbox                # what is waiting
ranger inbox 1              # dismiss the first, cleared in the vault
ranger inbox --all --full   # including dismissed, in full
```

Notices are markdown in `Ranger/inbox/`, read them in Obsidian. Nothing
interrupts; `ranger` and `ranger --voice` say what is waiting when you start
them.

**If the laptop is asleep at 07:00 the check catches up when it wakes.** The
inbox is the schedule: today's notice is either there or it is not. Being away
for a week does not replay six mornings.

## The rails (Tier 6)

```powershell
ranger pause      # stop everything proactive. You can still talk to Ranger
ranger resume
ranger log        # the audit trail for today
ranger log 2026-09-06
```

Anything consequential stops and asks, every time, stating plainly what it is
about to do. Approving one action never pre-authorises the next.

In voice mode a spoken yes is **not** taken as consent: the action is held, a
notice goes to `Ranger/inbox/`, and you approve it at a keyboard. The same
mechanism covers anything the heartbeat starts while you are not there.

## Test

```bash
pytest
```

Run it as bare `pytest`, not `python -m pytest`. The latter puts the working
directory on `sys.path`, which hides import mistakes the real invocation
catches.

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
