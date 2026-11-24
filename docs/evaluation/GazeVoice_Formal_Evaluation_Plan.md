# GazeVoice Evaluation Plan: 30-Minute User Study

Version 2.0 | 2026-11-24 | Unity VR + Quest Pro + Windows / Quest Link / OpenXR

**Two separate activities: engineering evaluation outside participant sessions, and a 30-minute eye-first + voice product trial.** This replaces the earlier 75-90-minute H/E comparison. No participant results are claimed, and this document does not implement study scenes, logging or validators.

## 1. Scope and sample

- Ask whether users can complete simple edits, understand feedback, recover, and identify useful improvements. Do not compare eye and head input or claim eye superiority.
- Plan two pilots and 12 formal participants: six Unity-experienced and six novices, 14 people total. Maximum 15 unique people, including pilots, withdrawals, incompatible tracking and interview-only participation. Pilots do not enter formal outcome summaries.
- Experienced: at least 20 total Unity hours AND, within the last six months, either a completed scene/project or at least five hours of use. Novice: at most two total hours and no independently completed Unity project. Other experience is intermediate; report separately, without relabeling.
- Adults 18+, voluntary consent and task-language comprehension. Record Unity hours, recent use/project, VR familiarity and initial discomfort. Developers may pilot only. Experience-group differences are descriptive, not causal.

## 2. Objective evaluation: run separately

Follow the [objective quickstart](GazeVoice_Objective_Evaluation_Quickstart.md): 48 fixed-ray cases, 72 command executions, 30 fault-recovery runs, 12 persistence cases, 100 continuous commands, and 18 performance segments of 60 seconds. These are engineering runs, not extra tasks for participants.

Retain first/final independently verified success, target/edit errors, repairs, latency/timeouts, actual eye availability/fallback, Undo/persistence and headset frame times. Use a backed-up study project, fixed versions and independent target/non-target checks; model 'done' is not evidence. Fault injection is participant-free. Fixed rays test code, not eye-tracking accuracy.

Inspected-source limitations [R1-R5]: eye-first input can fall back to the head; a recent eyes=true or command-majority gaze.source does not establish continuous eye use. Gaze samples are queryable but not automatically persisted as a research dataset; export promptly with gaps and command IDs. Independent state assertions, task reveal/end timestamps and reliable reset/cancellation still require preparation. The source audit was on 2026-11-23 at d581b98; verify the actual study build instead of treating it as a live repository-status report.

## 3. Participant session: target 30 minutes

Send information and screening ahead of time; allow questions and sign consent on site. Book **40-minute appointments**: 30 minutes with the participant plus 10 for cleaning/reset. Pilot the timing before formal collection; breaks and consent take priority over the clock.

| Elapsed | Stage | Action |
|---|---|---|
| 00-03 | Information and consent | Confirm eligibility, data destinations, questions and signature |
| 03-10 | Fit, calibration and practice | Verify real eyes, independent nine-point check, one practice exercise |
| 10-18 | Three tasks | Height, rotation, color; up to 90 seconds each including waiting/repair; reset between tasks |
| 18-22 | Headset-off survey | Complete ten-item SUS, overall workload and discomfort |
| 22-28 | Interview | Three questions, with one brief experience-specific probe if time permits |
| 28-30 | Close and buffer | Check notes, compensate, remind withdrawal deadline |

Calibration: at most two attempts within the setup period, without rushing or prolonging discomfort. If eyes cannot be verified by minute 10, mark tracking unavailable and offer a clearly labeled demo/interview only; do not count head-only use as a completed eye-first trial. Record actual duration, overruns and missing stages. Do not remove failed tasks to make the schedule fit. No second input condition, separate exploration block, per-condition survey, H/E preference question or NASA-TLX is administered.

## 4. Three tasks and records

Use one frozen scene set and the same T1-T2-T3 order for everyone. Freeze object positions, reference prompts, language and acceptance thresholds after pilots. No locomotion; use easily visible targets with modest distractors. These three tasks are not a dense-target accuracy benchmark. Target images/keys belong in the study prompts and researcher answer sheet, never in model ground truth.

| ID | First utterance | Independent acceptance |
|---|---|---|
| T1 height | Make this twice as tall along the Y axis. | Target scale.y x2; x/z unchanged; relative scale error <=2% |
| T2 rotation | Rotate this 30 degrees around the world Y axis. | Target world Y +30 degrees; angular error <=2 degrees |
| T3 color | Make this red. | Independent target material red; error <=0.02/channel after common color-space conversion |

Every task also requires the correct object and no unintended non-target changes. Use non-test objects for practice: recolor blue, then demonstrate Undo if time permits. Do not manufacture formal-task errors.

Per task: reset scene/Selection/queue/model context; verify tracking; reveal and start timing; save first-command outcome; allow natural repairs; stop at verified completion or 90 seconds including waiting; cancel and verify no late execution; record before reset. No target-location coaching; extra assistance is rescue. Record task/command/log IDs, first and final independent success, time/status, repair turns, rescue and actual eye/head/unavailable source. Record automatic model retries separately.

Use 1=verified success, 0=verified failure, ?=unknown, NA=not run. First success requires the first command, no extra input, rescue or non-target edit; final independent success allows user repair but no manual rescue. Report rescued completion separately. Write 90/timeout, never a completion time. Started technical failures and participant stops remain visible; tasks never revealed are not failures.

## 5. Survey and six-minute interview

- **SUS:** all ten English items, original wording [R8], 1 strongly disagree to 5 strongly agree; paper form in the personal packet. Chinese local version is a study translation. Score odd items minus 1, even items 5 minus response, sum x2.5. Missing item means total NA. SUS is perceived usability, not accuracy or a pass/fail percentage; avoid uncritical norms across languages.
- **Two custom items:** overall workload 0 none-10 extreme; current discomfort 0 none-10 extreme, with baseline recorded before use. Analyze separately. The workload item is not NASA-TLX and is not a validated workload total.
- Ask: (1) What was hardest or most uncertain, and how did you judge or fix it? (2) How could you tell it was listening, processing, done or failed? What was unclear? (3) What would you use this for, and what one thing should improve first? Allow about two minutes each; probe actual tasks neutrally. If no error occurred, label recovery suggestions hypothetical.
- Optional short probe: experienced users, what control/work was gained or lost? Novices, which term or step needed help? Ask about privacy/comfort if raised, within the same time budget; no claim of an in-depth interview or thematic saturation.

## 6. Consent, safety and readiness

Before recruitment, complete actual contacts, school authorization, compensation, speech/editing services, what raw audio/transcripts/scene-target information leave the PC, and applicable terms. Necessary data: experience, task/command records, voice processing and eye/head pointing. No eye-camera images. Interview audio, screen recording and anonymous quotes each require separate optional consent; notes replace declined interview audio.

Restricted storage outside Git, with identity/consent separate from task records. Planned deletion: raw recordings/traces within 30 days after verification; deidentified results until 90 days after the course ends. Withdrawal by ID within 14 days of the session, with limits for already untraceable anonymous summaries. Explain overriding school/provider requirements before consent. Compensation is independent of success and base compensation remains after withdrawal.

Prefer sitting, clear cables and disinfect. Stop on request; pause/remove headset for headache, eye pain, nausea, discomfort >=4/10 or a rise >=3 from baseline. This is an operational rule, not diagnosis. Never shorten consent/rest or force completion to hit 30 minutes. If setup exceeds the budget, record incomplete eye-first use; feedback still has value.

Before formal collection: two pilots; frozen protocol/build/scene/commands; verified real eyes; consent complete; three mapped targets; logs and independent verification; tested reset/Undo/cancellation/persistence. If automatic logging is incomplete, use documented observer evidence and report unavailable metrics; never invent timing or eye validity.

## 7. Report only what was measured

1. Participant flow: pilots, consented, experience groups, eye-startable, tasks started/completed, interview-only, withdrawals, actual session durations and overruns; unique total <=15.
2. Tasks: per-participant first/final independent success out of revealed tasks, task-specific denominators, repairs, rescues, times and failure/unknown counts. With 12 complete participants there are 36 tasks, not 36 independent people. Keep fallback cases; do not reclassify them as pure eye use.
3. Surveys: SUS distributions plus separate workload/discomfort ratings and baseline change. Experience-group contrasts are exploratory, with VR familiarity and language limits.
4. Interviews: issue -> people affected -> task/example or permitted quote -> suggested change. Two researchers align codes on two note sets and cross-check main findings [R9]. Keep dissent and distinguish notes from verbatim quotes.
5. Engineering results: use the separate quickstart, actual denominators, latency/fallback distributions and unknown-outcome bounds. Report product version and instrumentation gaps. No eye-vs-head advantage, native-Unity speed advantage or population-level generalization is supported.

For field use and print counts: [concise field guide](GazeVoice_User_Study_Field_Guide.md). Two staff can divide moderation and observation; one moderator may combine roles if the limitation is reported.

## References

Pinned code links document the historical source audit; methods references support measurement choices, not participant results.


Pinned source snapshot, not moving main.

[R1] GazeVoice. [Commit d581b98](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/commit/d581b9881fed6ba6ad42b5ef982372baab4fb448), 2026-11-21; inspected 2026-11-23.

[R2] GazeVoice. [README: VR, storage, limits](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/README.md).

[R3] GazeVoice. [GazeVoiceVR.cs](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/unity_package/Editor/VR/GazeVoiceVR.cs): EyeRay/Look/Cast/Status/Gaze/sampling.

[R4] GazeVoice. [VoiceService](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/service.py); [gaze.py](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/gaze.py): logs/ground_cast/source/candidates.

[R5] GazeVoice. [GazeVoiceVRKeep.cs](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/unity_package/Editor/VR/GazeVoiceVRKeep.cs); [Unity prompt](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/d581b9881fed6ba6ad42b5ef982372baab4fb448/gazevoice/voice/prompts/unity_editor.md): persistence/assets/scenes/object/part/execution.

[R6] Siqi Chen & Ghanibhuti Gogoi. Gaze and Voice for Context-Aware 3D Editing. Initial proposal, October 2026. Retained locally as Gaze_Voice_3D_Editing_Proposal.pdf outside repository. Extends original RQ3 to Quest Pro without transferring webcam assumptions.

[R7] Hart, S. G., & Staveland, L. E. (1988). Development of NASA-TLX (Task Load Index): Results of empirical and theoretical research. Advances in Psychology, 52, 139-183. [DOI](https://doi.org/10.1016/S0166-4115(08)62386-9). Background only; NASA-TLX is not administered in the 30-minute protocol.

[R8] Brooke, J. (1996). SUS: A “quick and dirty” usability scale. In Usability Evaluation in Industry, pp. 189-194. [DOI](https://doi.org/10.1201/9781498710411-35); [scoring guidance](https://measuringu.com/sus/). Overall perceived usability, not accuracy.

[R9] Braun, V., & Clarke, V. (2006). Using thematic analysis in psychology. Qualitative Research in Psychology, 3(2), 77-101. [DOI](https://doi.org/10.1191/1478088706qp063oa). Report actual codebook/interpretation/counterexample approach.
