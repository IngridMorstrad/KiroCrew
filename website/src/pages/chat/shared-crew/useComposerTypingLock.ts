/** Whether this tab's composer is locked because someone else is typing in the
 *  open shared session, and who. `null` means unlocked: no lease, the lease is
 *  this person's own (any of their tabs), the Crew does not report presence, or
 *  the viewer is not known. A single operator's Crew never locks. */
import { useMemo } from 'react'
import { leaseLocks, useRefusedLease } from '../../../api/typingLease'
import { capabilityUsable, principalInitials, principalName } from '../../../api/sharedCrewUi'
import { useSessionTypingLease, useSharedCrewCapabilities, useSharedCrewViewer } from '../../../hooks/sharedCrew/useSharedCrew'
import { i18nT } from '../../../i18n/t'

export interface ComposerTypingLock {
  holderId: string
  holderName: string
  holderInitials: string
}

export function useComposerTypingLock(sessionKey: string | null | undefined): ComposerTypingLock | null {
  const { capabilities } = useSharedCrewCapabilities()
  const presenceOn = capabilityUsable(capabilities.presence)
  const viewer = useSharedCrewViewer(presenceOn)
  const lease = useSessionTypingLease(sessionKey, presenceOn)
  const refused = useRefusedLease(sessionKey)
  // A refusal's lease wins only while it is newer than the last frame or read.
  const current = refused && refused.at > lease.dataUpdatedAt ? refused.lease : lease.data
  const viewerData = viewer.data
  return useMemo(() => {
    if (!presenceOn || !current || !leaseLocks(current, viewerData) || !current.holder) return null
    const holder = current.holder
    return {
      holderId: holder.id,
      holderName: principalName(holder) || i18nT('sharedCrew.unattributed'),
      holderInitials: principalInitials(holder),
    }
  }, [presenceOn, current, viewerData])
}
