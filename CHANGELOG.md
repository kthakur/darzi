# Changes

## 0.2.1 (25 November 2026)

A safer fetch of a job page, and clearer messages. No check rule, hash or file
format changes. Some inputs that 0.2.0 accepted, or that stopped it with a
traceback, are now refused with a short message. They are listed below.

### The fetch of `facts new-run --url`

- The fetch looks up the host name one time at each hop and connects only to
  an address that the guard tested. Before, the web library looked up the name
  a second time, and that lookup could give a different address.
- The fetch unpacks a compressed page itself, in small steps, and stops at the
  5 MiB size limit. Before, a small compressed page could unpack to many
  megabytes in memory first. The limit also applies to the bytes as they
  arrive.
- The fetch accepts no compression or one layer of gzip (also named `x-gzip`)
  or deflate. A page that the server marks `x-gzip` is now unpacked as gzip.
- New refusals:
  - A redirect to a URL that is not valid (for example a bad international
    host name, or a port that is not a number) gives `invalid_url` and exit
    code 2. No request goes to it. Before, it gave `connection_failed` and
    exit code 1.
  - These pages give `connection_failed` and exit code 1: a page in a
    compression that darzi does not read (for example `br` or `zstd`), a page
    in two or more layers of compression, and a compressed page that ends
    early. Before, darzi read the first two as text or unpacked each layer
    with no limit, and it used the part of the third that arrived.

### Clearer messages and clean-ups

- New refusals, each with one short line and exit code 2. Before, the command
  stopped with a traceback or accepted the value:
  - `facts finalize` and `facts render RUN --draft`: a `print_settings` level
    outside 1 to 5, before they build a file. Before, `finalize` stopped with a
    traceback when it built the Word file, and the draft did not test it.
  - `facts render RUN --draft`: a margin that is not a whole number. It now
    uses the whole print-settings check of `finalize`, with the same message.
    Before, the draft did not test it.
  - `facts check RUN`, `export`, `public`, `query`, `answers`, `fit`,
    `fill-plan` and `finalize` (also with `--handover`): a `rules.yaml` value of
    the wrong kind (for example `removed_items.terms: null`). The line names the
    key and the expected kind. Before, with `removed_items.terms: null`,
    `answers` and `fill-plan` accepted it, and the other commands stopped with a
    traceback.
  - `facts export`, `public`, `query`, `answers`, `fit`, `fill-plan` and
    `finalize --handover`: a list in `privacy_patterns.regex`. Write it as a
    mapping of name to pattern. Before, these commands stopped with a
    traceback.
- A named pipe at the path of a lock file is refused at once. Before, the
  command waited with no end.
- A job page with a charset that cannot decode the page (for example
  `punycode`) is read as UTF-8.
- The `facts` commands no longer print a warning about SOCKS support on
  stderr.
- Clearer texts: the run lock message names `fit`, which also takes the lock;
  the `approve` message for a bad signer or namespace in `facts.toml` says
  which characters are permitted; the hint for a `rules.yaml` entry that is
  not a mapping names `id` and `severity` only where the command reads them.
- Small clean-ups in the code and in the apply workflow text.

### Not in this release

- The whole fetch has no time limit. Each connect waits at most 5 seconds and
  each read at most 10 seconds, but a very slow site can keep a fetch running.
- Small internal points from the reviews wait for a later version. None of
  them changes what a command does.
- The limits of the checks are as in 0.2.0: read the README part "Limits of
  each step".

## 0.2.0 (18 November 2026)

The first release. It has the full apply workflow, from a job post to the
final files. It runs on macOS and Linux, with Python 3.12 or later; tested on 3.12 and
3.14.

### Facts

- `facts validate`: checks `master.yaml` and `rules.yaml`.
- `facts public`: prints the public copy of your facts as JSON.
- `facts query`: finds public bullets by tags and text.
- `facts check`: runs the rules on a run folder or a selection.
- `facts export`: writes `base.json` from an approved selection. It verifies
  the signature again.

### Runs

- `facts new-run`: makes a run folder and saves the job post first. The post
  comes from text or from a link (`--url`). The fetch of a link has a guard
  for the address and ignores the proxy settings of the environment.
- `facts fit`: checks `requirements.json` and writes the fit rating (green,
  yellow or red) to `fit.json`.
- `facts status`: lists all runs.

### Approval and final files

- `facts render`: renders a draft PDF through a local Resume Matcher, outside
  the run folder, and prints the page count.
- `facts approve`: shows the full text and signs your approval with an SSH
  key. You only.
- `facts finalize`: makes the final PDF and Word files and `manifest.json`.
  It verifies the signature again and does a round-trip check of each file.
- `facts finalize --handover`: makes the same files with no signature, for
  you to use by hand.

### Forms

- `facts answers`: checks `answers.yaml`.
- `facts fill-plan`: prints the only data that a form fill can use. It
  verifies the signature again.
- `facts submitted`: records that you submitted the application.

### Set-up and the agent

- `facts doctor`: checks the set-up (the approval key, Resume Matcher,
  LibreOffice and the settings).
- `facts workflow`: prints the steps for the agent (`facts workflow apply`).

## Known limits

Read the README part "Limits of each step".
