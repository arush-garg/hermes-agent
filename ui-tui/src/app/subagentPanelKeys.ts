export type SubagentPanelAction =
  | { type: 'navigate'; cursor: number }
  | { type: 'exit' }
  | { type: 'interrupt' }
  | { type: 'open-steer' }
  | { type: 'none' }

interface KeyState {
  downArrow: boolean
  upArrow: boolean
  leftArrow: boolean
  rightArrow: boolean
  home: boolean
  end: boolean
  ctrl: boolean
  shift: boolean
  alt: boolean
  meta: boolean
  pageDown: boolean
  pageUp: boolean
  return: boolean
  escape: boolean
}

interface PanelState {
  cursor: number
  subagentCount: number
  selectedStatus: string | undefined
}

export function handleSubagentPanelKeyDown(
  key: KeyState,
  ch: string | undefined,
  panel: PanelState
): SubagentPanelAction {
  const { cursor, subagentCount, selectedStatus } = panel
  const hasModifier = key.ctrl || key.alt || key.meta

  // Horizontal navigation: right/l = next dot, left/h = previous dot
  if ((key.rightArrow || (ch === 'l' && !hasModifier)) && subagentCount > 0) {
    return { type: 'navigate', cursor: Math.min(cursor + 1, subagentCount - 1) }
  }

  if ((key.leftArrow || (ch === 'h' && !hasModifier)) && subagentCount > 0) {
    return { type: 'navigate', cursor: Math.max(0, cursor - 1) }
  }

  if ((key.home || (ch === 'g' && !hasModifier)) && subagentCount > 0) {
    return { type: 'navigate', cursor: 0 }
  }

  if ((key.end || (ch === 'G')) && subagentCount > 0) {
    return { type: 'navigate', cursor: subagentCount - 1 }
  }

  if ((ch === 'x' || ch === 'r') && !hasModifier && selectedStatus === 'running') {
    return { type: 'interrupt' }
  }

  // Escape exits dot-navigation focus back to the composer
  if (key.escape) {
    return { type: 'exit' }
  }

  if (key.return && selectedStatus === 'running') {
    return { type: 'open-steer' }
  }

  return { type: 'none' }
}
