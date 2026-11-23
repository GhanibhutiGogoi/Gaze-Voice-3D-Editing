# GazeVoice Formal Evaluation and User Study Plan

Version v1.0 · 2026-11-23
Platform: **Unity VR + Meta Quest Pro + Windows PC / Meta Quest Link, primarily using real eye tracking.**
Scope: protocol, objective metrics and collection design, recruitment, questionnaires, and interviews. **No newly collected participant results are reported; proposed collection code is not assumed to exist.**

## 1. Executive summary

Evaluate both product reliability (understanding speech, identifying targets, editing correctly, and preserving results) and whether people can, trust, and want to use the product. Use three complementary parts rather than replacing evaluation with one satisfaction score.

| Part | Activity | Data and scale | Supported conclusions |
|---|---|---|---|
| A. Objective engineering evaluation | Regression, geometric hits, speech/command pipeline, latency, recovery, VR performance, persistence after Play | Scripts, standard scenes, independently labeled ground truth; scope in Section 5 | Functional coverage, failures, version-specific performance; not proof of good experience |
| B. Controlled user tasks | Head pointing + voice versus real eye gaze + voice on the same Quest Pro | **12 formal participants: six with and six without Unity experience; two pilots, 14 people total**; 12 core tasks per formal participant | Within-person performance, workload, confidence; experience differences exploratory |
| C. Product trial/interview | Free editing, recovery/understanding observations, real-use feedback | Same participants; eight-minute exploration and 15-20-minute interview | Needs, barriers, improvement evidence; theme counts are not population prevalence |

**Primary comparison: H = head pointing + voice; E = eye gaze + voice.** Use the same headset, push-to-talk buttons, model, feedback, and tasks. Head pointing exists as a fallback, but an effective control requires explicit source control in a study mode. Controller-ray selection is a future baseline; controller movement/button support does not establish controller-based voice grounding.

**Primary endpoint: first-attempt complete task success.** Secondary endpoints: task time, wrong-object/wrong-edit events, repair turns, tracking availability, workload, and usability. Assess correctness before efficiency; fewer actions do not automatically mean better interaction.

Recruit **two pilots + 12 formal participants = 14 people, maximum 15 unique participants overall**. Pilots are excluded from formal effect analysis; freeze protocol/version before formal collection. Incompatibility, withdrawals, interview-only participation, and any third pilot count toward the same cap. Do not add unlimited replacements. Numbers and engineering gates are proposals, not observed results or a power-validated sample size.

## 2. Repository snapshot and study boundaries

### 2.1 Verification snapshot

Remote origin/main was inspected without merging it into the original working directory. Local status below describes the source-plan audit on November 23, 2026, not every future checkout of this document.

| Item | Verified state on 2026-11-23 |
|---|---|
| Remote main | d581b9881fed6ba6ad42b5ef982372baab4fb448, abbreviated d581b98; Unity VR part-level pointing, controller, and HUD changes [R1] |
| Original local checkout | feature/quest-vr-foundation, HEAD 40bf709, 13 commits behind main; modified .gitignore and untracked vr/ preserved |
| Unity gaze | [PR #4](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/pull/4) merged; earlier “awaiting review” status obsolete |
| Cross-platform | [PR #6](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/pull/6) merged macOS/Windows support; [PR #7](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/pull/7) merged Windows calibration-startup fix |
| Unity Bridge | Remote package.json **0.3.5**; original local package **0.3.3**. Check the actually installed package before collection |
| CI | [Cross-platform tests](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/actions/runs/44869698185) for d581b98 succeeded; regression without model downloads is not headset acceptance |
| VR implementation | Windows Link/OpenXR; B/Y hold-to-talk; automatic head fallback; object/child/material-slot context; surface placement, HUD, preservation after Play [R2-R4] |
| Evidence limits | Code/repository review only; no new full-suite run, Quest connection, or participant collection. Old desktop webcam “about 23% screen error” is inapplicable to Quest Pro |

Resolve the integration of local Quest foundation and remote VR package before freezing a study build. Do not pool people using different implementations as one product version.

### 2.2 Implementation constraints

1. GazeVoiceVR.Look() uses head-forward whenever EyeRay is unavailable. Intended sampling is about 45 Hz; measure achieved rate rather than treating a constant as evidence. [R3]
2. vr_gaze returns per-sample t, uid, point, normal, eyes, path, root_uid, material, submesh, etc. vr_status.eyes only indicates a recent eye ray; one response does not prove eye input throughout a command. [R3]
3. VoiceService._ground_cast() sets command gaze.source=eyes/head by majority vote, hiding mixed windows. Save per-sample source and gaps; do not classify pure-eye trials from this field alone. [R4]
4. VR does not invoke desktop numbered clarification. Do not promise headset 1/2/3 choices. Use actual retries, explicit descriptions, and Undo for recovery. [R4]
5. Default record.json omits underscore-prefixed _t0/_t1/_cast/_look, and raw sequences are later released. Do not expect historical logs to reconstruct all timing after collection. [R4]
6. Unity permits broad edits through execute_csharp. Use an isolated study project; prompt constraints are not a security sandbox. Independently verify Undo, assets, and preservation after Play. [R2][R5]

### 2.3 Excluded from the primary study

- Desktop Blender/Unity, webcam calibration, mouse hover, and Windows/macOS speed differences: compatibility/future studies, not pooled with Quest Pro.
- ML-Agents training, code generation, package installation, complex model generation: excessive time/result variability; evaluate separately if needed.
- Controller selection + voice, voice-only, alternative LLMs: future extensions, not many tiny groups from 12 people. **Head pointing + voice is not voice-only.**

## 3. Questions and inference limits

| ID | Question | Evidence |
|---|---|---|
| RQ1 | Which scenes support correct object/part targeting, editing, and persistence? | Geometry/function benchmarks, state assertions, persistence |
| RQ2 | How do eye + voice and head + voice differ in first success, time, repair? | Within-participant H/E tasks |
| RQ3 | Can different Unity-experience groups understand targets, state, recovery? | Performance, questionnaires, event-based interviews |
| RQ4 | What explains workload, trust, privacy concerns, and reuse intention? | Ratings, usability, exploration, interviews |
| RQ5 | Which stage limits reliability: tracking, recognition, execution, network, or preservation? | Stage timing, source/fault events, actual-state checks |

E may reduce head turning/targeting effort, but eye loss, small targets, occlusion, and scope misunderstandings may increase errors. **Do not assume E is better.** Report tradeoffs, including reduced success alongside reduced effort.

Unity experience is observed, not randomized. Describe differences within this sample, not causal effects of experience. Measure VR, gaming, 3D tools, and Unity separately; a Unity novice need not be a VR novice.

## 4. Objective collection design

### 4.1 Existing capabilities and gaps

Existing = generated by inspected code; derived = requires parsing/validation; new = requires instrumentation/validators. Existing fields may be missing; report availability.

| Data | Capability | Caution |
|---|---|---|
| Command ID, transcript/refinement, prompt, provider/model, stage/error | Existing record.json | stage=done/model claims are not ground truth |
| STT/refine/edit/total | Existing timings.* | Total starts at command creation, excludes target search; stage sums may differ |
| Tool names, Undo flag | Existing tools/undo_step | Not complete arguments/affected objects; flag does not prove restoration |
| Candidate UID/score, part/material context, placement point | Existing gaze results | Vote shares are not calibrated correctness probabilities |
| Per-sample eyes/hit/position | Queryable vr_gaze, not normally persisted | Save promptly by cursor; 3,000 sample reply limit, about 120-second history; detect duplicates/gaps/truncation |
| Initial/final top-1, source switches | New | Capture before repair; final logs may reflect repaired inference |
| Session/trial/condition, stable keys, expected state | New | Independent ground truth, inaccessible to LLM; Selection is not truth |
| Reveal, PTT, tools, stable result timestamps | New | Monotonic duration and explicit clock alignment; unrelated process clocks cannot be subtracted |
| Actual object/property changes, creation/deletion, persistence | New exported snapshots/assertions | Internal SceneSnapshot is not a full research audit interface |
| Eye/head ray directions, target angular size/error, head motion | New; appropriate consent | Hit points cannot recover full eye-ray error; no eye-camera images |
| CPU/GPU, VR frame time, dropped frames, network/cost | New counters/logging | Provider usage may be absent; duration/tool counts are not tokens |

In VR, gaze.windows.good/readings counts hit samples/window samples, **not tracking validity**. A valid ray can point at empty space. matched_selection is not accuracy; Selection can be empty/stale. [R4]

### 4.2 Minimum instrumentation before collection

Add an off-by-default study mode; these are requirements, not completed implementation.

1. **Source control:** head always uses head rays; eye prioritizes eyes and records actual source. Preserve product fallback if needed, logging fallback_start/end. Eyes must work initially. Do not create H by unplugging hardware/changing runtime.
2. **Matched feedback:** same dot, HUD, volume, buttons, backend, scene; vary target source only and tag every sample.
3. **Orchestration:** neutral task prompts, reveal/start/end, command linkage, initial snapshots, clear Selection/queues/model context.
4. **Independent assertions:** stable keys, requested properties, non-target protection; export differences after commands/trials.
5. **Initial inference:** preserve first candidates/sources before results; log retries, Undo, manual rescue/intervention, not just final success.
6. **Integrity:** null/NA for missing, unique event_id, sequence numbers, clock domain, source config, checksums, session-end trial/command linkage.

Suggested events:

~~~text
session_start / device_check / calibration_start / calibration_end
trial_reveal / tracking_sample / tracking_gap / source_switch
ptt_down / ptt_up / stt_end / grounding_pre / refine_end
tool_start / tool_end / command_end / assertion_result
repair_input / manual_rescue / undo_requested / undo_verified
trial_end / play_exit / persistence_verified / session_end
~~~

Avoid synchronous bulk main-thread disk writes, whole-scene scans every frame, and ground-truth names in prompts. Compare instrumentation on/off overhead; buffer writes and report loss rather than damage VR performance.

### 4.3 Files and minimum fields

Data belong outside the repository, e.g. study_data/<protocol_version>/<session_uuid>/. Separate credentials, materials, participant records.

| File | Fields |
|---|---|
| session.json | protocol/code/scene/prompt hashes; PC/OS/GPU/Unity/Bridge/Quest firmware/OpenXR/Link/refresh; provider/model/language/group/VR/source/consent |
| trials.jsonl | session/trial/task/set/period/order; assigned_condition; gold_key; start/end/status; first/final assertion; command_ids; repair/rescue; missing_reason |
| events.jsonl | event_id, seq, trial/command_id, timestamp, clock_domain, event_type, actual_source, error_code, elapsed_ms |
| tracking.jsonl | t, seq, eye_available, actual_source, hit/miss, target_key, point/normal; consented eye/head origins/directions |
| snapshots/ | Target/non-target Transform, active, hierarchy, material slots/colors, creation/deletion; separate asset hashes |
| questionnaires.* | participant/condition/item/value/time, missing reason; avoid identity in answers |
| interview_notes.md | Events, anonymous excerpts, recording/transcription consent, codes/interpretations/counterexamples |

Use scene GUIDs or GlobalObjectId plus hierarchy mapping. Instance IDs change across Play/reloads; map to stable keys and rebuild after persistence. Register new objects and retain deletion tombstones so deletion cannot erase error evidence.

## 5. Objective engineering benchmarks

### 5.1 Layers and scope

| Layer | Minimum | Execution | Output |
|---|---|---|---|
| A0 Regression/compatibility | Python/JS tests, Unity compile, Quest installation/Link/controllers/PTT | CI/test project; fixed Windows for main study, others separately | Counts/versions/reproductions; static assertions are not headset evidence |
| A1 Geometry/context | **48 rays =6 categories ×8** | Fixed rays bypass eye noise, same Cast/context code | Object/part top-1, surface error, occlusion errors, exceptions |
| A2 Semantics/execution | **24 scripts ×3 =72 backend runs** | Known targets/rays, fixed prompt/model, reset; real-speech subset and gold-transcript branch | Correctness, steps, time, stochastic variation, failures |
| A3 Tracking/timing | Formal reference windows; optional **120 labeled windows** | Consented eye/head rays, word timing, answers; offline ablation | top-1/top-3, availability, mixed sources, timing sensitivity |
| A4 Stability/recovery | **10 faults ×3 =30; 100 continuous commands** | Participant-free injection, watchdog, stop rules | Recovery, timeouts, residual errors, memory/log loss |
| A5 Performance/persistence | 3 loads ×2 states ×3 runs of 60 seconds; 12 persistence cases | Unity/OpenXR idle/executing; Play exit/project reopen | Frame time, drops, persistence/asset assertions |

A2 categories, three scripts each: Transform; color/independent material; creation/placement; duplication; deletion; parts/material slots; explicit-name/deictic conflict; Undo/repair. Keep answers outside model input; separate recognition and model errors.

### 5.2 Scenes and reproducible criteria

A1: isolated; adjacent/repeated; front/back occlusion; parent/child parts; one mesh/multiple material slots; floor/table placement and empty/unhittable directions. Eight cases/category include positives and expected misses. Do not expect selection through complete occlusion. Verify with analytic geometry/independent labeling, not just the picker under test.

- **Object/part:** stable-key exact match, leaf/root separately; a sofa hit is not its requested cushion.
- **Transform:** predefined coordinate frames/parents; position error ≤0.02 m, rotation error ≤2°, relative scale error ≤2%, no non-target changes. No scaled parents in core tasks.
- **Materials:** correct slot, ≤0.02 per channel after consistent linear/sRGB conversion; no shared-material spillover.
- **Creation/placement:** right type/count, base contacts expected surface, region radius 0.15 m, penetration ≤0.02 m; retain continuous distances.
- **No target/unsupported:** no unintended edits, understandable status; distinguish no execution from wrong execution.
- **Undo:** restore pre-edit snapshot except prelisted HUD/runtime statistics; report asset limitations. undo_step=true is not proof.
- **Persistence:** Transform/materials/mesh/creation/deletion/hierarchy survive Play exit/reopen; XR rig/player motion/spontaneous runtime changes do not.

These study-defined tolerances require piloting and freezing before formal collection, never post-result relaxation.

### 5.3 Tracking/grounding ablation, recommended extension

On labeled windows compare eyes, head direction from the same interval, and eyes without word-level timing (whole-utterance window). Simultaneously record both poses; different utterances across conditions do not isolate an algorithm change.

Synchronous dual streams and counterfactual raycasts are **new work**; current collection returns the ray actually used. Replay requires matching geometry/object poses; use static scenes or saved snapshots rather than a changed scene.

Prefer 120 windows from separate development/pilot data. If formal windows also form a benchmark, disclose overlap. Split tuning/evaluation by person; repeated utterances from one person must not cross the split. Score multiple references per word but cluster by participant.

For STT, compare real versus manually corrected transcripts with all other inputs fixed. WER=(substitutions+deletions+insertions)/reference words; freeze case/punctuation/numeric normalization. WER is not intent error; annotate target/action/parameter recognition separately.

### 5.4 Fault injection

Ten faults: eye loss; Link interruption; controller/button interruption; empty STT; model timeout; tool exception; target deleted pre-execution; exit Play during execution; persistence failure; log-disk failure. Use isolated participant-free projects, never unexpected hardware disconnection during human tasks.

Specify expected outcomes: recoverable → continue/retry after clearance; unrecoverable → explicit termination without unintended edits, retaining audit. Recovery is clearance→ready for next valid command; detection delay separate. UI Stop may not interrupt arbitrary main-thread C#; external watchdog required, hangs count as serious faults.

### 5.5 Metric dictionary

| Metric | Definition / denominator | Caveat |
|---|---|---|
| First-attempt complete success, primary | First user command reaches truth, no extra input/rescue/non-target edits / all revealed formal trials | New assertion; internal automatic repair still within command, retries logged; missing per 12.4 |
| Final success | Truth reached within budget / all revealed trials | Repairs allowed; rescue-success flag separate |
| Initial target accuracy | Initial top-1 equals truth / all labeled object references | No candidate=wrong; distinguish repaired inference |
| top-3 recall | Truth in first three initial candidates / all labeled references | Not automatic-edit accuracy |
| Wrong-object rate | Trials ever modifying non-target / revealed trials | May overlap wrong edit; snapshots |
| Wrong-edit rate | Incorrect target property/magnitude/scope / revealed trials | Also conditional rate after correct targeting, denominator explicit |
| Placement error | Base-contact 3D distance, surface/orientation/penetration | Continuous +pass; no output is failure, not zero |
| Task time | Reveal→independently stable completion | Includes search/speech/wait/repair; timeout right-censored |
| Response latency | PTT release→first visible edit; →completion | New timestamps; median/p90/p95 and timeout rate, not successful averages only |
| Repairs/rescue | Extra user turns after first /trial; manual edits/selections separately | Undo +new edit are two turns; internal tools not user turns |
| Eye availability | EyeRay-available duration /reference-window duration | Time-weighted, gaps not filled; not windows.good |
| Valid-eye hit fraction | Eye-available duration hitting scene /eye-available duration | Empty gaze distinct from tracking loss |
| Fallback rate | E trials with any head fallback /all revealed E trials | Duration fraction, mixed windows, complete unavailability too |
| Collection completeness | Complete critical sequence /revealed trials | Missing logs do not automatically exclude failures |
| VR performance | Median/p95 frame time, over-budget, dropped/reprojected | Fixed refresh; 90 Hz implies 11.1 ms budget, not acceptance; PC FPS differs |
| Persistence/Undo | Independently correct cases /all specified cases | Actual versus believed restoration separate |
| Optional cost | Actual tokens/fees/calls per attempt and success | NA without provider usage; both denominators |

Do not invent universal launch thresholds. Core deterministic/non-target/persistence assertions should all pass. Halt formal recruitment for unresolved serious mis-saves, silent wrong-object edits, crashes, or critical missing logs. Freeze pilot-informed tracking/usability criteria rather than retaining only people above a promised accuracy.

## 6. Recruitment and study design

### 6.1 Sample and groups

**Two pilots + 12 formal participants (6 + 6), 14 total. Maximum 15 unique people** includes incompatibility, withdrawals, interview-only, and pilots. Count once/person; pilots never become formal effect cases. Aim for 10-12 analyzable formal participants; with two pilots at most 13 formal, with three at most 12. If short, report actual N/weaker claims, not extra recruitment past cap. Keep group difference ≤1 if feasible. Stop at preset deadline/cap, not significance.

| Group | Prespecified definition | Target |
|---|---|---|
| Experienced | ≥20 total Unity hours AND a scene/project or ≥5 hours in past six months | Six; about 5-7 if adjusted |
| No experience | Never/≤2 total hours, no independently completed project | Six; about 5-7 if adjusted |

Participants with >2 but <20 total hours, or ≥20 hours without the recency criterion, are intermediate; do not relabel them as novices. Qualitative-only participants still count toward 15. Preserve continuous hours/recency and actual counts.

Eligibility: ≥18, voluntary consent, comfortable headset with usual correction, understand task language. Tracking/fit incompatibility is not personal failure; record flow/reasons, offer unscored demo/interview where appropriate. Non-developers for formal sample; developers pilot-only.

Balance VR familiarity within Unity groups where possible. Record devices/use, 3D tools, gaming/controllers, language fluency, vision correction. Avoid underpowered additional demographic factors.

### 6.2 Mixed design and order

- Within-person: H/E, everyone both.
- Observed between-person: Unity experience, not randomized.
- Difficulty: matched/exploratory task strata, not an expanded confirmatory three-factor experiment.

Matched X/Y sets, six tasks each. Within each group allocate one to each sequence, then two more to balance first method; balance full sample too.

| Sequence | First | Second |
|---|---|---|
| S1 | H-X | E-Y |
| S2 | E-X | H-Y |
| S3 | H-Y | E-X |
| S4 | E-Y | H-X |

Each block: three task types ×two instances, one isolated and one adjacent/repeated per type. Save pregenerated random seed/order. Do not give E easier tasks based on performance; experienced users receive the same materials/instructions.

### 6.3 Conditions

| Item | H | E |
|---|---|---|
| Source | Forced head-forward | Eye-first, fallback fully logged |
| Speech | Same B/Y hold-to-talk | Same |
| HUD/dot/edit feedback | Same | Same |
| Selection | Cleared, no hidden desktop answer | Same |
| Backend | Same STT/model/prompt/tools/language | Same |
| Starting pose/scene | Same comfortable seated/standing scheme | Same |

Verify H uses head even with working eyes; tag every E sample eyes/head/unavailable. Do not create H by asking for immobile eyes, swapping headsets, or disabling all OpenXR.

**Primary analysis follows assigned H/E.** E fallback/errors remain E outcomes. Add exploratory “≥90% eye availability, no gap >200 ms” subset; proposed thresholds freeze before collection and never replace primary analysis. Other commands=mixed/degraded, not pure-eye.

If E cannot start, record calibration/device unavailability, retain H/interview data, and report recruitment/device attempts/E-startable/paired counts. Never fabricate E or replace failures indefinitely to reach 12 pairs; total cap 15.

## 7. Tasks, materials, and truth

### 7.1 Six tasks per condition, twelve total

Use independently verifiable simple edits. **Unity vertical axis is Y.** Present prompts via independent headset study UI/standard speech; no truth/highlights sent to backend. Log reveal when shown; users find target after prompt. Same delivery in H/E.

| Type | Two instances | Fixed first command | Expected state |
|---|---|---|---|
| Height | Isolated; one of similar objects | “Make this twice as tall along the Y axis.” | scale.y×2, x/z and others unchanged |
| Rotation | Isolated; adjacent target | “Rotate this 30 degrees around the world Y axis.” | world Y +30°, others unchanged |
| Color | Independent; repeated target | “Make this red.” | Independent material red, other objects/slots unchanged |

Six to eight distractors; no locomotion/teleportation/fully occluded targets. Predefine positions/angular size: proposed large 6-8°, adjacent 3-4° with matched spacing, frozen after pilots. Material choices are not device-accuracy promises.

X/Y change instances/positions but match size, distance, distractors, speech, initial transforms. Avoid complex parent scaling, standardize rotation. Reload reliable initial state and clear Selection/queue/model context; not repeated Undo alone.

### 7.2 Exploration, separate from primary success

Eight minutes; choose two: recolor only sofa cushion; move whole sofa; create primitive at table/floor position; recover after unexpected result. Natural commands reveal needs/misunderstandings/repair chains.

Do not manufacture failure for everyone. Designed recovery must be disclosed as practice, consistent, and separate from spontaneous errors. Complex generation is qualitative, not pooled with fixed-task efficiency/correctness.

### 7.3 Trial procedure

1. Reset, verify source/empty Selection/backend, enable recording.
2. Reveal/start. Use assigned input/BY/fixed first utterance; no concurrent think-aloud.
3. Preserve first grounding/state changes, independent assertion; “done” does not stop timer automatically.
4. Allow repair/Undo/retry; manual/moderator rescue separate. No silent target correction or “look left” coaching.
5. Stop when truth reached and execution ends, or **90 seconds including wait**. Cancel queue, confirm no late execution, reset.
6. Record errors/missing. Stop early if unsafe rather than waiting for cap.

Keep first complete success/final success/wrong object/wrong edit/no edit/rescue separate. Errors may overlap; no edit is not a wrong-object edit.

## 8. Session approximately 75-90 minutes

| Stage | Time | Procedure |
|---|---|---|
| Consent/screening | 8 min | Data/withdrawal/experience/VR/language; no E-superiority suggestion |
| Fit/calibration | 8-12 min | Quest eyes, IPD/clarity, Link/source, not webcam calibration |
| Instruction/practice | 8 min | H/E two practices each, same support limit, log extra help |
| First condition | 10-13 min | Six trials, 90s/reset; questionnaire |
| Headset-off rest | 3-5 min | Discomfort; no pressure to finish |
| Second condition | 10-13 min | Six matched trials, same questionnaire |
| Exploration | 8 min | Separate, think-aloud allowed |
| Overall survey/interview | 15-20 min | Headset off, SUS/interview |
| Close | 2 min | Contact/deadline, compensation, ID check |

About 72-94 minutes overall; book 90, allow early stopping. Never compress consent/rest. If pilots overrun, uniformly reduce tasks or split sessions before formal study; no performance-based deletion. Offer rest after about 15 continuous headset minutes.

Fix Windows PC, Quest Pro, Link, refresh, resolution/render scale/runtime; disable ML-Agents/background load. Enable eyes on headset/PC Link and verify after Play restart. Independent nine-point validation before session/between blocks records pass/fail/angular error; calibration training points are not independent tests.

At most two system calibrations/person, time/outcome logged. Record unavailability, not exclusion to inflate accuracy; natural eye movement must change ray, one eyes=true insufficient.

## 9. Screening questionnaire

Introduction: Quest Pro Unity editing prototype, not skill assessment; no Unity experience required; about 75-90 minutes, voluntary headset/mic/eye-data use under school recruitment/compensation requirements.

1. ≥18? Yes/no; no→ineligible.
2. Total Unity: 0 / >0 to 2 hours / >2 to <20 hours / 20-100 hours / >100 hours; approximate hours.
3. Past-six-month hours and completed scene/project? Hours +yes/no.
4. Other tools: none/Blender/Unreal/CAD/other, frequency.
5. VR uses 0/1-5/6-20/>20; devices.
6. Controller familiarity 1 completely unfamiliar-5 very familiar.
7. Voice/generative-editing use never/monthly/weekly/daily; this product before?
8. Preferred language, comfortable understanding/reading commands?
9. Glasses/contacts/accommodations? Optional, no diagnosis.
10. Any reluctance/difficulty using VR or questions? Private discussion/skip; no medical detail.
11. Product developer? Pilot-only.
12. Willing after understanding data? Contacts separately stored.

Not diagnosis; follow device/school guidance, avoid irrelevant sensitive information, never pressure someone with prior discomfort to try.

## 10. Post-use questionnaires

### 10.1 After each condition

“Rate only the tasks just completed, not the other method. No correct answers.”

**A. Raw NASA-TLX, six-dimension unweighted mean.** [R7]

| Study-adapted question; confirm wording before use | 0 | 100 |
|---|---|---|
| Mental: how much thought/attention/judgment? | Very low | Very high |
| Physical: how much activity/exertion? | Very low | Very high |
| Temporal: how much pace/time pressure? | Very low | Very high |
| Performance: how well did you complete tasks? | Very good/successful | Very poor/unsuccessful |
| Effort: how hard did you work? | Very little | Very much |
| Frustration: irritation/tension/frustration? | Very low | Very high |

Five-point increments; mean six, range 0-100, higher workload worse. No pairwise weighting; not full weighted TLX. **Preserve performance polarity.** Choose one language version; disclose adaptation, no uncritical norm comparison. Any missing→total NA, retain dimensions, never zero-fill. [R7]

**B. Custom items, not a standardized trust scale:**

1. I can tell which object the system is targeting. 1 strongly disagree-7 strongly agree.
2. I am confident the system will edit the right target. Same.
3. When results are unexpected, I know how to recover. Same, NA allowed without experience.
4. How effortful was targeting? 1 not effortful-7 very effortful.
5. Current discomfort 0 none-10 extreme; rest/stop now?

Report items separately, no invented trust index. Discomfort monitoring is not SSQ/diagnosis. Record pre-block discomfort too. Stop on request; proposed ≥4 or +3 baseline→headset-off pause, participant decides resume. Operational, not medical threshold; serious symptoms per school procedure.

### 10.2 Overall usability SUS

Once after whole product, not one “product” per targeting block. English below preserves translated Chinese study wording; confirm against chosen original/validated version, retaining wording/order/scoring. No deleting items while retaining SUS label. [R8]

1 strongly disagree, 2 disagree, 3 neutral, 4 agree, 5 strongly agree.

1. I think I would like to use this system frequently.
2. I find the system unnecessarily complex.
3. I find the system easy to use.
4. I think I would need technical assistance to use this system.
5. I find the system's functions well integrated.
6. I find too much inconsistency in this system.
7. I think most people could learn to use this system quickly.
8. I find the system cumbersome to use.
9. I feel confident using this system.
10. I need to learn many things before I can use this system.

Odd responses minus 1, even 5 minus response, sum × 2.5, 0-100, **not percent accuracy**. Missing item→NA total, retain responses/valid N. No uncritical pass line for small/adapted samples. If appropriate translation unavailable, use original English where suitable or custom usability items, explicitly abandoning standardized SUS total.

### 10.3 Acceptance/privacy/preference, custom

1. Prefer head/eye/depends/no preference/insufficient experience; why?
2. Would use again for similar tasks, 1-7 agreement.
3. Would use in real project, same, analyzed separately.
4. Concern about eye data, 1 not concerned-7 very concerned.
5. Concern about voice transcripts/scene sent to services, same.
6. Which leave machine: eye-camera images/transcript/scene-target information/unsure? Explain actual configuration; not deceptive testing; clarify after interview.
7. One improvement: targeting/speech/wait/feedback/recovery/comfort/privacy/other, example.

## 11. Semi-structured interview 15-20 minutes

Same core questions across groups, order adapted to real events. Ask examples without “is eye gaze more natural?” or implying expected advantage.

### 11.1 Core questions

1. Current 3D creation/editing practice; if none, how express desired results?
2. Before use, how did you think “this” would be resolved? Has that changed?
3. Most uncertain target/scope: what noticed, what happened? Review only their events.
4. How know listening/processing/done/failure? Missing/unclear feedback?
5. Unexpected result: recovery actions/help/confusion? No error→hypothetical, label it.
6. Tasks suited to head/eyes/neither?
7. Effort/discomfort from turning, staring, speaking, controls, waiting? Open first, then probes; not all eye-related.
8. Concrete real use tomorrow; reasons to abandon?
9. When direct execution versus confirmation/preview/clearer Undo? Do not claim preview exists.
10. Desired data transparency/control; reaction to text sent to services?
11. First improvement; anything missed?

### 11.2 Experience probes

Experienced: compared to Inspector/Hierarchy/shortcuts, saved work/lost precision; prefabs/hierarchies/material slots/real assets; commands requiring explicit selection. Retrospective opinions do not prove faster than native Unity without a native baseline.

Novice: difficult terms/axes/scope, need examples, expressing intent without learning Unity, remaining domain barriers. Axis misunderstanding is not automatically gaze failure.

VR separately: headset/controller learning dominating experience; glasses/fit/view effects, no diagnosis requested.

### 11.3 Coding

Begin deductively, allow new codes: target uncertainty, scope/part misunderstanding, hidden state, speech errors, model errors, tracking/fallback, waiting, recovery, burden, privacy, real value. Keep event IDs, verified excerpts/paraphrases, interpretations, counterexamples. [R9]

Two researchers independently code the same 2-3 transcripts, discuss/freeze codebook, split remaining work and cross-check key findings. Explain disagreement resolution. Do not claim purely reflexive analysis while treating agreement as sole quality criterion. Declined recording→notes confirmed afterward, never invented verbatim quotes.

Count participants per theme with examples/counterexamples, describing this sample only. Small interviews do not guarantee saturation, especially across Unity/VR backgrounds.

## 12. Analysis

### 12.1 Freeze first

Fix endpoint, tasks/tolerances/timeouts/source-purity/group/version/exclusion rules and protocol hash before formal collection. Serious bug fixes require pause, new version, separately reported batches, not silent pooling.

### 12.2 Objective outcomes

- The independent unit is the participant. Compute H/E six-task success per person and E-H, show paired/overall/group distribution. Twelve participants completing all tasks produce 144 trials, not 144 independent participants.
- Report differences/raw denominators/intervals. Participant-cluster bootstrap (e.g. 10,000 draws) possible; N≈12 intervals unstable, state this.
- Exploratory paired permutation possible. Secondary participant/task mixed models must not force complex interactions/random effects or p-values after separation/nonconvergence.
- Experience primarily descriptive; interaction exploratory. One group's significant result and another's nonsignificant result do not prove group difference.
- Successful-task times plus failures/timeouts. Right-censor at 90 seconds or separately show min(time,90) budget use; not actual completion. Do not discard failures to call E faster.
- Cluster continuous rays by person/scene; many samples are not independent people. Deterministic/human results separate.
- Secondary outcomes exploratory; prespecify Holm if multiple tests, show raw/adjusted p; prioritize practical effects/error costs.

### 12.3 Subjective/mixed evidence

- Pair Raw TLX; seven-point medians/ranges/individual responses, no zero filling; overall/group SUS distributions.
- Explain contradictions: easy but unsuccessful; correct but distrusted; unnoticed fallback. Combine events/interviews, no favorable cherry-picking.
- Joint table: event→feeling/quote→cause hypothesis→change→next metric. Verify actual Undo before blaming feedback.
- Experience is background; interviews cannot replace controlled comparisons. Perceived speed and actual latency stay distinct.

### 12.4 Missingness/faults/withdrawals

All revealed tasks enter run table as success/timeout/participant_stop/technical_failure/missing_log, with separate assertions/errors. Unrevealed after withdrawal=not run, not failure; started safety stops remain operational outcomes with reason.

Unable-to-start E enters availability flow, no invented pair. Report actual paired N plus failures/feedback for all attempts. Eye loss/low success/dislike are not post-hoc exclusions.

Use retained snapshots/prespecified manual checks to verify outcomes; otherwise unknown, not invented success/failure. Confirmed successes/all revealed is a conservative lower bound; give bounds assuming all unknowns fail/pass, verifiable proportion, sensitivity analyses. Do not silently delete unknowns to improve a rate.

## 13. Ethics/privacy/VR safety

- School-required approval/teaching authorization, not automatic exemption for 12 people. Voluntary/skip/withdraw/compensation rules, base amount after withdrawal, no success rewards.
- Distinguish behavior, local audio, eye/head, interview, screen, quotes permissions. Notes replace interview audio. Declining necessary eye data means no E study, other participation options where appropriate.
- Explain audio.wav/record.json, actual provider/CLI receiving transcript/target/scene text, terms/accounts; no third-party zero-retention promise. No eye-camera images, optional viewport Pictures off.
- Fictional scenes/research accounts, no personal logins/projects. Raw voice/trajectories/calibration features sensitive, team-only.
- Restricted/encrypted outside repository; contacts separate, no GitHub raw logs. Raw audio/poses/eyes/interviews deleted within 30 days after verification; deidentified metrics/excerpts until 90 days after course end. Disclose overriding school rules.
- Withdrawal before anonymous lock, proposed 14 days after the session; explain deletion and untraceable-public-summary limits. No need retain raw audio to project end.
- Fit/hygiene/glasses/boundary/cables/supervision. Seated/limited standing, no free locomotion/teleport/rapid turns; rest, stop for discomfort. Never insist for sample size.
- Arbitrary C# and files/assets may not Undo: isolate/backup/rehearse restore. Play exit preserves edits; resets never overwrite personal work.

Suggested explanation:

> We study the prototype, not your ability. You may stop, rest, or skip questions. We record tasks, events, and eye/head pointing. The program saves command audio locally; transcripts/scene text enter specified model services. No eye-camera images are saved/uploaded. Interview/screen recording require separate permission. Data are for this study and deleted on the explained schedule. Please confirm understanding of data and withdrawal.

## 14. Schedule, roles, readiness

| Stage | Effort | Deliverable |
|---|---|---|
| Freeze build | 0.5-1 day | Preserve local work, integrated code/scene hashes; no pull over Quest changes |
| Tools/benchmarks | 2-4 days, adjust to actual state | Source control/logs/truth/reset/performance/persistence,48 rays / 72 executions |
| Ethics/recruitment | Parallel | Authorization/consent,6+6 formal participants and 2 pilots; reserves within 15 |
| Pilot | Two, about 1 day; third if needed | Cap/fit/eyes/wording/duration; revise then freeze |
| Formal | Target 12, 90 minutes each + 15 minutes reset/cleaning; total ≤15 | Tasks/exploration/surveys/interview, daily checks |
| Analysis/report | 2-4 days | Paired outcomes/engineering/themes/counterexamples/limits/priorities |

Engineering lead: instrumentation/assertion/freeze. Moderator: script, no answers. Observer: rescue/discomfort/events. Second analyst: truth/key interpretations. Roles may combine with disclosed blinding limits. Participants cannot be H/E blinded; neutral codes in validation/analysis may reduce interpretation bias.

**Before formal collection:**

- [ ] Eyes reach application via Link; H forced; E sample source/gaps known.
- [ ] Otherwise matched conditions; no webcam/numbered-clarification promises.
- [ ] X/Y and independent truth ready; 90-second timeout, reset, and late-command handling validated.
- [ ] Commands/grounding/tools/state/PTT/task-time linked.
- [ ] No severe mis-save/rig change/unrecoverable error; exit/reopen assertions pass.
- [ ] STT/model budget/latency acceptable, logging does not harm frames.
- [ ] Consent/withdraw/safety/deletion/interview ready; pilots before formal.

Without source control, run single-condition E usability +engineering +experience interviews, report head fallback, **cancel causal H/E comparison**. Without real eyes, revise title/protocol/inform participants for head-only research; no eye-effect claim.

## 15. Reporting template

1. **Build/device:** code, scene and protocol hashes; Bridge; PC, Quest, OpenXR, Link, model, language, and connection.
2. **Sample flow:** contacted __; consented __; experienced __ / novice __; usable device __; paired completion __; withdrawal/calibration failure __ and reasons.
3. **Engineering:** geometry __/48; backend __/72; faults __/30; 100-command stability, frame-time and persistence results. Report actual denominators.
4. **Primary:** H/E first successes / revealed tasks per participant, difference and interval; assigned-condition and pure-eye results separately.
5. **Secondary:** final success, object/edit errors, latency/timeouts, repairs/rescue, tracking, TLX/SUS and custom-item distributions.
6. **Themes:** issue, event, anonymous example/counterexample, Unity/VR background, proposed change.
7. **Limits:** convenience/small sample, tracking incompatibility, experience confounding, device/model versions, learning, simple tasks and fallback.
8. **Next steps:** rank severity × people affected × evidence strength; prioritize incorrect saves and safety before cosmetics or features.

**No formal results have been collected yet.** All success rates, times, and scales require measurement; CI/source/developer demonstrations do not replace participants.

## References

Pinned source snapshot, not moving main.

[R1] GazeVoice. [Commit d581b98](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/commit/d581b9881fed6ba6ad42b5ef982372baab4fb448), 2026-11-21; inspected 2026-11-23.

[R2] GazeVoice. [README: VR, storage, limits](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/README.md).

[R3] GazeVoice. [GazeVoiceVR.cs](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/unity_package/Editor/VR/GazeVoiceVR.cs): EyeRay/Look/Cast/Status/Gaze/sampling.

[R4] GazeVoice. [VoiceService](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/service.py); [gaze.py](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/gaze.py): logs/ground_cast/source/candidates.

[R5] GazeVoice. [GazeVoiceVRKeep.cs](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/unity_package/Editor/VR/GazeVoiceVRKeep.cs); [Unity prompt](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/prompts/unity_editor.md): persistence/assets/scenes/object/part/execution.

[R6] Siqi Chen & Ghanibhuti Gogoi. Gaze and Voice for Context-Aware 3D Editing. Initial proposal, October 2026. Retained locally as Gaze_Voice_3D_Editing_Proposal.pdf outside repository. Extends original RQ3 to Quest Pro without transferring webcam assumptions.

[R7] Hart, S. G., & Staveland, L. E. (1988). Development of NASA-TLX (Task Load Index): Results of empirical and theoretical research. Advances in Psychology, 52, 139-183. [DOI](https://doi.org/10.1016/S0166-4115(08)62386-9). Unweighted Raw TLX; adaptation limits disclosed.

[R8] Brooke, J. (1996). SUS: A “quick and dirty” usability scale. In Usability Evaluation in Industry, pp. 189-194. [DOI](https://doi.org/10.1201/9781498710411-35); [scoring guidance](https://measuringu.com/sus/). Overall perceived usability, not accuracy.

[R9] Braun, V., & Clarke, V. (2006). Using thematic analysis in psychology. Qualitative Research in Psychology, 3(2), 77-101. [DOI](https://doi.org/10.1191/1478088706qp063oa). Report actual codebook/interpretation/counterexample approach.
