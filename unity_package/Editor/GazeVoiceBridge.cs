using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
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
    internal const string Version = "0.2.0";
    const int MaxLine = 1024 * 1024;  // scripts and C# snippets arrive as one line
    const int MaxConsoleLines = 300;
    const int ShotWidth = 1280, ShotHeight = 720;
    const int ToolWaitSeconds = 300;  // execute_csharp can run this long; everything else answers at once
    const int RequestWaitSeconds = 5;
    const int VoiceOnSeconds = 3;  // polled this recently means the app's voice loop is on
    const string DefaultKey = "⌥ Space";
    const string UndoGroupKey = "GazeVoice.UndoGroup";
    const string SnippetClass = "GazeVoiceSnippet";
    const string SnippetHeader =
        "using System;\nusing System.Collections;\nusing System.Collections.Generic;\nusing System.IO;\nusing System.Linq;\n" +
        "using UnityEditor;\nusing UnityEditor.SceneManagement;\nusing UnityEngine;\nusing UnityEngine.SceneManagement;\n" +
        "using Object = UnityEngine.Object;\nusing Random = UnityEngine.Random;\n" +
        "public static class " + SnippetClass + "\n{\n    public static object Run()\n    {\n#line 1 \"snippet\"\n";
    const string SnippetFooter = "\n#line default\n        return null;\n    }\n}\n";
    static readonly string Boot = Guid.NewGuid().ToString("N");
    static readonly ConcurrentQueue<Pending> Requests = new ConcurrentQueue<Pending>();
    static readonly ConcurrentQueue<Dictionary<string, object>> Events = new ConcurrentQueue<Dictionary<string, object>>();
    static readonly ConcurrentQueue<Action> MainThread = new ConcurrentQueue<Action>();
    static readonly ConcurrentDictionary<TcpClient, bool> Clients = new ConcurrentDictionary<TcpClient, bool>();
    static readonly List<string> CompileErrors = new List<string>();
    static readonly List<LogLine> ConsoleLines = new List<LogLine>();
    static readonly object CompilerLock = new object();
    static TcpListener listener;
    static Thread listenerThread;
    static int sequence;
    static string status = "Voice is off";
    static int undoGroup = -1;
    static Dictionary<int, string> beforeEdit;
    static bool editSurvivedReload;
    static int consoleCount;
    static DateTime scriptsPendingUntil = DateTime.MinValue;
    static DateTime retryListenAt = DateTime.MinValue;
    static DateTime lastPoll = DateTime.MinValue;
    static UnityEditor.PackageManager.Requests.AddRequest packageRequest;
    static string packageError;
    static string cscPath;
    static string dotnetPath;

    sealed class LogLine
    {
        public string Text;
        public bool IsError;
    }

    sealed class Pending
    {
        public TcpClient Client;
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
        if (Application.isBatchMode) return;  // creating a project or running tests: no UI, no listener
        undoGroup = SessionState.GetInt(UndoGroupKey, -1);  // a command that reloaded scripts is still the same command
        editSurvivedReload = undoGroup >= 0;
        SessionState.EraseInt(UndoGroupKey);
        Application.logMessageReceivedThreaded += OnLog;
        UnityEditor.Compilation.CompilationPipeline.compilationStarted += OnCompilationStarted;
        UnityEditor.Compilation.CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled;
        EditorApplication.update += Pump;
        StartListener();
        AssemblyReloadEvents.beforeAssemblyReload += BeforeReload;
        EditorApplication.quitting += StopListener;
    }

    static void BeforeReload()
    {
        SessionState.SetInt(UndoGroupKey, undoGroup);
        StopListener();
    }

    static void StartListener()
    {
        try
        {
            listener = new TcpListener(IPAddress.Loopback, Port);
            // Sockets from before a script reload can still hold the port for a moment.
            listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            listener.Start();
            listenerThread = new Thread(AcceptLoop) { IsBackground = true, Name = "GazeVoice Unity bridge" };
            listenerThread.Start();
            retryListenAt = DateTime.MinValue;
        }
        catch (SocketException ex)
        {
            if (retryListenAt == DateTime.MinValue)
                Debug.LogWarning("GazeVoice bridge could not listen on 127.0.0.1:" + Port + ": " + ex.Message);
            listener = null;
            retryListenAt = DateTime.UtcNow.AddSeconds(2);
        }
    }

    static void StopListener()
    {
        try { listener?.Stop(); } catch { }
        listener = null;
        retryListenAt = DateTime.MinValue;
        // A thread blocked reading a socket can hold up the domain reload, so close them all.
        foreach (var client in Clients.Keys)
        {
            try { client.Close(); } catch { }
        }
    }

    static void AcceptLoop()
    {
        var server = listener;
        while (server != null)
        {
            TcpClient client;
            try { client = server.AcceptTcpClient(); }
            catch (SocketException) { break; }
            catch (InvalidOperationException) { break; }  // Stop() landed between two accepts
            ThreadPool.QueueUserWorkItem(_ => Serve(client));
        }
    }

    static void Serve(TcpClient client)
    {
        Clients[client] = true;
        try { ServeClient(client); }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
        finally { Clients.TryRemove(client, out _); }
    }

    static void ServeClient(TcpClient client)
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
                    if (bytes.Count > MaxLine) return;
                    continue;
                }

                var pending = new Pending { Client = client, Line = Encoding.UTF8.GetString(bytes.ToArray()) };
                bytes.Clear();
                Requests.Enqueue(pending);
                var wait = pending.Line.Contains("\"tool\"") ? ToolWaitSeconds : RequestWaitSeconds;
                if (!pending.Done.Wait(TimeSpan.FromSeconds(wait))) return;
                var reply = Encoding.UTF8.GetBytes(pending.Reply + "\n");
                try { stream.Write(reply, 0, reply.Length); }
                catch { return; }
            }
        }
    }

    static void Pump()
    {
        if (listener == null && retryListenAt != DateTime.MinValue && DateTime.UtcNow >= retryListenAt) StartListener();
        if (packageRequest != null && packageRequest.IsCompleted)
        {
            if (packageRequest.Status == UnityEditor.PackageManager.StatusCode.Failure)
                packageError = packageRequest.Error != null ? packageRequest.Error.message : "The package couldn't be added.";
            packageRequest = null;
        }
        while (MainThread.TryDequeue(out var work)) work();
        while (Requests.TryDequeue(out var pending))
        {
            if (ClientGone(pending.Client))  // it timed out waiting, so running this now could run it twice
            {
                pending.Done.Set();
                continue;
            }
            var answerLater = false;
            try
            {
                var request = JsonConvert.DeserializeObject<Request>(pending.Line);
                if (request != null && request.cmd == "tool" && request.name == "execute_csharp")
                {
                    StartSnippet(request.arguments != null ? request.arguments.Value<string>("code") : null, pending);
                    answerLater = true;
                }
                else pending.Reply = JsonConvert.SerializeObject(Handle(request));
            }
            catch (Exception ex)
            {
                pending.Reply = JsonConvert.SerializeObject(new { ok = false, error = ex.Message });
            }
            finally
            {
                if (!answerLater) pending.Done.Set();
            }
        }
    }

    static bool ClientGone(TcpClient client)
    {
        try
        {
            return client.Client == null
                || (client.Client.Poll(0, SelectMode.SelectRead) && client.Client.Available == 0);
        }
        catch (Exception) { return true; }
    }

    static bool IsCompiling()
    {
        return EditorApplication.isCompiling || EditorApplication.isUpdating || packageRequest != null
            || DateTime.UtcNow < scriptsPendingUntil;
    }

    static void OnCompilationStarted(object context)
    {
        CompileErrors.Clear();
        scriptsPendingUntil = DateTime.MinValue;
    }

    static void OnAssemblyCompiled(string assemblyPath, UnityEditor.Compilation.CompilerMessage[] messages)
    {
        foreach (var message in messages)
            if (message.type == UnityEditor.Compilation.CompilerMessageType.Error) CompileErrors.Add(message.message);
    }

    static void OnLog(string message, string stackTrace, LogType type)
    {
        lock (ConsoleLines)
        {
            ConsoleLines.Add(new LogLine {
                Text = type + ": " + message,
                IsError = type == LogType.Error || type == LogType.Exception || type == LogType.Assert
            });
            consoleCount++;
            if (ConsoleLines.Count > MaxConsoleLines) ConsoleLines.RemoveAt(0);
        }
    }

    static int ConsoleCount()
    {
        lock (ConsoleLines) return consoleCount;
    }

    static string[] LogsSince(int count)
    {
        lock (ConsoleLines)
        {
            var fresh = Math.Min(consoleCount - count, ConsoleLines.Count);
            return ConsoleLines.Skip(ConsoleLines.Count - fresh).Select(line => line.Text).ToArray();
        }
    }

    // --- running C# -------------------------------------------------------------------------
    // Unity's own compiler builds the snippet in a child process, so the Editor stays responsive and no
    // domain reload happens. The result is loaded from memory and run on the main thread.

    static void StartSnippet(string code, Pending pending)
    {
        if (String.IsNullOrWhiteSpace(code)) throw new ArgumentException("code is required");
        var references = ReferencePaths();
        var contents = EditorApplication.applicationContentsPath;
        ThreadPool.QueueUserWorkItem(_ =>
        {
            byte[] compiled = null;
            string errors = null;
            try { compiled = CompileSnippet(code, references, contents, out errors); }
            catch (Exception ex) { errors = ex.Message; }
            MainThread.Enqueue(() => FinishSnippet(compiled, errors, pending));
        });
    }

    static string[] ReferencePaths()
    {
        var byName = new Dictionary<string, string>();
        foreach (var loaded in AppDomain.CurrentDomain.GetAssemblies())
        {
            if (loaded.IsDynamic) continue;
            string path;
            try { path = loaded.Location; }
            catch (NotSupportedException) { continue; }
            if (String.IsNullOrEmpty(path) || !File.Exists(path)) continue;
            var name = Path.GetFileNameWithoutExtension(path);
            if (!byName.ContainsKey(name)) byName[name] = path;
        }
        var facades = Path.Combine(Path.GetDirectoryName(typeof(object).Assembly.Location), "Facades");
        if (Directory.Exists(facades))
        {
            foreach (var path in Directory.GetFiles(facades, "*.dll"))
            {
                var name = Path.GetFileNameWithoutExtension(path);
                if (!byName.ContainsKey(name)) byName[name] = path;
            }
        }
        return byName.Values.ToArray();
    }

    static void FindCompiler(string contents)
    {
        lock (CompilerLock)
        {
            if (cscPath != null && dotnetPath != null) return;
            cscPath = Directory.GetFiles(contents, "csc.dll", SearchOption.AllDirectories)
                .OrderByDescending(path => path.Contains("DotNetSdkRoslyn")).FirstOrDefault();
            // Unity 6 keeps these under Resources/Scripting; Unity 2022 keeps them at the top.
            dotnetPath = new[] { Path.Combine(contents, "Resources", "Scripting"), contents }
                .Select(root => Path.Combine(root, "NetCoreRuntime", "dotnet"))
                .FirstOrDefault(path => File.Exists(path))
                ?? Directory.GetFiles(contents, "dotnet", SearchOption.AllDirectories).FirstOrDefault();
            if (cscPath == null || dotnetPath == null)
                throw new InvalidOperationException("Couldn't find the C# compiler inside this Unity editor. Use write_script instead.");
        }
    }

    static byte[] CompileSnippet(string code, string[] references, string contents, out string errors)
    {
        errors = null;
        FindCompiler(contents);
        var folder = Path.Combine(Path.GetTempPath(), "gazevoice-csharp-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(folder);
        try
        {
            var source = Path.Combine(folder, "Snippet.cs");
            var output = Path.Combine(folder, "GazeVoiceSnippet" + Guid.NewGuid().ToString("N") + ".dll");
            var options = Path.Combine(folder, "options.rsp");
            File.WriteAllText(source, SnippetHeader + code + SnippetFooter);
            var lines = new List<string> {
                "/nologo", "/target:library", "/nostdlib+", "/langversion:latest", "/warn:0", "/debug-",
                "/out:\"" + output + "\"", "\"" + source + "\""
            };
            lines.AddRange(references.Select(path => "/reference:\"" + path + "\""));
            File.WriteAllLines(options, lines);
            var start = new System.Diagnostics.ProcessStartInfo(dotnetPath) {
                Arguments = "\"" + cscPath + "\" /noconfig \"@" + options + "\"",
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true
            };
            using (var process = System.Diagnostics.Process.Start(start))
            {
                var problems = process.StandardError.ReadToEndAsync();
                var text = process.StandardOutput.ReadToEnd();
                process.WaitForExit();
                if (process.ExitCode != 0)
                {
                    var report = (text + "\n" + problems.Result).Split('\n').Where(line => line.Contains("error ")).Take(20);
                    errors = String.Join("\n", report.Select(line => line.Trim()));
                    if (errors.Length == 0) errors = "The compiler exited with code " + process.ExitCode + ": " + text + problems.Result;
                    return null;
                }
            }
            return File.ReadAllBytes(output);
        }
        finally
        {
            try { Directory.Delete(folder, true); } catch (IOException) { }
        }
    }

    static void FinishSnippet(byte[] compiled, string errors, Pending pending)
    {
        var logStart = ConsoleCount();
        object reply;
        try
        {
            if (compiled == null)
                reply = new { ok = false, error = "The C# didn't compile.", details = errors };
            else
            {
                var snippet = System.Reflection.Assembly.Load(compiled).GetType(SnippetClass);
                var result = snippet.GetMethod("Run").Invoke(null, null);
                reply = new { ok = true, result = Describe(result, 0), logs = LogsSince(logStart) };
            }
        }
        catch (System.Reflection.TargetInvocationException ex)
        {
            var inner = ex.InnerException ?? ex;
            reply = new { ok = false, error = inner.GetType().Name + ": " + inner.Message, details = inner.StackTrace,
                logs = LogsSince(logStart) };
        }
        catch (Exception ex)
        {
            reply = new { ok = false, error = ex.Message };
        }
        pending.Reply = JsonConvert.SerializeObject(reply);
        pending.Done.Set();
    }

    static string Describe(object value, int depth)
    {
        if (value == null) return null;
        if (value is string text) return text;
        if (value is UnityEngine.Object unityObject)
            return unityObject != null ? unityObject.name + " (" + unityObject.GetType().Name + ")" : "destroyed object";
        if (value is IEnumerable items && depth < 3)
        {
            var parts = new List<string>();
            foreach (var item in items)
            {
                parts.Add(Describe(item, depth + 1) ?? "null");
                if (parts.Count >= 300) break;
            }
            return "[" + String.Join(", ", parts) + "]";
        }
        return value.ToString();
    }

    static object Handle(Request request)
    {
        if (request == null || String.IsNullOrEmpty(request.cmd)) return new { ok = false, error = "invalid request" };
        if ((request.cmd == "undo_push" || request.cmd == "undo" || request.cmd == "redo") && IsCompiling())
            return new { ok = false, error = "busy" };
        switch (request.cmd)
        {
            case "ping":
                return new { ok = true, bridge = Version, unity = Application.unityVersion, key = TalkKey(),
                    compiling = IsCompiling() };
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
                lastPoll = DateTime.UtcNow;
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
                editSurvivedReload = false;
                return new { ok = true };
            case "undo_push":
                var changed = editSurvivedReload || (beforeEdit != null && !SnapshotsEqual(beforeEdit, SceneSnapshot()));
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
                editSurvivedReload = false;
                return new { ok = true, pushed = changed };
            case "end_edit":
                undoGroup = -1;
                beforeEdit = null;
                editSurvivedReload = false;
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
            if (name == "write_script") return WriteScript(args);
            if (name == "read_console") return ReadConsole(args);
            if (name == "capture_scene_view") return CaptureSceneView();
            if (name == "add_package") return AddPackage(args);
            if (name == "get_compile_status")
                return new { ok = true, compiling = IsCompiling(), errors = CompileErrors.ToArray(), package_error = packageError };
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
                    if (args.Value<int?>("target") == null) return new { ok = false, error = "target is required" };
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
                    if (args.Value<int?>("target") == null) return new { ok = false, error = "target is required" };
                    Undo.DestroyObjectImmediate(target);
                    return new { ok = true };
                default:
                    return new { ok = false, error = "Unknown Unity tool" };
            }
        }
        catch (Exception ex) { return new { ok = false, error = ex.Message }; }
    }

    static object WriteScript(JObject args)
    {
        var relative = (args.Value<string>("path") ?? "").Replace('\\', '/');
        var content = args.Value<string>("content");
        if (!relative.StartsWith("Assets/") || !relative.EndsWith(".cs") || relative.Contains(".."))
            return new { ok = false, error = "path must be a .cs file under Assets/" };
        if (content == null) return new { ok = false, error = "content is required" };
        var full = Path.Combine(Directory.GetParent(Application.dataPath).FullName, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(full));
        if (File.Exists(full) && File.ReadAllText(full) == content)
            return new { ok = true, path = relative, changed = false };
        File.WriteAllText(full, content);
        scriptsPendingUntil = DateTime.UtcNow.AddSeconds(5);  // until Unity notices the file and starts compiling
        AssetDatabase.ImportAsset(relative, ImportAssetOptions.ForceUpdate);
        return new { ok = true, path = relative, changed = true };
    }

    static object ReadConsole(JObject args)
    {
        var limit = Math.Max(1, Math.Min(args.Value<int?>("limit") ?? 50, MaxConsoleLines));
        var errorsOnly = args.Value<bool?>("errors_only") ?? false;
        lock (ConsoleLines)
        {
            var lines = ConsoleLines.Where(line => !errorsOnly || line.IsError).Select(line => line.Text).ToArray();
            return new { ok = true, lines = lines.Skip(Math.Max(0, lines.Length - limit)).ToArray() };
        }
    }

    static object CaptureSceneView()
    {
        var view = SceneView.lastActiveSceneView;
        if (view == null || view.camera == null) return new { ok = false, error = "Open a Scene view first." };
        var camera = view.camera;
        var target = RenderTexture.GetTemporary(ShotWidth, ShotHeight, 24);
        var previousTarget = camera.targetTexture;
        var previousActive = RenderTexture.active;
        var picture = new Texture2D(ShotWidth, ShotHeight, TextureFormat.RGB24, false);
        try
        {
            camera.targetTexture = target;
            camera.Render();
            RenderTexture.active = target;
            picture.ReadPixels(new Rect(0, 0, ShotWidth, ShotHeight), 0, 0);
            picture.Apply();
            var path = Path.Combine(Path.GetTempPath(), "gazevoice-view-" + Guid.NewGuid().ToString("N") + ".png");
            File.WriteAllBytes(path, picture.EncodeToPNG());
            return new { ok = true, path };
        }
        finally
        {
            camera.targetTexture = previousTarget;
            RenderTexture.active = previousActive;
            RenderTexture.ReleaseTemporary(target);
            UnityEngine.Object.DestroyImmediate(picture);
        }
    }

    static object AddPackage(JObject args)
    {
        var identifier = args.Value<string>("package");
        if (String.IsNullOrWhiteSpace(identifier)) return new { ok = false, error = "package is required" };
        if (packageRequest != null) return new { ok = false, error = "Another package is still being added." };
        packageError = null;
        packageRequest = UnityEditor.PackageManager.Client.Add(identifier.Trim());
        return new { ok = true, started = identifier.Trim() };
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

    internal static bool VoiceIsOn => (DateTime.UtcNow - lastPoll).TotalSeconds < VoiceOnSeconds;

    static string TalkKey()
    {
        try
        {
            var binding = UnityEditor.ShortcutManagement.ShortcutManager.instance
                .GetShortcutBinding(GazeVoiceTalkKey.ShortcutId).ToString();
            return String.IsNullOrEmpty(binding) ? DefaultKey : binding;
        }
        catch (ArgumentException) { return DefaultKey; }
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

internal static class GazeVoiceTalkKey
{
    internal const string ShortcutId = "GazeVoice/Hold to Talk";
    const string VoiceOff = "Voice is off. Turn it on in the GazeVoice dashboard.";
    static bool held;

    [InitializeOnLoadMethod]
    static void WatchFocus()
    {
        EditorApplication.focusChanged += OnFocusChanged;
    }

    [UnityEditor.ShortcutManagement.ClutchShortcut(ShortcutId, KeyCode.Space, UnityEditor.ShortcutManagement.ShortcutModifiers.Alt)]
    static void Talk(UnityEditor.ShortcutManagement.ShortcutArguments args)
    {
        if (args.stage == UnityEditor.ShortcutManagement.ShortcutStage.Begin)
        {
            if (!GazeVoiceBridge.VoiceIsOn)
            {
                var view = SceneView.lastActiveSceneView;
                if (view != null) view.ShowNotification(new GUIContent(VoiceOff));
                return;
            }
            held = true;
            GazeVoiceBridge.PushTalkEvent("down");
        }
        else if (held)
        {
            held = false;
            // Unity ends held shortcuts when it loses focus, and that isn't the person letting go.
            GazeVoiceBridge.PushTalkEvent(EditorApplication.isFocused ? "up" : "cancel");
        }
    }

    static void OnFocusChanged(bool focused)
    {
        if (focused || !held) return;
        held = false;
        GazeVoiceBridge.PushTalkEvent("cancel");
    }
}
