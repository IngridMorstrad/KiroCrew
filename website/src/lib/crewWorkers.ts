/** Crew Mode workers grouped the way the person reads them: a tool approval only
 *  they can give, what is moving, what waits on its conductor, what is idle, what
 *  is finished.
 *
 *  A worker never asks the person itself: its questions and blockers go to its
 *  conductor (`with_conductor`), which answers or asks in its own chat. The one
 *  thing a conductor cannot answer is a tool approval (`approval`), resolved on
 *  the conductor's approval card. The rule is the server's (`crew_workers._group`),
 *  from real signals only (a closed tab, the worker's ledger item, an open
 *  approval), never from "it has replied". The session list's ghost stack and
 *  "Approval needed" / "Done x/y" line, the breadcrumb and the side panel's cards
 *  all read the server's group. A live slot row alone cannot say a worker
 *  finished, so its mirror (`crewWorkerGroupOfRow`) has no done. */
import type { CrewWorker } from '../types'

export type CrewWorkerGroup = 'approval' | 'working' | 'with_conductor' | 'waiting' | 'done'

/** Display order: the approval only the person can give first. */
export const CREW_GROUP_ORDER: readonly CrewWorkerGroup[] = ['approval', 'working', 'with_conductor', 'waiting', 'done']

export const CREW_GROUP_LABEL_KEY: Record<CrewWorkerGroup, string> = {
  approval: 'commandCenter.worker_group_approval',
  working: 'commandCenter.worker_group_working',
  with_conductor: 'commandCenter.worker_group_with_conductor',
  waiting: 'commandCenter.worker_group_waiting',
  done: 'commandCenter.worker_group_done',
}

/** The tone each group's ghost and dot wear: theme tokens, so light and dark follow. */
export const CREW_GROUP_TONE: Record<CrewWorkerGroup, string> = {
  approval: 'crew-tone-warn',
  working: 'crew-tone-aim',
  with_conductor: 'crew-tone-muted',
  waiting: 'crew-tone-muted',
  done: 'crew-tone-ok',
}

/** The slot-row fields the grouping reads. */
export interface CrewWorkerRowLike {
  running?: boolean
  pending_approval?: boolean
  needs_input?: boolean
  messages?: number
}

/** One live worker row's group, before the worker list has answered: a slot row
 *  carries no finished signal, so an idle row is waiting, never done. */
export function crewWorkerGroupOfRow(row: CrewWorkerRowLike): CrewWorkerGroup {
  if (row.pending_approval) return 'approval'
  if (row.running) return 'working'
  if (row.needs_input) return 'with_conductor'
  return 'waiting'
}

/** One row of the worker list API's group: the server's own (`crew_workers._group`),
 *  which every row carries. `crewWorkerGroupOfRow` is the one client mirror of
 *  that rule, for live slot rows that have no API row. */
export function crewWorkerGroup(worker: CrewWorker): CrewWorkerGroup {
  return worker.group
}

/** The worker list in display order, grouped. Empty groups are left out. */
export function groupCrewWorkers(workers: CrewWorker[]): { group: CrewWorkerGroup; workers: CrewWorker[] }[] {
  return CREW_GROUP_ORDER
    .map(group => ({ group, workers: workers.filter(w => crewWorkerGroup(w) === group) }))
    .filter(g => g.workers.length > 0)
}

export interface CrewRootSummary {
  /** One entry per worker, approval first, for the row's ghost stack. */
  groups: CrewWorkerGroup[]
  /** Workers waiting on a tool approval: the conductor card's one "Approval
   *  needed" count. Questions and blockers are the conductor's, never counted. */
  approval: number
  done: number
  total: number
}

/** A Crew Mode root's workers, as the worker list API groups them, summarized
 *  for its session-list row. */
export function summarizeCrewWorkers(workers: readonly CrewWorker[]): CrewRootSummary {
  const groups = workers.map(crewWorkerGroup)
  groups.sort((a, b) => CREW_GROUP_ORDER.indexOf(a) - CREW_GROUP_ORDER.indexOf(b))
  return {
    groups,
    approval: groups.filter(g => g === 'approval').length,
    done: groups.filter(g => g === 'done').length,
    total: groups.length,
  }
}
