using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

// SceneView owns the coordinate conversion, picking and drawing. All calls run on the Editor main thread.
internal static class GazeVoiceGaze
{
    sealed class Job
    {
        public JObject Request;
        public Action<object> Reply;
        public double Deadline;
    }

    static readonly Queue<Job> Jobs = new Queue<Job>();
    static readonly string Session = Guid.NewGuid().ToString("N");
    static SceneView view;
    static Rect content, desktop;
    static Matrix4x4 cameraMatrix, projection;
    static int revision;
    static string mode = "off", source = "mouse";
    static bool live;
    static Vector2? pointer, marker;
    static double pointerAt, markerAt, sampledAt, lastPaint;
    static JArray marks = new JArray();
    static Texture2D bubble;
    static List<Geometry> geometry;
    static string hoverName = "";
    static string Token => Session + ":" + revision;
    static double Now => EditorApplication.timeSinceStartup;

    internal static void Initialize()
    {
        SceneView.duringSceneGui += OnGUI;
        EditorApplication.hierarchyChanged += Invalidate;
        EditorApplication.projectChanged += Invalidate;
        EditorApplication.focusChanged += focused => { if (!focused) { pointer = null; Repaint(); } };
    }

    static void Invalidate()
    {
        revision++;
        pointer = marker = null;
        marks.Clear();
        hoverName = "";
        Repaint();
    }

    internal static void Clear()
    {
        mode = "off";
        live = false;
        Invalidate();
        if (bubble != null) UnityEngine.Object.DestroyImmediate(bubble);
        bubble = null;
        while (Jobs.Count > 0) Jobs.Dequeue().Reply(new { ok = false, error = "gaze_stopped" });
    }

    internal static void Tick()
    {
        while (Jobs.Count > 0 && Jobs.Peek().Deadline < Now)
            Jobs.Dequeue().Reply(new { ok = false, error = "no_scene_view" });
        if (!GazeVoiceBridge.VoiceIsOn && (pointer.HasValue || marker.HasValue || marks.Count > 0))
            Invalidate();
        if (pointer.HasValue && (Now - pointerAt > 0.25 || EditorWindow.mouseOverWindow != view))
        { pointer = null; hoverName = ""; Repaint(); }
        if (marker.HasValue && Now - markerAt > 0.5) { marker = null; Repaint(); }
        if (view != null && GazeVoiceBridge.VoiceIsOn && live && mode != "off" && Now - lastPaint > 0.05)
        { lastPaint = Now; view.Repaint(); }
    }

    internal static void Poll(JObject request)
    {
        var nextMode = request.Value<string>("gaze_mode") ?? "off";
        var nextSource = request.Value<string>("gaze_source") ?? "mouse";
        var nextLive = request.Value<bool?>("gaze_live") ?? false;
        if (nextMode != mode || nextSource != source || nextLive != live) Invalidate();
        mode = nextMode; source = nextSource; live = nextLive;
        if (!request.ContainsKey("marker") && marker.HasValue && nextLive && nextSource == "camera") markerAt = Now;
        if (request.ContainsKey("marker"))
        {
            marker = UnitPoint(request["marker"]);
            markerAt = Now;
            Repaint();
        }
    }

    internal static void Request(JObject request, Action<object> reply)
    {
        // Clearing an overlay does not need a live SceneView or a viewport token.
        if (request.Value<string>("cmd") == "gaze_show" && request["marks"] is JArray empty && empty.Count == 0)
        { marks.Clear(); marker = null; Repaint(); reply(new { ok = true }); return; }
        if (Jobs.Count >= 16) { reply(new { ok = false, error = "gaze_busy" }); return; }
        Jobs.Enqueue(new Job { Request = request, Reply = reply, Deadline = Now + 1.5 });
        var current = SceneView.lastActiveSceneView;
        if (current != null) current.Repaint();
    }

    static void Repaint() { if (view != null) view.Repaint(); }

    static Vector2? UnitPoint(JToken token)
    {
        if (!(token is JArray a) || a.Count != 2) return null;
        for (int i = 0; i < 2; i++)
        {
            if (a[i].Type != JTokenType.Float && a[i].Type != JTokenType.Integer) return null;
            double n = a[i].Value<double>();
            if (double.IsNaN(n) || double.IsInfinity(n) || n < 0 || n > 1) return null;
        }
        return new Vector2(a[0].Value<float>(), a[1].Value<float>());
    }

    static Vector2 Local(Vector2 uv) => new Vector2(content.x + uv.x * content.width, content.y + uv.y * content.height);
    static Vector2 UV(Vector2 local) => new Vector2((local.x - content.x) / content.width, (local.y - content.y) / content.height);

    static bool Near(Rect a, Rect b) => (a.position - b.position).sqrMagnitude < .25f && (a.size - b.size).sqrMagnitude < .25f;

    static bool Near(Matrix4x4 a, Matrix4x4 b)
    {
        for (int i = 0; i < 16; i++) if (Mathf.Abs(a[i] - b[i]) > 0.0001f) return false;
        return true;
    }

    static void OnGUI(SceneView current)
    {
        if (current != SceneView.lastActiveSceneView || current.camera == null) return;
        var camera = current.camera;
        // Project actual camera corners into GUI space; this excludes toolbars and respects Retina scaling.
        float depth = camera.nearClipPlane + 1f;
        var a = HandleUtility.WorldToGUIPoint(camera.ViewportToWorldPoint(new Vector3(0, 1, depth)));
        var b = HandleUtility.WorldToGUIPoint(camera.ViewportToWorldPoint(new Vector3(1, 0, depth)));
        var rect = Rect.MinMaxRect(a.x, a.y, b.x, b.y);
        if (rect.width <= 0 || rect.height <= 0) return;
        var screen = GUIUtility.GUIToScreenPoint(rect.position);
        var screenRect = new Rect(screen, rect.size);
        var ev = Event.current;
        if ((ev.type == EventType.Layout || view != current) &&
            (view != current || !Near(rect, content) || !Near(screenRect, desktop) ||
             !Near(cameraMatrix, camera.worldToCameraMatrix) || !Near(projection, camera.projectionMatrix)))
        {
            view = current; content = rect; desktop = screenRect;
            cameraMatrix = camera.worldToCameraMatrix; projection = camera.projectionMatrix;
            Invalidate();
        }
        if (ev.type == EventType.KeyDown && marks.Count > 0 && !EditorGUIUtility.editingTextField
            && ev.keyCode >= KeyCode.Alpha1 && ev.keyCode <= KeyCode.Alpha3)
        {
            GazeVoiceBridge.PushChoice((int)ev.keyCode - (int)KeyCode.Alpha1 + 1, Token);
            ev.Use();
        }
        if (EditorApplication.isFocused && EditorWindow.mouseOverWindow == current && content.Contains(ev.mousePosition)
            && ev.type != EventType.MouseLeaveWindow && live && mode != "off" && source == "mouse")
        {
            pointer = UV(ev.mousePosition); pointerAt = Now;
            if (Now - sampledAt >= 0.04)
            {
                sampledAt = Now;
                GazeVoiceBridge.PushPointer(pointer.Value.x, pointer.Value.y, Token);
            }
        }
        else if (source == "mouse") pointer = null;

        // CPU picking avoids nested GPU selection contexts during UI Toolkit repaint/layout.
        if (ev.type == EventType.Layout)
        {
            geometry = null;
            while (Jobs.Count > 0)
            {
                var job = Jobs.Dequeue();
                try { job.Reply(job.Deadline < Now ? new { ok = false, error = "gaze_timeout" } : Handle(job.Request)); }
                catch (Exception ex) { job.Reply(new { ok = false, error = ex.Message }); }
            }
            if (pointer.HasValue)
            {
                var hit = Pick(pointer.Value);
                hoverName = hit != null ? JObject.FromObject(hit).Value<string>("name") : "Empty space";
            }
            geometry = null;
        }
        if (ev.type == EventType.Repaint && GazeVoiceBridge.VoiceIsOn && live && mode != "off") Draw();
    }

    static object Handle(JObject request)
    {
        string cmd = request.Value<string>("cmd");
        string expected = request.Value<string>("viewport_id");
        if (expected != null && expected != Token) return new { ok = false, error = "stale_viewport" };
        if (cmd == "gaze_view") return new { ok = true, viewport_id = Token,
            width = content.width, height = content.height,
            screen_area = new { left = desktop.x, top = desktop.y, width = desktop.width, height = desktop.height } };
        if (cmd == "gaze_status") return new { ok = true, viewport_id = Token, mode, source, live,
            indicator = source == "mouse" ? (pointer.HasValue ? new[] { pointer.Value.x, pointer.Value.y } : null)
                : (marker.HasValue ? new[] { marker.Value.x, marker.Value.y } : null),
            hover_name = hoverName, marks = marks.Count };
        if (expected == null) return new { ok = false, error = "viewport_id_required" };
        if (cmd == "gaze_pick")
        {
            if (!(request["points"] is JArray points) || points.Count == 0 || points.Count > 64)
                return new { ok = false, error = "invalid_points" };
            var hits = new List<object>();
            foreach (var point in points)
            {
                var uv = UnitPoint(point);
                if (!uv.HasValue) return new { ok = false, error = "invalid_point" };
                hits.Add(Pick(uv.Value));
            }
            return new { ok = true, viewport_id = Token, hits };
        }
        if (cmd == "gaze_ground")
        {
            var uv = UnitPoint(request["point"]);
            if (!uv.HasValue) return new { ok = false, error = "invalid_point" };
            var ray = HandleUtility.GUIPointToWorldRay(Local(uv.Value));
            var plane = new Plane(Vector3.up, Vector3.zero);
            if (!plane.Raycast(ray, out float distance) || distance <= 0.000001f || distance > 10000f)
                return new { ok = false, error = "no_forward_plane_intersection" };
            var point = ray.GetPoint(distance);
            return new { ok = true, viewport_id = Token, point = new[] { point.x, point.y, point.z }, plane = "y=0" };
        }
        if (cmd == "gaze_validate")
        {
            if (!(request["uids"] is JArray ids) || ids.Count > 16)
                return new { ok = false, error = "invalid_ids" };
            foreach (var id in ids)
            {
                var go = GazeVoiceBridge.ObjectWithId(id.Value<long>()) as GameObject;
                if (go == null || !go.scene.IsValid() || !go.activeInHierarchy)
                    return new { ok = false, error = "object_gone" };
            }
            return new { ok = true };
        }
        if (cmd == "gaze_piece")
        {
            var id = request.Value<long?>("uid");
            var go = id.HasValue ? GazeVoiceBridge.ObjectWithId(id.Value) as GameObject : null;
            return go == null ? (object)new { ok = false, error = "object_gone" }
                : new { ok = true, part = (string)null, object_id = id.Value, scope = "object" };
        }
        if (cmd == "gaze_show")
        {
            if (!(request["marks"] is JArray items) || items.Count > 3) return new { ok = false, error = "invalid_marks" };
            foreach (var item in items)
                if (!(item is JObject) || !UnitPoint(new JArray(item["u"], item["v"])).HasValue)
                    return new { ok = false, error = "invalid_mark" };
            marks = (JArray)items.DeepClone(); Repaint();
            return new { ok = true };
        }
        return new { ok = false, error = "unsupported_gaze_command" };
    }

    sealed class Geometry
    {
        public GameObject Object;
        public Bounds Bounds;
        public Matrix4x4 Matrix;
        public Vector3[] Vertices;
        public int[] Triangles;
    }

    static List<Geometry> SceneGeometry()
    {
        var result = new List<Geometry>();
        foreach (var renderer in UnityEngine.Object.FindObjectsOfType<Renderer>())
        {
            var go = renderer.gameObject;
            if (!go.scene.IsValid() || !renderer.enabled || !go.activeInHierarchy || renderer.forceRenderingOff
                || (view.camera.cullingMask & (1 << go.layer)) == 0 || SceneVisibilityManager.instance.IsHidden(go)
                || SceneVisibilityManager.instance.IsPickingDisabled(go)) continue;
            var entry = new Geometry { Object = go, Bounds = renderer.bounds, Matrix = go.transform.localToWorldMatrix };
            var filter = go.GetComponent<MeshFilter>();
            Mesh mesh = filter != null ? filter.sharedMesh : null;
            Mesh baked = null;
            try
            {
                var skin = go.GetComponent<SkinnedMeshRenderer>();
                if (skin != null) { baked = new Mesh(); skin.BakeMesh(baked); mesh = baked; }
                if (mesh != null && mesh.isReadable) { entry.Vertices = mesh.vertices; entry.Triangles = mesh.triangles; }
                result.Add(entry);
            }
            finally { if (baked != null) UnityEngine.Object.DestroyImmediate(baked); }
        }
        // Terrain has no Renderer. Until terrain surface picking is supported, its bounds block rays too.
        foreach (var terrain in Terrain.activeTerrains)
        {
            var go = terrain.gameObject;
            if (!terrain.enabled || !terrain.drawHeightmap || terrain.terrainData == null || !go.scene.IsValid()
                || (view.camera.cullingMask & (1 << go.layer)) == 0 || SceneVisibilityManager.instance.IsHidden(go)
                || SceneVisibilityManager.instance.IsPickingDisabled(go)) continue;
            var bounds = terrain.terrainData.bounds;
            bounds.center += terrain.GetPosition();
            result.Add(new Geometry { Object = go, Bounds = bounds });
        }
        return result;
    }

    static object Pick(Vector2 uv)
    {
        var ray = HandleUtility.GUIPointToWorldRay(Local(uv));
        if (geometry == null) geometry = SceneGeometry();
        Geometry best = null;
        Vector3 point = Vector3.zero;
        float nearest = float.PositiveInfinity, blocker = float.PositiveInfinity;
        foreach (var item in geometry)
        {
            if (!item.Bounds.IntersectRay(ray, out float boundsDistance)) continue;
            if (item.Vertices == null)
            {
                // Unsupported renderers can occlude. Do not claim a surface hit from their bounding box.
                blocker = Mathf.Min(blocker, boundsDistance);
                continue;
            }
            if (!TriangleHit(ray, item.Vertices, item.Triangles, item.Matrix, out var hit)) continue;
            float distance = Vector3.Dot(hit - ray.origin, ray.direction);
            if (distance >= nearest) continue;
            nearest = distance; best = item; point = hit;
        }
        // A non-readable renderer or Terrain blocks this ray. Mark it separately from true empty
        // space so a "here" command cannot place a new object through visible geometry.
        if (blocker < nearest && blocker < float.PositiveInfinity)
            return new { occluded = true, name = "Unpickable surface" };
        if (best == null) return null;
        return new { uid = GazeVoiceBridge.IdOf(best.Object), name = best.Object.name,
            faces = best.Triangles.Length / 3, point = new[] { point.x, point.y, point.z }, exact_surface = true };
    }

    // Triangle intersection avoids requiring scene colliders or adding temporary components to the user's scene.
    internal static bool MeshHit(Ray ray, Mesh mesh, Matrix4x4 matrix, out Vector3 point)
        => TriangleHit(ray, mesh.vertices, mesh.triangles, matrix, out point);

    static bool TriangleHit(Ray ray, Vector3[] vertices, int[] triangles, Matrix4x4 matrix, out Vector3 point)
    {
        var inverse = matrix.inverse;
        var origin = inverse.MultiplyPoint3x4(ray.origin);
        var direction = inverse.MultiplyVector(ray.direction);
        float nearest = float.PositiveInfinity;
        point = Vector3.zero;
        for (int i = 0; i < triangles.Length; i += 3)
        {
            var a = vertices[triangles[i]];
            var e1 = vertices[triangles[i + 1]] - a;
            var e2 = vertices[triangles[i + 2]] - a;
            var p = Vector3.Cross(direction, e2);
            float det = Vector3.Dot(e1, p);
            if (Mathf.Abs(det) < 1e-8f) continue;
            var t = origin - a;
            float u = Vector3.Dot(t, p) / det;
            var q = Vector3.Cross(t, e1);
            float v = Vector3.Dot(direction, q) / det;
            float d = Vector3.Dot(e2, q) / det;
            if (u < 0 || v < 0 || u + v > 1 || d < 0 || d >= nearest) continue;
            nearest = d; point = ray.GetPoint(d);
        }
        return !float.IsPositiveInfinity(nearest);
    }

    static void Draw()
    {
        Handles.BeginGUI();
        try
        {
            Vector2? at = source == "mouse" ? pointer : marker;
            if (at.HasValue)
            {
                if (bubble == null)
                {
                    bubble = new Texture2D(64, 64, TextureFormat.RGBA32, false) { hideFlags = HideFlags.HideAndDontSave };
                    var pixels = new Color[64 * 64];
                    for (int y = 0; y < 64; y++) for (int x = 0; x < 64; x++)
                    {
                        float d = Vector2.Distance(new Vector2(x + .5f, y + .5f), new Vector2(32, 32)) / 32;
                        float alpha = Mathf.Clamp01((1 - d) * 8) * (.12f + .35f * Mathf.Exp(-Mathf.Pow((d - .86f) * 18, 2)));
                        pixels[y * 64 + x] = new Color(.2f, .9f, .85f, alpha);
                    }
                    bubble.SetPixels(pixels); bubble.Apply();
                }
                var p = Local(at.Value);
                float size = source == "mouse" ? 32 : 90;
                GUI.DrawTexture(new Rect(p.x - size / 2, p.y - size / 2, size, size), bubble);
                string label = source == "mouse" ? "Mouse focus · " + hoverName : "Camera gaze";
                GUI.Label(new Rect(p.x + size / 2, p.y, 240, 22), label, EditorStyles.helpBox);
            }
            foreach (var item in marks)
            {
                var p = Local(new Vector2(item.Value<float>("u"), item.Value<float>("v")));
                GUI.Label(new Rect(p.x, p.y, 220, 25), item.Value<int>("n") + " · " + item.Value<string>("name"), EditorStyles.helpBox);
            }
        }
        finally { Handles.EndGUI(); }
    }
}
