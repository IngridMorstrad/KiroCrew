import { useCallback } from 'react'
import { useChatReactions, type ReactionError } from '../../../hooks/useChatReactions'

/**
 * Message reactions for the active slot.
 *
 * A thin wrapper over `useChatReactions`, the same split `useTranscriptPins`
 * makes over `useChatPins`: the page-level hook owns the per-message handler
 * shape (`mid, emoji`) that `transcriptRendering.tsx`'s render call sites pass
 * straight to `UserMessage`/`AssistantMessage`, so the row components stay
 * ignorant of React Query and the API client.
 *
 * The wrapper also forwards the hook's `error`/`clearError`: a failed reaction
 * must not disappear silently (`errors-use-error-notice`). The error is scoped
 * to the message it happened on so the matching row's bar renders the notice.
 */
export function useTranscriptReactions(activeSlot: string | null) {
  const { reactions, loading: reactionsLoading, loadFailed, reactionsFor, toggleReaction, error, clearError } =
    useChatReactions(activeSlot ?? undefined)

  const handleToggleReactionForMessage = useCallback(
    (mid: string, emoji: string): Promise<void> => toggleReaction(mid, emoji),
    [toggleReaction],
  )

  /** The error for `mid`, when the last failed reaction was on THIS message in
   *  THIS slot. Matching on `mid` alone would leak a slot A quota error onto
   *  the identical message in a fork of A (fork_slot copies the mid), so the
   *  active slot must match too. */
  const reactionErrorFor = useCallback(
    (mid: string): ReactionError | null =>
      error && error.mid === mid && error.slot === activeSlot ? error : null,
    [error, activeSlot],
  )

  return {
    reactions,
    reactionsLoading,
    /** The reactions list GET failed — the page shows a notice rather than
     *  letting an empty list read as "no reactions". */
    reactionsLoadFailed: loadFailed,
    reactionsFor,
    handleToggleReactionForMessage,
    reactionErrorFor,
    clearReactionError: clearError,
  }
}
