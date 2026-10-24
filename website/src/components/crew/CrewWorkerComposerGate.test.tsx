import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))

import { CrewWorkerComposerGate } from './CrewWorkerComposerGate'
import { clearCrewSteerNotice, noteCrewSteerReceipt } from '../../lib/crewSteerNotice'

async function renderGate(slots: unknown[], waitingForApproval = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  await act(async () => {
    render(
      <QueryClientProvider client={qc}><Provider store={store}>
        <CrewWorkerComposerGate root="root" worker="w1" workerTitle="Worker B" waitingForApproval={waitingForApproval} />
      </Provider></QueryClientProvider>,
    )
  })
  return store
}

const live = [{ key: 'root', title: 'Sample goal', crew_mode_root: true }, { key: 'w1', title: 'Worker B', crew_root: 'root' }]

describe('CrewWorkerComposerGate (edge review R2)', () => {
  afterEach(() => { vi.clearAllMocks(); clearCrewSteerNotice('w1') })

  it('F6: says when a steer was delivered but not recorded, and not otherwise', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: null, ledger_error: 'crew_log_off' }) })
    expect(screen.getByTestId('crew-steer-not-recorded')).toHaveTextContent('Delivered to Worker B, but not recorded on the ledger (crew_log_off).')
    // A later recorded steer clears it; a plain send (no ledger fields) leaves it alone.
    act(() => { noteCrewSteerReceipt('w1', { ok: true }) })
    expect(screen.getByTestId('crew-steer-not-recorded')).toBeInTheDocument()
    act(() => { noteCrewSteerReceipt('w1', { ok: true, ledger_event: 'it_1', ledger_error: null }) })
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
  })

  it('F6: a queued steer (recorded at delivery) is not called unrecorded', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    await renderGate(live)
    act(() => { noteCrewSteerReceipt('w1', { ok: true, queued: true, ledger_event: null, ledger_error: null, ledger_pending: true }) })
    expect(screen.queryByTestId('crew-steer-not-recorded')).toBeNull()
  })

  it('Y1: names the conductor where the approval is decided, with a link to it', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', workers: [] })
    const store = await renderGate(live, true)
    expect(screen.getByTestId('crew-worker-approval-waiting')).toHaveTextContent('Waiting for approval in Sample goal')
    fireEvent.click(screen.getByTestId('crew-worker-open-root'))
    expect(store.getState().chat.activeSlot).toBe('root')
  })

  it('Y2: with the conductor closed it says to reopen it, from its saved name', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root', root_closed: true, root_title: 'Toy Flag Review', workers: [] })
    await renderGate([{ key: 'w1', title: 'Worker B', crew_root: 'root' }], true)
    expect(await screen.findByText('Reopen Toy Flag Review to keep working')).toBeInTheDocument()
    expect(screen.getByTestId('crew-worker-reopen-root')).toHaveTextContent('Reopen')
    // The approval line gives way: nothing can be decided until the conductor is back.
    expect(screen.queryByTestId('crew-worker-approval-waiting')).toBeNull()
  })
})
