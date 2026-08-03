# pv_roi_tracker — always match HA's Energy Dashboard, don't guess entities

## Context (revised — supersedes the original diagnosis below)

**Why this plan changed.** The original version of this plan claimed pv_roi_tracker's
stored `produced_kwh` was understated by up to 40% for some months, based on
comparing stale `historic.json` data against `sensor.inverter_total_yield`. That
comparison was invalid — comparing data recorded *then* against a sensor read
*now* isn't a fair test. When re-checked properly (utility_meter vs its own
source, live, same instant), production drift is small (~1–6%), not 40%. That
overstated claim is retracted.

**What's still true and confirmed:** `sensor.house_consumption_energy_monthly`
(the sensor pv_roi_tracker currently reads for `consumed_kwh`) diverges sharply
from its own source `sensor.house_consumption_energy_total` — by +12% to +82%
across sampled months, worse in months with more battery-to-grid activity.
Reading the actual template (`house_consumption_energy_total` = `inverter_total_yield
− power_meter_exported + power_meter_consumption`, a `state_class: total` sensor
that can legitimately dip when the battery discharges to the grid) explains why:
HA's `utility_meter` helper treats any dip in a non-`total_increasing` source as
a meter reset and keeps accumulating from the new lower value instead of netting
the dip out. Confirmed by comparing the utility_meter's own reported value
against its own source's true (end − start) delta for the same months — no
historic.json or CSV data involved in that comparison. This bug is real.

**The bigger correction — what changed the whole approach.** The original plan
picked `sensor.inverter_total_yield` / `sensor.power_meter_consumption` as "the"
lifetime meters by matching entity names. That was a guess. Checking
`ha_manage_energy_prefs`, the user's actual HA Energy Dashboard is configured
with **different** entities:

| Role | Energy Dashboard's actual source | What the original plan guessed |
|---|---|---|
| Solar production | `sensor.energy_pv` | `sensor.inverter_total_yield` |
| Grid export | `sensor.power_meter_exported` | (same — this one was right) |
| Grid import | `sensor.daily_energy_peak` + `sensor.daily_energy_offpeak` | `sensor.power_meter_consumption` |
| Battery charge/discharge | `sensor.battery_total_charge` / `sensor.battery_total_discharge` | (same — this one was right) |

For March 2026, `energy_pv` = 643.98 kWh vs `inverter_total_yield` = 685.53 kWh
— a real ~6.5% difference, not because one is "wrong," but because they are
genuinely different sensors (`energy_pv` integrates `input_power_with_efficiency_loss`;
`inverter_total_yield` is the Huawei integration's native lifetime register).
Reading `/config/packages/huawei-solar-yield-package.yaml` also shows a *third*
candidate, `sensor.solar_yield` (661.65 kWh for March), whose own code comment
says "For Production and used in the cards" — three plausible entities, three
different numbers, and picking by name or by comment is exactly how the
original plan went wrong.

**The user's explicit requirement going forward: pv_roi_tracker's numbers must
always match whatever the Energy Dashboard is configured to use — not whatever
entity looks right by name.** The right fix is to stop guessing and read HA's
own Energy Dashboard preferences at runtime (the same `.storage/energy` config
`ha_manage_energy_prefs`/the Settings→Energy page reads), so the add-on
automatically follows the user's own configured source, including if they ever
reconfigure it.

**Open item, not blocking:** the user reported seeing 671 kWh for March on the
Energy Dashboard's UI; direct LTS queries (`period=month` and independently
summed `period=day` — both agree at 643.98) don't reproduce that number for
`energy_pv`. Not yet reconciled — needs a Playwright screenshot (blocked this
session on: no persisted HA long-lived access token available, and the user
declined creating one on the spot). Proceed without it: once the add-on reads
the *same* entities and the *same* `statistics_during_period` mechanism the
dashboard itself uses, any residual gap is a HA UI-side rendering nuance, not
a pv_roi_tracker defect — but flag it to the user again after release in case
they want to chase it further with a screenshot.

**Important self-correction on the "balance check" design.** The original
`balance.py` design (R1/R2 residuals) assumed `self_consumed_kwh` and
`consumed_kwh` were *independent measurements* that should cross-validate
against `produced`/`exported`/`purchased`. They are not independent in this
installation — reading the actual templates:

```
house_consumption_energy_total  = inverter_total_yield − power_meter_exported + power_meter_consumption
inverter_yield_self_use_total   = inverter_total_yield − power_meter_exported
```

Both are *algebraically derived* from the same three primitives. There is no
separate whole-house meter. So `R1 = produced − exported − self_consumed` is
**tautologically zero** whenever self_consumed is itself defined as
produced−exported — it was never real evidence of correctness, just algebra.
This needs to be re-framed (see Design below) rather than sold as a physical
cross-check it can't actually be.

---

## Design

### 1. New: read HA's Energy Dashboard preferences at runtime

New function (in `live_reader.py` or a new small `energy_prefs.py`), reusing
the existing WS auth pattern already in `_ws_statistics` (`ws://supervisor/core/websocket`,
`SUPERVISOR_TOKEN`): send `{'type': 'energy/get_prefs'}`, parse `energy_sources`:

```python
def get_energy_dashboard_sources() -> dict:
    """Returns {'solar': [...], 'grid_import': [...], 'grid_export': [...],
    'battery_charge': [...], 'battery_discharge': [...]} — lists because HA
    supports multiple entries per role (this system already has 3 grid
    entries), summed for a total. Empty dict on failure (no prefs configured,
    WS error) — caller falls back to hardcoded defaults."""
```

Cache the result (module-level, refreshed once at startup + once daily
alongside the existing `cpi_job`-style daily jobs — prefs rarely change).
Fallback to today's hardcoded entity choice (`inverter_total_yield` /
`power_meter_exported` / `power_meter_consumption` / `battery_total_charge`
/ `battery_total_discharge`) if the call fails or nothing is configured, so
the add-on still works on an install without an Energy Dashboard set up.

### 2. `live_reader.py`: swap fixed constants for resolved lists

Where `_PRODUCED_LIFETIME_METER` etc. are currently single hardcoded strings
(from the prior implementation pass this session), change to: resolve once
per fetch from `get_energy_dashboard_sources()`, summing each role's LTS
`change` across all configured entities for that role. Concretely, for this
installation: `produced = LTS(energy_pv)`, `exported = LTS(power_meter_exported)`,
`imported = LTS(daily_energy_peak) + LTS(daily_energy_offpeak)`, `battery_charge
= LTS(battery_total_charge)`, `battery_discharge = LTS(battery_total_discharge)`.

`daily_energy_peak`/`daily_energy_offpeak` already carry `tariff: peak`/`offpeak`
attributes — use them directly for the peak/offpeak zone split too (dropping
`monthly_energy_peak`/`monthly_energy_offpeak` as a separate source), so the
zone split and the import total are the same numbers, not two utility_meter
instances that can drift apart from each other.

### 3. `self_consumed_kwh` / `consumed_kwh`: derived, honestly labeled

No independent whole-house meter exists here, so stop claiming
`self_consumed_kwh` is "measured" (the `inverter_yield_self_use_total`
sensor is itself `produced − exported`, same primitives). Compute both
directly from the now dashboard-matched primitives:

```
self_consumed_kwh = max(0, produced − exported)
consumed_kwh      = self_consumed_kwh + imported     # = produced − exported + imported
```

`self_consumed_source` field becomes `'derived'` always in this topology
(keep the field/schema — a future install *could* have a real whole-house
meter, worth keeping the provenance tag).

### 4. `balance.py`: reframe as a cross-family plausibility check, not a physical proof

Since R1 is tautological once self_consumed is derived from produced−exported,
replace it with something that actually carries information: compare the
Energy-Dashboard-sourced `produced_kwh` against the *other* template family's
own independent number (`sensor.inverter_total_yield`'s own LTS change, or
equivalently re-derive via `house_consumption_energy_total`/`inverter_yield_self_use_total`
directly) for the same month. A large divergence between the two families is
a genuine signal worth surfacing (as seen: ~6.5% for March 2026) — not proof
either is "wrong," but worth an operator's attention. Document this limitation
plainly in the module docstring so it's never oversold again.

### 5. Everything else from the original implementation pass stays

These parts didn't depend on which entity is canonical and remain valid:
- `historic_store.py` schema v2 bump (new fields, no migration needed)
- `main.py`: `misfire_grace_time`/`coalesce` on all cron jobs, `_last_current`
  scoped to `(year, month)`, `month_close_reconcile_job` (00:05 unconditional
  LTS reconcile), broadened `_scan_and_heal_all_months` sweep (replacing the
  old previous-month-only healers), `close_month()` outcome now recorded
- `rebase.py`: `simulate()`/`apply()` dry-run-then-write workflow, invoice-
  reconciled fields protected via `historic_store._RECONCILE_FIELDS`
- `web.py`: `/api/historic/simulate-rebase` + `/api/historic/apply-rebase`
  endpoints via the existing callback-injection pattern
- `get_ha_tariff_stats`'s local-midnight fix (was querying UTC midnight,
  losing the first 1–2 local hours of every backfilled month)
- `get_hourly_energy`'s negative-change rejection + DST-duplicate-hour
  accumulation fix

473 tests currently pass (421 baseline + 52 new). The entity-resolution
change touches `live_reader.py`'s constants and the `_fetch_lifetime_month_stats`
call sites; existing tests that patch `_get_state`/`get_ha_monthly_stats`
directly are unaffected by the swap (they don't depend on which entity name
is baked in), but new tests must mock `get_energy_dashboard_sources()`.

---

## Files (updated)

| File | Change |
|---|---|
| `pv_roi_tracker/live_reader.py` | Add `get_energy_dashboard_sources()`; resolve produced/exported/imported/battery from Energy Dashboard prefs (fallback to current hardcoded defaults); drop `monthly_energy_peak/offpeak` in favor of `daily_energy_peak/offpeak`; `self_consumed_kwh`/`consumed_kwh` always derived (drop "measured" framing) |
| `pv_roi_tracker/balance.py` | Reframe as cross-family plausibility check (see Design §4), update docstring to stop overselling it as physical validation |
| `pv_roi_tracker/rebase.py`, `historic_store.py`, `main.py`, `web.py`, `roi.py` | No further changes needed — already correct and entity-agnostic |
| `tests/test_live_reader_rebase.py` | Update to mock `get_energy_dashboard_sources()` instead of hardcoding entity names in fixtures |
| `tests/test_balance.py` | Update for the reframed check |
| `docs/BLUEPRINT.md` (already in the add-on repo) | Update with this corrected narrative — useful history of what was tried and walked back |

## Verification

1. `pytest` — must stay green (473 currently; expect similar count after
   adjusting the entity-source tests).
2. Live sanity check via `ha_get_history`/`ha_manage_energy_prefs` (already
   done this session): confirm `get_energy_dashboard_sources()` returns
   exactly the 5 roles listed in the table above for this installation.
3. Dry-run `rebase.simulate()` against real `historic.json` once deployed,
   review the diff report (per-month before/after using the *new*
   dashboard-matched entities), before calling `apply-rebase`.
4. After release + rebase apply: spot-check one month's `produced_kwh` in
   pv_roi_tracker against the Energy Dashboard's own number for that month
   (ideally via the still-pending Playwright screenshot, or the user
   reading it off their own screen) to close the 671-vs-644 loop.
5. Release per the existing checklist (config.yaml + `__init__.py` version
   bump, push, publish GH release, verify version==tag, Supervisor update).

---

## Implementation log — code complete, not yet released

- `live_reader.py`: `get_energy_dashboard_sources()` implemented — WS
  `energy/get_prefs` via the existing `_ws_connect_authed` handshake helper
  (factored out of `_ws_statistics`), parses `energy_sources` into
  `{'solar', 'grid_import', 'grid_export', 'battery_charge',
  'battery_discharge'}` role-lists, per-role fallback to
  `_FALLBACK_ENERGY_SOURCES`, cached in-process (`_energy_prefs_cache`,
  `force_refresh=True` to bypass). `_sum_role_month()` sums whatever's
  available per role from an already-fetched `_fetch_lifetime_month_stats`
  result. `read_month_from_statistics()` and `read_current_month()` both
  rewritten to resolve entities dynamically instead of reading hardcoded
  names; `read_current_month()` keeps the day-1 reset guard on the old live
  `sensor.inverter_yield_monthly` REST read as a trip-wire, then corrects
  produced/exported/imported against the dashboard-resolved lifetime meters
  via one supplementary LTS call, falling back to the live REST values if
  that call comes back empty. Zone split moved from
  `monthly_energy_peak`/`offpeak` to `daily_energy_peak`/`offpeak` (what the
  Energy Dashboard's own grid entries actually reference). `_build_record()`
  no longer takes a `consumed` parameter or a `self_consumed_measured`
  parameter — `self_consumed_kwh` and `consumed_kwh` are always computed
  (produced−exported, and self_consumed+imported respectively); gained
  `cross_family_produced_kwh` for the balance.py check.
- `models.py`: added `cross_family_produced_kwh`; updated `self_consumed_source`
  docstring (always `'derived'` on this installation, kept for provenance).
- `balance.py`: fully rewritten per Design §4 — `compute_balance()` now
  compares `produced_kwh` against `cross_family_produced_kwh`
  (`ALERT_TOLERANCE_PCT = 10.0`, above the observed 0.6–6.5% normal drift),
  not the old tautological R1/R2. Module docstring explains the correction.
- `rebase.py`: added `cross_family_produced_kwh` to `_MERGE_FIELDS` so it
  survives the rebase merge (needed for the "still_broken" check to work
  post-rebase).
- `main.py`: `_heal_month_if_needed()` and the `poll_and_publish()` health
  check updated to the new `{'diff_kwh', 'diff_pct'}` shape.
- `roi.py`: autarky/self-consumption clamp comment corrected (no longer
  claims an independent measurement exists).
- Tests: `test_live_reader_rebase.py` rewritten (dynamic-source mocking,
  `get_energy_dashboard_sources()` coverage — real-shape fixture matching
  this installation's actual 5-entry Energy Dashboard config);
  `test_balance.py` rewritten for the cross-family check; `test_main.py`
  and `test_rebase.py` updated to the new `MonthlyRecord` shape.
  **478 tests passing** (421 baseline + 57 new), 0 failures.
- Live end-to-end sanity check (read-only, real HA instance, this session):
  `get_energy_dashboard_sources()` correctly resolved all 5 roles;
  `read_month_from_statistics(2026, 3, ...)` returned produced=643.98,
  exported=308.96, purchased=339.87 (peak 50.86/offpeak 289.01),
  cross_family_produced_kwh=685.53, balance_residual_kwh=41.55 — all
  internally consistent and matching hand-verified figures from this
  session's investigation.

## Self-review — 2 real bugs found and fixed

A full review of the diff before committing turned up two confirmed bugs,
both fixed and covered by new/updated regression tests:

1. **`read_current_month()` read peak/offpeak from daily-cycle sensors via a
   raw state read, not a month-to-date figure.** `sensor.daily_energy_peak`/
   `offpeak` reset every day, so `_get_state()` on them only returns *today's*
   import, not the month's — confirmed live 2 days into August 2026
   (`monthly_energy_offpeak`=7.58 kWh vs `daily_energy_offpeak`=2.49 kWh,
   already diverged). Fixed: `read_current_month()` now fetches peak/offpeak
   via the same supplementary LTS call already used for produced/exported
   (which correctly integrates the daily-cycle entity over the whole month),
   falling back to the old `sensor.monthly_energy_peak`/`offpeak` REST read
   only if that call comes back empty. `read_month_from_statistics()` was
   never affected — LTS `change` with `period=month` already integrates
   correctly regardless of the source entity's own reset cycle.
2. **`rebase._merge_month()` left `consumed_kwh` inconsistent with the
   restored billed `self_consumed_kwh`/`purchased_kwh` for invoice-reconciled
   months.** `self_consumed_kwh` is in `historic_store._RECONCILE_FIELDS` and
   gets restored to the old billed value, but `consumed_kwh` isn't in that
   list and stayed at the freshly-rebuilt LTS value (`new self_consumed +
   new imported`) — verified directly: merging July 2026 with
   `protect_reconcile=True` gave `self_consumed_kwh=432.02`,
   `purchased_kwh=84.32` (billed) but `consumed_kwh=520.20` (LTS), and
   432.02+84.32 ≠ 520.20. Fixed: `_merge_month()` now recomputes
   `consumed_kwh = self_consumed_kwh + purchased_kwh` from the just-restored
   billed values whenever `protect_reconcile` applies.

Also fixed while reviewing: `rebase.apply()` never accepted or forwarded
`roi_kwargs`, so its report's `roi_before`/`roi_after` silently used `roi.py`'s
module defaults instead of the add-on's actual configured
`gross_investment`/`subsidy`/etc. if the user had changed those options.
`apply()` now accepts `roi_kwargs`, and `main.py`'s `_apply_rebase` callback
passes `_roi_kwargs()` (already used by `_simulate_rebase`). Also added the
daily `energy_prefs_refresh_job` (03:00) the original plan called for but
that hadn't actually been wired into the scheduler — without it,
`get_energy_dashboard_sources()`'s cache would only ever refresh on an add-on
restart, not daily as designed.

478 tests still passing after all fixes.

**Not yet done:**
- `publisher.py` new MQTT sensors — deliberately skipped this round; the
  cross-family breach already surfaces via the existing
  `sensor.pv_roi_tracker_pv_roi_tracker_health` attributes (`energy_balance`
  job), which was judged sufficient without adding new sensor entities.
- `static/app.js` UI badge for `self_consumed_source`/balance status — not
  built; low priority given the health-sensor visibility above.
- The 671-vs-644 kWh discrepancy for March on the native Energy Dashboard
  UI is still unreconciled (blocked on no persisted HA long-lived access
  token this session — chromium was installed and a GPU-hang workaround
  added to `/etc/chromium/chromium.conf`, but no login token was available
  to actually drive the dashboard with Playwright). Worth chasing with a
  screenshot before or shortly after release.
- Not committed, not released. Version still 0.34.0 in `config.yaml`/
  `__init__.py`. `rebase.apply()` has not been run against the real
  `historic.json` — only `simulate()`-equivalent dry checks via
  `read_month_from_statistics()` directly.

## 0.35.1 — invoice is always final, not just the billed fields

0.35.0 shipped and was released. `simulate()` was then run against the real
`historic.json` (38 months, 2023-06 through 2026-06, all invoice-reconciled)
via an ad-hoc script from the shell (the add-on's own
`/api/historic/simulate-rebase` endpoint kept hitting the Supervisor proxy's
30s cap on a full 38-month, multi-entity-per-month LTS sweep). First run had
a real bug — `reconciled_months` was passed as an empty set, so all 37
reconciled months' billed data got discarded and replaced with flat fallback
tariff rates, producing a misleading ROI drop (13826→8198 PLN). Retracted;
re-run with the correct reconciled-months set gave sensible near-flat
results.

Reviewing that correct run's diff surfaced the actual problem the user then
named directly: even for reconciled months, `produced_kwh`/`battery_charge_kwh`/
`battery_discharge_kwh`/`specific_yield` were still being overwritten by the
fresh LTS rebuild — only the fields in `historic_store._RECONCILE_FIELDS`
(import/export/self-consumption/pricing) were protected. User's instruction:
"for the past data that exists on the invoices and is reconciled - I want to
keep it as final data, even if it's slightly off from inverter data. invoice
should be always final."

**Redesign:** `rebase._build()` now branches before doing any fetch. For a
reconciled month it never calls the full multi-entity `fetch_month()` at
all — it copies the old record unchanged (`_freeze_month()`) and only
refreshes `cross_family_produced_kwh` via a new one-entity call,
`live_reader.fetch_cross_family_produced()` (single LTS query against
`sensor.inverter_total_yield`, not the whole Energy Dashboard role set).
That field is a read-only diagnostic — never billed, never a stored "true"
production figure — so refreshing it doesn't compromise the "invoice is
final" guarantee. `balance_residual_kwh` is recomputed from it for the same
reason. Non-reconciled months are untouched by this change: they still take
the full `_merge_month()` rebuild exactly as in 0.35.0.

Side benefit: since 37 of 38 months in the real historic.json are
reconciled, this also fixes most of the timeout problem — a full rebase run
now does 37 cheap single-entity queries plus 1 full multi-entity query
instead of 38 full multi-entity queries.

`_MERGE_FIELDS`/`_merge_month()` no longer take a `protect_reconcile` flag —
there was nothing left for `_merge_month()` to protect once reconciled
months bypass it entirely, so the old `_RECONCILE_FIELDS` restore-and-
recompute-consumed_kwh logic inside `_merge_month()` was deleted along with
it. `historic_store._RECONCILE_FIELDS` itself is unchanged and still used by
`historic_store.reconcile_invoice()` (unrelated code path).

Report shape gained a `frozen: bool` per month so simulate/apply output (and
any future UI) can show which months were frozen vs rebuilt.

Also shipped in this release: the query-bounding fix already implemented in
0.35.0's working tree but not yet released — `_fetch_lifetime_month_stats`/
`get_ha_tariff_stats` bound their LTS query to `[this month, next month)`
instead of leaving the end open (HA's default queries to "now"), which
measurably risked >30s multi-entity queries against a 2.2GB recorder
database for older months.

`tests/test_rebase.py` rewritten: the two tests asserting reconciled-month
behavior now assert `produced_kwh`/`consumed_kwh`/etc. stay at the **old**
value (not the rebuilt one), plus a new stub for `fetch_cross_family` and
assertions on the `frozen` flag. 478 tests passing.

**Still pending:** release checklist (bump done: 0.35.0→0.35.1 in both
`config.yaml` and `__init__.py`; CHANGELOG.md/README.md updated), commit,
push to GitHub via the two-repo workflow, publish GH release, trigger
Supervisor update, verify. Re-run `simulate()` against the real
`historic.json` once more after this change lands to confirm reconciled
months now show `frozen: true` with zero delta on all but the diagnostic
field. `apply()` has still never been run against the real `historic.json`
— needs explicit user approval before that step.

**Update:** `simulate()` re-run confirmed the fix — 37 months `frozen: true`
with zero delta on every field but the diagnostic, 1 unfrozen (2026-07,
correctly not yet reconciled). ROI barely moved (82.06%→82.05%). 7 months
flagged `still_broken` (cross-family divergence, diagnostic only, not
touched). User then said "apply if everything looks good" —
`/api/historic/apply-rebase` was called; the HTTP call itself timed out at
the Supervisor proxy's 30s cap (the endpoint writes, then also runs
`poll_and_publish()`, pushing total time past 30s), but the write had
already completed server-side (confirmed via add-on logs: "Rebase applied:
38 month(s) touched, 0 unavailable, snapshot at
/data/historic.pre-rebase-20260802T160326Z.json") and was independently
verified against `/api/data`: reconciled months (2026-05/06) unchanged,
the one unreconciled month (2026-07) shows the full rebuild. Did not blindly
retry `apply()` — re-running on top of an already-rebased file could have
masked a real problem, so verified the actual written data instead.

## 0.35.2 — the automatic healer had no reconciled check either

User asked directly, right after 0.35.1 was applied to the real data: "will
rcem updates or invoice corrections work - they wont be locked?" Tracing
every write path to `historic.json` confirmed RCEm updates
(`historic_store.backfill_rcem()`) and invoice corrections
(`reconcile_invoice()`, `reconcile_pending_invoices()`, `patch_month_field()`)
all write directly via `_mutate_month()`, completely independent of
`rebase.py` — not locked, never were. The freeze in fact makes them *more*
durable, since a later rebase can't undo them.

But answering that question surfaced a real, separate problem:
`_scan_and_heal_all_months()` (`main.py`) — the automatic healer that runs on
every add-on startup and on the daily month-close-verify job — had no
reconciled-month check at all. It calls `_heal_month_if_needed()`, which
flags a month for repair on two conditions: no data, or a balance breach
(`balance.py`'s cross-family check, >10% divergence). Either condition sends
the month through `_reread_month()` → `historic_store.replace_month()`,
which preserves only `tariff`/`rcem_status` and overwrites everything else.

Before 0.35.1, this was inert for reconciled months: `cross_family_produced_kwh`
was always `None` on them, so `compute_balance()` returned `incomplete` and
the breach branch was unreachable. **0.35.1's own `apply-rebase` armed it** —
it populated `cross_family_produced_kwh` on all 37 reconciled months, and 7
of them (2023-06, 2025-01, 2025-10, 2025-11, 2025-12, 2026-01, 2026-02) now
exceed the 10% threshold (visible in the apply report's `still_broken`).
Left unfixed, the very next add-on restart would have rebuilt those 7
months from LTS — `reconcile_pending_invoices()` runs right after and
restores the billed subset, but `produced_kwh`/`battery_charge_kwh`/
`battery_discharge_kwh`/`specific_yield` would stay clobbered, on a repeating
cycle every restart. That's the opposite of "invoice is always final."
Nothing was actually damaged — the add-on hadn't restarted since the
0.35.1 rebase — but it was armed and waiting for the next one.

**Fix, user-approved choice** (asked via AskUserQuestion: skip breaches
outright vs. still heal genuinely-empty reconciled rows): the latter. A
reconciled month with a balance breach is now skipped entirely (logged,
left untouched); a reconciled month with literally no data is still healed,
since there's nothing final to destroy and `reconcile_pending_invoices()`
reapplies the billed figures immediately afterward in the same startup
sequence — that ordering was already deliberate
(`_catch_up_missing_month_close()`'s own docstring says as much).

Implementation: `_heal_month_if_needed()` now returns `(code, detail)`
instead of a bare Polish sentence, so callers get a stable
`'no_data'`/`'balance_breach'` to branch on. New pure function
`_heal_action(reason, reconciled) -> 'ok'|'heal'|'skip_reconciled'` — split
out specifically so the regression is unit-testable without booting the
scheduler, matching this file's existing convention of keeping decision
logic as plain, isolated functions. `_scan_and_heal_all_months()` now
returns `(healed, skipped)` instead of just `healed`; its two callers
(`_catch_up_missing_month_close()`, `month_close_verify_job()`) log skipped
months too. No changes needed to `rebase.py`, `balance.py`, or
`historic_store.py` — the existing `energy_balance` job already surfaces
balance breaches on the health sensor via `balance.check_all()`, so the
healer change only needed to stop *acting* on a breach for reconciled
months, not add new visibility.

Before deploying, captured the 7 affected months' `produced_kwh`/
`purchased_kwh`/`exported_kwh`/`consumed_kwh` from the live add-on's
`/api/data` as a real before/after baseline (not just trusting logs) —
the decisive check is confirming these are byte-identical after the
add-on restarts as part of the 0.35.2 update.

## 0.35.3 — the health job never learned the reconciled-month rule either

2026-08-02, the 0.35.2 deploy restart flipped `sensor.pv_roi_tracker_health`
to `degraded`; the phone alert fired ~2h later per the automation's `for:`
guard. User asked for a full verification. Everything else on the add-on was
green (all other jobs `ok`, zero exceptions in the logs, poll succeeding
every 30 min, release/version consistent) — the only failing job was
`energy_balance`, flagging the exact same 7 reconciled months 0.35.2 just
finished protecting: 2023-06, 2025-01, 2025-10, 2025-11, 2025-12, 2026-01,
2026-02.

0.35.2's fix note above says it plainly and undersold the consequence: *"the
existing `energy_balance` job already surfaces balance breaches on the
health sensor via `balance.check_all()`, so the healer change only needed to
stop acting on a breach for reconciled months, not add new visibility."*
True, but `check_all()` itself never got a reconciled-month exception — only
`_heal_action()` did. Since these 7 months are now permanently exempt from
repair (by design, "invoice is always final"), and `check_all()` scans every
record with no exception, the health sensor had no path back to `ok`. Not a
data problem — a structural omission: the same "reconciled is final" rule
was taught to the healer but not to the job that watches for what the healer
should act on.

**Second, independent defect surfaced by the same 7 months**: all of them
are low-production winter/shoulder months (174–368 kWh) with 30–95 kWh
absolute drift. `ALERT_TOLERANCE_PCT = 10.0` is purely relative, so the same
absolute drift that trips 10% in a 220 kWh month sits under 2% in a 850 kWh
summer month. Fixing only the reconciled-skip would have left the health
sensor exposed to re-tripping on the next live (unreconciled) December or
January.

**Root cause of the underlying drift, investigated and documented, not
fixed** (user's explicit choice — report only): `sensor.energy_pv`
(`sensors.yaml`, `platform: integration, method: left`) integrates
`sensor.input_power_with_efficiency_loss` (`template.yaml`), which scales
the inverter's raw DC input power by ×0.90 (<600 W), ×0.95 (<1200 W), or
×0.98 (else) before integration. `sensor.inverter_total_yield` is the
Huawei integration's native lifetime register — no such scaling. At low
irradiance, most of a month's production sits in the ×0.90/×0.95 bands, so
`energy_pv` structurally under-reads, worst in winter. Confirmed via HA's
own long-term statistics (`recorder/statistics_during_period`,
`sum`-of-period, 2023-06..2026-07):

| month | energy_pv | inverter_total_yield | diff | diff% |
|---|---:|---:|---:|---:|
| 2025-01 | 426.8 | 460.8 | -34.0 | 7.97% |
| 2025-10 | 301.6 | 355.5 | -53.9 | 17.88% |
| 2025-11 | 217.8 | 282.4 | -64.6 | 29.67% |
| 2025-12 | 257.7 | 334.1 | -76.5 | 29.68% |
| 2026-01 | 241.5 | 337.2 | -95.7 | 39.62% |

(Values differ slightly from the health sensor's own `cross_family_produced_kwh`,
which is a live per-record fetch computed at a specific poll moment, not
a pure LTS window — same order of magnitude, same months, same sign.)
Lifetime aggregate: `total_produced_kwh` 21746.6 kWh (from `/api/data`
summary) vs `sensor.inverter_total_yield`'s own LTS-summed total ~22193 kWh
over the same window ≈ **-2.0%**, concentrated in exactly these
low-production months. One recorder-side anomaly excluded from this table:
2023-09's `sum` statistic shows an obviously bogus -2276.9 kWh delta
(a meter-reset artifact in the recorder's own bookkeeping, not present in
`state`-based deltas) — not evidence of anything wrong with either
production family, just a reminder that `sum` isn't immune to recorder
resets either. Billed kWh (export/import, sourced from the Tauron meter,
not from either of these two chains) are entirely unaffected by this drift.
No changes made to `template.yaml`, `sensors.yaml`, or the Energy
Dashboard's configured solar source — purely a report, per user's explicit
choice via AskUserQuestion.

**Fix:**
- `balance.py`: new `ALERT_MIN_ABS_KWH = 100.0`; `compute_balance()` now
  breaches only when `diff_pct > ALERT_TOLERANCE_PCT` **and**
  `abs(diff_kwh) > ALERT_MIN_ABS_KWH`. Placed in `compute_balance()`, not
  `check_all()`, so the same floor also protects the automatic healer via
  `_heal_month_if_needed()` — a live, not-yet-reconciled December would
  otherwise still trip a rebuild for the same reason.
- `check_all(records, reconciled: set[(year, month)] | None = None)` skips
  any month in `reconciled` — mirrors `_heal_action()`'s `skip_reconciled`
  rule exactly. `main.py`'s `poll_and_publish()` now calls
  `balance.check_all(all_records, reconciled=_reconciled_months())`, the
  same closure the healer already uses. `reconciled=None` (default)
  preserves pre-0.35.3 behavior for any other caller.
- `/api/data`: each month now carries `cross_family_produced_kwh`,
  `balance_residual_kwh`, and `balance_reconciled` (previously computed but
  never serialized). Historia tab gained a collapsible, informational-only
  diagnostics table showing the per-month drift — visibility without
  alerting, so a real operator-worth-a-look divergence is still
  discoverable, just not paged.

**Lesson, extending 0.35.2's own**: this add-on now has *three* independent
paths that read the reconciled-month rule — `rebase.py` (never rebuilds),
the healer's `_heal_action()` (never repairs), and now the health job's
`check_all()` (never alerts). Any future change to what counts as "final"
must be checked against all three, not just the two identified in 0.35.2.

Verified: 487 tests passing (482 baseline + 5 new in `test_balance.py`
covering the abs-floor and the `reconciled` parameter, including a
regression case reproducing the real 2025-01 numbers). Deploy verification
(baseline capture, restart, health-sensor state, logs, UI diagnostics
screenshot) recorded in the plan file
`/data/home/.claude/plans/verify-the-whole-pv-roi-tracker-nested-river.md`.

482 tests passing (478 + 4 new for `_heal_action`).
