---
title: Recipes — the decisions, from the owner interview
status: settled 2026-09-07, no code written yet
---

# Recipes — the decisions

A **recipe** is a tool made of tools. It calls tool one, calls tool two, merges the two answers,
calls tool three on the merged result, and sends an email. treg runs the whole pipeline for the
caller, on the caller's account.

This file is the record of a question-and-answer session with the owner on 2026-09-07, before any
design work. **The questions are kept word for word.** Under each one is the answer the owner
approved. Where the owner added something the question did not ask, it is written under "The owner
added".

The purpose: no design decision below rests on a guess.

---

## The frame the owner set

- The team writes many recipes first. Then outside people are invited to submit their own.
- The contribution road is a pull request: create, submit, checked, verified, approved, added to
  the catalog.
- The catalog page shows every recipe with its state: built by treg, verified, submitted and not
  yet verified.
- The script language is JavaScript. One language everybody knows. Interpreted or compiled is an
  implementation choice.
- Some recipes are only steps and need no script. Those need no sandbox.
- treg's own sandbox runs the recipes treg built or verified. A team that wants its own sandbox is
  a later, paid thing.
- The script never holds a key. It names a tool and gives inputs; treg injects the credential.
- Every metered step deducts from the caller's balance, exactly as a direct call does today. There
  is no extra fee for the recipe itself.
- Version one runs inside the request, start to finish.
- The product is called the tool hub internally. One item of this kind is a recipe.

---

## Round 1 — the shape of the contract

**1. Does a recipe declare the tools it may call, in its manifest, before it runs? That list is what
a reviewer reads and what stops a script from reaching anything else.**
Yes, required.

**2. What happens when a recipe needs a key the caller does not have?**
Refuse before any step runs, and name exactly what to connect, so nothing is charged for a run that
cannot finish.

The owner added: the caller reads the message, adds the key, and continues. When the tool needs no
key from the caller, the call is served on treg's key and deducted from the caller's balance.

**3. Does a recipe declare its inputs and its output fields, the way a catalog endpoint declares its
parameters?**
Yes. That is what puts it in search with a price, and what lets an agent call it without reading the
code.

---

## Round 2 — how it reaches a caller

**1. How does an agent call a recipe?**
treg has one call road today. A recipe could ride it with its own id, or get a new road of its own.
The same road, `/call/<recipe id>`. Then the CLI, the MCP tools and every plugin serve recipes on
day one with no change.

**2. Does a recipe show up in the same search as endpoints?**
An agent searches by the job it wants. A recipe is a bigger job made of several endpoints.
Yes, the same search, with a mark that says it is a recipe and how many steps it makes.

**3. What price do we show before a run?**
A single endpoint has one price. A recipe's price depends on its inputs, because a step may loop
over rows.
Show the price of one run at the default inputs, and let the caller set a ceiling for the whole run.
The run stops when the next step would pass the ceiling.

**4. May a recipe call another recipe?**
It is useful, and it also makes loops and a cost nobody can read.
No in version one. A recipe calls tools only, one level deep.

**5. What does a recipe return?**
It could return the last step's answer as it came, or one object it declares.
One JSON object the recipe declares, so an agent knows the field names before it calls.

**6. Step four of six fails. What does the caller get?**
Either an error, or the part that finished with a warning.
An error that says which step failed, what ran, and what was charged. A recipe may mark a step as
allowed to fail, and then the run goes on.

**7. How long may one run take, and how many steps?**
The run sits inside one HTTP request, so a caller is waiting.
120 seconds and 20 steps in version one, both declared per recipe under those ceilings.

**8. Where does a recipe's source live?**
The contribution road is a pull request, so the source is probably files in the repository, like the
catalog. The other choice is a row in the database, edited in the dashboard.
Files in the repository, one folder per recipe, exactly like the catalog. A dashboard editor can
come later for a team's own private recipe.

**9. Is a recipe that sends an email marked differently from one that only reads?**
A read can be retried safely. A send cannot.
Yes. A recipe declares that it writes, the catalog shows it, and the write ones are separated the
same way the Claude connector surface already separates read calls from write calls.

---

## Round 3 — the life of a recipe

**1. What happens when a recipe changes after people use it?**
The author fixes a bug or changes a field name. A caller may depend on the old shape.
Every recipe carries a version number. The catalog serves the newest. A caller may ask for an older
one by naming the version in the id.

**2. One file or two?**
A recipe with no logic needs only a description of its steps. A recipe with logic also needs a
script.
Always one manifest file. A script file beside it, only when it is needed. No script means the steps
in the manifest run in order.

**3. How do we know a submitted recipe works before we merge it?**
A reviewer cannot judge a pipeline by reading it.
Every recipe ships a check file: sample inputs and the least it must return. The review runs it
once, live, and the result goes in the pull request.

**4. Does a recipe get a health state, like an endpoint's reliability?**
An endpoint the recipe uses can break, and then the recipe breaks.
Yes. A scheduled run of the check file, and the catalog shows the state. A failing recipe sinks in
search and says why.

**5. Where does the script run?**
Inside the web server's own process, or in a separate short-lived process.
A separate process, with no network of its own, a memory cap and a time cap. A bad script then
cannot touch the server or another run.

The owner added: the script has no network at all. Every tool call goes out through the treg context
object the script is given. There is no other road to the network.

**6. What may the script see?**
It could see only its own inputs, or also the team's name, the balance, the token.
Only its inputs and the answers of the steps it ran. Nothing about the team, no balance, no token,
no environment.

**7. What does the caller see about the run?**
A pipeline is opaque when it only returns the final answer.
An ordered list of steps with the tool, the status, the time and the cost of each, plus the recipe's
own log lines. The step bodies stay out unless the recipe returns them.

The owner added: this visibility is important. A person watching sees the cascade of tools and knows
it is running. An API caller sees the same as data.

**8. A recipe that sends an email, retried after a timeout. Does it send twice?**
treg already has a retry label for single calls.
The same label covers the whole run. A retry with the same label returns the stored result and runs
nothing again.

**9. How many recipes may one team run at the same time?**
Each run holds a request and a sandbox for up to two minutes.
Four at once per team, and a clear refusal above that, saying when to try again.

---

## Round 4 — data, identity and ownership

**1. In a recipe with no script, how does step two use the answer of step one?**
The steps must pass data. A full expression language is one more thing to learn and to review.
Two things only. A placeholder that reads one field of an earlier step, and a repeat over a list.
Anything more needs the script.

The owner added, and this shapes the design: a step names an earlier step and a field of it, nested
fields included, as `$0.field.subfield`. Those references make the dependency graph. A step that
names nobody has no dependency and starts at once. treg runs every ready step together, then the
next ready set, until the end. Nothing is declared by hand. Example: four steps, where three needs
two and two needs one, and four needs nobody. Then four and one start together, two follows one,
three follows two. Another example: three needs one and two separately, and four needs nobody. Then
one, two and four start together, and three follows when one and two are both done. The algorithm
must stay simple and readable.

**2. When a recipe names a tool, does it name a catalog id or a team's own tool name?**
A team names its own tools itself. One team calls its Stripe tool "stripe", another calls it
"billing". A shared recipe cannot know that.
A catalog id only. treg's ladder then uses the team's own key for that provider when they have one,
and treg's key when they do not.

**3. Does every run get an id and a page that shows the cascade?**
You want the caller to watch the pipeline as it goes.
Yes. The reply carries a run id and the step list. A page at that id shows the same cascade, and the
dashboard reads it.

**4. Do we keep the run after it ends, and for how long?**
Each step is already written to the call log today. The run level is new.
One row per run with its steps, kept 30 days. A caller sees their own runs. An admin sees the
team's.

**5. Does each step pass the caller's own rules?**
A team blocks a host. A member is limited to three tools.
Yes. Every step passes the same gates as a direct call. A blocked step stops the run and names the
rule that stopped it.

**6. May a recipe ask the caller for a secret that treg does not store?**
Some sites need the person's own cookie, not an API key. That is what we hit in Crawl4AI.
Yes, an input marked secret. Never logged, never stored, never in the trace, and the run is never
cached.

**7. A recipe sent the email, then step five failed. Do we undo the email?**
An email cannot be taken back.
No undo. The trace says exactly what was done. The rule for authors: put the writes last.

**8. A recipe with no script needs no sandbox. May a team keep a private one from day one?**
The sandbox is the part that waits for a paid plan.
Yes. A team may register a private recipe with steps only, today. A private recipe with a script
waits for the team sandbox.

**9. What does a recipe id look like, and who owns the name?**
The catalog uses dotted ids made of a provider and a job. Recipes come from many authors.
The author's handle plus the recipe name, as one id. treg's own recipes use treg's handle. A name
belongs to its author and cannot be taken.

**10. Can an author run a recipe from their own folder before the pull request?**
Waiting for a review to find a typo is slow.
Yes. One command runs the recipe from the folder with the author's own token. Every step goes to the
real treg and is charged as usual.

---

## Round 5 — the runner's rules

**1. Do steps refer to each other by number or by name?**
`$0.data.email` is short. But if you insert a step in the middle, every later number shifts.
Each step gets a short name, and a reference reads that name, like `$lead.data.email`. The number
stays valid as an alias for the ones already written that way.

**2. How many steps may run at the same time inside one run?**
Four independent steps could all start at once. Each one holds a connection and money.
Four at a time. The run's own money ceiling and time ceiling still apply above that.

**3. A step repeats over a list of 500 rows. What stops it?**
That is 500 calls from one recipe run.
Each item counts as a step. So the step count and the money ceiling both count them, and the recipe
declares its own smaller limit.

**4. Two steps run together. One fails. What happens to the other?**
It is already sent and already paid.
Let it finish, start nothing new, and return the error with the full trace. The trace shows the one
that finished and the one that failed.

**5. Is there a condition in the no-script road?**
"Run step three only if step two found something" is the common case. A full condition language is a
language.
One rule only. A step may say "skip me when the value I read is empty". The trace marks it skipped.
Anything more needs the script.

**6. Is there a step that only changes data, with no tool call?**
Your first example merged two answers before the next call.
No such step in the no-script road. A step that reads two earlier steps already receives both, and
that is the merge. Real logic goes in the script.

**7. Does the caller see progress while the run goes, or only the final answer?**
The dashboard can poll the run page. An API caller is holding one request open.
Version one returns the whole trace at the end, and the run page polls while it runs. A live stream
on the API is a later addition.

**8. How is the recipe's declared output filled?**
The recipe promises field names. Something must fill them.
The output block maps each field to a reference, with the same syntax as the steps. A script recipe
returns the object itself.

**9. Does treg retry a step that fails?**
treg does not retry a single call today, by design.
No retry in version one. The run stops and the trace says which step failed and why.

**10. A step returns zero rows. Is that a failure?**
No result is a normal answer for a search.
No. Zero rows is data, not a failure. Only a refused or failed call stops the run, and the "skip when
empty" rule handles the rest.

---

## The backlog, in the order it was named

1. **A job queue.** Version one runs inside the request. A queue and a worker come later, and then a
   recipe may run far longer than 120 seconds.
2. **A recipe that calls a recipe.** With loop control, so a cycle cannot form and the cost stays
   readable.
3. **Retry of a failed step.**
4. **Live progress on the API**, not only on the run page.
5. **A team's own sandbox.** A paid plan, and the door to a private recipe that carries a script.
