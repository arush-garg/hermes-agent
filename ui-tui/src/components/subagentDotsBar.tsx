import { Box, NoSelect, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useEffect, useMemo, useState } from 'react'

import { $turnState } from '../app/turnStore.js'
import { $uiState } from '../app/uiStore.js'
import { $viewState, patchViewState } from '../app/viewStore.js'
import { topLevelSubagents } from '../lib/subagentTree.js'
import type { Theme } from '../theme.js'
import type { SubagentProgress, SubagentStatus } from '../types.js'

import { SubagentSteerInput } from './subagentSteerInput.js'

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

const STATUS_DOT: Record<SubagentStatus, string> = {
  completed: '◉',
  error: '⚠',
  failed: '✕',
  interrupted: '◼',
  queued: '○',
  running: '●',
  timeout: '◷'
}

function statusColor(status: SubagentStatus, theme: Theme): string {
  switch (status) {
    case 'running':
      return theme.color.accent
    case 'queued':
      return theme.color.label
    case 'completed':
      return theme.color.muted
    case 'interrupted':
    case 'timeout':
      return theme.color.warn
    case 'failed':
    case 'error':
      return theme.color.error
    default:
      return theme.color.muted
  }
}

function truncate(s: string, max: number): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`
}

interface SubagentDotsBarProps {
  /** Called when the user presses x/r on a running subagent dot. */
  onInterrupt: (id: string) => void
  /** Called when the user submits a steer message for a subagent. */
  onSteerSubmit: (id: string, text: string) => void
}

/**
 * Compact horizontal indicator row rendered just below the chat input.
 *
 * Passive state (composer focused): dots only — no navigation hint, no cursor
 * highlight — so the bar is a glanceable status line that doesn't compete with
 * the composer.
 *
 * Active state (subagent-focus): a bracket cursor highlights the selected dot,
 * a navigation hint appears, and pressing ↵ opens the steer input inline.
 *
 * Background tasks (bgTasks) are shown as dim ◎ glyphs at the end; they have
 * no steer capability and are skipped by left/right cursor navigation.
 */
export const SubagentDotsBar = memo(function SubagentDotsBar({ onInterrupt: _onInterrupt, onSteerSubmit }: SubagentDotsBarProps) {
  const subagents = useStore($turnState).subagents
  const ui = useStore($uiState)
  const vs = useStore($viewState)
  const theme = ui.theme
  const cursor = vs.subagentPanel.cursor
  const steerOpen = vs.subagentPanel.steerOpen
  const focused = vs.focus === 'subagent-focus'
  const [spinnerIdx, setSpinnerIdx] = useState(0)

  const topLevel: SubagentProgress[] = useMemo(() => topLevelSubagents(subagents), [subagents])
  const bgCount = ui.bgTasks.size
  const hasAny = topLevel.length > 0 || bgCount > 0

  useEffect(() => {
    const hasRunning = topLevel.some(s => s.status === 'running')
    if (!hasRunning) return
    const id = setInterval(() => setSpinnerIdx(i => (i + 1) % SPINNER_FRAMES.length), 100)
    return () => clearInterval(id)
  }, [topLevel])

  if (!hasAny) return null

  const selected: SubagentProgress | undefined = topLevel[cursor]

  return (
    <Box flexDirection="column" flexShrink={0}>
      {/* ── Dots row ────────────────────────────────────────────────── */}
      <Box flexDirection="row" flexWrap="nowrap">
        {topLevel.map((item, i) => {
          const isSelected = focused && i === cursor
          const glyph = item.status === 'running' ? SPINNER_FRAMES[spinnerIdx] : (STATUS_DOT[item.status] ?? '●')
          const color = statusColor(item.status, theme)

          return (
            <Box key={item.id} marginRight={1}>
              <NoSelect>
                {isSelected ? (
                  <Text bold color={theme.color.text}>
                    [{glyph}]
                  </Text>
                ) : (
                  <Text color={color}>{glyph}</Text>
                )}
              </NoSelect>
            </Box>
          )
        })}

        {/* Background task dots — dim, non-interactive */}
        {bgCount > 0 && (
          <Box marginRight={1}>
            <Text color={theme.color.muted} dimColor>
              {'◎'.repeat(Math.min(bgCount, 3))}
              {bgCount > 3 ? `+${bgCount - 3}` : ''}
            </Text>
          </Box>
        )}

        {/* Selected agent goal label (active mode only) */}
        {focused && selected != null && !steerOpen && (
          <Text color={theme.color.muted}>{truncate(selected.goal, 30)} </Text>
        )}

        {/* Navigation hint (active mode, steer closed) */}
        {focused && !steerOpen && (
          <Text color={theme.color.muted} dimColor>
            ← → nav · ↵ msg · esc back
          </Text>
        )}
      </Box>

      {/* ── Steer input (active mode, Enter pressed) ────────────────── */}
      {steerOpen && selected != null && (
        <SubagentSteerInput
          subagentId={selected.id}
          theme={theme}
          onClose={() =>
            patchViewState(state => ({
              ...state,
              subagentPanel: { ...state.subagentPanel, steerOpen: false, steerTargetId: null }
            }))
          }
          onSteerSubmit={onSteerSubmit}
        />
      )}
    </Box>
  )
})
