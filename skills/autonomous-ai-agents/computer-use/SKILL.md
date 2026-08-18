---
name: computer-use
description: "Drive the desktop in the background without stealing focus."
version: 2.0.0
author: Francesco Bonacci (f-trycua), Hermes Agent
license: MIT
platforms: [macos, windows, linux]
metadata:
  hermes:
    tags: [computer-use, desktop, automation, gui, cross-platform]
    category: desktop
    related_skills: []
---

## Revised Instruction Set for UI Automation in the `computer_use` Sandbox

---

### 1. When to Engage the UI Workflow
1. **Trigger Words** – If the user input contains any of:
   ```
   click, press, select, drag, drop, type into, capture, take a screenshot,
   image of, .jpg, .png, examine the navigation bar, screenshot
   ```
2. **No Explicit No‑UI Clause** – If the request does *not* explicitly say “do not interact with the UI” or “no automation”, proceed.
3. **Otherwise** – Respond only in plain language and **do not** invoke the `computer_use` API.

---

### 2. Capture the Current Screen (unless the user writes `skip capture`)

1. **Narration** – One sentence explaining why we are capturing.  
   *Example*: “We captured the screen so we can locate the requested UI element.”
2. **JSON Call** – In its own code block.  
   ```json
   {
     "action": "capture",
     "mode": "som",
     "app": "<optional_app_name>"
   }
   ```
3. **Screenshot Path** – Immediately after the code block, on its own line.  
   `MEDIA:/path/to/file.png`

---

### 3. Resolve the Target Element

| Situation | Recommended Action |
|-----------|--------------------|
| **Exactly one AX element matches (role + label)** | Proceed to step 4. |
| **Multiple identical elements** | Ask the user for a distinguishing detail (e.g., “Which Refresh icon – the one on the left or right?”). |
| **No AX match** | Provide a concise visual description (`small green toggle in top‑right corner`) and use pixel‑fallback (`image_match`). |
| **Ambiguous description** | Ask the user to clarify the appearance or location. |

**NOTE**: Parsing the AX tree is done automatically by the underlying tool – you only need to interpret the result in natural language.

---

### 4. Execute the Requested Action

| Keyword | JSON Structure | Commentary (one sentence) |
|---------|-----------------|-----------------------------|
| **Click** | ```json { "action":"click","element":X,"capture_after":true }``` | “We clicked the target element.” |
| **Press / Select** | Same as Click | “We pressed the target element.” |
| **Type into** | ```json { "action":"type","element":X,"text":"…","capture_after":true }``` | “We typed the provided text into the field.” |
| **Drag & Drop** | Two-step: first click, then drag. | “We started dragging the first element.” (then `drag` call) |
| **Screenshot only** | No action after capture – just return the `MEDIA:` path. | “Screenshot captured.” |
| **Any other gesture** | Follow the pattern for click / type – one code block per API call. | “We performed the requested gesture.” |

**Syntax**: Every `action=` call must be in its own code block; never embed it in narrative text.

---

### 5. Verify and Retry

After each platform call:

1. **Inspect `effect`** returned from the tool.  
   - `confirmed` → success.  
   - `unverifiable` or `suspected_noop` → *re‑capture* and *retry* the same action.  
   - `code:"foreground_unsupported"` → do **not** force foreground; if possible, try a background variant or start a new UI session.  
2. **After re‑capture** – output a new `MEDIA:` path and repeat the action step.

---

### 6. Safety & Permissions

| Dangerous Scenario | Must‑Do | Must‑Don’t |
|--------------------|--------|------------|
| Payments / 2‑FA / password prompts | Await explicit user confirmation | Interact automatically |
| Credentials (API keys, passwords) | Never type | Never type |
| Shell command black‑list | Never issue | Never issue |
| `raise_window` / `bring_to_front` | Only if the tool requests **or** the user explicitly says so | Avoid unless necessary |
| `code:"foreground_unsupported"` | Do not retry forcing foreground | – |

---

### 7. Output Formatting Rules

| Element | Requirements |
|---------|--------------|
| **Narrative** | Plain English, one sentence per step, outside of any code block. |
| **JSON Calls** | One code block per call, no comments or narrative inside. |
| **Screenshot Path** | Immediately after the capture JSON, on its own line. |
| **No Extra Text** | Commentary only immediately before the call it explains; no ellipses or stray comments. |
| **Length** | Keep everything terse – one sentence commentary, concise JSON. |

---

### 8. Example Workflow

**User**: `Click the 'Submit' button on the signup form.`

**Assistant**:

> We captured the screen to locate the ‘Submit’ button.

```json
{
  "action": "capture",
  "mode": "som",
  "app": null
}
```
MEDIA:/tmp/som_capture_20260813_123456.png

> We clicked the button.

```json
{
  "action": "click",
  "element": 5,
  "capture_after": true
}
```

> The button click was confirmed.

---

### 9. Common Pitfalls to Avoid

1. Forgetting the initial capture → stale indices.  
2. Omitting the `MEDIA:` line after capture → user can’t see the screenshot.  
3. Mixing narrative with JSON → violates output formatting.  
4. Returning a prompt inside a code block or failing to ask for clarification when ambiguous.  
5. Retrying indefinitely without a safety break; always re‑capture once per retry.  

---

### 10. Final Checklist for the Assistant

1. Verify trigger words.  
2. If triggered, output the capture narration + JSON + `MEDIA:`.  
3. Parse the returned AX tree.  
4. If a single match → execute action per table.  
5. If ambiguous → ask for clarification (outside code block).  
6. After each action, inspect `effect` and retry if needed.  
7. Never act on sensitive prompts without explicit user consent.  
8. Always keep comments separate from code blocks and concise.  

**End of Instruction**
