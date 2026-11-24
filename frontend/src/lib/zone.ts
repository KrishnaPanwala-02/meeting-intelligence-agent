// Time zones. A meeting's wall-clock time is stored naive, together with the IANA zone it is in.

const FALLBACK = [
  'UTC', 'Europe/London', 'Europe/Dublin', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Europe/Rome', 'Europe/Moscow', 'Africa/Cairo',
  'Africa/Lagos', 'Africa/Johannesburg', 'Asia/Dubai', 'Asia/Karachi', 'Asia/Kolkata', 'Asia/Dhaka', 'Asia/Bangkok', 'Asia/Singapore',
  'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Asia/Seoul', 'Australia/Perth', 'Australia/Sydney', 'Pacific/Auckland',
  'America/Sao_Paulo', 'America/Argentina/Buenos_Aires', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Toronto', 'America/Mexico_City', 'Pacific/Honolulu',
]

/** The time zone this browser is set to, e.g. "Asia/Kolkata". */
export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** Every zone this browser knows (a short list if it cannot say), with `extra` included. */
export function allZones(...extra: (string | null | undefined)[]): string[] {
  let zones: string[] = FALLBACK
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone')
    if (supported?.length) zones = supported.includes('UTC') ? supported : ['UTC', ...supported]
  } catch {
    /* use the short list */
  }
  const set = new Set(zones)
  for (const z of extra) if (z) set.add(z)
  return [...set].sort((a, b) => (a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)))
}

/** The real moment a wall-clock time ("2026-11-13T10:00:00") is, in the zone `tz`. */
export function instantOf(naive: string, tz: string | null | undefined): Date {
  const clean = naive.replace(/(Z|[+-]\d{2}:?\d{2})$/, '')
  const [d, t = '00:00:00'] = clean.split('T')
  const [y, mo, da] = d.split('-').map(Number)
  const [h, mi, s = 0] = t.split(':').map(Number)
  if (!tz) return new Date(y, mo - 1, da, h, mi, Math.floor(s)) // no zone: the viewer's own
  const asUtc = Date.UTC(y, mo - 1, da, h, mi, Math.floor(s))
  // Find the zone's offset at that moment, then back it out (twice, for times near a clock change).
  let guess = asUtc
  for (let i = 0; i < 2; i++) guess = asUtc - offsetMinutes(new Date(guess), tz) * 60_000
  return new Date(guess)
}

/** Minutes the zone is ahead of UTC at the given moment. */
export function offsetMinutes(at: Date, tz: string): number {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).formatToParts(at)
    const get = (k: string) => Number(parts.find((p) => p.type === k)?.value)
    return Math.round((Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second')) - at.getTime()) / 60_000)
  } catch {
    return 0
  }
}

/** A short name for the zone at that moment: "IST", "GMT+5:30", "PST". */
export function zoneAbbr(tz: string, at: Date = new Date()): string {
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'short' }).formatToParts(at).find((p) => p.type === 'timeZoneName')?.value ?? tz
  } catch {
    return tz
  }
}
