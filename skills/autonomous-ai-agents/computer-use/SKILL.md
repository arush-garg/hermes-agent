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

## UI Automation with `computer_use` – Revised Instruction Set

**Purpose**  
Automate user‑interface interactions safely and reliably in the sandboxed *computer_use* environment. The workflow must honor the background‑first safety model, use the AX API to locate elements, and provide clear, minimal narration mixed with discrete JSON calls.  

| Key concept | What it means |
|-------------|---------------|
| **AX tree** | A list of accessible UI objects (`AXButton`, `AXTextField`, etc.) each with a label and bounding box. |
| **som** | “Screen‑on‑display” capture mode – returns a PNG and the AX tree. |
| **Element index** | The integer after `#` in the AX tree (`#12 AXButton 'Help' …`). |
| **Effect field** | Status string returned by `computer_use` (`confirmed`, `unverifiable`, `suspected_noop`, `foreground_unsupported`, …). |
| **PIXEL fallback** | When the AX list cannot locate an element, identify it by visual description and `image_match`. |

---

### 1. Detect UI Relevance

1. **Trigger**  
   - If the user’s request contains one of the tokens below, *and* the request does **not** explicitly prohibit UI interaction, start the UI workflow.  
   ```
   click, press, select, drag, drop, type into, capture, take a screenshot,
   image of, .jpg, .png, screenshot, examine the navigation bar
   ```  
2. **Otherwise**  
   - Respond in plain language and do **not** call `computer_use`.  

---

### 2. Start with a Capture

Always begin with a screen capture (unless the user writes `"skip capture"`).  

```json
{
  "action": "capture",
  "mode": "som",
  "app": "<optional_app_name>"
}
```

The tool will return a PNG file (`MEDIA:/path/to/file.png`) and an AX tree, each element formatted like:  

```
#<index> AX<role> '<label>' @ (<x1>,<y1>,<x2>,<y2>)
```

---

### 3. Locate the Target Element

1. **AX Search**  
   - Scan the AX list for an exact match on `role` + `label`.  
   - Example: `AXButton 'Confirm Payment'` → `#3 AXButton 'Confirm Payment' @ (10, 20, 150, 40)`.  

2. **If no match**  
   - **Pixel‑Based Fallback**  
     1. Record a concise visual description (e.g., “small green toggle button labeled ‘Dark Mode’ in the top‑right corner”).  
     2. Invoke `image_match` (pseudo‑function) to get a rectangle.  
     3. Convert that rectangle to a point (for click) or two points (for drag‑and‑drop).  

3. **If still uncertain**  
   - Respond to the user asking for clarification.  
   - Example:  
     ```
     I’m not sure what element you mean by “Help”. Could you describe its appearance or location?
     ```

---

### 4. Execute the Requested Action

| Requested keyword | JSON structure | Notes |
|-------------------|---------------|-------|
| **Click** | ```json { "action":"click","element":N,"capture_after":true }``` | Use element index from AX; if only coordinates, use `"coordinate":[x,y]`. |
| **Type** | ```json { "action":"type","element":N,"text":"…","capture_after":true }``` | Target must be a focused text field. |
| **Drag‑and‑Drop** | Step‑by‑step: (1) click grab, (2) drag to target. | ```{ "action":"click","element":N1,"capture_after":false }``` followed by ```{ "action":"drag","element":N2,"by":"relative","capture_after":true }```. |
| **Screenshot only** | Same capture call but no subsequent action. | Provide the `MEDIA:` path after the call. |
| **Any other UI gesture** | Follow the pattern in this table – always a JSON object per platform call. | |

All calls must be **separate JSON objects in individual code blocks**. Do **not** embed JSON inside narrative text.

---

### 5. Verify the Result

After each platform call, check the `effect` field in the `computer_use` response:

| `effect` value | What to do |
|----------------|------------|
| `confirmed` | Success – proceed to next step or finish. |
| `unverifiable` | Re‑capture and retry the same action. |
| `suspected_noop` | Retry or fall back to pixel detection; if still no effect, ask user for clarification. |
| `code:"foreground_unsupported"` | Do not retry with foreground. If possible, try a background operation or start a new UI session. |

When the action is a **screenshot request**, deliver the PNG by writing the `MEDIA:` path on its own line, immediately after the capture JSON call.

---

### 6. Safety & Permissions

| Dangerous scenario | Must-do | Must-don’t |
|---------------------|--------|------------|
| Sensitive dialogs (payments, 2‑FA, password prompts) | Confirm user explicitly gives permission | Interact automatically |
| Credentials (API keys, passwords) | Never type | Never type |
| Shell commands matching dangerous black‑list | Never issue | Never issue |
| `raise_window` or `bring_to_front` | Only if tool response requests | Avoid unless explicitly needed |

If the tool returns `code:"foreground_unsupported"`, do **not** try to force the action to foreground.

---

### 7. Output Style

1. **Narrative** – Plain English explanation *outside* of any code block.  
2. **JSON Calls** – Each in a separate code block.  
3. **Screenshot** – After the capture call, immediately write the `MEDIA:` line.  
4. **No extraneous text** – Do not use ellipsis (`...`) or commented JSON inside narrative.  
5. **Keep it short** – One sentence commentary for each step, followed by the JSON call, then the verification line.  

---

### 8. Example Flow

```
User: Click the 'Submit' button on the signup form.

Assistant:
We captured the screen and found element #5 labeled 'Submit'.

```json
{
  "action":"click",
  "element":5,
  "capture_after":true
}
```

Assistant: The button click was confirmed.
```

---

### 9. Common Pitfalls to Avoid

1. Forgetting the initial capture ⇒ element indices become stale.  
2. Assuming AX will expose a custom‑canvas widget; always fallback to pixel detection.  
3. Forcing foreground input without explicit consent.  
4. Not providing the screenshot image (`MEDIA:`) after a capture.  
5. Mixing JSON with narrative; keep them separate.  

--- 
**End of Revised Instruction**
