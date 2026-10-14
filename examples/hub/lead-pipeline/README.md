# lead-pipeline

The people at a company, each with a work email that has been checked. One call.

## What it does

1. Searches the company for people (routed: treg picks the provider).
2. For each person, finds a work email. Skipped when the search row already has one.
3. Verifies every email.
4. With `include_phone`, also finds a phone number.

It returns one table with the same columns whichever provider answered:
`name, first_name, last_name, title, company, linkedin, location, email, email_status, phone, note`.

## What it costs

- Every provider call, at cost, exactly as if you made it yourself.
- Plus **$0.01 per person whose email came back `valid` or `risky`**, at most $0.09 a run.
  People whose email is `invalid`, `unknown` or not found are returned but not charged the fee.

`email_status` is always one of four values, whatever verifier answered:
`valid` (the mailbox accepts mail), `risky` (the domain accepts everything, so the mailbox cannot
be proven), `invalid` (it will bounce), `unknown` (no verdict). `not_found` means no email.

## Limits

- At most 9 people per run, or 6 with `include_phone`. A run may make 20 calls.
- **A phone costs about $0.25**, fifty times an email. Turn `include_phone` on only when you need it.
- The provider calls may cost at most `max_spend_usd` (default $0.80). When the next call would
  pass it, the run stops early and returns the people already done, never an error.
- A company where no provider finds anyone returns an empty table, not an error.
- The run stops starting new people after 90 seconds and returns what it has, with
  `stopped_early: true` and a `note` on the people it did not reach. `spent_usd` says what the
  provider calls cost.

## Why use this instead of the three endpoints

You make one call instead of two or three per person, duplicates are removed before any money
is spent, and the fee follows the result you wanted: a person with an email you can use.
