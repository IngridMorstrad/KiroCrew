import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

/** A Crew Mode worker never reaches the person on its own (owner decisions A and B,
 *  edge review R1 X2/S2/S4): its approval raises no note, chime, banner or badge
 *  (the conductor's approval card shows it), and its finished or paused turn
 *  raises no chime or toast (its conductor reads it). */
const read = (name: string) => readFileSync(path.join(__dirname, name), 'utf-8')

describe('Crew Mode workers stay quiet for the person', () => {
  it('an approval frame for a worker returns before any note, chime or badge', () => {
    const src = read('approvals.ts')
    const frame = src.slice(src.indexOf('onApprovalFrame(data, reconnecting)'))
    const early = frame.indexOf('isCrewWorkerSlot(data.slot)')
    expect(early).toBeGreaterThan(0)
    expect(early).toBeLessThan(frame.indexOf('dispatchMcNotification(APPROVAL_KIND)'))
    expect(early).toBeLessThan(frame.indexOf('dispatch(addNotification(approvalNote))'))
    // The reconnect reconcile writes the worker's row but adds no note for it.
    const reconcile = src.slice(src.indexOf('async reconcilePending'), src.indexOf('onApprovalFrame(data, reconnecting)'))
    expect(reconcile.indexOf('isCrewWorkerSlot(slot)')).toBeLessThan(reconcile.indexOf('dispatch(addNotification({'))
  })

  it('a worker turn raises no turn-done chime or toast', () => {
    const src = read('turnCompletion.ts')
    expect(src).toContain('const crewWorker = !!soundSlot?.crew_root')
    expect(src).toMatch(/&& !muteSuppresses && !crewWorker/)
  })
})
