# Unity Gaze: Implementation and Validation

Date: 2026-11-09

The Unity Editor bridge now accepts the shared Gaze grounding flow. Mouse focus is the primary deterministic test input; the camera provider is shared with Blender and remains subject to webcam accuracy limits. Quest/XR runtime eye tracking is a future provider.

## Behavior

- Routing exposes Off, Watch only and On for Unity, with Mouse or Camera sources.
- SceneView mouse samples carry the viewport/session revision and epoch timestamps, matching the existing Voice/audio clock. The integration does not introduce the monotonic protocol proposed in the earlier solution report.
- A transparent indicator follows focus; labels show the hovered object's name. Candidate numbers can be confirmed by spoken number or SceneView number key.
- Spoken pointing words use existing timestamp windows, fixations and multi-ray candidate scoring. Explicit object names retain priority through the prompts.
- On an empty-space "here" or "there" fixation, the bridge intersects the forward SceneView ray with Unity's Y=0 work plane. A new-object request such as “Make a sofa here” may use this ground-contact coordinate without selection; ordinary object edits still require a target. Blender uses the matching Z=0 convention. Parallel rays, rearward intersections and stale view tokens give no valid placement.
- Unity target instance IDs are sent to both refiner and editor. Watch-only comparison uses selected object IDs, so duplicate names do not falsely count as a match.
- Camera UV is converted with the SceneView GUI screen rectangle. Mouse UV is viewport-local. The same current view revision is required for picking, clarification and validation before execution.
- A scene/view change invalidates queued targets. An unavailable target uses selection; a pointing command with no valid gaze and no selection is refused. Voice without gaze remains available.
- Geometry is picked on the CPU in a SceneView Layout callback. Readable MeshFilter and baked SkinnedMeshRenderer surfaces do not require colliders. Unsupported geometry conservatively occludes based on bounds; no exact point is claimed for it. Mesh parts, Terrain surfaces and optional viewport-picture descriptions are not implemented for Unity.

## Automated checks

The placement extension was checked with deterministic Python replays: empty-space creation without selection, an adjacent object at the edge of the fixation patch, a non-intersecting work plane, an unpickable occluder and a changed SceneView token before editing. Unity 2022.3.62f3c1 compiled the package in a disposable project. The running Unity project was updated to Bridge 0.3.1 and answered a read-only gaze_ground query, an empty-ID gaze_validate query, and a stale-token rejection; the manual spoken placement check below remains.

Python tests replay pointer samples through VoiceService and cover resolved targets, Watch only, stale view data, missing view/selection, object deletion after refinement, duplicate names, camera screen conversion, poll configuration and view-scoped number-key confirmation. Existing Blender gaze tests remain part of the regression suite.

The earlier Unity Gaze baseline regression ran 1,626 tests successfully (3 skipped). Run the current full suite with `python3 -m unittest discover -s tests -t . -q`; its latest result is reported separately from this historical baseline.

The C# package was compiled and loaded in Unity 2022.3.62f3c1 in a disposable project. A live socket test used two overlapping Cube objects with identical names and no colliders, with the rear Cube selected. The front Cube was hit, scaled through the actual Unity tool bridge, and restored with one Undo; the rear selection was untouched. Background rays missed, and a changed SceneView rejected the old revision token.

This live test used synthetic audio and deterministic speech/refiner/editor substitutes. It validates the real Unity bridge, scene hit testing, focus handoff, tool execution and Undo, not microphone recognition or LLM interpretation. A separate Unity mesh replay checked transformed meshes, including non-uniform and negative scale, plus a miss. Real-camera accuracy and XR input are not validated by these tests.

The indicator protocol accepted a simulated camera point and two numbered candidates in the live Editor. Visual alignment, actual pointer movement and keyboard interaction remain manual checks: macOS screen capture failed during verification, so these are not claimed as visually verified. The test project is disposable; the user's Unity project was not modified.

## Manual regression checklist

1. Update the package in a test Unity project. Start Voice with Unity / Watch only / Mouse.
2. Hover at the center and edges of a target, then over empty space. Check that indicator, label, command target and resulting change refer to the same place. The indicator must not alter selection.
3. Move the pointer into Hierarchy, Inspector or another app; verify that mouse focus disappears. Stop Voice and verify overlays disappear.
4. Resize/move the Unity window, change display scaling, orbit/zoom the view, and switch between two SceneViews. Verify invalidation and correct new coordinates. Old commands must never use a new view with old UV.
5. Use adjacent objects, same-name objects, foreground occlusion and objects without colliders. Test hidden/picking-disabled objects, mesh deletion, and unsupported/non-readable geometry.
6. In On mode say “make this taller” over an unselected object; inspect the stored focus ID and Undo. Check explicit object names still take priority.
7. Trigger ambiguous candidates, verify numbered labels, answer by speech and by number key, then test timeout/cancel. Confirm stale-view number input is ignored.
8. Test gaze loss with and without selection. Ensure selection fallback is explained and no-target edits are refused.
9. If camera testing is needed, calibrate on the display containing Unity, use Watch only, compare marker position to intended targets and record actual object-level hit rate. Do not infer eye-tracking accuracy from mouse-mode success.
10. Recheck Blender Voice/Gaze and Unity Voice with Gaze Off.
11. In On/Mouse mode, deselect all objects and aim at a clear patch of Scene space with a downward-looking view. Say “Make a sofa here”; inspect the command's gaze miss for a Y=0 point and verify the sofa's ground contact matches it. Try a horizontal/upward view, “Make this yellow here” without a selection, and a changed SceneView during refinement: none should place a new object.
