import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import type { PublicPrincipal, TypingLease } from '../../api/sharedCrew'
import { isComposerLocked, leaseLocks, noteComposerLocked, refusedLeaseFor, resetTypingLeaseForTests } from '../../api/typingLease'
import { applySharedCrewFrame, setSharedCrewSender, sharedCrewKeys } from './sharedCrewSocket'
import { MIN_SPACING_MS, usePresenceReporter } from './usePresenceReporter'

const alice: PublicPrincipal = { id: 'p_alice', kind: 'human', display: { name: 'Alice' } } as PublicPrincipal
const bob: PublicPrincipal = { id: 'p_bob', kind: 'human', display: { name: 'Bob' } } as PublicPrincipal

function lease(holder: PublicPrincipal | null, extra: Partial<TypingLease> = {}): TypingLease {
  return {
    schema_version: 1,
    session_key: 's1',
    holder,
    holder_ids: holder ? [holder.id] : [],
    expires_at: holder ? 1_000 : null,
    idle_seconds: 10,
    ended: '',
    ...extra,
  }
}

describe('typing lease state', () => {
  beforeEach(() => resetTypingLeaseForTests())

  it('locks everyone but the holder, and fails open without a viewer', () => {
    expect(leaseLocks(lease(alice), bob)).toBe(true)
    expect(leaseLocks(lease(alice), alice)).toBe(false)
    expect(leaseLocks(lease(null), bob)).toBe(false)
    expect(leaseLocks(lease(alice), null)).toBe(false)
    // One operator's other local sign-in is the same holder.
    expect(leaseLocks(lease(alice, { holder_ids: ['p_alice', 'p_alice_local'] }), { ...alice, id: 'p_alice_local' })).toBe(false)
  })

  it('folds a lease frame into the cache as a whole value', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(alice) })
    expect(qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))?.holder?.id).toBe('p_alice')
    applySharedCrewFrame(qc, { type: 'shared_crew.typing_lease', ...lease(null, { ended: 'idle' }) })
    const after = qc.getQueryData<TypingLease>(sharedCrewKeys.typingLease('s1'))
    expect(after?.holder).toBeNull()
    expect(after?.ended).toBe('idle')
  })

  it('recognizes and records a composer_locked refusal', () => {
    const body = { error: 'x', code: 'composer_locked', session_key: 's1', holder: alice, lease: lease(alice) }
    expect(isComposerLocked(body)).toBe(true)
    expect(isComposerLocked({ code: 'send_race' })).toBe(false)
    noteComposerLocked(body as never)
    expect(refusedLeaseFor('s1')?.lease.holder?.id).toBe('p_alice')
  })
})

describe('the reporter drives the lease', () => {
  let sent: Array<Record<string, unknown>>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-18T00:00:00Z'))
    sent = []
    setSharedCrewSender(p => sent.push(p as Record<string, unknown>))
  })

  afterEach(() => {
    setSharedCrewSender(null)
    vi.useRealTimers()
  })

  it('renews typing once per spacing while keystrokes continue', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS) })
    for (let i = 0; i < 8; i++) {
      act(() => { result.current.noteTyping(false); vi.advanceTimersByTime(1_000) })
    }
    const typing = sent.filter(f => f.activity === 'typing').length
    // 8 s of keystrokes at one frame per 2.5 s spacing, plus the trailing one.
    expect(typing).toBeGreaterThanOrEqual(3)
    expect(typing).toBeLessThanOrEqual(4)
  })

  it('reports an emptied draft at once with the content-free flag', () => {
    const { result } = renderHook(() => usePresenceReporter('s1', true))
    act(() => { vi.advanceTimersByTime(MIN_SPACING_MS); result.current.noteTyping(false) })
    act(() => { vi.advanceTimersByTime(100); result.current.noteTyping(true) })
    const last = sent[sent.length - 1]
    expect(last).toEqual({ type: 'shared_crew.activity', session_key: 's1', activity: 'viewing', draft: 'empty' })
    // An empty edit from a tab that never drafted reports nothing.
    const n = sent.length
    act(() => { result.current.noteTyping(true) })
    expect(sent.length).toBe(n)
  })
})
