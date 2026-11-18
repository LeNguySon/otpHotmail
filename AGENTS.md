# Instructions for AI coding agents

These instructions apply to every AI agent or subagent that reads this repository.

## Required ECC workflow

Before making any code change, read and follow the project skill at
[`.github/skills/ecc-engineering/SKILL.md`](.github/skills/ecc-engineering/SKILL.md).
If your environment does not automatically load project skills, open that file
manually. Apply the workflow to implementation, tests, configuration, and code
review; do not treat it as optional.

For project behavior and security requirements, also consult
[`docs/README.md`](docs/README.md). Never use real mailbox credentials, access
tokens, email contents, or OTPs in development or tests; use authorized test
mailboxes and synthetic data only.

## Completion

Report what changed and which checks actually ran. Do not claim a check passed
unless it was executed, and call out any unavailable validation or unresolved
issue.
