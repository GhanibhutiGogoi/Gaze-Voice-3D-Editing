using UnityEditor;
using UnityEngine;

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
