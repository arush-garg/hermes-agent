# Hermes Stealth Mode Implementation Plan

## Overview

Add a `--stealth` flag to Hermes CLI that launches an invisible, always-on-top overlay window (like Cluely/cue) with full Hermes agent capabilities. Reuse Hermes Desktop's existing Electron infrastructure (pet overlay pattern, preload/IPC, agent backend) and adapt cue's window flags, capture pipeline, and global shortcuts.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Hermes CLI: `hermes --stealth` / `hermes desktop --stealth`    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Electron Main Process (apps/desktop/electron/main.ts)          │
│  ├── Main Window (existing)                                     │
│  ├── Pet Overlay Window (existing)                              │
│  └── Stealth Overlay Window (NEW)                               │
│       ├── setContentProtection(true)  // Windows: WDA_EXCLUDE  │
│       ├── setHiddenInMissionControl(true)  // macOS            │
│       ├── type: 'panel' (macOS) / 'toolbar' (Windows)          │
│       ├── frame: false, transparent: true, alwaysOnTop: true   │
│       ├── skipTaskbar: true, focusable: false                  │
│       └── Global shortcuts: Cmd+Enter / Ctrl+Enter             │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
            ┌─────────────┐      ┌─────────────┐
            │ Preload     │      │ Renderer    │
            │ (IPC bridge)│      │ (minimal UI)│
            └─────────────┘      └─────────────┘
                    │                   │
                    └─────────┬─────────┘
                              ▼
                    ┌─────────────────────┐
                    │ Agent Backend       │
                    │ (existing Hermes    │
                    │  JSON-RPC / gateway)│
                    └─────────────────────┘
```

---

## Reuse Map

| Feature | Source | Reuse Strategy |
|---------|--------|----------------|
| Invisible window flags | **cue** (`main.js:233`, `main.js:242`) | Copy `setContentProtection`, `setHiddenInMissionControl`, `type: 'toolbar'` |
| Pet overlay window | **Hermes** (`main.ts:13780-13880`) | Extend `spawnPetOverlayWindow` → `spawnStealthOverlayWindow` |
| Preload/IPC pattern | **Hermes** (`preload.ts`, `main.ts`) | Add `hermes:stealth:*` channels to existing preload |
| Global shortcuts | **cue** (`main.js:34`, `globalShortcut.register`) | Register `Cmd+Enter` / `Ctrl+Enter` for assist |
| Screenshot capture | **cue** (`src/screen.js`) + **Hermes** (`tools/screenshot_tool.py`) | Reuse `desktopCapturer` + route to agent tool |
| Mic capture | **cue** (`renderer/renderer.js`: getUserMedia) | Add `mic:pcm` IPC to existing audio pipeline |
| System audio loopback | **cue** (`getDisplayMedia` loopback) | **Defer** — macOS needs ScreenCaptureKit, Windows works |
| Agent backend | **Hermes** (existing) | Zero changes — stealth window is just another client |
| Settings/Config | **Hermes** (config.yaml, skills, memory) | Full reuse — stealth uses your models, tools, skills |

---

## Implementation Phases

### Phase 1: CLI Flag & Desktop Entry (Week 1)

**Files to modify:**
- `hermes_cli/_parser.py` — Add `--stealth` to top-level flags (like `--tui`, `--cli`)
- `hermes_cli/main.py` — Handle `--stealth` in `_try_fast_chat_launch` / dispatch
- `hermes_cli/main_desktop.py` — Add `--stealth` to `desktop` subcommand

**Behavior:**
```bash
hermes --stealth                    # Launch stealth overlay only
hermes desktop --stealth            # Same, explicit
hermes --stealth --source           # Dev mode against source
```

**No main window created** — only stealth overlay window.

---

### Phase 2: Stealth Overlay Window (Week 1-2)

**New file:** `apps/desktop/electron/stealth-overlay.ts` (or inline in `main.ts`)

Adapt from `spawnPetOverlayWindow` (main.ts:13756-13858) + cue's window options:

```typescript
// cue main.js:198-216, 227-238
const winOptions = {
  width: 400,
  height: 500,
  x: workArea.x + workArea.width - 420,
  y: workArea.y + 40,
  frame: false,
  transparent: true,
  hasShadow: false,
  resizable: true,
  skipTaskbar: true,
  alwaysOnTop: true,
  fullscreenable: false,
  focusable: false,           // Never steal focus
  show: false,
  backgroundColor: '#00000000',
  type: IS_MAC ? 'panel' : 'toolbar',  // cue: toolbar on Windows
  hiddenInMissionControl: IS_MAC,
  webPreferences: {
    preload: PRELOAD_PATH,
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    backgroundThrottling: false
  }
};

// Content protection — cue main.js:230-238
if (!process.env.CUE_NO_PROTECT) {
  if (WIN_SUPPORTS_CONTENT_PROTECTION) {
    win.setContentProtection(true);
  }
  if (IS_MAC) {
    win.setHiddenInMissionControl?.(true);
  }
}

win.setAlwaysOnTop(true, IS_MAC ? 'floating' : 'screen-saver');
win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
```

**Key differences from pet overlay:**
- Click-through by default (pet overlay toggles `focusable`)
- No sprite/bubble — minimal glass panel UI
- Global shortcut registration (see Phase 3)

---

### Phase 3: Global Shortcuts (Week 2)

**cue pattern** (`main.js:34`, registered in `app.whenReady`):

```typescript
// cue: assist = Cmd+Enter / Ctrl+Enter
// cue: leetcode = Cmd+H / Ctrl+H
// cue: quit = Cmd+Shift+X / Ctrl+Shift+X

const shortcuts = {
  assist: IS_MAC ? 'Command+Enter' : 'Control+Enter',
  solve: IS_MAC ? 'Command+H' : 'Control+H',
  quit: IS_MAC ? 'Command+Shift+X' : 'Control+Shift+X'
};

Object.entries(shortcuts).forEach(([name, accelerator]) => {
  const registered = globalShortcut.register(accelerator, () => {
    stealthWindow?.webContents.send(`hermes:stealth:${name}`);
  });
  shortcutState[name] = registered;
});
```

**Hermes integration:** Send IPC to stealth renderer → renderer calls agent via existing `hermes:ask` / `hermes:captureToggle` channels.

---

### Phase 4: IPC & Preload Extensions (Week 2)

**Extend** `apps/desktop/electron/preload.ts` (or create `stealth-preload.ts`):

```typescript
// New channels for stealth overlay
contextBridge.exposeInMainWorld('hermesStealth', {
  // Screen capture
  captureScreen: () => ipcRenderer.invoke('stealth:capture-screen'),
  
  // Audio capture (mic)
  startMic: () => ipcRenderer.invoke('stealth:mic:start'),
  stopMic: () => ipcRenderer.invoke('stealth:mic:stop'),
  onMicData: (cb) => ipcRenderer.on('stealth:mic:data', cb),
  
  // System audio loopback (Windows only, macOS deferred)
  startSystemAudio: () => ipcRenderer.invoke('stealth:system:start'),
  stopSystemAudio: () => ipcRenderer.invoke('stealth:system:stop'),
  
  // Agent features
  assist: (payload) => ipcRenderer.send('stealth:assist', payload),
  solveScreen: () => ipcRenderer.send('stealth:solve-screen'),
  ask: (question) => ipcRenderer.send('stealth:ask', question),
  
  // State
  onCaptureState: (cb) => ipcRenderer.on('stealth:capture:state', cb),
  onLLMToken: (cb) => ipcRenderer.on('stealth:llm:token', cb),
  onLLMDone: (cb) => ipcRenderer.on('stealth:llm:done', cb),
  
  // Window control
  hide: () => ipcRenderer.send('stealth:hide'),
  show: () => ipcRenderer.send('stealth:show'),
  quit: () => ipcRenderer.send('stealth:quit'),
});
```

**Main process handlers** in `main.ts` (new IPC section):

```typescript
// Screen capture — reuse Hermes screenshot tool logic + cue desktopCapturer
ipcMain.handle('stealth:capture-screen', async () => {
  const { desktopCapturer, screen } = require('electron');
  const primary = screen.getPrimaryDisplay();
  const sources = await desktopCapturer.getSources({
    types: ['screen'],
    thumbnailSize: { width: primary.size.width, height: primary.size.height }
  });
  return sources[0]?.thumbnail.toDataURL();
});

// Mic capture — reuse cue's getUserMedia pattern in renderer, pipe PCM to main
ipcMain.on('stealth:mic:pcm', (_, arrayBuffer) => {
  // Forward to agent STT pipeline (existing Hermes audio tooling)
});

// Agent feature triggers — route to existing Hermes agent via JSON-RPC
ipcMain.on('stealth:assist', (_, payload) => {
  // Call agent with screen + recent transcript context
  // Reuse: agent can call terminal, web_search, tools, etc.
});
```

---

### Phase 5: Minimal Renderer UI (Week 2-3)

**New:** `apps/desktop/src/stealth-overlay/` (mirror `src/` structure)

```
src/stealth-overlay/
├── index.html          # Minimal entry
├── main.tsx            # React root (or vanilla JS like cue)
├── components/
│   ├── StealthPanel.tsx      # Glass panel, click-through
│   ├── StatusIndicator.tsx   # Green dot = listening
│   ├── ResponseStream.tsx    # Streaming LLM tokens
│   └── InputBox.tsx          # Type question + Enter
├── hooks/
│   useStealthIPC.ts          # Wrapper around window.hermesStealth
│   useGlobalShortcuts.ts     # Keyboard hints
└── styles.css                # Transparent, glassmorphism
```

**UI States** (from cue):
1. **Collapsed** — Top pill only (logo, hide/quit, smart toggle)
2. **Expanded** — Glass panel with response streaming
3. **Listening** — Green dot, mic active
4. **Thinking** — Spinner, streaming tokens

**Click-through:** CSS `pointer-events: none` on panel background, `pointer-events: auto` only on interactive elements (buttons, input).

---

### Phase 6: Agent Backend Integration (Week 3)

**Zero backend changes needed.** The stealth overlay is just another client connecting to the same Hermes backend that the main window and TUI use.

**Connection flow:**
1. Stealth window loads → connects to local Hermes backend (via `HERMES_DESKTOP=1` env)
2. Backend spawns agent with user's config (model, tools, skills, memory)
3. Stealth IPC calls → backend agent tools → results stream back

**Context for "Assist":**
- Screenshot (via `stealth:capture-screen`)
- Recent transcript (if capturing)
- User's AGENTS.md, skills, memory — all automatic

---

### Phase 7: Audio Capture (Week 3-4) — Optional / Deferred

| Channel | Platform | Status |
|---------|----------|--------|
| Mic (You) | macOS + Windows | **Doable** — `getUserMedia` in renderer, stream PCM to main |
| System Audio (Them) | Windows only | **Doable** — `getDisplayMedia` loopback works |
| System Audio (Them) | macOS | **Deferred** — Needs ScreenCaptureKit + Chromium flags (`MacLoopbackAudioForScreenShare`) |

**cue approach** (`renderer/renderer.js`):
```javascript
// Mic
const micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
const micContext = new AudioContext({ sampleRate: 16000 });
// ... pipe to AudioWorklet → PCM → ipcRenderer.send('mic:pcm', buffer)

// System audio (Windows)
const displayStream = await navigator.mediaDevices.getDisplayMedia({
  video: false, audio: { mandatory: { chromeMediaSource: 'system' } }
});
```

**Hermes has no audio pipeline today** — would need new tooling. Recommend: **Phase 7 deferred**, launch with screen-only features first (assist, solve-screen, ask).

---

## Configuration

**config.yaml additions:**
```yaml
stealth:
  enabled: true
  shortcuts:
    assist: "Cmd+Enter"      # Override default
    solve: "Cmd+H"
    quit: "Cmd+Shift+X"
  window:
    width: 400
    height: 500
    position: "top-right"    # top-right, top-left, bottom-right, bottom-left
  features:
    screen_capture: true
    mic_capture: false       # Requires audio pipeline
    system_audio: false      # Windows only, macOS unsupported
  content_protection: true   # Set false for debugging (CUE_NO_PROTECT=1)
```

---

## Testing Checklist

- [ ] `--stealth` flag parsed and launches only overlay (no main window)
- [ ] Window invisible in screen share (Zoom, Teams, Meet, QuickTime)
- [ ] Window hidden from Mission Control (macOS) / Alt+Tab (Windows)
- [ ] Global shortcuts register and trigger IPC
- [ ] Screenshot capture works and feeds agent
- [ ] Agent responds with tools, skills, memory
- [ ] Streaming tokens render in overlay
- [ ] Click-through works (panel doesn't block clicks)
- [ ] Settings persist across restarts
- [ ] Works in `--source` dev mode
- [ ] Packaged app (`npm run dist:mac`) includes stealth mode

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| macOS 15.4+ ignores `setContentProtection` | High | Document as best-effort (cue does same); user controls via Zoom setting |
| Global shortcut conflicts | Medium | Check `globalShortcut.register` return; show warning in UI if taken |
| Audio pipeline doesn't exist | High | Defer audio; launch with screen-only features |
| Packaged app rebuild resets TCC grants | Medium | Use `--setup-tcc-identity` (already in Hermes) |
| Pet overlay + stealth overlay conflict | Low | Different window types, separate IPC channels |

---

## Effort Estimate

| Phase | Effort | Notes |
|-------|--------|-------|
| 1. CLI Flag | 0.5 days | Trivial |
| 2. Stealth Window | 2 days | Adapt pet overlay + cue flags |
| 3. Global Shortcuts | 1 day | Standard Electron API |
| 4. IPC/Preload | 2 days | Extend existing pattern |
| 5. Renderer UI | 3 days | New minimal React app |
| 6. Agent Integration | 0.5 days | Zero backend changes |
| 7. Audio (deferred) | — | Separate effort |
| **Total (core)** | **~9 days** | Can parallelize 2-5 |

---

## File Tree (New/Modified)

```
hermes-agent/
├── hermes_cli/
│   ├── _parser.py              # + --stealth flag
│   ├── main.py                 # + stealth dispatch
│   └── main_desktop.py         # + --stealth arg
└── apps/desktop/
    ├── electron/
    │   ├── main.ts             # + spawnStealthOverlayWindow, IPC handlers
    │   ├── preload.ts          # + hermesStealth API
    │   └── stealth-overlay.ts  # NEW: window creation logic
    └── src/
        └── stealth-overlay/    # NEW: minimal renderer
            ├── index.html
            ├── main.tsx
            ├── components/
            ├── hooks/
            └── styles.css
```

---

## Launch Commands (Post-Implementation)

```bash
# Production (packaged app)
hermes --stealth

# Development (source mode, hot reload)
hermes --stealth --source

# With custom config
hermes -p myprofile --stealth

# Debug (disable content protection)
CUE_NO_PROTECT=1 hermes --stealth
```

---

## References

- **cue window flags:** `main.js:198-238`, `README.md:190-195`
- **cue shortcuts:** `main.js:34`, `main.js:486-490`
- **cue screen capture:** `src/screen.js`
- **cue audio:** `renderer/renderer.js` (mic + system audio)
- **Hermes pet overlay:** `main.ts:13756-13880`
- **Hermes preload:** `electron/preload.ts`
- **Hermes desktop CLI:** `hermes_cli/main_desktop.py`