/** Who may open this Crew over Tailscale (`/api/shared-crew/access`).
 *
 *  Tailscale is the only access authority. The gateway computes the set from
 *  the owner, the people who accepted a Crew invite and the people the owner
 *  approved here; nobody types it. These are the owner-only read and writes. */

export interface WaitingPerson {
  user_id: string
  login: string
  display_name: string
  /** The person's Tailscale profile picture (https only), display only. */
  avatar_url: string
  node: string
  first_seen: number
  last_seen: number
  attempts: number
}

export interface ApprovedPerson {
  user_id: string
  login: string
  display_name: string
  avatar_url: string
  first_seen: number
  approved_at: number
  approved_by: string
}

export interface HostPower {
  state: 'available' | 'unavailable'
  reason: string
  backend: string
  laptop: boolean
  laptop_reason: string
}

export interface FirstShare {
  /** True until the owner acknowledged the warning, while nobody else has access. */
  needs_ack: boolean
  /** Sessions teammates will see; `null` when the count could not be read. */
  sessions: number | null
  /** Crew memory items teammates will see; `null` when unknown. */
  memory_items: number | null
  host: HostPower
}

export interface AccessView {
  readable: boolean
  waiting: WaitingPerson[]
  approved: ApprovedPerson[]
  /** Entries the first start carried over from the old config allowlist. */
  seeded: string[]
  first_share_ack: boolean
  first_share: FirstShare
}

/** The header the dashboard sends after its own dialog, for an action that cannot be undone. */
export const CONFIRM_IRREVERSIBLE_HEADER = { 'X-Kirocrew-Confirm': 'irreversible' } as const

/** A picture URL the browser may load: plain https, no credentials. Anything else shows initials. */
export function safeAvatarUrl(url: string | undefined): string {
  if (!url || !url.startsWith('https://')) return ''
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : ''
  } catch {
    return ''
  }
}

export function initials(name: string): string {
  return (name.trim()[0] ?? '?').toUpperCase()
}
