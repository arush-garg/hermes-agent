import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { SessionPanel } from '../components/branding.js'
import { DEFAULT_THEME } from '../theme.js'
import type { SessionInfo } from '../types.js'

// Invariant under test: the TUI banner shows the yolo display the same way the
// classic CLI banner does (`_banner_left_lines` in hermes_cli/banner.py):
// "⚠ YOLO mode — all approval prompts bypassed" under the model line whenever
// the session runs with approval bypass. `info.yolo` is the same OR the backend
// reports (approvals.mode=off, --yolo/HERMES_YOLO_MODE, the per-session /yolo
// toggle), so the banner also flips live when yolo is toggled mid-session.

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const makeStreams = (columns = 100) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns, isTTY: false, rows: 40 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })

  let captured = ''
  stdout.on('data', chunk => {
    captured += chunk.toString()
  })

  return { capture: () => captured, stderr, stdin, stdout }
}

const baseInfo = (yolo?: boolean): SessionInfo => ({
  cwd: '/tmp/hermes-test',
  model: 'test-model',
  skills: { core: ['a', 'b'] },
  tools: { file: ['read_file', 'write_file'] },
  yolo
})

async function renderPanel(info: SessionInfo, columns = 100, maxWidth?: number): Promise<string> {
  const streams = makeStreams(columns)

  const instance = renderSync(React.createElement(SessionPanel, { info, maxWidth, sid: 'test', t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: streams.stderr as NodeJS.WriteStream,
    stdin: streams.stdin as NodeJS.ReadStream,
    stdout: streams.stdout as NodeJS.WriteStream
  })

  try {
    await delay(20)

    // Strip ANSI so we can assert on the rendered text content.
    // eslint-disable-next-line no-control-regex
    return streams.capture().replace(/\u001b\[[0-9;]*m/g, '')
  } finally {
    instance.unmount()
    instance.cleanup()
  }
}

describe('branding yolo display', () => {
  it('shows the yolo line when the session runs with approval bypass', async () => {
    const frame = await renderPanel(baseInfo(true))

    expect(frame).toContain('⚠ YOLO mode')
    // The hero column is only as wide as the caduceus art, so the classic
    // line wraps; assert both halves render instead of truncating.
    expect(frame).toContain('all approval')
    expect(frame).toContain('prompts bypassed')
  })

  it('shows the yolo line in the narrow layout too', async () => {
    // useStdout() is undefined under renderSync, so cols falls back to 100;
    // maxWidth is the prop the app uses to force the narrow layout.
    const frame = await renderPanel(baseInfo(true), 60, 60)

    expect(frame).toContain('⚠ YOLO mode')
    expect(frame).toContain('all approval prompts bypassed')
  })

  it('places the yolo line under the model line, before cwd and session', async () => {
    const frame = await renderPanel(baseInfo(true))

    const yoloAt = frame.indexOf('YOLO mode')

    // Mirrors the classic banner's left-column order: model, yolo, cwd, session.
    expect(yoloAt).toBeGreaterThan(frame.indexOf('test-model'))
    expect(yoloAt).toBeLessThan(frame.indexOf('/tmp/hermes-test'))
    expect(yoloAt).toBeLessThan(frame.indexOf('Session:'))
  })

  it('hides the yolo line when yolo is off', async () => {
    const frame = await renderPanel(baseInfo(false))

    expect(frame).not.toContain('YOLO mode')
    expect(frame).not.toContain('all approval prompts bypassed')
  })

  it('hides the yolo line when yolo is unset', async () => {
    const frame = await renderPanel(baseInfo())

    expect(frame).not.toContain('YOLO mode')
    expect(frame).not.toContain('all approval prompts bypassed')
  })
})
