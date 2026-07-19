import { Box, Text, useInput } from '@hermes/ink'
import { memo, useCallback, useState } from 'react'

import type { Theme } from '../theme.js'

import { TextInput } from './textInput.js'

interface SubagentSteerInputProps {
  subagentId: string
  theme: Theme
  onSteerSubmit: (subagentId: string, text: string) => void
  onClose: () => void
}

type FlashState = 'idle' | 'ok' | 'fail'

const FLASH_MS = 1500

function SubagentSteerInputImpl({ subagentId, theme, onSteerSubmit, onClose }: SubagentSteerInputProps) {
  const [value, setValue] = useState('')
  const [flash, setFlash] = useState<FlashState>('idle')

  const handleSubmit = useCallback(
    (text: string): void => {
      const trimmed = text.trim()

      if (!trimmed) {
        return
      }

      try {
        onSteerSubmit(subagentId, trimmed)
        setFlash('ok')
      } catch {
        setFlash('fail')
      }

      setValue('')

      setTimeout(() => setFlash('idle'), FLASH_MS)
    },
    [onSteerSubmit, subagentId]
  )

  const handleClose = useCallback((): void => {
    setValue('')
    setFlash('idle')
    onClose()
  }, [onClose])

  // TextInput passes Escape through to the global handler, so we catch it
  // here to dismiss the steer prompt without submitting.
  useInput((_ch, key) => {
    if (key.escape) {
      handleClose()
    }
  })

  return (
    <Box marginTop={1}>
      <Text color={theme.color.muted}>steer{'>'} </Text>
      {flash === 'ok' ? (
        <Text color={theme.color.statusGood}>✓ steered</Text>
      ) : flash === 'fail' ? (
        <Text color={theme.color.error}>✗ failed</Text>
      ) : (
        <TextInput
          columns={60}
          focus
          placeholder="nudge this subagent…"
          value={value}
          onChange={setValue}
          onSubmit={handleSubmit}
          onPaste={e => ({ cursor: e.cursor + e.text.length, value: e.value + e.text })}
        />
      )}
    </Box>
  )
}

export const SubagentSteerInput = memo(SubagentSteerInputImpl)
