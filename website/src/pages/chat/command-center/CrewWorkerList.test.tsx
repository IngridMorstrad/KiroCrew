import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import CrewWorkerList, { CrewWorkersTab } from './CrewWorkerList'

const W = (key: string, title: string, status: string, group: string) =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: '', ledger_item: '', created_by: 'root-1' })

const WORKERS = {
  root: 'root-1',
  workers: [
    W('w-done', 'Worker D', 'idle', 'done'),
    { ...W('w-work', 'Worker A', 'working', 'working'), summary: 'Writing a 3-line welcome note' },
    W('w-need', 'Worker B', 'needs_approval', 'approval'),
    W('w-ask', 'Worker F', 'idle', 'with_conductor'),
    W('w-wait', 'Worker C', 'idle', 'waiting'),
    { ...W('w-gone', 'Worker E', 'closed', 'done'), ledger_item: 'it_9' },
  ],
}

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return store
}

describe('CrewWorkerList (design B)', () => {
  afterEach(() => vi.clearAllMocks())

  it('groups cards Approval needed, Working, Waiting on conductor, Waiting, Done; never Needs you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    await screen.findByText('Worker B')
    const groups = screen.getAllByTestId('crew-worker-group')
    expect(groups.map(g => g.getAttribute('data-group'))).toEqual(['approval', 'working', 'with_conductor', 'waiting', 'done'])
    expect(within(groups[0]).getByRole('heading')).toHaveTextContent('Approval needed · 1')
    // A worker's question waits on its conductor, never on the person (owner decision A).
    expect(within(groups[2]).getByRole('heading')).toHaveTextContent('Waiting on conductor · 1')
    expect(screen.queryByText(/Needs you/)).toBeNull()
    expect(within(groups[4]).getByRole('heading')).toHaveTextContent('Done · 2')
    expect(screen.getByTestId('crew-projects-soon')).toHaveTextContent('Projects tab · coming soon')
  })

  it('opens a worker in the main pane with the Workers tab in front, and marks the one on screen', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<CrewWorkerList root="root-1" viewing="w-need" active />)
    const current = await screen.findByRole('button', { name: /Worker B/ })
    expect(current).toHaveAttribute('aria-current', 'true')
    fireEvent.click(screen.getByRole('button', { name: /Worker A/ }))
    expect(panel.openPanelView).toHaveBeenCalledWith('w-work', 'workers')
    // A plain switch, like a tab click: the main pane now shows that worker.
    expect(store.getState().chat.activeSlot).toBe('w-work')
    // On desktop the Workers panel stays beside it (U2): the target's own,
    // usually closed, panel state is not restored over it.
    expect(store.getState().chat.activityOpen).toBe(true)
  })

  it('a card says the task in one line under its title', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    const card = (await screen.findByText('Worker A')).closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(within(card).getByTestId('crew-worker-summary')).toHaveTextContent('Writing a 3-line welcome note')
    const plain = screen.getByText('Worker C').closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(within(plain).queryByTestId('crew-worker-summary')).toBeNull()
  })

  it('reopens a worker whose tab has closed from its saved transcript (X4)', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'w-gone', messages: [] })
    const store = renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-gone'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-gone', 'Worker E')
  })

  it('says so when a closed worker cannot be reopened', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockRejectedValue(new Error('gone'))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen Worker E to read it' }))
    expect(await screen.findByText('Could not reopen that session. Try again.')).toBeInTheDocument()
  })

  it('shows a loading line while the first read is in flight (X6)', () => {
    api.crewWorkers.mockReturnValue(new Promise(() => {}))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(screen.getByTestId('crew-workers-loading')).toHaveTextContent('Loading the workers…')
  })

  it('shows no raw ledger id on a card', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    const card = (await screen.findByText('Worker E')).closest('[data-testid="crew-worker-row"]') as HTMLElement
    expect(card).not.toHaveTextContent('it_9')
  })

  it('says when the list cannot be read', async () => {
    api.crewWorkers.mockRejectedValue(new Error('nope'))
    renderWith(<CrewWorkerList root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('Could not load the workers.')).toBeInTheDocument()
  })

  it('the Workers tab finds the root from a worker row and says so on a plain chat', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkersTab slot="w-need" active />, [{ key: 'w-need', crew_root: 'root-1' }, { key: 'plain' }])
    await screen.findByText('Worker B')
    expect(api.crewWorkers).toHaveBeenCalledWith('root-1')
  })

  it('the Workers tab on a plain chat lists nothing', () => {
    renderWith(<CrewWorkersTab slot="plain" active />, [{ key: 'plain' }])
    expect(screen.getByTestId('crew-workers-none')).toBeInTheDocument()
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })
})
