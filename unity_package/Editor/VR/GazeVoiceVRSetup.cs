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
using UnityEditorInternal;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;
using UnityEngine.XR.Management;
using UnityEngine.XR.OpenXR;
using UnityEngine.XR.OpenXR.Features;
using UnityEngine.XR.OpenXR.Features.Interactions;
using Object = UnityEngine.Object;

// The Unity VR starting point: a terrain to build on and the VR template's XR Origin, with its controllers, moving,
// turning and teleporting, set up for a Meta Quest Pro over Link with eye tracking. It is a scene of its own, so the rest
// of the project is left alone. GazeVoice runs it once when it makes a Unity VR project (-executeMethod
// GazeVoiceVRSetup.Batch), and the dashboard can run it again: an existing scene is opened, never rebuilt.
public static class GazeVoiceVRSetup
{
    internal const string ScenePath = "Assets/Scenes/GazeVoice VR.unity";
    const string Folder = "Assets/GazeVoice/VR";
    const string TemplateScene = "Assets/Scenes/BasicScene.unity";
    const string TemplateFloor = "Plane";
    const string RigPrefab = "XR Origin (XR Rig)";
    const string OpenLater = "Library/GazeVoice/open-vr-scene";
    const string OpenXRLoaderType = "UnityEngine.XR.OpenXR.OpenXRLoader";
    const string OculusLoaderType = "Unity.XR.Oculus.OculusLoader";
    const string URPTerrainMaterial = "Packages/com.unity.render-pipelines.universal/Runtime/Materials/TerrainLit.mat";
    const float Size = 200f, Height = 30f, Ground = 0.25f;  // Ground: the spawn's height, as a share of Height
    const float FlatRadius = 14f, HillsFrom = 45f, RimFrom = 70f;
    const int Resolution = 257, GrassSize = 256;
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
        Folders(Folder);
        Folders(Path.GetDirectoryName(ScenePath).Replace('\\', '/'));
        Scene scene;
        Component teleport = null;
        if (File.Exists(TemplateScene) && AssetDatabase.CopyAsset(TemplateScene, ScenePath))
        {
            scene = EditorSceneManager.OpenScene(ScenePath, OpenSceneMode.Single);
            var floor = scene.GetRootGameObjects().FirstOrDefault(go => go.name == TemplateFloor);
            if (floor != null)
            {
                teleport = floor.GetComponents<Component>().FirstOrDefault(item => item != null && item.GetType().Name == "TeleportationArea");
                if (teleport != null) ComponentUtility.CopyComponent(teleport);
                Object.DestroyImmediate(floor);
            }
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
        var terrain = MakeTerrain();
        SceneManager.MoveGameObjectToScene(terrain, scene);
        terrain.transform.SetSiblingIndex(0);
        Teleport(terrain, teleport != null, notes);
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

    // The template's floor lets the controllers teleport; the terrain takes its place, so it gets the same component,
    // which reaches the rig's teleport ray through its interaction layer. An empty collider list makes it use the
    // terrain's own collider.
    static void Teleport(GameObject terrain, bool copied, List<string> notes)
    {
        Component area = null;
        if (copied && ComponentUtility.PasteComponentAsNew(terrain))
            area = terrain.GetComponents<Component>().FirstOrDefault(item => item != null && item.GetType().Name == "TeleportationArea");
        if (area == null)
        {
            var type = FindType("UnityEngine.XR.Interaction.Toolkit.Locomotion.Teleportation.TeleportationArea")
                       ?? FindType("UnityEngine.XR.Interaction.Toolkit.TeleportationArea");
            if (type == null)
            {
                notes.Add("The XR Interaction Toolkit has no teleport area, so the terrain can't be teleported on.");
                return;
            }
            area = terrain.AddComponent(type);
            copied = false;
        }
        var serialized = new SerializedObject(area);
        var colliders = serialized.FindProperty("m_Colliders");
        if (colliders != null) colliders.ClearArray();
        var bits = serialized.FindProperty("m_InteractionLayers.m_Bits");
        if (!copied && bits != null) bits.longValue = TeleportLayer();
        serialized.ApplyModifiedPropertiesWithoutUndo();
    }

    static long TeleportLayer()
    {
        var mask = FindType("UnityEngine.XR.Interaction.Toolkit.InteractionLayerMask");
        var getMask = mask?.GetMethod("GetMask", new[] { typeof(string[]) });
        var value = getMask != null ? Convert.ToInt64(getMask.Invoke(null, new object[] { new[] { "Teleport" } })) : 0;
        return value != 0 ? (uint)value : 1u << 31;  // the Starter Assets put Teleport on the last layer
    }

    static GameObject MakeTerrain()
    {
        var data = new TerrainData { heightmapResolution = Resolution };
        data.size = new Vector3(Size, Height, Size);
        data.SetHeights(0, 0, Heights());
        data.terrainLayers = new[] { Grass() };
        AssetDatabase.CreateAsset(data, Folder + "/Terrain.asset");
        var go = Terrain.CreateTerrainGameObject(data);
        go.name = "Terrain";
        go.transform.position = new Vector3(-Size / 2, -Ground * Height, -Size / 2);  // the spawn is at the origin, on the ground
        var terrain = go.GetComponent<Terrain>();
        var material = TerrainMaterial();
        if (material != null) terrain.materialTemplate = material;
        GameObjectUtility.SetStaticEditorFlags(go, StaticEditorFlags.ContributeGI | StaticEditorFlags.BatchingStatic);
        return go;
    }

    // Flat where the person starts, low hills around that, and a rim that rises toward the edge.
    static float[,] Heights()
    {
        var heights = new float[Resolution, Resolution];
        for (var row = 0; row < Resolution; row++)
        for (var column = 0; column < Resolution; column++)
        {
            var x = column / (Resolution - 1f) * Size - Size / 2;
            var z = row / (Resolution - 1f) * Size - Size / 2;
            var hills = (Mathf.PerlinNoise(x * 0.018f + 31.7f, z * 0.018f + 12.3f) - 0.5f) * 0.32f
                        + (Mathf.PerlinNoise(x * 0.07f + 5.1f, z * 0.07f + 77.9f) - 0.5f) * 0.06f;
            var away = Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(FlatRadius, HillsFrom, Mathf.Sqrt(x * x + z * z)));
            var rim = Mathf.SmoothStep(0f, 1f, Mathf.InverseLerp(RimFrom, Size / 2, Mathf.Max(Mathf.Abs(x), Mathf.Abs(z)))) * 0.45f;
            heights[row, column] = Mathf.Clamp01(Ground + hills * away + rim);
        }
        return heights;
    }

    static TerrainLayer Grass()
    {
        var path = Folder + "/Grass.png";
        var texture = new Texture2D(GrassSize, GrassSize, TextureFormat.RGB24, false);
        var pixels = new Color[GrassSize * GrassSize];
        for (var y = 0; y < GrassSize; y++)
        for (var x = 0; x < GrassSize; x++)
        {
            var broad = Tiling(x, y, 0.035f, 3.1f);
            var fine = Tiling(x, y, 0.21f, 17.5f);
            var shade = 0.78f + 0.3f * broad + 0.16f * (fine - 0.5f);
            pixels[y * GrassSize + x] = new Color(0.24f * shade, 0.42f * shade, 0.16f * shade);
        }
        texture.SetPixels(pixels);
        File.WriteAllBytes(path, texture.EncodeToPNG());
        Object.DestroyImmediate(texture);
        AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceSynchronousImport);
        var layer = new TerrainLayer { diffuseTexture = AssetDatabase.LoadAssetAtPath<Texture2D>(path), tileSize = new Vector2(8f, 8f) };
        AssetDatabase.CreateAsset(layer, Folder + "/Grass.terrainlayer");
        return layer;
    }

    // Noise that repeats every GrassSize pixels, so the tiles join without a seam.
    static float Tiling(int x, int y, float scale, float seed)
    {
        float Sample(float u, float v) => Mathf.PerlinNoise(u * scale + seed, v * scale + seed);
        float s = x / (float)GrassSize, t = y / (float)GrassSize;
        return Sample(x, y) * (1 - s) * (1 - t) + Sample(x - GrassSize, y) * s * (1 - t)
               + Sample(x, y - GrassSize) * (1 - s) * t + Sample(x - GrassSize, y - GrassSize) * s * t;
    }

    static Material TerrainMaterial()
    {
        var pipeline = GraphicsSettings.currentRenderPipeline;
        if (pipeline != null)
        {
            try
            {
                if (pipeline.GetType().GetProperty("defaultTerrainMaterial")?.GetValue(pipeline) is Material material) return material;
            }
            catch (Exception) { }
        }
        return AssetDatabase.LoadAssetAtPath<Material>(URPTerrainMaterial);
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
