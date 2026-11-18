# Darzi

Darzi is a command-line tool. It turns a job post into a checked résumé and
cover letter that you approve. The command is `facts`.

This repo holds code and synthetic test data only. Your own facts live in a
separate, private folder: the data root.

## What darzi does

You send a job link to a coding agent. The agent uses `facts` for each step:

1. It saves the job post in a new run folder.
2. It writes a tailored résumé and a cover letter from your facts only.
   `facts check` must pass: no new facts, no banned words.
3. It renders a draft PDF and shows you the result.
4. You approve at your desktop with a signature. Then darzi makes the final
   PDF and Word files, and the agent fills the employer's form in your
   browser. It stops before Submit. You submit.

A second path, the hand-over path (darzi makes the files with no signature,
and you apply by hand), comes in a later version.

## What you need

- A Mac or a Linux computer.
- Python 3.12 or later, and `uv` to install darzi.
- OpenSSH. It is part of macOS and of most Linux systems. Darzi uses
  `/usr/bin/ssh-keygen` for the approval signature.
- LibreOffice. Darzi uses it to count the pages of the Word file.
- A copy (a git clone) of Resume Matcher at the commit that `facts.toml`
  pins. Darzi uses it to make the PDF.
- A coding agent that can run shell commands in your data root.

## Set-up

### 1. The data root

The data root is one folder. Each `facts` command finds it: it is the nearest
folder at or above the current folder that has `master.yaml`. You can also
give `--repo PATH`.

| Path | What it holds | Who writes it |
|---|---|---|
| `master.yaml` | Your facts | You |
| `rules.yaml` | The check rules | You |
| `questions.md` | Open questions | You and the agent |
| `selections/<type>.yaml` | Facts and layout for one job type | The agent drafts, you approve |
| `selections/<type>.approval.json`, `.sig` | Your signed approval of a selection | `facts approve` |
| `applications/<run>/` | One folder for each application | `facts` and the agent |
| `answers.yaml` | Standard form answers (keep it out of git) | You |
| `facts.toml` | Settings | You |
| `allowed_signers` | Your public approval key | You |

### 2. `facts.toml`

An example with synthetic values:

```toml
[owner]
name = "Test Person"

[approval]
signer = "owner"
namespace = "darzi-approval"
allowed_signers = "allowed_signers"
key = "~/.ssh/darzi_approval"

[render]
adapter = "resume-matcher"

[render.resume_matcher]
path = "../Resume-Matcher"
commit = "950b700"
data_dir = ".rm-data"
frontend_url = "http://localhost:3000"
backend_url = "http://127.0.0.1:8000"

[form]
blocked_hosts = ["jobs.example.com"]
```

### 3. The approval key

Make a key that you use for darzi approvals only. Give it a passphrase:
`ssh-keygen` asks for one. Do not leave the passphrase empty.

```sh
ssh-keygen -t ed25519 -f ~/.ssh/darzi_approval -C darzi-approval
```

Then write the `allowed_signers` file in the data root. It holds your public
key and the namespace:

```sh
printf 'owner namespaces="darzi-approval" %s\n' \
  "$(cut -d' ' -f1,2 ~/.ssh/darzi_approval.pub)" > allowed_signers
```

The line looks like this:

```text
owner namespaces="darzi-approval" ssh-ed25519 AAAAC3Nza...
```

Keep the private key outside the data root and outside each repo. Do not add
it to `ssh-agent` or to the macOS keychain.

### 4. Resume Matcher

Clone Resume Matcher next to the data root and check out the pinned commit.
Start its backend and its frontend on this computer.

Give darzi its own Resume Matcher data folder: `data_dir`, for example
`.rm-data` in the data root. Darzi keeps one scratch record there and
overwrites it for each render. Do not use the data folder that holds your own
Resume Matcher résumés: darzi refuses a data folder with a record that it did
not make.

### 5. LibreOffice

Install LibreOffice. If darzi cannot find it, `facts doctor` prints the
install command.

### 6. Check the set-up

```sh
facts doctor
```

Each line starts with `ok`, `WARN` or `FAIL`. A `WARN` line does not stop
you. Correct each `FAIL` line before you apply. `facts doctor` checks:

- the data root and `facts.toml`;
- the `[approval]` settings and the `allowed_signers` file;
- `ssh-keygen`, the key file (owner, file mode, place) and its fingerprint;
- that the key has a passphrase, and that `ssh-agent` does not hold it;
- the key of each signed run;
- the Resume Matcher settings, its commit, its data folder, and its backend
  and frontend on this computer;
- the newest Resume Matcher release (one request to GitHub; `--offline`
  skips it);
- LibreOffice and the `[form]` settings.

`facts doctor` never signs, never reads the bytes of your private key and
never asks for a passphrase.

## How an application goes

1. You send the agent a job link.
2. The agent runs `facts new-run`, chooses the job type, runs `facts export`,
   and writes the tailored résumé and the cover letter.
3. `facts check` and `facts render --draft` must pass. The agent sends you a
   short summary.
4. You run `facts approve RUN` in a terminal on your desktop.
5. The agent runs `facts finalize RUN` and `facts fill-plan RUN`, fills the
   form, and stops before Submit. You submit. Then the agent runs
   `facts submitted RUN`.

The agent reads the full steps with `facts workflow apply`.

## Commands

Each command accepts `--repo PATH` and `--json`. Exit codes: 0 = no errors,
1 = errors found, 2 = invalid input.

| Command | What it does |
|---|---|
| `facts validate` | Checks `master.yaml` and `rules.yaml` |
| `facts public` | Prints the public copy of your facts as JSON |
| `facts query` | Finds public bullets by tags and text |
| `facts check` | Runs the rules on a run folder or a selection |
| `facts export` | Writes `base.json` from an approved selection |
| `facts new-run` | Makes a run folder and saves the job post first |
| `facts render` | Renders a draft PDF outside the run folder and prints the page count |
| `facts approve` | Shows the full text and signs your approval. You only. |
| `facts finalize` | Makes the final PDF and Word files and `manifest.json` |
| `facts answers` | Checks `answers.yaml` |
| `facts fill-plan` | Prints the only data that a form fill can use |
| `facts submitted` | Records that you submitted the application |
| `facts status` | Lists all runs |
| `facts doctor` | Checks the set-up |
| `facts workflow` | Prints the steps for the agent (`facts workflow apply`) |

## What your approval protects, and what it does not

- **The trust limit.** `allowed_signers` and `facts.toml` are files in the data
  root, and the agent can write there. A process that changes them can
  approve with its own key. Darzi detects this; it does not prevent it. Look
  at the signer fingerprint in each `manifest.json`, at `facts doctor`
  (allowed_signers, key fingerprint, signer history) and at the git history
  of the data root.
- **The key rules.** The key must have a passphrase. Use it for darzi only.
  Do not load it in `ssh-agent` or in the macOS keychain.
- **What `doctor` tests.** It tests the passphrase and `ssh-agent`.
- **What `doctor` cannot test.** It cannot see the macOS keychain or another
  use of the key.
- **A selection approval** covers the whole selection file except its old
  `approval` block: the job type (`type`), the `tier`, the headline, the
  facts for the summary (`summary_basis`), the section order, the fact ids
  of each section, the skills and the page limit (`max_pages`). A change to
  one of these makes the approval invalid. It does not cover the fact text
  in `master.yaml`.
- **A run approval** covers the final résumé JSON and the cover letter. It is
  the last gate.
- **An older approval** stays valid for the exact older content that it
  signed. A change makes it invalid for the new content.
- **Not covered:** the print settings, and `company`, `role` and `apply_url`
  in `run.yaml`.
- **The review screen** shows a summary with its line breaks.
- **The binding to the data root.** A run or a selection counts only inside
  the data root whose `allowed_signers` darzi uses. A copy in another folder
  is not approved.

## Limits of each step

### `facts finalize`

- A second program that changes the run folder during a finalize can change
  the result.
- A damaged old `manifest.json` stops `finalize`. Delete it by hand, then run
  `finalize` again.

### The round-trip check

`finalize` reads the text back from each PDF and Word file. Each approved
item must be there, in the approved order.

- **It catches:** a missing bullet, a missing section item, two sections that
  changed places, and a changed word.
- **Limit:** it does not find extra text that nobody approved.
- **Limit:** it can miss a missing item whose text is a separate word in a
  longer name next to it.
- **Limit:** it can miss an item that is equal to a fixed label of the PDF
  template.
- **Limit:** it does not check that a section heading is there.
- **Limit:** it does not check the personal title, the project links or
  custom sections.
- **Limit:** in rare cases it refuses a correct file.
- **Advice:** read the PDF one time before you submit.

### `facts fill-plan`

- **It verifies again:** the signature, the manifest, the file hashes, the
  Word files built again, the round-trip check, `answers.yaml`, and an
  `apply_url` whose host is not blocked.
- **Unproven:** extra text in the PDF.
- **Unproven:** the values that the signature does not cover (company, role,
  apply link, answers).
- **Unproven:** a redirect of the apply link to another site.
- **Unproven:** a change to a file between the print and the upload.

### `facts new-run --url`

- **What one fetch sends:** one GET request with the User-Agent
  `darzi/<version>`. No login and no data about you. No cookie from your
  computer or your browser is sent. A cookie that the job site sets during
  one fetch can go back to that site in a later redirect of the same fetch.
  darzi keeps no cookie after the fetch.
- **The guard:** it refuses a URL that is not http or https, a URL with a
  user name or password, and each address that is not a public internet
  address. It follows at most 5 redirects and checks each one.
- **Limit:** the host name is looked up again when the connection starts.
- **Limit:** proxy settings of the environment apply to the fetch.
- **Limit:** a login page can return text. Read `job.md`. If it is not the
  job post, paste the text.

### The fit rating

<!-- W-12: fit rating limits (SPEC section 14) -->

This part comes with the fit rating.

## Privacy

- Your facts, run folders and answers stay in your data root on your
  computer.
- Darzi sends data outside this computer in two cases only: the one fetch of
  `facts new-run --url`, and the release lookup of `facts doctor`.
- `facts doctor` also checks Resume Matcher on this computer only.
- Test data in this repo is synthetic: "Test Person", addresses at
  `example.com`, and the phone number 555-0100.

## For developers

Read `AGENTS.md` and `docs/SPEC.md` first. Before each pull request:

```sh
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

Two privacy gates protect this repo: a test in CI, and a pre-push scan that
compares each change with the terms of the private data root.

## License

MIT. See `LICENSE`.
