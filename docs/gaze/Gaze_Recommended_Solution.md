# Recommended Gaze Solution for GazeVoice

**Date:** 2026-11-06
**Status:** Built for Blender on the branch `feature/gaze-blender`. Unity is not built. The real camera and voice checks are still to do.
**Scope:** Gaze grounding for Unity and Blender. This proposal does not replace current Unity Voice work and does not select a final gaze model.

## 1. Goals and Design Decisions

Define Gaze as a source of focus context for voice commands. Gaze answers “which scene object might the user mean?” Voice interprets “what should be done to it?” The Unity/Blender adapter maps editor viewport coordinates to scene objects. Gaze itself does not execute edits and is not equivalent to explicit selection.

Recommended design decisions:

1. **Separate tracking, scene hit testing, and speech grounding.** The camera model returns gaze points; the host bridge maps them to scene candidates; the Voice layer combines candidates with speech timing and object names.
2. **Standardize a cross-host protocol, then implement host-specific adapters.** Unity and Blender may use different APIs while sharing focus data, refusal/clarification semantics, and acceptance behavior [R §§3-4].
3. **Keep the gaze provider replaceable.** Validate the protocol and raycast with replay data or simulated screen coordinates before connecting a camera. Treat a shared Blender/Unity model as a later decision; stabilize the provider interface first.
4. **Clarify or fall back when confidence is low.** Gaze is probabilistic evidence. Ask rather than silently edit the wrong object [R §§2.1, 2.4].
5. **Keep edits in the existing Voice workflow.** Gaze only supplies grounding. Command execution, logs, and Undo continue through the existing host bridge and one-command undo boundary.

## 2. User-Visible Behavior

### Normal flow

1. The user starts Gaze tracking and completes screen calibration.
2. The system estimates gaze in the active Unity SceneView or Blender 3D View, maps the point to a viewport ray, and returns scene candidates.
3. The user holds PTT and speaks an editing command.
4. Voice passes the transcript, word timestamps, and recent gaze/fixation history to grounding.
5. If grounding returns one sufficiently trusted target, that object becomes the command focus. Otherwise the system shows a small set of candidates for confirmation or falls back to explicit selection.
6. The editor runs the command and uses the existing completion/error status and Undo behavior.

### Target selection rules

| Utterance/input | Target source |
|---|---|
| The user names one unique object | Prefer the name-resolution result; use gaze as a consistency signal |
| “This/it/here” and one trusted candidate near the relevant words | Gaze-grounded target |
| A demonstrative has several similar candidates | Show numbered candidates and wait for confirmation |
| No demonstrative, but one explicit selection exists | Use selection for compatibility with current behavior |
| Gaze confidence is low, tracking is off, or no valid viewport exists | Do not infer; request an object name or use existing selection |
| Gaze conflicts with an object name or selection | Do not automatically run a targeted edit; explain the conflict and clarify |

Set thresholds, candidate count, and time windows from shadow-mode data rather than hard-coding them at design time [R §§2.3, 6].

## 3. Recommended Architecture

```text
Camera / replay provider
  -> GazeProvider (landmarks or calibrated screen point)
  -> GazeCoordinator (session clock, calibration, filter, fixation)
  -> host bridge: active viewport + raycast + visible scene candidates
  -> FocusCandidate history
  -> Voice grounding: words + audio interval + candidate history + explicit selection
  -> target / clarify / fallback / no-target
  -> existing editor command and Undo flow
```

### Module boundaries

| Module | Responsibility | Does not |
|---|---|---|
| `GazeProvider` | Produces screen/viewport gaze coordinates, sample time, and estimator confidence; supports camera and replay/mock implementations | Read Blender/Unity scenes or decide which object to edit |
| `GazeCoordinator` (Python app) | Manages tracking session, calibration state, timeline, filtering/fixation events, and short-term history; serves queries from Voice | Access host scene objects directly |
| Unity/Blender host adapter | Provides active viewport, converts normalized coordinates to a host ray, returns scene hits and stable IDs, draws candidates, handles confirmation | Run the camera model or interpret natural language |
| `VoiceGrounder` | Uses utterance type, word times, candidate history, object names, and selection to resolve or clarify a target | Force a target when confidence is low |
| Editor command pipeline | Executes with the grounded target, logs the command, and supports Undo | Reinterpret gaze coordinates |

Give Gaze explicit lifecycle states: `off -> calibrating -> tracking -> degraded/error`. If the camera, calibration, or SceneView is unavailable, degrade gracefully and continue the existing Voice-plus-selection workflow.

## 4. Cross-Host Data Contract

An initial protocol version could be `gaze.v1`. The fields follow the FocusCandidate recommendation in the research report [R §3].

### Gaze sample

```json
{
  "schema": "gaze.v1",
  "session_id": "session-uuid",
  "seq": 1842,
  "t_session_ms": 15340.5,
  "viewport": {
    "host": "unity",
    "id": "scene-view-id",
    "uv": [0.42, 0.61]
  },
  "gaze": {
    "confidence": 0.78,
    "valid": true
  },
  "fixation": {
    "start_ms": 15120.0,
    "end_ms": 15340.5,
    "dispersion": 0.018
  }
}
```

### Host-resolved focus snapshot

```json
{
  "schema": "gaze.v1",
  "session_id": "session-uuid",
  "seq": 1842,
  "t_session_ms": 15340.5,
  "viewport_id": "scene-view-id",
  "hits": [
    {
      "object_id": "stable-host-object-id",
      "name": "BodyPanel",
      "distance": 4.28,
      "visible_hit": true,
      "hit_confidence": 0.91
    }
  ]
}
```

### Contract constraints

- `t_session_ms` uses one session-relative monotonic timeline established by the Python coordinator. Map PTT, audio intervals, and gaze records to it. Do not mix Unity `DateTime` epoch values, Python `time.monotonic()`, and camera clocks directly [R §3].
- Preserve word timestamps relative to the audio clip, then map them to the shared timeline using the clip's session start time. Record PTT down/up, audio pre-roll, capture delay, and the mapping.
- Define `uv` with origin at the viewport's upper-left and values in `[0,1]`. The host adapter handles Y-axis conversion, window scaling, and content-area clipping.
- `confidence` is gaze-estimator confidence; `hit_confidence` is raycast/hit confidence. Keep them separate and interpretable.
- Do not use names as `object_id`. Names support UI and language resolution. Invalidate candidates immediately after object deletion, scene switch, reload, or ID invalidation.
- Deduplicate by `session_id + seq`. Dropped samples are acceptable; out-of-order samples must not overwrite newer focus.
- Record `source` (`gaze`, `selection`, `spoken_name`, `confirmed_candidate`) and a grounding reason code for inspection and evaluation.

## 5. Host Interfaces

Existing bridges keep scene access on the host main thread. Preserve this boundary: the Python side submits the latest gaze snapshot or makes low-frequency batched queries; Unity `Pump()` and the Blender timer read the active viewport, raycast, and return hits on the host main thread. Do not access Editor APIs from the camera sampling thread [R §§1, 4].

Suggested logical bridge capabilities (command names can follow each bridge's conventions):

| Capability | Purpose |
|---|---|
| `gaze_capabilities` | Return supported schema, viewport, raycast, candidate overlay, and confirmation capabilities |
| `gaze_focus` | Submit the latest gaze coordinates or query the current focus snapshot |
| `gaze_candidates` | Return visible ranked candidates for a sample/time point |
| `gaze_overlay` | Show or clear temporary numbered candidates |
| `gaze_resolve` | Confirm a candidate number and return its stable object ID |
| `gaze_status` | Return tracking state, active viewport, calibration hints, and related status |

To avoid saturating sockets and Editor queues at camera sampling rates, the provider may retain its native sampling rate while host hit updates use a configurable cap. Evaluate 20-30 Hz as an initial range. Send only the latest value and drop stale queued items; Voice grounding should query recent fixation/candidate history rather than ask the host to replay every sample. Measure the final rate against latency and host load.

### Unity Editor adapter

- Raycast only in a valid SceneView and define how the active view is selected when multiple SceneViews are open.
- Convert SceneView GUI coordinates to a ray and pick scene objects using supported Editor APIs. Filter Prefab/Hierarchy and other non-scene objects, and provide stable IDs [R §4].
- Bind candidate labels and status to the relevant SceneView. Invalidate overlays/candidates when a view closes or switches, the scene changes, or view state is refreshed.
- Keep the bridge listener thread limited to network framing and queueing; execute Unity Editor API calls through `EditorApplication.update`.

### Blender adapter

- Use a valid `VIEW_3D` area/region and region coordinates to create the view ray, then raycast against the current depsgraph/scene [R §4].
- Filter objects that are not editable or visible in the active scene. Define and test behavior in Object and Edit modes.
- Bind draw-handler/HUD candidate labels to their area. Clear stale results when an area closes, the view changes, or the depsgraph changes substantially.
- Continue polling the socket from the Blender timer and keep all `bpy` scene access on Blender's main thread.

Both hosts need shared behavioral tests, not identical host APIs. For equivalent scene types, the same gaze UV should prefer the visible foreground object; occluders should not be penetrated by default; a miss should return no candidates; and old targets must not survive a viewport or scene change.

## 6. Grounding Decisions and Clarification

### Grounding inputs

```text
utterance text
word timestamps and audio interval
focus candidates near relevant words and utterance bounds
scene object vocabulary and stable IDs
current explicit selection
tracking/calibration/viewport validity
```

### Decision output

```text
status: resolved | clarify | fallback_selection | no_target
target: stable object ID and display name, optional
candidates: ordered stable object IDs and labels
evidence: gaze / spoken_name / selection / confirmed_candidate
reason: concise machine-readable reason code
```

### Rules

1. If the utterance contains one uniquely resolved object name, prefer the name. Gaze may detect a conflict but must not silently rewrite an explicit target.
2. If the utterance contains “this/it/here” or a similar demonstrative, use fixation/candidate history near those words. Rank using candidate persistence, visible hit, gaze confidence, and object-name clues.
3. Directly ground only a unique candidate above the validated confidence threshold. Set the threshold after shadow-mode measurement; commands with different impact may use different thresholds.
4. If candidates are close, conflict, or the likely hit is occluded, return `clarify` and show numbered viewport candidates. Accept a spoken number or key input and clear the prompt after a short timeout.
5. After confirmation, validate `session_id`, scene version, and object ID before passing the target to Voice execution.
6. With no valid gaze, prefer explicit selection. If there is no selection either, ask the user to name an object and do not execute an ungrounded edit.

The first version should not ask the LLM to infer unstated intent from a stream of coordinates. Candidate IDs and grounding reasons may be supplied as structured context to the existing refiner/editor, but deterministic code should validate target IDs and state transitions.

## 7. State, Privacy, and Recovery

| State | Behavior |
|---|---|
| `off` | Camera is not read; Voice plus selection remains available |
| `calibrating` | Show calibration target and progress; do not allow gaze to ground edits |
| `tracking` | Emit valid coordinates/candidates; allow confidence-based grounding |
| `degraded` | Camera loss, sustained low confidence, or invalid viewport; pause gaze grounding and fall back to selection |
| `error` | Camera permission/model/bridge failure; show a concrete recovery action without blocking Voice |

- Process the camera and gaze estimates locally. Do not save raw video/images by default.
- Make tracking state visible and provide an immediate stop action. Release the camera and clear overlays when the app exits or the host disconnects.
- Keep calibration data in local application state and provide recalibration and deletion. Do not put it in committed source or cloud logs.
- By default, command logs may store coordinate summaries, candidate IDs/scores, source, and reason, but not video frames.
- Tracking failure must not leave PTT stuck or alter the existing selection workflow.

## 8. Implementation Roadmap

### Phase A: Protocol and replay, no camera

- Define the `gaze.v1` structure, clock mapping, and Python `FocusCandidate` type.
- Add replay fixtures and a mouse-position provider to validate timing order, viewport UV, and end-to-end messages.
- Wrap current selection as one `focus source` and establish an interface that supports selection and gaze together.

**Exit criteria:** Unity and Blender return equivalent candidate-priority semantics for the same replay sequence; duplicate/out-of-order/old-session data cannot contaminate current focus.

### Phase B: Host hits and overlays

- Implement Unity SceneView and Blender VIEW_3D adapters, stable object IDs, foreground hit testing, and numbered candidate overlays.
- Use fixed test scenes for empty scene, one target, adjacent targets, occlusion, hidden objects, multiple viewports, object deletion/rename, and host restart.

**Exit criteria:** Hit behavior passes shared cases; overlays can be confirmed and cleared; invalid candidates never enter command records.

### Phase C: Camera calibration and shadow mode

- Add a camera provider and calibration flow behind the local model abstraction. Record suggested gaze targets while continuing to execute against explicit selection.
- Compare utterance-end, demonstrative-nearby, and full-utterance fusion strategies with labeled targets and ambiguous examples.
- Evaluate object hit rate, Top-3 accuracy, tracking loss, sample/command latency, calibration duration, and recalibration frequency.

**Exit criteria:** Meet accuracy, latency, and failure-rate targets agreed by the team for target hardware and typical lighting. Set these thresholds before collecting the evaluation data.

### Phase D: Controlled activation

- Enable direct grounding for unique, high-confidence, non-conflicting demonstratives.
- Clarify or fall back to selection in other cases. Keep all gaze-grounded edits recoverable through existing Undo.
- Record wrong edits, user corrections, clarification rate, and undo rate; let users disable automatic grounding.

**Exit criteria:** User testing shows an acceptable wrong-edit rate, an understandable clarification flow, and no Voice-plus-selection regression when Gaze is off.

## 9. Acceptance Metrics

| Metric | Definition/purpose |
|---|---|
| Gaze availability | Fraction of operation time with valid tracking, including loss/degraded periods |
| Object grounding accuracy | Fraction of automatic targets matching user-labeled targets |
| Top-3 accuracy | Fraction of correct targets present in the first three clarification candidates |
| Wrong-edit rate | Fraction of commands that actually modify the scene because grounding was wrong; highest priority |
| Clarification rate | Fraction of commands requiring confirmation; interpret together with wrong-edit rate |
| Grounding latency | Time from PTT release to target/clarify decision |
| Recovery cost | Confirmations, Undo actions, or repeated commands needed to correct an error |
| Calibration burden | Calibration time, recalibration frequency, and performance decay after calibration |

Evaluation utterances should include explicit object names, single demonstratives, similar objects, occlusion, background misses, gaze scanning without editing intent, fast head/view movement, and selection fallback without gaze. Compare against a selection-only baseline rather than reporting screen-angle error alone [R §§5-6].

## 10. Out of Scope and Review Items

This proposal does not select a final gaze model, define how to install a shared Blender/Unity model, add cloud services, add a wake word, or support gaze-only automatic editing. Model unification remains future work; `GazeProvider` isolates current code from that decision.

Before camera integration, the team should agree on:

- Active viewport selection when multiple Unity/Blender views are open.
- Whether gaze UV refers to display coordinates or editor viewport coordinates; this proposal recommends viewport-local UV at the coordinator boundary.
- Whether high-impact commands such as deletion or large transformations need stricter confirmation thresholds.
- Minimum sample counts and launch thresholds for calibration and shadow-mode evaluation.
- Measured provider output rate, host raycast rate, and candidate retention time.

## Basis

- **[R]** [Gaze Implementation Research Report](Gaze_Implementation_Research.md), especially §2 (research findings), §3 (cross-host architecture), §4 (host API mapping), §5 (phased evaluation), and §6 (risks and hypotheses). Its bibliography links to the papers and official API documentation.
- **Project proposal:** `Gaze_Voice_3D_Editing_Proposal.pdf`, supplied locally and not included in this repository.
- **Existing design:** [GazeVoice design specification](../superpowers/specs/2026-10-18-gazevoice-design.md)
