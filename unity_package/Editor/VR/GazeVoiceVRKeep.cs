using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Object = UnityEngine.Object;

// Play mode throws its changes away when it stops. In Unity VR the person edits while the scene plays, so this keeps what
// GazeVoice's edits did. The bridge calls Began and Ended around each edit, and this notes what the edit added, changed and
// deleted. When Play stops, the new objects are saved as prefabs, with meshes and materials made at runtime saved as
// assets beside them, and the changes to objects that were already there are written down with where those objects are
// in the scene. Back in Edit mode it all goes into the scene as one undo step, and the scene is saved. Only GazeVoice's
// edits are kept: what the game did by itself while playing, and where the person walked, are not.
[InitializeOnLoad]
internal static class GazeVoiceVRKeep
{
    const string PendingFile = "Library/GazeVoice/vr-pending.json";
    const string EditsFolder = "Assets/GazeVoice/VR Edits";
    const string UndoName = "GazeVoice VR edits";
    static readonly Regex Reference = new Regex("\\{\"instanceID\":(-?\\d+)\\}");
    static readonly Regex Placeholder = new Regex("\\{\"instanceID\":\"@(\\d+)\"\\}");

    sealed class Origin
    {
        public string Scene;
        public string[] Names;
        public int[] Indices;
        public string[] Components;
    }

    static Dictionary<long, Origin> origins;  // every object the scene had when the first edit began, by ID
    static Dictionary<long, string> before;  // and the state of each when the current edit began
    static readonly HashSet<long> Created = new HashSet<long>();
    static readonly HashSet<long> Changed = new HashSet<long>();
    static readonly Dictionary<long, Origin> Deleted = new Dictionary<long, Origin>();

    static GazeVoiceVRKeep()
    {
        if (Application.isBatchMode) return;
        GazeVoiceBridge.EditBegan += Began;
        GazeVoiceBridge.EditEnded += Ended;
        EditorApplication.playModeStateChanged += OnPlayMode;
        if (File.Exists(PendingFile)) EditorApplication.delayCall += Restore;  // scripts reloaded after Play stopped
    }

    internal static int Edits => Created.Count + Changed.Count + Deleted.Count;
    internal static bool Pending => Edits > 0 || File.Exists(PendingFile);

    static void OnPlayMode(PlayModeStateChange change)
    {
        if (change == PlayModeStateChange.EnteredPlayMode) Clear();
        else if (change == PlayModeStateChange.ExitingPlayMode)
        {
            try { Save(); }
            catch (Exception ex)
            {
                Debug.LogException(ex);
                Debug.LogError("GazeVoice VR couldn't keep the edits made in Play mode: " + ex.Message);
            }
            Clear();
        }
        else if (change == PlayModeStateChange.EnteredEditMode && File.Exists(PendingFile))
            EditorApplication.delayCall += Restore;
    }

    static void Clear()
    {
        origins = null;
        before = null;
        Created.Clear();
        Changed.Clear();
        Deleted.Clear();
    }

    // --- while playing: what each edit did --------------------------------------------------------------------------

    static void Began()
    {
        if (!EditorApplication.isPlaying) return;
        if (origins == null) origins = Tracked().ToDictionary(Id, OriginOf);
        before = Tracked().ToDictionary(Id, State);
    }

    static void Ended()
    {
        if (!EditorApplication.isPlaying || before == null || origins == null) return;
        var after = Tracked().ToDictionary(Id, State);
        foreach (var pair in after)
        {
            if (origins.ContainsKey(pair.Key))
            {
                if (!before.TryGetValue(pair.Key, out var was) || was != pair.Value) Changed.Add(pair.Key);
            }
            else if (!before.ContainsKey(pair.Key)) Created.Add(pair.Key);
        }
        foreach (var id in before.Keys.Where(id => !after.ContainsKey(id)))
        {
            if (GazeVoiceBridge.ObjectWithId(id) is GameObject alive && alive != null) continue;  // hidden, not gone
            Created.Remove(id);
            Changed.Remove(id);
            if (origins.TryGetValue(id, out var origin)) Deleted[id] = origin;
        }
        before = null;
    }

    static long Id(GameObject go) => GazeVoiceBridge.IdOf(go);

    // The objects an edit can change: in a saved scene, not hidden, and not in the player's rig, which moves by itself.
    static IEnumerable<GameObject> Tracked()
    {
        var origin = GazeVoiceVRSetup.FindType("Unity.XR.CoreUtils.XROrigin");
        var head = Camera.main != null ? Camera.main.transform.root : null;
        for (var i = 0; i < SceneManager.sceneCount; i++)
        {
            var scene = SceneManager.GetSceneAt(i);
            if (!scene.isLoaded || String.IsNullOrEmpty(scene.path)) continue;
            foreach (var root in scene.GetRootGameObjects())
            {
                if (root.transform == head || (origin != null && root.GetComponentInChildren(origin, true) != null)) continue;
                foreach (var transform in root.GetComponentsInChildren<Transform>(true))
                    if (transform.gameObject.hideFlags == HideFlags.None) yield return transform.gameObject;
            }
        }
    }

    static string State(GameObject go)
    {
        var parts = new List<string> { EditorJsonUtility.ToJson(go), go.transform.parent != null ? Id(go.transform.parent.gameObject).ToString() : "-" };
        foreach (var component in go.GetComponents<Component>())
            if (component != null) parts.Add(EditorJsonUtility.ToJson(component));
        return String.Join("\n", parts);
    }

    static Origin OriginOf(GameObject go)
    {
        var names = new List<string>();
        var indices = new List<int>();
        for (var transform = go.transform; transform != null; transform = transform.parent)
        {
            names.Insert(0, transform.name);
            indices.Insert(0, transform.GetSiblingIndex());
        }
        return new Origin { Scene = go.scene.path, Names = names.ToArray(), Indices = indices.ToArray(),
            Components = Components(go).Select(pair => pair.Value).ToArray() };
    }

    // Each component with its key: its type, and its number among the components of that type on the object.
    static List<KeyValuePair<Component, string>> Components(GameObject go)
    {
        var counts = new Dictionary<Type, int>();
        var result = new List<KeyValuePair<Component, string>>();
        foreach (var component in go.GetComponents<Component>())
        {
            if (component == null) continue;
            var type = component.GetType();
            counts.TryGetValue(type, out var number);
            counts[type] = number + 1;
            result.Add(new KeyValuePair<Component, string>(component, type.FullName + ", " + type.Assembly.GetName().Name + "#" + number));
        }
        return result;
    }

    static Component ComponentWithKey(GameObject go, string key)
    {
        var mark = key.LastIndexOf('#');
        var type = Type.GetType(key.Substring(0, mark));
        if (type == null || !Int32.TryParse(key.Substring(mark + 1), out var number)) return null;
        return go.GetComponents(type).Where(component => component != null && component.GetType() == type).ElementAtOrDefault(number);
    }

    // --- when Play stops: write it all down --------------------------------------------------------------------------

    static void Save()
    {
        if (before != null) Ended();  // Play stopped in the middle of an edit: keep what it did so far
        if (Edits == 0 || origins == null) return;
        var folder = EditsFolder + "/" + DateTime.Now.ToString("yyyyMMdd-HHmmss");
        var made = Created.Select(id => GazeVoiceBridge.ObjectWithId(id) as GameObject)
            .Where(go => go != null && (go.transform.parent == null || !Created.Contains(Id(go.transform.parent.gameObject))))
            .ToList();
        var madeItems = new JArray();
        var kept = new List<GameObject>();
        foreach (var root in made)
        {
            KeepAssets(root.GetComponentsInChildren<Component>(true), folder, new HashSet<Object>());
            var path = AssetDatabase.GenerateUniqueAssetPath(Folder(folder) + "/" + FileName(root.name, "Object") + ".prefab");
            PrefabUtility.SaveAsPrefabAsset(root, path, out var saved);
            if (!saved)
            {
                Debug.LogWarning("GazeVoice VR couldn't keep " + root.name + ".");
                continue;
            }
            kept.Add(root);
            var transform = root.transform;
            var parent = transform.parent != null ? RefOf(transform.parent.gameObject, kept) : null;
            var item = new JObject { ["prefab"] = path, ["name"] = root.name, ["scene"] = root.scene.path,
                ["index"] = transform.GetSiblingIndex(), ["active"] = root.activeSelf };
            if (parent != null)
            {
                item["parent"] = parent;
                Pose(item, transform.localPosition, transform.localRotation, transform.localScale);
            }
            else Pose(item, transform.position, transform.rotation, transform.lossyScale);
            madeItems.Add(item);
        }
        var changed = new JArray();
        foreach (var id in Changed)
        {
            var go = GazeVoiceBridge.ObjectWithId(id) as GameObject;
            if (go == null || !origins.TryGetValue(id, out var origin)) continue;
            var transform = go.transform;
            var components = new JArray();
            var current = Components(go);
            foreach (var pair in current)
            {
                if (pair.Key is Transform) continue;
                KeepAssets(new Object[] { pair.Key }, folder, new HashSet<Object>());
                var refs = new JArray();
                var json = Reference.Replace(EditorJsonUtility.ToJson(pair.Key), match => Token(match, refs, kept));
                components.Add(new JObject { ["key"] = pair.Value, ["json"] = json, ["refs"] = refs });
            }
            var item = new JObject { ["path"] = PathOf(origin), ["name"] = go.name, ["active"] = go.activeSelf, ["layer"] = go.layer,
                ["tag"] = go.tag, ["components"] = components,
                ["removed"] = new JArray(origin.Components.Except(current.Select(pair => pair.Value))) };
            if (transform.parent == null) item["parent"] = JValue.CreateNull();
            else if (RefOf(transform.parent.gameObject, kept) is JObject parent) item["parent"] = parent;
            Pose(item, transform.localPosition, transform.localRotation, transform.localScale);
            changed.Add(item);
        }
        var pending = new JObject { ["folder"] = folder, ["made"] = madeItems, ["changed"] = changed,
            ["deleted"] = new JArray(Deleted.Values.Select(PathOf)) };
        AssetDatabase.SaveAssets();
        Directory.CreateDirectory(Path.GetDirectoryName(PendingFile));
        File.WriteAllText(PendingFile, pending.ToString());
    }

    // Meshes, materials and textures made while playing are only in memory, and go when Play stops. They are saved as
    // assets first, so the prefabs and the changes point at something that lasts.
    static void KeepAssets(IEnumerable<Object> owners, string folder, HashSet<Object> seen)
    {
        foreach (var owner in owners)
        {
            if (owner == null || !seen.Add(owner)) continue;
            var property = new SerializedObject(owner).GetIterator();
            while (property.Next(true))
            {
                if (property.propertyType != SerializedPropertyType.ObjectReference) continue;
                var value = property.objectReferenceValue;
                if (value == null || value is GameObject || value is Component || EditorUtility.IsPersistent(value)
                    || (value.hideFlags & HideFlags.DontSave) != 0) continue;
                if (value is Material || value is ScriptableObject) KeepAssets(new[] { value }, folder, seen);  // a material's textures first
                if (EditorUtility.IsPersistent(value)) continue;
                try
                {
                    var extension = value is Material ? ".mat" : value is AnimationClip ? ".anim" : ".asset";
                    var name = FileName(value.name.Replace(" (Instance)", ""), value.GetType().Name);
                    AssetDatabase.CreateAsset(value, AssetDatabase.GenerateUniqueAssetPath(Folder(folder) + "/" + name + extension));
                }
                catch (Exception ex)
                {
                    Debug.LogWarning("GazeVoice VR couldn't keep " + value.name + ": " + ex.Message);
                }
            }
        }
    }

    // A reference to a scene object can't be saved as its ID, which is gone once Play stops: it becomes where the object is.
    static string Token(Match match, JArray refs, List<GameObject> kept)
    {
        var target = GazeVoiceBridge.ObjectWithId(Int64.Parse(match.Groups[1].Value));
        var owner = target is GameObject go ? go : target is Component component ? component.gameObject : null;
        var reference = owner != null ? RefOf(owner, kept) : null;
        if (reference == null) return "{\"instanceID\":0}";
        if (target is Component part) reference["key"] = Components(owner).FirstOrDefault(pair => pair.Key == part).Value;
        refs.Add(reference);
        return "{\"instanceID\":\"@" + (refs.Count - 1) + "\"}";
    }

    static JObject RefOf(GameObject go, List<GameObject> kept)
    {
        if (origins.TryGetValue(Id(go), out var origin)) return new JObject { ["path"] = PathOf(origin) };
        for (var number = 0; number < kept.Count; number++)
        {
            if (!go.transform.IsChildOf(kept[number].transform)) continue;
            var below = new List<int>();
            for (var transform = go.transform; transform != kept[number].transform; transform = transform.parent)
                below.Insert(0, transform.GetSiblingIndex());
            return new JObject { ["made"] = number, ["below"] = new JArray(below) };
        }
        return null;
    }

    static JObject PathOf(Origin origin) => new JObject { ["scene"] = origin.Scene, ["names"] = new JArray(origin.Names),
        ["indices"] = new JArray(origin.Indices) };

    static void Pose(JObject item, Vector3 position, Quaternion rotation, Vector3 scale)
    {
        item["position"] = new JArray(position.x, position.y, position.z);
        item["rotation"] = new JArray(rotation.x, rotation.y, rotation.z, rotation.w);
        item["scale"] = new JArray(scale.x, scale.y, scale.z);
    }

    static string Folder(string folder)
    {
        if (AssetDatabase.IsValidFolder(folder)) return folder;
        var parent = Path.GetDirectoryName(folder).Replace('\\', '/');
        Folder(parent);
        AssetDatabase.CreateFolder(parent, Path.GetFileName(folder));
        return folder;
    }

    static string FileName(string name, string fallback)
    {
        var clean = new string((name ?? "").Where(c => !Path.GetInvalidFileNameChars().Contains(c)).ToArray()).Trim();
        return clean.Length > 0 ? clean : fallback;
    }

    // --- back in Edit mode: put it in the scene ----------------------------------------------------------------------

    static void Restore()
    {
        if (!File.Exists(PendingFile) || EditorApplication.isPlayingOrWillChangePlaymode) return;
        JObject pending;
        try { pending = JObject.Parse(File.ReadAllText(PendingFile)); }
        catch (Exception ex)
        {
            Debug.LogError("GazeVoice VR couldn't read the edits kept from Play mode: " + ex.Message);
            File.Delete(PendingFile);
            return;
        }
        File.Delete(PendingFile);
        Undo.IncrementCurrentGroup();
        var group = Undo.GetCurrentGroup();
        Undo.SetCurrentGroupName(UndoName);
        var scenes = new HashSet<Scene>();
        var missed = 0;
        // Everything is found by its place before anything moves: adding and deleting objects changes the places.
        var changes = pending["changed"].OfType<JObject>().Select(item => new { item, go = Find(item["path"]),
            parent = item["parent"] is JObject parent && parent["path"] != null ? Find(parent["path"]) : null }).ToList();
        var gone = pending["deleted"].Select(Find).ToList();
        var items = pending["made"].OfType<JObject>().ToList();
        var parents = items.Select(item => item["parent"] is JObject parent ? Find(parent["path"]) : null).ToList();
        var made = new List<GameObject>();
        for (var number = 0; number < items.Count; number++)
        {
            var item = items[number];
            var prefab = AssetDatabase.LoadAssetAtPath<GameObject>((string)item["prefab"]);
            var scene = SceneManager.GetSceneByPath((string)item["scene"]);
            if (!scene.IsValid() || !scene.isLoaded) scene = SceneManager.GetActiveScene();
            if (prefab == null)
            {
                made.Add(null);
                missed++;
                continue;
            }
            var instance = (GameObject)PrefabUtility.InstantiatePrefab(prefab, scene);
            PrefabUtility.UnpackPrefabInstance(instance, PrefabUnpackMode.Completely, InteractionMode.AutomatedAction);
            instance.name = (string)item["name"];
            var transform = instance.transform;
            if (parents[number] != null)
            {
                transform.SetParent(parents[number].transform, false);
                SetLocal(transform, item);
            }
            else
            {
                transform.SetPositionAndRotation(Vector(item["position"]), Rotation(item["rotation"]));
                transform.localScale = Vector(item["scale"]);
            }
            var index = (int)item["index"];
            var siblings = transform.parent != null ? transform.parent.childCount : scene.rootCount;
            if (index >= 0 && index < siblings) transform.SetSiblingIndex(index);
            instance.SetActive((bool)item["active"]);
            Undo.RegisterCreatedObjectUndo(instance, UndoName);
            scenes.Add(scene);
            made.Add(instance);
        }
        foreach (var change in changes)
        {
            if (change.go == null)
            {
                missed++;
                continue;
            }
            try
            {
                Apply(change.item, change.go, change.parent, made);
                scenes.Add(change.go.scene);
            }
            catch (Exception ex)
            {
                missed++;
                Debug.LogWarning("GazeVoice VR couldn't keep the changes to " + change.go.name + ": " + ex.Message);
            }
        }
        foreach (var go in gone)
        {
            if (go == null) continue;
            scenes.Add(go.scene);
            Undo.DestroyObjectImmediate(go);
        }
        foreach (var item in items)
            AssetDatabase.DeleteAsset((string)item["prefab"]);  // they only carried the objects across; the scene has them now
        var folder = (string)pending["folder"];
        if (AssetDatabase.IsValidFolder(folder) && AssetDatabase.FindAssets("", new[] { folder }).Length == 0)
            AssetDatabase.DeleteAsset(folder);
        Undo.CollapseUndoOperations(group);
        foreach (var scene in scenes)
        {
            if (!scene.IsValid()) continue;
            EditorSceneManager.MarkSceneDirty(scene);
            if (!String.IsNullOrEmpty(scene.path)) EditorSceneManager.SaveScene(scene);
        }
        AssetDatabase.SaveAssets();
        var kept = items.Count + changes.Count + gone.Count - missed;
        var message = "GazeVoice kept " + kept + (kept == 1 ? " edit" : " edits") + " from Play mode"
                      + (missed > 0 ? " (" + missed + " couldn't be placed)" : "") + " and saved the scene.";
        Debug.Log(message);
        if (SceneView.lastActiveSceneView != null) SceneView.lastActiveSceneView.ShowNotification(new GUIContent(message), 4);
    }

    static void Apply(JObject item, GameObject go, GameObject parent, List<GameObject> made)
    {
        Undo.RecordObject(go, UndoName);
        go.name = (string)item["name"];
        go.layer = (int)item["layer"];
        try { go.tag = (string)item["tag"]; }
        catch (UnityException) { }
        var transform = go.transform;
        var wanted = item["parent"];
        if (wanted != null)
        {
            var target = wanted.Type == JTokenType.Null ? null
                : parent != null ? parent.transform
                : wanted["made"] != null ? (Resolve((JObject)wanted, made) as GameObject)?.transform : transform.parent;
            if (target != transform.parent) Undo.SetTransformParent(transform, target, UndoName);
        }
        Undo.RecordObject(transform, UndoName);
        SetLocal(transform, item);
        foreach (var entry in item["components"].OfType<JObject>())
        {
            var key = (string)entry["key"];
            var component = ComponentWithKey(go, key);
            if (component == null)
            {
                var type = Type.GetType(key.Substring(0, key.LastIndexOf('#')));
                if (type == null) continue;
                component = Undo.AddComponent(go, type);
                if (component == null) continue;
            }
            var refs = (JArray)entry["refs"];
            var json = Placeholder.Replace((string)entry["json"], match =>
            {
                var target = Resolve((JObject)refs[Int32.Parse(match.Groups[1].Value)], made);
                return "{\"instanceID\":" + (target != null ? GazeVoiceBridge.IdOf(target) : 0) + "}";
            });
            Undo.RecordObject(component, UndoName);
            EditorJsonUtility.FromJsonOverwrite(json, component);
        }
        foreach (var key in item["removed"].Select(token => (string)token))
        {
            var component = ComponentWithKey(go, key);
            if (component != null && !(component is Transform)) Undo.DestroyObjectImmediate(component);
        }
        go.SetActive((bool)item["active"]);
    }

    static Object Resolve(JObject reference, List<GameObject> made)
    {
        GameObject go = null;
        if (reference["path"] != null) go = Find(reference["path"]);
        else if (reference["made"] != null)
        {
            var number = (int)reference["made"];
            go = number >= 0 && number < made.Count ? made[number] : null;
            foreach (var index in reference["below"] ?? new JArray())
            {
                if (go == null || (int)index >= go.transform.childCount) return null;
                go = go.transform.GetChild((int)index).gameObject;
            }
        }
        if (go == null) return null;
        var key = (string)reference["key"];
        return key != null ? (Object)ComponentWithKey(go, key) : go;
    }

    // An object's place: its scene, then the names and sibling numbers from the root down. The numbers pick between
    // objects with the same name; the names catch an object that has moved.
    static GameObject Find(JToken token)
    {
        if (!(token is JObject place)) return null;
        var scene = SceneManager.GetSceneByPath((string)place["scene"]);
        if (!scene.IsValid() || !scene.isLoaded) return null;
        var names = place["names"].Select(name => (string)name).ToArray();
        var indices = place["indices"].Select(index => (int)index).ToArray();
        var level = scene.GetRootGameObjects().Select(go => go.transform).ToList();
        Transform found = null;
        for (var depth = 0; depth < names.Length; depth++)
        {
            var at = depth < indices.Length ? indices[depth] : -1;
            found = at >= 0 && at < level.Count && level[at].name == names[depth] ? level[at]
                : level.FirstOrDefault(transform => transform.name == names[depth]);
            if (found == null) return null;
            level = Enumerable.Range(0, found.childCount).Select(found.GetChild).ToList();
        }
        return found != null ? found.gameObject : null;
    }

    static void SetLocal(Transform transform, JObject item)
    {
        transform.localPosition = Vector(item["position"]);
        transform.localRotation = Rotation(item["rotation"]);
        transform.localScale = Vector(item["scale"]);
    }

    static Vector3 Vector(JToken value) => new Vector3((float)value[0], (float)value[1], (float)value[2]);

    static Quaternion Rotation(JToken value) => new Quaternion((float)value[0], (float)value[1], (float)value[2], (float)value[3]);
}
