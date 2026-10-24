/**
 * Screenshots + assertions for #23572: "Optimize prompt" stays in the composer
 * while a turn runs.
 *
 * Drives the isolated capture entry (website/capture/optimize-while-busy.html),
 * which mounts the REAL ChatInput in four states: idle with a draft, a running
 * turn with a draft (queue), the same with steer available (split button), and
 * a running turn with an empty composer (Stop).
 *
 * Asserted before each PNG, per row: the send-slot control the state calls for
 * is on screen, and Optimize is present and enabled exactly when there is a
 * draft. Pass --record-only to photograph a tree without asserting (the
 * "before" frame from main, where the busy rows have no Optimize).
 *
 * Starts its own loopback vite dev server.
 *
 * Usage (in website/):
 *   node scripts/capture-23572-optimize-while-busy.mjs ../temp-screenshots/23572 after
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'

const args = process.argv.slice(2).filter(a => !a.startsWith('--'))
const OUT = args[0] || '../temp-screenshots/23572-optimize-while-busy'
const NAME = args[1] || 'after'
const RECORD_ONLY = process.argv.includes('--record-only')
mkdirSync(OUT, { recursive: true })

// Its own loopback dev server on a free port, closed at the end.
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

/** episode id -> [the send-slot control's locator, Optimize enabled?] */
const EXPECTED = {
  idle: ['button[aria-label="Send"]', true],
  queue: ['button[aria-label="Queue message"]', true],
  steer: ['[data-testid="busy-send-button"]', true],
  stop: ['[data-testid="stop-button-armed"], button[aria-label="Stop"]', false],
}

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 900, height: 620 }, deviceScaleFactor: 2 })
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/optimize-while-busy.html?theme=${theme}`, { waitUntil: 'networkidle' })
  await page.waitForSelector('[data-episode="stop"] [aria-label="Message input"]')

  for (const [episode, [slot, enabled]] of Object.entries(EXPECTED)) {
    const row = page.locator(`[data-episode="${episode}"]`)
    const slotCount = await row.locator(slot).count()
    const optimize = row.getByRole('button', { name: 'Optimize prompt' })
    const present = (await optimize.count()) === 1
    const isEnabled = present && (await optimize.isEnabled())
    const ok = slotCount >= 1 && present && isEnabled === enabled
    console.log(`${theme} ${episode.padEnd(6)}: slot=${slotCount} optimize=${present ? (isEnabled ? 'enabled' : 'disabled') : 'absent'} -> ${ok ? 'ok' : 'FAIL'}`)
    if (!ok && !RECORD_ONLY) failures++
  }
  if (errors.length) { console.error(errors.join('\n')); failures++ }

  const shot = `${OUT}/${NAME}-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path: shot })
  console.log(`  -> ${shot}`)
  await page.close()
}

await browser.close()
await server.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed.`)
  process.exit(1)
}
console.log('\nall rows render as specified.')
