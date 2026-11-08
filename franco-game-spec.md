# Franco Dataset Game — Build Spec

A small web game that shows players Egyptian Arabic sentences and collects how they naturally write each word in Franco (Arabizi). The goal is a **private dataset** of Franco spelling variations per Arabic word, with sentence context, for training and evaluating Kalamna's transliteration model.

There are no correct answers. Every honest spelling is valid data.

---

## 1. Stack

- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL (SQLite is fine for local development)
- **Frontend:** plain HTML + JS (or Jinja templates served by FastAPI). Mobile-first, since most players will be on phones.
- **Sentence sources:** sentences come from two places, mixed into the same rotation:
  - **Generated:** Anthropic API, called **server-side only**. The API key lives in an environment variable (`ANTHROPIC_API_KEY`) and never reaches the browser. The model name is configurable via env (`ANTHROPIC_MODEL`, default `claude-haiku-5-5`, the cheapest; switch to `claude-sonnet-5-5` if quality is poor). Requests use structured JSON output at low effort.
  - **Owner input:** sentences typed or pasted in by owners through the owner panel (see 4.1 and section 6).
- **Hosting:** Render free web service (sleeps when idle) + Neon free Postgres (doesn't expire). Render's free Postgres is deleted after 30 days, so it isn't used. See the README for setup.

---

## 2. Roles

| Role | How they get in | What they can do |
|---|---|---|
| **Player** | No sign-up. A random player ID (e.g. `p_7f3a`) is generated on first visit and stored in the browser's localStorage. | Play rounds, report sentences, set an optional display name |
| **Owner** | Username + password (hashed with bcrypt or argon2). Several owner accounts are supported. There is no public sign-up: the first owner is created once on a setup page that needs a secret setup code set on the server (and disappears once an owner exists), or from the command line. | Everything in the owner panel (section 6), including export |

Only owners can see stats, the review queues, or export data. Players can only **add** answers and reports; they can never read the dataset.

---

## 3. Player experience

### 3.1 Welcome screen (shown on first visit, re-openable from a help icon)

Shown in Egyptian Arabic first, with English below.

> اكتب كل كلمة بالفرانكو زي ما بتكتبها عادي. مفيش صح ومفيش غلط.
>
> لو لقيت جملة فيها غلطة إملائية، أو مش مصري، أو مش لايقة، أو مش مفهومة، دوس على «إبلاغ».
>
> اتأكد إن الأوتوكوريكت أو الكتابة التلقائية في الكيبورد مش بتغيّر اللي بتكتبه.
>
> إجاباتك بتتحفظ من غير اسمك، وبنستخدمها عشان نبني داتا.

> Write each word the way you'd naturally type it in Franco. There's no right or wrong.
> If a sentence looks misspelled, isn't Egyptian, is inappropriate, or doesn't make sense, tap Report.
> Make sure your keyboard's autocorrect or autocomplete isn't changing what you type.
> Your answers are saved anonymously and used to build a dataset.

There is an optional display-name field, used only for the leaderboard.

Owners can edit both texts in Settings (one paragraph per line; emptying a box restores the original text).

### 3.2 A round

1. Show the **full Arabic sentence** (right-to-left) and an **English gloss** under it.
2. Below it, show **one input box per Arabic word**, in right-to-left order, with the Arabic word above each box. The inputs themselves are left-to-right.
3. Between each pair of adjacent boxes, show a **link toggle**. Joining two (or more) adjacent words merges them into **one** box, for players who'd type them as one ("hanro7feen"). Clicking again splits them.
4. **Submit** requires every box to be filled. Empty boxes show an inline error and block the submit.
5. **There is no skip button.** The only way past a sentence is **Report** (3.3).
6. After a successful submit, show a short confirmation and load the next sentence.

Inputs: **do not** disable autocorrect, autocapitalize, or spellcheck. Players' natural typing, including capitals, is kept in `franco_raw`. The welcome screen asks them to watch their keyboard instead.

### 3.3 Report

- A **Report** button on every round opens a small panel with **one required reason**:
  - Misspelled
  - Not Egyptian Arabic
  - Inappropriate
  - I don't understand it
- Plus an **optional comment** ("why is it wrong?").
- Submitting the report skips the round. **Nothing typed for that sentence is saved.**
- A player can report a given sentence only once.

### 3.4 Motivation

- A counter of how many words this player has contributed.
- A leaderboard by display name. Players without a display name are left off it. It shows the top 20 by words, plus your own rank.
- Words are counted per Arabic word: a joined box of two words counts as two. Answers an owner excluded don't count.
- The display name is optional, at most 30 characters, and can be changed from the help (?) screen.

---

## 4. Sentence pool

### 4.1 Sources

Each sentence has a `source`:
- `generated`: produced by the batch generator (4.2)
- `owner`: typed or pasted in by an owner

### 4.2 Batch generation

- Sentences are **generated in batches of 50**, never one per round. This keeps API costs low and prevents abuse.
- A batch is triggered when the number of active sentences that a typical player hasn't seen yet drops to a threshold or below (configurable; default 10), or manually from the owner panel. "A typical player" means a brand-new player: active sentences still under the player limit. The check runs when a player loads a round; the batch runs in the background after the response. After a successful batch, automatic generation waits at least 2 minutes before the next.
- Every generated sentence comes with an English gloss.
- **Generation prompt requirements:**
  - Egyptian colloquial Arabic (عامية مصرية) only, **never MSA (فصحى)**.
  - Natural everyday sentences, roughly 4–10 words.
  - Each batch is spread across the **active themes** (4.3).
  - The prompt includes some **target words** (4.4) to work in naturally.
  - Steer toward **under-covered words**: the prompt includes words that have the fewest collected answers so far, and avoids over-represented ones.
  - Return structured JSON, e.g. `[{"arabic": "...", "english": "...", "theme": "..."}]`.
- **Review setting:** `require_review` (owner setting, default **off**). When it's on, new batches start as `pending_review` and only go live once an owner approves them. When it's off, they go live immediately, and the report button is the safety net.
- **Duplicate check:** a new sentence whose normalized Arabic matches one that already exists is discarded. This applies everywhere a new sentence row is created: generated batches, sentences owners type or paste (including repeats inside one bulk paste), and the "Arabic changed" fix (section 5). The database enforces it too (`normalized_text` is unique). Deleted sentences still count, so a deleted sentence can't be re-added or regenerated; an owner brings it back with **Restore** (section 6) instead.

### 4.3 Themes

- Owners manage a **list of themes** (e.g. "delivery complaints", "ordering food", "banking", "daily life").
- Adding a theme **widens the mix**; it never rewrites old sentences.
- Themes can be activated or deactivated.
- Each sentence records which theme it came from.

### 4.4 Target words

- An optional owner-managed list of Arabic words to get variants for.
- The generator works them into sentences until each has enough answers (threshold configurable).

### 4.5 Splitting a sentence into words

- Strip punctuation (، ؟ ! . , ? ؛ : " « » etc.) before splitting.
- Split on whitespace.
- Store the word list on the sentence so the split is fixed once and the same for everyone.

### 4.6 Rotation rules

- A sentence is shown to at most **50 different players** (`max_players`, configurable), then it's retired automatically.
- A player is **never shown the same sentence twice**. This is tracked by player ID (localStorage), not IP.
- Returning players always continue with sentences they haven't seen.
- Among eligible sentences, prefer those with **fewer answers so far**, so coverage spreads evenly.
- A sentence that has been shown but neither answered nor reported (the player closed the tab) can be shown again to that player. It is offered to them **first**, before any new sentence, so reloading the page can't be used to skip.
- "Players" for the limit means players who **answered or reported** the sentence. A player who was already typing when it hit the limit can still submit.
- Lowering `max_players` retires any sentence already at the new limit.

### 4.7 When generation fails

Generation can fail: the Anthropic account is out of credits, the API is down or rate-limited, or the response is unusable. None of this stops the game.

- Generation runs in the background, never inside a player's request, so a failed call never blocks or slows a round. Only one batch runs at a time.
- On failure, nothing from that call is saved. The error is shown in the owner panel, and the app waits 30 minutes before trying again automatically (so it doesn't call the API on every round while it's failing).
- Players keep playing the active sentences they haven't seen. A player who has seen every active sentence gets a "you've done them all, come back later" screen instead of an error.
- Everything that doesn't use the API keeps working: owner-added sentences, reports, the owner panel, stats, and exports.
- If generation stays down, the pool shrinks as sentences retire. Owners can keep it going by adding sentences themselves.

### 4.8 Sentence statuses

`pending_review` → `active` → `retired` (hit max players) / `reported` (auto-pulled) / `deleted`

An owner can **restore** a `deleted` sentence back to `active`.

---

## 5. Reports lifecycle

- A sentence with **2 reports** (configurable) is automatically set to `reported` and pulled from rotation.
- The owner panel lists reported sentences, with each report's reasons and comments. It also lists sentences with fewer reports than the threshold that are still in the game.
- Reports are never deleted. Dismissing, fixing or deleting a sentence marks its reports **resolved**, so they stop counting toward the threshold and a fixed sentence starts from zero.
- From the queue, an owner can:
  - **Fix and re-release**:
    - **Gloss-only fix** (also punctuation or harakat): update the existing sentence row and put it back in rotation. No new row is created; its answers stay attached and valid.
    - **Arabic changed:** create a **new** sentence row with `replaces_id` pointing to the old one, and split the words again. The old row and its answers stay as history and still count as dataset rows in exports. The duplicate check (4.2) applies to the new text.
  - **Dismiss** the reports and put it back in rotation.
  - **Delete** it.

---

## 6. Owner panel

- Login (username + password).
- **First-time setup:** while no owner exists, `/owner` shows a "Create owner account" form. It requires the server's `SETUP_CODE` (at least 12 characters; without it the page is switched off) and is gone once an owner exists.
- **Your account:** change your own password (needs the current one).
- **Settings:** edit every value in the `settings` table, with allowed ranges.
- **Stats:** number of players, answers, unique Arabic words, and variants; most- and least-covered words.
- **Themes:** add, edit, activate/deactivate.
- **Target words:** add, remove, see progress.
- **Own sentences:** add one or bulk paste (one per line, with an optional English gloss).
- **Generation:** trigger a batch manually; toggle `require_review`; review queue for pending batches (approve, edit, delete).
- **Reports queue** (section 5).
- **Deleted sentences:** list them and **restore** one to `active` (the only way to bring back deleted text, see 4.2).
- **Flagged answers:** review rows the junk filter flagged (section 7); keep or exclude them.
- **Players:** list by player ID and display name with their contribution count. Owners can **rename or clear** a display name (e.g. a rude one). **Delete all rows from a player** in one action (for junk players).
- **Export** (section 9).

---

## 7. Validation and anti-junk

Checked **server-side** on every submit, per box:

| Case | Example | Action |
|---|---|---|
| Arabic letters in a Franco box | `أنا` → `أنا` | **Reject** with "Write it in Franco, not Arabic" |
| Emojis / symbols only | `معرفش` → `😂😂` | **Reject** |
| Empty after trimming | `"   "` | **Reject** |
| Length way off for the Arabic word | `فين` → `asdfghjklqwerty` | **Save, but flag** (`flagged = true`, with a reason) |
| Keyboard mashing / repeated characters | `aaaaaaa` | **Save, but flag** |

The exact flagging thresholds are configurable. Allowed characters: Latin letters, digits (2, 3, 5, 6, 7, 8, 9 are core to Franco), apostrophes, hyphens, and spaces.

- **Flag rules** (defaults): longer than 4× the Arabic word's letters + 4; one character repeated 5+ times in a row ("ya3niiii" with 4 passes); 5+ neighbouring keyboard keys in a row ("asdfg", "qwert").
- Numbers-only answers are allowed ("72", "45"); one digit repeated ("55", "22222") is rejected unless the Arabic word is itself a number.
- Owners review flagged answers: **keep** (counts as a normal answer) or **exclude** (never exported). Reviewed answers are never re-flagged. Changing the thresholds, or the "Re-check all answers" button, re-runs the rules on every unreviewed answer, including ones saved before flagging existed.

**Rate limiting:** per IP and per player ID (default 20 submissions per minute per player; an IP gets 5× that, since friends on the same Wi-Fi share one). Covers answers, reports and new player IDs. Raw IPs are used only for rate limiting, are **not stored** in the dataset tables, and are never exported.

---

## 8. Data model

### `sentences`
| column | type | notes |
|---|---|---|
| id | pk | |
| text | text | Arabic sentence as displayed |
| normalized_text | text, unique | normalized Arabic, for the duplicate check (4.2) |
| words | json array | the fixed word split (4.5) |
| english | text | gloss |
| source | enum | `generated` / `owner` |
| theme_id | fk, nullable | |
| batch_id | fk, nullable | for generated sentences |
| status | enum | see 4.8 |
| replaces_id | fk, nullable | set when created by "fix and re-release" |
| created_at | timestamp | |

### `answers` (one row per filled box; this is the dataset)
| column | type | notes |
|---|---|---|
| id | pk | |
| sentence_id | fk | |
| player_id | text | anonymous ID, e.g. `p_7f3a` |
| position_start | int | 1-based index of the first word |
| position_end | int | equal to the start unless words were joined |
| arabic | text | the word, or the joined words separated by a space |
| franco_raw | text | exactly as typed |
| franco | text | normalized (section 10) |
| joined | bool | true if more than one word |
| flagged | bool | from the junk filter |
| flag_reason | text, nullable | |
| flag_review | enum, nullable | `kept` / `excluded`, an owner's decision |
| created_at | timestamp | stored, but **not exported by default** |

### Others
- `players`: id, display_name (nullable), created_at
- `sentence_views`: player_id, sentence_id, outcome (`answered` / `reported` / `shown`), created_at. Powers rotation (4.6).
- `reports`: id, sentence_id, player_id, reason, comment (nullable), resolved (bool), created_at
- `themes`: id, name, description, active
- `target_words`: id, word, active
- `generation_batches`: id, requested_by (owner username as text, or `auto` when triggered by the pool threshold; stays readable if the owner is deleted), model, prompt_snapshot, created_at
- `owners`: id, username (`auto` is reserved), password_hash
- `settings`: key/value (max_players, report_threshold, require_review, batch_size, pool_threshold, flag thresholds)

---

## 9. Export (owners only)

Two exports, each available as **CSV (UTF-8 with BOM, so Excel shows Arabic correctly) and JSONL**:

1. **Raw answers** (the real dataset; used for training):
   `sentence_id, sentence_text, english, position_start, position_end, arabic, franco_raw, franco, joined, player_id`
   - Options: include/exclude flagged rows (default: exclude), and include timestamps (default: off).
2. **Grouped variants** (a summary built from the raw answers; used for evaluation):
   `arabic, joined, variant, count`, one row per (word, normalized variant), sorted by count.
   - Joined entries (e.g. `هنروح فين`) are kept as their **own** keys and are never merged into their parts.

The grouped file is always generated from the raw answers on demand. The raw answers are the source of truth.

Both exports always leave out answers an owner **excluded** and answers on **deleted** sentences (answers on retired, reported or replaced sentences stay in). Answers an owner **kept** count as normal. Grouped rows with equal counts are ordered by the Arabic word, then the spelling.

Exports always use a sentence's current gloss, so after a gloss-only fix (section 5) its old answers export with the corrected gloss.

---

## 10. Normalization

Applied automatically at save time. Both columns are stored.

```python
def clean(text: str) -> str:
    return " ".join(text.split()).lower()
```

- Trims the edges, collapses inner runs of whitespace to a single space, and lowercases.
- **Inner single spaces are kept**: "ma 3rafsh" and "ma3rafsh" are different variants.
- **Capitalization carries no meaning.** Arabic has no case, so `franco` is lowercase, and `franco_raw` keeps whatever the player typed.
- **Curly apostrophes become straight** (`’` → `'`), since phones insert them automatically and they'd otherwise split one spelling into two variants.
- If the rule ever changes, `franco` can be rebuilt from `franco_raw`.

---

## 11. Privacy and data policy

- The dataset is **private** for now. No public release and no license yet; this is to be revisited later.
- Players are identified only by random IDs. Display names stay in the owner panel and are never exported.
- No IPs are stored in the dataset or exported.

---

## 12. Suggested build order

1. Project skeleton: FastAPI app, database models and migrations, settings.
2. Owner auth plus a basic owner panel shell.
3. Owner-entered sentences, word splitting, and the player round UI (sentence, gloss, per-word boxes, link toggle, submit, validation, normalization).
4. Player IDs, rotation rules, and `sentence_views`.
5. Report flow, auto-pull, and the reports queue.
6. Batch generation (themes, target words, under-covered steering, optional review).
7. Junk filtering, flagging, and rate limiting.
8. Exports.
9. Welcome screen, counter, and leaderboard.
10. Deployment.
