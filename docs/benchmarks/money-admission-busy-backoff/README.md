# Money admission: local queue with bounded busy backoff

This is the historical 12 ms experiment. The refreshed-main comparison and selected 24 ms
trial evidence are in [current-main/README.md](current-main/README.md). The measurements below
retain their original source versions and limitations.

This experiment evaluates a change from fixed `U(3,10) ms` Redis busy retries to
equal jitter `U(3,6) ms`, then `U(6,12) ms` after further busy replies. The existing
per-process, per-org local queue remains. The first Redis attempt is immediate;
unavailable Redis still falls back immediately. One acquisition deadline covers
local waiting and all org leases. These retries do not repeat accounting or
upstream requests.

The measured benefit is fewer acquire attempts per completed admission. It comes
with longer wait tails and lower throughput in some scenarios. This is a draft
for review, not evidence that the change improves every workload or is ready for
a broad rollout.

## Revisions and evidence

The original baseline was `9ea9972c12396d8ae3f14f5f06c0e41a38fb1501` from #931.
The publication branch starts from main at
`18f0100e96aa5823e1a81503cea75ecaf43ed9b1`, which includes merged #931. The only
change between that baseline and main was a test cleanup; the admission source is
byte-identical. The candidate admission source also remains byte-identical after
moving the final net change onto main. No removed-local-queue implementation or
duplicate #931 commit is part of this PR.

- Baseline admission SHA-256: `291c2a0a3b9f0161ae55a4cc1437bbb623cbd764b645dae99e98e3147f3368da`.
- Candidate admission SHA-256: `2edf9dc1c7f575f7d7e22b95686da9f2542d139b37722dc0032a2076b19f2525`.
- [summary.csv](summary.csv): all 52 cells, including candidate and CPU-pressure runs.
- [measurements.json](measurements.json): raw worker CPU seconds, completion and
  attempt counts, lane progress, and cell summaries; final/pressure cells also
  retain Redis INFO CPU counters and exact parent elapsed time.
- [normalized.csv](normalized.csv): final two-run aggregates and normalized costs,
  including mean-of-run and pooled p99 results.
- [initial-burst-summary.json](initial-burst-summary.json): separate real-process
  queue-head and cancellation observations.

These are synthetic data, with no production logs, credentials, payloads, local
paths, temporary service URLs, process IDs or process-instance IDs. Full per-request
wait/lag arrays are not included in this compact publication. Pooled quantiles were
computed from those retained arrays; the published counters independently reproduce
CPU and attempt normalization, but cannot independently reconstruct pooled quantiles.

## Method and limitations

Each benchmark invocation starts a private loopback Redis, with an ephemeral port,
persistence disabled and its PID checked, then stops it on exit. Four spawned
Python processes each run an independent event loop and Redis client with 16 lanes:
64 concurrent admission scopes. Actual `money_admission.admit`, lease operations
and `RedisStore` execute. The body is only `asyncio.sleep`: no HTTP, PostgreSQL,
accounting writes or upstream work. Worker CPU is not production Web CPU.

| Scenario | Org count | Synthetic held duration |
|---|---:|---|
| hot_short | 1 | 2 ms |
| hot_mixed | 1 | 2 ms, with a per-request 2% probability of 200 ms |
| many_short | 8 | 2 ms |
| many_mixed | 8 | 2 ms, with a per-request 2% probability of 200 ms |

Every scope has one org. This is not a multi-org-batch performance experiment.
The functional tests cover sorted org acquisition, partial local/Redis ownership,
zero-org paths, deadline exhaustion, cancellation, late renewals and session cleanup.
The explicit settings are wait=5 seconds and lease=15 seconds. Initially, hot
scenarios have 16 waiters per process/org; many scenarios have two per process/org.
Lanes subsequently rotate orgs by `(process*16 + lane + serial) % org_count`.

The workload is closed-loop: complete one scope, immediately start another until
the fixed reception window ends, then drain. Policies therefore complete different
numbers and mixtures of long/short scopes. Equal concurrency and duration do not
mean equal work or a fixed external arrival rate. Per-org instantaneous concurrency
and per-request org/lane/time labels were not recorded.

Candidate sweep: 7 policies x 4 scenarios x one 4-second run = 28 cells. Final
comparison: 2 policies x 4 scenarios x two 8-second runs = 16 cells. CPU pressure:
2 policies x 4 scenarios x one 6-second run = 8 cells. These short samples do not
establish statistical significance, production capacity or starvation freedom.

Busy jitter uses `random.seed(311 + round + process*1000)`; each lane's hold RNG
uses `Random(311 + round + process*1000 + lane)`. Policy order uses a shuffle with
seed `1729 + round`. **Both final runs happened to execute current before
local_fixed: the order was not balanced.** Scenario order is fixed. OS scheduling
and shared jitter draws still vary.

Runtime: macOS arm64, Python 3.13.15, Redis 8.10.2. Functional tests also used
PostgreSQL 16.15 and Node 26.10.0. The CI environment uses Ubuntu, Redis 7.4 and
Node 22; local results do not substitute for the current PR's CI.

## Final comparison

Values below are old fixed polling -> bounded equal jitter. Window throughput
uses completed scopes within the two 8-second windows. Attempts/s uses total
attempts, including drain, divided by summed parent elapsed time. Normalization
uses total completed scopes, including drain. Thus attempts/s and window
throughput must not be divided to estimate attempts per scope.

| Scenario | Window scopes/s | Acquire attempts/s | Attempts/completed scope | Worker CPU s/1,000 scopes |
|---|---:|---:|---:|---:|
| hot_short | 372.25 -> 371.94 | 792.03 -> 694.04 | 2.130 -> 1.868 (-12.3%) | 0.6595 -> 0.6292 (-4.6%) |
| hot_mixed | 153.38 -> 147.13 | 563.08 -> 460.84 | 3.698 -> 3.103 (-16.1%) | 1.2039 -> 1.1209 (-6.9%) |
| many_short | 2857.38 -> 2791.06 | 3938.36 -> 3582.75 | 1.381 -> 1.287 (-6.8%) | 0.2590 -> 0.2533 (-2.2%) |
| many_mixed | 608.38 -> 567.88 | 1344.62 -> 1082.91 | 2.264 -> 1.993 (-12.0%) | 0.5686 -> 0.5782 (+1.7%) |

The historical summary CSV averages per-cell rates instead of weighting by elapsed
time, so its rates differ slightly. Worker CPU sums `process_time` from synchronized
start through drain and metric collection, excluding imports, connection warmup
and process startup. It includes the event-loop probe. One core is 100%.

| Scenario | Mean p50 old/new ms | Mean p95 old/new ms | Mean p99 old/new ms | Window throughput change | Loop lag p99 old/new ms |
|---|---:|---:|---:|---:|---:|
| hot_short | 110.77 / 112.45 | 477.66 / 517.48 | 680.77 / 697.07 (+2.4%) | -0.1% | 0.720 / 0.730 |
| hot_mixed | 289.79 / 259.81 | 1161.80 / 1288.41 | 1691.71 / 2133.32 (+26.1%) | -4.1% | 0.778 / 0.769 |
| many_short | 15.55 / 12.81 | 62.80 / 79.38 | 108.36 / 144.95 (+33.8%) | -2.3% | 0.436 / 0.667 |
| many_mixed | 2.70 / 1.96 | 415.67 / 504.21 | 906.65 / 830.14 (-8.4%) | -6.7% | 0.738 / 0.763 |

Quantiles use index `floor((N-1)*q)` on sorted waits, from entering `admit` until
the body starts, including drain. The table averages each run's quantile. Pooling
both runs' samples instead gives **hot_mixed p99 +7.7% and many_short +33.6%**.
These are different statistics. The largest final candidate wait was 3.112 seconds.
Loop lag is the overrun of a 5 ms sleep.

All 52 retained-local cells had zero fallback, KV errors, lease loss and residual
waiting/active counts; every lane completed at least once in its window. In the
final comparison, mean Jain process fairness changed from 0.9871 to 0.9618
(hot_short), 0.9826 to 0.9308 (hot_mixed), 0.9994 to 0.9945 (many_short), and
0.9898 to 0.9812 (many_mixed). Per-process and per-lane distributions are in the
published data. Aggregate progress is not a global FIFO or no-starvation guarantee.

Some rate reduction comes from completing less work. Using a common elapsed-time
denominator, `attempts/s = attempts/scope * completed scopes/s`. An accounting
decomposition first changes per-scope cost, then completion rate:

| Scenario | Attempts/s reduction | Per-scope cost contribution | Lower completion-rate contribution |
|---|---:|---:|---:|
| hot_short | 12.37% | 12.30 percentage points | 0.07 percentage points |
| hot_mixed | 18.16% | 16.09 percentage points | 2.06 percentage points |
| many_short | 9.03% | 6.81 percentage points | 2.22 percentage points |
| many_mixed | 19.46% | 11.95 percentage points | 7.51 percentage points |

This is not a causal estimate. In many_mixed, CPU/s falls about 7% while CPU/scope
increases 1.7%; lower completed work explains the apparent CPU-rate improvement.

## Parameter choice and external guidance

The sweep retains local queueing throughout: fixed polling, full jitter with a
3 ms floor at caps 12/24 ms, equal jitter at caps 12/24/48 ms, and decorrelated
jitter at cap 24 ms. Full-with-floor is `max(3 ms,U(0,ceiling))`; equal is
`U(ceiling/2,ceiling)`; decorrelated is `min(cap,U(3 ms,previous*3))`.

For many_short, candidate equal12 p99 was 138.804 ms, versus 268.822 ms for equal24,
433.109 ms for equal48, and 112.819 ms for fixed. Larger caps reduced attempts but
worsened the tail. Full12's hot_short attempts/scope was 2.228 versus fixed's
2.198; its lower sleep range did not provide a clear polling reduction. These
single-run observations motivated a conservative equal12 cap, not an optimum claim.

[AWS's comparison](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/)
supports capped backoff with jitter, but its simulation found equal jitter did
slightly more work and took longer than full jitter. Equal is not an AWS-endorsed
best choice. Here it preserves a nonzero sleep floor and reduces persistent busy
polling; full with an explicit floor is also a valid alternative.
[Redis's randomized retry guidance](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/#retry-on-failure)
aims to desynchronize contenders. That section concerns multi-node Redlock; this
change borrows retry randomization without adopting that lock architecture.

## Estimates and what cannot be inferred

Per 1,000 similar single-org admissions, the four measured scenarios save about
262, 595, 94 and 271 acquire attempts respectively. Busy failed attempts per scope
fall 21.4%-24.7%; total acquire attempts per scope fall only 6.8%-16.1% because
successful acquisitions still occur. Worker CPU savings per 1,000 scopes are
0.0303, 0.0830, 0.0057 and **-0.0096** seconds respectively (negative means more CPU).
These are not whole-Redis command or production Web CPU savings.

Given a Web completion count `W`, a possible accounting model is
`A = W*f*k + b`, where `f` is the fraction associated with eligible admissions,
`k` is admissions per such request and `b` is additional background activity.
Those quantities, multi-org behavior, contention/hold distributions and total Redis
commands are required before extrapolation. Web completion counts alone do not
identify `A`, and the benchmark's per-scope coefficients need not transfer across loads.

In an idealized, continuously busy capped phase, mean sleep changes from 6.5 ms to
9 ms. Ignoring RTT, polling frequency would fall by `1 - 6.5/9 = 27.8%`. With
average RTT `r` in ms, the model is `1 - (6.5+r)/(9+r)` (17.9% at 5 ms RTT).
This is a model, not a measurement or total Redis reduction. Initial retry mean
sleep is only 4.5 ms, shorter than the old policy, so short busy episodes may not
save attempts. Scheduling, lock turnover and acquisition successes also change the result.

## Additional observations

CPU-pressure runs add a separate process issuing eight bounded arithmetic Lua
commands per pipeline, then sleeping 10 ms. This is not a CPU quota/throttling
simulation. Redis CPU includes that extra load, whose progress can differ by policy.
Attempts/s still decrease, but hot_short worker CPU rises 0.9%, hot_mixed p99 rises
25.0%, many_short p99 rises 14.6%, and many_mixed throughput falls 6.9%.
Full metrics, including Redis CPU and lag, are in `summary.csv`.

A separate burst experiment starts four processes with 16 waiters each while an
external synthetic owner holds the keys for 50 ms. Only one distinct token per
process/org competes. With one org the sampled per-process connection peaks are
`[1,1,1,1]`; with eight orgs they are `[8,8,8,8]`. All 128 waiters across both
scenarios cancel cleanly, preserving the external tokens and leaving no waiting
or active scopes. The burst probe warms one connection; the throughput benchmark
warms 16 and therefore cannot establish connection-creation burst behavior.

A prior excluded experiment removed local queueing. It increased polling and CPU,
worsened tails and observed one deadline fallback. That implementation is not
delivered here; its separate historical evidence is retained outside this PR and
is not treated as validation of this candidate.

## Reproduction and validation

From the repository root, with the locked server dependencies and a local
`redis-server` available:

```bash
PYTHONPATH=src uv run --locked python scripts/bench_money_admission_contention.py --baseline-ref 9ea9972c12396d8ae3f14f5f06c0e41a38fb1501 --seconds 4 --rounds 1 --output /tmp/admission-candidates.json
PYTHONPATH=src uv run --locked python scripts/bench_money_admission_contention.py --baseline-ref 9ea9972c12396d8ae3f14f5f06c0e41a38fb1501 --seconds 8 --rounds 2 --variants local_fixed current --output /tmp/admission-selected.json
PYTHONPATH=src uv run --locked python scripts/bench_money_admission_contention.py --baseline-ref 9ea9972c12396d8ae3f14f5f06c0e41a38fb1501 --seconds 6 --rounds 1 --variants local_fixed current --pressure --output /tmp/admission-pressure.json
```

The PR description records full functional test commands/results against its final
publication SHA, separately from these unchanged-source benchmark observations.
It includes the complete SQLite suite, the full PostgreSQL/Redis file list from
`.github/workflows/ci.yml`, frontend unit/E2E, dashboard build, import boundaries,
catalog validation, package-asset assertions and full-history secret scanning.
Suites overlap: their pass counts must not be added as unique tests.
