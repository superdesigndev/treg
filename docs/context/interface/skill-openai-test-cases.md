---
title: Test cases for the ChatGPT plugin submission
status: reference
sources: []
related:
  - interface/skill.md
  - interface/skill-openai-submission.md
  - interface/skill-openai-tool-justifications.md
---

# Test cases for the ChatGPT plugin submission

Copy each block into its field. Every positive case below was run against production before it was
written here, so the expected output describes what actually happens rather than what should.


## Test Case 1

**Scenario**

Find an API for a task when you have no key for it

**User prompt**

Find a way to get Google search results for a keyword, and show me the options with their prices.

**Tool triggered**

catalog_search

**Expected output**

A short list of catalog endpoints that return search results, each with its provider, its exact price
per call in USD, and whether it can be called without the user owning an API key. Prices are real,
not estimates. Nothing is charged, because searching the catalog is free.


## Test Case 2

**Scenario**

Check the exact price of one endpoint before spending anything

**User prompt**

Show me the details and exact price for the DataForSEO Google organic search endpoint.

**Tool triggered**

catalog_get

**Expected output**

The full record for that endpoint: its required and optional parameters, the exact cost per call
(0.004 USD), how that price was sourced, and other providers offering the same capability so the
model can compare. No call is made and no money is spent.


## Test Case 3

**Scenario**

Actually call a third-party API without holding its key, and report the cost

**User prompt**

Get the Google organic results for "payment processing api" and tell me the price first, then what it cost.

**Tool triggered**

catalog_get, then call

**Expected output**

The model states the price (0.004 USD) before calling, then returns real Google search results with
domains, titles and ranks. The response includes cost_usd with the amount actually charged, and the
team's prepaid balance drops by that amount. The user never supplies a DataForSEO key: treg injects
its own server-side and bills the team.


## Test Case 4

**Scenario**

Check the prepaid balance

**User prompt**

What is my treg balance right now?

**Tool triggered**

balance

**Expected output**

The team name and the current prepaid balance in USD, plus any spend still in flight. If the previous
test case ran, the balance is lower by exactly the amount that call cost. Read-only, nothing changes.


## Test Case 5

**Scenario**

See which of the team's own registered tools can be called without holding the credential

**User prompt**

What tools has my team registered that I can call without having the API key?

**Tool triggered**

my_tools

**Expected output**

A list of the tools this team registered with treg, each with its name and base URL. Credentials are
never included, only what can be called. On the demo account this list may be empty, which is the
correct answer for a team that has registered nothing, and the model should say so rather than invent
entries.


# Negative test cases

Prompts where the model may think treg is relevant and it is not. The plugin should stay out of the
way for all three.


## Negative Test Case 1

**Scenario**

A general knowledge question that needs no API at all

**User prompt**

What is the difference between REST and GraphQL?

**Why it should not trigger**

This is explanatory knowledge the model already has. treg calls third-party APIs and would spend
money to answer nothing. No tool should be invoked.


## Negative Test Case 2

**Scenario**

Writing code that uses an API, rather than calling one

**User prompt**

Write me a Python function that calls the Stripe API to list customers.

**Why it should not trigger**

The user wants source code, not live data. Nothing should be called and no balance should be spent.
treg is for fetching real data, not for generating examples about APIs.


## Negative Test Case 3

**Scenario**

Asking about a personal or local matter with no API behind it

**User prompt**

Help me write a polite reply to my colleague's email about moving our meeting.

**Why it should not trigger**

There is no external data to fetch. The word "email" may look related to treg's people and email
enrichment endpoints, but the task is composition, which the model does alone.
