# Gaze-Voice-3D-Editing

GazeVoice lets you edit Blender or Unity scenes by talking, using gaze or mouse focus to identify spoken targets.

- **Voice:** your speech is transcribed on your Mac and cleaned up into one clear instruction. Claude Code, Codex or a configured API provider edits the selected host through MCP.
- **Focus:** each command carries what you're focused on, so edits stay scoped to it. That's your selection, or what you were looking at when you said "this" or "there" (see [Use gaze](#use-gaze)).

**Status:** three stages are built:

- **The control center:** a local dashboard that installs each piece, connects them and checks that the whole workflow is ready.
- **The voice loop:** hold to talk in Blender or Unity, say what you want, and release to edit. The chain runs hold-to-talk → speech-to-text → cleanup → editor → one undo step.
- **Gaze in Blender:** look at what you mean and say "this" or "there". It's built, and the latest real calibration (2026-11-09) was typically off by about 23% of the screen, so expect it to be rough. Unity now supports mouse focus in SceneView and can use the same calibrated camera tracker; its real-camera accuracy still needs validation.

See [the design](docs/superpowers/specs/2026-10-18-gazevoice-design.md), [the control center plan](docs/superpowers/plans/2026-10-18-control-center.md), [the voice loop plan](docs/superpowers/plans/2026-10-19-voice-loop.md), [the gaze plan](docs/superpowers/plans/2026-11-06-gaze-blender.md) and [the gaze proposal](docs/gaze/Gaze_Recommended_Solution.md).

## Start the control center

```bash
python3 -m gazevoice            # opens http://127.0.0.1:8977
python3 -m gazevoice --port 9000 --no-browser
```

It needs only macOS on Apple silicon and Python 3.12 or newer. Nothing gets installed just to open it.

## Setting up, unit by unit

Every unit shows its real state on this Mac. Nothing installs, records audio, opens the camera or uses your Claude/Codex plan until you press its key. Keys that need consent ask first, right on the unit.

1. **Blender:** *Install Blender* (Homebrew), then *Launch*. Launch turns on online access, which the MCP add-on needs to start.
2. **Blender MCP add-on:** *Install add-on* downloads Blender Lab's official add-on (v1.0.3). After installing, quit Blender and *Launch* it again.
3. **Blender MCP server:** *Install server* downloads Blender Lab's server and installs its packages with `uv`. *Test* lists its tools and, if Blender is open, reads the scene.
4. **GazeVoice Bridge:** *Install add-on* adds our small Blender add-on (v0.3.4). It gives Blender the hold-to-talk key and a GazeVoice tab in the sidebar, shares your selection, shows a status line in the viewport, and for gaze says what's under a point in the viewport, draws a ring and numbered labels, and shows a see-through bubble where you're looking (see [The marker](#the-marker)). If you installed an older version, the unit says *Update add-on*. Restart Blender after installing or updating.
5. **Speech-to-text:** *Install* sets up Parakeet (or Whisper, chosen in Routing) in its own Python environment and downloads the model. *Test* transcribes a clip spoken by macOS.
6. **Microphone:** *Test* records 3 seconds. If it hears silence, allow the app you started GazeVoice from in System Settings › Privacy & Security › Microphone.
7. **Claude Code / Codex:** detected automatically, using whatever login they already have. GazeVoice never asks for a key for these two. *Test* and *Connect to Blender* each spend a little of your plan.
8. **DeepSeek, Qwen or a custom API** (optional): see [Use DeepSeek, Qwen or another API](#use-deepseek-qwen-or-another-api).
9. **Gaze** (optional, Blender only): *Install gaze* sets up gaze tracking in its own Python environment and downloads about 340 MB. *Calibrate* opens its own page in your browser and *Test* asks first, and the camera turns on only when you start one. See [Use gaze](#use-gaze).
10. **Check everything** runs every test from left to right and lights up the signal path. It doesn't include the Gaze unit.

## Talk to Blender

1. Get every unit ready. Until then, the **Voice** panel lists exactly what's missing.
2. Press **Start voice**. This:
   - connects to Blender;
   - opens the microphone;
   - loads the speech model;
   - starts two Claude Code sessions, a quick one that cleans up what you said and one that edits Blender;
   - if **Gaze** is on Watch only or On with the camera as its source, opens the camera. It stays on until you stop voice, even if you close Blender.

   The first start takes a while because the model loads. Warming up the brain uses a little of your plan.
3. In Blender, select what you mean. Then hold **⌥ Space** (Alt+Space), say it, and let go. For example:
   - "make this twice as tall"
   - "bevel the edges a little"
   - "add an array modifier with five copies"
4. Watch the status line at the bottom of the viewport, or the **GazeVoice** tab in the sidebar (press N). It goes Listening → Transcribing → Understanding → Working → ✓. The dashboard shows each command with:
   - what was heard;
   - what it was cleaned up to;
   - what changed;
   - how long each step took.
5. **What you say gets written up properly.** Ask for something new, like "make a bird model", and the cleanup step writes the editor a detailed brief: real proportions and size, separate named parts, enough geometry and smooth shading, materials, and a tidy group. That way you get a finished-looking model instead of a few basic shapes. Exact edits like "make it red" or "play the animation" go through exactly as you said them. Open **Prompt sent to the editor** under a command to read the brief. A detailed build can take a minute or two; "stop" still ends it at once.
6. Every command that changes the scene becomes **one undo step**, named "GazeVoice: …", so Ctrl+Z works as usual. Commands that only look something up don't add one.

**Saying "stop", "undo" or "redo"** skips the AI:

- **"Stop"** is instant. "Cancel", "wait", "never mind" and "no, stop it" work too. It ends the running edit and drops anything queued. If the edit already changed something, that change is its own undo step.
- **"Undo"** and **"redo"** wait until the edit in progress finishes, so they never land in the middle of one.
- While you're holding the key, Esc cancels. So does switching away from Blender.
- While voice is off, the key does nothing except say so in Blender's status bar.

## Talk to Unity

The Unity target reuses the microphone, speech-to-text, cleanup, command queue and dashboard. It includes its own MCP server and a local GazeVoice Editor bridge.

1. In the control center's Routing unit, choose **Unity**. The rack then shows the Unity units.
2. Press **Set up Unity** on the Unity unit. It asks first, listing what it's about to do, then does whatever is still missing:
   - installs Unity Hub and the Unity CLI with Homebrew;
   - downloads the latest Unity LTS for Apple silicon (several GB);
   - makes a new project at `~/Unity/GazeVoice`, or keeps the one you picked;
   - copies the GazeVoice package into the project's `Packages` folder;
   - opens the project in Unity.

   **You sign in once.** Unity's free Personal license can only be activated by signing in to Unity Hub, so GazeVoice can't do that step. Setup opens Unity Hub as soon as it's installed, so you can sign in and add the Personal license while Unity downloads. If Unity still has no license when it's time to make the project, setup stops and says so. Sign in, press **Set up Unity** again, and it carries on from where it stopped.
3. Start voice. Then, in Unity, select what you mean, hold **⌥ Space**, say it, and let go. Your selection is the focus. Typed commands in the dashboard work too.

The key works anywhere in the Editor, except in text fields, which type it instead. To change it, look for "GazeVoice" in Edit › Shortcuts. **Window › GazeVoice** has a Talk control and shows the status line. Switching away from Unity cancels a hold. The package listens on `127.0.0.1:9878`.

Prefer your own project? **Choose…** on the Unity project unit picks it, and **New project** makes another one. Nothing goes into a project until you press **Set up Unity** or **Add to project**, and **Remove from project** takes the package out again. After you update GazeVoice, the bridge unit offers **Update** if the package changed.

The MCP server is built into GazeVoice, so there's nothing separate to download. To use another stdio Unity MCP server, set **Unity MCP command** and **Unity MCP arguments** (a JSON string array) in Routing. The external server must provide its own Unity scene tools and implement the standard MCP stdio protocol.

**What the editor can do.** Nothing in the Unity Editor is out of reach:

- **`execute_csharp`** runs any C# right away, with no recompile, the way Blender's MCP server runs Python. It uses the compiler inside your Unity editor, so there's nothing extra to install.
- **`write_script`** writes a real script into the project, waits while Unity compiles and reloads, and hands back any compile errors so the editor can fix them.
- **`add_package`** adds what a job needs, such as ProBuilder.
- **`read_console`**, **`get_compile_status`** and **`capture_scene_view`** let the editor check its own work.
- Everyday edits have their own tools: inspecting, selecting, color, transform, duplicate, delete and creating primitives.

For animals, people and other organic shapes, the editor can build the model in Blender, if it's installed, and import it into Unity. *Test* on the bridge unit proves C# really runs in your Unity.

**Undo.** Each command is one undo step, even if scripts recompile partway through. A few things can't be undone by voice: files (scripts, imported models, added packages) stay in the project, and a change made in C# without Unity's Undo calls can't be taken back. External MCP tools that change scenes without Unity's Undo API can't be made undoable either.

**Save often.** Like Blender's MCP server, this runs AI-written code inside Unity without guards. C# can reach your files and the network, and an endless loop hangs Unity, so "stop" can't end it.

### Unity gaze

1. Update the Unity Bridge package from the dashboard (version 0.3.1), then let Unity finish compiling.
2. In Routing choose **Unity**, **Gaze: Watch only**, and **Gaze source: Mouse**. Start or restart voice.
3. Move the pointer over the active **Scene** view. A transparent aqua indicator and object name show the focus. It does not select or change the object. Game view and Hierarchy are not gaze viewports.
4. Keep the pointer over an object while holding the talk key and saying a pointing word, such as “make this twice as tall”. Watch only records the suggested object but edits your existing selection. Compare the command's gaze result with the intended target.
5. Switch Gaze to **On** and restart voice to use the pointer target. When several objects are plausible, numbered labels appear: say a number with PTT, or press **1**, **2** or **3** while the Scene view is focused.
6. With Gaze **On**, look at empty Scene space and say “Make a sofa here”. A forward ray intersecting Unity's Y=0 work plane supplies the new object's ground-contact position even with nothing selected. A nearly horizontal ray, a point behind the camera, or a missing viewport supplies no position. Existing-object edits still require a target or selection.
7. Empty or unavailable gaze otherwise falls back to your selection. If no usable target or selection exists, the pointing edit is refused. Moving the view, changing the scene, or deleting a target invalidates old gaze: point again and repeat the command.

Mouse focus needs neither a camera nor calibration. **Camera** uses the same local tracker and browser calibration as Blender. Start with Watch only; the webcam accuracy limitations below apply, and this Unity integration has not been validated with a real camera session. The camera indicator is a larger bubble. Eye-tracking headsets/Quest runtime input are not connected yet.

Unity hits readable MeshFilter and SkinnedMeshRenderer surfaces without requiring colliders. Hidden, disabled and picking-disabled objects are excluded. Unsupported renderers (for example non-readable meshes or Terrain) conservatively block the ray rather than inventing a surface or selecting through them. Grounding is at GameObject level, with session-scoped IDs to distinguish duplicate names. Complex scenes may increase CPU picking cost. No model usage or camera access is needed to test the Mouse mode.

See the [Unity gaze validation notes](docs/gaze/Unity_Gaze_Validation.md) for verified behavior, test limits and a manual regression checklist.

**More ways to talk in Blender:**

- *Talk* in the sidebar starts listening on the first click and sends on the second.
- The dashboard has *Hold to talk* and a box to **type a command**. Typing is handy for testing without a mic.
- To use a key other than ⌥ Space, change "GazeVoice: Hold to Talk" in Blender's Preferences › Keymap. The dashboard and Blender's hints follow your choice.
- If ⌥ Space does nothing, another app (Alfred, Raycast, a chat app's quick launcher) probably owns that shortcut system-wide. Change one of the two.
- Use the key over the 3D viewport. In Blender's Text Editor or Python Console, ⌥ Space types instead.

**Settings:**

- **New conversation** makes the editor forget earlier commands, so "it" starts fresh.
- In Routing, **Editing** and **Cleanup** each pick their own source:
  - Auto (Claude Code when it's logged in, otherwise Codex);
  - Claude Code;
  - Codex;
  - any API provider you've added.
- For Claude Code, pick a model for each step:
  - the choices are Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5;
  - "Default" lets Claude Code choose (GazeVoice doesn't use your personal Claude Code settings);
  - they need a recent Claude Code, so run `claude update` if a choice fails to start.
- Next to each model, pick how much it **thinks**: Low, Medium, High, Extra high or Max. More thinking can make big builds better, but each command takes longer. Haiku 5.5 keeps Cleanup quick.
- Each command on the dashboard shows which model did the edit. Stop and start voice to apply changes.
- **Codex** works as the brain too, but it's experimental and slower, about 10 seconds or more per command.

**Privacy:**

- The mic is open only while voice is on.
- Audio and command records are saved in GazeVoice's local logs folder.
- The camera is used only by gaze: while voice is on with camera gaze turned on, and while you calibrate or test it. It stays on for as long as voice is on, even if Blender is closed. While it's on, the status line ends in "gaze on" and the Voice panel's note names the camera. See [The camera](#the-camera).
- Pictures of Blender's viewport are off unless you turn them on. See [Pictures](#pictures-optional-off-by-default).
- Claude Code runs with session saving off, so your commands don't appear in its history.
- Codex keeps its own history of the commands it handled.

### What to expect

On an M4 MacBook Air, measured outside Blender:

| Step | Time |
|---|---|
| Speech-to-text, once loaded | well under a second |
| Cleanup (Haiku) | about 2–5 s |
| Editor start (warm-up and connecting to the Blender MCP server) | about 6 s |
| Editor reply for a lookup | a few seconds |

Real edits take longer with bigger models.

## Use gaze

Gaze lets you look at what you mean, say "make this bigger", and skip the selecting. Blender supports object/part grounding and optional pictures. Unity supports SceneView object grounding, a visible focus indicator, numbered clarification and Watch only; optional pictures and mesh-part grounding remain Blender-only. See [Unity gaze](#unity-gaze).

Gaze is new. The latest real calibration (2026-11-09) measured its typical miss at about 23% of the screen. The closer seat, the eyelids and the head-turn correction haven't been tried on a real run yet. Gaze also hasn't been seen picking the right thing in a real spoken session: in the one so far (2026-11-08, five commands) it found nothing, because of a head-position bug that has since been fixed. So what follows about how well it will do is what to expect, not a result.

### Set it up

1. **Update the Bridge.** Press *Update add-on* on the Bridge unit (or *Install add-on*, if it isn't there yet), then restart Blender. Gaze and the marker need Bridge v0.3.4, which draws the marker as a see-through bubble and puts the same bubble on pictures. If you had v0.3.3 or earlier, the unit offers *Update add-on* again: press it and restart Blender. Until you do, voice won't start, and the Voice panel says the Bridge needs updating. Press *Update add-on* whenever the unit offers it.
2. **Install gaze.** Press *Install gaze* on the Gaze unit. It asks first, then sets up its own Python environment and downloads about 340 MB (MediaPipe and its face model). It needs `uv`. Seeing the camera also needs ffmpeg, which the Microphone unit installs.
3. **Calibrate.** Press *Calibrate* on the Gaze unit. It opens its own page in your browser, and Blender doesn't need to be open. If voice is on and using the camera, turn it off first. Press *Start*, then click the circle in the middle of the screen. That is when the camera turns on, and it stays on for about a minute.
   - **A calibration made before this update must be made again.** It was made without allowing for turning your head, so it is refused: the Gaze unit says "Your saved calibration is out of date. Calibrate gaze again.", and voice starts without gaze and says the same.
   - Do this on the screen you use Blender on. The page goes full screen and turns bright, because your screen is part of the light on your face.
   - Sit where you'll work, close to the screen: about 30 cm, and no nearer than 25 or further than 45, with your whole face in view of the camera. Close up, your eyes are big and clear in the picture. If part of your face is out of view, the page says "Move back a little, part of your face is out of view". Gaze learns how your eyes look at the distance you calibrate from, so it works best from about that distance. If you later sit further back, calibrate again. In a computer simulation, calibrating at 30 cm and then sitting 55 cm away made looks near the edges of the screen read up to about 18% of the screen too close to the middle.
   - Keep your head level and fairly still. Sitting close and keeping your head level both help it read you better.
   - First you line your head up. The page shows an outline of a head and shoulders and a ring that is your head, and tells you which way to move: "Move left", "Come closer" and so on. When the ring sits in the outline and you've held still for a second, the dots start by themselves. The ring is drawn from a few numbers about where your head is. The camera's picture stays in the camera program and never reaches the browser.
   - The outline's limits are a first guess, because the closer seat they ask for hasn't been tried on a real run yet. So the page always has a *Start anyway* key that skips this step. If your head isn't lined up within a minute, it stops and turns the camera off.
   - Look at each dot until it moves: there are 24, about a second and a half each. Sixteen teach it, reaching from edge to edge of the screen, and eight more, mixed in among them where no other dot is, check how well it learned. The dots jump around the screen on purpose, so that a head slowly drifting isn't mistaken for your eyes moving.
   - Gaze reads your eyes: where your irises sit, and how open your eyes are, because your eyelids follow your look up and down more than your irises do. It doesn't learn where your head sits or how it's turned, because a calibration made sitting still can't learn that. So shifting a little in your seat afterwards shouldn't push your look off the screen. Turning your head is allowed for by a fixed sum instead: it moves your look by your distance from the screen over the screen's width, times the turn. That needs the screen's size, which macOS gives for a Mac's own screen. If it can't be read, turning your head isn't allowed for, and the page says so.
   - The first time, macOS may ask to allow the camera.
   - Press Esc to stop. Closing or hiding the page stops it too, and the camera turns off.
   - The page then shows how far off it typically was, as a share of the screen, beside what always guessing the middle of the screen would have scored. That's measured on the eight check dots you stared at, so real use will probably be looser. A calibration that's typically off by more than a quarter of the screen isn't saved. Try again.
   - It also says which of your readings it chose to learn from, whether turning your head is allowed for, and how far your head moved during the run. The calibration it saves learns from all 24 dots, after that figure is measured.
   - If it doesn't finish, the page says why, and how many camera frames had no face or had your eyes counted as shut.
4. **Test** (optional). *Test* shows a ring in Blender that follows where you look for about ten seconds.
5. **Start with Watch only.** In Routing, set **Gaze** to **Watch only**, then start voice. These rows are available for both Blender and Unity.

Routing has three gaze settings:

- **Gaze:**
  - **Off** is the default. The camera stays off.
  - **Watch only** records what you were looking at when you said a pointing word and compares it with your selection. Edits land exactly as they do without gaze, and no pictures are taken. The Voice panel shows how often gaze picked what you had selected, and each command there shows what gaze would have picked. So does its `record.json` in the logs folder. Switch to On once it agrees with you often enough to trust.
  - **On** passes what you looked at to the cleanup step and the editor, along with your command.
- **Gaze source:** **Camera** is the default. **Mouse** uses the pointer over Blender's 3D viewport or Unity's active SceneView while you hold the key. It needs no install or calibration, so it's a way to try gaze first.
- **Camera:** Auto uses the Mac's own camera, never a phone's. Pick another camera here.

Stop and start voice after you change any of them.

### The marker

While voice is on with **Gaze** set to Watch only or On and **Gaze source** set to Camera, Blender shows a see-through aqua bubble in the 3D viewport, right where gaze thinks you're looking. It's about 90 points across (about 2 cm on a MacBook's screen), with a soft edge and a faint rim, and your model shows through every part of it. It glides after your eyes, stretches a little when it moves fast, and fades away when gaze loses you.

- **It tells you whether gaze is working.** If it follows your eyes, it is. If it isn't there while you look at the viewport, either gaze isn't tracking you right now, or it puts your eyes well outside the viewport, which means gaze needs calibrating. Check that voice is on and gaze isn't Off, that your face is in the camera's view, and that Blender is on the screen you calibrated on. It also goes away, as it should, when you really look off the viewport or at another screen.
- **In the wrong place?** Then gaze needs calibrating. Press *Calibrate* again, on the screen you use Blender on, and sit where you'll work: gaze works from about the distance you calibrated at, so if you've moved further back, calibrate again from there. The page takes 25 to 45 cm, and about 30 cm is best. Beyond 45 cm the page says "Come closer"; you can press *Start anyway*, but gaze will be less exact from that far. It uses the same readings as "this" and "there", so a marker that's off means those will be off too.
- **It only shows.** It's drawn on top of the viewport and changes nothing in your scene. It never picks anything by itself.
- **Only for the camera.** With Gaze source on Mouse there's no marker, because the pointer already shows where you are.
- **It needs Bridge v0.3.4.** Press *Update add-on* on the Bridge unit, then restart Blender. Until you do, voice won't start, and the Voice panel says the Bridge needs updating.
- **Big scenes.** Each time the marker moves, Blender has to draw the viewport again. In a slow scene, the marker waits after each redraw for as long as that redraw took before it asks for the next one. So it redraws at most about half the time.

### What to expect from gaze

- **Rough, not a mouse.** A webcam is far less exact than a pointer. The latest real calibration (2026-11-09) was typically off by about 23% of the screen, which is several centimetres. The closer seat, the eyelids and the head-turn correction haven't been tried on a real run yet, so it may do better, but that is an expectation, not a measurement.
- **Objects and big parts.** We expect it to tell objects apart, and big parts of a model that have a vertex group or a material to call them by. It can't pick a small detail like one vertex, an edge or a small face.
- **Numbers when it isn't sure.** If two guesses are too close to call, or the best guess is weak, numbered labels appear on the likeliest ones in the viewport and the status line says "Which one? Say the number." A single object under your gaze is used without asking, unless it covers very little of where you looked. To answer, hold the talk key and say "two" within eight seconds. If you're still saying it when the time runs out, it still counts. Say nothing and the command uses your selection. With nothing selected, the command doesn't run and the status line says "Couldn't tell what you meant. Select it or say its name."
- **Instanced objects.** Look at a collection instance and gaze may name the object inside it, not the instance. An edit may then change the source object, and every copy with it.
- **Only pointing words.** "this", "that", "these", "those", "it", "them", "here" and "there" use gaze, and only in a command you speak while holding the talk key. Look at the thing as you say the word. Each pointing word takes its own look, so "move this over there" uses two. A command without a pointing word doesn't use gaze, and if it names something, the name wins.
- **Creating in empty space.** A "here" or "there" fixation over empty space may give a work-plane coordinate for a new object's ground contact: Z=0 in Blender, Y=0 in Unity. The ray must intersect the plane in front of the camera. This is a placement destination, not an existing object; editing "this" still needs an object hit or selection.
- **It falls back, and says so.** If gaze can't help (it couldn't start, lost track of your eyes, found nothing where you looked, or Blender isn't on the screen you calibrated on), the command runs on your selection and the status line says why, such as "Gaze lost, using your selection". If nearly all of what gaze read landed off the screen, it says "Gaze reads off the screen, using your selection. If you were looking at Blender, calibrate gaze again" instead, because that can mean the calibration reads wrong. Each command's `record.json` keeps, for every pointing word, how many readings fell in its window, how many were good enough to use, and how many were off the screen. If gaze couldn't start, the Voice panel says why for as long as voice is on. If one pointing word finds something and another finds empty space, the editor is told that word pointed at nothing. Gaze never stops voice and never edits anything itself. Each edit is still one undo step.
- **Calibrated for one screen.** Move or resize Blender as you like. If you move it to another screen, calibrate on that one. Until you do, voice uses your selection. If you plug in a display that becomes the main display, calibrate again too, because positions are measured from the main display.

### The camera

- **When it's on.** While voice is on with **Gaze** set to Watch only or On and **Gaze source** set to Camera, even if Blender is closed. Also for the length of *Calibrate* (from when you click the circle) or *Test* (once you've confirmed). Those two won't run while voice is using the camera. With Gaze Off, or on Mouse, voice never opens it, and they can run while voice is on. *Test* draws a ring in Blender, and voice doesn't draw over it: if voice would need to ask you to pick a number while a *Test* runs, it uses your selection instead.
- **What stays on this Mac.** Frames are processed on this Mac and aren't saved or sent. GazeVoice reads one at a time, finds where your eyes point and drops it. It keeps only the last 90 seconds of where you looked, as screen positions in memory, plus the part for any command still waiting its turn, your calibration, and the readings from your last calibration run. Those last two are numbers only, and stay on this Mac's disk. With Pictures on, it also keeps the last 20 pictures of Blender's viewport that went to the AI, in memory only, and they are gone when the dashboard stops (see [Pictures](#pictures-optional-off-by-default)).
- **What the last calibration leaves behind.** Each calibration run saves its readings in `gaze-last-run.json`, so a run that goes wrong can be looked into without doing it again. For each frame whose readings were kept, in the order they came, it holds 12 numbers (where your irises sat in your eyes, how open your eyes were, and how your head was turned and placed), which dot that frame went with and whether it was one held back to check the result. With them go when the frame came, MediaPipe's 14 scores for your eyes (how far each looked up, down, in and out, and how much it blinked, squinted or opened wide), the 16 numbers that say how your head was turned and placed, how wide your face was in pixels, how far your face reached toward each edge of the picture, and where the outline and iris of each eye were, as shares of the picture. It also holds how many frames had no face or had your eyes shut, the size and frame rate your camera really sends, the size its frames were scaled to, and when the run was made. A run's file is about 1 MB. For each frame looked at while the head outline was on or a dot's readings were being kept (the first 4,000 at most, which is more than a run takes), it also holds one short line: when the frame came, whether it went with the head outline or which dot, whether a face was found in it (and, if so, whether your eyes were shut), and how bright the frame was. It holds no picture; how bright a frame was is a single number. Each run that gets through the dots replaces the last, whether or not the calibration was saved. A run that stops sooner (you press Esc, the page closes, or the head outline gives up) leaves the file from the run before; the time in it tells you which run it is from. Voice and *Test* never write it. Delete it whenever you like.
- **What leaves.** What you looked at reaches the cleanup step and the editor as text with your command. That text has object and part names, how sure gaze was, a 3D point, face numbers and how far gaze may be off as a share of the viewport. It goes where your commands go, so with an API provider on either step it goes to that company's servers, as your selection does. Command records on this Mac keep what gaze found as names and numbers, and the one-line description of a picture if there was one. They never keep an image.

### Pictures (optional, off by default)

A big part of a model with no vertex group or material has nothing to call it by. With pictures on, gaze takes a closer look at it.

- **What's sent.** A small picture (512 pixels on its long side at most) of just that spot in Blender's 3D viewport, with the gaze bubble drawn on it: a light aqua tint with a clear rim over the area gaze may mean. The live marker itself is never in it. With it goes a short question holding the pointing word you said and the object's name. It's the viewport, never your camera.
- **When.** Only with **Gaze** set to On, never in Watch only. Only when you look at a connected piece of 500 or more faces with nothing to name it. Only after you've set **Pictures** to On in Routing and the check below has passed for the Claude Code you have installed.
- **To whom.** One short-lived Claude Code process, on Haiku 5.5 and with session saving off. It says in a few words what's inside the bubble, then exits. Only those words go on, cut to one short plain line. They join the gaze text, so they reach the cleanup step and the editor, and any API provider on those steps. `record.json` keeps them too. No other step sees a picture, so Codex and API providers behave as before. Pictures go through Claude Code even if another brain does your editing, and need Claude Code logged in.
- **What it costs.** Each picture uses a little of your Claude plan, and adds time to that command. The picture step gets 20 seconds in all. After that, or if anything about it fails, the command goes on with text only.
- **How it's cleaned up.** GazeVoice never saves a picture. Claude Code gets it for one reply, and the check below looks for any copy left behind. Anthropic keeps what's sent under your account's terms. Blender makes the picture in memory, and drops one nobody reads after 30 seconds. The control center passes it straight to Claude Code, and keeps a copy in its memory for the dashboard (see the next point). Pictures never go into logs, command records or the Blender file.
- **What the dashboard keeps.** So you can see what the AI was shown, the dashboard keeps the last 20 pictures that were sent, in its memory. Each command's line in the Voice panel shows a small picture beside what the AI said about it. Click it to see it full size, and press Esc or click again to close it. A picture is never saved to disk, and it isn't in a log, a command record or the Blender file. It's gone once 20 newer ones have been sent, when its command is more than 20 commands old, and when the dashboard stops, and the line then says the picture isn't kept any more. A command record keeps only a note that a picture was sent, with the AI's one line about it.

To turn pictures on, set **Pictures** to On in Routing, then press *Check pictures get cleaned up* on the Gaze unit. It asks first and uses a little of your plan. It sends one small random picture the way a real one is sent, and waits for Claude Code to exit. Then it searches this Mac for a copy. It looks in Claude Code's folder (`~/.claude`), the temp folders, GazeVoice's folder and wider parts of `~/Library` and `~/.local`. If it finds a copy it says where, leaves the file alone (it may be Claude Code's own) and records no pass. If it can't look somewhere it has to, it records no pass either.

**The limits:**

- The check only finds a copy that's unchanged. It can't find one saved in another form, such as a JPEG, a resized picture or a compressed file. It also can't find one stored as text that was broken across lines or had characters changed. It can't read other users' files or places macOS keeps private. In the wider places it reads only what it's allowed to, so a copy kept in one it can't read isn't found. A pass means no plain copy turned up where it could look. It doesn't prove none exists.
- A pass is for one Claude Code version. After `claude update`, pictures turn off by themselves until you check again, and the Gaze unit says so. If a check finds a copy, leave pictures off.

## Use DeepSeek, Qwen or another API

Either step can run on a model you reach with your own API key instead of Claude Code.

1. **Add the key.** In the rack, under Brain, press **Add API key** on DeepSeek or Qwen, paste your key and press Save.
   - Qwen also asks for your account's region. Alibaba keys only work in their own region. If Model Studio gives you a Workspace ID, add it too; regions without a shared address need it.
   - For any other service, press **Add provider** on Custom provider. Give it a name, its address and your key. The address must be an OpenAI-compatible endpoint: the part before `/chat/completions`, such as `https://…/v1`.
2. **Pick it in Routing.** Set Editing, Cleanup or both to that provider.
   - Pick a model. The list comes from the provider; if it doesn't publish one, type the model name.
   - DeepSeek and Qwen also get a thinking menu. Cleanup starts with thinking Off, so it stays quick.
   - Editing needs a model that can call tools; Cleanup doesn't.
3. **Test** on the provider checks your key and the models you picked, for a fraction of a cent. Then press Start voice.

Good to know:

- **The key goes to your Mac's Keychain** (service "GazeVoice"). The dashboard shows only its last four characters. **Remove** deletes it, and any step using that provider goes back to Auto.
- **Your commands go to that provider:** with an API provider, what you say, what's selected and the scene's object names go to that company's servers. The Voice panel says which one.
- **Errors say what to do:** a rejected key, an empty account, a model that can't call tools or a provider that can't be reached each get their own message.

## Where things live

| What | Where |
|---|---|
| Settings, downloads, the MCP server, the speech environment | `~/Library/Application Support/GazeVoice/` (override with `GAZEVOICE_HOME`) |
| The last good microphone test | `…/GazeVoice/state/microphone.json` (delete it to make the mic show "Installed" again) |
| Speech models | `~/.cache/huggingface/` (or `$HF_HOME` if you set it) |
| Spoken commands (audio.wav + record.json each) | `…/GazeVoice/logs/commands/<time>-<n>/` |
| The last 20 pictures sent to the AI | Nowhere on disk. The dashboard holds them in memory, and they're gone when it stops |
| Blender add-ons | Blender's `extensions/user_default/` (`mcp`, `gazevoice_bridge`) |
| Gaze's Python environment | `…/GazeVoice/venvs/gaze/` |
| Gaze's face model, your calibration and the last calibration's readings (both numbers only), and the picture check | `…/GazeVoice/state/` (`face_landmarker.task`, `gaze-calibration.json`, `gaze-last-run.json`, `gaze-picture-check.json`) |
| API keys for DeepSeek, Qwen and custom providers | macOS Keychain, service "GazeVoice" |

## Good to know

- **Local only:** the dashboard listens on 127.0.0.1 only. Requests from other sites are refused.
- **Save often:** Blender's MCP server runs AI-written Python inside Blender without guards.
- **Personal use:** running on your own Claude login is fine for you. Anthropic doesn't allow products for other people to use claude.ai logins without approval.

## Develop

```bash
python3 -m unittest discover -s tests -t . -v
```

| Path | What's there |
|---|---|
| `gazevoice/` | The control center (Python standard library only) |
| `gazevoice/components/` | One file per unit |
| `gazevoice/voice/` | The voice loop: mic ring buffer, speech service, brain sessions, prompts, orchestration |
| `gazevoice/server/static/` | The dashboard: plain HTML, CSS and JS |
| `blender_addon/gazevoice_bridge/` | Our Blender add-on |
| `unity_package/` | Unity Editor package and the matching local bridge protocol |
| `DESIGN.md` | The dashboard's visual system |
| `PRODUCT.md` | What the product is for |
