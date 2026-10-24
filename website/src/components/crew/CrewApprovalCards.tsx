import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'
import { useAppSelector } from '../../store'
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { useCrewWorkerSummary, crewWorkersQueryKey } from '../../pages/chat/command-center/CrewWorkerList'
import type { ChatSlot, PendingApproval } from '../../types'

/** The longest tool input a card shows; the server cuts its copy the same way. */
export const CREW_APPROVAL_INPUT_MAX = 2000

/** One request's key in `firstSeen`: its worker and request id. */
const requestKey = (worker: string, requestId: string): string => [worker, requestId].join('\u0000')

/** When this tab first saw each pending request, so the stack is oldest first. */
const firstSeen = new Map<string, number>()

export interface CrewApprovalRequest {
  worker: string
  title: string
  task: string
  approval: PendingApproval & { trust_grantable?: string }
}

/** The pending tool approvals of *root*'s workers, oldest first.
 *
 *  Read from the live slot rows, so a request decided anywhere else (the worker's
 *  own chat, another window) leaves the stack on the next slot frame. The task
 *  line is the worker list's (its ledger item's summary or title, redacted on the
 *  server). Exported for tests. */
export function crewApprovalRequests(
  slots: readonly ChatSlot[],
  root: string,
  tasks: ReadonlyMap<string, string>,
  now: number = Date.now(),
): CrewApprovalRequest[] {
  const out: CrewApprovalRequest[] = []
  for (const slot of slots) {
    const info = slot.pending_approval_info
    if (slot.crew_root !== root || !slot.pending_approval || !info?.request_id) continue
    const id = requestKey(slot.key, info.request_id)
    if (!firstSeen.has(id)) firstSeen.set(id, now)
    out.push({ worker: slot.key, title: sessionDisplayTitle(slot), task: tasks.get(slot.key) ?? '', approval: info })
  }
  out.sort((a, b) =>
    (firstSeen.get(requestKey(a.worker, a.approval.request_id)) ?? 0)
    - (firstSeen.get(requestKey(b.worker, b.approval.request_id)) ?? 0))
  return out
}

/**
 * The Crew Mode conductor's approval card stack, above its composer.
 *
 * A worker never asks the person anything: its questions go to its conductor. A
 * tool approval is the one exception, because only the person may grant it. Each
 * waiting request gets a small card naming the worker, what it is working on and
 * the exact tool call, with Approve, Reject and Trust this worker. The decision
 * runs the existing per-slot approve route on the WORKER's own slot, through the
 * root's owner-only crew route (`POST .../crew-workers/{worker}/approve`).
 */
export default function CrewApprovalCards({ root }: { root: string }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  const { workers } = useCrewWorkerSummary(root)
  const tasks = new Map((workers ?? []).map(w => [w.key, w.summary ?? ''] as const))
  const requests = crewApprovalRequests(slots, root, tasks)
  if (requests.length === 0) return null
  return (
    <section
      aria-label={i18nT('components.crewMode.approvals_label', { n: requests.length })}
      className="mx-2 mb-2 space-y-2 max-h-[45vh] overflow-y-auto"
      data-testid="crew-approval-cards"
    >
      {requests.map(r => <CrewApprovalCard key={`${r.worker}:${r.approval.request_id}`} root={root} request={r} />)}
    </section>
  )
}

function CrewApprovalCard({ root, request }: { root: string; request: CrewApprovalRequest }) {
  const queryClient = useQueryClient()
  const [done, setDone] = useState(false)
  const locked = useRef(false)
  const { worker, title, task, approval } = request
  const input = approval.tool_input || ''
  const shown = input.length > CREW_APPROVAL_INPUT_MAX ? `${input.slice(0, CREW_APPROVAL_INPUT_MAX)}…` : input
  const headingId = `crew-approval-${worker}-${approval.request_id}`
  const mutation = useMutation({
    retry: false,
    mutationFn: (action: 'approve' | 'reject' | 'trust') =>
      api.crewWorkerApprove(root, worker, action, approval.request_id, approval.request_mid || '', approval.request_instance || ''),
    onSuccess: () => setDone(true),
    onSettled: () => {
      locked.current = false
      void queryClient.invalidateQueries({ queryKey: crewWorkersQueryKey(root) })
    },
  })
  const decide = (action: 'approve' | 'reject' | 'trust') => {
    if (locked.current || done) return
    locked.current = true
    mutation.mutate(action)
  }
  if (done) return null
  return (
    <article
      aria-labelledby={headingId}
      className="rounded-lg border border-warn bg-card p-3 space-y-2 text-text"
      data-testid="crew-approval-card"
      data-worker={worker}
    >
      <h3 id={headingId} className="flex items-center gap-1.5 text-[13px] font-semibold min-w-0">
        <ShieldCheck size={15} aria-hidden="true" className="shrink-0 text-warn" />
        <span className="truncate">{i18nT('components.crewMode.approval_title', { name: title })}</span>
      </h3>
      {task && <p className="text-[12px] text-muted break-words line-clamp-2">{task}</p>}
      <p className="text-[12px] break-words">
        <span className="font-semibold">{approval.tool || i18nT('components.crewMode.approval_tool_unknown')}</span>
        {approval.tool_purpose ? <span className="text-muted"> · {approval.tool_purpose}</span> : null}
      </p>
      {shown && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact command.
        <pre tabIndex={0} role="region" aria-label={i18nT('components.crewMode.approval_input_label')}
          className="max-w-full max-h-40 overflow-auto whitespace-pre-wrap break-all bg-bg-hover rounded-md px-2 py-1.5 text-[12px] font-mono"
        >{shown}</pre>
      )}
      <ErrorNotice message={mutation.error ? i18nT('components.crewMode.approval_failed') : undefined} />
      <div className="flex flex-wrap gap-2">
        <Btn primary className="min-h-11" disabled={mutation.isPending} onClick={() => decide('approve')}>
          {i18nT('components.crewMode.approval_approve')}
        </Btn>
        <Btn className="min-h-11" disabled={mutation.isPending} onClick={() => decide('reject')}>
          {i18nT('components.crewMode.approval_reject')}
        </Btn>
        {approval.trust_grantable === '1' && (
          <Btn className="min-h-11" disabled={mutation.isPending} onClick={() => decide('trust')}
            title={i18nT('components.crewMode.approval_trust_hint')}>
            {i18nT('components.crewMode.approval_trust')}
          </Btn>
        )}
      </div>
    </article>
  )
}
