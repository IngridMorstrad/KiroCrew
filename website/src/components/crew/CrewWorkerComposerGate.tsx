import { useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { useAppDispatch } from '../../store'
import { useIsMobile } from '../../hooks/useIsMobile'
import { openCrewSession, reopenCrewWorker } from '../../pages/chat/command-center/CrewWorkerList'
import { clearCrewSteerNotice, useCrewSteerNotice } from '../../lib/crewSteerNotice'
import ErrorNotice from '../ErrorNotice'
import { useCrewRoot } from './useCrewRoot'

/**
 * What a Crew Mode worker's own composer says in place of the decisions it does
 * not make (edge review R2 Y1, Y2, F6). Owner decisions:
 *
 * - A worker's tool approval is decided ONLY on its conductor's approval card, so
 *   the worker's composer shows no Allow / Trust / Reject controls; this line says
 *   where the decision is, with a link that opens the conductor.
 * - With the conductor closed the worker composer does not send: this line says
 *   to reopen it, with a Reopen button. The server refuses the send too.
 * - A steer that was delivered but not recorded on the ledger is said once,
 *   muted, under the composer.
 */
export function CrewWorkerComposerGate({ root, worker, workerTitle, waitingForApproval }: {
  root: string
  worker: string
  workerTitle: string
  waitingForApproval: boolean
}) {
  const dispatch = useAppDispatch()
  const isMobile = useIsMobile()
  const { rootClosed, rootTitle } = useCrewRoot(root)
  const unrecorded = useCrewSteerNotice(worker)
  const [reopening, setReopening] = useState(false)
  const [reopenFailed, setReopenFailed] = useState(false)
  const reopen = () => {
    if (reopening) return
    setReopening(true)
    setReopenFailed(false)
    void reopenCrewWorker(dispatch, root, rootTitle, !isMobile).then(ok => {
      setReopening(false)
      if (!ok) setReopenFailed(true)
    })
  }
  return (
    <>
      {rootClosed ? (
        <div role="status" data-testid="crew-worker-root-closed"
          className="mb-1.5 px-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-muted">
          <span className="min-w-0 break-words">{i18nT('components.crewMode.worker_root_closed_composer', { name: rootTitle })}</span>
          <button type="button" onClick={reopen} disabled={reopening} data-testid="crew-worker-reopen-root"
            className="inline-flex items-center justify-center min-h-11 md:min-h-8 px-2.5 rounded-lg border border-border bg-bg-elevated text-[12px] text-text hover:bg-bg-hover cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed">
            {i18nT('components.crewMode.worker_reopen')}
          </button>
        </div>
      ) : waitingForApproval ? (
        <div role="status" data-testid="crew-worker-approval-waiting"
          className="mb-1.5 px-1 flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[12px] text-muted">
          <ShieldCheck size={13} aria-hidden="true" className="shrink-0 text-warn" />
          <span className="min-w-0 break-words">{i18nT('components.crewMode.worker_approval_waiting', { name: rootTitle })}</span>
          <button type="button" onClick={() => openCrewSession(dispatch, root, !isMobile)} data-testid="crew-worker-open-root"
            className="inline-flex items-center min-h-11 md:min-h-0 text-[12px] text-aim underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0">
            {i18nT('components.crewMode.worker_open_conductor', { name: rootTitle })}
          </button>
        </div>
      ) : null}
      {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} />}
      {unrecorded && (
        <p className="mt-1 px-1 text-[11px] text-muted" data-testid="crew-steer-not-recorded">
          {i18nT('components.crewMode.steer_not_recorded', { name: workerTitle, reason: unrecorded })}
          {' '}
          <button type="button" onClick={() => clearCrewSteerNotice(worker)}
            aria-label={i18nT('components.errorNotice.dismiss')}
            className="underline underline-offset-2 bg-transparent border-0 p-0 cursor-pointer text-muted [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11">×</button>
        </p>
      )}
    </>
  )
}
