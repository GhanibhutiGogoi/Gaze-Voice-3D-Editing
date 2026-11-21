using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.XR.Management;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.XR;
using UnityEngine.XR.OpenXR;
using UnityEngine.XR.OpenXR.Features.Interactions;

// Unity VR: Unity plays the scene to a headset, such as a Meta Quest Pro over Link. While it plays, this listens to the
// controllers' B and Y buttons for hold-to-talk, follows where the eyes look (or the head, without eye tracking), and
// shows GazeVoice's status in the headset, where the dashboard can't be seen. Each gaze sample is cast into the scene
// when it is taken, so what was looked at is still known after the head has moved on.
[InitializeOnLoad]
internal static class GazeVoiceVR
{
    internal const string TalkButtons = "B or Y";  // matches TALK_BUTTONS in gazevoice/components/unity_vr.py
    const double SampleSeconds = 1.0 / 45;
    const double KeepSeconds = 120;
    const int MaxReply = 3000;
    const float Reach = 300f;
    const double ShapeSeconds = 1;
    const int MaxTriangles = 200000;
    const double StatusSeconds = 6;
    const double EyesFresh = 3;
    const int LineChars = 44;
    const string EyeGazeExtension = "XR_EXT_eye_gaze_interaction";
    const string VoiceOff = "Voice is off. Turn it on in the GazeVoice dashboard.";

    struct Sample
    {
        public double T;
        public bool Hit, Eyes;
        public long Id, RootId;
        public string Name, Parent, Path, Root, Material;
        public int Submesh, Submeshes;
        public Vector3 Point, Normal;
    }

    sealed class Shape
    {
        public GameObject Object;
        public Renderer Renderer;
        public Mesh Mesh;
        public Vector3[] Vertices;
        public int[] Triangles;
    }

    static readonly List<Sample> Samples = new List<Sample>();
    static readonly List<InputDevice> Devices = new List<InputDevice>();
    static readonly InputFeatureUsage<Vector3> GazePosition = EyeTrackingUsages.gazePosition;
    static readonly InputFeatureUsage<Quaternion> GazeRotation = EyeTrackingUsages.gazeRotation;
    static List<Shape> shapes = new List<Shape>();
    static double lastSample, shapesAt = double.NegativeInfinity, eyesAt = double.NegativeInfinity;
    static double statusAt = double.NegativeInfinity, noticeAt = double.NegativeInfinity;
    static bool held, talking, swept;
    static readonly System.Reflection.PropertyInfo FailedToStart = typeof(UnityEngine.XR.OpenXR.Features.OpenXRFeature)
        .GetProperty("failedInitialization", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
    static readonly List<Material> Materials = new List<Material>();
    static readonly Dictionary<Mesh, int[]> SubmeshEndsOf = new Dictionary<Mesh, int[]>();
    const int MaxPathParts = 8;
    static string shownStatus = "", notice = "", hudLine, lastError;
    static GameObject hud, dot;
    static TextMesh hudText;
    static Transform hudBack;

    static GazeVoiceVR()
    {
        if (Application.isBatchMode) return;
        GazeVoiceBridge.VrRequest = Request;
        EditorApplication.update += Tick;
        EditorApplication.playModeStateChanged += OnPlayMode;
        AssemblyReloadEvents.beforeAssemblyReload += () => Hide(true);
        MatchControllers();
    }

#if GAZEVOICE_INPUTSYSTEM
    static readonly string[][] TouchControllers =
    {
        new[] { "QuestProTouchController", "Meta Quest Pro Touch Controller OpenXR" },
        new[] { "QuestTouchPlusController", "Meta Quest Touch Plus Controller OpenXR" },
    };

    static void MatchControllers()
    {
        UnityEngine.InputSystem.InputSystem.onLayoutChange += (name, change) =>
        {
            if (change != UnityEngine.InputSystem.InputControlLayoutChange.Removed && TouchControllers.Any(pair => pair[0] == name))
                MatchController(name);
        };
        var known = new HashSet<string>(UnityEngine.InputSystem.InputSystem.ListLayouts());
        foreach (var pair in TouchControllers)
            if (known.Contains(pair[0])) MatchController(pair[0]);
    }

    static void MatchController(string layout)
    {
        var product = TouchControllers.First(pair => pair[0] == layout)[1];
        try
        {
            UnityEngine.InputSystem.InputSystem.RegisterLayoutMatcher(layout, new UnityEngine.InputSystem.Layouts.InputDeviceMatcher()
                .WithInterface("^(XRInput)").WithProduct(product).WithCapability("deviceName", product));
        }
        catch (Exception e) { Debug.LogWarning("GazeVoice could not claim " + product + ": " + e.Message); }
    }
#else
    static void MatchControllers() { }
#endif

    internal static double Now => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    static void OnPlayMode(PlayModeStateChange change)
    {
        if (change == PlayModeStateChange.EnteredPlayMode || change == PlayModeStateChange.EnteredEditMode) Strays();
        if (change != PlayModeStateChange.ExitingPlayMode) return;
        if (talking) GazeVoiceBridge.PushTalkEvent("cancel");
        talking = held = false;
        Samples.Clear();
        shapes.Clear();
        SubmeshEndsOf.Clear();
        Hide(true);
    }

    static void Tick()
    {
        if (!swept) { swept = true; Strays(); }
        if (!EditorApplication.isPlaying) return;
        try
        {
            Talk();
            var head = Head();
            var now = Now;
            if (head != null && now - lastSample >= SampleSeconds)
            {
                lastSample = now;
                Look(head, now);
            }
            Show(head, now);
        }
        catch (Exception ex)
        {
            if (ex.Message != lastError) Debug.LogWarning("GazeVoice VR: " + ex.Message);
            lastError = ex.Message;
        }
    }

    // --- hold to talk ---------------------------------------------------------------------------

    static void Talk()
    {
        var pressed = false;
        InputDevices.GetDevicesWithCharacteristics(InputDeviceCharacteristics.Controller, Devices);
        foreach (var device in Devices)
            if (device.TryGetFeatureValue(CommonUsages.secondaryButton, out bool down) && down) { pressed = true; break; }
        if (pressed == held) return;
        held = pressed;
        if (pressed)
        {
            if (!GazeVoiceBridge.VoiceIsOn) { notice = VoiceOff; noticeAt = Now; return; }
            talking = true;
            GazeVoiceBridge.PushTalkEvent("down");
        }
        else if (talking)
        {
            talking = false;
            GazeVoiceBridge.PushTalkEvent("up");
        }
    }

    // --- where the person looks -----------------------------------------------------------------

    static Camera Head()
    {
        var main = Camera.main;
        if (main != null && main.isActiveAndEnabled) return main;
        return Camera.allCameras.FirstOrDefault(camera => camera.stereoTargetEye != StereoTargetEyeMask.None);
    }

    // The eyes' pose is in the same tracking space as the head, which is the head camera's parent in an XR Origin.
    static bool EyeRay(Camera head, out Ray ray)
    {
        ray = default;
        InputDevices.GetDevicesWithCharacteristics(InputDeviceCharacteristics.EyeTracking, Devices);
        foreach (var device in Devices)
        {
            if (device.TryGetFeatureValue(CommonUsages.isTracked, out bool tracked) && !tracked) continue;
            if (!(device.TryGetFeatureValue(GazePosition, out Vector3 position) && device.TryGetFeatureValue(GazeRotation, out Quaternion rotation))
                && !(device.TryGetFeatureValue(CommonUsages.devicePosition, out position)
                     && device.TryGetFeatureValue(CommonUsages.deviceRotation, out rotation))) continue;
            var space = head.transform.parent;
            var forward = (space != null ? space.rotation * rotation : rotation) * Vector3.forward;
            if (forward.sqrMagnitude < 0.5f) continue;
            ray = new Ray(space != null ? space.TransformPoint(position) : position, forward);
            return true;
        }
        return false;
    }

    static void Look(Camera head, double now)
    {
        var eyes = EyeRay(head, out var ray);
        if (eyes) eyesAt = now;
        else ray = new Ray(head.transform.position, head.transform.forward);
        Samples.Add(Cast(ray, head.transform.root, now, eyes));
        var old = 0;
        while (old < Samples.Count && now - Samples[old].T > KeepSeconds) old++;
        if (old > 0) Samples.RemoveRange(0, old);
    }

    static bool Skip(GameObject go, Transform rig)
    {
        return go.hideFlags != HideFlags.None || (rig != null && go.transform.IsChildOf(rig));
    }

    // Colliders first; meshes without one are tested triangle by triangle, like the Scene view's gaze.
    static Sample Cast(Ray ray, Transform rig, double now, bool eyes)
    {
        var sample = new Sample { T = now, Eyes = eyes, Submesh = -1 };
        var nearest = Reach;
        GameObject best = null;
        var triangle = -1;
        var hits = Physics.RaycastAll(ray, Reach, Physics.DefaultRaycastLayers, QueryTriggerInteraction.Ignore);
        foreach (var hit in hits)
        {
            var go = hit.collider.gameObject;
            if (hit.distance >= nearest || Skip(go, rig)) continue;
            nearest = hit.distance; best = go; sample.Point = hit.point; sample.Normal = hit.normal;
            triangle = hit.collider is MeshCollider ? hit.triangleIndex : -1;
        }
        var holder = best;
        if (now - shapesAt > ShapeSeconds) { shapes = Shapes(rig); shapesAt = now; }
        foreach (var shape in shapes)
        {
            if (shape.Object == null || shape.Renderer == null || !shape.Renderer.enabled || !shape.Object.activeInHierarchy) continue;
            if (!shape.Renderer.bounds.IntersectRay(ray, out float enter) || enter >= nearest) continue;
            if (shape.Triangles == null)
            {
                nearest = enter; best = shape.Object; sample.Point = ray.GetPoint(enter); sample.Normal = -ray.direction;
                triangle = -1;
                continue;
            }
            if (!TriangleHit(ray, shape, nearest, out float distance, out Vector3 point, out Vector3 normal, out int index)) continue;
            nearest = distance; best = shape.Object; sample.Point = point; sample.Normal = normal; triangle = index;
        }
        if (best != null && best == holder && best.GetComponent<Renderer>() == null)
            Within(ray, hits, rig, ref best, ref sample, ref triangle);
        if (best == null) return sample;
        sample.Hit = true;
        sample.Id = GazeVoiceBridge.IdOf(best);
        sample.Name = best.name;
        sample.Parent = best.transform.parent != null ? best.transform.parent.name : null;
        Describe(best, triangle, ref sample);
        return sample;
    }

    // Where the part sits in its object, and the material on the spot looked at: a cushion is told from the sofa it is in,
    // and one material of a mesh that has several from the rest of it.
    static void Describe(GameObject part, int triangle, ref Sample sample)
    {
        var root = part.transform.root;
        if (root != part.transform)
        {
            sample.Root = root.name;
            sample.RootId = GazeVoiceBridge.IdOf(root.gameObject);
        }
        var names = new List<string>();
        for (var at = part.transform; at != null && names.Count < MaxPathParts; at = at.parent) names.Add(at.name);
        if (names.Count == MaxPathParts && root != part.transform) names[names.Count - 1] = "\u2026";
        names.Reverse();
        sample.Path = String.Join("/", names);
        var renderer = part.GetComponent<Renderer>();
        var materials = renderer != null ? renderer.sharedMaterials : null;
        if (materials == null || materials.Length == 0) return;
        var mesh = part.GetComponent<MeshFilter>() != null ? part.GetComponent<MeshFilter>().sharedMesh : null;
        var submesh = mesh != null && triangle >= 0 ? SubmeshOf(mesh, triangle) : -1;
        sample.Submeshes = mesh != null ? mesh.subMeshCount : materials.Length;
        sample.Submesh = sample.Submeshes > 1 ? submesh : -1;
        var material = submesh >= 0 && submesh < materials.Length ? materials[submesh] : materials.Length == 1 ? materials[0] : null;
        sample.Material = material != null ? material.name : null;
    }

    // Triangles are numbered across the submeshes in order, the way mesh.triangles and a mesh collider's hits count them.
    static int SubmeshOf(Mesh mesh, int triangle)
    {
        if (!SubmeshEndsOf.TryGetValue(mesh, out var ends))
        {
            ends = new int[mesh.subMeshCount];
            var total = 0;
            for (var i = 0; i < ends.Length; i++)
            {
                if (mesh.GetTopology(i) == MeshTopology.Triangles) total += (int)mesh.GetIndexCount(i) / 3;
                ends[i] = total;
            }
            SubmeshEndsOf[mesh] = ends;
        }
        for (var i = 0; i < ends.Length; i++)
            if (triangle < ends[i]) return i;
        return -1;
    }

    // A collider on an empty parent, such as one box around a whole sofa, would hide its parts, so the nearest part hit wins:
    // looking at a cushion means the cushion.
    static void Within(Ray ray, RaycastHit[] hits, Transform rig, ref GameObject best, ref Sample sample, ref int triangle)
    {
        var holder = best.transform;
        var nearest = Reach;
        GameObject part = null;
        Vector3 point = default, normal = default;
        var found = -1;
        foreach (var hit in hits)
        {
            var go = hit.collider.gameObject;
            if (go == best || hit.distance >= nearest || Skip(go, rig) || !go.transform.IsChildOf(holder)) continue;
            nearest = hit.distance; part = go; point = hit.point; normal = hit.normal;
            found = hit.collider is MeshCollider ? hit.triangleIndex : -1;
        }
        foreach (var shape in shapes)
        {
            if (shape.Object == null || shape.Triangles == null || shape.Renderer == null || !shape.Renderer.enabled
                || !shape.Object.activeInHierarchy || !shape.Object.transform.IsChildOf(holder)) continue;
            if (!shape.Renderer.bounds.IntersectRay(ray, out float enter) || enter >= nearest) continue;
            if (!TriangleHit(ray, shape, nearest, out float distance, out Vector3 at, out Vector3 facing, out int index)) continue;
            nearest = distance; part = shape.Object; point = at; normal = facing; found = index;
        }
        if (part == null) return;
        best = part;
        triangle = found;
        sample.Point = point;
        sample.Normal = normal;
    }

    static List<Shape> Shapes(Transform rig)
    {
        var known = shapes.Where(shape => shape.Mesh != null).GroupBy(shape => shape.Mesh).ToDictionary(group => group.Key, group => group.First());
        var result = new List<Shape>();
#if UNITY_6000_6_OR_NEWER
        foreach (var filter in UnityEngine.Object.FindObjectsByType<MeshFilter>())
#else
        foreach (var filter in UnityEngine.Object.FindObjectsByType<MeshFilter>(FindObjectsSortMode.None))
#endif
        {
            var go = filter.gameObject;
            var mesh = filter.sharedMesh;
            var renderer = go.GetComponent<MeshRenderer>();
            if (mesh == null || renderer == null || go.GetComponent<Collider>() != null || Skip(go, rig)) continue;
            var shape = new Shape { Object = go, Renderer = renderer, Mesh = mesh };
            if (known.TryGetValue(mesh, out var seen)) { shape.Vertices = seen.Vertices; shape.Triangles = seen.Triangles; }
            else
            {
                try
                {
                    if (mesh.triangles.Length / 3 <= MaxTriangles) { shape.Vertices = mesh.vertices; shape.Triangles = mesh.triangles; }
                }
                catch (UnityException) { shape.Triangles = null; }
                known[mesh] = shape;
            }
            result.Add(shape);
        }
        return result;
    }

    static bool TriangleHit(Ray ray, Shape shape, float limit, out float distance, out Vector3 point, out Vector3 normal,
                            out int triangle)
    {
        distance = 0; point = normal = default;
        triangle = -1;
        var matrix = shape.Object.transform.localToWorldMatrix;
        var inverse = matrix.inverse;
        var origin = inverse.MultiplyPoint3x4(ray.origin);
        var direction = inverse.MultiplyVector(ray.direction);
        var vertices = shape.Vertices;
        var triangles = shape.Triangles;
        var best = float.PositiveInfinity;
        var found = -1;
        for (int i = 0; i + 2 < triangles.Length; i += 3)
        {
            var a = vertices[triangles[i]];
            var e1 = vertices[triangles[i + 1]] - a;
            var e2 = vertices[triangles[i + 2]] - a;
            var p = Vector3.Cross(direction, e2);
            var det = Vector3.Dot(e1, p);
            if (Mathf.Abs(det) < 1e-8f) continue;
            var s = origin - a;
            var u = Vector3.Dot(s, p) / det;
            if (u < 0 || u > 1) continue;
            var q = Vector3.Cross(s, e1);
            var v = Vector3.Dot(direction, q) / det;
            if (v < 0 || u + v > 1) continue;
            var d = Vector3.Dot(e2, q) / det;
            if (d < 0 || d >= best) continue;
            best = d; found = i;
        }
        if (found < 0) return false;
        point = matrix.MultiplyPoint3x4(origin + direction * best);
        distance = Vector3.Distance(ray.origin, point);
        if (distance >= limit) return false;
        var face = Vector3.Cross(vertices[triangles[found + 1]] - vertices[triangles[found]],
                                 vertices[triangles[found + 2]] - vertices[triangles[found]]);
        normal = inverse.transpose.MultiplyVector(face).normalized;
        if (Vector3.Dot(normal, ray.direction) > 0) normal = -normal;
        triangle = found / 3;
        return true;
    }

    // --- what the person sees in the headset ----------------------------------------------------

    static bool Busy(string status)
    {
        return status.StartsWith("Working", StringComparison.Ordinal) || status.StartsWith("Understanding", StringComparison.Ordinal)
            || status.StartsWith("Which one", StringComparison.Ordinal);
    }

    static void Show(Camera head, double now)
    {
        var status = GazeVoiceBridge.Status ?? "";
        if (status != shownStatus) { shownStatus = status; statusAt = now; }
        string line = null;
        var last = Samples.Count > 0 ? Samples[Samples.Count - 1] : default;
        if (talking) line = "\u25cf Listening\u2026 let go to send." + (last.Hit ? " Aiming at " + Aimed(last) + "." : "")
                            + (now - eyesAt < EyesFresh ? "" : " (head gaze)");
        else if (now - noticeAt < StatusSeconds) line = notice;
        else if (GazeVoiceBridge.VoiceInVr && status.Length > 0 && (now - statusAt < StatusSeconds || Busy(status))) line = status;
        var point = talking && last.Hit && GazeVoiceBridge.VoiceIsOn;
        if (head == null || (line == null && !point)) { Hide(false); return; }
        Build();
        var eye = head.transform;
        hud.SetActive(line != null);
        if (line != null)
        {
            var target = eye.position + eye.forward * 1.4f - eye.up * 0.32f;
            var wasHidden = hudLine == null;
            if (line != hudLine) Write(line);
            hudLine = line;
            hud.transform.position = wasHidden ? target : Vector3.Lerp(hud.transform.position, target, 0.15f);
            hud.transform.rotation = Quaternion.LookRotation(hud.transform.position - eye.position, eye.up);
        }
        else hudLine = null;
        dot.SetActive(point);
        if (point)
        {
            dot.transform.position = last.Point + last.Normal * 0.004f;
            dot.transform.localScale = Vector3.one * Mathf.Clamp(Vector3.Distance(eye.position, last.Point) * 0.012f, 0.008f, 0.4f);
        }
    }

    static string Aimed(Sample sample)
    {
        var part = sample.Root != null ? sample.Name + " of " + sample.Root : sample.Name;
        return sample.Material != null && sample.Submesh >= 0 ? part + " (" + sample.Material + ")" : part;
    }

    static void Write(string line)
    {
        hudText.text = Wrap(line);
        var rotation = hud.transform.rotation;
        hud.transform.rotation = Quaternion.identity;
        var size = hudText.GetComponent<Renderer>().bounds.size;
        hud.transform.rotation = rotation;
        hudBack.localScale = new Vector3(size.x + 0.06f, size.y + 0.04f, 1f);
    }

    static string Wrap(string text)
    {
        var lines = new List<string>();
        var current = new StringBuilder();
        foreach (var word in text.Split(' '))
        {
            if (current.Length > 0 && current.Length + 1 + word.Length > LineChars) { lines.Add(current.ToString()); current.Clear(); }
            if (current.Length > 0) current.Append(' ');
            current.Append(word);
        }
        if (current.Length > 0) lines.Add(current.ToString());
        return String.Join("\n", lines.Take(4));
    }

    static Material Unlit(Color color)
    {
        var shader = Shader.Find("Universal Render Pipeline/Unlit") ?? Shader.Find("Unlit/Color");
        var material = new Material(shader) { hideFlags = HideFlags.HideAndDontSave };
        Materials.Add(material);
        if (material.HasProperty("_BaseColor")) material.SetColor("_BaseColor", color);
        if (material.HasProperty("_Color")) material.SetColor("_Color", color);
        return material;
    }

    static Mesh BuiltinMesh(PrimitiveType type)
    {
        var temporary = GameObject.CreatePrimitive(type);
        var mesh = temporary.GetComponent<MeshFilter>().sharedMesh;
        UnityEngine.Object.DestroyImmediate(temporary);
        return mesh;
    }

    static GameObject Part(string name, Transform parent, Mesh mesh, Material material)
    {
        var go = new GameObject(name) { hideFlags = HideFlags.HideAndDontSave };
        if (parent != null) go.transform.SetParent(parent, false);
        if (mesh != null) go.AddComponent<MeshFilter>().sharedMesh = mesh;
        if (material != null) go.AddComponent<MeshRenderer>().sharedMaterial = material;
        return go;
    }

    static void Build()
    {
        if (hud != null && dot != null) return;
        Hide(true);
        hud = Part("GazeVoice Status", null, null, null);
        var label = Part("Text", hud.transform, null, null);
        hudText = label.AddComponent<TextMesh>();
        var font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        hudText.font = font;
        label.GetComponent<MeshRenderer>().sharedMaterial = font.material;
        hudText.fontSize = 64;
        hudText.characterSize = 0.0042f;
        hudText.anchor = TextAnchor.MiddleCenter;
        hudText.alignment = TextAlignment.Center;
        hudText.color = Color.white;
        hudBack = Part("Back", hud.transform, BuiltinMesh(PrimitiveType.Quad), Unlit(new Color(0.07f, 0.08f, 0.1f))).transform;
        hudBack.localPosition = new Vector3(0f, 0f, 0.005f);
        dot = Part("GazeVoice Gaze", null, BuiltinMesh(PrimitiveType.Sphere), Unlit(new Color(0.2f, 0.9f, 0.85f)));
        hudLine = null;
    }

    static void Hide(bool destroy)
    {
        if (!destroy)
        {
            if (hud != null) hud.SetActive(false);
            if (dot != null) dot.SetActive(false);
            hudLine = null;
            return;
        }
        foreach (var go in new[] { hud, dot })
            if (go != null) UnityEngine.Object.DestroyImmediate(go);
        foreach (var material in Materials)  // only the ones made here: the label's font material is Unity's own
            if (material != null) UnityEngine.Object.DestroyImmediate(material);
        Materials.Clear();
        hud = dot = null;
        hudText = null;
        hudBack = null;
        hudLine = null;
    }

    // A reload forgets hud and dot without destroying them, so earlier ones can be left showing an old status.
    static void Strays()
    {
        foreach (var go in Resources.FindObjectsOfTypeAll<GameObject>())
        {
            if (go == null || go == hud || go == dot || go.transform.parent != null || go.hideFlags != HideFlags.HideAndDontSave) continue;
            if (go.name != "GazeVoice Status" && go.name != "GazeVoice Gaze") continue;
            foreach (var renderer in go.GetComponentsInChildren<MeshRenderer>(true))
            {
                if (renderer.GetComponent<TextMesh>() != null) continue;
                var material = renderer.sharedMaterial;
                if (material != null && material.hideFlags == HideFlags.HideAndDontSave && !EditorUtility.IsPersistent(material))
                    UnityEngine.Object.DestroyImmediate(material);
            }
            UnityEngine.Object.DestroyImmediate(go);
        }
    }

    // --- the dashboard's questions ----------------------------------------------------------------

    static object Request(JObject request)
    {
        switch (request.Value<string>("cmd"))
        {
            case "vr_status":
                return Status();
            case "vr_gaze":
                return Gaze(request.Value<double?>("t0") ?? Now - 5, request.Value<double?>("t1") ?? Now);
            case "vr_play":
                if (!EditorApplication.isPlaying) EditorApplication.EnterPlaymode();
                return new { ok = true };
            case "vr_stop":
                if (EditorApplication.isPlaying) EditorApplication.ExitPlaymode();
                return new { ok = true };
            case "vr_setup":
                if (EditorApplication.isPlayingOrWillChangePlaymode) return new { ok = false, error = "Stop Play mode first." };
                return GazeVoiceVRSetup.Prepare(true);
            default:
                return new { ok = false, error = "unknown command" };
        }
    }

    static object Status()
    {
        var general = XRGeneralSettingsPerBuildTarget.XRGeneralSettingsForBuildTarget(BuildTargetGroup.Standalone);
        var manager = general != null ? general.AssignedSettings : null;
        var loaders = manager != null ? manager.activeLoaders.Where(loader => loader != null).Select(loader => loader.name).ToArray() : new string[0];
        var playing = EditorApplication.isPlaying;
        var openxr = playing ? OpenXRSettings.Instance : OpenXRSettings.GetSettingsForBuildTargetGroup(BuildTargetGroup.Standalone);
        var eyeFeature = openxr != null ? openxr.GetFeature<EyeGazeInteraction>() : null;
        InputDevices.GetDevicesWithCharacteristics(InputDeviceCharacteristics.HeadMounted, Devices);
        var headset = Devices.Select(device => device.name).FirstOrDefault(name => !String.IsNullOrEmpty(name));
        InputDevices.GetDevicesWithCharacteristics(InputDeviceCharacteristics.Controller, Devices);
        var controllers = Devices.Select(device => device.name).Where(name => !String.IsNullOrEmpty(name)).Distinct().ToArray();
        var active = playing && XRSettings.isDeviceActive;
        // While playing, OpenXR reports a feature the runtime couldn't start as off, though the project has it on.
        var eyeFailed = playing && eyeFeature != null && FailedToStart != null && (bool)FailedToStart.GetValue(eyeFeature);
        var eyes = playing && Now - eyesAt < EyesFresh;
        return new
        {
            ok = true, xr = true, loaders, playing, active,
            headset = headset ?? (active ? XRSettings.loadedDeviceName : null),
            controllers, key = TalkButtons,
            eye_feature = eyeFeature != null && (eyeFeature.enabled || eyeFailed), eye_failed = eyeFailed,
            eye_runtime = active ? OpenXRRuntime.IsExtensionEnabled(EyeGazeExtension) : (bool?)null,
            eyes, gaze = playing ? (eyes ? "eyes" : "head") : null,
            scene = SceneManager.GetActiveScene().path,
            vr_scene = File.Exists(GazeVoiceVRSetup.ScenePath) ? GazeVoiceVRSetup.ScenePath : null,
            keeping = GazeVoiceVRKeep.Pending,
        };
    }

    static object Gaze(double t0, double t1)
    {
        var samples = Samples.Where(sample => sample.T >= t0 && sample.T <= t1).Take(MaxReply).Select(sample => new
        {
            t = sample.T,
            uid = sample.Hit ? (long?)sample.Id : null,
            name = sample.Hit ? sample.Name : null,
            parent = sample.Hit ? sample.Parent : null,
            path = sample.Hit ? sample.Path : null,
            root = sample.Hit ? sample.Root : null,
            root_uid = sample.Hit && sample.Root != null ? (long?)sample.RootId : null,
            material = sample.Hit ? sample.Material : null,
            submesh = sample.Hit && sample.Submesh >= 0 ? (int?)sample.Submesh : null,
            submeshes = sample.Hit && sample.Submeshes > 1 ? (int?)sample.Submeshes : null,
            point = sample.Hit ? new[] { sample.Point.x, sample.Point.y, sample.Point.z } : null,
            normal = sample.Hit ? new[] { sample.Normal.x, sample.Normal.y, sample.Normal.z } : null,
            eyes = sample.Eyes,
        }).ToArray();
        return new { ok = true, playing = EditorApplication.isPlaying, samples,
            oldest = Samples.Count > 0 ? Samples[0].T : (double?)null };
    }
}
