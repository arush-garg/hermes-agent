import { Box, NoSelect, Text, useInput } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo, useCallback, useEffect, useMemo, useState } from 'react'

import { $turnState } from '../app/turnStore.js'
import { $uiState } from '../app/uiStore.js'
import { $viewState, getViewState, patchViewState } from '../app/viewStore.js'
import { handleSubagentPanelKeyDown, type SubagentPanelAction } from '../app/subagentPanelKeys.js'
import { topLevelSubagents } from '../lib/subagentTree.js'
import type { SubagentProgress, SubagentStatus } from '../types.js'

import { SubagentSteerInput } from './subagentSteerInput.js'

const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
const MAX_VISIBLE_ROWS = 8

const STATUS_GLYPH: Record<string, string> = {
  running: '●',
  queued: '○',
  completed: '✓',
  interrupted: '■',
  failed: '✗',
  timeout: '⌛',
  error: '⚠'
}

const isRunningOrQueued = (s: SubagentStatus) => s === 'running' || s === 'queued'

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  return `${Math.floor(seconds / 3600)}h`
}

function truncateGoal(goal: string, maxLen: number): string {
  if (goal.length <= maxLen) return goal
  return goal.slice(0, Math.max(3, maxLen - 3)) + '...'
}

interface SubagentPanelProps {
  onInterrupt: (subagentId: string) => void
  onSteerSubmit: (subagentId: string, text: string) => void
}

export const SubagentPanel = memo(function SubagentPanel({ onInterrupt, onSteerSubmit }: SubagentPanelProps) {
  const subagents = useStore($turnState).subagents
  const theme = useStore($uiState).theme
  const vs = getViewState()
  const cursor = vs.subagentPanel.cursor
  const steerOpen = vs.subagentPanel.steerOpen
  const [spinnerIdx, setSpinnerIdx] = useState(0)

  // Animate spinner for running subagents
  useEffect(() => {
    const hasRunning = subagents.some(s => isRunningOrQueued(s.status))
    if (!hasRunning) return
    const id = setInterval(() => setSpinnerIdx(i => (i + 1) % SPINNER_FRAMES.length), 500)
    return () => clearInterval(id)
  }, [subagents])

  const topLevel = useMemo(() => topLevelSubagents(subagents), [subagents])
  const visible = topLevel.slice(0, MAX_VISIBLE_ROWS)
  const selected = topLevel[cursor]
  const selectedStatus = selected?.status

  if (!topLevel.length) return null

  return (
    <Box flexDirection="column" flexShrink={0}>
      <Box>
        <Text color={theme.color.muted}>── </Text>
        <Text bold color={theme.color.label}>
          ▸ Subagents ({topLevel.length})
        </Text>
      </Box>

      {visible.map((item, i) => {
        const sg = STATUS_GLYPH[item.status] ?? '?'
        const isSelected = i === cursor
        const elapsed = item.durationSeconds ?? 0
        const color = isSelected ? theme.color.label : theme.color.muted
        const bg = isSelected ? theme.color.muted : undefined
        const statusColor =
          item.status === 'running' ? theme.color.accent
          : item.status === 'completed' || item.status === 'queued' ? theme.color.muted
          : item.status === 'interrupted' || item.status === 'timeout' ? theme.color.warn
          : theme.color.error

        return (
          <Box key={item.id} backgroundColor={bg}>
            <NoSelect>
              <Text color={color}>
                <Text color={statusColor}>{item.status === 'running' ? SPINNER_FRAMES[spinnerIdx] : sg}</Text>{' '}
                {truncateGoal(item.goal, 40)}
                {'  '}
                <Text dimColor>
                  {item.status} · {formatElapsed(elapsed)}
                </Text>
              </Text>
            </NoSelect>
          </Box>
        )
      })}

      {topLevel.length > MAX_VISIBLE_ROWS && (
        <Text dimColor>▸ {topLevel.length - MAX_VISIBLE_ROWS} more</Text>
      )}

      {steerOpen && selected && (
        <SubagentSteerInput
          subagentId={selected.id}
          theme={theme}
          onSteerSubmit={onSteerSubmit}
          onClose={() =>
            patchViewState(state => ({
              ...state,
              subagentPanel: { ...state.subagentPanel, steerOpen: false, steerTargetId: null }
            }))
          }
        />
      )}
    </Box>
  )
})
