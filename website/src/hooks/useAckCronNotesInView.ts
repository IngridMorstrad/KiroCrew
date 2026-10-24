/**
 * Mark a cron run's bell notes read once its chat is on screen (#23564).
 *
 * A scheduled job's run lands in its own session and posts a `cron` note to the
 * bell. Opening that session's chat shows the run, so the note has done its
 * job; leaving it unread made the bell claim something still needed attention.
 *
 * "On screen" starts from the test the live banner uses to stay quiet
 * (`targetsCurrentView`): a chat route showing the note's slot, in a focused,
 * visible window, with the local Crew in front. It adds one condition: the
 * slot's transcript has loaded, so a load that fails never reads as seen. Only `cron` notes are read
 * this way. Every other kind (an approval, a crewmate's question) still needs
 * an explicit action, so seeing its session is not an answer to it.
 *
 * Mark-read only. The cron "Acknowledge" button (`CronAckBar`) also tells the
 * job not to repeat the result in later runs; that stays a deliberate press.
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useAppDispatch, useAppSelector } from '../store'
import { ackNotification, NOTIFICATIONS_RING_CAP } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { isChatPath } from './notificationBanner'
import { isWindowAway } from './windowAway'

/** The `ts` of every unread cron note for *slot*. */
export function unreadCronNotesFor(notes: readonly Notification[], slot: string | null | undefined): string[] {
  if (!slot) return []
  return notes.filter(n => n.kind === 'cron' && !n.acked && n.slot === slot).map(n => n.ts)
}

/**
 * Cron notes this browser has seen read, or has asked to mark read, kept in
 * localStorage. It is in storage, not memory, because a user's "Mark unread"
 * has to survive a reload and a second tab: the server keeps only `acked`, so
 * after a reload a note the user marked unread looks the same as a run nobody
 * has seen. Capped at the notification list's own size, oldest out first.
 * Never pruned to the notes currently loaded, because they are empty until the
 * first fetch lands.
 */
export const HANDLED_CRON_NOTES_KEY = 'kirocrew.cronNotesHandled'

function loadHandled(): Set<string> {
  try {
    const parsed: unknown = JSON.parse(safeGetItem(HANDLED_CRON_NOTES_KEY) ?? '[]')
    return new Set(Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [])
  } catch {
    return new Set()
  }
}

function saveHandled(seen: Set<string>): void {
  const kept = [...seen].slice(-NOTIFICATIONS_RING_CAP)
  if (kept.length < seen.size) {
    seen.clear()
    kept.forEach(ts => seen.add(ts))
  }
  safeSetItem(HANDLED_CRON_NOTES_KEY, JSON.stringify(kept))
}

/** `!isWindowAway()`, kept current across focus and visibility changes. */
function useWindowPresent(): boolean {
  const [present, setPresent] = useState(() => !isWindowAway())
  useEffect(() => {
    const sync = () => setPresent(!isWindowAway())
    window.addEventListener('focus', sync)
    window.addEventListener('blur', sync)
    document.addEventListener('visibilitychange', sync)
    sync()
    return () => {
      window.removeEventListener('focus', sync)
      window.removeEventListener('blur', sync)
      document.removeEventListener('visibilitychange', sync)
    }
  }, [])
  return present
}

export function useAckCronNotesInView({ hidden = false }: { hidden?: boolean } = {}): void {
  const dispatch = useAppDispatch()
  const { pathname } = useLocation()
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The run is on screen only once its transcript is: `activeSlot` moves the
  // moment a switch starts, and a failed load leaves the pane empty
  // (`switchSlot.rejected` clears `messages`), so neither alone shows the run.
  const transcriptShown = useAppSelector(s => !s.chat.slotLoading && s.chat.messages.length > 0)
  const notes = useAppSelector(s => s.notifications.items)
  const present = useWindowPresent()
  // Notes already handled here (see `loadHandled`). One request per note: the
  // optimistic flip makes a second pass see it read, but a refused ack rolls it
  // back to unread, and asking again on every render after that would loop
  // against a server that keeps saying no.
  const handled = useRef<Set<string> | null>(null)
  if (handled.current === null) handled.current = loadHandled()

  useEffect(() => {
    const seen = handled.current as Set<string>
    let changed = false
    const remember = (ts: string) => {
      if (seen.has(ts)) return
      seen.add(ts)
      changed = true
    }
    // A cron note seen read, by any path, is never read again here: if it later
    // turns unread, that is the user's "Mark unread", and undoing it would
    // override them. Recorded before the gate so it holds for notes read while
    // their chat was not on screen too.
    for (const n of notes) if (n.kind === 'cron' && n.acked) remember(n.ts)
    if (!hidden && present && transcriptShown && isChatPath(pathname)) {
      for (const ts of unreadCronNotesFor(notes, activeSlot)) {
        if (seen.has(ts)) continue
        remember(ts)
        void dispatch(ackNotification(ts))
      }
    }
    if (changed) saveHandled(seen)
  }, [dispatch, hidden, present, transcriptShown, pathname, activeSlot, notes])
}
