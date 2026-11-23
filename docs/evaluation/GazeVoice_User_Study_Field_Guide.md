# GazeVoice User Study Field Guide and Forms

2026-11-23 · Unity VR + Quest Pro with real eye tracking · Follow in order; copy Sections 3-8 for each participant

**Questions: which targeting method is easier to use, where does the product fail, and what should improve first?** Each participant tries H (head pointing + voice) and E (eye-first targeting + voice). Target **2 pilot + 12 formal participants = 14 people**; six with and six without Unity experience in the formal sample. Maximum 15 unique people including pilots, withdrawals, and interview-only participants.

## 1. Moderator: prepare before inviting participants

- [ ] Complete school-required approval/authorization, information, compensation, and withdrawal contacts. Fill the blanks in Section 3.
- [ ] Back up a study-only project; fix versions, PC, headset, model, language, and feedback. Do not load personal projects.
- [ ] **H must force head rays; E must record eye/head fallback per sample. This study configuration still needs implementation. Automatic fallback is not a randomized control condition.**
- [ ] Validate X/Y scenes, expected states, timing, logs, reset, and timeout cancellation; use independent outcome checks.
- [ ] Verify eye tracking, talk buttons, Undo, and error feedback on hardware. Freeze the procedure after two pilots.
- [ ] Collect only after consent. Clear cables, disinfect the headset, and prefer sitting; core tasks require no walking/teleportation.

**If preparation is incomplete:** run a single-condition product trial plus questionnaires/interviews, recording actual fallback. Drop H/E effect comparisons. Without functioning real eye tracking, do not call it an eye-tracking study.

## 2. Session flow: book 90 minutes

| Step | Approximate duration | Moderator action |
|---|---|---|
| 1. Information, consent, screening | 8 min | Section 3; explain data destinations and withdrawal |
| 2. Fit, calibration, instruction | 15-20 min | Read Section 4; two practice tasks per condition, excluded from formal results |
| 3. First condition | 10-13 min | Six tasks, maximum 90 seconds each; Sections 6 and 7 |
| 4. Headset-off rest | 3-5 min | Ask about discomfort; recheck eye tracking and selected source |
| 5. Second condition | 10-13 min | Six matched tasks; same records and questionnaire |
| 6. Free exploration | 5-8 min | Part recoloring, whole-object movement, creation/placement, or Undo; thinking aloud is allowed |
| 7. Headset-off evaluation and interview | 15-20 min | Section 8; check data, compensate, finish |

Allow rest at any time; offer a break after approximately 15 continuous headset minutes. If pilots overrun, adjust consistently before formal collection. Do not drop tasks mid-study because one condition performs poorly.

## 3. Form A: participant details and consent

ID: ____ Date: ____ Moderator: ____ Pilot/formal: ____ Code/scene version: ____

| Screening question | Complete on site |
|---|---|
| Aged 18 or over? | □ Yes □ No; no → not eligible under this protocol |
| Total / last-six-month Unity hours? Scene/project completed in the last six months? | __ hours / __ hours; □ Yes □ No |
| Unity group | □ Experienced: ≥20 total hours AND a scene/project or ≥5 hours in the last six months; □ No experience: ≤2 total hours and no independently completed project; □ Intermediate: qualitative only |
| VR use count / other 3D-tool experience | □ 0 □ 1-5 □ 6-20 □ >20; tools/frequency: ____ |
| Understand task language and controller? Comfortable fit? | Language: ____; □ Yes □ Needs help; fit: ____ |
| Product developer? | □ No □ Yes; pilot only |
| Initial discomfort 0-10; requested accommodations | __; ____; no medical diagnosis requested |

**Moderator: complete these fields, then read the statement.**

Research contact: ____ Compensation: ____ Actual model/services: ____
Where transcripts and raw audio are each sent: ____

> We are testing the prototype, not your ability. You may rest, withdraw, or skip questions at any time; performance will not affect compensation. The program records command audio, task logs, and eye/head pointing. Transcripts and scene information enter the stated model services; the actual destination of raw audio is explained above. We do not collect eye-camera images. Interview recording, screen recording, and anonymous quotations require separate permission. Data are used for this study and stored with restricted access. Raw recordings/traces are deleted within 30 days after verification; anonymous results remain until 90 days after the course ends. You may request withdrawal within 14 days after your session; already untraceable anonymous summaries are excluded. Approved school requirements take precedence and will be explained.

- [ ] I understand and consent to participation and the necessary behavior logs, voice processing, and eye/head data recording. If not, do not begin this study.
- Interview recording: □ Agree □ Decline; use notes if declined. Screen recording: □ Agree □ Decline. Anonymous quotations: □ Agree □ Decline.
- Participant confirmation/signature: ____ Date: ____ Withdrawal deadline: ____

Store contact details separately from IDs. Do not upload raw data to GitHub. Compensation is not tied to success; provide the agreed base amount even after withdrawal.

## 4. Participant one-minute guide

1. **Read the task and find the intended object.** The moderator presents the target and requested change.
2. **H: turn your head toward the target. E: look at it with your eyes.** Use the current method naturally; do not freeze your head or eyes.
3. **Hold right B or left Y while speaking, release when finished, and wait for feedback.** Practice once to verify the button.
4. Read the first command as provided. Repairs may use natural phrasing, retries, or “Undo the last edit.” Wait for the previous command to finish.
5. Keep trying if necessary; each task lasts at most 90 seconds. Do not think aloud during formal tasks; we will ask afterward.
6. Tell the moderator if you feel uncomfortable, cannot see clearly, or need help. You may rest or stop at any time.

**Teaching demonstration:** Y is the vertical axis; height changes affect height only; rotation around world Y turns an object horizontally. Demonstrate using non-test objects. Give two practices per condition and note extra assistance.

**Moderator device check:** independent fixation validation □ Pass □ Fail; calibrations __, maximum two; forced head source in H □ Pass; real eye source in E □ Pass; practice assistance: ____

After calibration, validate against nine independent fixation points. Recheck before condition two; retain pass/fail and angular error where measurable. If E cannot start, record device unavailability and retain completed trials/interviews. Do not invent paired data or recruit past the total cap of 15.

## 5. Moderator task cards: six tasks in each condition

X/Y change instances and positions while matching angular size, distance, and distractor counts. Each scene contains six to eight distractors; adjacent tasks include similar objects. Prepare participant target-reference images in advance. **Do not send ground truth, target IDs, or research highlights to the model.** Timing begins at task reveal.

| Task ID, prefixed X/Y | Participant task / first command | Moderator acceptance |
|---|---|---|
| T1 isolated / T2 adjacent | Double the designated object's height. “Make this twice as tall along the Y axis.” | scale.y ×2; x/z unchanged |
| T3 isolated / T4 adjacent | Rotate the object +30° around the vertical world axis. “Rotate this 30 degrees around the world Y axis.” | world Y +30° |
| T5 isolated / T6 repeated | Recolor the object red. “Make this red.” | Independent material red; no shared-material spillover |

All require the right object and no non-target changes. Rotation tolerance ≤2°, relative scale error ≤2%, color error ≤0.02 per channel in a common color space. Avoid complex parent scaling; fix initial states. Freeze language, tolerances, and order before formal testing; any translated commands must be identical across conditions.

**Assign separately to participants 1-6 within each experience group:**

| Within-group number | First / second condition |
|---|---|
| 1, 5 | H-X / E-Y |
| 2, 6 | E-X / H-Y |
| 3 | H-Y / E-X |
| 4 | E-Y / H-X |

Pregenerate and save the six-task order for each material set after piloting. Use the same saved order for that set; never change it based on performance. Reset scene, Selection, queued commands, and model context before every task and participant.

## 6. Form B: task observation record

ID: ____ Actual condition/task order: ____ Log directory: ____

**Every task: reset → verify source → reveal/start timer → record first outcome → allow repair → stop at success or 90 seconds → save.** After timeout, cancel and verify that late commands cannot run before proceeding. Do not coach target position; record any extra help as rescue.

| Condition / actual task ID | First success | Final independent success | Seconds | Repair turns | Rescue | Error/fallback/command ID |
|---|---|---|---|---|---|---|
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 1 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |
| Block 2 / ____ | __ | __ | __ | __ | __ | ____ |

First success means the first user command is correct without extra input, rescue, or non-target edits. Record internal automatic retries separately. Final independent success permits user repair but no manual rescue; rescued success is separate. Use 1=success, 0=confirmed failure, ?=unverifiable, NA=not run. Write “90/timeout,” not a completion time. Error shorthand: object/operation/no edit/speech/tracking/system.

## 7. Form C: immediately after each condition

ID: ____ Pre-condition discomfort: H __/10, E __/10

**A. Workload: 0-100 in five-point steps. Rate only the condition just completed.** These are study-adapted Raw TLX prompts; confirm consistent wording before formal use.

| Question | 0 → 100 | H | E |
|---|---|---|---|
| How much thinking, attention, or judgment was required? | Very low → Very high | __ | __ |
| How much physical activity or effort was required? | Very low → Very high | __ | __ |
| How much time pressure did you feel? | Very low → Very high | __ | __ |
| How well did you complete the tasks? | Very good/successful → Very poor/unsuccessful | __ | __ |
| How much effort did you invest? | Very little → Very much | __ | __ |
| How much frustration, tension, or irritation did you feel? | Very low → Very high | __ | __ |

**B. Brief experience items: first three use 1 strongly disagree to 7 strongly agree. Recovery may be not applicable. These are custom items.**

| Item | H | E |
|---|---|---|
| I can tell which object the system is targeting. | __ | __ |
| I am confident the system will edit the right target. | __ | __ |
| After an error, I know how to recover. | __/NA | __/NA |
| Current discomfort, 0 none to 10 extreme; rest/stop requested? | __; ____ | __; ____ |

Stop immediately on request. Pause and remove the headset if discomfort reaches 4 or rises by 3 from baseline; this is an operational rule, not a medical diagnosis. Pause immediately for headache, eye pain, or nausea; do not insist on finishing tasks.

## 8. Form D: final evaluation and interview, headset off

**Overall usability, SUS: 1 strongly disagree to 5 strongly agree. Complete once for the whole product.** These English translations preserve the Chinese study wording; confirm against the chosen original version before use and retain all ten items.

| Item | Score |
|---|---|
| 1. I think I would like to use this system frequently. | __ |
| 2. I find the system unnecessarily complex. | __ |
| 3. I find the system easy to use. | __ |
| 4. I think I would need technical assistance to use this system. | __ |
| 5. I find the system's functions well integrated. | __ |
| 6. I find too much inconsistency in this system. | __ |
| 7. I think most people could learn to use this system quickly. | __ |
| 8. I find the system cumbersome to use. | __ |
| 9. I feel confident using this system. | __ |
| 10. I need to learn many things before I can use this system. | __ |

Preference: □ H □ E □ Depends on task □ No preference □ Insufficient experience; why: ____

Willingness to use in a real project, 1-7: __; concern about eye data, 1-7: __; concern about voice/scene information entering model services, 1-7: __. Higher means more willingness/concern respectively.

**Ask neutrally, then request examples where time allows.**

| Core question | Notes / relevant task or timestamp |
|---|---|
| When were you most unsure what would be edited? How did you judge it? | ____ |
| How could you tell it was listening, processing, done, or failed? What was unclear? | ____ |
| How did you recover from errors? What was hardest? If no error occurred, mark the answer as hypothetical | ____ |
| When would you choose H or E? What felt effortful or uncomfortable? | ____ |
| What would you actually use it for? What needs confirmation? Any data concerns? | ____ |
| If only one thing could improve first, what and why? | ____ |
| Experienced: what work/control is saved/lost? Novice: which terms or steps still need help? | ____ |

Free-exploration activities: ____ Key event/quotation: ____ Counterexample or dissent: ____

**Close:** □ Participant free to leave □ Logs linked □ No identity in logs □ Compensation □ Withdrawal deadline reminded □ Notes/quotation permission checked. Reset and disinfect before the next person.

## 9. After collection: four summaries only

1. **Tasks:** per participant, calculate H/E first success / revealed tasks, final independent success, repairs, and time; plot paired results. Keep E fallback in E. Separate timeout/withdrawal/technical failure. Give bounds for ? outcomes; unrun tasks are not failures.
2. **Questionnaires:** Raw TLX is the mean of six items, lower means less workload. SUS: odd items minus 1, even items subtracted from 5, sum ×2.5; it is not an accuracy percentage. Missing items → total NA. Report other items separately, not as an invented trust index.
3. **Interviews:** two researchers first review two sets of notes; code targeting/feedback/speech/recovery/comfort/privacy/value. Keep quotations and counterexamples; never fabricate verbatim speech without a recording.
4. **Recommendations:** rank a table of problem → number affected → task/quotation evidence → proposed change. Experience groups are exploratory; 12 people are not 144 independent observations. Do not promise eye superiority or thematic saturation.

Sample flow: pilots __; formal consented __; eye tracking usable __; paired completion __; interview-only/withdrawn __; unique total __, maximum 15.

Definitions, scale references, and analysis limits: [full plan](GazeVoice_Formal_Evaluation_Plan.md). Collection preparation: [objective quickstart](GazeVoice_Objective_Evaluation_Quickstart.md). Printable forms: [PDF set](print/).

## Printable English materials

Print A4 portrait, single-sided, at 100% actual size. Test one copy before batch printing. For 14 planned people plus one spare set, the quantities below use **133 sheets**.

| Material | Pages per copy | Copies | Use |
|---|---:|---:|---|
| [Moderator handbook](print/01_Moderator_Handbook_EN.pdf) | 2 | 2 | One for moderator, one for observer |
| [Participant quick guide](print/02_Participant_Quick_Guide_EN.pdf) | 1 | 2 | Shared during teaching |
| [Information and consent](print/03_Information_and_Consent_EN.pdf) | 2 | 15 | Participant keeps page 1; retain signed page 2 separately |
| [Personal study packet](print/04_Personal_Study_Packet_EN.pdf) | 6 | 15 | Clip one per participant; hand out pages 3, 4 and 5 at the appropriate time |
| [Moderator task cards](print/05_Task_Cards_EN.pdf) | 7 | 1 | Cut on center lines: 12 formal cards plus 2 practice cards |

Before use, complete actual services, data destinations, contacts, compensation and retention fields. Bind cards to real scenes/target keys and prepare matching in-headset prompts; cards do not implement scenes or study instrumentation. Do not show moderator answers to participants. If school rules require a signed-page copy for participants, add 15 sheets of consent page 2. Spare forms do not increase the overall participant cap.
