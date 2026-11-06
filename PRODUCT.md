# Product

<!-- impeccable:product-schema 1 -->

Written 2026-10-18 from the planning conversation with the user. Items marked *(inferred)* weren't stated outright. Updated 2026-11-07 to cover gaze in Blender.

## Platform

web

The control center is a local web page. GazeVoice as a whole also includes a Blender add-on and background services on macOS.

## Users

The primary user is the builder: a 3D creator who models in Blender and wants to edit by talking, and by looking in Blender, with no AI chat window open. They also run the setup themselves: installing each component, connecting them, and checking that the workflow is ready. *(inferred)* They are technical, comfortable with Blender, Claude Code and Codex, and on track toward research-style evaluation.

## Product Purpose

Edit Blender scenes by voice, and by eye gaze too. The AI is told what the user is focused on (the selection, or what they were looking at when they said "this" or "there") and edits only that part. The control center makes a multi-part setup visible, installable, connectable and verifiable. That setup is Blender, the official Blender MCP add-on and server, local speech-to-text, the microphone, Claude Code or Codex, our own bridge add-on, and, for gaze, the camera with a face model and a calibration.

Success means every stage of the workflow shows ready, and then a spoken command changes Blender within seconds.

## Positioning

LLM 3D editing today works from the prompt alone. GazeVoice also passes focus context, so edits stay scoped to what the user means. The idea comes from SketchGPT's use of a second, non-verbal channel. There's no chat window, and it runs on the Claude Code or Codex login already on the machine, never asking for an API key.

## Operating Context

- **Hardware:** macOS on Apple Silicon (M4, 16 GB).
- **Screen:** Blender 5.2 LTS fills the main screen. The control center is a browser tab opened to set things up, check status, or fix something.
- **Background:** the voice pipeline runs in the background.
- **Network:** everything is local, on 127.0.0.1.

## Capabilities and Constraints

- **Components:**
  - Claude Code, the first-choice brain
  - Codex, the fallback brain
  - Blender, version 5.1 or newer
  - Blender Lab MCP add-on v1.0.3, on port 9876
  - Blender Lab MCP server
  - GazeVoice Bridge add-on, on port 9877
  - Speech-to-text: Parakeet or Whisper, via MLX
  - Microphone, via ffmpeg
  - Gaze (optional, Blender only): the Mac's camera via ffmpeg, and MediaPipe's face model in its own Python environment
- **Consent:**
  - Installs, downloads, mic recordings, camera use and model calls only happen when the user clicks.
  - Actions marked "uses your plan" spend Claude or Codex usage.
- **Camera:** it's open only while voice is on with gaze turned on, and during a confirmed Calibrate or Test. Frames are processed on the Mac and aren't saved or sent.
- **Pictures:** off by default. A picture of the Blender viewport (never the camera) goes only to Claude Code, and only after a check that it gets cleaned up has passed for the installed Claude Code version.
- **Gaze is rough:** we expect it to tell objects and big parts apart, not small details. When it isn't sure it numbers its guesses, and when it can't help the command uses the selection.
- **No key prompts:** it never asks for an API key.
- **Self-contained:** the server uses only Python's standard library. The page uses no CDN and no web fonts, so it works offline.
- **Scope:** personal use only.
- **Undecided:** the final product name. "GazeVoice" is the working name.

## Brand Commitments

None yet. The user wants it to "feel more like a plugin tool instead of bare bones."

## Evidence on Hand

There are no logos, screenshots or brand assets. Don't invent users, metrics, testimonials or capabilities. Gaze accuracy hasn't been measured on a real face yet, so don't quote a figure for it.

## Product Principles

1. **The 3D work stays center stage.** No chat window. The tool recedes behind Blender.
2. **Show only what's true.** Every status is checked live on this Mac, and nothing claims to be ready unless it was verified.
3. **Nothing happens without consent.** Installs, recordings, camera use and model calls are explicit, and the cost is labeled.
4. **Context beats prompts.** The system knows what the user is focused on.
5. **Gaze is an extra.** It fills the focus slot when it can. When it can't, the command uses the selection.
