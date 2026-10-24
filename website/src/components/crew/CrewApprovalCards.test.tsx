import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'
import { updateSlot } from '../../store/dashboardSlice'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), crewWorkerApprove: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))

import CrewApprovalCards, { CREW_APPROVAL_INPUT_MAX, crewApprovalRequests } from './CrewApprovalCards'

/** A worker slot row waiting on a tool approval, as the live slot frame carries it. */
const waiting = (key: string, extra: Record<string, unknown> = {}) => ({
  key,
  title: `Worker ${key}`,
  crew_root: 'root',
  pending_approval: true,
  pending_approval_info: {
    origin: 'native', request_id: `rid-${key}`, request_mid: `mid-${key}`,
    tool: 'Shell', tool_input: `ls ${key}`, tool_kind: 'execute', tool_purpose: 'list files',
    trust_grantable: '1',
  },
  ...extra,
})

/** Mount the stack and let its worker-list read settle inside act, so no state
 *  update lands after a test has ended (edge review R2 F8: no act() warnings). */
async function renderCards(slots: unknown[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  await act(async () => {
    render(<QueryClientProvider client={qc}><Provider store={store}><CrewApprovalCards root="root" /></Provider></QueryClientProvider>)
  })
  return store
}

/** Let a decision's mutation and its list re-read settle inside act. */
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

describe('CrewApprovalCards (owner decision B)', () => {
  afterEach(() => vi.clearAllMocks())

  it('shows one card per waiting worker request: worker, task, exact tool, three choices', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [{ key: 'w1', summary: 'Fix the build', group: 'approval' }] })
    await renderCards([{ key: 'root', crew_mode_root: true }, waiting('w1'), { key: 'plain', pending_approval: true }])
    const card = screen.getByTestId('crew-approval-card')
    expect(within(card).getByRole('heading')).toHaveTextContent('Worker w1 needs approval')
    expect(card).toHaveTextContent('Shell')
    expect(within(card).getByRole('region', { name: 'The exact tool input' })).toHaveTextContent('ls w1')
    expect(await within(card).findByText('Fix the build')).toBeInTheDocument()
    for (const name of ['Approve', 'Reject', 'Trust this worker']) {
      expect(within(card).getByRole('button', { name })).toBeInTheDocument()
    }
    // Another root's worker, and a plain session, get no card here.
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
  })

  it.each([
    ['Approve', 'approve'],
    ['Reject', 'reject'],
    ['Trust this worker', 'trust'],
  ] as const)('%s decides on the worker slot through the crew route', async (label, action) => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: label }))
    await waitFor(() => expect(api.crewWorkerApprove).toHaveBeenCalledWith('root', 'w1', action, 'rid-w1', 'mid-w1', ''))
    await waitFor(() => expect(screen.queryByTestId('crew-approval-card')).toBeNull())
    await settle()
  })

  it('pins a coordinator request to its record instance (edge review R2 F7)', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockResolvedValue({ ok: true })
    const row = waiting('w1')
    await renderCards([{ ...row, pending_approval_info: { origin: 'coordinator', request_id: 'spawn:abc', request_instance: 'a1b2c3', tool: 'spawn_run', tool_input: '', tool_kind: 'spawn' } }])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.crewWorkerApprove).toHaveBeenCalledWith('root', 'w1', 'approve', 'spawn:abc', '', 'a1b2c3'))
    await settle()
  })

  it('offers Trust only when the request is trust-grantable', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const row = waiting('w1')
    await renderCards([{ ...row, pending_approval_info: { ...row.pending_approval_info, trust_grantable: undefined } }])
    expect(screen.queryByRole('button', { name: 'Trust this worker' })).toBeNull()
  })

  it('leaves the stack live when the request is decided elsewhere', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const store = await renderCards([waiting('w1'), waiting('w2')])
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(2)
    act(() => { store.dispatch(updateSlot({ key: 'w1', pending_approval: false, pending_approval_info: null } as never)) })
    expect(screen.getAllByTestId('crew-approval-card')).toHaveLength(1)
  })

  it('says so when a decision fails, and keeps the card', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    api.crewWorkerApprove.mockRejectedValue(new Error('404'))
    await renderCards([waiting('w1')])
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Could not send that decision. It may already be decided.')).toBeInTheDocument()
    expect(screen.getByTestId('crew-approval-card')).toBeInTheDocument()
    await settle()
  })

  it('stacks several requests oldest first and caps a long input', async () => {
    const long = 'x'.repeat(CREW_APPROVAL_INPUT_MAX + 50)
    const first = crewApprovalRequests([waiting('a')] as never, 'root', new Map(), 1000)
    expect(first.map(r => r.worker)).toEqual(['a'])
    const both = crewApprovalRequests([waiting('b'), waiting('a')] as never, 'root', new Map(), 2000)
    expect(both.map(r => r.worker)).toEqual(['a', 'b'])
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const row = waiting('c')
    await renderCards([{ ...row, pending_approval_info: { ...row.pending_approval_info, tool_input: long } }])
    const pre = screen.getByRole('region', { name: 'The exact tool input' })
    expect(pre.textContent?.length).toBe(CREW_APPROVAL_INPUT_MAX + 1)
  })

  it('renders nothing when no worker waits', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderCards([{ key: 'root', crew_mode_root: true }])
    expect(screen.queryByTestId('crew-approval-cards')).toBeNull()
  })
})
