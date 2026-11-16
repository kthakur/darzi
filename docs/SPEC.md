# Darzi: specification for the rebuild

Version 0.3, 2026-11-15. This file is public. It contains no personal data.

Darzi ("tailor") turns one job post into a checked, approved résumé and cover
letter, and then prepares the application form. A coding agent does the writing.
Darzi does the checks. The owner approves and submits.

This repo is a rebuild. The working code exists in a private repo. The work here
is to port that code, not to design it again.

## 0. What the owner sees

The owner sends a job link to an agent: from a phone, from a web chat or from a
terminal. The agent does these steps and reports after each one:

1. It gets the job post and saves it.
2. It compares the post with the owner's facts and gives a rating: green,
   yellow or red, with the reasons.
3. It writes a tailored résumé and a cover letter. The checks must pass.
4. Then one of two paths:
   - **Desktop path.** The owner approves with a signature. The agent makes the
     final files, fills the employer's form in the owner's browser, and stops
     before Submit. The owner submits.
   - **Hand-over path.** The owner is not at the desktop. The agent makes the
     files with no signature and gives them to the owner. The owner applies by
     hand.

Darzi is the engine for these steps. The front door (the chat on the phone, the
web chat, the terminal) is the agent product, not code in this repo. Section 13
has the details.

## 1. Fixed constraints

| ID | Constraint |
|---|---|
| C-1 | No personal data in this repo. Code and synthetic test data only. |
| C-2 | The coding agent does all LLM reasoning. The tools are deterministic. No tool calls an LLM. |
| C-3 | Only the owner can approve a résumé or submit an application. No agent has a path to a signed approval. The agent can put files into a form only after a signed approval. |
| C-4 | Free and open-source dependencies only. Prefer a maintained community project to own code. |
| C-5 | Each gate is in code (a `facts` command), not only in a prompt or a workflow file. |
| C-6 | The workflow works with each coding agent that can run shell commands. |

## 2. System context

```text
owner: job post --> agent runs `facts` commands ... owner approves and signs ... owner clicks Submit

data root (private repo)          darzi (this repo)              renderer (sidecar)
  master.yaml, rules.yaml    -->  facts/   validate, export, check
  selections/                     runs/    approve, finalize, form   -->  Resume Matcher,
  applications/<run>/        <--  render/  Word file, PDF adapter        pinned, render only
  answers.yaml, facts.toml        sourcing/ fetch (more later)
```

- The data root is a second, private repo. It depends on this repo at a tag.
  This repo never depends on the data root.
- The agent works in the data root. It installs `darzi` as a dependency and runs
  `uv run facts ...`.
- Resume Matcher (github.com/srbhr/Resume-Matcher, Apache-2.0) has one job: it
  renders the PDF from a template. It is not a store, a checker or a tracker.

## 3. Source material

The owner prepares a read-only snapshot before the first session. It is a folder
named `darzi-sources/`, outside this repo. The owner's start guide (not in this repo)
has the script. The snapshot has three parts:

| Folder | Content | Use |
|---|---|---|
| `darzi-sources/resume-facts/` | `src/resume_facts/` (21 modules), `tests/*.py`, `schema/`, `docs/workflows/apply.md`, `pyproject.toml`, `answers.example.yaml` | The base. Port this code. |
| `darzi-sources/darzi-e1/` | `darzi/server/fetch.py`, `darzi/server/approval.py`, `darzi/models.py`, their tests | Three modules from the old Darzi. |
| the Resume Matcher clone | The upstream repo at the pinned commit | Read to write the adapter. Never change it. |

The snapshot has no fact file, no run folder and no test fixture file. Two
warnings:

- The source tests use the owner's real contact data and real entry ids. Do not
  copy them. Write the same test with synthetic data (section 10).
- Messages and docstrings in the source name the owner and one private term.
  Replace them with "the owner" and with data from `facts.toml`.
- Four `description` texts in `schema/master.schema.json` name real things.
  Write them again in generic words (work item W-1). The same applies to the
  `description` of `schema/answers.schema.json`. Descriptions that point to
  the old `SRS.md` or `AGENTS.md` lose the pointer and keep the meaning. Each
  `$id` is the bare file name.

## 4. Layout

```text
src/darzi/
  facts/       repo.py, validate.py, rules.py, patterns.py, public.py, selection.py,
               export.py, check.py, check_meta.py, check_provenance.py, check_structure.py,
               fit.py
  runs/        runs.py, signing.py, approve.py, finalize.py, roundtrip.py, form.py, status.py
  render/      port.py, word.py, pages.py, resume_matcher.py
  sourcing/    fetch.py, model.py
  schemas/     master, rules, selection, answers, requirements (*.schema.json, package data)
  workflows/   apply.md
  config.py    reads facts.toml
  doctor.py
  cli.py       console script `facts`
tests/
  data/        the synthetic data root
scripts/
  privacy_scan.py, pre-push
docs/SPEC.md
AGENTS.md
```

Module map (source file to new file):

| Source (`darzi-sources/resume-facts/src/resume_facts/`) | New (`src/darzi/`) | Change |
|---|---|---|
| `repo.py` | `facts/repo.py` | Schemas load from package data, not from the data root. |
| `validate.py`, `rules.py`, `patterns.py`, `public.py`, `export.py` | `facts/` | None |
| `selection.py` | `facts/selection.py` | `is_approved()` uses the signed approval (section 7). |
| `check.py`, `check_meta.py`, `check_provenance.py`, `check_structure.py` | `facts/` | None. Do not change a rule. |
| `runs.py` | `runs/runs.py` | `run.yaml` has no `resume_matcher` block. `new-run` accepts `--url`. |
| `approve.py` | `runs/approve.py`, `runs/signing.py` | Signed approval. No write to the selection YAML. Remove `ruamel-yaml`. |
| `finalize.py` | `runs/finalize.py` | Uses the renderer interface and the round-trip check. |
| `form.py` | `runs/form.py` | `mark_submitted()` writes `run.yaml` only. No tracker. |
| `word.py`, `pages.py` | `render/` | `word.py` writes fixed timestamps (section 9). No other change. |
| `resume_matcher.py` | `render/resume_matcher.py` | Becomes the adapter. `facts rm` goes away. |
| `doctor.py` | `doctor.py` | Checks the renderer, LibreOffice and the signer. |
| `cli.py` | `cli.py` | Commands of section 6. |
| old Darzi `server/fetch.py`, `models.py` | `sourcing/fetch.py`, `sourcing/model.py` | Remove the MCP handler shape. Keep the SSRF guard and the tests. |
| old Darzi `server/approval.py` | (design only) | Use its claim format and its verify order. Do not use HMAC (section 7). |

## 5. The data root

`facts/repo.py` finds the data root: the nearest folder at or above the current
folder that has `master.yaml`. Each command also accepts `--repo PATH`.

| Path | Content | Written by |
|---|---|---|
| `master.yaml` | The fact database | The owner |
| `rules.yaml` | The rules as data | The owner |
| `questions.md` | Open and resolved questions | The owner, the agent |
| `selections/<type>.yaml` | Fact ids and layout for one job type | The agent drafts, the owner approves |
| `selections/<type>.approval.json`, `.sig` | Signed approval of a selection | `facts approve` |
| `applications/<run>/` | One folder for each application (section 8) | `facts`, the agent |
| `answers.yaml` | Standard form answers. Not in git. | The owner |
| `facts.toml` | Settings | The owner |
| `allowed_signers` | The owner's public signing key | The owner |

`facts.toml`:

```toml
[owner]
name = "Test Person"                 # used in messages

[approval]
signer = "owner"                     # identity in allowed_signers
namespace = "darzi-approval"
allowed_signers = "allowed_signers"  # relative to the data root
key = "~/.ssh/darzi_approval"        # private key; outside each repo

[render]
adapter = "resume-matcher"

[render.resume_matcher]
path = "../Resume-Matcher"
commit = "950b700"
data_dir = ".rm-data"
frontend_url = "http://localhost:3000"
backend_url = "http://127.0.0.1:8000"
node = "24.22.1"                     # used only by the start script in the data root

[form]
blocked_hosts = ["linkedin.com", "indeed.com"]

[fit]
green = 0.7                          # coverage at or above this value is green
yellow = 0.4                         # at or above this value is yellow; below is red

[handover]
copy_to = ""                         # optional folder that syncs to the phone
```

## 6. Commands

All commands accept `--repo PATH` and `--json`. JSON output has
`"schema_version"`. Exit codes: 0 = no errors, 1 = errors found, 2 = invalid
input. No command sends data outside the machine, except `new-run --url` (one
fetch) and `doctor` (a release-tag lookup).

| Command | Does | Source |
|---|---|---|
| `facts validate` | Checks `master.yaml` and `rules.yaml` | Port |
| `facts public` | Prints the public copy of the facts as JSON | Port |
| `facts query --tags a,b [--text t]` | Finds public bullets | Port |
| `facts check SELECTION` or `RUN` | Runs the rules. For a run, writes `check.json`. | Port |
| `facts export SELECTION --run RUN` | Writes `base.json` and `base.map.json`. Refuses a selection with no valid approval. | Port |
| `facts new-run --company X --role Y (--job FILE or - or --url URL)` | Makes the run folder. Writes `job.md` first. | Port, plus `--url` |
| `facts fit RUN` | Reads `requirements.json`, checks it, and writes `fit.json` with the rating (section 14) | New |
| `facts render RUN --draft` | Renders `tailored.json` to a temporary PDF and prints the page count. Writes nothing in the run folder. | New |
| `facts approve SELECTION` or `RUN` | Runs the checks, shows the full text, asks for yes, signs. Owner only. | Changed |
| `facts finalize RUN` | Verifies the approval, makes the PDF and Word files, runs the round-trip check, writes `manifest.json`. Refuses a submitted run. | Changed |
| `facts finalize RUN --handover` | The same, with no approval. `facts check` must have no errors. The manifest says `"signed": false`. | New |
| `facts answers` | Checks `answers.yaml` | Port |
| `facts fill-plan RUN` | Prints the only data that a form fill can use. Verifies the approval and the file hashes. Refuses a manifest with `"signed": false`. | Port |
| `facts submitted RUN` | Sets the status and the date in `run.yaml` | Changed |
| `facts status` | Lists all runs: date, company, role, type, status | New |
| `facts doctor` | Checks the renderer, LibreOffice, the signer key and its fingerprint | Changed |
| `facts workflow apply` | Prints `workflows/apply.md` | New |

Removed: `facts rm`. Not in this build: `facts next`, `facts suggest`.

## 7. Approval

The source has two weak points. `facts approve` only tests that stdin is a
terminal, and a pseudo-terminal passes that test. The approval file holds a
plain hash, so a process can write it. This build closes both.

Rules:

- `facts approve` runs the same checks and shows the same review text as the
  source (`approve.py`: `selection_text()`, `run_text()`, `unreviewable()`,
  `safe()`). It still refuses when stdin is not a terminal.
- It writes an approval record and then signs it with the owner's private key.
- A shared secret is not permitted. The verifier runs in the agent's shell, so a
  process that can verify with a shared secret can also sign. Use a key pair.
- The signature tool is OpenSSH, which is on macOS and Linux:
  - Sign: `ssh-keygen -Y sign -f <key> -n <namespace> <record>` makes `<record>.sig`.
  - Verify: `ssh-keygen -Y verify -f <allowed_signers> -I <signer> -n <namespace> -s <record>.sig < <record>`.
  - These two commands were tested with OpenSSH 8.9 on Linux: a changed record
    and a key that is not in `allowed_signers` both fail. Confirm them on macOS.
- For the sign call, remove `SSH_AUTH_SOCK`, `SSH_ASKPASS` and
  `SSH_ASKPASS_REQUIRE` from the environment. Then only a passphrase typed at
  the terminal can open the key.
- The record is JSON:

```json
{"schema_version": 2, "kind": "run", "target": "<run folder name>",
 "date": "YYYY-MM-DD", "content_hash": "<sha256>", "signer": "owner"}
```

- For a run: `applications/<run>/approval.json` and `approval.json.sig`. The
  hash is `content_hash()` from `runs.py`, with no change.
- For a selection: `selections/<type>.approval.json` and `.sig`. The hash is
  `selection_hash()` from `selection.py`, with no change. The old `approval`
  block in the YAML is ignored.
- "Approved" means: the signature is valid, and the hash in the record equals
  the hash of the current content. `export`, `finalize` and `fill-plan` use the
  same function.
- `finalize` writes the signer's key fingerprint into `manifest.json`.
  `doctor` prints it.

The hand-over path has no signature, and that is on purpose:

- `facts finalize RUN --handover` makes the same files after the same checks.
  It writes `"signed": false` in `manifest.json`.
- These files are for the owner to use by hand. `facts fill-plan` refuses them,
  so the agent cannot put them into a form.
- If the owner signs later, a normal `facts finalize RUN` replaces the files and
  the manifest.

Tests make a key with no passphrase in a temporary folder and a temporary data
root. Production code has no environment variable or flag that replaces
`allowed_signers` or skips the signature.

Required negative tests: a hand-written record with no signature; a valid
signature for different content; a signature from a key that is not in
`allowed_signers`; a changed `tailored.json` after approval; `fill-plan` on a
hand-over manifest.

## 8. The run folder

`applications/<YYYY-MM-DD>-<company>-<role>/` is the only copy of an application.

| File | Content | Written by |
|---|---|---|
| `job.md` | The job post, written before each other file | `new-run` |
| `run.yaml` | company, role, created, type, selection_hash, status, print_settings, apply_url, submitted | `new-run`, the agent, `submitted` |
| `requirements.json` | The requirements of the job post, with the evidence for each (section 14) | The agent |
| `fit.json` | The rating and its reasons | `fit` |
| `base.json`, `base.map.json` | The exported base and the map from each entry to a fact id | `export` |
| `tailored.json` | The tailored résumé. The agent starts from a copy of `base.json`. | The agent |
| `cover_letter.md` | The cover letter | The agent |
| `check.json` | The last check report | `check` |
| `approval.json`, `.sig` | The signed approval | `approve` |
| `manifest.json` | Each output file with its hash and page count; `signed` (true or false); the approval hash and the signer fingerprint when signed; the renderer and its settings | `finalize` |
| `form_fill.md` | What the agent filled and what is empty | The agent |
| `out/` | PDF, Word résumé, Word cover letter | `finalize` |

- Status values: `new`, `finalized`, `submitted`.
- The résumé JSON format does not change. It is the Resume Matcher `ResumeData`
  shape that `export.py` writes today. Keep the loader in `check.py`
  (`load_run()`) as it is.
- The agent changes only the order and the wording. `facts check` enforces this.

## 9. Rendering

`render/port.py` defines the interface:

```python
class Renderer(Protocol):
    def render(self, resume: dict, settings: dict, out: Path) -> RenderResult: ...
# RenderResult: pages, template, page_size
```

- `word.py` and `pages.py` keep their layout code. The Word file follows the
  print settings. LibreOffice counts its pages.
- A `.docx` file is a zip that stores a write time for each entry. After the
  save, write the zip again with one fixed date for each entry. Then two builds
  of the same content give the same bytes, and a new build can confirm a hash
  in `manifest.json`. The PDF from Resume Matcher cannot do this, because
  Chrome writes a creation date.
- `render/resume_matcher.py` is the only adapter in this build. It calls the
  Resume Matcher CLI as a subprocess with no shell, as the source does.
- The adapter must put the résumé into Resume Matcher to render it. Resume
  Matcher allows 5 master résumés. Use one scratch record and overwrite it for
  each render. Work item W-6 starts with a test of this.
- Resume Matcher can change the JSON when it stores it. So the adapter does not
  compare JSON. The round-trip check decides.

Round-trip check (`runs/roundtrip.py`), run by `finalize` on each output file:

- Extract the text (`pypdf` for PDF, `python-docx` for Word).
- Normalize: Unicode NFKC, soft hyphens removed, white space collapsed.
- Each approved text item must be in the extracted text: name, contact data,
  each title, organization and date, each bullet, each skill, the summary. For
  the cover letter file: each paragraph.
- Inside one entry, the bullets must be in the approved order.
- The sections must be in the approved order (`sectionMeta`). This shows that
  the text reads in the visual order.
- A condensed entry shows one line and no bullets. Do not expect its bullets.
- A failure stops `finalize`. No manifest is written.

This check replaces the Resume Matcher parse score.

## 10. Tests and the privacy gates

- Tooling comes from the source `pyproject.toml`: uv, ruff (same rule set),
  pyright strict, pytest, line length 88.
- `tests/data/` is a synthetic data root: a person named "Test Person", employers
  such as "Acme", email addresses at `example.com`, phone numbers in the
  555-0100 range. Write `master.yaml`, `rules.yaml`, two selections,
  `answers.yaml` and `questions.md` from the schemas.
- Port each source test. Keep what it tests. Replace its data.
- Each rule R-1 to R-22 has one passing and one failing test.
- The rules and their `rules.yaml` keys. Each entry has this `id` and
  `severity`:

| Rule | `rules.yaml` keys | Severity |
|---|---|---|
| R-1 | `public_contact`, `private_contact_patterns` | error |
| R-2 | `years_patterns` | error |
| R-3 | `banned_words`, `banned_phrases` | error |
| R-4 | `pronouns` | error |
| R-5 | `education_forbidden`, `credential_wording` | error |
| R-6 | `removed_items`, `doubtful_items` | error |
| R-7 | `inflation_phrases` | error |
| R-8 | `titles` | error |
| R-9 | `employer_of_record` | error |
| R-10 | `not_working_periods` | error |
| R-11 | `exclude_from` | error |
| R-12 | `same_shape` | error |
| R-13 | `numbers_and_names` | error |
| R-14 | `never_as_company` | error |
| R-15 | `unpaid_projects` | error |
| R-16 | `unverified_bullets` | warning |
| R-17 | `open_questions` | error |
| R-18 | `page_count` | warning |
| R-19 | `skills` | error |
| R-20 | `provenance` | error |
| R-21 | `tech_terms` | error |
| R-22 | `cover_letter_claims` | error |
| PV-3 | `privacy_patterns` | error |

- The 14 edit cases are fixtures. Build them as functions that edit the
  synthetic base:

| Case | Edit | Expected | Rule (confirm in the source tests) |
|---|---|---|---|
| 01 | Change the order of bullets | pass | |
| 02 | Reword a bullet, same facts | pass | |
| 03 | Change a job title | fail | R-8 |
| 04 | Remove the employer-of-record words | fail | R-9 |
| 05 | Add a number that the source does not have | fail | R-13 |
| 06 | Add a years-of-experience count to the summary | fail | R-2 |
| 07 | Add "led the ... design team" | fail | R-7 |
| 08 | Add a banned word | fail | R-3 |
| 09 | Remove a bullet | fail | R-12 |
| 10 | Remove a role | fail | R-12 |
| 11 | Add a bullet with new technology names | fail | R-12, R-21 |
| 12 | Add a skill that is not in the fact file | fail | R-19 |
| 13 | Change the email address | fail | R-1 |
| 14 | Move the dates of a role | fail | R-10 |

Two privacy gates:

- **In CI (generic).** A test scans `src/`, `tests/`, `docs/` and `scripts/`.
  It fails on an email address that is not at `example.com` or `example.org`,
  and on a phone number outside the 555-0100 range.
- **Before each push (private terms).** `scripts/pre-push` runs
  `scripts/privacy_scan.py`. The script reads the real data root from the
  environment variable `DARZI_DATA_ROOT`. If it is not set, the push fails.
  `scripts/pre-push` runs the script with `uv`. If `uv` is not found, the push
  fails. The term list comes from these sources:
  - `rules.yaml`: the email and phone in `public_contact`;
    `private_contact_patterns.terms`; `privacy_patterns.terms` and
    `privacy_patterns.regex`; each word in each list of
    `employer_of_record.required_any`, and `employer_of_record.terms`;
    `never_as_company.terms`. The generic `any_*` patterns are not used.
  - `master.yaml`: each `id`; `org` and `display_org` of each role and each
    project; `alt_names` of each project; `institution` and
    `display_institution`; from each entry in `profile.links`, only `url` (not
    `label` or `sources`), as two terms: the full URL and the host name without
    "www."; `profile.email`,
    `profile.phone`; `profile.name` and each part of it.
  - `privacy_scan_terms.txt` in the data root (the owner's file): one term for
    each line. An absent file adds no terms.

  No rule drops a term, also not a common word. A term matches with letter case
  ignored, on word boundaries, and a space matches each run of white space. A
  phone number matches on its last ten digits with any separators.

  For each commit of the push that is not on the remote, the script scans the
  commit message, the path of each added or changed file and the full content
  of that file. It also scans the ref names of the push, and the tag name and
  message of each annotated tag (not the tagger line, as it does not scan the
  commit author). A finding gives the commit, the file and the line number,
  never the term. An error message never quotes a data root file. The option `--show-terms` also shows the term. It is for the owner
  only. The hook does not use it.

  Allow-list: the word "darzi" in each letter case; the owner's name in
  `LICENSE` and `pyproject.toml`; each line of `privacy_scan_allow.txt` in the
  data root (the owner's file). An entry is case-sensitive and lets only that
  exact form pass: "abc" lets "abc" pass, but not "Abc". An entry also lets a
  regex finding pass when the found text is equal to the entry. An absent file
  gives an empty list.

CI runs on GitHub-hosted runners only. Never connect a self-hosted runner to
this repo.

## 11. Work items

One work item = one issue = one branch = one pull request. Do them in this
order. Each one leaves `main` with passing CI.

| ID | Work | Done when |
|---|---|---|
| W-0 | Scaffold: uv project, the layout, CI, MIT licence, both privacy gates | CI passes. The scan stops a push that holds a term from a synthetic data root. The owner tested it with a real term. |
| W-1 | Schemas as package data (with generic description texts), and the synthetic data root | Each synthetic file is valid against its schema. |
| W-2 | Port `facts/`: repo, validate, rules, patterns, public, selection (hash only), export | Ported tests pass. `facts validate`, `public`, `query`, `export` work on `tests/data/`. |
| W-3 | Port the checks: check, check_meta, check_provenance, check_structure | A pass and a fail test for each rule. The 14 cases give the table in section 10. |
| W-4 | `runs/runs.py`, `new-run`, `status`, `config.py` | `job.md` is the first file written. `facts status` lists the synthetic runs. |
| W-5 | Signed approval: `signing.py`, `approve.py`, and the approval test in `export` | The four negative tests of section 7 pass. No write to a selection YAML. |
| W-6 | Renderer interface and the Resume Matcher adapter. First, test the scratch-record method and write the result in the issue. | `facts render RUN --draft` gives a PDF and a page count for a synthetic run, two times in sequence. |
| W-7 | `word.py`, `pages.py`, `roundtrip.py` | The round-trip check passes on a correct file. It fails when one bullet is removed from the file, and when two sections change places. Two builds of the same Word content have the same SHA-256. |
| W-8 | `finalize` and the manifest | A synthetic run gets three files and a manifest. `finalize` refuses a changed file, a bad signature and a submitted run. |
| W-9 | `form.py`: `answers`, `fill-plan`, `submitted` | Ported tests pass. `fill-plan` refuses a host in `blocked_hosts`. |
| W-10 | `sourcing/fetch.py`, `model.py`, `new-run --url` | The old fetch tests pass. A blocked address is refused. A failed fetch tells the user to paste the text. |
| W-11 | `doctor`, `workflows/apply.md` (generic), `facts workflow`, README | `facts doctor` reports each check. The workflow names no person and no company. |
| W-12 | Fit analysis: `facts/fit.py`, the requirements schema, `facts fit` | The tests of section 14 pass. A quote that is not in `job.md` is an error. The workflow has step 2. |
| W-13 | Hand-over path: `finalize --handover`, the `signed` field, the refusal in `fill-plan`, `[handover] copy_to` | A synthetic run with no approval gets three files and a manifest with `"signed": false`. `fill-plan` refuses it. The workflow has the hand-over path. |
| W-14 | Release | Tag `v0.2.0`. CI passes on the tag. |

The apply workflow. W-11 writes it with no step 2 and no hand-over path. W-12
adds step 2. W-13 adds the hand-over path.

1. `facts new-run` saves the job post. From a link: `--url`. If the fetch fails,
   the agent asks the owner to paste the text.
2. The agent writes `requirements.json` and runs `facts fit RUN`. It sends the
   owner the rating and the reasons. If a hard requirement is not met, it stops
   and asks the owner if it must continue.
3. The agent selects the job type and writes `type` and `selection_hash` in `run.yaml`.
4. `facts export`.
5. The agent copies `base.json` to `tailored.json`, edits it, and writes `cover_letter.md`.
6. `facts check RUN`. A maximum of 3 correction rounds, then the agent asks the owner.
7. `facts render RUN --draft` for the page count. Layout settings go in `run.yaml`. Never remove content to fit.
8. The agent shows the owner the changes, the summary, the letter and the check
   result. It asks which path the owner wants, if the owner did not say.

Desktop path:

9. The owner runs `facts approve RUN` in a terminal on the desktop.
10. `facts finalize RUN`.
11. `facts fill-plan RUN`. The agent fills the form and stops before Submit.
12. The owner submits. Then `facts submitted RUN`.

Hand-over path:

9. `facts finalize RUN --handover`.
10. The agent gives the owner the three files and the apply link. If
    `[handover] copy_to` is set, the files are also in that folder.
11. The owner applies by hand. When the owner says so: `facts submitted RUN`.

## 12. Not in this build

- Job sourcing other than one fetch. Planned design: one file for each job
  board behind `sourcing/port.py`; an append-only event log for the inbox;
  filters in code with settings in `facts.toml`; a mode that prints a prepared
  prompt. The fit analysis of section 14 is the triage step for sourced jobs.
- An MCP server. If it comes, it has the same functions as the CLI and no
  approve tool.
- A second renderer. First choice: RenderCV (github.com/rendercv/rendercv, MIT),
  a Python library with no server and no browser. Fallback: LibreOffice, which
  makes the PDF from the Word file. Each is one more adapter behind
  `render/port.py`.
- A user interface or a chat bot of its own (section 13).
- A signature from the phone. A later option: a passkey on a small page that
  the desktop serves.
- `facts next` and `facts suggest`.

## 13. The front door

Darzi has no chat interface. The front door is an agent product that can run
shell commands in the data root. Constraint C-6 applies: no step can depend on
one product.

What the front door must do:

- Receive a message from the owner and start the apply workflow in the data
  root. A message that holds only a link means "apply to this job".
- Send short text back to the owner, and send or show files.
- For the desktop path: operate the owner's browser on the desktop.

One example that exists today is Claude Code with Remote Control: the owner
uses a phone app or a web page, and the session runs on the owner's computer.
Chat channels (Telegram, iMessage) can also send a message into a session. The
owner's start guide has the commands.

Rules that follow from this:

- The computer with the data root must be on, with the agent and the renderer
  available. `facts doctor` reports what is missing.
- Messages to the owner are short. A phone shows them. Give the rating, the
  reasons and the next question, not the full report.
- The agent never treats a chat message as an approval. "Looks good" in a chat
  selects the hand-over path at most. Only `facts approve` at a terminal makes
  a signed approval.
- If the product cannot send a file to the phone, `[handover] copy_to` is the
  fallback: a folder that the owner's computer syncs to the phone.

## 14. Fit analysis

`facts fit RUN` rates how well the owner's facts cover one job post. The agent
reads and judges. The code checks that the agent's input is real and does the
arithmetic. The result is a colour with reasons, not a score from a model.

The agent writes `requirements.json` in the run folder:

```json
{"schema_version": 1,
 "requirements": [
  {"id": "q1", "kind": "hard", "quote": "<words copied from job.md>",
   "status": "met", "basis": "answers:<answer id>"},
  {"id": "q2", "kind": "wanted", "quote": "<words copied from job.md>",
   "evidence": ["<bullet id>", "skill:<exact skill name>"]}
 ]}
```

- A **hard** requirement is a condition that no wording can change: the right
  to work, the location or the days on site, a required degree, licence or
  clearance. Its `status` is `met`, `not_met` or `unknown`. `met` and `not_met`
  need a `basis`: an answer in `answers.yaml` or a fact id.
- A **wanted** requirement is a skill or an experience that the post asks for.
  Its `evidence` is a list of bullet ids and skill names from the public copy
  of the facts. An empty list means "no evidence".

What the code does:

1. Each `quote` must be in `job.md` (white space and letter case ignored). If it
   is not, the command fails. This stops an invented requirement.
2. Each id in `evidence` and each `basis` must exist: in the public copy of the
   facts, or in `answers.yaml`. If one does not, the command fails.
3. A hard requirement with `met` or `not_met` and no `basis` counts as `unknown`,
   with a warning.
4. `unknown` never lowers the rating. The report lists it for the owner.
5. Coverage = wanted requirements with evidence, divided by all wanted
   requirements.
6. The rating: **red** if a hard requirement is `not_met`. If not: **green**
   when the coverage is at or above `[fit] green`, **yellow** when it is at or
   above `[fit] yellow`, and **red** below that.
7. Fewer than 5 wanted requirements gives a warning. A short list can hide gaps.

`fit.json` holds the rating, the two counts, each requirement with the text of
its evidence, and the list of gaps. The printed summary is short: the rating,
the counts, the hard requirements that are not met or unknown, and the gaps.

Limits to state in the README:

- The rating shows how much of the post has a fact behind it. It does not show
  the chance of an interview.
- The match of a requirement to a fact is the agent's judgment. The owner can
  read each pair in `fit.json`.
- The rating never blocks a run. A red rating stops the agent only when a hard
  requirement is not met, and then the owner decides.

Tests: a quote that is not in the post; an evidence id that does not exist; a
hard requirement that is not met gives red; the two limits of the colours; an
`unknown` hard requirement does not change the colour.
