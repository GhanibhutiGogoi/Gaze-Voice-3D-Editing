# GazeVoice Objective Evaluation Quickstart

2026-11-23 · Unity VR + Quest Pro with real eye tracking · Team execution checklist

**Purpose: verify that the system selects the right target, makes the right edit, responds promptly, and correctly undoes and preserves changes.** This is a test plan. Collectors and validators still need implementation; existing CI does not establish headset acceptance.

## 1. Do these five things first

- [ ] Back up an isolated study project. Fix the code commit, scene, Bridge, PC/Quest/Link/OpenXR, model, and language; complete the run card below.
- [ ] On the headset, confirm that the eye ray follows changes in gaze. Record eyes/head/unavailable per sample; a single eyes=true is insufficient.
- [ ] Prepare the initial scene, correct target, and expected state (the ground truth). Reset each case rather than repeatedly relying on Undo.
- [ ] Compare before/after states with an independent validator, checking targets and non-targets. Do not expose answers to the model.
- [ ] Run one success, one wrong-target case, and one timeout to confirm that logs and judgments are saved.

**Run card: one per version**

| Date / tester | Code commit / scene version | Bridge / Unity | Quest / Link / OpenXR | PC / refresh rate | STT / editing model / language | Log directory |
|---|---|---|---|---|---|---|
| ____ | ____ | ____ | ____ | ____ | ____ | ____ |

## 2. Run tests in this order

| Order | Coverage / quantity | What to do | What to retain |
|---|---|---|---|
| 1. Headset smoke test | One complete workflow | Link → Play → verify eye tracking → hold B/Y to speak → inspect edit → Undo → exit Play / reopen | Pass/fail at each step, errors, screenshots |
| 2. Geometric targeting | 6 categories × 8 = **48 cases** | Inject fixed rays into the same targeting function: isolated objects, adjacent duplicates, occlusion, parent/part hierarchies, multiple material slots, surface placement/empty space | Expected/actual target and hit position; separate expected misses |
| 3. Semantics and execution | 8 categories × 3 scripts × 3 repetitions = **72 runs** | Transform, color, creation, duplication, deletion, parts, explicit-name/deictic conflicts, and Undo. Reset every run | Initial target, actual changes, latency, errors; distinguish real speech from manually transcribed branches |
| 4. Fault recovery | 10 fault types × 3 = **30 runs** | Eye loss, Link loss, controller interruption, empty transcript, model timeout, tool exception, vanished target, Play exit during execution, save failure, log-write failure | Clear failure/recovery, unintended edits, recovery time |
| 5. Persistence | **12 cases** | Two each: Transform, material/mesh, creation, deletion, hierarchy, and XR rig/non-edit runtime state. Exit Play and reopen | Intended edits persist; unintended runtime state is not written back |
| 6. Stability and performance | **100 commands**; **18 segments × 60 seconds** | Continuous simple commands for crashes/memory/log loss; 3 scene loads × idle/executing × 3 repetitions for frame times | Error counts, memory change, median/p95 frame time, dropped/reprojected frames |

Run fault injection only in an isolated project without participants. A main-thread hang needs external supervision/termination; do not assume the UI Stop button can interrupt it. Fixed-ray tests assess targeting code, not real eye-tracking accuracy.

**Optional extension:** label 120 referring-expression windows; compare eye rays, head rays, and removal of word-level timing offline. Record both rays synchronously with the corresponding scene. Separate development/pilot data from final evaluation.

## 3. Keep these core metrics

| Metric | Calculation |
|---|---|
| First-attempt complete success rate, primary | Cases reaching ground truth from the first user command, without extra input, rescue, or non-target edits / all started cases. Record internal automatic retries separately |
| Final independent success rate | Cases correct within the time limit after any user repairs, without manual rescue / all started cases. Report rescued success separately |
| Initial target accuracy | References with correct initial top-1 / all references with a ground-truth object. No candidate counts as incorrect |
| Error distribution | Wrong object / wrong operation / no edit / tracking / STT / model or tool / saving. Multiple labels are allowed; totals need not sum to 100% |
| Latency | Talk-button release → first visible edit, and → command completion. Report median/p95 and timeouts; this is not full task time |
| Eye availability and fallback | Eye-ray available time / reference-window duration; fallback count/duration. Looking at empty space without a hit is not tracking loss |
| Recovery and persistence | Independently verified passes / all specified cases; fault clearance → readiness for the next valid command |
| VR performance | Actual headset frame times, fraction above refresh-rate budget, dropped/reprojected frames. PC-window FPS is not a substitute |

**Common criteria:** match stable object/part keys; position error ≤0.02 m, rotation ≤2°, relative scale error ≤2%; color error ≤0.02 per channel after consistent color-space conversion; placement within 0.15 m of the ground-truth region center and penetration ≤0.02 m. These are proposed tolerances, to be frozen after the pilot. Ignore only prelisted HUD/runtime-statistic changes. A model's “done” response is not evidence.

## 4. Minimum collection: existing logs plus three additions

| Existing capability | Add before collection |
|---|---|
| record.json: command, transcript, model, stage, error, some timings and candidates | 1. Link run/case/command IDs; preserve initial candidate/source, task start/end, button release, and first-edit timestamps |
| vr_gaze: queryable hit samples with eyes; not persisted by default | 2. Promptly save actual source, tracking gaps, and fallback per sample; prevent query truncation and missing samples |
| Internal Undo/snapshot support | 3. Export target/non-target state differences, stable object keys, and independent Undo/save assertions |

Use one monotonic clock or explicit cross-process alignment. Do not fill sampling gaps as valid eye time. windows.good counts hits, not eye validity; command-level gaze.source majority voting does not prove uninterrupted eye use. Collection must not block the VR main thread.

**Per-case record: copy for use; logs may export the same columns to CSV**

| Run / case / repetition / command ID | Mode and ground-truth target | Initial target | First / final independent success 1/0/? | Error / repair / rescue | First edit / completion ms | Fallback ms | Snapshot or log path |
|---|---|---|---|---|---|---|---|
| ____ | ____ | ____ | ____ / ____ | ____ | ____ / ____ | ____ | ____ |

? means unverifiable. Missing values are NA, not zero. Keep evidence for manual judgments. Prespecify time limits in the run card/case list; retain timeouts without treating them as successful completion times.

## 5. Deliver one summary table

| Area | This run | Failed cases / evidence | Next action |
|---|---|---|---|
| Geometry / backend execution | __/48; __/72 | ____ | ____ |
| Faults / persistence | __/30; __/12 | ____ | ____ |
| Continuous operation | __/100 successes; __ crashes; __ missing logs | ____ | ____ |
| Latency / VR frame time | Median __; p95 __; timeouts __ | ____ | ____ |
| Real eye tracking / fallback | Available __%; __ fallbacks / __ seconds | ____ | ____ |

Report actual denominators and success-rate bounds for unknown outcomes. Repeated runs are not independent users. **Resolve silent unintended edits, incorrect saves, hard crashes, missing critical logs, or failed resets before the formal user study.** All deterministic assertions should pass; report measured model success without relaxing tolerances afterward.

Keep data in a restricted directory outside the repository. Do not upload raw voice/eye data. Any human eye-tracking collection must use the user-study consent process.

Background and sources: [full plan](GazeVoice_Formal_Evaluation_Plan.md). Participant workflow: [user study field guide](GazeVoice_User_Study_Field_Guide.md).
