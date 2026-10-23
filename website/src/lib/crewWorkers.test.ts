import { describe, expect, it } from 'vitest'
import { CREW_GROUP_ORDER, crewWorkerGroup, crewWorkerGroupOfRow, groupCrewWorkers, summarizeCrewWorkers } from './crewWorkers'
import type { CrewWorker } from '../types'

describe('crewWorkers', () => {
  it('a live row alone never says done: replying is not finishing', () => {
    expect(crewWorkerGroupOfRow({ pending_approval: true, running: true })).toBe('approval')
    // A worker's question is its conductor's (edge review R1 S2, owner decision A).
    expect(crewWorkerGroupOfRow({ needs_input: true })).toBe('with_conductor')
    expect(crewWorkerGroupOfRow({ running: true })).toBe('working')
    expect(crewWorkerGroupOfRow({ messages: 3 })).toBe('waiting')
    expect(crewWorkerGroupOfRow({ messages: 0 })).toBe('waiting')
  })

  it('takes the group the API row carries', () => {
    const w = (over: Partial<CrewWorker>) => ({ key: 'k', title: '', agent: '', status: 'idle', group: 'waiting', last_activity: '', ledger_item: '', created_by: '', ...over }) as CrewWorker
    expect(crewWorkerGroup(w({ group: 'done', status: 'idle' }))).toBe('done')
    expect(groupCrewWorkers([w({ key: 'a', group: 'done' }), w({ key: 'b', group: 'approval' }), w({ key: 'c', group: 'with_conductor' })]).map(g => g.group))
      .toEqual(['approval', 'with_conductor', 'done'])
  })

  it('has no group that says a worker needs the person', () => {
    expect(CREW_GROUP_ORDER).not.toContain('needs_you')
  })

  // The server's table (test_crew_mode_workers.py: status, messages -> group), restated
  // as live-row flags, so the client mirror cannot drift from it.
  it.each([
    [{ pending_approval: true, messages: 4 }, 'approval'],
    [{ needs_input: true, messages: 4 }, 'with_conductor'],
    [{ running: true, messages: 0 }, 'working'],
    // The server says done only from a closed tab or the ledger; a row has neither.
    [{ messages: 2 }, 'waiting'],
    [{ messages: 0 }, 'waiting'],
  ] as const)('a live row %o groups as %s', (row, group) => {
    expect(crewWorkerGroupOfRow(row)).toBe(group)
  })

  it('summarizes the API groups, approval first, and counts only approvals for the person', () => {
    const w = (group: CrewWorker['group']) => ({ key: group, title: '', agent: '', status: 'idle', group, last_activity: '', ledger_item: '', created_by: '' }) as CrewWorker
    expect(summarizeCrewWorkers([w('done'), w('with_conductor'), w('approval'), w('waiting')]))
      .toEqual({ groups: ['approval', 'with_conductor', 'waiting', 'done'], approval: 1, done: 1, total: 4 })
  })
})
