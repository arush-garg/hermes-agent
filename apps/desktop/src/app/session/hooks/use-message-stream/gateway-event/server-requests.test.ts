import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { clearClarifyRequest } from '@/store/clarify'
import { $nativeNotifyPrefs } from '@/store/native-notifications'
import { __resetNativeNotifyBaselineForTests } from '@/store/notify-baseline'
import { resetServerRequestsForTests } from '@/store/server-requests'
import { setActiveSessionId, setSessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import { $toursEnabled } from '@/store/tours'
import type { SessionInfo } from '@/types/hermes'

import { handleServerRequest, previewSessionRoute } from './server-requests'
import type { ServerRequestContext } from './server-requests'

const deps = {
  activeSessionIdRef: { current: null },
  sessionInterrupted: () => false,
  updateSessionState: (_sessionId, update) => update(createClientSessionState('stored-session')),
  upsertToolCall: () => undefined
} as ServerRequestContext['deps']

function deliver(method: string, params: Record<string, unknown>, activeSessionId: null | string) {
  const respond = vi.fn()
  const fail = vi.fn()

  const handled = handleServerRequest(
    { fail, id: 'srq-1', method, params, profile: 'default', respond },
    deps,
    activeSessionId
  )

  return { fail, handled, respond }
}

describe('connection request routing', () => {
  it('does not route connection operations through the server-request rail', () => {
    const { handled, respond } = deliver(
      'connection',
      {
        deadline_at: 1_800_000_000,
        op_id: 'op-1',
        session_id: 'session-a',
        targets: [{ action: 'install', kind: 'mcp', name: 'linear' }],
        timeout_seconds: 60,
        tool_call_id: 'call-1'
      },
      'session-a'
    )

    expect(handled).toBe(false)
    expect(respond).not.toHaveBeenCalled()
  })
})

describe('clarify request notifications', () => {
  const notify = vi.fn().mockResolvedValue(true)
  const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
  const originalHidden = Object.getOwnPropertyDescriptor(document, 'hidden')
  const originalHasFocus = Object.getOwnPropertyDescriptor(document, 'hasFocus')
  let previousPrefs = $nativeNotifyPrefs.get()

  beforeEach(() => {
    notify.mockClear()
    previousPrefs = $nativeNotifyPrefs.get()
    $nativeNotifyPrefs.set({ ...previousPrefs, enabled: true, kinds: { ...previousPrefs.kinds, input: true } })
    desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
    setActiveSessionId('session-clarify')
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => true })
    __resetNativeNotifyBaselineForTests()
  })

  afterEach(() => {
    delete desktopWindow.hermesDesktop
    clearClarifyRequest()
    resetServerRequestsForTests()
    $nativeNotifyPrefs.set(previousPrefs)
    setActiveSessionId(null)
    setSessions([])

    if (originalHidden) {
      Object.defineProperty(document, 'hidden', originalHidden)
    } else {
      Reflect.deleteProperty(document, 'hidden')
    }

    if (originalHasFocus) {
      Object.defineProperty(document, 'hasFocus', originalHasFocus)
    } else {
      Reflect.deleteProperty(document, 'hasFocus')
    }

    __resetNativeNotifyBaselineForTests()
  })

  it('sends the live clarify question through the shared native notification bridge while Desktop is focused', () => {
    const question = 'Which approach should I use?'
    const { handled } = deliver('clarify', { question, session_id: 'session-clarify' }, 'session-clarify')

    expect(handled).toBe(true)
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        body: question,
        kind: 'input',
        sessionId: 'session-clarify',
        title: expect.stringContaining('Input needed')
      })
    )
  })
})

describe('approval request routing', () => {
  const notify = vi.fn().mockResolvedValue(true)
  const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

  beforeEach(() => {
    notify.mockClear()
    desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
    setSessions([{ id: 'session-a', title: 'Fix the flaky test' } as SessionInfo])
    setActiveSessionId('session-b')
  })

  afterEach(() => {
    delete desktopWindow.hermesDesktop
    setSessions([])
    setActiveSessionId(null)
  })

  it('titles the parked approval toast with the session it belongs to', () => {
    deliver(
      'approval',
      { command: 'rm -rf /', description: 'dangerous', request_id: 'r1', session_id: 'session-a' },
      'session-b'
    )

    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'approval', title: expect.stringContaining('Fix the flaky test') })
    )
  })
})

describe('preview action request routing', () => {
  it('retries a replayed scoped request only while no session is bound yet', () => {
    expect(previewSessionRoute({ replayed: true, sessionId: 'session-a', activeSessionId: null })).toBe('retry')
    expect(previewSessionRoute({ replayed: true, sessionId: 'session-a', activeSessionId: 'session-a' })).toBe('run')
    expect(previewSessionRoute({ replayed: true, sessionId: 'session-a', activeSessionId: 'session-b' })).toBe('ignore')
    expect(previewSessionRoute({ replayed: true, sessionId: '', activeSessionId: null })).toBe('run')
  })

  it('leaves a scoped action request unanswered in a window showing another session', () => {
    const { handled, respond, fail } = deliver(
      'preview.act',
      { action: 'elements', session_id: 'session-a' },
      'session-b'
    )

    expect(handled).toBe(true)
    expect(respond).not.toHaveBeenCalled()
    expect(fail).not.toHaveBeenCalled()
  })

  it('leaves scoped pane reads unanswered in a window showing another session', async () => {
    const reads = ['preview.read', 'terminal.read', 'window.read'].map(method =>
      deliver(method, { session_id: 'session-a' }, 'session-b')
    )

    await Promise.resolve()

    for (const { handled, respond } of reads) {
      expect(handled).toBe(true)
      expect(respond).not.toHaveBeenCalled()
    }
  })

  it("answers pane reads for a session hosted in one of this window's tiles", async () => {
    // The tile session is not the active one, but this window hosts it: its
    // panes are here, so an 'ignore' would stall the tool until its deadline.
    $sessionTiles.set([{ runtimeId: 'session-a', storedSessionId: 'stored-a' } as never])

    try {
      const reads = ['preview.read', 'terminal.read', 'window.read'].map(method =>
        deliver(method, { session_id: 'session-a' }, 'session-b')
      )

      await new Promise(resolve => setTimeout(resolve, 0))

      for (const { handled, respond } of reads) {
        expect(handled).toBe(true)
        expect(respond).toHaveBeenCalledTimes(1)
      }
    } finally {
      $sessionTiles.set([])
    }
  })

  it('fails fast for an unscoped request with no session in view', () => {
    const { respond } = deliver('preview.act', { action: 'elements' }, null)

    expect(JSON.parse(respond.mock.calls[0][0].value)).toMatchObject({ success: false })
  })
})

describe('tour request routing', () => {
  afterEach(() => {
    $toursEnabled.set(true)
  })

  it('leaves a scoped request unanswered in another session even when tours are disabled', () => {
    $toursEnabled.set(false)
    const { handled, respond } = deliver('tour', { action: 'discover', session_id: 'session-a' }, 'session-b')

    expect(handled).toBe(true)
    expect(respond).not.toHaveBeenCalled()
  })

  it('fails fast for an unscoped request with no session in view', () => {
    const { respond } = deliver('tour', { action: 'discover' }, null)

    expect(JSON.parse(respond.mock.calls[0][0].value)).toMatchObject({ success: false })
  })
})
