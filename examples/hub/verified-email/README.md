# verified-email

One person's work email, found and then checked, in one call.

## What it does

1. Finds the email (routed: treg picks the provider). Give a `linkedin_url`, or `full_name` +
   `domain`, or `first_name` + `last_name` + `domain`.
2. Verifies it (routed). When the finder itself vouched for the mailbox (`verified: true`), the
   verify step is skipped: no second charge for the same fact.

`email_status` is always one of four values, whatever verifier answered: `valid` (the mailbox
accepts mail), `risky` (the domain accepts everything, so the mailbox cannot be proven), `invalid`
(it will bounce), `unknown` (no verdict). `not_found` means no email.

## What it costs

- The provider calls at cost, usually under a cent for both.
- Plus **$0.01, only when the email is `valid` or `risky`**. An `invalid`, `unknown` or missing
  email is returned but not charged the fee.

## When to use the lead pipeline instead

This tool starts from a person you already have. `treg-hub.lead-pipeline` starts from a company
and finds the people first.
