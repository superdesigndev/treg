---
title: Lens API — profiles, quotes, execution and receipts
status: built; behind TREG_LENS_ENABLED, disabled by default
sources:
  - src/treg/routers/lens.py
  - src/treg/application/lens.py
  - frontend/src/lens/types.ts
  - tests/test_lens.py
related:
  - interface/lens.md
  - interface/api.md
---

# Lens HTTP contracts

All responses use `Cache-Control: private, no-store`; mutations enforce the existing same-origin
guard and native session/token authentication. Input objects reject unknown fields.

| Route | Input | Response |
|---|---|---|
| `GET /lens/projects` | — | `{projects: Project[]}` |
| `POST /lens/projects` | Profile + `request_id` | Project |
| `GET /lens/projects/{id}` | — | `{project: Project, runs: Run[]}` |
| `PUT /lens/projects/{id}` | Profile + integer `revision` | Project |
| `POST /lens/quotes` | `{kind:"onboarding",url}` or `{kind:"audit",project_id,project_revision,engines?}` | Quote |
| `POST /lens/runs` | `{quote_id}` | Run immediately, normally `running` |
| `GET /lens/runs/{id}` | — | Run, including a quote that has not started |

- **Profile:** `{brand,url,description,competitors:[{name,domain?}],questions:string[],engines:string[]}`.
  **Project** adds `{id,revision,can_edit,created_at,updated_at}`. Research-only competitor reasons
  are excluded from profile writes. `request_id` is 8–80 ASCII letters/digits/underscores/hyphens;
  a UUID works. Reusing it for the same user and normalized creation input returns the saved project;
  different input/user returns 409. PUT atomically compares `revision`; stale writes return 409.
- **Quote:** `{id,run_id,kind,total_cost_micro,expires_at,note,currency:"USD",input,project_id,project_revision}`.
  IDs are identical and allocated before execution. Input and catalog request bodies are frozen
  server-side; the client never chooses an endpoint. Quotes expire after five minutes. A changed
  project revision invalidates an audit quote before claim; changed catalog/tier/pricing is rejected
  before execution and checked again before each provider request.
- **Run:** `{id,kind,project_id,state,created_at,started_at,completed_at,result,error,quote}`.
  States are `quoted`, `expired`, `running`, `completed`, `failed`, `interrupted`. A completed run
  can contain partial failures or uncertain charges; inspect its result rather than assuming success.
- **Result:** `{kind,input,results,errors,attempts,known_cost_micro,total_cost_micro,cost_known,completed_at}`.
  Onboarding adds `research:{brand,description,competitors,questions,sources}`. Audit answers contain
  `{id,question,engine,answer,citations,mentioned_brands,brand_mentioned,position,charged_micro,cost_known,source_kind,provider,completed_at,truncated?}`.
  Attempts retain their endpoint, state, optional engine/question/call reference, charge and detail.
  Errors retain inspectable details and available call references; absent final charges are null.
