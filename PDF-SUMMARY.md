# Short notes from an uploaded PDF

Upload a lecture PDF, DOCX or PPTX and get back **brief, to-the-point notes** you can
actually revise from — with a page number on every bullet, so anything can be checked
against the original.

## How it works (and why it is built this way)

The naive version of this feature — "paste the document into the AI and ask for a
summary" — does not work, for two reasons:

1. **The document does not fit.** A 200-page textbook is roughly 150,000 tokens. Groq's
   free tier allows about 8,000 tokens per minute and 200,000 per day, so one large PDF
   could exhaust a day's budget and still not fit through the door.
2. **Most of a document is not worth paying for.** The 20% that carries the exam is
   spread thinly through the 80%.

So the pipeline maps first and deepens on request:

```
PDF ─▶ extract ─▶ clean ─▶ study map ─▶ expand sections ─▶ assemble notes
      (local, free)         (1 call)      (a few calls)      (1 call)
```

| Stage | What happens | Cost |
|---|---|---|
| **1. Extract** | `pypdf` reads the text, and with it the font size and position of every line. Font size identifies headings; position identifies running headers/footers and page numbers. | **Zero** — local CPU only, ~160 ms for 40 pages |
| **2. Clean** | Running headers/footers and page numbers are removed (only repetition *inside the top/bottom band* counts as furniture — a sentence the author repeats in the body is content and survives). Lines repeated more than four times are collapsed, and reported. | **Zero** |
| **3. Study map** | The model receives only the **skeleton** — heading, page, first sentence per section. It returns a table of contents with a one-line gist and an importance score (1-5) per section. On a dense 12-page fixture this is ~12% of the document's tokens. | 1 call |
| **4. Expand** | Sections are expanded into note bullets (3-6 per section) with page citations, key terms, a drug table and — only when genuinely useful — a mnemonic. Sections are processed highest-importance first, so if the budget runs out you keep the most exam-relevant material. | 1 call per section |
| **5. Assemble** | Bullets are merged **deterministically** (duplicates collapsed, pages clamped to the section's real range). One final call supplies the "remember five" list and the exam traps. | 1 call |

## What a student sees

1. **Vault → a document → "Short notes".**
2. **A free preview**, computed on the server with no AI: page count, sections found,
   text density, the table of contents, and what the reader noticed (headers removed,
   duplicates collapsed, multi-column pages, no text layer). Plus the **token cost of
   each depth** before anything is spent.
3. **A depth dial**:
   - **brief** — the study map plus "remember five". Cheapest.
   - **standard** — expands the sections that matter most.
   - **full** — expands every section.
4. **Progress**, section by section. The run is spread over short requests, so it never
   ties up a hosting worker.
5. **The notes**: "if you remember only five things", exam traps, then one card per
   topic with bullets + page chips, a drug table where drugs are named, key terms, and a
   mnemonic. **Copy** to clipboard, or **Print / PDF** (a print stylesheet emits just the
   notes).

## Cost in practice

Measured on a dense generated fixture (~1,500 chars/page, 12 pages ≈ 2,700 tokens):

| Depth | Model calls | Example cost |
|---|---|---|
| brief | 2 | ~560 tokens |
| standard | 4-5 | ~3,400 tokens |
| full | 8+ | ~5,000 tokens |

Extrapolating at realistic textbook density (~2,500 chars/page):

| Document | Full pass | Free-tier daily budget (200K) |
|---|---|---|
| 40-page handout | ~25,000 tokens | 12% |
| 200-page textbook | ~125,000 tokens | 63% |
| 400-page reference | ~250,000 tokens | more than one day — split it, or raise the budget |

The preview quotes an **upper-bound estimate** before you commit, and the app enforces
two ceilings: a per-client allowance and an app-wide daily budget
(`SUMMARISE_DAILY_TOKEN_BUDGET`). That budget is the thing that protects your key if the
endpoint is ever hit by someone you did not invite.

## Resumability and caching

- Notes are cached **by file hash** — a document is summarised once, ever. Reopening it
  costs nothing.
- Each section is committed as it finishes. If a run stops — daily budget, rate limit,
  crash — **press Continue** and it resumes where it stopped instead of paying again.
- Changing the depth discards the expansion and rebuilds at the new depth.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `SUMMARISE_ENABLED` | `true` | Kill switch for the whole feature |
| `GROQ_MAP_MODEL` | *(= `GROQ_MODEL`)* | Outline + section expansion. A small/cheap model is right here |
| `GROQ_SUMMARY_MODEL` | *(= `GROQ_MODEL`)* | Final synthesis |
| `GROQ_SUMMARY_MAX_TOKENS` | `8000` | Reasoning models spend this on thinking first — too low and the answer is empty |
| `SUMMARISE_MAX_INPUT_TOKENS` | `5000` | Per model call |
| `SUMMARISE_DAILY_TOKEN_BUDGET` | `150000` | App-wide, rolling 24h |
| `SUMMARISE_PER_IP_DAILY_TOKENS` | `30000` | Per client, rolling 24h |
| `SUMMARISE_CALLS_PER_REQUEST` | `3` | Model calls per HTTP request |
| `SUMMARISE_MAX_SECTIONS` | `24` | Ceiling on sections expanded per run |
| `GROQ_BASE_URL` | *(unset)* | Only for routing Groq through a proxy |

## What it will not do (honestly)

- **Scanned or photographed documents.** There is no text layer to read, and OCR is not
  realistic on shared hosting. The preview says so and generation is refused — it will not
  invent content from a picture of a page.
- **Legacy `.doc` / `.ppt`.** Binary formats that need LibreOffice. The app tells you to
  re-save as `.docx`/`.pptx`/PDF.
- **Tables are the weakest point.** `pypdf` flattens them into word soup; the model is
  asked to reconstruct what it can, and drug tables are kept where the text allows.
- **Multi-column layouts** can scramble reading order on some pages; the preview warns
  when it detects them.
- **It is not a substitute for reading.** Every bullet carries its page so you can check
  it. Treat the notes as a revision aid, not a source.

## Running the tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -t .     # 37 tests
```

`requirements-dev.txt` adds `fpdf2`, used only to generate the PDF fixtures.

## Not built yet

Deliberately left for the next stage (the pipeline is structured to allow them):

- a cron-driven worker, so a very large document finishes without the browser open;
- per-section drill-down ("expand 3.1 only") from the notes view;
- merging several PDFs of one unit into a single revision sheet;
- flashcards and quiz questions generated from the same notes JSON.
