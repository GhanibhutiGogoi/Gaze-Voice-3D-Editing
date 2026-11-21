using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEditor.XR.Management;
using UnityEditor.XR.Management.Metadata;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.XR.Management;
using UnityEngine.XR.OpenXR;
using UnityEngine.XR.OpenXR.Features;
using UnityEngine.XR.OpenXR.Features.Interactions;
using Object = UnityEngine.Object;

// The Unity VR starting point: a flat plane to build on and the VR template's XR Origin, with its controllers, moving,
// turning and teleporting, set up for a Meta Quest Pro over Link with eye tracking. It is a scene of its own, so the rest
// of the project is left alone. GazeVoice runs it once when it makes a Unity VR project (-executeMethod
// GazeVoiceVRSetup.Batch), and the dashboard can run it again: an existing scene is opened, never rebuilt.
public static class GazeVoiceVRSetup
{
    internal const string ScenePath = "Assets/Scenes/GazeVoice VR.unity";
    const string TemplateScene = "Assets/Scenes/BasicScene.unity";
    const string TemplateFloor = "Plane";
    const string RigPrefab = "XR Origin (XR Rig)";
    const string OpenLater = "Library/GazeVoice/open-vr-scene";
    const string OpenXRLoaderType = "UnityEngine.XR.OpenXR.OpenXRLoader";
    const string OculusLoaderType = "Unity.XR.Oculus.OculusLoader";
    const float FloorScale = 10f;  // a built-in plane is 10 m across, so the floor is 100 m by 100 m
    static readonly BuildTargetGroup Windows = BuildTargetGroup.Standalone;

    public static void Batch()
    {
        var result = Prepare(false);
        Debug.Log("GazeVoice VR setup: " + Newtonsoft.Json.JsonConvert.SerializeObject(result));
        if (!Ok(result))
        {
            EditorApplication.Exit(1);
            return;
        }
        Directory.CreateDirectory(Path.GetDirectoryName(OpenLater));
        File.WriteAllText(OpenLater, ScenePath);
    }

    static bool Ok(object result) => (bool)result.GetType().GetProperty("ok").GetValue(result);

    internal static object Prepare(bool open)
    {
        var notes = new List<string>();
        try
        {
            if (open && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
                return new { ok = false, error = "Save or discard the open scene's changes first.", notes };
            var xr = ConfigureXR(notes);
            var made = !File.Exists(ScenePath);
            if (made) BuildScene(notes);
            else if (open) EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            AddToBuild();
            AssetDatabase.SaveAssets();
            return new { ok = true, scene = ScenePath, made, xr, notes };
        }
        catch (Exception ex)
        {
            Debug.LogException(ex);
            return new { ok = false, error = ex.Message, notes };
        }
    }

    // A project made by Batch opens on the VR scene the first time, instead of the template's sample.
    [InitializeOnLoadMethod]
    static void OnLoad()
    {
        if (Application.isBatchMode) return;
        EditorApplication.delayCall += () =>
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode) return;
            Clean();
            if (!File.Exists(OpenLater)) return;
            File.Delete(OpenLater);
            if (File.Exists(ScenePath) && !SceneManager.GetActiveScene().isDirty)
                EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
        };
    }

    // --- XR: OpenXR on Windows, with eye gaze and the Quest controllers ---------------------------------------------

    static object ConfigureXR(List<string> notes)
    {
        if (!EditorBuildSettings.TryGetConfigObject(XRGeneralSettings.k_SettingsKey, out XRGeneralSettingsPerBuildTarget perTarget)
            || perTarget == null)
        {
            Directory.CreateDirectory("Assets/XR");
            perTarget = ScriptableObject.CreateInstance<XRGeneralSettingsPerBuildTarget>();
            AssetDatabase.CreateAsset(perTarget, "Assets/XR/XRGeneralSettingsPerBuildTarget.asset");
            EditorBuildSettings.AddConfigObject(XRGeneralSettings.k_SettingsKey, perTarget, true);
        }
        if (!perTarget.HasManagerSettingsForBuildTarget(Windows)) perTarget.CreateDefaultManagerSettingsForBuildTarget(Windows);
        var general = perTarget.SettingsForBuildTarget(Windows);
        general.InitManagerOnStart = true;
        EditorUtility.SetDirty(general);
        var manager = general.AssignedSettings;
        if (manager.activeLoaders.Any(loader => loader != null && loader.GetType().FullName == OculusLoaderType))
        {
            XRPackageMetadataStore.RemoveLoader(manager, OculusLoaderType, Windows);  // it has no eye gaze: OpenXR does
            notes.Add("Turned off the Oculus XR plug-in for Windows, so OpenXR runs the headset.");
        }
        if (!manager.activeLoaders.Any(loader => loader != null && loader.GetType().FullName == OpenXRLoaderType)
            && !XRPackageMetadataStore.AssignLoader(manager, OpenXRLoaderType, Windows))
            notes.Add("Couldn't turn on OpenXR for Windows. Turn it on in Project Settings > XR Plug-in Management.");
        EditorUtility.SetDirty(manager);

        var repaired = Clean();
        UnityEditor.XR.OpenXR.Features.FeatureHelpers.RefreshFeatures(Windows);
        var settings = OpenXRSettings.GetSettingsForBuildTargetGroup(Windows);
        var enabled = new List<string>();
        if (settings != null)
        {
            Enable<EyeGazeInteraction>(settings, enabled);
            Enable<MetaQuestTouchProControllerProfile>(settings, enabled);
            Enable<OculusTouchControllerProfile>(settings, enabled);
            EditorUtility.SetDirty(settings);
        }
        else notes.Add("OpenXR has no settings for Windows yet. Open Project Settings > XR Plug-in Management > OpenXR once.");
        PlayerSettings.runInBackground = true;  // the headset keeps playing while another window has the focus
        return new { loaders = manager.activeLoaders.Where(loader => loader != null).Select(loader => loader.name).ToArray(),
            features = enabled, repaired };
    }

    static void Enable<T>(OpenXRSettings settings, List<string> enabled) where T : OpenXRFeature
    {
        var feature = settings.GetFeature<T>();
        if (feature == null) return;
        feature.enabled = true;
        EditorUtility.SetDirty(feature);
        enabled.Add(typeof(T).Name);
    }

    // Strays, lost scripts, then extra copies; saved, so the next session reads the same file.
    static int Clean()
    {
        var changed = StripStrays() + RepairFeatures() + Dedupe();
        if (changed == 0) return 0;
        UnityEditor.XR.OpenXR.Features.FeatureHelpers.RefreshFeatures(Windows);
        AssetDatabase.SaveAssets();
        return changed + StripStrays();
    }

    // The VR template's settings file also holds whole extra copies of the features, and a second Standalone settings,
    // under the same names. OpenXR's editor takes one copy per name in whatever order the file loads, so eye gaze turned
    // on in one session can be swapped for a turned-off copy in the next. Only the copies the live settings use are kept.
    static int Dedupe()
    {
        var settings = OpenXRSettings.GetSettingsForBuildTargetGroup(Windows);
        var path = settings != null ? AssetDatabase.GetAssetPath(settings) : null;
        var package = String.IsNullOrEmpty(path) ? null : AssetDatabase.LoadMainAssetAtPath(path);
        var values = package != null ? new SerializedObject(package).FindProperty("Values") : null;
        if (values == null || !values.isArray) return 0;
        var live = new HashSet<OpenXRSettings>();
        for (var i = 0; i < values.arraySize; i++)
            if (values.GetArrayElementAtIndex(i).objectReferenceValue is OpenXRSettings each) live.Add(each);
        if (!live.Contains(settings)) return 0;
        var used = new HashSet<OpenXRFeature>(live.SelectMany(each => each.GetFeatures()).Where(feature => feature != null));
        var removed = 0;
        foreach (var item in AssetDatabase.LoadAllAssetsAtPath(path))
        {
            var extra = item is OpenXRSettings copy ? !live.Contains(copy) : item is OpenXRFeature feature && !used.Contains(feature);
            if (!extra) continue;
            AssetDatabase.RemoveObjectFromAsset(item);
            Object.DestroyImmediate(item, true);
            removed++;
        }
        if (removed == 0) return 0;
        foreach (var each in live) EditorUtility.SetDirty(each);
        Debug.Log("GazeVoice VR: removed " + removed + " extra copies from the OpenXR settings.");
        return removed;
    }

    // Some sessions can't load the stray copies at all and others load them as features, so setup cuts them from the file.
    static int StripStrays()
    {
        var settings = OpenXRSettings.GetSettingsForBuildTargetGroup(Windows);
        var path = settings != null ? AssetDatabase.GetAssetPath(settings) : null;
        if (String.IsNullOrEmpty(path) || !File.Exists(path)) return 0;
        var text = File.ReadAllText(path);
        var kept = new StringBuilder();
        var removed = 0;
        foreach (var block in Regex.Split(text, "(?=^--- !u!)", RegexOptions.Multiline))
        {
            var anchor = Regex.Match(block, "^--- !u!114 &(-?\\d+)");
            if (anchor.Success && block.Contains("m_Script: {fileID: 0}")
                && !Regex.IsMatch(text, "fileID: " + Regex.Escape(anchor.Groups[1].Value) + "\\b"))
            {
                removed++;
                continue;
            }
            kept.Append(block);
        }
        if (removed == 0) return 0;
        File.WriteAllText(path, kept.ToString());
        AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceUpdate | ImportAssetOptions.ForceSynchronousImport);
        return removed;
    }

    // The VR template's OpenXR settings file holds stray copies of some features, saved without their script. OpenXR
    // reads every feature in the file, and when a stray copy wins it finds no folder for it, takes every loader library
    // to belong to it, and Play fails with "No OpenXR loader library found". Stray copies are removed; a copy that is in
    // use is pointed back at its script.
    static int RepairFeatures()
    {
        var all = new[] { BuildTargetGroup.Standalone, BuildTargetGroup.Android }
            .Select(OpenXRSettings.GetSettingsForBuildTargetGroup).Where(settings => settings != null).ToArray();
        var path = all.Length > 0 ? AssetDatabase.GetAssetPath(all[0]) : null;
        if (String.IsNullOrEmpty(path)) return 0;
        var used = new HashSet<OpenXRFeature>(all.SelectMany(settings => settings.GetFeatures()).Where(feature => feature != null));
        var repaired = 0;
        MonoScript[] scripts = null;
        foreach (var feature in AssetDatabase.LoadAllAssetsAtPath(path).OfType<OpenXRFeature>().ToArray())
        {
            if (feature == null || MonoScript.FromScriptableObject(feature) != null) continue;
            repaired++;
            if (!used.Contains(feature))
            {
                AssetDatabase.RemoveObjectFromAsset(feature);
                Object.DestroyImmediate(feature, true);
                continue;
            }
            var type = feature.GetType();
            scripts = scripts ?? MonoImporter.GetAllRuntimeMonoScripts();
            var script = scripts.FirstOrDefault(item => item != null && item.GetClass() == type);
            if (script == null) continue;
            var serialized = new SerializedObject(feature);
            serialized.FindProperty("m_Script").objectReferenceValue = script;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }
        if (repaired == 0) return 0;
        foreach (var settings in all) EditorUtility.SetDirty(settings);
        Debug.Log("GazeVoice VR: repaired " + repaired + " OpenXR features that had lost their script.");
        return repaired;
    }

    // --- the scene ------------------------------------------------------------------------------------------------

    static void BuildScene(List<string> notes)
    {
        Folders(Path.GetDirectoryName(ScenePath).Replace('\\', '/'));
        Scene scene;
        GameObject floor = null;
        if (File.Exists(TemplateScene) && AssetDatabase.CopyAsset(TemplateScene, ScenePath))
        {
            scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            floor = scene.GetRootGameObjects().FirstOrDefault(go => go.name == TemplateFloor);
        }
        else
        {
            scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            var sun = new GameObject("Directional Light").AddComponent<Light>();
            sun.type = LightType.Directional;
            sun.shadows = LightShadows.Soft;
            sun.transform.rotation = Quaternion.Euler(50f, -30f, 0f);
            var managerType = FindType("UnityEngine.XR.Interaction.Toolkit.XRInteractionManager");
            if (managerType != null) new GameObject("XR Interaction Manager").AddComponent(managerType);
            var rig = FindRig();
            if (rig != null) PrefabUtility.InstantiatePrefab(rig, scene);
            else notes.Add("No XR Origin prefab: import the XR Interaction Toolkit's Starter Assets, then add an XR Origin.");
        }
        if (floor == null)
        {
            floor = GameObject.CreatePrimitive(PrimitiveType.Plane);
            floor.name = TemplateFloor;
            floor.transform.localScale = new Vector3(FloorScale, 1f, FloorScale);
            GameObjectUtility.SetStaticEditorFlags(floor, StaticEditorFlags.ContributeGI | StaticEditorFlags.BatchingStatic);
            SceneManager.MoveGameObjectToScene(floor, scene);
            floor.transform.SetSiblingIndex(0);
        }
        Teleport(floor, notes);
        if (!EditorSceneManager.SaveScene(scene, ScenePath)) throw new IOException("Couldn't save " + ScenePath);
    }

    static GameObject FindRig()
    {
        return AssetDatabase.FindAssets("t:Prefab " + RigPrefab.Split(' ')[0])
            .Select(AssetDatabase.GUIDToAssetPath)
            .Where(path => Path.GetFileNameWithoutExtension(path) == RigPrefab)
            .Select(AssetDatabase.LoadAssetAtPath<GameObject>)
            .FirstOrDefault(prefab => prefab != null);
    }

    // The template's plane already lets the controllers teleport. A plane made here gets the same component, which reaches
    // the rig's teleport ray through its interaction layer; an empty collider list makes it use the plane's own collider.
    static void Teleport(GameObject floor, List<string> notes)
    {
        if (floor.GetComponents<Component>().Any(item => item != null && item.GetType().Name == "TeleportationArea")) return;
        var type = FindType("UnityEngine.XR.Interaction.Toolkit.Locomotion.Teleportation.TeleportationArea")
                   ?? FindType("UnityEngine.XR.Interaction.Toolkit.TeleportationArea");
        if (type == null)
        {
            notes.Add("The XR Interaction Toolkit has no teleport area, so the plane can't be teleported on.");
            return;
        }
        var serialized = new SerializedObject(floor.AddComponent(type));
        var colliders = serialized.FindProperty("m_Colliders");
        if (colliders != null) colliders.ClearArray();
        var bits = serialized.FindProperty("m_InteractionLayers.m_Bits");
        if (bits != null) bits.longValue = TeleportLayer();
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    static long TeleportLayer()
    {
        var mask = FindType("UnityEngine.XR.Interaction.Toolkit.InteractionLayerMask");
        var getMask = mask?.GetMethod("GetMask", new[] { typeof(string[]) });
        var value = getMask != null ? Convert.ToInt64(getMask.Invoke(null, new object[] { new[] { "Teleport" } })) : 0;
        return value != 0 ? (uint)value : 1u << 31;  // the Starter Assets put Teleport on the last layer
    }

    static void AddToBuild()
    {
        var scenes = EditorBuildSettings.scenes.Where(scene => scene.path != ScenePath).ToList();
        scenes.Insert(0, new EditorBuildSettingsScene(ScenePath, true));
        EditorBuildSettings.scenes = scenes.ToArray();
    }

    static void Folders(string path)
    {
        if (AssetDatabase.IsValidFolder(path)) return;
        var parent = Path.GetDirectoryName(path).Replace('\\', '/');
        Folders(parent);
        AssetDatabase.CreateFolder(parent, Path.GetFileName(path));
    }

    internal static Type FindType(string fullName)
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            var type = assembly.GetType(fullName, false);
            if (type != null) return type;
        }
        return null;
    }
}
