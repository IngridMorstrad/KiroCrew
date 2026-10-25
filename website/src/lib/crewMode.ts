/** Crew Mode: a UI over a conductor agent.
 *
 *  Every conductor is Crew Mode: a chat is in Crew Mode exactly when it runs a
 *  conductor TEMPLATE (or the agent its Crew Mode stamp names), so the composer
 *  switch is an ordinary agent switch from the default agent to the conductor.
 *  WHICH conductor is the gateway's `agent.crew_mode_agent`; the gateway moves
 *  every Crew Mode chat onto it when it changes, and refuses any other switch of
 *  a Crew Mode chat (`409 crew_mode_locked`). The conductor list is the gateway's
 *  one set (`kiro_crew.crew_mode.conductor_agents`);
 *  it arrives with the agent catalog (`GET /api/agents/catalog`, read by
 *  `useAgents`), which records them here. Until the catalog lands the shipped
 *  conductors stand in.
 */

/** The conductor Crew Mode runs while the gateway has not said otherwise. */
export const DEFAULT_CREW_MODE_AGENT = 'kirocrew-conductor'

/** The longest steer a person may type into a worker: the server's
 *  `MAX_SEND_MESSAGE_CHARS`, which the steer route and the worker's own composer
 *  send both refuse past (`message_too_long`). */
export const CREW_STEER_MAX_CHARS = 50_000

/** The shipped conductors, every one of them Crew Mode, until the catalog lands. */
export const SHIPPED_CONDUCTORS: readonly string[] = [
  DEFAULT_CREW_MODE_AGENT,
  'kirocrew-ledger-conductor',
  'kirocrew-pipeline-conductor',
  'kirocrew-security-conductor',
  'kirocrew-team-lead',
]

let configuredAgent = DEFAULT_CREW_MODE_AGENT
let conductors: ReadonlySet<string> = new Set(SHIPPED_CONDUCTORS)

/** Record the catalog's Crew Mode facts. Called by `useAgents` on every load. */
export function setCrewModeCatalog(
  agent: string | undefined,
  conductorAgents: readonly string[] | undefined,
): void {
  configuredAgent = agent || DEFAULT_CREW_MODE_AGENT
  conductors = new Set([...(conductorAgents ?? SHIPPED_CONDUCTORS), configuredAgent])
}

/** The agent a chat switches to when Crew Mode is turned on. */
export function crewModeAgent(): string {
  return configuredAgent
}

/** Whether `name` is one of the gateway's conductors. */
export function isConductorAgent(name: string | null | undefined): boolean {
  return !!name && conductors.has(name)
}

/** Whether a session on `agent` in namespace `kind` runs a conductor TEMPLATE. A
 *  crewmate that carries a conductor's name is that crewmate, not a conductor. */
export function runsConductor(agent: string | null | undefined, kind: string | null | undefined): boolean {
  return kind !== 'member' && isConductorAgent(agent)
}

/** Whether a session on `agent` in namespace `kind` is in Crew Mode: on any
 *  conductor, or on the agent its `stamp` names (a custom conductor the config no
 *  longer names, until the gateway moves the chat onto the configured one). */
export function runsCrewMode(agent: string | null | undefined, kind: string | null | undefined, stamp?: string | null): boolean {
  if (kind === 'member' || !agent) return false
  return conductors.has(agent) || (!!stamp && stamp === agent)
}

/** Whether `name` is the default agent: the switch is offered only there. The
 *  literal `kirocrew` counts only while no default agent is configured; with
 *  one configured, a chat on the `kirocrew` template is that template. */
export function isDefaultAgentName(name: string, defaultAgent: string | undefined): boolean {
  if (name === '') return true
  return defaultAgent ? name === defaultAgent : name === 'kirocrew'
}

/** Crew Mode needs a Persistent chat: an Incognito or Temporary session may not
 *  open sessions, so a conductor there could not conduct. */
export function memoryModeAllowsCrewMode(memoryMode: string | undefined): boolean {
  return (memoryMode ?? 'persistent') === 'persistent'
}

/** Rows of a normal chat's agent picker: every conductor left out, since
 *  Crew Mode is the way into it. Crewmates are kept whatever their name. */
export function withoutConductors<T extends { name: string; selection_kind?: string }>(choices: T[]): T[] {
  return choices.filter(c => c.selection_kind === 'member' || !isConductorAgent(c.name))
}

/** Every surface that can show a session's pending tool approval. Listed so one
 *  test walks them all; a new surface joins the list and the rule below. */
export const CREW_APPROVAL_SURFACES = [
  'conductor_card',       // the conductor's approval card stack (CrewApprovalCards)
  'worker_main_pane',     // the worker's own composer in the main pane
  'worker_split_pane',    // the worker's composer in a Split View pane
  'conductor_side_panel', // the conductor side panel's Approvals and attention tab
  'session_dashboards',   // All Dashboards' Needs you section
] as const
export type CrewApprovalSurface = typeof CREW_APPROVAL_SURFACES[number]

/** Whether *surface* may draw decision buttons (Approve, Reject, Trust) for
 *  *slot*'s pending tool approval. The one rule, for every surface: a Crew Mode
 *  worker's approval (a row with `crew_root`) is decided ONLY on its conductor's
 *  approval card; every other surface shows it as info with a way to that card.
 *  A session that is no worker keeps its usual controls everywhere. */
export function crewApprovalDecidableHere(
  slot: { crew_root?: string | null } | null | undefined, surface: CrewApprovalSurface,
): boolean {
  if (!slot?.crew_root) return true
  return surface === 'conductor_card'
}

/** The person's own session list: a Crew Mode root's workers are left out (the
 *  side panel lists them under their root). `rootsOnly` keeps just the roots,
 *  for the session filter's Crew Mode chip. */
export function crewModeView<T extends { crew_root?: string; crew_mode_root?: boolean }>(rows: T[], rootsOnly = false): T[] {
  return rows.filter(r => (rootsOnly ? !!r.crew_mode_root : !r.crew_root))
}

/** The agent the keyboard cycle lands on from `current`, `step` places along
 *  (`1` next, `-1` previous), over the same conductor-free list the chat picker
 *  shows, so a chord can never move a normal chat onto a conductor. `null` when
 *  there is nothing to cycle: an empty list, or a chat that already runs a
 *  conductor template, which has no agent picker to mirror. */
export function agentCycleTarget<T extends { name: string; selection_kind?: string }>(
  agents: T[], current: string, currentKind: string | undefined, step: 1 | -1, crewStamp?: string,
): string | null {
  // A Crew Mode chat has no picker to mirror, stamp included; another conductor's
  // chat cycles over the conductor-free list like any chat.
  if (runsCrewMode(current, currentKind, crewStamp)) return null
  const rows = withoutConductors(agents)
  if (rows.length === 0) return null
  const idx = rows.findIndex(a => a.name === current)
  const next = idx === -1 ? (step === 1 ? 0 : rows.length - 1) : (idx + step + rows.length) % rows.length
  return rows[next].name
}
