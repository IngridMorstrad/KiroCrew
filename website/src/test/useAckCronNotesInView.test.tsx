// A cron run's bell note is read on two events only (#23564): the user opens
// its chat and a mounted pane then shows the transcript, or the note arrives
// live while a mounted pane shows it. Nothing is read from state alone: not a
// restored chat, not a loading or failed load, not a closed split pane, not a
// background tab, and never against a user's "Mark unread".
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import chatReducer, { setActiveSlot, switchSlot } from '../store/chatSlice'
import notificationsReducer, { addNotification, unackNotification } from '../store/notificationsSlice'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import type { ChatMessage, Notification } from '../types'
import { transcriptShownFor, unreadCronNotesFor, useAckCronNotesInView, useMarkTranscriptShown } from '../hooks/useAckCronNotesInView'
import { createTestStore, renderWithProviders } from './helpers'

const note = (over: Partial<Notification>): Notification => ({
  kind: 'cron', title: 'Nightly digest', body: 'done', ts: '2026-10-24T01:00:00Z', slot: 'cron-a', job_id: 'job-a',
  ...over,
})

const RUN = [{ role: 'assistant', content: 'Digest posted.', ts: 1, cls: '' }] as unknown as ChatMessage[]

type Store = ReturnType<typeof createTestStore>

/** A store with *items* in the bell and *activeSlot* holding a loaded transcript in Redux. */
function storeWith(items: Notification[], activeSlot: string | null = 'cron-a', chat: Record<string, unknown> = {}) {
  const base = { ...chatReducer(undefined, setActiveSlot(activeSlot as string)), messages: RUN, slotLoading: false, ...chat }
  const notifications = { ...notificationsReducer(undefined, { type: '@@test/init' }), items }
  return createTestStore({ chat: base, notifications } as never)
}

/** The user's open of *key* landing: `switchSlot.fulfilled` for a gesture. */
function landOpen(store: Store, key: string, arg: object = { announceOnMissing: true, gesture: true }) {
  act(() => {
    store.dispatch(switchSlot.fulfilled(
      { key, messages: RUN, running: false, stopping: false, hasMore: false, total: 1, queue: [] } as never,
      'req-open',
      { key, ...arg },
    ))
  })
}

/** A live `notification` frame, as useWebSocket handles one. */
function arrive(store: Store, n: Notification) {
  act(() => {
    store.dispatch(addNotification(n))
    dispatchLiveNotification(n)
  })
}

function Pane({ slot, shown }: { slot: string; shown: boolean }) {
  useMarkTranscriptShown(slot, shown)
  return null
}

interface View { panes?: { slot: string; shown: boolean }[]; hidden?: boolean }

function Harness({ panes = [], hidden = false }: View) {
  useAckCronNotesInView({ hidden })
  return <>{panes.map((p, i) => <Pane key={i} slot={p.slot} shown={p.shown} />)}</>
}

function mount(store: Store, view: View = {}, route = '/chat') {
  const r = renderWithProviders(<Harness {...view} />, { store, route })
  return { ...r, update: (next: View) => r.rerender(<Harness {...next} />) }
}

const ackedTs = (store: Store) => store.getState().notifications.items.filter(n => n.acked).map(n => n.ts).sort()
const settle = () => act(async () => { await Promise.resolve() })

let ack: ReturnType<typeof vi.spyOn>
let focused = true
let docHidden = false
beforeEach(() => {
  focused = true
  docHidden = false
  ack = vi.spyOn(api, 'ackNotification').mockResolvedValue(new Response('{}'))
  vi.spyOn(api, 'unackNotification').mockResolvedValue(new Response('{}'))
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => docHidden)
})
afterEach(() => vi.restoreAllMocks())

const SHOWN_A = { panes: [{ slot: 'cron-a', shown: true }] }

describe('unreadCronNotesFor', () => {
  it('picks unread cron notes of the slot and nothing else', () => {
    const notes = [
      note({ ts: '1' }),
      note({ ts: '2', acked: true }),
      note({ ts: '3', slot: 'cron-b' }),
      note({ ts: '4', kind: 'approval' }),
      note({ ts: '5', kind: 'agent' }),
    ]
    expect(unreadCronNotesFor(notes, 'cron-a')).toEqual(['1'])
    expect(unreadCronNotesFor(notes, null)).toEqual([])
  })
})

describe('useMarkTranscriptShown', () => {
  it('counts a chat shown while any mounted pane shows it', () => {
    const r = mount(createTestStore(), { panes: [{ slot: 'cron-a', shown: true }, { slot: 'cron-a', shown: true }] })
    expect(transcriptShownFor('cron-a')).toBe(true)
    r.update({ panes: [{ slot: 'cron-a', shown: true }] })
    expect(transcriptShownFor('cron-a')).toBe(true)
    r.update({ panes: [{ slot: 'cron-a', shown: false }] })
    expect(transcriptShownFor('cron-a')).toBe(false)
    r.unmount()
    expect(transcriptShownFor('cron-a')).toBe(false)
  })
})

describe('switchSlot records the user open', () => {
  it('for a gesture only, not a plain, automatic or merely announced switch', () => {
    const store = storeWith([])
    landOpen(store, 'cron-a', {})
    landOpen(store, 'cron-a', { automatic: true })
    // An announced switch the page makes itself (a late ?sid= recovery, a
    // forwarded nav intent) is not the user opening the chat.
    landOpen(store, 'cron-a', { announceOnMissing: true })
    expect(store.getState().chat.userOpen).toBeUndefined()
    landOpen(store, 'cron-a')
    landOpen(store, 'cron-a')
    expect(store.getState().chat.userOpen).toEqual({ key: 'cron-a', seq: 2 })
  })
})

describe('useAckCronNotesInView', () => {
  it('opening the cron chat reads its notes, leaving every other note alone', async () => {
    const store = storeWith([
      note({ ts: 'run-1' }),
      note({ ts: 'run-2' }),
      note({ ts: 'other-session', slot: 'cron-b' }),
      note({ ts: 'approval', kind: 'approval' }),
    ])
    mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1', 'run-2']))
    expect(ack.mock.calls.map(c => c[0]).sort()).toEqual(['run-1', 'run-2'])
  })

  it('reads nothing for a chat that is merely restored or already active', async () => {
    // A reload restores the chat with its transcript loaded: no one opened it.
    const store = storeWith([note({ ts: 'run-1' })])
    mount(store, SHOWN_A)
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('waits for the transcript of an open still loading, and reads nothing for a load that failed', async () => {
    // Loading: the open's pane has not shown the transcript yet.
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a', { messages: [], slotLoading: true })
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: false }] })
    await settle()
    expect(ack).not.toHaveBeenCalled()
    landOpen(store, 'cron-a')
    await settle()
    expect(ack).not.toHaveBeenCalled()
    r.update(SHOWN_A)
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    r.unmount()

    // Failed: switchSlot.rejected lands no open, and the pane shows nothing.
    ack.mockClear()
    const failed = storeWith([note({ ts: 'run-2' })], 'cron-a', { messages: [], slotLoading: false })
    mount(failed, { panes: [{ slot: 'cron-a', shown: false }] })
    act(() => { failed.dispatch(switchSlot.rejected(new Error('boom'), 'req-open', { key: 'cron-a', announceOnMissing: true, gesture: true })) })
    arrive(failed, note({ ts: 'run-3' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('reads a note arriving live while a pane shows its chat', async () => {
    const store = storeWith([])
    mount(store, SHOWN_A)
    arrive(store, note({ ts: 'run-1' }))
    arrive(store, note({ ts: 'elsewhere', slot: 'cron-b' }))
    arrive(store, note({ ts: 'approval', kind: 'approval' }))
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('leaves a note arriving for a split pane that was closed', async () => {
    // Closing the cron pane keeps its activeSlot and messages in Redux; only the
    // mounted panes say what is on screen.
    const store = storeWith([])
    const r = mount(store, { panes: [{ slot: 'cron-a', shown: true }, { slot: 'other', shown: true }] })
    r.update({ panes: [{ slot: 'other', shown: true }] })
    arrive(store, note({ ts: 'run-1' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('leaves a note arriving in a background tab unread, also once the tab comes back', async () => {
    const store = storeWith([])
    mount(store, SHOWN_A)
    docHidden = true
    arrive(store, note({ ts: 'run-1' }))
    await settle()
    docHidden = false
    act(() => { document.dispatchEvent(new Event('visibilitychange')); window.dispatchEvent(new Event('focus')) })
    await settle()
    expect(ack).not.toHaveBeenCalled()

    // An unfocused window counts as away too.
    focused = false
    arrive(store, note({ ts: 'run-2' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('never undoes a Mark unread, before or after a reload', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    const r = mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    await act(async () => { await store.dispatch(unackNotification('run-1')) })
    await settle()
    expect(ackedTs(store)).toEqual([])
    r.unmount()

    // The reload: a fresh store, the same chat restored, the note unread.
    const reloaded = storeWith([note({ ts: 'run-1' })])
    mount(reloaded, SHOWN_A)
    await settle()
    expect(ackedTs(reloaded)).toEqual([])
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('keeps a Mark unread through every switch the page makes on its own', async () => {
    const store = storeWith([note({ ts: 'run-1' })])
    const r = mount(store, SHOWN_A)
    landOpen(store, 'cron-a')
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
    await act(async () => { await store.dispatch(unackNotification('run-1')) })
    await settle()

    // With the chat on screen: the mount re-fetch or a URL switch (plain), the
    // auto-select (automatic), a late ?sid= recovery or a forwarded nav intent
    // (announced, no gesture).
    for (const arg of [{}, { automatic: true }, { announceOnMissing: true }]) {
      landOpen(store, 'cron-a', arg)
      await settle()
    }
    expect(ackedTs(store)).toEqual([])

    // A gesture open whose transcript the restore gate still hides, left before
    // the gate lifts: the pane never showed it.
    r.update({ panes: [{ slot: 'cron-a', shown: false }] })
    landOpen(store, 'cron-a')
    await settle()
    r.update({ panes: [{ slot: 'other', shown: true }] })
    await settle()
    expect(ackedTs(store)).toEqual([])
    expect(ack.mock.calls.map(c => c[0])).toEqual(['run-1'])
  })

  it('does nothing away from a chat route, or while the Crew or a crew window covers the pane', async () => {
    const away = storeWith([])
    mount(away, SHOWN_A, '/notifications')
    arrive(away, note({ ts: 'run-1' }))

    const covered = storeWith([note({ ts: 'run-2' })])
    mount(covered, { ...SHOWN_A, hidden: true })
    landOpen(covered, 'cron-a')
    arrive(covered, note({ ts: 'run-3' }))
    await settle()
    expect(ack).not.toHaveBeenCalled()
  })

  it('asks once per event when the server refuses, instead of looping', async () => {
    ack.mockRejectedValue(new Error('nope'))
    const store = storeWith([])
    mount(store, SHOWN_A)
    arrive(store, note({ ts: 'run-1' }))
    await waitFor(() => expect(ack).toHaveBeenCalledTimes(1))
    await settle()
    expect(ack).toHaveBeenCalledTimes(1)
  })
})
