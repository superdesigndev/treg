---
name: code-quality
description: Use when writing or reviewing code, comments, tests, or docs.
metadata:
  internal: true  # repo tooling: hidden from `npx skills add superdesigndev/treg`
---

# Code quality

1. **Enforce, don't instruct.** If a machine can check a rule, add the check.
   Apply these rules to code you change; fix old code in its own PR.

2. **Comments: none by default.** Keep one only for behavior we cannot change
   (vendor, platform, protocol) or a public contract. A reason worth keeping
   moves to a test, the docs, or the commit message.

3. **Tests must be able to fail.** Test behavior through the real entry point;
   if a test still passes with every dependency returning None, it is fake.
   Break the behavior, show the failing run, then land it. Never turn CI
   green by skipping, retrying, sleeping, or loosening an assertion.

4. **Docs say only what code cannot.** Keep a sentence only if, without it,
   the next person would change the code wrongly. Each fact once, where it
   belongs; when docs and code disagree, fix the doc.
