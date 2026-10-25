/**
 * Mark a cron run's bell notes read when the user has seen the run (#23564).
 *
 * A scheduled job's run lands in its own session and posts a `cron` note to the
 * bell. Leaving that note unread once the user had looked at the run made the
 * bell claim something still needed attention.
 *
 * What counts as "seen" is two events, never a reading of state:
 *
 *  1. The user OPENS the cron chat by a gesture (a sidebar row, a tab, the
 *     palette, the note's own "go to chat": every switch marked `gesture`;
 *     a restore, a URL, a late `?sid=` recovery or a forwarded nav intent is
 *     not a gesture),
 *     and its transcript then shows in a mounted chat pane. Every unread cron
 *     note of that chat is read, once, at that moment.
 *  2. A cron note arrives LIVE (`MC_LIVE_NOTIFICATION_EVENT`, never the boot
 *     fetch or a reconnect replay) while a mounted chat pane shows that
 *     chat's transcript. That one note is read.
 *
 * Both also need the user to be looking: a chat route, a visible and focused
 * window (`isWindowAway`), and nothing over the pane (`hidden`: a remote Crew
 * in front, or a crew window covering the chat). If any of those fails at the
 * moment of the event, the note stays unread; nothing is retried later.
 *
 * So a reload or a restored chat reads nothing, a chat still loading or whose
 * load failed reads nothing (no pane shows it), a split pane that was closed
 * reads nothing (it is no longer mounted), a note that arrives in a background
 * tab stays unread, and a user's "Mark unread" is never undone, since neither
 * event fires for it.
 *
 * Only `cron` notes are read this way. Every other kind (an approval, a
 * crewmate's question) still needs an explicit action. Mark-read only: the
 * cron "Acknowledge" button (`CronAckBar`) also tells the job not to repeat the
 * result in later runs, and that stays a deliberate press.
 */
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useStore } from 'react-redux'
import { useLocation } from 'react-router-dom'
import { useAppDispatch, useAppSelector, type RootState } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { isChatPath } from './notificationBanner'
import { MC_LIVE_NOTIFICATION_EVENT, type McLiveNotificationDetail } from './notificationEvent'
import { isWindowAway } from './windowAway'

/** The `ts` of every unread cron note for *slot*. */
export function unreadCronNotesFor(notes: readonly Notification[], slot: string | null | undefined): string[] {
  if (!slot) return []
  return notes.filter(n => n.kind === 'cron' && !n.acked && n.slot === slot).map(n => n.ts)
}

// Chats whose transcript a mounted pane is showing in this document, counted
// per pane: two split panes can show one chat.
const shownTranscripts = new Map<string, number>()
const shownListeners = new Set<() => void>()
let shownVersion = 0

function bumpShown() {
  shownVersion += 1
  shownListeners.forEach(l => l())
}

/** Whether a mounted chat pane in this document shows *slot*'s transcript. */
export function transcriptShownFor(slot: string | null | undefined): boolean {
  return !!slot && (shownTranscripts.get(slot) ?? 0) > 0
}

/**
 * Called by every chat pane: *shown* is true while the pane is mounted with
 * *slot*'s transcript on it (loaded, at least one row). The single chat pane
 * (`ChatTranscriptView`) and each split pane (`ChatPane`) both call it, so a
 * pane that closes, or a chat that has not loaded, does not count.
 */
export function useMarkTranscriptShown(slot: string | null | undefined, shown: boolean): void {
  useEffect(() => {
    if (!slot || !shown) return
    shownTranscripts.set(slot, (shownTranscripts.get(slot) ?? 0) + 1)
    bumpShown()
    return () => {
      const left = (shownTranscripts.get(slot) ?? 1) - 1
      if (left > 0) shownTranscripts.set(slot, left)
      else shownTranscripts.delete(slot)
      bumpShown()
    }
  }, [slot, shown])
}

function subscribeShown(listener: () => void) {
  shownListeners.add(listener)
  return () => { shownListeners.delete(listener) }
}

export function useAckCronNotesInView({ hidden = false }: { hidden?: boolean } = {}): void {
  const dispatch = useAppDispatch()
  const reduxStore = useStore<RootState>()
  const { pathname } = useLocation()
  const shownTick = useSyncExternalStore(subscribeShown, () => shownVersion, () => 0)
  const userOpen = useAppSelector(s => s.chat.userOpen)

  // Read at event time by the listener below, which outlives a render.
  const looking = useRef({ hidden, pathname })
  looking.current = { hidden, pathname }
  const userLooking = () => !looking.current.hidden && isChatPath(looking.current.pathname) && !isWindowAway()

  // Event 1. An open that lands before its pane shows the transcript waits for
  // it; the first moment the transcript is shown decides, and a later open
  // replaces it. The open in the store at mount is history (it is null on a
  // fresh load), so it never counts.
  const consumedSeq = useRef(userOpen?.seq ?? 0)
  useEffect(() => {
    if (!userOpen || userOpen.seq <= consumedSeq.current) return
    if (!transcriptShownFor(userOpen.key)) return
    consumedSeq.current = userOpen.seq
    if (!userLooking()) return
    const notes = reduxStore.getState().notifications.items
    for (const ts of unreadCronNotesFor(notes, userOpen.key)) void dispatch(ackNotification(ts))
    // `shownTick` re-runs this when a pane starts showing the opened chat.
  }, [dispatch, reduxStore, userOpen, shownTick])

  // Event 2. A live arrival for a chat a mounted pane is showing.
  useEffect(() => {
    const onLive = (e: Event) => {
      const note = (e as CustomEvent<McLiveNotificationDetail>).detail?.note
      if (!note || note.kind !== 'cron' || note.acked || !transcriptShownFor(note.slot)) return
      if (!userLooking()) return
      // The store holds the arrival already (useWebSocket dispatches it first);
      // a note gone from it was cleared or deleted, so there is nothing to read.
      if (!reduxStore.getState().notifications.items.some(n => n.ts === note.ts && !n.acked)) return
      void dispatch(ackNotification(note.ts))
    }
    window.addEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
    return () => window.removeEventListener(MC_LIVE_NOTIFICATION_EVENT, onLive)
  }, [dispatch, reduxStore])
}
