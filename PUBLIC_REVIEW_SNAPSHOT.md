# Public Review Snapshot

Prepared: 2026-07-27

## Context and AI Assistance

MoneyMan is a personal, local research project that I direct and maintain. I set the research goals and safety boundaries and evaluate the results; AI-assisted coding tools were used during implementation, testing, documentation, and iterative review. AI assistance was also used to audit and sanitize this public snapshot and to create the linked sanitized development history.

The public repository began as a sanitized snapshot and keeps a deliberately short, publication-only history. The private working history contains raw-capture references, machine paths, local configuration, account-related material, and operating notes that were never intended for a public, general-purpose distribution. Rather than rewrite that history commit by commit, this repository presents a reviewed snapshot that preserves the source, tests, synthetic fixtures, research design, and functional intent.

The included tests and synthetic fixtures exercise the implementation. Some documented historical experiments cannot be independently reproduced from this repository alone because the associated market captures and generated datasets remain private.

## Preserved Development History

[`DEVELOPMENT_HISTORY.md`](DEVELOPMENT_HISTORY.md) renders every commit reachable from the preserved real branch heads with its date, public-safe subject, and actual parent relationship. It distinguishes the separate logger prototype from `main` and identifies GitHub's pull-request merge preview as synthetic rather than claiming that the branch was merged. Hashes, author metadata, emails, and private operational details are omitted.

## Publication Boundary

This snapshot includes source code, tests, synthetic fixtures, and substantive technical documentation. It intentionally excludes the complete private Git history, raw market captures, generated datasets, account exports, machine-local configuration, and internal operating notes.

The complete research repository remains preserved privately. This public snapshot is not the remote used by any unattended collector.

Copyright (c) 2026 Harrison Doyle. All rights reserved.
