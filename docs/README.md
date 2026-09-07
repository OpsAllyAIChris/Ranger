# docs

The build spec is `start-here.md` at the repo root, and `ranger-master-prompt.md`
belongs beside it. `start-here.md` arrived after Tier 1 was first written, so
Tier 1 was reconciled against it afterwards: the tier structure matched, and the
resilience requirement it names (handle a slow or unreachable model with a clear
message and a clean prompt, never a stack trace) was added in a follow-up commit.

If anything in `AGENT.md` ever disagrees with `start-here.md`, `start-here.md`
wins and `AGENT.md` is the thing to correct.
