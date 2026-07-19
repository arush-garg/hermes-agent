import { atom } from 'nanostores'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ViewFocus = 'composer' | 'subagent-focus'

interface SubagentPanelState {
  active: boolean
  cursor: number
  steerOpen: boolean
  steerTargetId: string | null
}

interface ViewState {
  focus: ViewFocus
  subagentPanel: SubagentPanelState
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

const buildViewState = (): ViewState => ({
  focus: 'composer',
  subagentPanel: { active: false, cursor: 0, steerOpen: false, steerTargetId: null }
})

// ---------------------------------------------------------------------------
// Store + helpers
// ---------------------------------------------------------------------------

export const $viewState = atom<ViewState>(buildViewState())

export const getViewState = (): ViewState => $viewState.get()

export const patchViewState = (next: Partial<ViewState> | ((state: ViewState) => ViewState)) =>
  $viewState.set(typeof next === 'function' ? next($viewState.get()) : { ...$viewState.get(), ...next })

export const resetViewState = (): void => $viewState.set(buildViewState())

// Re-export types for consumers
export type { ViewState, ViewFocus, SubagentPanelState }
