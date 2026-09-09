# Documents, and the preview

Item D and D2, built together because they are one piece of work: the preview
renders from the generated file, so the file has to exist before there is
anything to preview.

## Generation: one seam, three writers

A `Spec` is a title and a list of blocks — headings, paragraphs, bullets,
tables — and each writer renders those blocks in its own idiom. Tables become
sheets in a workbook and stay tables in the other two. One model is what lets
`ranger gp export` and a model-written proposal go through the same code, and
it means a rendering bug has one place to be fixed rather than three.

Everything lands in `Ranger/drafts/`, beside the markdown drafts. **A generated
document is a draft that happens not to be text.** It lists with them, it
clears with them, it is never sent, and it is never overwritten: regenerating
produces a new file beside the old one, so the version that went to a customer
is still the version on disk.

Ungated, the same as `draft_and_hold`, and for the same reason: nothing leaves
the machine. The preview is the review.

Three libraries, installed as an extra rather than a dependency, the same shape
as hands free:

```
pip install python-docx openpyxl reportlab
```

`ranger doctor` reports which are present. `write_document` refuses a format it
cannot write and names the package to install; it never produces a file that is
not one.

## The preview rule: preview the artifact, not the intention

`ranger/preview.py` opens the file that was written and reads what is in it.
Nothing previews the content the model produced before it became a document. A
preview built upstream of generation agrees with the export right up until it
does not, and the place that disagreement surfaces is in front of a customer.

`test_the_preview_reads_the_file_and_not_the_content_it_was_made_from` is the
test that holds the rule: it writes a document, edits the bytes on disk, and
requires the preview to show what is in the file.

What each preview honestly is, and each says so in the chrome:

| Format | What you are looking at |
| ------ | ----------------------- |
| `.pdf` | **The real PDF.** The browser's own viewer, the bytes on disk, every page. Exact. |
| `.docx` | **An approximation.** Text, headings, bullets and table cells, in order, read out of `word/document.xml`. Not fonts, not spacing, not page breaks. |
| `.xlsx` | **Sheet values only.** Formulas are not shown, and neither are formats, widths or charts. |

The `.docx` caveat is on screen at all times, not in a tooltip. It is the
difference between a preview and a claim about Word.

For `.xlsx`, a formula's cached value is not shown either: it is whatever was
true when the file was last opened by something that calculates. Files this
repository writes contain no formulas at all; one that came from elsewhere gets
its formula cells counted and left blank, with a line saying how many.

## You cannot validate with the library that wrote the file

python-docx reading back what python-docx wrote proves python-docx is
self-consistent, which was never in question. The consumers are Word and Excel
and neither can be run here. So:

- The tests unzip the OOXML and assert on the parts — `word/document.xml`,
  `xl/workbook.xml`, the sheet XML.
- The PDF is read back with `pypdf`, which did not write it.
- `preview.py` is stdlib only — `zipfile` and `ElementTree`, no docx, no
  openpyxl, no lxml — and a test walks its import graph to keep it that way.
  When the preview agrees with the file, that is evidence about the file.

**The operator opening it in the real application is the only green light, and
that is once per template rather than once per document.**

## Three real holes in this path

Content in these documents comes from account notes and pasted customer email.
Each of these has a test, and one of the tests demonstrates the hazard rather
than asserting on our own behaviour.

**Excel formula injection.** openpyxl turns any value starting with `=`, `+`,
`-` or `@` into a live formula. A note containing `=HYPERLINK(...)` would become
a live formula in a workbook the operator sends back to the customer it came
from. Every cell is written with its data type forced to string, so Excel shows
the characters and evaluates nothing. The count of cells this applied to is
reported to the operator rather than handled silently.

**reportlab markup.** `Paragraph` parses a small HTML-like markup that includes
`<img src="...">`, and reportlab opens that path when the page renders.
`test_reportlab_would_have_read_a_file_off_disk` builds an unescaped paragraph
and watches it read a file, so the escaping in the writer is a control with
evidence behind it rather than tidiness.

**Word field codes.** Nothing writes `w:fldChar` or `w:instrText`, and a test
asserts the generated XML has none, so pasted text cannot arrive as a DDE or
link field.

Instruction-shaped language is scanned for and reported at generation time,
never edited out — this path never silently changes what it was asked to write
— and anything read back out of a generated document is fenced as untrusted,
because Jarvis having written the file does not make its contents trusted.
`tests/test_planted_instructions.py` covers the document path through a real
turn.

## The three design questions

### Where the preview lives

**A third place: a sheet on the left, over the starfield, and the orb steps
aside.** Not in the activity panel, which is 260px and holds rows; a document
does not fit in it. Not over the orb, because the orb is the one thing on
screen that says what state Jarvis is in and that meaning stays unmuddied.

While the preview is open, `orb.setOffset()` slides the orb to the right in the
scene so it is never covered, the response cards shift out of the way, and the
activity panel keeps its place. Escape closes the preview — third in the escape
order, after killing hands free and after answering an open card, so it can
never take the microphone's escape.

### What happens with a forty-page document

**It scrolls, and what is not drawn is stated.**

- A PDF is the browser's viewer: forty pages, scrolling, all of it, and the
  page count comes from the file.
- `.docx` and `.xlsx` are capped at `documents.preview_blocks` (400) and
  `documents.preview_rows` (300), and the preview says "showing 400 of 1,240
  blocks. Open the file itself for the rest." A silent truncation would be a
  document that looks complete and is not, which is the same failure as a stale
  figure that looks current.

The caps are config because they are about the machine, not the document: this
window sits open during customer calls, and building ten thousand table rows
into the page mid-call is not a trade worth making by default.

### How the file gets out

**Both, because they are different acts.**

- **Show in folder** opens the file manager with the file selected
  (`explorer /select,` on Windows). It is the one the operator wants when the
  next step is dragging it into an email.
- **Download** hands a copy through the browser, from the same
  `/document/<vault path>` route the PDF preview uses.

Neither opens Word. Launching an application on the operator's machine is their
decision to make by double-clicking, not something a button in a browser does
to them. On a machine with no file manager, "show in folder" says so and points
at the download.

That route serves exactly three kinds of file, from exactly one folder. The
drafts folder also holds markdown that quotes customer email; none of that is
reachable, and everything refused is a 404 so a probe learns nothing.

## The assembly animation

Particles leaving the starfield and coalescing into the shape of a page where
the preview sheet is about to be.

- **It never gates the content.** The preview opens in the same frame the
  particles start. Any click, scroll or key ends the animation immediately.
  It plays once per document and never on a reopen — enforced in two places,
  the shell's `seenDocuments` and the server marking a reopen as no-assembly.
  `prefers-reduced-motion` skips it entirely. A tax paid on every document
  forever is not a delight.
- **It never lies about state.** The animation is started by the `document`
  event and by nothing else, and the server sends that event only after the
  write returned a path that is on disk right now. A generation that failed
  draws nothing at all; the failure renders as a failure. This is the
  empty-ring-over-a-live-microphone rule applied to a flourish.
- **It is not orange.** Orange means the microphone is live and keeps one
  meaning. These are the nebula's own teal and blues, and a test reads the
  colours out of the source and fails if any of them is red-dominant.

### What to watch

The particle count is `documents.assembly_particles`, 220 by default, capped at
600 in the scene itself whatever config says. The particles are one extra
`THREE.Points` draw call for about 1.4 seconds, with a per-frame position
update on the CPU — cheap, but not free on a laptop that is also encoding a
Teams screen share.

Watch, in this order:

1. Open the window with `?debug` and read the frame rate while a document
   lands. If it dips below about 45fps, halve `assembly_particles`.
2. Land a document **during** a screen share and watch whether Teams starts
   dropping frames. That is the case that matters and the one this cannot be
   tested for here.
3. If either is bad, `assembly = false` turns it off entirely and nothing else
   changes: the preview is the point, the particles are not.

## GP out of the vault

`ranger gp export` writes the ledger through the same seam, `.xlsx` by default.
Values only, no formulas, so the preview's formula caveat cannot bite on the
first document the operator opens — what the preview shows is the whole of what
is in the file. Three sheets: Summary, By month, and Superseded, because a
figure that was corrected is part of the record and a spreadsheet that quietly
omitted it would be the one place in this repository where something got thrown
away.

`--format docx` and `--format pdf` work too, through the same spec.

## Markdown drafts, and getting one into Outlook

Every file in `Ranger/drafts/` has a preview button, markdown included. A draft
is an email on its way somewhere, and the way to read one used to be: open
Obsidian, find the file. Same sheet as a .docx, same open and close, same orb
offset — one surface, not a second one that has to learn all of that again.

Clicking preview on a draft that is already open closes it. A preview that gets
opened often gets dismissed often, and reaching for the close button every time
is the friction that stops it being used. Escape closes it too, as before.

### The provenance line

Above the draft, never inside it: which file, when it was written, the account,
and what the draft says about itself. That is the front matter, split off
rather than rendered as content — **`status: draft, not sent` copied into an
email would be a bad day**, and the copy button takes what is in the body.

### Copy: plain text is the default

Two buttons on a markdown draft, and the order is the decision:

- **copy text** — the words, with markdown taken off. Bold, italic, code
  fences, headings and table pipes all go; a link keeps its target in
  parentheses; a table keeps tabs so it pastes into Word or Excel as cells.
- **copy markdown** — the file's own text, asterisks and all. Only offered
  where there is a source to copy; a .docx has no markdown and offering it
  would be inventing a format the file never had.

Plain text is first because a draft full of `**` and `#` landing in an Outlook
email is worse than having no button at all. Nobody notices their own asterisks
until the customer has them.

Two things the plain text does that a naive strip would not:

- **A wrapped paragraph comes back together.** A draft hard-wrapped at eighty
  characters must not paste into an email with a break every eighty characters.
- **A sign-off does not.** `Best,` / `Chris` is two lines on purpose. The
  length of the line before the break decides which it was — under 60
  characters is somebody ending a line, over it is wrapping. A heuristic, and
  wrong sometimes: a short line inside a wrapped paragraph keeps a break it did
  not ask for. That is the harmless direction. A stray break is a typo; a
  sign-off run onto one line is a draft that looks careless.

Markup is taken off conservatively. `GP_TOTAL_2026` keeps its underscores and
`2 * 3 * 4` keeps its asterisks, because an underscore inside a word is a
column header far more often than it is emphasis, and a mangled account name in
front of a customer is worse than a stray character.

If the clipboard is refused — it needs a secure context and can still be denied
— the button falls back to the older selection-based copy, and if that fails
too it says **press ctrl+C** rather than sitting there looking like it worked.

### Download

Markdown drafts download like documents do. That widened the `/document` route,
which had refused markdown on the grounds that drafts quote customer email. The
reasoning for the change: the .docx already served there is assembled from the
same account notes and the same pasted email, so the line was between
"generated" and "written", not between sensitive and not. What holds it in is
unchanged — 127.0.0.1, inside the drafts folder, and now a suffix list of four.

That list is written out in `server.py` rather than taken from `preview.KINDS`.
They used to be the same tuple, which meant teaching the preview a new format
would have put that format on the network with nobody deciding to.
