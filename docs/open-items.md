# Open items

Things that are known not to work, with what was tried, so they are not
rediscovered from scratch. An item leaves this file when it works or when it is
abandoned on purpose.

---

## The spoken dismissal does not minimise the window

**Status: parked 2026-09-08. Everything except the minimise works.**

Saying "that's all Jarvis" is recognised, the window stays armed, and the
window does not go away.

### What is known to work

- **The phrase matches.** The trailing-clause rule fires on all four real
  transcripts, including the one ending in a stray "So".
  `tests/test_dismissal.py` holds them verbatim.
- **Surfacing on the same window handle works.** Confirmed on the machine:
  `ranger doctor` reports *the interface window is findable: 'Jarvis'*,
  `SW_RESTORE` un-minimises, and a refused foreground degrades to a flashing
  taskbar button. So `_find_window` resolves a real HWND and `ShowWindow` on it
  does something.
- **`window.blur()` from the page does nothing** in Chrome's app mode. That was
  the first attempt and it was removed; a test asserts it has not come back.

### What is not known

Whether `minimise_window` is being reached at all, and if so what it returns.
The audit line would say — `hands-free dismissed minimised`, `already_minimised`
or `failed`, each with a reason — and **the operator's log paste was empty**, so
it is not known whether there is a `dismissed` line at all. That single line
splits the problem in two:

- **A `dismissed` line exists, with outcome `failed`.** Then the HWND resolved
  and `ShowWindow(SW_MINIMIZE)` returned without `IsIconic` becoming true. That
  would be surprising: minimising is not foreground-gated, which is the whole
  reason this path was chosen over the browser. Next step would be checking
  whether Chrome's app window responds to `SW_MINIMIZE` differently from
  `SW_RESTORE`, and trying `SW_FORCEMINIMIZE`.
- **There is no `dismissed` line at all.** Then `_dismissed` never ran, and the
  phrase never reached it — which would mean the failure is upstream of the
  minimise, in the transcript path rather than in Win32. Candidates: the
  utterance is not arriving as a hands-free turn, or `_run_audio` is not
  reaching the dismissal check, or `wake.phrase` in the live config is not what
  the dismissal names are derived from.

**Do not guess between these two.** They have nothing in common and the log
line settles it in one look.

### How to get the answer

```powershell
ranger log | Select-String dismissed
```

Nothing at all is itself the answer, and it is the more interesting one.

### What was ruled out

- Not the window handle: surfacing finds it and acts on it.
- Not the phrase rule: it is tested against the real transcripts.
- Not `window.blur()`: that path is gone.
- Not foreground refusal: `SW_MINIMIZE` does not need foreground rights.
