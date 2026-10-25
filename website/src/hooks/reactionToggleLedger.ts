import type { QueryClient } from '@tanstack/react-query'

/**
 * Cross-consumer ledger for in-flight reaction toggles, shared by
 * `useChatReactions` (the mutations), the `reactions_changed` WebSocket frame
 * router, and the reconnect catch-up — none of which can hold the others'
 * React state. A toggle holds an optimistic entry in the
 * `['chat-reactions', slot]` cache until its request settles; refetching that
 * cache while the entry is pending drops it, and a queued off-toggle then reads
 * the identity as absent and sends POST instead of DELETE, persisting the
 * reaction the user ended OFF. So every refetch of a reaction query — the
 * hook's own settled refetch, a cross-tab `reactions_changed` frame, and the
 * reconnect reconcile — is routed through here and held until the slot's
 * pending count drains. Keyed by `slot_key` so a toggle on one slot never
 * defers another slot's refresh.
 *
 * This lives in its own module (no React imports) so the hook, the socket leaf
 * and the reconnect path can all share ONE singleton without a hook importing a
 * leaf or a leaf importing a hook.
 */

export const reactionQueryKey = (slotKey: string | undefined) =>
  ['chat-reactions', slotKey] as const

const pendingToggles = new Map<string, number>()
// Slots whose refetch was held back while a toggle was pending — a cross-tab
// `reactions_changed` frame or a reconnect reconcile that must still land.
// Flushed once the slot's pending count hits zero.
const refetchWhenDrained = new Set<string>()

/** Register that a toggle has started for `slotKey`. */
export function beginToggle(slotKey: string): void {
  pendingToggles.set(slotKey, (pendingToggles.get(slotKey) ?? 0) + 1)
}

/** Release a settled toggle for `slotKey`. When its last toggle drains, fire
 *  the single settled refetch — the server truth the hook always wants once
 *  its own toggles drain, and the flush of any refresh held back meanwhile. */
export function endToggle(slotKey: string, qc: QueryClient): void {
  const next = Math.max(0, (pendingToggles.get(slotKey) ?? 0) - 1)
  if (next > 0) {
    pendingToggles.set(slotKey, next)
    return
  }
  pendingToggles.delete(slotKey)
  refetchWhenDrained.delete(slotKey)
  qc.invalidateQueries({ queryKey: reactionQueryKey(slotKey) })
}

/** True when a reaction toggle is still in flight for `slotKey`. A refetch
 *  checks this so it never stomps an optimistic entry a queued off-toggle
 *  still needs to read. */
export function reactionTogglePending(slotKey: string): boolean {
  return (pendingToggles.get(slotKey) ?? 0) > 0
}

/** Record that a refresh for `slotKey` was held back because a toggle was in
 *  flight; it is flushed when the last toggle settles. */
export function deferReactionInvalidation(slotKey: string): void {
  refetchWhenDrained.add(slotKey)
}

/** Invalidate (or defer) the reaction query for ONE slot, respecting pending
 *  toggles. Used by the `reactions_changed` frame and the reconnect reconcile. */
export function invalidateReactionsForSlot(qc: QueryClient, slotKey: string): void {
  if (reactionTogglePending(slotKey)) {
    deferReactionInvalidation(slotKey)
  } else {
    qc.invalidateQueries({ queryKey: reactionQueryKey(slotKey) })
  }
}

/** After a reconnect, reconcile every cached reaction query: a
 *  `reactions_changed` frame pushed while the socket was down was never
 *  delivered, and the query carries the app-wide `staleTime: Infinity`, so the
 *  view would show stale markers until a reload or a local toggle. Invalidate
 *  each observed slot's query, deferring any slot with a pending local toggle
 *  so the refetch cannot drop a still-pending optimistic entry. */
export function reconcileReactionsOnReconnect(qc: QueryClient): void {
  const queries = qc.getQueryCache().findAll({ queryKey: ['chat-reactions'] })
  for (const q of queries) {
    const slotKey = q.queryKey[1]
    if (typeof slotKey === 'string' && slotKey) invalidateReactionsForSlot(qc, slotKey)
  }
}

/** One identity's toggle chain: `tail` serializes its writes, `want` is the
 *  user's LATEST intended state for it. */
export interface ReactionToggleChain {
  tail: Promise<void>
  want: boolean
}

// The per-identity toggle chains, one map per QueryClient. They live here, not
// in a `useRef` inside the hook, because the hook remounts (the embedded
// companion chat closes and reopens) while the QueryClient — and the optimistic
// row a pending POST left in its cache — survives. A per-mount map would start
// empty on the new mount, so a remove there would not wait for the add the old
// mount still has in flight: its DELETE 404s first (counted as success), the
// late POST then persists the reaction, and the marker the user removed comes
// back. Keying by QueryClient matches the cache the chain is coordinating with,
// and a WeakMap lets a discarded client (a test's) take its chains with it.
let toggleChainsByClient = new WeakMap<QueryClient, Map<string, ReactionToggleChain>>()

/** The toggle chains shared by every hook mount on `qc`. */
export function reactionToggleChains(qc: QueryClient): Map<string, ReactionToggleChain> {
  let chains = toggleChainsByClient.get(qc)
  if (!chains) {
    chains = new Map()
    toggleChainsByClient.set(qc, chains)
  }
  return chains
}

/** Test seam: drop all cross-consumer toggle state between cases. */
export function __resetReactionToggleLedgerForTests(): void {
  pendingToggles.clear()
  refetchWhenDrained.clear()
  toggleChainsByClient = new WeakMap()
}

/** Test seam: pin a slot's pending-toggle count so a frame/reconnect test can
 *  drive the deferral branch without standing up real mutations. */
export function __setReactionTogglePendingForTests(slotKey: string, count: number): void {
  if (count <= 0) pendingToggles.delete(slotKey)
  else pendingToggles.set(slotKey, count)
}
