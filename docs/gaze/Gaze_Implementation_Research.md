# Research Report: Gaze Implementation for GazeVoice

**Project:** GazeVoice: Voice- and Gaze-Assisted 3D Editing
**Date:** 2026-11-06
**Scope:** Shared implementation guidance for Blender and Unity Gaze. This report focuses on future Gaze integration and does not change the current Unity Voice work.

## Executive Summary

Implement Gaze as an independent input channel for gaze estimation and scene hit testing, rather than coupling camera capture, gaze estimation, and object selection in either host. Both hosts should eventually provide VoiceService with the same focus data contract: a focus sample with a monotonic timestamp, normalized viewport coordinates, confidence, and scene-hit candidates. Each host adapter maps viewport rays into its own scene and returns stable object IDs and display names.

Treat gaze as uncertain evidence of intent. For a command such as “make this bigger,” combine gaze targets around the relevant spoken words with object-name clues. If the target remains ambiguous, mark a small set of candidates in the viewport and ask the user to clarify instead of modifying an arbitrary object. Public research shows that low-cost webcam eye tracking can support some interaction tasks, but accuracy depends on the user, lighting, camera placement, and display geometry; published results are not a guarantee of object-level selection accuracy in this project [1][2][3].

## 1. Project Status and Goal

The project proposal describes Gaze as a second context channel for Voice: estimate gaze from a local webcam, calibrate and smooth it, detect fixations, then combine ray-hit objects with word-level speech timestamps. When candidate selection is uncertain, show numbered candidates, ask for confirmation, and support undo and grounding evaluation [P].

The repository currently uses selection as the focus context. `VoiceService` reads bridge `selection` after the PTT speech segment ends and stores it in the command record; it also retains speech event and timing information, but has no continuous gaze input [4]. The Blender add-on uses `bpy.app.timers` to process socket requests and access the scene on Blender's main thread [5]. The Unity bridge uses `EditorApplication.update` and `Pump()` to handle queued requests on Unity's Editor main thread [6]. The host integrations differ, but both can keep viewport-to-scene hit testing inside the host adapter.

Unity SceneView APIs provide ways to create a world ray from a GUI point, pick a GameObject, and draw UI from a SceneView GUI callback. Blender provides APIs to create a view ray from region coordinates and to call `Scene.ray_cast` [7][8][9][10][17]. Calls must run in a valid SceneView/VIEW_3D context and account for multiple viewports, view changes, and editing modes.

## 2. Research Findings

### 2.1 Webcam Eye Tracking Is an Input, Not a Precise Pointer

Eye-tracking interaction research shows that gaze can support fast pointing and contextual selection, but looking at an object does not necessarily mean the user intends to select it. Users also look at objects while reading, comparing, and searching. The Midas Touch problem motivates combining gaze with an explicit action, such as speech, a key press, or dwell, rather than triggering an edit on every fixation [2].

A webcam eye-tracking system evaluated under specific experimental conditions reported mean accuracy of about 1.4 degrees and precision of about 1.1 degrees. This shows what that system achieved under those conditions; it is not a guarantee for MediaPipe and does not establish 3D scene-object selection accuracy for this project [1]. MediaPipe Face Landmarker can return face landmarks and blendshape results and may be used for local face/eye feature extraction. It is not, by itself, a calibrated gaze-to-screen model [3].

**Implication:** First test whether the target hardware and user environment can reliably distinguish adjacent objects in the viewport before enabling automatic object grounding. Camera processing should remain local, camera start/stop state should be visible, and raw video should not be retained. Store calibration data and output locally only when the implementation needs them.

### 2.2 Calibration, Filtering, and Fixation Events

The proposal suggests nine-point calibration, a 1€ Filter, and I-DT fixation detection [P]. This is a reasonable starting design: nine points cover the screen; the 1€ Filter reduces jitter at low speed while limiting lag during fast motion; and I-DT detects fixations from spatial dispersion in a sliding window [11][12]. Calibration-point count, filter parameters, minimum fixation duration, and dispersion threshold must still be measured for the camera, display, and editing task. A “0.5 second dwell” or any other fixed threshold should not be treated as universal.

A fixation event should ideally include its start and end, mean coordinates, dispersion, and confidence, while preserving the timestamps of the underlying samples. Low confidence should reduce automation rather than force a single filtered point to select an object.

### 2.3 Fusing Speech and Gaze

Gaze, gesture, and speech are temporally coordinated in conversation. Language understanding can use nonverbal pointing cues before or after speech, but the timing relationship depends on the task and utterance [13]. “Look-before-naming” is therefore a hypothesis to evaluate in this project, not a fixed rule. Research systems also explore combining gaze, speech, and scene content to identify targets in 3D environments, but their results do not establish performance for this Unity/Blender integration [14][15].

For each pronoun or spatial demonstrative, build candidate targets from a time window around the relevant words. Consider word-level timestamps, fixation intervals, target persistence, ray-hit confidence, and scene visibility. Compare at least three strategies: focus at utterance end, focus near the demonstrative, and fusion across the full utterance plus object-name clues. Determine window length and weights from project data.

### 2.4 Numbered Markers and Clarification

Set-of-Mark prompting overlays numbers on image candidates so a model can refer to regions by number. It supports the interaction idea of explicitly addressable candidates, but the paper concerns image question-answering and vision-language models; it does not show that the same marking method improves selection accuracy in a 3D editor [16]. For this project, briefly mark the top-K scene-hit candidates in the viewport and let the user confirm by speech or hotkey. Deduplicate candidates, rank by visible foreground hit and gaze confidence, and invalidate stale candidates after scene or view changes.

Candidate count K, label duration, and clarification threshold are measurable parameters. Start with a small candidate set and conservative thresholds. Do not automatically raycast through foreground geometry to arbitrary distant objects when the target is occluded or the ray misses. Commands that modify the scene, such as “move this,” should ask for confirmation when confidence is low.

## 3. Recommended Cross-Host Architecture

```text
Webcam
  -> local face/eye landmarks
  -> calibrated screen/viewport coordinates + confidence + monotonic timestamp
  -> gaze filter / fixation detector
  -> host adapter: viewport ray, visibility, and scene hits
  -> focus candidates (stable object ID, name, score, time interval)
  -> Voice grounding: word timestamps + fixations/candidates + utterance
  -> direct target / clarify / no target
```

The shared data object should include at least:

| Field | Meaning | Constraint |
|---|---|---|
| `session_id` | Current camera calibration/tracking session | Change when the session restarts |
| `t_monotonic` | Monotonic sample time in seconds | Do not use wall-clock time directly; processes must share a clock domain or provide a mapping |
| `viewport_id` | Active viewport identifier | Handles multiple SceneViews/VIEW_3D areas and focus changes |
| `uv` | Normalized viewport coordinate `(0..1, 0..1)` | Define origin direction, viewport bounds, and letterboxing behavior |
| `confidence` | Gaze-estimation confidence | Keep separate from scene-hit confidence |
| `fixation` | Fixation interval and summary statistics | Optional; retain timestamps for raw gaze samples |
| `hits[]` | Host-returned scene candidate objects | Stable ID, name, depth/hit distance, and score |

Normalize coordinates, but do not require Blender and Unity to share a Gaze model yet; model parity can be addressed later. Object IDs should not rely on names, which can change or be duplicated. Blender could use an object UUID/custom property; Unity could use a GlobalObjectId or stable scene identifier, with a readable name alongside it. Invalidate IDs after a scene change or object reload and run grounding again.

Current PTT bridge events use epoch-style `t` values, and microphone ring-buffer positions must correspond to those event times [4][6]. A gaze stream using `time.monotonic()` independently cannot be compared directly with PTT or word timestamps. Define a shared clock protocol before fusion: preferably timestamp gaze and audio events on the application coordinator's monotonic clock; if capture-time reconstruction is required, estimate clock offset/drift during session setup and retain both original and mapped times. Test recording pre-roll, transport delay, dropped samples, and a busy Editor before enabling timing-based fusion.

## 4. Blender and Unity Implementation Mapping

| Capability | Blender | Unity Editor | Shared behavior |
|---|---|---|---|
| Viewport ray | Use `bpy_extras.view3d_utils` with the active `VIEW_3D` area/region coordinates [7] | In SceneView, create a ray from GUI coordinates with `HandleUtility.GUIPointToWorldRay` [9] | Accept normalized viewport coordinates; account for pixel/GUI coordinates and Y-axis direction |
| Scene hit | Use `Scene.ray_cast` with the depsgraph and active-scene filters [8] | `HandleUtility.PickGameObject` can pick Editor objects; evaluate explicit physics/editor ray approaches if distance, hierarchy, and multi-hit behavior need tighter control [10] | Return visible foreground hits, stable IDs, names, and distances; do not penetrate occluders by default |
| Viewport lifecycle | Manage the VIEW_3D area/region and redraw state | Use `SceneView.duringSceneGui` for SceneView GUI/drawing; handle multiple views and callback lifetime [17] | Resolve only against a valid active viewport; recompute after view switches |
| Candidate markers | Viewport draw handler / HUD | SceneView GUI handles or overlay | Keep numbering, color meaning, timeout, and confirmation behavior consistent |
| Edit and undo | Existing bridge main-thread/timer and Blender undo operations | Existing bridge `Pump()` and `Undo` APIs [6][18] | Gaze only provides grounding; scene edits remain within the existing one-command undo boundary |

Raycast API semantics, coordinate spaces, and pick filters differ between Blender and Unity. Share the focus-candidate protocol and behavioral acceptance tests rather than requiring identical internal APIs. Unity's `PickGameObject` is an Editor API and is not equivalent to a runtime physics raycast; test in the actual SceneView workflow.

## 5. Phased Implementation and Evaluation

1. **Define the protocol and clock.** Without a camera, add a shared `FocusCandidate` structure to selection-based focus with session, monotonic time, viewport, stable ID, name, and confidence. Implement bridge queries/events and log replay.
2. **Implement host ray adapters.** Add viewport ray hits and visible candidate lists independently in Unity and Blender. Test known geometry, occluders, multiple viewports, and coordinate boundaries. Align both hosts with shared behavioral cases.
3. **Validate gaze offline.** Add local camera landmarks and calibration, but initially only record coordinates. Measure nine-point calibration, drift/recalibration, varied lighting and natural head motion, angular error, target hit rate, drop rate, and latency.
4. **Run speech grounding in shadow mode.** Record the suggested target but continue executing against explicit selection. Compare utterance-end focus, focus near demonstrative words, and full-utterance fusion; label correct targets and ambiguous cases.
5. **Enable edits after confirmation.** Initially ground automatically only when confidence is sufficient and there is one unique candidate. Otherwise show numbered candidates and wait for confirmation. Preserve one-step Undo and record grounding source and score for review.

Evaluate object-level grounding accuracy, Top-3 accuracy, ambiguity/refusal rate, end-to-end grounding latency, wrong-edit rate, user corrections, undo rate, calibration time, and recalibration frequency. For automatic selection, wrong-edit rate and recoverability are more relevant than pixel error alone. User studies should cover demonstrative plus explicit object name, demonstrative alone, occluded/adjacent objects, and gaze that scans without intent to select; compare with a selection-only baseline.

## 6. Risks and Hypotheses to Validate

- **Uncertain camera estimates:** landmark output is not itself a gaze point; user and environment differences affect accuracy [1][3].
- **Midas Touch:** looking at an object does not mean the user wants it changed; constrain gaze with speech and explicit confirmation [2].
- **Timing alignment:** sampling, audio pre-roll, word timestamps, network polling, and host updates occur at different stages; a shared clock is a prerequisite for fusion.
- **Object identity:** names may be duplicated or changed; use stable IDs and invalidate them after object deletion, reload, or scene changes.
- **Occlusion and depth:** a 2D gaze ray can correspond to multiple possible targets; prefer visible foreground hits, but clarify when the intended target may be occluded.
- **Cross-host consistency:** hidden objects, edit modes, overlays, hierarchy, and scene filters can make host hit results diverge. Use shared test scenes and behavioral assertions.
- **Overgeneralizing research metrics:** webcam and gaze-plus-speech results from specific experiments do not represent this project's hardware, model, editor views, or users [1][14][15].

The following are **design hypotheses to validate**, not established results: nine-point calibration is sufficient on the target device; 1€ Filter parameters achieve an acceptable stability/latency tradeoff; 0.5 second dwell is appropriate; gaze before a spoken target is more accurate than utterance-end selection; numbered Top-5 markers reduce ambiguity; and one gaze model can meet acceptable performance in both Blender and Unity Editor views.

## References

### Project materials

- **[P]** GazeVoice project proposal, `Gaze_Voice_3D_Editing_Proposal.pdf`, supplied locally and not included in this repository.
- **[4]** GazeVoice `VoiceService`: PTT handling and command focus recording. [Source](../../gazevoice/voice/service.py)
- **[5]** Blender GazeVoice bridge: timer polling and main-thread scene access. [Source](../../blender_addon/gazevoice_bridge/__init__.py)
- **[6]** Unity GazeVoice bridge: `EditorApplication.update`, request queue, and PTT time. [Source on the Unity Voice branch](https://github.com/GhanibhutiGogoi/Gaze-Voice-3D-Editing/blob/feature/unity-voice/unity_package/Editor/GazeVoiceBridge.cs)

### Papers and technical references

- **[1]** Kaduk, F. et al. “Webcam eye tracking close to laboratory standards.” *Behavior Research Methods* (2024). [DOI: 10.3758/s13428-023-02237-8](https://doi.org/10.3758/s13428-023-02237-8)
- **[2]** Jacob, R. J. K. “What You Look At Is What You Get: Eye Movement-Based Interaction Techniques.” *CHI* (1990). [DOI: 10.1145/97243.97246](https://doi.org/10.1145/97243.97246)
- **[3]** Google AI Edge. “Face Landmarker for Python.” [Official documentation](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python)
- **[7]** Blender API. `bpy_extras.view3d_utils`. [Official documentation](https://docs.blender.org/api/current/bpy_extras.view3d_utils.html)
- **[8]** Blender API. `Scene.ray_cast`. [Official documentation](https://docs.blender.org/api/current/bpy.types.Scene.html#bpy.types.Scene.ray_cast)
- **[9]** Unity 2022.3 API. `HandleUtility.GUIPointToWorldRay`. [Official documentation](https://docs.unity3d.com/2022.3/Documentation/ScriptReference/HandleUtility.GUIPointToWorldRay.html)
- **[10]** Unity 2022.3 API. `HandleUtility.PickGameObject`. [Official documentation](https://docs.unity3d.com/2022.3/Documentation/ScriptReference/HandleUtility.PickGameObject.html)
- **[11]** Casiez, G., Roussel, N., and Vogel, D. “1€ Filter: A Simple Speed-Based Low-Pass Filter for Noisy Input in Interactive Systems.” *CHI* (2012). [DOI: 10.1145/2207676.2208639](https://doi.org/10.1145/2207676.2208639)
- **[12]** Salvucci, D. D. and Goldberg, J. H. “Identifying Fixations and Saccades in Eye-Tracking Protocols.” *ETRA* (2000). [DOI: 10.1145/355017.355028](https://doi.org/10.1145/355017.355028)
- **[13]** Griffin, Z. M. and Bock, K. “What the Eyes Say About Speaking.” *Psychological Science* (2000). [DOI: 10.1111/1467-9280.00255](https://doi.org/10.1111/1467-9280.00255)
- **[14]** “GazePointAR: A Gaze-Based Augmented Reality Authoring Tool.” *CHI* (2024). [DOI: 10.1145/3613904.3642230](https://doi.org/10.1145/3613904.3642230)
- **[15]** “SemanticScanpath: Combining Gaze and Speech for Situated Human-Robot Interaction Using LLMs.” (2025 preprint; 2026 RO-MAN publication record). [arXiv:2503.16548](https://arxiv.org/abs/2503.16548) · [DOI: 10.1109/ro-man72286.2026.11714973](https://doi.org/10.1109/ro-man72286.2026.11714973)
- **[16]** Yang, J. et al. “Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding in GPT-4V.” (2023). [arXiv:2310.11441](https://arxiv.org/abs/2310.11441)
- **[17]** Unity 2022.3 API. `SceneView.duringSceneGui`. [Official documentation](https://docs.unity3d.com/2022.3/Documentation/ScriptReference/SceneView-duringSceneGui.html)
- **[18]** Unity 2022.3 API. `Undo`. [Official documentation](https://docs.unity3d.com/2022.3/Documentation/ScriptReference/Undo.html)
