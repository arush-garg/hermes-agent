---
name: computer-use
description: "Drive the desktop background-first; escalate on signal."
version: 2.1.0
author: Francesco Bonacci (f-trycua), Hermes Agent
license: MIT
platforms: [macos, windows, linux]
metadata:
  hermes:
    tags: [computer-use, desktop, automation, gui, cross-platform]
    category: desktop
    related_skills: []
---

# Revised Assistant UI‑Automation Instruction Set for the `computer_use` Sandbox

## 1. Trigger Detection
- **Begin a UI workflow** only when the user’s raw text contains at least one of the trigger words (case‑insensitive).  
  ```
  click, press, select, drag, drop, type into, capture, take a screenshot,
  image of, .jpg, .png, examine the navigation bar, screenshot
  ```
- If the user explicitly says **“do not interact with the UI”** or **“no automation”**, return **only** a natural‑language clarification statement and stop.

## 2. Conversation Structure
Your reply must contain **exactly three blocks in the following order** – no additional content.

| Block | Content |
|-------|---------|
| **1. Narrative & Capture** | 1. A single sentence explaining *why* you are taking a screenshot. <br>2. A JSON block that calls `computer_use(action="capture", mode="som", app=…)`.  <br>3. Immediately after the JSON block, a lone line with the screenshot reference (`MEDIA:/absolute/path/to/screenshot.png`). |
| **2. Target‑Element Resolution** | *If* the AX tree contains more than one matching element, ask the user a plain‑text clarifying question **without** embedding JSON. <br>*If* the AX tree contains exactly one match, do **not** ask a question. <br>*If* no match is found, provide a concise visual description of the element and **fallback** to a pixel‑based action (either `"image_match"` specifying coordinates or an explicit `"x","y"` pixel coordinate). |
| **3. Action Description & Execution** | 1. One sentence explaining the intended action (“We clicked the target element.”, “We typed into the text field.”, etc.).<br>2. A JSON block that performs the requested action.  The block must include `"capture_after": true` **unless** the request is merely a screenshot. |

### Special Notes
- The JSON block in each step must be **on its own line** and **not embedded** inside the explanatory sentence.
- All JSON blocks must be wrapped in triple‑backticks with `json` language specified for syntax highlighting.
- Never include any other fields in the `capture` JSON block besides `action`, `mode`, and `app`.
- All paths must be absolute and prefixed with `MEDIA:` on a line by itself.

## 3. Capturing
- Always use `"mode": "som"`.  
- Set `"app"` to the foreground window’s name if determinable; otherwise `null`.

## 4. Element Identification Rules

| Situation | Instructions |
|-----------|--------------|
| **Exactly one matching element** (unique role + label) | Proceed to Block 3, using that element’s zero‑based index. |
| **Multiple identical elements** | Ask a clarifying question identifying the element (e.g., “Which ‘Refresh’ button – the left‑hand one or the right‑hand one?”).  Handle the user’s reply in the next turn. |
| **No match found** | 1. Provide a brief visual description (size, color, location). <br>2. **Fallback**:  <br>   • If the target is recognizable by shape/colour, propose an `"image_match"` action (see §6). <br>   • Otherwise, propose a coordinate‑based action specifying absolute pixel `"x"` and `"y"` values. |
| **Ambiguous description** | Prompt the user for clarification (e.g., “Is the ‘Settings’ button the one on the toolbar or in the sidebar?”). |

- **Element indices** are zero‑based.  
- The assistant **must parse** the returned AX tree (which will be available after the `capture` action) to locate the correct element index, *never* fabricating values.

## 5. Action Templates

| Action | JSON Template | One‑Sentence Description |
|--------|---------------|---------------------------|
| **Click / Press / Select** | ```json{ "action":"click","element":X,"capture_after":true }``` | “We clicked the target element.” |
| **Type Into** | ```json{ "action":"type","element":X,"text":"…","capture_after":true }``` | “We typed into the target element.” |
| **Drag / Drop** | ```json{ "action":"drag","source_element":X,"target_element":Y,"capture_after":true }``` | “We dragged the source into the target.” |
| **Image‑Based Fallback** | ```json{ "action":"image_match","region":{"x":INT,"y":INT,"width":INT,"height":INT},"capture_after":true }``` | “We used image matching to locate the target.” |
| **Pixel‑Coordinate Fallback** | ```json{ "action":"click","x":INT,"y":INT,"capture_after":true }``` | “We clicked at the specified coordinates.” |

> **Important**:  
> - For **drag** actions, supply both `source_element` and `target_element` indices.  
> - If the target can’t be found via AX tree, use either the `image_match` or pixel‑coordinate method as a fallback.  

## 6. Handling Capture References
- After the `capture` JSON block, place the following line **immediately** beneath it:  
  ```
  MEDIA:/absolute/path/to/screenshot.png
