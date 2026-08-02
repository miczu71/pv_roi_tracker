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
