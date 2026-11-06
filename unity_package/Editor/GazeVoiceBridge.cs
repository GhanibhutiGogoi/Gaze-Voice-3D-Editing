using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

[InitializeOnLoad]
internal static class GazeVoiceBridge
{
    internal const int Port = 9878;
    internal const string Version = "0.1.0";
    static readonly string Boot = Guid.NewGuid().ToString("N");
    static readonly ConcurrentQueue<Pending> Requests = new ConcurrentQueue<Pending>();
    static readonly ConcurrentQueue<Dictionary<string, object>> Events = new ConcurrentQueue<Dictionary<string, object>>();
    static TcpListener listener;
    static Thread listenerThread;
    static int sequence;
    static string status = "Voice is off";
    static int undoGroup = -1;
    static Dictionary<int, string> beforeEdit;

    sealed class Pending
    {
        public string Line;
        public ManualResetEventSlim Done = new ManualResetEventSlim(false);
        public string Reply;
    }

    sealed class Request
    {
        public string cmd;
        public int? ack;
        public string boot;
        public string message;
        public string text;
        public string name;
        public JObject arguments;
    }

    static GazeVoiceBridge()
    {
        EditorApplication.update += Pump;
        StartListener();
        AssemblyReloadEvents.beforeAssemblyReload += StopListener;
        EditorApplication.quitting += StopListener;
    }

    static void StartListener()
    {
        try
        {
            listener = new TcpListener(IPAddress.Loopback, Port);
            listener.Start();
            listenerThread = new Thread(AcceptLoop) { IsBackground = true, Name = "GazeVoice Unity bridge" };
            listenerThread.Start();
        }
        catch (SocketException ex)
        {
            Debug.LogWarning("GazeVoice bridge could not listen on 127.0.0.1:" + Port + ": " + ex.Message);
        }
    }

    static void StopListener()
    {
        try { listener?.Stop(); } catch { }
        listener = null;
    }

    static void AcceptLoop()
    {
        var server = listener;
        while (server != null)
        {
            TcpClient client;
            try { client = server.AcceptTcpClient(); }
            catch (SocketException) { break; }
            catch (ObjectDisposedException) { break; }
            ThreadPool.QueueUserWorkItem(_ => Serve(client));
        }
    }

    static void Serve(TcpClient client)
    {
        using (client)
        using (var stream = client.GetStream())
        {
            var bytes = new List<byte>();
            var one = new byte[1];
            while (client.Connected)
            {
                int count;
                try { count = stream.Read(one, 0, 1); }
                catch { return; }
                if (count == 0) return;
                if (one[0] != (byte)'\n')
                {
                    bytes.Add(one[0]);
                    if (bytes.Count > 65536) return;
                    continue;
                }

                var pending = new Pending { Line = Encoding.UTF8.GetString(bytes.ToArray()) };
                bytes.Clear();
                Requests.Enqueue(pending);
                if (!pending.Done.Wait(TimeSpan.FromSeconds(5))) return;
                var reply = Encoding.UTF8.GetBytes(pending.Reply + "\n");
                try { stream.Write(reply, 0, reply.Length); }
                catch { return; }
                pending.Done.Dispose();
            }
        }
    }

    static void Pump()
    {
        while (Requests.TryDequeue(out var pending))
        {
            try
            {
                var request = JsonConvert.DeserializeObject<Request>(pending.Line);
                pending.Reply = JsonConvert.SerializeObject(Handle(request));
            }
            catch (Exception ex)
            {
                pending.Reply = JsonConvert.SerializeObject(new { ok = false, error = ex.Message });
            }
            finally { pending.Done.Set(); }
        }
    }

    static object Handle(Request request)
    {
        if (request == null || String.IsNullOrEmpty(request.cmd)) return new { ok = false, error = "invalid request" };
        switch (request.cmd)
        {
            case "ping":
                return new { ok = true, bridge = Version, unity = Application.unityVersion, key = "Talk button" };
            case "selection":
                var selected = Selection.gameObjects.Where(go => go != null).Select(go => go.name).ToArray();
                return new { ok = true, active = Selection.activeGameObject != null ? Selection.activeGameObject.name : null,
                    selected, mode = "editor" };
            case "scene_names":
                return new { ok = true, names = Resources.FindObjectsOfTypeAll<GameObject>()
                    .Where(go => go != null && go.scene.IsValid() && go.hideFlags == HideFlags.None)
                    .Select(go => go.name).Distinct().Take(300).ToArray() };
            case "status":
                status = request.text ?? "";
                return new { ok = true };
            case "poll":
                var events = new List<Dictionary<string, object>>();
                if (!request.ack.HasValue)
                {
                    while (Events.TryDequeue(out _)) { }
                    return new { ok = true, boot = Boot, events };
                }
                var ack = request.boot == null || request.boot == Boot ? request.ack.Value : 0;
                while (Events.TryPeek(out var evt) && Convert.ToInt32(evt["seq"]) <= ack)
                    Events.TryDequeue(out _);
                foreach (var evt in Events.ToArray())
                    if (Convert.ToInt32(evt["seq"]) > ack) events.Add(evt);
                return new { ok = true, boot = Boot, events };
            case "tool":
                return RunTool(request.name, request.arguments ?? new JObject());
            case "begin_edit":
                Undo.IncrementCurrentGroup();
                undoGroup = Undo.GetCurrentGroup();
                beforeEdit = SceneSnapshot();
                return new { ok = true };
            case "undo_push":
                var changed = beforeEdit != null && !SnapshotsEqual(beforeEdit, SceneSnapshot());
                if (undoGroup >= 0)
                {
                    if (changed)
                    {
                        Undo.SetCurrentGroupName(request.message ?? "GazeVoice");
                        Undo.CollapseUndoOperations(undoGroup);
                    }
                    undoGroup = -1;
                }
                beforeEdit = null;
                return new { ok = true, pushed = changed };
            case "end_edit":
                undoGroup = -1;
                beforeEdit = null;
                return new { ok = true };
            case "undo":
                Undo.PerformUndo();
                return new { ok = true };
            case "redo":
                Undo.PerformRedo();
                return new { ok = true };
            default:
                return new { ok = false, error = "unknown command" };
        }
    }

    static object RunTool(string name, JObject args)
    {
        try
        {
            if (name == "get_scene_objects")
                return new { ok = true, objects = Resources.FindObjectsOfTypeAll<GameObject>()
                    .Where(go => go != null && go.scene.IsValid() && go.hideFlags == HideFlags.None)
                    .Take(300).Select(go => new { id = go.GetInstanceID(), go.name,
                        parent = go.transform.parent != null ? go.transform.parent.name : null,
                        selected = Selection.Contains(go) }).ToArray() };
            if (name == "create_primitive")
            {
                if (!Enum.TryParse(args.Value<string>("shape"), true, out PrimitiveType shape) ||
                    !new[] { PrimitiveType.Cube, PrimitiveType.Sphere, PrimitiveType.Capsule,
                        PrimitiveType.Cylinder, PrimitiveType.Plane, PrimitiveType.Quad }.Contains(shape))
                    return new { ok = false, error = "Unknown primitive shape" };
                var hasPosition = TryVector(args, "position", out var position);
                var created = GameObject.CreatePrimitive(shape);
                if (args.Value<string>("name") is string newName && !String.IsNullOrWhiteSpace(newName)) created.name = newName;
                if (hasPosition) created.transform.position = position;
                Undo.RegisterCreatedObjectUndo(created, "GazeVoice create primitive");
                return new { ok = true, id = created.GetInstanceID(), created.name };
            }
            if (!new[] { "get_object_details", "select_object", "transform_object", "set_color",
                    "duplicate_object", "delete_object" }.Contains(name))
                return new { ok = false, error = "Unknown Unity tool" };
            var target = Target(args);
            if (target == null) return new { ok = false, error = "Select a target or provide its instance ID" };
            switch (name)
            {
                case "get_object_details":
                    var renderer = target.GetComponent<Renderer>();
                    return new { ok = true, id = target.GetInstanceID(), target.name,
                        position = Vector(target.transform.localPosition), rotation = Vector(target.transform.localEulerAngles),
                        scale = Vector(target.transform.localScale),
                        components = target.GetComponents<Component>().Where(c => c != null).Select(c => c.GetType().Name).ToArray(),
                        color = renderer != null && renderer.sharedMaterial != null && renderer.sharedMaterial.HasProperty("_Color")
                            ? ColorUtility.ToHtmlStringRGBA(renderer.sharedMaterial.color) : null };
                case "select_object":
                    if (args["target"] == null) return new { ok = false, error = "target is required" };
                    Selection.activeGameObject = target;
                    return new { ok = true, id = target.GetInstanceID(), target.name };
                case "transform_object":
                    var didTransform = TryVector(args, "position", out var newPosition);
                    var didRotate = TryVector(args, "rotation", out var newRotation);
                    var didScale = TryVector(args, "scale", out var newScale);
                    if (!didTransform && !didRotate && !didScale)
                        return new { ok = false, error = "Provide position, rotation or scale" };
                    Undo.RecordObject(target.transform, "GazeVoice transform");
                    if (didTransform) target.transform.localPosition = newPosition;
                    if (didRotate) target.transform.localEulerAngles = newRotation;
                    if (didScale) target.transform.localScale = newScale;
                    EditorUtility.SetDirty(target.transform);
                    return new { ok = true, id = target.GetInstanceID() };
                case "set_color":
                    var colorText = args.Value<string>("color");
                    if (!ColorUtility.TryParseHtmlString(colorText, out var color))
                        return new { ok = false, error = "Use a hex color such as #2E5BFF" };
                    var targetRenderer = target.GetComponent<Renderer>();
                    if (targetRenderer == null || targetRenderer.sharedMaterial == null)
                        return new { ok = false, error = "Target has no material renderer" };
                    var material = new Material(targetRenderer.sharedMaterial);
                    material.name = targetRenderer.sharedMaterial.name + " (GazeVoice)";
                    if (!material.HasProperty("_Color"))
                    {
                        UnityEngine.Object.DestroyImmediate(material);
                        return new { ok = false, error = "Material has no color property" };
                    }
                    material.color = color;
                    Undo.RegisterCreatedObjectUndo(material, "GazeVoice material");
                    Undo.RecordObject(targetRenderer, "GazeVoice color");
                    targetRenderer.sharedMaterial = material;
                    EditorUtility.SetDirty(targetRenderer);
                    return new { ok = true, id = target.GetInstanceID(), color = colorText };
                case "duplicate_object":
                    var duplicate = UnityEngine.Object.Instantiate(target, target.transform.parent);
                    duplicate.name = args.Value<string>("name") ?? target.name + " Copy";
                    Undo.RegisterCreatedObjectUndo(duplicate, "GazeVoice duplicate");
                    return new { ok = true, id = duplicate.GetInstanceID(), duplicate.name };
                case "delete_object":
                    if (args["target"] == null) return new { ok = false, error = "target is required" };
                    Undo.DestroyObjectImmediate(target);
                    return new { ok = true };
                default:
                    return new { ok = false, error = "Unknown Unity tool" };
            }
        }
        catch (Exception ex) { return new { ok = false, error = ex.Message }; }
    }

    static GameObject Target(JObject args)
    {
        var id = args.Value<int?>("target");
        return id.HasValue ? EditorUtility.InstanceIDToObject(id.Value) as GameObject : Selection.activeGameObject;
    }

    static float[] Vector(Vector3 value) => new[] { value.x, value.y, value.z };

    static bool TryVector(JObject args, string name, out Vector3 value)
    {
        value = default;
        if (!(args[name] is JArray items)) return false;
        if (items.Count != 3) throw new ArgumentException(name + " needs three numbers");
        value = new Vector3(items[0].Value<float>(), items[1].Value<float>(), items[2].Value<float>());
        if (float.IsNaN(value.x) || float.IsNaN(value.y) || float.IsNaN(value.z) ||
            float.IsInfinity(value.x) || float.IsInfinity(value.y) || float.IsInfinity(value.z))
            throw new ArgumentException(name + " needs finite numbers");
        return true;
    }

    internal static void PushTalkEvent(string state)
    {
        Events.Enqueue(new Dictionary<string, object> {
            ["type"] = "ptt", ["state"] = state, ["t"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["seq"] = Interlocked.Increment(ref sequence)
        });
        while (Events.Count > 256) Events.TryDequeue(out _);
    }

    internal static string Status => status;

    static Dictionary<int, string> SceneSnapshot()
    {
        var snapshot = new Dictionary<int, string>();
        foreach (var go in Resources.FindObjectsOfTypeAll<GameObject>()
                     .Where(item => item != null && item.scene.IsValid() && item.hideFlags == HideFlags.None))
        {
            var parts = new List<string> { EditorJsonUtility.ToJson(go) };
            foreach (var component in go.GetComponents<Component>())
                if (component != null) parts.Add(EditorJsonUtility.ToJson(component));
            snapshot[go.GetInstanceID()] = String.Join("\n", parts);
        }
        return snapshot;
    }

    static bool SnapshotsEqual(Dictionary<int, string> first, Dictionary<int, string> second)
    {
        return first.Count == second.Count && first.All(pair => second.TryGetValue(pair.Key, out var value) && value == pair.Value);
    }
}

internal sealed class GazeVoiceWindow : EditorWindow
{
    bool talking;

    [MenuItem("Window/GazeVoice")]
    static void Open() => GetWindow<GazeVoiceWindow>("GazeVoice");

    void OnDisable()
    {
        if (talking) GazeVoiceBridge.PushTalkEvent("cancel");
        talking = false;
    }

    void OnGUI()
    {
        EditorGUILayout.LabelField("GazeVoice", EditorStyles.boldLabel);
        EditorGUILayout.LabelField(GazeVoiceBridge.Status, EditorStyles.wordWrappedLabel);
        var rect = GUILayoutUtility.GetRect(0, 44, GUILayout.ExpandWidth(true));
        GUI.Box(rect, talking ? "Listening… release to send" : "Hold to talk", EditorStyles.miniButton);
        var e = Event.current;
        if (e.type == EventType.MouseDown && e.button == 0 && rect.Contains(e.mousePosition))
        {
            talking = true;
            GazeVoiceBridge.PushTalkEvent("down");
            e.Use();
        }
        else if (talking && ((e.type == EventType.MouseUp && e.button == 0) ||
                             (e.type == EventType.MouseLeaveWindow)))
        {
            talking = false;
            GazeVoiceBridge.PushTalkEvent("up");
            e.Use();
        }
        else if (talking && e.type == EventType.KeyDown && e.keyCode == KeyCode.Escape)
        {
            talking = false;
            GazeVoiceBridge.PushTalkEvent("cancel");
            e.Use();
        }
        if (e.type == EventType.Repaint) Repaint();
    }
}
