# Current Outage Detection by TFF

# Outage Detection V2.1

**Complete Handoff Pack — PRD · Data Model & Guards · Config Parameters · Worked Examples**

**16 July 2026 · all four parts synced: K = 30 min, grace = 15 days, age cap = 15 days, dual grid 10/15 min, no night logic in detection**

> **Current implementation override:** The final **Additional Requirement** is the latest decision. It removes plan-based eligibility and the 15-day plan grace period. Where earlier sections mention a plan feed, population feed, recharge state, or `OD_EXPIRED_GRACE_DAYS`, the final requirement supersedes them.

> **Scope:** That decision governs Detection V2.1. The downstream spatial-triage/map export has a separate comparison-cohort gate: an active `CUSTOMER_V_2` device is retained only when `PLAN_EXPIRY_TIME > last_successful_ping_ist + 12 hours`. It does not change Detection's telemetry-only rule.

## Contents

1. [Part 1 — PRD: Deterministic Outage Detection](#part-1--prd-deterministic-outage-detection)
2. [Part 2 — Data Model, Guards, Risks & Assumptions](#part-2--data-model-guards-risks--assumptions)
3. [Part 3 — Configuration Parameters](#part-3--configuration-parameters)
4. [Part 4 — Worked Examples](#part-4--worked-examples)
5. [Additional Requirement](#additional-requirement)

---

# PART 1 — PRD: Deterministic Outage Detection

## Outage Detection PRD - Deterministic Outage Detection

**Author:** Yash Rana. **Updated:** 16 July 2026. **Companion docs:** Data Model & Guards, Config Parameters, Worked Examples. Every config key in this document starts with `OD_` and its default lives in the Config Parameters doc.

## 1. Executive Summary

**What we are building:** a system that watches router pings and turns real outages into outage records. One physical outage should become exactly one record. Detection should be fast. Every rule should be a simple threshold that anyone can check by hand.

**What this fixes:** today, one real outage gets recorded about 30 times, and about 282k records per month point to no real event at all. Big outages are detected late.

**What detection does NOT do:** it does not decide who gets alerted, or when. It only records facts. Other systems (communication, compensation, analytics) read those facts and make their own choices.

## 2. Problem Statement

### What is happening today, measured on June 2026 data

- The current system created 614,616 incidents in June. That is about 20,000 per day (614,616 / 30).
- We matched every incident against the raw ping data, row by row. One real outage becomes 28 to 37 separate incidents (the middle value varies by outage size tier; worst case: 225).
- 47% of the smallest incidents match no real event at all. ("Tiny" is the old system's 1-to-9 device class; it had 595,398 of the 614,616 incidents.)
- The median detection delay is 21 minutes.

### Why this happens

- The old system creates a fresh incident every 15 minutes for newly failing devices, and never lets a device join an existing incident. So one long outage keeps producing new incidents.
- There is no check that a failing device was recently alive. Routers that have been dead for days still get counted.
- Every outage needs 3 failed pings, even when 200 devices are clearly down together.

**Why it matters:** nobody can act on 20,000 incidents a day. Real outages hide inside the noise.

## 3. Objectives & Scope

### Objectives

1. **Deterministic:** the same input always produces the same outages. No interpretation, no scores, no guessing.
2. **Evidence matches impact:** when many devices fail together, 2 missed pings are enough proof. When few fail, we demand 3.
3. **One event, one outage:** an outage's member list is frozen when it is created. A device can be in only one open outage.
4. **Facts only:** detection records what happened. It never filters or hides anything for a specific audience. (Alarms to our own on-call engineers ARE allowed; those are for us, not customers.)
5. **Fail loud:** if a config value or input feed is missing, the service stops with a clear error. It never silently guesses.

### In scope (V2.1)

- Outage detection from router pings (one ping expected per router every 5 minutes)
- Device state tracking and eligibility
- Outage creation on two schedules (10-minute and 15-minute, explained in FR-4)
- Recovery tracking and closing of outages
- Safety guards (defined in the Data Model doc, section 5)
- The output records that other systems read (FR-8)

### Out of scope (V2.1)

- Customer and partner messaging. The existing communication PRD ("Outage V2.1 PRD" by Akhil Mahajan) sits on top of this system and reads its output.
- Finding the cause (power cut vs ISP problem vs local fault). That is a separate future system.
- Any scoring, probability, or machine learning.
- Night-time special handling. A 3 am outage is recorded exactly like a 3 pm outage. Whether to wake anyone up is the consumer's decision, not detection's.

## 4. Core Definitions (read first; everything else uses these)

1. **CSP:** the local service partner who owns and services a set of customer routers. Every router belongs to exactly one CSP. Detection looks at each CSP separately and never mixes devices across CSPs.
2. **Ping:** a heartbeat message from a router. One is expected roughly every 5 minutes (`OD_PING_INTERVAL_MIN = 5`).
3. **Silence is downtime:** a missing ping counts the same as a failed ping. Nobody needs to send a "no data" message for a dead router. The system notices silence on its own, using time (see FR-2).
4. **No extra waiting:** the Nth missed ping IS the confirmation. Example: the 2nd missed ping confirms FAIL-2 at that moment. Nothing waits after it.
5. **Tick:** a moment when the system evaluates. One scheduler wakes up every 5 minutes, at fixed clock times (IST, Indian Standard Time). At some ticks it runs the big check (:00, :10, :20, :30, :40, :50). At some it runs the small check (:00, :15, :30, :45). Both checks are defined in FR-4. "Tick" is the only word this document uses for these moments.
6. **Tiers, by frozen size:** Large = more than 100 devices. Medium = 26 to 100. Small = 10 to 25. Micro = 3 to 9.
7. **Eligibility window (K = 30 minutes):** a device counts toward opening an outage only if it had a successful ping in the last 30 minutes (`OD_ELIGIBLE_LOOKBACK_MIN`). A device dead for longer is a chronic problem, not evidence of a new outage.
8. **Frozen membership:** once an outage is created, its device list never changes. No additions, no removals.
9. **One outage per device:** a device can be inside at most one open outage at a time.
10. **Edges always count:** "within 30 minutes" includes exactly 30 (code: `<=`, not `<`). "At least 10" includes exactly 10 (code: `>=`). Percentages are compared exactly, with no rounding: 25% of a base of 15 is 3.75, so a count of 4 clears that bar and a count of 3 does not. This rule exists because an off-by-one at an edge silently drops devices and is very hard to catch in testing.
11. **Timestamps:** every timestamp is stamped by our receiving server. Router clocks are not trusted. Storage is UTC; all clock rules in this document are IST.

## 5. Functional Requirements

### FR-1: Ping Data Processing

Every router sends a heartbeat ping about every 5 minutes. Each ping arrives as one message:

| Field | Type | Meaning |
|---|---|---|
| `device_id` | string | the router's serial number |
| `csp_id` | string | the CSP this router belongs to |
| `ping_timestamp` | timestamp | when our server received the ping |
| `ping_status` | SUCCESS / FAIL / NO_DATA | whether the router responded |

**Rules:**

- **FR-1.1:** NO_DATA is treated exactly like FAIL.
- **FR-1.2:** if a message arrives whose timestamp is older than, or equal to, the last processed message for that device, throw it away. Message queues sometimes deliver duplicates or deliver late. Without this rule, an old success arriving late could erase a real failure streak, and a duplicated success could make a recovery look one ping further along than it is.

### FR-2: Device State Tracking

At any tick, the system must be able to answer five questions about any device:

- **FR-2.1 Is it FAIL-2 confirmed?** Meaning: has it been at least 10 minutes since its last successful ping? That equals 2 missed pings. Example: last success 1:00, misses at 1:05 and 1:10. At 1:10 it is FAIL-2. (From the first miss it is 5 minutes; from the last success it is 10. Same moment. We count from the last success because that is the timestamp we store.)
- **FR-2.2 Is it FAIL-3 confirmed?** Same idea, 15 minutes = 3 missed pings. In the example above: third miss at 1:15, so FAIL-3 at 1:15.
- **FR-2.3 Is it eligible?** Three things must all be true: its last success was within 30 minutes (K), it is in-population (FR-3), and it is not already inside an open outage.
- **FR-2.4 Is it recovered?** See the recovery rules below.
- **FR-2.5 Which open outage is it in, if any?**

These answers must cost almost nothing to compute (one subtraction), and there must be no background job that loops over devices to keep counters fresh.

#### Recovery rules

- A recovery run starts at the first SUCCESS after a failure.
- The run breaks and resets if a FAIL or NO_DATA arrives, OR if 10 or more minutes (`>=`, edges count) pass with no new SUCCESS. Silence is downtime, so three successes with a silent gap between them are NOT in a row. Note this matches FAIL-2: at exactly 10 minutes of silence the device is FAIL-2 confirmed and its recovery run is broken; there is no moment where a device is both failing and recovering.
- A device is recovered when its current run has at least 3 successes (`OD_RECOVERY_SUCCESS_COUNT`) AND at least 15 minutes have passed since the run's first success (`OD_RECOVERY_SUSTAIN_MIN`). Both must be true. No 4th ping is needed at the 15-minute mark; time passing is enough, as long as the run is not broken.

#### A simple way to build this (a suggestion; tech can choose differently)

Store ONE thing per device: the time of its last successful ping (`last_up_at`).

That one timestamp answers the first three questions. How long has the device been down? Now minus `last_up_at`. Is that 10 or more minutes? Then 2 pings are missed (FAIL-2). 15 or more? Then 3 are missed (FAIL-3). Was it up in the last 30 minutes? Same subtraction.

A silent device needs no special handling. Time passes on its own, so the device becomes "long down" by itself, with no code running.

For recovery, store two more small fields: when the current run of successes started, and how many successes it has so far.

For the duplicate rule (FR-1.3), also store the time of the last processed message of ANY status. `last_up_at` alone cannot spot a duplicated FAIL.

Do NOT store a counter of missed pings. A dead router sends nothing, so nothing would ever increase that counter. To keep counts honest you would need a background job that touches every down device every 5 minutes. The timestamp needs no such job.

### FR-3: Population & Eligibility Base

Not every router should count. A router whose customer stopped paying months ago is not outage evidence. This FR defines who is in.

The input is a feed: `device_id`, `csp_id`, `plan_expiry_date`. This feed does not exist today. Its owner and freshness must be locked before build.

- **FR-3.1** a device is in-population if its plan is active (expiry date is today or later, IST calendar date), or expired at most 15 days ago (`OD_EXPIRED_GRACE_DAYS`).
- **FR-3.2** a CSP's eligible base = how many in-population devices it has. Careful: this is NOT the count of "eligible" devices from FR-2.3; the base ignores the 30-minute window and open memberships. The 25% rule in FR-4 uses this same population on both sides of the division. Never count failures from one population and divide by another.
- **FR-3.3** until the feed exists, every device counts as in-population (so detection works on day one), BUT the base is treated as unknown, so FR-3.4 applies and the percentage path stays off until the feed arrives.
- **FR-3.4** if a CSP's base is unknown or zero, the percentage path (Micro) is switched off for that CSP. The absolute thresholds still work. Never guess a base.

### FR-4: Outage Creation

Everything here is counted per CSP. Each check counts one CSP's devices and can create an outage only for that CSP.

**Order of work at every tick:** filter first, then count, then compare. Each device is tested alone against the full checklist (enough missed pings for this check, up within the last 30 minutes, in-population, not already in an open outage). Only the survivors are counted, and that count is what meets or misses the threshold. The raw number of "down devices" is never compared to anything. This guarantees: every outage's frozen size genuinely passed the bar it opened on. Example: 15 devices down with 3-ping proof, but 5 are long-dead (past K) and 1 is already in an open outage; survivors = 9, and 9 < 10, so nothing opens, even though "15 are down".

There are two checks on two schedules:

- **FR-4.1 Big check** - runs at :00, :10, :20, :30, :40, :50. It counts devices that are FAIL-2 confirmed AND eligible. If the count is at least 26 (`OD_FAST_POOL_MIN_DEVICES`), it creates one outage containing all of them. Why 26? That is the Medium tier's lower edge, so this fast 2-ping path can only ever produce a Medium or a Large. It can never produce something small.
- **FR-4.2 Small check** - runs at :00, :15, :30, :45. It counts devices that are FAIL-3 confirmed AND eligible. It creates one outage if the count is at least 10 (`OD_OPEN_MIN_DEVICES`), OR at least 25% of the CSP's eligible base (`OD_OPEN_BASE_PCT`) with a floor of 3 devices (`OD_OPEN_PCT_FLOOR_DEVICES`).
- **FR-4.3 Why two schedules?** Each schedule matches its proof. The 2-ping proof completes in about 10 minutes, so the big check runs every 10. The 3-ping proof completes in about 15, so the small check runs every 15. This way a group finishes its proof and gets recorded on the same tick, instead of finishing at minute 15 and waiting for a tick at minute 20.

#### Hard rules

- **FR-4.4** at shared ticks (:00 and :30), the big check runs first, always. If it fires, it takes every qualifying device (3 misses also counts as 2 misses), and the small check finds nothing left. This stops a piece of a big outage being recorded as a separate Small.
- **FR-4.5** one CSP gets at most one new outage per tick.
- **FR-4.6** on creation, the member list freezes and the record is stamped with the fields in FR-8. A member's own failure time = its last success + 5 minutes (its first missed ping). The outage's `first_fail_at` = the earliest of those.

For step-by-step examples of all of this (a wire cut with 30 devices, with 15 devices, mixed evidence at one tick, and more), see the Worked Examples doc.

### FR-5: Recovery & Closure

- **FR-5.1** a member is recovered per the recovery rules in FR-2.
- **FR-5.2** an outage closes when at least 90% of its frozen members are recovered (`OD_CLOSE_RECOVERED_PCT`). This is checked every 5 minutes, at every tick.
- **FR-5.3** an outage closes automatically when its age reaches 21,600 minutes or more (`OD_MAX_AGE_MIN`, = 15 days, `>=` per the edges rule), with `close_reason = AGE_CAP`. Fifteen days is enough to see a chronic problem; after that the record describes chronically dead routers, not a live event.
- **FR-5.4** closing an outage releases its devices. They can join future outages. A closed outage never reopens. A device that fails again later is a fresh event and must earn its eligibility again.
- **FR-5.5** once a member's `recovered_at` is set, it is never cleared. If that device fails again while the outage is still open, the 90% count does not go back down; the new failure is a fresh event (it can join a new outage only after this one closes).

### FR-7: Concurrency & Integrity

These rules make race conditions impossible, instead of merely unlikely:

1. **FR-7.1 One CSP, one evaluation at a time.** The same CSP is never evaluated by two workers at once. Different CSPs can run in parallel; they share nothing.
2. **FR-7.2 One (CSP, tick) evaluation is one transaction.** Read state, evaluate, freeze, commit - all or nothing, per CSP. At shared ticks, the small check sees what the big check just did, inside that same transaction.
3. **FR-7.3 The database enforces uniqueness.** A unique index makes it impossible for one device to be in two open outages.
4. **FR-7.4 Every tick leaves a receipt.** A ledger table records every evaluated (`csp_id`, tick time) pair, even when nothing was created, with a unique key. If the process crashes and the tick is retried, the retry finds the receipt and does nothing. No duplicates, ever.
5. **FR-7.5 Same input, same output.** Replaying the same ping stream must produce identical outage tables. This is a required automated test.

### FR-8: Output Contract (what consumers read)

Detection publishes two record types. Communication, compensation and analytics read these and apply their own filters. Detection never pre-filters for anyone.

#### Outage - one row per outage

| Field | Meaning |
|---|---|
| `outage_id` | unique id |
| `csp_id` | the CSP |
| `opened_at` | the tick that created it |
| `first_fail_at` | earliest member failure time (FR-4.6) |
| `size` | member count, frozen at creation |
| `tier` | LARGE / MEDIUM / SMALL / MICRO, from frozen size |
| `status` | OPEN / CLOSED |
| `closed_at`, `close_reason` | set on close; reason is RECOVERED or AGE_CAP |

#### OutageMember - one row per frozen member

| Field | Meaning |
|---|---|
| `outage_id`, `device_id` | keys |
| `member_first_fail_at` | this device's failure time (FR-4.6) |
| `days_past_expiry` | plan state at freeze (FR-3.5); 0 = active, null = feed unavailable |
| `recovered_at` | set when the member satisfies the recovery rules |

## 7. Edge Cases

| Case | Handling |
|---|---|
| Device already in an open outage fails more | Not counted again (eligibility requires no open membership) |
| Ping arrives as NO_DATA | Treated as FAIL (FR-1.1) |
| Device recovers, outage closes, device fails next day | Fresh event; may enter a new outage |
| CSP base unknown or zero | Percentage path off for that CSP; absolute thresholds still work (FR-3.4) |
| Tick finds zero qualifying devices | Nothing created; the ledger receipt is still written (FR-7.4); not an error |
| Crash in the middle of a tick, tick retried | The ledger receipt makes the retry do nothing (FR-7.4) |
| 11 FAIL-3 devices + 90 FAIL-2 devices at a shared tick | One Large(101). Big check first, takes all; small check finds none left |
| Devices left out of a frozen outage reach FAIL-3 later | They form a new outage only if they clear a creation bar on their own at a later tick (the small-check bars, or the big check's 26 if the left-out group is that large) |
| Outage never recovers | Auto-close at 15 days, reason AGE_CAP |
| Router moves to another CSP mid-outage | The outage keeps its original `csp_id` (frozen at creation) |

## 8. Acceptance Criteria

**Notation:** Tier(count) means an outage of that tier with that many members. Example: Large(150).

### Detection & state (AC-1)

| ID | Scenario | Expected |
|---|---|---|
| 1.1 | Last success 12:00; misses at 12:05, 12:10, 12:15 | FAIL-2 at 12:10, FAIL-3 at 12:15 |
| 1.2 | Same device, one successful ping at 12:20 | All failure state fully reset |
| 1.3 | Last success 12:00, then total silence (no messages) | Same FAIL-2/FAIL-3 clock as 1.1 |
| 1.4 | Device last alive 90 minutes ago (K = 30) | Not eligible; counted by no check |
| 1.5 | Device never seen alive | Never eligible |
| 1.6 | An already-processed ping is delivered again | No state change |
| 1.7 | Successes at 12:00 and 12:05, silence, then a success at 12:40 | The silent gap broke the run; 12:40 starts a new run |

### Creation (AC-2)

| ID | Scenario | Expected |
|---|---|---|
| 2.1 | 150 devices, all last alive 02:04 | One Large(150) at the 02:20 tick; `first_fail_at` 02:09 |
| 2.2 | 8 devices last alive 11:58 (too few alone) + 30 more last alive 12:03; base 500 | Nothing at 12:15 (8 < 10 and 8 < 125); one Medium(38) at 12:20 |
| 2.3 | 11 FAIL-3 + 90 FAIL-2 at 12:30 (shared tick) | Exactly one Large(101) |
| 2.4 | Medium(40) froze at 12:20; 11 more reach FAIL-3 between 12:31 and 12:45 | Small(11) at 12:45; two outages total |
| 2.5 | CSP base 16; 4 devices at FAIL-3 at a small tick | Micro(4): 4 >= 3 and 4 >= 25% of 16 (= 4 exactly; edges count) |
| 2.6 | Same as 2.5 but base unknown | Nothing created |
| 2.7 | 9 FAIL-3 devices, base 200 | Nothing (9 < 10, and 9 < 50) |
| 2.8 | Two CSPs, each with 20 FAIL-2 devices, same tick | Nothing (counting is per CSP; 20 < 26 for each) |

### Recovery & closure (AC-3)

| ID | Scenario | Expected |
|---|---|---|
| 3.1 | Member: success 12:00, fail 12:08 | Not recovered (run broken early) |
| 3.2 | Member: successes 12:00, 12:05, 12:10; checked at 12:15 | Recovered (3 successes, 15 minutes, run unbroken; no 4th ping needed) |
| 3.3 | Outage of 20; 18 recovered | CLOSED (18 >= 0.9 x 20); all 20 devices released |
| 3.4 | Outage of 15; 13 recovered (0.9 x 15 = 13.5) | Still OPEN; closes at 14 (compared exactly, no rounding) |
| 3.5 | Outage opened at T, never recovers | CLOSED at T + 21,600 minutes, reason AGE_CAP |
| 3.6 | Released device fails the next day | Joins a new outage; the old one is untouched |

## 9. Dependencies

| Dependency | Impact | Status |
|---|---|---|
| Ping feed (FR-1; owner: infra/telemetry) | Core input | Exists; same feed the current system uses |
| Population feed: `device_id`, `csp_id`, `plan_expiry_date` (FR-3) | Needed for the 25% path and `days_past_expiry` | Does not exist. Owner and freshness must be locked. Until then: everyone is in-population, the 25% path is off, `days_past_expiry` is null |
| Config service with `OD_*` keys | All tunables | Locked 16 July: K = 30, grace = 15 days, age cap = 21,600 min. Still pending: `OD_MASS_PAUSE_PCT`, used only by the mass-failure pause guard in the Data Model doc section 5 (if more than N% of all in-population devices are failing at one tick, stop creating outages; a fault that big is our telemetry, not the internet). No rule in this document reads it. Build proceeds on 30% |

---

# PART 2 — Data Model, Guards, Risks & Assumptions

## Outage Detection - Data Model & (Guards + Risk + Assumptions)

*Detection only. Just the data: what we need, what we work out, the tables, and how they link. In sync with the PRD dated 16 July 2026 (K = 30, grace = 15 days, age cap = 15 days, no night logic).*

## 1. Data we NEED (raw inputs)

| Input | Comes as | Have it? |
|---|---|---|
| Router ping — device id, csp id, status (SUCCESS / FAIL / NO_DATA), server timestamp | event, every ~5 min | Yes |
| Plan state per device — device id, csp id, `plan_expiry_date` | feed from the connection system | No — need to get. Until it exists: every device is treated as in-population, the 25% path is off, `days_past_expiry` is null |

## 2. Data we WORK OUT (derived — never a separate source)

All of this comes from the pings over time plus the plan feed. We do not fetch it; we calculate it.

- Minutes down = now - `last_up_at`. FAIL-2 confirmed at 10+ minutes, FAIL-3 at 15+ minutes. No stored miss counter (silent devices send nothing to increment one).
- Eligible = in-population AND was up in the last 30 minutes (K) AND not already in an open outage.
- In-population = plan active, or expired at most 15 days ago. `days_past_expiry` = days since expiry, 0 if active.
- A CSP's eligible base = count of its in-population devices (same population for the 25% rule's top and bottom).
- Outage size, tier, duration, MTTR — all from timestamps.

## 3. Tables (four)

### A. router_state — one row per router (live working memory, updated per ping)

| Field | Meaning |
|---|---|
| `device_id` | key |
| `csp_id` | which CSP |
| `plan_expiry_date` | from the plan feed (null until the feed exists) |
| `last_up_at` | last successful ping (this one timestamp drives FAIL-2, FAIL-3 and eligibility) |
| `up_run_started_at` | start of the current success run (recovery tracking) |
| `up_run_count` | consecutive successes in the current run |
| `open_outage_id` | which open outage this device is frozen into (null if none); a unique index on this enforces one open outage per device |

### B. outage — one row per outage

| Field | Meaning |
|---|---|
| `outage_id` | key |
| `csp_id` | which CSP |
| `opened_at` | the tick that created it |
| `first_fail_at` | earliest member failure time (member's last success + 5 min) |
| `size` | member count, frozen at open |
| `tier` | LARGE / MEDIUM / SMALL / MICRO, stamped from frozen size |
| `status` | OPEN / CLOSED |
| `closed_at` | when it closed (blank while open) |
| `close_reason` | RECOVERED or AGE_CAP |

### C. outage_router — which routers are in each outage (frozen at open)

| Field | Meaning |
|---|---|
| `outage_id` | which outage |
| `device_id` | which router |
| `member_first_fail_at` | this device's failure time (its last success + 5 min) |
| `days_past_expiry` | plan state at freeze; 0 = active, null = feed unavailable |
| `recovered_at` | set when the device completes its recovery run (3 successes + 15 min sustained) |

### D. tick_ledger — one row per evaluated tick per CSP

| Field | Meaning |
|---|---|
| `csp_id + tick_ts` | unique key; written for EVERY evaluated tick, even when nothing opens |
| `result` | what happened (nothing / `outage_id` created) |

The ledger is what makes a crashed tick safe to retry: the rerun finds the row and does nothing.

## 4. How they connect

`router_state ──(device_id)── outage_router ──(outage_id)── outage`; `tick_ledger` stands alone per (`csp_id`, `tick_ts`).

- One outage has many `outage_router` rows (its frozen list of routers).
- Each `outage_router` points to one `device_id` in `router_state`.
- `csp_id` is on `router_state`, `outage` and `tick_ledger`, so everything groups by CSP.

## 5. Guards (canonical home; the PRD points here)

- **No duplicate outage** — the `tick_ledger` unique key per (`csp_id`, `tick_ts`) means a re-run cannot create anything twice.
- **One open outage per router at a time** — enforced by a unique index, not by convention.
- **Time-based state, not counters** — a missing ping IS a fail because time keeps moving; no sweeper jobs.
- **No data = a fail** (normalize at ingest; a fully silent device needs no synthetic messages).
- **Stale ticks** — a tick more than 5 minutes past its scheduled time is skipped and logged, never evaluated late.
- **Age cap** — an outage still open after 15 days (21,600 min) closes automatically with `close_reason AGE_CAP`, so never-recovering outages cannot pile up as zombies.

## 6. Risks

- **Plan feed stale or wrong** -> the 25% rule and `days_past_expiry` both degrade. Guard: keep last snapshot + alarm; the absolute thresholds keep working.
- **Partial pipeline lag** — one region goes quiet; the safety pause only catches near-total failures. Regional silence looks like a real outage (which, for the customer, it is).
- **State loss mid-outage** — losing `router_state` loses in-flight streak knowledge; cold start covers re-learning, but outages that would have opened during the 60-minute wait are lost.
- **Flapping hardware** — handled by design: any fail or a 10-minute silent gap breaks a recovery run, so recovery needs 3 clean successes sustained 15 minutes.
- **K = 30 tightness** — a small-outage device is countable for exactly one 15-minute tick; a late ping batch or a skipped tick can permanently lose that outage. Accepted trade (locked 16 July).

## 7. Assumptions

- Pings arrive about every 5 minutes per router, stamped by the server clock.
- A plan-state feed (device, csp, `plan_expiry_date`) will exist and be reasonably fresh; until then all devices count as in-population.
- Each router maps to one CSP; remapping mid-outage does not move the outage.
- A missed ping = real customer downtime, not just our monitoring path failing (the mass pause guards the extreme case).
- Pings returning reliably means service is actually restored.]

---

# PART 3 — Configuration Parameters

## Outage Detection v2 — Configuration Parameters (Dev Handoff)

*All values runtime-config. Defaults are simulation-validated on June 2026 data. PENDING params ship with the default; tuning is a config edit, not a deploy. Missing param = fail loud, never a silent fallback.*

## A. Ping confirmation

| Parameter | Type & unit | Default | Meaning |
|---|---|---:|---|
| `OD_PING_INTERVAL_MIN` | int, minutes | 5 | Router ping cadence (infra-fixed; reference only) |
| `OD_GRID_TICK_BIG_MIN` | int, minutes | 10 | Big check (2-ping, Medium/Large) evaluates every N minutes, wall-clock aligned |
| `OD_GRID_TICK_SMALL_MIN` | int, minutes | 15 | Small check (3-ping, Small/Micro) evaluates every N minutes, wall-clock aligned |
| `OD_CONFIRM_PINGS_DEFAULT` | int, count of pings | 3 | Consecutive missed pings that confirm one device down. No extra timer — the Nth miss IS the confirmation |
| `OD_CONFIRM_PINGS_FAST` | int, count of pings | 2 | Confirmation misses when the candidate pool is large |
| `OD_FAST_POOL_MIN_DEVICES` | int, devices | 26 | Pool size at/above which fast confirmation applies |

## B. Outage opening

| Parameter | Type & unit | Default | Meaning |
|---|---|---:|---|
| `OD_OPEN_MIN_DEVICES` | int, devices | 10 | Open when >= N confirmed-down devices of one CSP |
| `OD_OPEN_BASE_PCT` | int, % of CSP active base (0-100) | 25 | OR open when >= N% of the CSP's active base is down |
| `OD_OPEN_PCT_FLOOR_DEVICES` | int, devices | 3 | Minimum absolute devices for the %-path to fire |

## C. Eligibility

| Parameter | Type & unit | Default | Meaning |
|---|---|---:|---|
| `OD_ELIGIBLE_LOOKBACK_MIN` | int, minutes | 30 | K: a down device counts toward opening only if it was up within the last K minutes |
| `OD_EXPIRED_GRACE_DAYS` | int, days | 15 | Plan-state gate: null = no plan check; 0 = active plan only; N = expired up to N days ago still counts. Denominator for the %-rule follows the SAME population automatically |

## D. Size tiers (stamped at freeze; lower bounds, devices)

| Parameter | Type & unit | Default | Meaning |
|---|---|---:|---|
| `OD_TIER_SMALL_MIN_DEVICES` | int, devices | 10 | Small = size 10 up to MEDIUM_MIN-1. Below this on the %-path = Micro |
| `OD_TIER_MEDIUM_MIN_DEVICES` | int, devices | 26 | Medium = size 26 up to LARGE_MIN-1 |
| `OD_TIER_LARGE_MIN_DEVICES` | int, devices | 101 | Large = size 101 and above (i.e. >100) |

## E. Recovery & closure

| Parameter | Type & unit | Default | Meaning |
|---|---|---:|---|
| `OD_RECOVERY_SUCCESS_COUNT` | int, count of pings | 3 | Consecutive successful pings to start device recovery |
| `OD_RECOVERY_SUSTAIN_MIN` | int, minutes | 15 | Must stay up this long after first success; guards flap re-entry (21% of June spells flapped back within 15 min) |
| `OD_CLOSE_RECOVERED_PCT` | int, % of frozen members (0-100) | 90 | Close the outage when this share of members recovered |
| `OD_MAX_AGE_MIN` | int, minutes; null | null | Auto-close age cap; 21600 = 15 days; null = outages stay open until recovered |

---

# PART 4 — Worked Examples

## Outage Detection V2.1 - Worked Examples

*Companion to the PRD (16 July 2026). Nothing here adds rules; every example only applies the PRD's rules step by step, at the locked defaults: K = 30 min, big check every 10 min, small check every 15 min. Use this doc to build intuition and to settle "what happens if" debates without re-deriving.*

## 1. The two checks, in one table

| | Big check | Small check |
|---|---|---|
| Runs at | :00, :10, :20, :30, :40, :50 | :00, :15, :30, :45 |
| Counts devices with | at least 2 missed pings (10+ min down) | at least 3 missed pings (15+ min down) |
| Fires when count is | at least 26 | at least 10, OR at least 25% of the CSP's base (floor 3) |
| Can create | Medium, Large | usually Small, Micro; rarely Medium+ (see section 2) |
| Also required of every counted device | up within the last 30 min (K), in-population, not already in an open outage | same |

At shared ticks (:00, :30) the big check runs first; if it fires, it takes everything (3 misses implies 2), and the small check sees nothing left. At most one outage per CSP per tick. Evaluation happens ONLY at these exact clock times; nothing is event-driven between ticks.

## 2. Which tick can create what (June replay frequencies)

| Tick | Can create | How often in June |
|---|---|---|
| :10, :20, :40, :50 | Medium / Large only (the 26+ bar means the big check can never make something Small) | most Medium/Large |
| :15, :45 | Small / Micro; occasionally Medium+ when a 26+ group only completed its 3-ping proof between big ticks | 20 of 5,319 Medium/Large = 0.4% |
| :00, :30 | anything; big check first, small check on the remainder | rest |
| Small/Micro at a pure 10-min tick | impossible by construction | 0 in the whole month |

## 3. Example: wire cut, 30 devices (the big-check path)

Wire cut at 12:04. Each device pings on its own 5-minute rhythm, so the last successful pings are spread between 11:59 and 12:04.

| Time | State | Action |
|---|---|---|
| 12:04 | Cut happens. Nothing observable. | Nothing. |
| 12:05 to 12:09 | Each device misses its first ping. | Nothing. One miss proves nothing. |
| 12:10 (big tick) | Only devices last alive at or before 12:00 are 10+ min down: roughly 6. | 6 < 26. Nothing. |
| 12:14 | All 30 now 10+ min down. | No tick at 12:14. The system only looks at tick times. |
| 12:15 (small tick) | 15+ min down: roughly 6. | 6 < 10. Nothing. |
| 12:20 (big tick) | All 30 are 10+ min down, all up within the last 30 min. | One outage: Medium(30), frozen, `opened_at` 12:20, `first_fail_at` ~12:09. |

Detection ~16 minutes after the physical cut, on 2-ping evidence. Note the count rule: "at least 2 misses" is a floor, so devices already at 3 or 4 misses are inside the 30.

## 4. Example: same wire cut, 15 devices (the small-check path, and what K = 30 does)

| Time | State | Action |
|---|---|---|
| 12:15 (small tick) | 15+ min down: only ~3 devices (those last alive by 12:00). | 3 < 10. Nothing. |
| 12:19 | All 15 are now 15+ min down. | No tick at 12:19. |
| 12:30 (small tick) | All 15 have 3-ping proof. BUT eligibility needs last success within 30 min, i.e. at or after 12:00 exactly. A device last alive 11:59 is 31 min stale and drops out. | Small(~14) at 12:30 (the 11:59 stragglers are excluded by K). |

Two lessons: a Small pays up to one extra grid wait when its proof completes just after a tick (12:19 vs the 12:15 tick), and K = 30 starts shaving the earliest fallers of slow-building events. This device-level shaving is exactly why the June replay books 37.7% fewer Smalls at K=30 than at K=60; the trade was accepted deliberately (locked 16 July).

## 5. Example: mixed evidence at one big tick

At a 10-min tick one CSP has: 27 devices with 2 misses, 9 with 3 misses, 5 with 1 miss (all fresh, in-population, in no outage).

Big pool = devices with AT LEAST 2 misses = 27 + 9 = 36. 36 >= 26 -> one outage, Medium(36).

The 5 one-miss devices are not confirmed by anything; they stay out. Frozen membership means they can never join later. Alone they clear no bar (5 < 10), so unless more devices fail near them they never become an outage at all.

## 6. Example: the same state, but between ticks (12:14)

Same device state as example 5, existing at 12:14. Nothing happens at 12:14; there is no evaluation between ticks, even if 500 devices die at once. At 12:20 the picture has improved by itself: the 27 two-miss devices are at 3 misses, the 9 at 4, and the 5 one-miss devices crossed to 2 misses. The big check counts 27+9+5 = 41 -> Medium(41) at 12:20, whole. The six-minute wait scooped the stragglers in; this is the batching argument for the 10-minute grid in miniature.

## 7. Example: the same state at a small tick (12:15)

Same state at 12:15. The small check counts only 3-miss devices: 9. The 27 two-miss devices are invisible to this check no matter how many there are. 9 < 10, and the % path needs 9 >= 25% of base (impossible here: the CSP has at least 41 in-population devices, so the bar is over 10). Nothing at 12:15; the whole 41 books at 12:20 as in example 6.

Near-miss worth knowing: had the 3-miss group been 10 instead of 9, the small check would legally open Small(10) at 12:15 and the remaining 31 would book separately at 12:20 as Medium(31). Two records for one physical event. That fragment case is real but bounded: it happens only when the early group independently clears a bar (PRD acceptance test 2.4).

## 8. Example: shared tick absorption (why big runs first)

At 12:30 (a shared tick) one CSP has 11 devices at 3 misses and 90 at 2 misses. The big check runs first: pool = 101 (the 11 are also 2+ missed) -> one Large(101). The small check then finds nothing left. Without the fixed order, the small check could have grabbed 11 as a Small and misled everyone about the event's scale. This ordering is a hard requirement, not a code accident.

## 9. Reading detection delay honestly (the two clocks)

**Per-device clock** (each device's own failure to an outage record covering it): June replay median 20 min all members, 15 min for Medium/Large members; the old system measured the same way is 19 to 23 min. v2 is faster where it matters.

**Whole-event clock** (the very FIRST faller of a consolidated event to creation): median 30 to 35 min. The old system never shows this number because each of its ~30 fragments restarts its own stopwatch; v2's number looks bigger only because it is honest about gradual events, where the first faller precedes the crowd by design.

Never compare the old 21-minute figure with the whole-event clock; that comparison misled us once already.

## 10. Alternatives tested and rejected (so nobody re-litigates blind)

| Alternative | June replay result | Why rejected |
|---|---|---|
| Big check every 5 min instead of 10 | Medium/Large 5 min faster, but 19 Larges break up and Medium count +17% (5,319 -> 6,138 objects for the same events) | fragmentation is the disease this redesign cures |
| Create instantly on threshold, freeze | 1.8x outages; one 647-device event became 58 incidents | worst case of the same disease |
| One uniform 10-min grid for everything | Small proof (15 min) completes at minute 15 and waits for a mismatched tick at minute 20 | cadence must match the evidence window |
| Offset grids to avoid shared ticks | impossible: a 10-min and a 15-min grid must meet every 30 minutes wherever you place them | overlap is useful anyway (section 8) |
| Tick phase tuning from data | June minute-of-hour histogram of 5.64M downtime starts is flat (4.6 to 5.4% per 5-min slot) once the hour-boundary data artifacts (:00 = 25.5%, :55 = 24.7%, from silence-stitching in the hourly source table) are excluded | uniform onsets make every phase equally good; :00-anchored is simplest |

---

# Additional Requirement

## Decision: Remove plan-based eligibility. Use ping liveness only.

## Background

The design evolved through three approaches for defining the eligible customer base:

1. Active connections only
2. Active + 15-day grace period
3. Devices with at least one successful ping in the last 24 hours

Since outage eligibility (numerator) already requires a successful ping within the last 30 minutes (K=30), an additional plan or active-state filter is unnecessary. The decision is to remove the plan dependency entirely and derive both eligibility and the 25% base directly from ping history.

## Implementation

### 1. Outage eligibility (numerator)

A failed device is eligible to join an outage only if:

- It is currently in the required failed state (per tier), and
- Its last successful ping was within the last 30 minutes (K=30).

No change from current PRD.

### 2. 25% CSP base (denominator)

For Micro-tier evaluation, define the CSP base as:

**Number of devices whose last successful ping was within the last 24 hours.**

Configuration:

`OD_BASE_LIVENESS_HOURS = 24`

The Micro rule becomes:

`Eligible Down Devices >= max(3, 25% × Live Base)`

where

`Live Base = devices with last_up_at within 24 hours`

### 3. Remove plan-state dependency

Completely remove:

- Active / expired plan checks
- 15-day grace period
- Population / plan feed dependency
- Recharge-state dependency from outage detection

Outage detection becomes fully telemetry-driven.

## Why this works

- A successful ping is our only reliable signal that a customer is active.
- Devices silent for more than 24 hours naturally fall out of the CSP base.
- The numerator is always a subset of the denominator:

`Ping within 30 min ⊂ Ping within 24 hours`

This guarantees the outage percentage is always mathematically consistent.

## Data Model Changes

### Remove

- Population table / population feed
- `plan_expiry_date` from `router_state`
- `days_past_expiry` from `outage_member`
- PopulationService
- Population repository/entity
- `OD_EXPIRED_GRACE_DAYS`
- Feed freshness configuration

### Add

Nothing.

Only introduce:

`OD_BASE_LIVENESS_HOURS = 24`

The base is computed directly from existing data:

- `router_state.last_up_at`
- `csp:devices:<csp_id>`

No new tables, fields, or services are required.

## Implementation Notes

- Compute the 24-hour base only for the Micro path.
- If the outage already satisfies the absolute thresholds (≥10 devices), skip the base calculation entirely.
- This avoids scanning the CSP device list for Medium and Large outages.
- The existing `last_up_at` Redis TTL (~16 days) already exceeds the 24-hour liveness window, so no storage changes are required.

## Acceptance Criteria

For every evaluation tick:

**Live Base**

= Devices with last successful ping ≤ 24 hours

**Eligible Devices**

= Failed devices with last successful ping ≤ 30 minutes

**Micro outages open when:**

`Eligible Devices >= max(3, 25% × Live Base)`

The outage detection path has no dependency on plan state, recharge status, or any external population feed. It is derived entirely from device telemetry.
