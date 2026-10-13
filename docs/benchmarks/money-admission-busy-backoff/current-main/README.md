# Current-main comparison: selected 24 ms busy cap

The candidate retains the per-process, per-org `asyncio.Lock` and the existing immediate first
Redis attempt. Only `busy` acquisition responses use equal jitter: 3–6 ms after the first busy
reply, then 6–12 ms, then 12–24 ms. The selected cap is 24 ms. Unavailability still immediately falls
back after the existing command budget; accounting and upstream calls are not retried.

## Code and correctness

- Refreshed main baseline: `c321776143e0690f780281fa1bb5051a15594ed1`.
- Actual 24 ms default tested: `a3b301a7104a4f9865beedc3bfec6e850622101b`.
- Earlier core and cap-sweep source: `de4deeb2f9714e0c573ae6c0a86441b7559a0867`, whose default
  was 12 ms; 24/48 ms were explicit runtime cap overrides. Those runs use main
  `92419f7b323077f7a5b60a241ffce828dc9947c3`. Both baseline admission and KV modules are
  byte-identical between that commit and refreshed main. Candidate admission differs only in
  the selected cap; candidate KV and the harness are unchanged.
- Main merges: `25519bea597ed74173f94f4a6b0e998c8f02d382` and
  `330822ec410afb482bf024b0300b912ebe6c0fc5`. The prior PR commits, including cancellation/deadline
  tests and recovered-renewal/loss-reason telemetry, remain in history.
- Merged #947 (`07944d0a2d52d7b552bd2c660f9a1dcc99293f60`) is an ancestor. Both policies retain
  its 1 s socket/connect timeout, 200 ms optional deadline, 1.5 s lease-attempt deadline and
  2.5 s retry budgets; the renewal budget is additionally clipped by earliest confirmed lease
  validity, while release cleanup keeps its own budget. SET is not retried after
  unavailability. The new #947 retry test expects `recovered` only in the candidate's renewed
  return contract; its timing and same-token assertions remain unchanged.
- This is a Draft PR code/test update; no live configuration, SQL, PR merge or deployment is performed.

## Method

The committed harness executes actual admission and KV code in four spawned Python processes,
with 16 concurrent lanes per process and real private loopback Redis. Each baseline process
loads both its admission and KV sources from the pinned Git commit; candidate processes load
both working-tree sources. Connection warmup (PING) completes before CPU measurements begin;
there is no workload warmup to establish a steady state. Each cell starts
new worker processes. Each run group owns a disposable Redis instance, validates its PID,
disables persistence, uses an ephemeral port and shuts it down afterwards.

Four synthetic scenarios cover one hot org or eight orgs, with 2 ms held scopes, or 2 ms held
scopes with a 2% per-request chance of a 200 ms hold. The body is `asyncio.sleep`, not a real
HTTP request, PostgreSQL transaction or accounting write. Every scope has one org; multi-org
ordering, partial acquisition and financial invariants are covered by correctness tests.

- Initial 12 ms comparison: four 15-second reception windows per scenario/policy, then drain (32 cells).
- Extra Redis CPU pressure: two 12-second windows per scenario/policy (16 cells). Finite batches
  of eight Lua commands perform 50,000 additions each, followed by a 10 ms pause.
- Injected command latency: two 12-second windows per scenario/policy (16 cells), with a 2 ms
  cancellable sleep before and after each actual SET/EVAL, inside its original deadline.
- Cap comparison: main, 12, 24 and 48 ms; four balanced 8-second windows per scenario/policy
  (64 cells). Each policy occupies every execution position once in each scenario.
- Actual selected 24 ms default: repeat pressure and injected-delay groups against refreshed main,
  two balanced 12-second windows per scenario/policy (32 cells).
- A separate four-cell one-second smoke check precedes the measurements and is excluded from
  the performance tables.

Policy position is balanced within each scenario: AB/BA/BA/AB or its inverse for four rounds,
AB/BA for two. Workload and jitter use separate RNGs and matching seeds for each pair. The raw
execution order and seeds are retained. No concurrent test suite or other benchmark was started by
this task during measurement. This does not establish exclusive control of the host.

Attempts/completion and CPU per 1,000 completions divide totals by all completed scopes,
including drain. Window throughput divides only in-window completions by reception duration.
CPU/s and attempts/s use the same drain-inclusive parent elapsed time. They must not be divided
by window throughput. CPU includes admission/probe work; Redis CPU under pressure also includes
background Lua work and is not isolated admission CPU.

The main table reports the mean of each run's p95/p99, with per-cell values and ranges retained.
Pooled quantiles are separately labeled. Process/lane Jain fairness uses window completions;
Redis-mode process fairness includes drain and excludes fallback. A lane's maximum entry gap
includes its preceding held body and cleanup; it is not a pure starvation-wait measurement.

## Limits

Short, closed-loop synthetic runs are not a replay of production. Matching RNG seeds does not
produce identical completion counts, scheduling or long-hold counts. Four/two repeats and
balanced order do not establish statistical significance. Loopback, injected application delay
and added Redis work do not reproduce a managed service's network, CPU quota or throttling.
The pressure harness does not count completed Lua commands; logs and Redis CPU counters are
checked for evidence that the background workload ran. No absence-of-starvation or global FIFO
claim follows from nonzero per-lane completion counts. Worker CPU results cannot be relabeled
as Web-server CPU improvements, and fewer acquisitions cannot be relabeled as fewer total
Redis commands. Runtime cancellation is tested; task-cancel tests alone do not prove an actual
HTTP disconnect will cancel a request.

## Selected cap: balanced comparison

The four-policy sweep compares fixed 3–10 ms polling with equal jitter capped at 12, 24 and
48 ms. All four retain the local org queue. `current` in this group means **12 ms**; `equal:24`
is the selected algorithm with an explicit cap override. The later selected groups exercise
**24 ms as the actual source-code default**. Do not combine group labels without their source.

| Scenario / cap | Attempts/completion | Worker CPU s/1,000 completions | Window scopes/s | Mean p95 ms | Mean p99 ms | Mean process Jain | Fallback / total |
|---|---:|---:|---:|---:|---:|---:|---:|
| hot_short / main fixed | 2.203 | 1.0112 | 347.3 | 444.5 | 616.5 | 0.9906 | 0/11371 |
| hot_short / 12 ms | 1.930 | 0.9304 | 347.5 | 473.0 | 654.0 | 0.9817 | 0/11376 |
| hot_short / 24 ms | 1.529 | 0.8536 | 345.0 | 609.1 | 967.9 | 0.9741 | 0/11295 |
| hot_short / 48 ms | 1.310 | 0.7833 | 344.2 | 791.2 | 1344.7 | 0.9442 | 0/11272 |
| hot_mixed / main fixed | 3.843 | 1.6799 | 144.1 | 1206.3 | 1703.8 | 0.9931 | 0/4868 |
| hot_mixed / 12 ms | 3.208 | 1.6202 | 140.2 | 1243.1 | 1988.3 | 0.9531 | 0/4743 |
| hot_mixed / 24 ms | 2.188 | 1.2889 | 141.9 | 1623.0 | 2349.8 | 0.9567 | 0/4796 |
| hot_mixed / 48 ms | 1.604 | 1.0879 | 144.3 | 2227.6 | 4078.6 | 0.9140 | 31/4875 |
| many_short / main fixed | 1.376 | 0.2632 | 2857.0 | 66.3 | 116.0 | 0.9987 | 0/91680 |
| many_short / 12 ms | 1.291 | 0.2586 | 2782.6 | 78.9 | 147.0 | 0.9954 | 0/89298 |
| many_short / 24 ms | 1.143 | 0.2342 | 2739.7 | 105.4 | 300.9 | 0.9792 | 0/87926 |
| many_short / 48 ms | 1.072 | 0.2017 | 2876.7 | 61.5 | 512.1 | 0.9358 | 0/92311 |
| many_mixed / main fixed | 2.265 | 0.6487 | 583.1 | 457.9 | 882.2 | 0.9725 | 0/18915 |
| many_mixed / 12 ms | 1.952 | 0.5754 | 598.5 | 423.8 | 789.0 | 0.9923 | 0/19409 |
| many_mixed / 24 ms | 1.475 | 0.5483 | 598.2 | 426.2 | 936.2 | 0.9754 | 0/19400 |
| many_mixed / 48 ms | 1.291 | 0.4955 | 569.5 | 472.3 | 856.7 | 0.9867 | 0/18480 |

Compared with fixed polling in this matched sweep, 24 ms reduced attempts/completion by
16.9–43.1% and worker CPU/completion by 11.0–23.3%. Throughput changed by −4.1% to +2.6%.
The many-org short-scope p99 rose from 116 to 301 ms; hot-org mixed-scope p99 rose from
1.70 to 2.35 seconds. The 24 ms cells had no fallback. This does not establish zero future
fallback or starvation freedom.

48 ms is excluded from the trial recommendation: the hot/mixed scenario had **31 wait-timeout
fallbacks among 4,875 total completions (0.636%)**, with only 4,844 Redis-acquired completions.
Two runs reached approximately five seconds at p99. Redis-acquired, drain-inclusive process
Jain averaged 0.91775, minimum 0.8384; the table's Jain instead uses all window completions.
Fallback bypasses Redis admission, so aggregate throughput must not be interpreted as preserved
admitted throughput. Attempts per Redis-acquired completion were 1.6142 (versus 1.6039 per all
completions). The raw data do not identify exact Redis-only window throughput. 48 ms also raised
many/short p99 to 512 ms despite a lower p95, illustrating uneven progress hidden by lower quantiles.

See [cap-normalized.csv](cap-normalized.csv), [cap-aggregates.json](cap-aggregates.json),
[cap-summary.csv](cap-summary.csv) and [cap-measurements.json](cap-measurements.json).

## Actual 24 ms default under pressure and delay

| Group / scenario | Attempts/completion main → 24 ms | CPU s/1,000 main → 24 ms | Window scopes/s main → 24 ms | Mean p95 ms main → 24 ms | Mean p99 ms main → 24 ms | Fallback main / 24 ms |
|---|---:|---:|---:|---:|---:|---:|
| selected-pressure / hot_short | 2.237 → 1.555 (-30.5%) | 0.8577 → 0.7291 (-15.0%) | 333.5 → 332.4 (-0.3%) | 416.4 → 655.7 (+57.4%) | 539.3 → 980.3 (+81.8%) | 0 / 0 |
| selected-pressure / hot_mixed | 3.844 → 2.174 (-43.4%) | 1.6739 → 1.2015 (-28.2%) | 139.3 → 146.0 (+4.8%) | 1063.3 → 1351.3 (+27.1%) | 1437.1 → 2058.6 (+43.2%) | 0 / 0 |
| selected-pressure / many_short | 1.467 → 1.197 (-18.4%) | 0.2764 → 0.2438 (-11.8%) | 2720.4 → 2513.0 (-7.6%) | 67.9 → 117.7 (+73.4%) | 111.7 → 246.2 (+120.5%) | 0 / 0 |
| selected-pressure / many_mixed | 2.333 → 1.534 (-34.3%) | 0.5896 → 0.5125 (-13.1%) | 619.4 → 585.8 (-5.4%) | 426.3 → 462.5 (+8.5%) | 750.3 → 908.4 (+21.1%) | 0 / 0 |
| selected-latency4 / hot_short | 3.519 → 2.843 (-19.2%) | 2.7519 → 2.6384 (-4.1%) | 94.6 → 91.7 (-3.1%) | 924.2 → 1075.2 (+16.3%) | 1113.5 → 1210.0 (+8.7%) | 0 / 0 |
| selected-latency4 / hot_mixed | 4.661 → 3.388 (-27.3%) | 3.6195 → 3.2061 (-11.4%) | 65.8 → 65.0 (-1.1%) | 1531.6 → 1756.1 (+14.7%) | 2105.1 → 2088.2 (-0.8%) | 0 / 0 |
| selected-latency4 / many_short | 2.688 → 2.193 (-18.4%) | 0.6151 → 0.6821 (+10.9%) | 806.3 → 765.5 (-5.1%) | 203.6 → 231.0 (+13.5%) | 293.8 → 343.5 (+16.9%) | 0 / 0 |
| selected-latency4 / many_mixed | 2.944 → 2.181 (-25.9%) | 1.1319 → 1.1495 (+1.6%) | 431.7 → 399.5 (-7.5%) | 575.1 → 616.5 (+7.2%) | 1116.9 → 1168.5 (+4.6%) | 0 / 0 |

On the actual 24 ms default under added Redis work, lower synthetic worker CPU per completion
came with measurable capacity and latency costs. Many-org throughput fell by **7.6% for short
scopes and 5.4% for mixed scopes**, repeated in both matched pairs (−7.46%/−7.79% and
−4.85%/−6.00%). Every paired p99 worsened; many/short mean p99 increased from **112 to 246 ms**.
The candidate maximum wait was 2.677 seconds. Keeping this candidate as a Draft for the accepted
controlled trial does not imply that its capacity loss is acceptable for a general rollout.

Pressure-group mean process Jain changed from 0.9958→0.9951, 0.9775→0.9869, 0.9988→0.9934 and
0.9958→0.9940 in scenario order. Pooled event-loop lag p99 rose in all four scenarios, most in
many/short (0.440→0.689 ms). Reduced CPU therefore does not establish improved event-loop lag.
Redis CPU ranged from 14.251% to 17.575% of one core, including background work; this is not a
quota/throttling simulation. All 16 pressure cells had zero fallback, KV errors, lease loss,
residual active/waiting scopes and zero-completion lanes. Temporary lease keys were absent on exit.

With 4 ms injected around each lease command, the 24 ms default still reduced attempts per
completion by 18.4–27.3%, but CPU results were mixed: hot/short −4.1%, hot/mixed −11.4%,
**many/short +10.9% and many/mixed +1.6%**. Window throughput fell by 3.1%, 1.1%, 5.1% and
7.5%, respectively. Mean p99 changed by +8.7%, −0.8%, +16.9% and +4.6%. All 16 delay cells had
zero fallback. Fewer retries therefore did not consistently reduce worker CPU per completed
scope. This adverse result is retained and limits the case for expanding the selected trial.

The many/short delayed CPU regression repeated in both rounds (+8.52% and +13.22%). The small
hot/mixed improvement in the mean of run p99 values must not be read as a uniform tail gain:
its separately pooled p99 increased from 1,928.3 to 2,240.0 ms (+16.2%). Both quantile methods and
run ranges are retained in the aggregates; neither is silently substituted for the other.

See [selected-normalized.csv](selected-normalized.csv), [selected-aggregates.json](selected-aggregates.json), [selected-summary.csv](selected-summary.csv) and [selected-measurements.json](selected-measurements.json).

## Earlier 12 ms refreshed-main comparisons

These 64 cells precede the cap selection and remain evidence about 12 ms, not measurements of
the final 24 ms default. Each row is a separate matched comparison; absolute costs across load
groups are not comparable. `current` in these records means 12 ms at the earlier measured SHA.

| Group / scenario | Attempts/completion change | CPU/completion change | Throughput change | Mean p95 change | Mean p99 change |
|---|---:|---:|---:|---:|---:|
| balanced / hot_short | -12.7% | -11.4% | +1.0% | +0.8% | +12.2% |
| balanced / hot_mixed | -17.5% | -13.6% | +0.3% | +8.9% | -4.0% |
| balanced / many_short | -7.5% | -4.5% | -1.8% | +18.4% | +30.3% |
| balanced / many_mixed | -17.4% | -11.2% | +0.3% | -3.8% | +1.9% |
| pressure / hot_short | -12.4% | -7.8% | +0.6% | +10.4% | +15.7% |
| pressure / hot_mixed | -17.3% | -2.9% | -2.1% | +1.9% | +4.1% |
| pressure / many_short | -6.3% | -0.8% | -3.0% | +18.7% | +29.5% |
| pressure / many_mixed | -16.2% | -6.9% | +4.2% | -13.8% | -7.6% |
| latency4 / hot_short | -7.8% | -0.4% | +2.0% | -7.7% | -13.9% |
| latency4 / hot_mixed | -10.6% | -4.3% | +2.5% | -7.7% | -6.9% |
| latency4 / many_short | -4.9% | -2.3% | +1.0% | -3.2% | -6.2% |
| latency4 / many_mixed | -8.6% | -5.1% | +5.1% | -7.7% | -10.9% |

All 64 earlier cells have zero fallback, KV errors, lease loss, residual waiters/active scopes and
zero-completion lanes. All temporary lease keys were absent after drain. Ordinary many/short
p99 increased in all four pairs. Redis CPU under added pressure ranged from 12.684% to 16.937%
of one core, versus 1.724% to 9.689% without extra work; logs contained no pressure-child exception.
See [summary.csv](summary.csv), [normalized.csv](normalized.csv), [aggregates.json](aggregates.json)
and [measurements.json](measurements.json). Full wait/lag arrays remain in local evidence;
public counters reproduce normalization but cannot independently reconstruct pooled quantiles.
Historical evidence in the parent directory retains its original unbalanced-order limitation
and regressions; these later results do not erase those observations.

## Correctness validation of the actual 24 ms source

At `a3b301a7104a4f9865beedc3bfec6e850622101b`, the complete SQLite CI command passed:
**5,374 passed, 70 skipped**. The complete PostgreSQL/Redis CI command passed on a fresh local
PostgreSQL instance and disposable Redis: **2,330 passed, 1 skipped**. The first PostgreSQL run
had one failure in unchanged `test_upload_timeout_retries_once_and_recovers`, whose 80 ms total
archive-upload budget expired before a retry. The unchanged full rerun passed; the first failure
is retained in local evidence rather than omitted. This unrelated timing-sensitive result is not
an admission correctness fix. The current-main dashboard build also passed.

Coverage includes initial immediate acquisition, capped growth and per-org reset, unavailability,
absolute deadline exhaustion, cancellation during queued waits/backoff, partial acquisition,
at-deadline/uncertain SET cleanup, multi-org order, zero orgs, late renewal, and session cleanup
before admission release. Both Grok commits remain: cancellation/deadline regression tests and
recovered-renewal/loss-reason diagnostics. No accounting write or upstream retry is added.

## Controlled-trial decision

The 24 ms cap is selected to prioritize lower contention cost while explicitly accepting longer
admission waits. Relative to 12 ms in the four-policy sweep, it further reduced attempts per
completion by 11.4–31.8% and synthetic worker CPU per completion by 4.7–20.4%; throughput changed
by −1.54% to +1.18%. Mean run-level p99 increased in every scenario: 654→968 ms, 1,988→2,350 ms,
147→301 ms and 789→936 ms. Mean process fairness declined in three scenarios. This is a material
CPU-versus-wait tradeoff, not evidence that 24 ms dominates 12 ms or will reduce total Web CPU.
The actual-default pressure and delay runs also show throughput losses and, in delayed many-org
workloads, CPU-per-completion regressions; the evidence does not support broad rollout.

Treat the candidate as a reversible, limited evaluation. Keep the admission org allowlist, the set of admitted operations,
worker concurrency, wait/lease settings and #947 budgets constant. Compare matched traffic
windows using acquisition attempts and Web CPU per completed eligible scope, retaining offered
load, failed/cancelled scopes, completion throughput, request/admission tails, process progress,
KV deadlines, fallback and lease-loss signals. CPU per second falling because fewer requests
complete is not a success. Process-window counters have boundary effects; aggregate enough
windows and keep numerator/denominator populations aligned.

Define the permitted request/admission tail budget before any rollout, incorporating the accepted
synthetic waiting cost. Stop for any accounting or cleanup regression, a breach of that latency
budget, or a sustained increase in fallback or loss of completion capacity against matched
baseline traffic. End without expansion if reduced attempts do not yield repeatable Web CPU
savings per completion. The coarse admission buckets cannot reconstruct precise p95/p99; use
suitable request timing where available and report bucket exceedance fractions honestly otherwise.

There is no independent backoff feature flag in this change. Disabling admission would remove
both queueing and Redis admission, making it a different experiment. Rollback should use the
previous compatible code version while preserving #947. Mixed-version processes share the
same org leases and influence one another, so they are not independent A/B samples. Deployment,
traffic routing and live configuration changes require separate authorization; none is performed
by this benchmark or PR update.

## Reproduction

Run the committed harness with Python server/test dependencies and `redis-server` available.
For the actual selected 24 ms source:

```sh
PYTHONPATH=src python scripts/bench_money_admission_contention.py \
  --baseline-ref c321776143e0690f780281fa1bb5051a15594ed1 \
  --variants local_fixed current --balanced-order --rounds 2 --seconds 12 \
  --pressure --output /tmp/backoff-selected-pressure.json
```

Replace `--pressure` with `--lease-rtt-ms 4` for injected command delay. Each invocation starts
and cleans up its own private Redis. To reproduce the cap sweep at the earlier measured commit
`de4deeb2f9714e0c573ae6c0a86441b7559a0867`, use baseline
`92419f7b323077f7a5b60a241ffce828dc9947c3` and
`--variants local_fixed current equal:24 equal:48 --balanced-order --rounds 4 --seconds 8`.
The earlier normal 12 ms comparison uses that same source/baseline, two variants and four
15-second rounds. Do not rerun an old `current` label on newer source and treat it as the same policy.
