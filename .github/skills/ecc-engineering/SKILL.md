---
name: ecc-engineering
description: "Required workflow for planning, implementing, testing, reviewing, and verifying code changes in this project, adapted from affaan-m/ECC."
---

# ECC engineering workflow

Use this workflow for every code change in this project. It is adapted for this
repository from [ECC](https://github.com/affaan-m/ECC), an agent engineering
toolkit. It captures the plan, test, implement, review, and verify cycle without
installing ECC's full runtime or assuming every agent host supports the same
features.

## Procedure

1. **Understand the task.** Read the relevant code and project documentation,
   including [`../../../docs/README.md`](../../../docs/README.md) for mailbox,
   OAuth, OTP, and privacy requirements. Identify expected behavior and
   constraints before editing.
2. **Plan proportionally.** For a non-trivial change, write down a short
   implementation and verification plan. Keep a small change focused; do not
   add unrelated refactors.
3. **Test the behavior.** Find the existing test and validation commands. Add
   or update focused tests for changed behavior. Prefer testing the failure and
   boundary cases that could make the result incorrect.
4. **Implement surgically.** Follow existing patterns and preserve the public
   contract unless the task explicitly changes it. Surface errors clearly;
   never substitute a success-shaped fallback for a failure.
5. **Verify.** Run the smallest relevant test, build, type-check, or linter.
   Review its output and fix regressions caused by the change. If validation
   cannot run, state why rather than implying success.
6. **Review the final diff.** Check for accidental files, unrelated edits,
   missing tests or documentation, and regressions in correctness, security,
   privacy, compatibility, and resource usage. Request an independent review
   only when the change warrants it and the environment supports one.
7. **Summarize accurately.** Report the implementation, checks run and their
   outcomes, and any remaining limitations.

## Project-specific safeguards

- Work only with mailboxes the user or organization owns or has explicitly
  authorized.
- Never put mailbox passwords, OAuth tokens, OTPs, or real email content in
  source, tests, logs, fixtures, or telemetry. Use synthetic data and
  authorized test infrastructure.
- Preserve the separation between Microsoft Graph API and IMAP/OAuth2
  providers; consult the architecture and permission guidance in the project
  documentation.
- Do not add behavior that bypasses authentication, MFA, tenant consent, or
  mailbox access controls.
