# GazeVoice: 30-Minute User Study Field Guide

Version 2.0 | 2026-11-24 | Quest Pro real eyes + voice | One product mode, three tasks

**Use this in order.** Two pilots + 12 formal participants (six Unity-experienced, six novices); at most 15 unique people including withdrawals/interview-only. This is a product trial, without an H/E comparison. Engineering evaluation is separate.

## 1. Before the session

- [ ] Send information/screening ahead; book 40 minutes per person, including 10 for reset/cleaning. Pilot and freeze before formal collection.
- [ ] Fill consent fields: contacts, actual providers/data destinations, retention, compensation and authorization.
- [ ] Back up a study-only project; fix build/scene/model/language; map P0 and T1-T3 to real targets and in-headset reference prompts.
- [ ] Verify Link, real eyes, B/Y, feedback, Undo, reset, cancellation and independent target/non-target checks. Prepare logs/timer/pens.
- [ ] Keep participant identity/consent separate; no raw voice/eye data in Git. Clear cables, disinfect and prefer sitting.

## 2. Follow the clock

| Minutes | Do | Paper |
|---|---|---|
| 0-3 | Confirm adult eligibility, consent and baseline; do not rush questions | Consent + packet p1 |
| 3-10 | Fit/calibrate, independent nine-point eye check, one practice exercise | Quick guide, P0; packet p1 |
| 10-18 | T1 height -> T2 rotation -> T3 red; <=90 seconds each including waiting | Cards; packet p1 |
| 18-22 | Headset off; ten-item SUS, workload, discomfort | Packet p2 |
| 22-28 | Ask the three questions; optional one short experience probe | Packet p3 |
| 28-30 | Confirm notes, compensate, withdrawal reminder | Packet p3 |

Safety and consent take priority. At most two calibration attempts in setup. If real eyes are not verified by minute 10, record unavailable and offer demo/interview only; do not score head-only tasks as eye-first performance. Mark incomplete/overrun honestly. Stop on request; remove headset for nausea, eye pain, headache, discomfort >=4 or rise >=3 from baseline. Rest whenever needed.

## 3. Read the one-minute instruction

> We are testing the prototype, not your ability. Find the indicated object and look at it naturally. Hold right B or left Y while saying the first command, release, then wait for feedback. If needed, retry or say 'Undo the last edit.' Wait for the previous command to finish. Each task lasts up to 90 seconds. We will discuss your thoughts afterward. Tell us whenever you need help, rest or wish to stop.

Explain Y as vertical; demonstrate using a non-test object. Practice P0: 'Make this blue.' If time permits, demonstrate Undo. Practice is not scored.

## 4. Run only these three tasks

| ID | First command | Researcher check |
|---|---|---|
| T1 | Make this twice as tall along the Y axis. | Target Y scale x2, X/Z unchanged; <=2% error |
| T2 | Rotate this 30 degrees around the world Y axis. | Target world Y +30 degrees; <=2 degrees error |
| T3 | Make this red. | Independent target material; <=0.02/channel in common color space |

All require the correct target and no unintended non-target changes. Keep task order, scene and language fixed. Do not show researcher answers or send ground truth to the model.

**Each task:** reset scene/Selection/queue/context -> verify source -> reveal/start timer -> preserve first outcome -> allow repair -> stop at verified success or 90 seconds -> cancel/check late commands -> save before reset. Extra help is rescue; do not coach target location.

Packet p1: first/final independent success (1/0/?), seconds/status, repairs, rescue, actual fallback and log ID. '?' is unknown; NA is not run. First success: correct from first command, no extra input/rescue/non-target edit. Final: user repairs allowed, no manual rescue. Record rescued completion separately. Timeouts are '90/timeout', not successful completion times. A model 'done' is insufficient. Log automatic retries separately.

## 5. Survey, interview and finish

- Packet p2: all ten SUS items plus two custom 0-10 ratings. No NASA-TLX, H/E preference or separate condition forms. Do not guide answers.
- Packet p3: hardest/uncertain moment; feedback clarity; real use and one improvement. Ask for actual examples, about two minutes each. Keep the optional experience probe within six minutes.
- If recording is declined, take notes; do not invent verbatim quotations. Check quote permission, log linkage and missing stages. Compensate, remind withdrawal deadline, reset and disinfect.
- Summary: per-person/task success and errors; separate timeouts, unknowns, fallback and unrun tasks; SUS/workload/discomfort; issue -> affected participants -> evidence -> proposed fix. No causal experience effect or eye/head advantage. See [plan and scoring](GazeVoice_Formal_Evaluation_Plan.md).

## 6. Print checklist

A4 portrait, single-sided, 100% actual size. Choose one language per participant. For 14 planned people plus one spare packet: **83 sheets per language** with the quantities below. A school-required extra signed-consent copy adds 15 sheets. Spare paper does not increase the participant cap.

| Material | Pages | Copies | Use |
|---|---:|---:|---|
| [Moderator handbook](print/01_Moderator_Handbook_EN.pdf) | 2 | 2 | Moderator and observer |
| [Participant quick guide](print/02_Participant_Quick_Guide_EN.pdf) | 1 | 2 | Shared during teaching |
| [Information and consent](print/03_Information_and_Consent_EN.pdf) | 2 | 15 | Give p1 to participant; store signed p2 separately |
| [Personal study packet](print/04_Personal_Study_Packet_EN.pdf) | 3 | 15 | p1 observer, p2 participant, p3 interviewer |
| [Task cards](print/05_Task_Cards_EN.pdf) | 2 | 1 | Cut into P0 practice + T1/T2/T3 cards; researcher only |

Blank provider/contact/target fields must be completed before use. Paper cards do not implement scenes or study instrumentation. Collection requirements: [objective quickstart](GazeVoice_Objective_Evaluation_Quickstart.md).
