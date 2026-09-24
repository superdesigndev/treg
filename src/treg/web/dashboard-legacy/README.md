# Deprecated Dashboard: temporary rollback artifact

**DEPRECATED. Scheduled for removal after rollout, not a maintained frontend.**
All new features and routine fixes belong in `frontend/`; do not backport them here or refresh
this snapshot during normal main-branch syncs. If a critical security or compatibility problem
requires changing the fallback, explicitly reassess the rollout before regenerating the snapshot.
Retirement criteria and the removal checklist live in [frontend/README.md](../../../../frontend/README.md#retire-the-deprecated-dashboard).

Snapshot of the dashboard and onboarding/tutorial JavaScript from `81e84d6e19e3b54f7b591d1ea45e99c8c7a4560a`.
Generated with `git show <revision>:src/treg/web/<path>` (dashboard-tour maps to tour).
Vue's global runtime and license are generated from the pinned npm package by
`frontend/scripts/copy-runtime.mjs`; its bytes match the snapshot version. It is not checked in.
Script URLs are rewritten to `/app/legacy/assets/<revision>/<path>`; inline code is unchanged.
Do not hand-edit this snapshot. New frontend work belongs in `frontend/`.
Shared images, fonts and tutorial content retain existing public URLs. Tracking scripts retain
their server-rendered routes so runtime analytics configuration and advertising opt-out still apply. Legacy CSS is inline;
it does not load the redesigned dashboard stylesheet. Remove this directory after rollout.
