# Changes

## Unreleased

Small corrections. No check rule, hash or file format changes.

- `facts finalize` and `facts render RUN --draft` refuse a `print_settings`
  level outside 1 to 5 with one short line and exit code 2, before they build
  a file. Before, `finalize` stopped with a traceback.
- `facts render RUN --draft` now uses the whole print-settings check of
  `finalize`: it also refuses a margin that is not a whole number, with the
  same message.
- `facts check`, `export`, `public` and `query` refuse a `rules.yaml` value of
  the wrong kind (for example `terms: null`) with a short line that names the
  key and the expected kind. Before, they stopped with a traceback.
- The `facts` commands no longer print a warning about SOCKS support on stderr.
- The run lock message names `fit`, which also takes the lock.
- The `approve` message for a bad signer or namespace in `facts.toml` says
  which characters are permitted.
- A named pipe at the path of a lock file is refused at once. Before, the
  command waited with no end.
- A job page with a charset that cannot decode the page (for example
  `punycode`) is read as UTF-8.
- The hint for a `rules.yaml` entry that is not a mapping no longer names
  `id` and `severity` in commands that do not read them.
- Small clean-ups in the code and in the apply workflow text.

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
