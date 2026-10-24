import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import type { AcceptedWork } from '../../api/sharedCrew'
import type { WorkListResponse } from '../../api/sharedCrewUi'
import { applySharedCrewFrame, onSharedCrewSocketOpen, sendActivity, setSharedCrewSender, sharedCrewKeys } from './sharedCrewSocket'

const work = (revision: number, state: AcceptedWork['state']): AcceptedWork => ({
  work_id: 'w1', session_key: 's1', operation: 'chat_turn', state, revision,
  accepted_at: 1, expires_at: 2, deadline_at: null, reason: null,
})
const frame = (w: AcceptedWork) => ({ type: 'shared_crew.work', schema_version: 1, work: w })

afterEach(() => setSharedCrewSender(null))

describe('shared crew frames', () => {
  it('folds work frames by id and revision, so a replay never duplicates', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, frame(work(1, 'queued')))
    applySharedCrewFrame(qc, frame(work(2, 'running')))
    applySharedCrewFrame(qc, frame(work(1, 'queued')))
    const items = qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('s1'))?.items
    expect(items).toHaveLength(1)
    expect(items?.[0].state).toBe('running')
  })

  it('files a chat work frame under the tab key the work list reads', () => {
    const qc = new QueryClient()
    const chat = { ...work(1, 'queued'), session_key: 'dashboard:tab-1' }
    applySharedCrewFrame(qc, { ...frame(chat), slot_key: 'tab-1' })
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('tab-1'))?.items[0].state).toBe('queued')
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('dashboard:tab-1'))?.items).toHaveLength(1)
  })

  it('an HTTP read that lands before a newer frame is overtaken, not kept', () => {
    const qc = new QueryClient()
    qc.setQueryData<WorkListResponse>(sharedCrewKeys.work('s1'), { items: [work(1, 'queued')] })
    applySharedCrewFrame(qc, frame(work(4, 'completed')))
    expect(qc.getQueryData<WorkListResponse>(sharedCrewKeys.work('s1'))?.items[0].state).toBe('completed')
  })

  it('replaces presence as a whole value', () => {
    const qc = new QueryClient()
    const entry = { principal: { id: 'p_a', kind: 'human', display: { name: 'A', login: '', captured_at: null } }, session_key: 's1', activity: 'typing', connections: 2, expires_at: 9 }
    applySharedCrewFrame(qc, { type: 'shared_crew.presence', schema_version: 1, session_key: 's1', entries: [entry] })
    applySharedCrewFrame(qc, { type: 'shared_crew.presence', schema_version: 1, session_key: 's1', entries: [] })
    expect(qc.getQueryData<{ entries: unknown[] }>(sharedCrewKeys.presence('s1'))?.entries).toEqual([])
  })

  it('ignores malformed frames', () => {
    const qc = new QueryClient()
    applySharedCrewFrame(qc, null)
    applySharedCrewFrame(qc, { type: 'shared_crew.work' })
    applySharedCrewFrame(qc, { type: 'shared_crew.decision', decision: {} })
    expect(qc.getQueryCache().getAll()).toHaveLength(0)
  })

  it('a reopened socket re-reads every Shared Crew query', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    onSharedCrewSocketOpen(qc)
    expect(spy).toHaveBeenCalledWith({ queryKey: ['shared-crew'] }, { cancelRefetch: false })
  })
})

describe('activity frames', () => {
  it('carry a session and an activity, never a principal or text', () => {
    const sent: object[] = []
    setSharedCrewSender(p => sent.push(p))
    expect(sendActivity('s1', 'typing')).toBe(true)
    expect(sent).toEqual([{ type: 'shared_crew.activity', session_key: 's1', activity: 'typing' }])
  })

  it('send nothing without a socket or a session', () => {
    expect(sendActivity('s1', 'viewing')).toBe(false)
    setSharedCrewSender(() => { throw new Error('should not send') })
    expect(sendActivity('', 'viewing')).toBe(false)
  })
})
