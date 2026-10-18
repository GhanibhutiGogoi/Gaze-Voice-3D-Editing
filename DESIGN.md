---
name: GazeVoice Control Center
description: A studio console and outboard rack for a voice-to-Blender signal chain.
colors:
  desk: "#121314"
  faceplate: "#1c1d20"
  faceplate-raised: "#24262a"
  bevel: "#34363b"
  seam: "#0a0a0b"
  screw: "#0e0f10"
  ink: "#ebe8e1"
  ink-muted: "#aaa69e"
  ink-faint: "#8f8c85"
  tape: "#ebe5d0"
  tape-ink: "#23211c"
  readout: "#0c0d0e"
  readout-ink: "#e4e1d9"
  readout-key: "#8d8a83"
  led-live: "#3ddc8c"
  led-standby: "#f0b43c"
  led-fault: "#ff5f55"
  led-off: "#3b3d42"
  led-busy: "#5cb8ff"
  fault-ink: "#ff8a80"
  key-cap: "#2c2e33"
  key-cap-top: "#34373d"
  key-amber: "#eaa338"
  key-amber-top: "#f4b552"
  key-amber-ink: "#1d1a14"
  focus: "#9ad8ff"
  lens-light: "#ffffff"
  lens-dark: "#000000"
  desk-light: "#dcdbd6"
  faceplate-light: "#efeee9"
  faceplate-raised-light: "#f8f7f3"
  seam-light: "#b7b5ae"
  screw-light: "#c9c7c0"
  ink-light: "#1c1c1a"
  ink-muted-light: "#4f4d48"
  ink-faint-light: "#5f5d57"
  tape-light: "#fffbe6"
  tape-edge-light: "#d9d3bc"
  readout-light: "#1d1f22"
  readout-ink-light: "#ecebe6"
  readout-key-light: "#a5a39c"
  led-live-light: "#1fae69"
  led-standby-light: "#d79316"
  led-fault-light: "#e2453b"
  led-off-light: "#b3b1aa"
  led-busy-light: "#2f8fe0"
  fault-ink-light: "#b42318"
  key-cap-light: "#f1f0ec"
  key-amber-light: "#e59a2b"
  key-amber-top-light: "#f0ab45"
  focus-light: "#1f6fd1"
typography:
  plate:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif"
    fontSize: "18px"
    fontWeight: 700
    letterSpacing: "0.01em"
  heading:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif"
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.2
  ui:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  small:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.4
  data:
    fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace"
    fontSize: "11.5px"
    lineHeight: 1.55
  legend:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', Arial, sans-serif"
    fontSize: "10.5px"
    fontWeight: 600
    letterSpacing: "0.06em"
rounded:
  plate: "3px"
  key: "5px"
  tape: "2px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  key:
    backgroundColor: "{colors.key-cap}"
    textColor: "{colors.ink}"
    rounded: "{rounded.key}"
    padding: "8px 13px"
  key-primary:
    backgroundColor: "{colors.key-amber}"
    textColor: "{colors.tape-ink}"
    rounded: "{rounded.key}"
    padding: "8px 13px"
  scribble-strip:
    backgroundColor: "{colors.tape}"
    textColor: "{colors.tape-ink}"
    rounded: "{rounded.tape}"
    padding: "3px 9px"
  readout:
    backgroundColor: "{colors.readout}"
    textColor: "{colors.readout-ink}"
    rounded: "{rounded.plate}"
    padding: "8px 12px"
---

## Overview

GazeVoice is a signal chain: your voice goes through the mic, speech-to-text, refiner and editor, and ends in Blender. The control center is laid out like a studio desk instead of a grid of status cards:

- **Signal path:** a console routing strip of five stages.
- **Rack:** each component is a unit with a scribble-strip label, a lens LED, a dark readout window and hardware keys.
- **Activity tape:** the log, where long jobs stream line by line.

The scene: an artist in a dim room, with Blender's dark viewport on the main screen, glances at this tab between modeling passes. So dark anodized gear is the designed default. Light mode (when the OS asks for it) becomes the same gear in brushed aluminium.

## Colors

The palette is restrained: neutral faceplates, plus color that only means something.

- **LED colors are state only:**
  - live (green) means Ready
  - standby (amber) means Installed but not live
  - off (unlit grey) means Not installed
  - fault (red) means Problem
  - busy (blue, blinking) means a job is running, or a check that hasn't answered yet ("Checking…")

  LEDs are never decoration.
- **Amber key cap:** reserved for the key that moves the chain forward, such as Install or Check everything. Other keys are plain caps.
- **Readout windows stay dark in both themes**, because displays are dark glass even on silver gear.
- **Light theme values:**

  | Token | Value |
  |---|---|
  | desk | `#dcdbd6` |
  | faceplate | `#efeee9` / `#f8f7f3` |
  | ink | `#1c1c1a` |
  | ink-muted | `#4f4d48` |
  | tape | `#fffbe6` |
  | led-off | `#b3b1aa` |
  | live | `#1fae69` |
  | standby | `#d79316` |
  | fault | `#e2453b` |
  | busy | `#2f8fe0` |

## Typography

- One system UI family carries everything.
- Monospace is used only for measured or machine data: versions, paths, levels, log lines, and readout values.
- Uppercase tracked legends appear only on readout keys (VERSION, LOGIN). Never put them over sections.
- There are exactly six fixed steps, each a CSS token, never fluid:
  - legend 10.5 (`--fs-legend`)
  - data 11.5 (`--fs-data`): mono logs, meters
  - small 12.5 (`--fs-small`): secondary text, keys, readout values
  - ui 14 (`--fs-ui`): body, summaries, tape labels
  - heading 16 (`--fs-heading`): sections, stage names
  - plate 18 (`--fs-plate`): the name plate
- A new size means a new token here first.

## Layout

- **Header strip** (sticky): the name plate, a chain readout ("Workflow ready" or "N stages need attention"), a live-link LED, Refresh, and Check everything.
- **Signal path:** five stage modules in one row, joined by patch wires. A wire lights when the stage upstream of it is ready.
- **Below that:**
  - The rack, with three shelves: Input, Brain and Blender.
  - A side column, sticky below the header, with the Routing unit (settings) above the Activity tape.
- **Breakpoints:**
  - From 1180px down, the side column drops below the rack.
  - From 980px down, a unit's columns stack: label and status, then the readout, then the keys.
  - From 560px down, the margins tighten and the live-link LED hides.

## Elevation & Depth

- **Faceplates:** a light bevel on the top border, a dark seam on the bottom, and a 2px soft drop shadow.
- **Keys:** have physical travel. At rest they show a 2px seam shadow; pressed, they move down 2px.
- **Readouts:** inset, with an inner shadow and a bevel line under them.
- **No floating glass and no glows.** LEDs light through a lens gradient, not a halo.

## Shapes

- Faceplates are nearly square (3px corners), with four screw heads drawn into the plate background.
- Keys have 5px corners.
- Tape labels have 2px corners and sit a fraction of a degree off true, like tape applied by hand.

## Components

- **Rack unit:** tape label plus role; LED plus state word; readout (summary, spec pairs, last result); keys.
- **Consent pattern:** a key whose action needs consent arms on the first press. The readout shows exactly what will happen, the key becomes "Confirm", a Cancel key appears, and it disarms after 8 seconds.
- **Plan tag:** keys that spend Claude or Codex usage carry a small red ring and the text "uses your plan".
- **Busy unit:** the unit's LED blinks blue and its keys are disabled, showing "Working…".
- **Stage module:** LED, stage name, state word, and the parts it depends on. Clicking it highlights those units in the rack.
- **Activity entry:** LED, "Unit · Action", and the time. It expands to a readout of the live log, and failures show their error first.

## Do's and Don'ts

- **Do** let LEDs and state words agree. Color is never the only signal.
- **Do** say what will happen before any install, download or recording.
- **Don't** add decorative icons, gradients on text, glows, or a modal where the arm/confirm pattern works.
- **Don't** use monospace for prose, or uppercase legends as section headings.
