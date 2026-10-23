## Browser Module

The agent browses through one driver, and the person watches it in the chat.
[Browser Harness](https://github.com/browser-use/browser-harness) (MIT) drives a
Chromium-family browser over CDP; the gateway pins it, runs its daemon, holds the
only CDP connection to the browser through a loopback relay, and streams the page
into the transcript as a live card the person can take over with one click. The
`browser` MCP tool keeps its name, its governance check and its URL gate; only
what sits behind it changed.

This document is the spec for the whole stack. The design and its measurements
are in
[rfc-browser-live-harness.md](../../request-for-change/rfc-browser-live-harness.md);
that RFC supersedes
[rfc-playwright-cli-migration.md](../../request-for-change/rfc-playwright-cli-migration.md).
The whole stack landed in one change: the driver, the stream, the tool, the card,
Settings and the removal of what they replaced, so no release carries a half of
this.

### Architecture

```
browser ◀──one CDP websocket──▶ gateway relay ◀── Harness daemon (as if it were the browser)
                                     │
                                     ├── screencast sessions ──▶ browser_frame ──▶ live card
                                     └── input sessions ◀── /api/browser/input ◀── takeover

agent turn ──MCP browser(op, args)──▶ ops ──▶ supervisor.call(<helper>) ──▶ daemon IPC
```

| Part | Where |
|---|---|
| Driver install, environment fence, browser discovery, relay, supervisor, helper worker, status and install routes | the `browser_harness` package: `env.py`, `install.py`, `chromium.py`, `discover.py`, `relay.py`, `supervisor.py`, `helper_worker.py`; `dashboard/handlers/browser_harness_status.py` |
| Screencast, adaptive ladder, takeover input, the live hub, the card's routes and the `browser_live` event | `screencast.py`, `takeover.py`, `live_hub.py` in that package; `dashboard/handlers/browser_live.py` |
| The `browser` MCP tool's ops, the snapshot walker and the op route that also drives the card | `mcp_tools/browser.py`, `ops.py`, `snapshot_walker.js` in that package; `dashboard/handlers/browser_ops.py` |
| The live card in the transcript | `BrowserLiveCard*.tsx` under the chat page, the `useBrowserLive*` hooks, and the binary-frame branch of `useWebSocket.ts` |
| Settings → Browser and the Preview tab | `website/src/pages/settings/Browser*.tsx`, `config/sections_browser.py`, `website/src/components/WebPreviewPanel*.tsx` |
| Screenshot directory, retired keys, doctor row, Preview tab's native view | `browser_snapshots.py`, `config/resolution.py`, `doctor_checks/install.py`, `website/electron/browser-view.js` |

One supervisor per gateway owns the daemon and the relay. One relay connection
per browser. One live card per agent turn. The side panel keeps one surface,
the **Preview** tab, for the person's own local dev server; the agent never
drives it.

### The driver

The gateway installs `browser-harness==0.1.13` into its own virtual environment
at `<data-home>/browser-harness/venv` (`python -m venv --clear` on that
directory alone, so the Chromium beside it survives a re-install; then
`pip install --require-hashes -r browser_harness/requirements.lock`: 12 pinned
requirements, 176 sha256 hashes, compiled for Python 3.11+ on every platform).
Kiro's own Chromium (Chrome for Testing `154.0.8037.0`, the build Playwright's
`chromium-1244` ships, fetched from Google's bucket and then Playwright's CDN,
which alone hosts linux-arm64) is downloaded into the same prefix for hosts with
no usable Chrome (`chromium.py`). The prefix is a `READONLY` crew-home leaf, so
a sandboxed process can neither replace the interpreter nor the browser the
gateway executes; the install job (`POST /api/browser/install`, one at a time,
`409` otherwise, `{"components": ["harness"|"chromium"]}` to redo one part) runs
in the gateway, outside the sandbox.

The daemon starts with a scrubbed environment built by one function and pinned
by one test:

| Variable | Value | Why |
|---|---|---|
| `BH_TELEMETRY`, `BROWSER_HARNESS_TELEMETRY`, `ANONYMIZED_TELEMETRY` | `false` | the three switches the package reads |
| `BH_POSTHOG_HOST` | `http://127.0.0.1:9` | a second fence if a switch is missed |
| `BH_TAB_MARKER` | `0` | no badge painted on the page |
| `BH_UPDATE_CHECK` | `0` | the CLI's PyPI probe never runs |
| `BU_CDP_URL` | the relay's URL, `http://127.0.0.1:<port>/<secret>` | connects to the relay, not the browser; the per-start secret is the only credential that opens it |
| `BU_NAME` | the gateway's daemon name | a named daemon creates its OWN tab instead of attaching the first page it finds (the person's) |
| `BH_HOME`, `BH_CONFIG_DIR`, `BH_RUNTIME_DIR`, `BH_TMP_DIR`, `BH_AGENT_WORKSPACE` | under `<data-home>/browser-harness-ipc` | the socket, pid file, log and scratch never leave the hidden leaf |
| `BROWSER_USE_API_KEY`, `BU_BROWSER_ID`, `BU_CDP_WS`, `BU_AUTOSPAWN`, the cloud API/OAuth variables, inherited `BH_*` roots | removed | the only ways in to the cloud, or to a path outside the leaf |

The removed keys stay removed even when the gateway's own environment carries
them. The gateway never runs Harness's CLI, its `run.py`, its MCP server or its
update check, and never calls `browser_js`, `browser_cdp` or `http_get`; it
calls the daemon through Harness's own IPC (a Unix socket on POSIX, loopback TCP
with a token on Windows), whose path or token lives in a `HIDDEN` crew-home
leaf. A data home so deep that the socket path would pass the AF_UNIX limit
(100 bytes) is REFUSED with a `BrowserUnavailable` naming the path: the only
shorter place is the system temp dir, which no sandbox mask covers, and the
daemon resolves symlinks before binding, so an alias cannot help (the guard
measures the RESOLVED home for the same reason). The daemon runs with
`python -I`, like the helper worker, so the gateway's working directory can never
shadow the pinned package; the INSTALLER's two spawns (`python -I -m venv`,
`<venv>/python -I -m pip`) run the same way, with the `PYTHON*` and `PIP_*`
families scrubbed from their environment and the prefix as their working
directory, because a pod gateway's cwd is the agent's workspace and a `pip/`
package written there would otherwise run as the owner the moment they press
Install. `NO_PROXY`/`no_proxy` always carry `127.0.0.1,localhost`: neither
`urllib` nor `websockets` bypasses a proxy for loopback on its own, and a shell
with `HTTPS_PROXY` set would send the relay URL (secret and all) to the proxy
host. A version bump re-reads the package's telemetry and auth modules before it
lands, because 0.1.13's telemetry is on by default and its run event carries the
agent's whole script unscrubbed.

**The helper worker.** The gateway's Python has no `browser_harness` package, so
each helper call runs in a worker the venv's interpreter executes
(`<prefix>/bin/python -I browser_harness/helper_worker.py`, JSON lines on its
pipes, one call at a time behind a lock, `CALL_TIMEOUT_SECS = 90`). The pipe is
read with an 8 MiB line limit (`WORKER_LINE_LIMIT`; asyncio's 64 KiB default is
smaller than an ordinary snapshot) and the worker refuses to write a reply over
4 MiB (`REPLY_CAP_BYTES`, an error reply instead); a line that still overruns is
a fatal protocol failure that discards the worker, since the rest of the line is
still in the pipe. The walker caps what it returns (1,500 nodes, 2,000 characters
per console entry). The worker's allowlist is the whole surface, and the
supervisor checks the same table before it sends (`supervisor.HELPERS`, pinned
equal by a test):

| helper | kwargs | Harness function |
|---|---|---|
| `goto` | `url` | `goto_url` (returns `Page.navigate`'s reply, `errorText` on failure) |
| `click` | `x, y, button="left", clicks=1` | `click_at_xy` |
| `mouse_move` | `x, y` | the worker's own `Input.dispatchMouseEvent` (Harness has no hover) |
| `drag` | `x, y, to_x, to_y` | the worker's own press / stepped moves / release |
| `type_text` | `text` | `type_text` (`Input.insertText`) |
| `press_key` | `key, modifiers=0` (1 Alt, 2 Ctrl, 4 Meta, 8 Shift) | `press_key` |
| `scroll` | `x, y, dy=-300, dx=0` | `scroll` |
| `screenshot` | `path, full=False, max_dim=None` | `capture_screenshot` |
| `page_info` | -- | `page_info` (`{url,title,w,h,sx,sy,pw,ph}` or `{dialog}`) |
| `wait_for_load` | `timeout=15.0` | `wait_for_load` |
| `new_tab`, `switch_tab`, `list_tabs`, `close_tab`, `current_tab` | `url` / `target, activate` / `include_chrome` / `target` / -- | the tab helpers |
| `js` | `expression, target_id=None` | `js`, ONLY for `snapshot_walker.js` |

`run.py`, `browser_js`, `browser_cdp`, `http_get`, `upload_file`, `fill_input` and
the agent-helper loader are not reachable through it. A helper's failure comes back
as `HelperError` with Harness's own one-line message; `HarnessNotInstalled` and
`BrowserUnavailable` come out of `ensure()`.

**The supervisor.** `HarnessSupervisor` (one per gateway, `get_supervisor(home)`)
brings browser, relay, daemon and worker up in `ensure()` (serialised: a second
caller waits for the first), reports `status()` (`installed`, `version`,
`wanted_version`, `browser {kind, headless}`, `connection`
`connected|waiting_allow|disconnected|error`, `detail`, `agent_tabs`, `chromium`),
and stops all of it in `stop()`. `waiting_allow` has no deadline because the
connection is what keeps Chrome's Allow sheet on screen. The gateway stops the
supervisor on shutdown and before an in-place restart's exec
(`server_runtime/browser_lifecycle.py`, registered with `pre_exec_stops`), and
reclaims a stray daemon from a previous life at startup.

### The browser

| Host | Browser | First run |
|---|---|---|
| macOS, Linux desktop, Windows with Chrome | the user's Chrome | enable `chrome://inspect/#remote-debugging` once, then Allow |
| Windows without Chrome | Edge | same |
| Server, no display, remote gateway | Kiro's Chromium, `--headless=new --remote-debugging-pipe --user-data-dir=<data-home>/browser-profile` (`--remote-debugging-port=0` on Windows only) | none |
| Chrome's `RemoteDebuggingAllowed` policy is off | Kiro's Chromium | none |

A snap-packaged Chromium is skipped: snap confinement breaks the debugging port.
On macOS and Linux Kiro's Chromium speaks CDP over `--remote-debugging-pipe`
(fds 3 and 4, moved into place by `/bin/sh` before `exec`), so it opens no port
at all; Python cannot hand a Windows child fds 3 and 4, so there it keeps an
ephemeral port, reachable by any local process (security table).
`--no-sandbox` is added only when the gateway runs as root. Kiro's Chromium is
always launched with `--use-mock-keychain --password-store=basic`: its network
service otherwise fetches the cookie-encryption key from the OS keystore, and on a
fresh Chrome for Testing that is a macOS Keychain (or Linux keyring) prompt a headless
browser can never answer, behind which every `http(s)` navigation -- loopback included
-- hangs forever while `data:` pages still load. The key stays in memory; the profile
it protects is Kiro's own hidden leaf. Kiro's profile is a
`HIDDEN` crew-home leaf: cookies from a takeover login stay there and the agent
cannot read the file. `browser.choice` (`auto|user_chrome|kiro_chromium|headless`)
overrides the order by hand.

Chrome 144+ asks "Allow remote debugging?" once per connection. The supervisor's
`waiting_allow` state never times out that prompt; the live card shows the one
step ("click Allow in Chrome") until it is answered.

### The relay

- **One upstream connection, one secret.** The relay listens on `127.0.0.1:0`
  and every path it serves carries a per-start secret (`/<secret>/json/version`,
  `/devtools/browser/<secret>`); the bare Chrome-shaped paths and a wrong secret
  are 404 before anything is forwarded. The sandbox does not fence loopback, so
  a process the agent runs CAN find the port by probing; the secret is what it
  does not have -- it is in the daemon's environment and nowhere else. The
  upstream is a websocket (the user's Chrome; Kiro's Chromium on Windows) or the
  debugging pipe (Kiro's Chromium elsewhere).
- **Disjoint ids.** Every daemon request is renumbered and mapped back on reply,
  so several clients never collide; the relay's own requests use ids from
  1,000,000,000 up.
- **Private sessions.** The relay attaches its own flat sessions for screencast
  and input. Replies and events on those sessions stay in the relay and never
  reach the daemon's shared event buffer.
- **Agent tabs stay live.** Every target the daemon creates gets focus emulation,
  so a tab behind another tab keeps painting and Harness's input calls on it do
  not hang. The user's own tabs are never attached.
- **Tab ledger.** Each target the agent creates is recorded at the create reply,
  whether or not the page then loads; `tabs` lists only those and session end
  closes only those.
- Late `screencastFrameAck` errors after a stop are swallowed; close is clean.

### The stream

`Page.startScreencast` on a relay-owned session: JPEG, quality 60, 1280 wide.
Chrome emits a frame only when the page changes, so a still page costs nothing.
The relay acks a frame to Chrome only after the card has acked that frame's
sequence number (bounded at 2 s, so a card that stopped acking slows the stream
instead of freezing it, and the bounded wait counts as a late frame), on a fixed
time grid (`due = max(last_due + period, now - period)`), so transit time counts
toward the interval. Measured in the RFC: 57.6 fps headed and 57.8 fps headless
at cap 60 on macOS (53.5 under ack-after-sleep; headed Chrome tops out near 95
fps on a 120 Hz display, headless Chromium's compositor is clocked at 60, and
lifting the cap reaches 81-84 fps at about 450% CPU, so the product does not).

Once a second the stream picks a rung of the ladder:

| Rung | fps | quality | width |
|---|---|---|---|
| 0 | 60 | 60 | 1280 |
| 1 | 45 | 55 | 1280 |
| 2 | 30 | 50 | 1280 |
| 3 | 30 | 45 | 960 |
| 4 | 20 | 40 | 960 |
| 5 | 15 | 40 | 800 |
| 6 | 10 | 35 | 640 |
| 7 | 5 | 30 | 640 |

It steps down at once when more than 20% of frames arrive later than 1.5
periods or the link cannot carry the rung, as far as the measured rate needs;
it steps up after 2 calm seconds to the best rung the link covers with 30%
headroom. An fps change is free; a quality or size change restarts the
screencast on the same session. An off-screen or collapsed card stops the
screencast (the card reports visibility). Input sent with no frame for 3 s
restarts it. While the person holds the browser the stream is pinned to the
15 fps rung. `browser.live.*` holds the knobs: `enabled`, `quality`
(`auto|high|medium|low`), `max_fps` (15|30|60), `mask_passwords`; `max_fps` and a
fixed `quality` set the ladder's top rung. They are read per stream start from the
live config snapshot, so a Settings change applies to the next turn, never to a
cast under the person's hands. With `mask_passwords` (default on) the page is
asked, at most every 250 ms, for the boxes of visible `input[type=password]`
(a read-only `querySelectorAll` + `getBoundingClientRect` on the relay's own
session, walking open shadow roots to the same depth the snapshot walker does),
scaled from CSS to frame pixels; a failed query keeps the last boxes rather than
unmasking. Every clock read and sleep is injected, so the ladder is
tested on a fake clock.

**Wire.** Owner websockets only. State changes are the JSON event
`browser_live` (`session_key`, `turn_id`, `state`, `url`, `title`, `browser`,
`headless`, `step`, `steps`, `rung`, `fps`). Frames are BINARY messages: a
4-byte big-endian header length, a UTF-8 JSON header
(`{"type":"browser_frame","session_key","stream","seq","w","h","mask":[[x,y,w,h],...]}`),
then the JPEG bytes. `stream` names the screencast the frame came from and `seq`
restarts at 1 with every stream (a new turn, a tab switch, a gateway restart),
so the card scopes its once-per-frame ack to `(stream, seq)` and a new stream is
never held behind an old stream's high-water mark; the ack body names the stream
too, and the hub ignores an ack for a stream that is no longer the session's. `mask` holds the boxes of
focused or visible password fields, asked of the page before the frame is sent,
clipped to the frame (a full-width field must not produce a box the card's
decoder would refuse) and painted over by the card.
`detail` rides on a `state: error` event and is rendered verbatim.

**The hub and the turn.** `live_hub.LiveHub` holds one `LiveSession` per slot
key: turn id, step strip, state (`waiting_allow | live | in_control | done |
login_needed | error`), input lock and stream. The glue is in the op route
(`dashboard/handlers/browser_ops.py`), the one place that knows both the session
and the browser: the FIRST `browser` op of a turn opens the hub's turn with a
fresh `turn_id` (stable until the chat runner closes it from `_end_turn_tail`,
`chat_turn/queue_cycle.py`, when the agent's turn ends), every op is a step, and
after each op the card follows the daemon's current tab -- one cheap
`current_tab` helper call; a new target (first op, `tabs new`, `tabs switch`)
starts or restarts the screencast on it, the same target only refreshes the
address pill. One daemon serves every chat, so every op runs under one
gateway-wide lock and is preceded by a tab bind: the session keeps the agent tab
it was bound to while the browser still has it (`switch_tab` back to it if the
daemon moved), adopts the daemon's current tab when no other session owns it,
and otherwise opens a tab of its own. Chat B therefore never clicks on chat A's
page, `tabs` lists and switches only the caller's own tabs, and a takeover in
chat A fences A's tab while B keeps browsing. With `browser.live.enabled` off
the hub publishes no event at all, so no card is mounted; the ops still run. While the owner's Chrome shows its Allow sheet the op route tells
the hub `waiting_allow`, bounded at 60 s per op (the Allow sheet itself outlasts
the op). The session key the shim resolves (`dashboard:<slot>` or the bare slot)
and the key the card posts (`activeSlot`) meet through
`state_parts.slot_directory._normalize_slot_key`.

### Takeover

Clicking the frame posts `POST /api/browser/input/claim`. Every live-view and
input route (`/api/browser/live/visibility`, `/api/browser/live/ack`,
`/api/browser/input/claim`, `/api/browser/input/release`,
`/api/browser/input/event`, `/api/browser/live/open-in-browser`) accepts the
dashboard owner's cookie or token and refuses the internal secret with 403, so
an agent cannot claim input for itself, and validates that the caller owns the
session. While claimed, mouse, wheel and key events from the card go to
`Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` on the relay's input
session; keystrokes are never logged and never enter the transcript. The event
body is `{type: mouse|wheel|key, action: down|up|move, x, y, button, clicks, dx,
dy, key, code, text, modifiers}` in frame pixels (`action` is what lets the card
drag and hold keys; without it a mouse event with a `button` is a click and one
without is a move; a move carries `clicks: 0` and the held button, so a drag
stays a drag), every field bounded before it is sent (`InputEventError`
`invalid_input_event` otherwise). CDP takes viewport CSS pixels and the JPEG is
narrower than the viewport at every rung but the top (and always during takeover,
which floors at the 15 fps rung), so the stream keeps `frame width /
deviceWidth` from each frame's metadata and positions and wheel deltas are
divided by it at the one place the event becomes CDP. A hand back the gateway
refuses puts the card back in control with the gateway's text in an inline
`ErrorNotice`; the lock is still held, and Hand back is the retry. A take over the
gateway refuses leaves the card live with the same notice, and Take over is the
retry. The claim and the op route share one per-session fence
(`LiveHub.op_fence`): the route holds it from the tab bind to the op's end, the
claim takes the lock first (so no new op starts), cancels the op in flight and is
confirmed only once the route has left the fence, so a takeover never lands
inside an op; `in_control` is published only if the lock is still held after the
claim's own awaits (an Esc in between has already said `live`). A claim on a
finished card (`done`, `error`) is refused (409 `turn_over`). **An agent op is
not lost while the person is in control: it waits for the hand-back and then
runs**, which is how the agent resumes by itself after a sign-in. The wait lives in
the TOOL, not in one HTTP call: the op route answers at once with
`user_in_control` and `hold: true`, and the `browser` tool parks itself as a
`DeferredTool` (the shim runs one worker thread, and the ACP tool-stall watchdog
ends a tool call silent for 600 s) that every `HANDBACK_POLL_S` (5 s) posts
`/api/browser/handback` -- a short long-poll of at most `HANDBACK_WAIT_S` (3 s) on
`LiveHub.wait_for_release`, the awaitable the release sets -- and every 30 s pings
the session keepalive. On `released` the op is posted again and runs. Bounded by
`HOLD_TIMEOUT_S` (10 minutes) in the shim, after which the agent is told
`user_in_control` ("held for 10 minutes; tell them you are waiting and end your
turn"). An op the claim cut short mid-flight answers `interrupted` with `hold`:
the tool waits for the hand-back and then tells the agent to snapshot first; the op
is never re-run blind. A cancelled tool call stops stepping. The turn's thinking
line reads "Waiting for you to hand the browser back". Esc or "Hand back"
releases; the turn's end releases too. Keystrokes typed into the
frame stop at the canvas (`stopPropagation`): the dashboard's window-level
shortcuts (Alt+digit switches session) never fire mid-takeover. A pointer
release outside the drawn frame is clamped to its edge, never dropped, so the
page's button is released where the drag ended. "Open in Chrome"
(`Target.activateTarget`, plus `Page.bringToFront` while a stream session is
attached) focuses the real tab for what the frame cannot carry (a password
manager's autofill popup, a file picker), and still works from the finished
card: the tab outlives the screencast. The six routes refuse the
internal secret (403 `internal_secret_required`), require the dashboard owner
(403 `owner_only`) and accept only a `session_key` naming a slot this dashboard
owns (404 `unknown_session`).

### The live card

One card per turn, attached to the turn's first `browser` call (anchor =
`tool_call_id`, else `ts`), rendered after the turn's last browser row and never
folded. A browser row is recognised by the stamped `tool_name` when there is one
(`browser` or a server-qualified `…/browser`, `…::browser`, `…___browser`) and
otherwise by its pill text, which for the managed-MCP tool reads
`🔧 @kirocrew-core/browser` -- the `@server/tool` form every MCP call arrives in; a new `turn_id` closes the previous record as done. Takeover grows the
same element; it never swaps in another component. Frames are decoded from the
binary message (the TS mirror of `decode_frame`, every field checked), acked ONCE
per `(stream, seq)` even when no live turn shows them (so the relay never
stalls), kept in a ring (1800 frames / 48 MB; only the newest two turns of a
session keep theirs) and painted to a `<canvas>` outside React's render
cycle (`createImageBitmap`, newest frame wins, mask boxes after the image); the
first frame is the one React render, later frames none. Visibility is posted only
on a change of (any card instance showing the CURRENT turn intersecting) AND
(document visible): a finished turn's card still on screen does not keep a later
turn's screencast running while that turn's card is scrolled off. A turn no card
shows (another slot's browsing, a card the virtualised transcript has not
mounted) is reported hidden after 30 of its frames, so its screencast stops
instead of streaming into memory nobody looks at, and resumes when a card mounts. A claim is optimistic and reverts on a
refused POST; so does a hand back (see Takeover); Esc releases and is never
forwarded; nothing on the input path is logged. Replay plays the frames the
stream already sent, kept in memory for the life of the page, original spacing
capped at 500 ms per gap. Strings live at `pages.chat.browserLiveCard.*`.

| State | Shows |
|---|---|
| Waiting for Allow | which browser, the one step, no timeout |
| Live | address, live dot, which browser, Take over, frame, current action, step strip |
| Hover | "Click to take over" |
| In control | grown card, accent border, "You are in control", Open in Chrome, Hand back (Esc) |
| Done | one line: thumbnail, step count, duration, Replay, Last frame, Open in Chrome |
| Login needed | "Take over to sign in" |
| Error | the thumbnail and `detail` through `ErrorNotice` |

The Allow face's "Use Kiro's own browser instead" opens `/settings/browser`.

### The tool

`browser` is one MCP tool in the `kirocrew-core` shim with fourteen ops
(`browser_harness.ops.OPS`), not a tool namespace. The shim runs two gates before
anything leaves the process: `capabilities.browse` governance (a denial refuses
outright; there is no other browser) and the navigate gate (public http(s) only,
plus the exact loopback `host:port` entries of `dashboard.browser_local_origins`,
never the gateway's port; applied to `navigate` and to the URL a new tab opens
with). It then posts `{session_key, op, args}` to `POST /api/browser/op` (strict,
internal-secret only) and renders the gateway's answer. It holds no state.

`browser_harness.ops.run_op(op, args, ctx)` runs in the gateway. It reaches the
browser only through `ctx.helper(name, **kwargs)` -- one allowlisted Harness
helper over the daemon's IPC -- and the `js` helper is only ever given
`snapshot_walker.js` plus one method call. The walker installs `window.__kcWalker`
per document; it mints `eN` refs into `window.__kcRefs` (dropped when the element
leaves the DOM), never reads a password or credential-shaped value, and hooks
`console.*` so `console` returns in-page messages since the last navigate or
snapshot.

| Op | Behaviour |
|---|---|
| `navigate`, `back` | stamp the old document and wait for a new one (or a matching href) with an injected clock |
| `snapshot` | the accessibility walker, run in the page; refs are per snapshot |
| `click` | a ref or `x`,`y`; `clicks: 2` is a double click |
| `drag`, `hover`, `scroll` | ref or coordinates (`mouse_move` / `drag` helpers) |
| `type` | refuses a password field twice: by the ref's type and by whatever holds focus when the text would land |
| `press_key`, `select_option`, `wait_for` (text / textGone / time / load), `console` | as named |
| `screenshot` | writes a PNG under the screenshot directory below and returns its absolute path |
| `tabs` | lists and switches `relay.agent_targets()` only |

Every op calls `live_hub.step`; `run_op`'s own `in_control` check is the backstop
under the route's fence (the route holds the op until the hand-back, see Takeover).
A refusal is a value (`{ok: false, code}`); a transport
failure is the route's `browser_error`; `timeout` is the route's budget (20 s, 75 s
for the slow ops); `browser_busy` is the bounded wait for another chat's op on the
gateway-wide lock (75 s); `not_installed` comes from `browsing_available(home)`.
The budgets live in one module (`browser_harness/budgets.py`) that both the route
and the tool shim import: the shim's HTTP timeout is DERIVED as the route's worst
case (browser start + the 10-minute hold + lock wait + bind + op) plus a margin, so the tool never
reports an op failed while the gateway is still landing it -- a retry of a click
that landed is a double click. The two focus checks (`type` without a ref,
printable `press_key`) and the frame mask resolve the deepest active element
through open shadow roots (`document.activeElement` is the shadow HOST for a
web-component login form). The
`{{BROWSER_BEGIN}}..{{BROWSER_END}}` section of `config/prompt.md` and every
builtin skill that drove a browser (`web-browse`, `web-verify`, `web-preview`,
`browser-auth`, and the rest) speak only of this tool; `browser-auth` is "take
over to sign in".

### Capability model

**An installed driver is availability, not approval.** The tool is always
advertised and checks `capabilities.browse` governance on every call; a
governance denial refuses outright and never falls back to another driver,
because a fallback would let browsing continue and defeat the control
(`mcp_core_parts/governance.py`). The browser-not-installed case is a plain
refusal that points at Settings → Browser.

`navigate` auto-drives only public HTTP(S) targets. Literal loopback, private,
link-local, reserved, alternate- or percent-encoded IP or host name, non-ASCII
host, parser-differential and non-HTTP forms are refused; DNS names are not
resolved, so public-name-to-private-address rebinding remains an accepted
residual. The one exception is `dashboard.browser_local_origins`, an
operator-written list of exact loopback origins (`localhost`, `127.0.0.1` or
`[::1]` plus a required port, e.g. `localhost:5173`). The config coercer drops
every other shape; the tool admits a `navigate` whose own `host:port` equals an
entry byte for byte, after the same refusals and with userinfo refused outright;
the gateway's own API port is refused even when listed. The list is read fresh
per call and fails closed to empty. The agent cannot extend it: `config.json` is
read-only inside the sandbox and `PUT /api/dashboard/config` does not accept the
key. **Accepted residual:** once a listed page is open, `click`/`back` follow
whatever that page links to, since only `navigate` is URL-gated.

Passwords never pass the model: `type` refuses a password field, the frame masks
one, and logins happen during takeover.

### Screenshot directory

`browser_snapshots.py` names the one directory the `screenshot` op writes to:
`<data-home>/browser-snapshots` (`snapshot_dir()`), derived from `config_dir()`
so an isolated `KIROCREW_HOME` stays isolated. It is never inside a working
directory by construction, which is why `messaging/outbound_files.py` admits it
as the second approved root for an inline image in a channel reply: a
screenshot the agent just took can be sent, and admitting the directory grants
nothing the `file_send` tool does not already deliver from it. The read is
pinned to the directory's canonical spelling with the leaf kept literal, so a
link planted at that name resolves the file elsewhere and fails the pin.

### Settings → Browser

First run is a three-step list (engine installed; connect your Chrome via
`chrome://inspect/#remote-debugging` and Allow, or "Skip, use the fallback
browser"; try it) until done or dismissed (`browser.first_run_dismissed`). Then
the page is a status card: the browser in use, the connection state (yellow on
`waiting_allow`), the fallback order, Test and Disconnect; an Engine section
(Harness version, Kiro's Chromium version, Redownload -- which posts `{components:
["chromium"], refresh: true}` so a Chromium already on disk is REPLACED, the
repair for a damaged copy; the ordinary install keeps a whole one); a locked
Privacy row ("No usage data, no cloud browser"); the Live view section; the Local
preview origins editor; and an Advanced picker for `browser.choice`. Every
failure is an `ErrorNotice` with the Ask-agent hand-off: a rejected request, a
Test that answers `ok: false` (in the status card and in the first-run step), and
an install JOB that failed after its 202 (`install_job.error_detail`, in the Engine
section and in the first-run step). Data comes from
`GET /api/browser/status` (polled every 2 s while a job runs or Chrome waits for
Allow, else every 15 s; `supervisor.status()` plus `installing`, `install_job
{status: running|succeeded|failed|interrupted, stage, error_detail}`,
`chromium.downloaded` and `platform`) and `POST /api/browser/install|test|disconnect`,
all owner-only and refusing the internal secret; writes go through
`PATCH /api/config/kirocrew` one path at a time. Every control carries its
`configKey`; every string is in the i18n catalog (`pages.settings.browserPanel.*`).

The config section is `browser`: `choice`, `live.enabled`, `live.quality`,
`live.max_fps`, `live.mask_passwords`, `first_run_dismissed`.
`dashboard.browser_local_origins` stays where it is.

#### Retired keys

`dashboard.use_builtin_browser` and `dashboard.browser_view_port` configured the
agent browser this stack replaced. They are no longer fields. A `config.json`
that still carries them loads, logs ONE warning naming them
(`config/resolution.py`, `warn_retired_section_keys`), and drops them on the
next save (`_SECTION_KEYS_DELIBERATELY_DROPPED`). `PUT /api/dashboard/config`
no longer accepts `use_builtin_browser`, and the change-card catalog no longer
offers `browser.use-the-built-in-browser`.

### The Preview tab

The side panel's Browser tab is the **Preview** tab: the person's own local dev
server, for front-end work that wants page annotation. It keeps the native
embedded Chromium view (`website/electron/browser-view.js`), the element
annotation overlay (`website/electron/browser-annotate.js`, splicing the shared
page helpers in `website/electron/browser-page-helpers.js`) and
`WebPreviewPanel`. It opens only the origins in
`dashboard.browser_local_origins`; an outside URL answers "Open this in your
Chrome, or ask Kiro". The list is judged on the URL the user chose; the
cookie-isolation host swap (`localhost` framed as `127.0.0.1`, so the dashboard's
host-scoped cookie never reaches the dev server) is applied only to the URL the
frame, the liveness probe and the native view load, never to what is shown,
persisted or checked. The list is LIVE: a change to `dashboard.browser_local_origins`
or any `browser.*` key that reaches the gateway from outside this dashboard (the
CLI, another window, `config.json` in an editor) is noticed by the live config
watcher, whose `browser.settings` applier pushes a `refresh` frame; the frame
invalidates the dashboard's config query, so an open Preview tab and Settings ->
Browser re-read without a reload. The agent does not drive it: the Electron main process
exposes `open`, `navigate`, `setBounds`, `setOverlayActive`, `setInactive`,
`close`, `getState` and `annotate` on the `browserAPI` preload bridge and
nothing else (`website/electron/preload.js`, `website/electron/ipc-registrar.js`,
`website/electron/runtime/window/browser-panels.js`).

The view renders arbitrary untrusted web content, so it is the most locked-down
`WebContentsView` in the app: a persistent partition isolated from the
dashboard's cookie jar, `contextIsolation`, no `nodeIntegration`, `sandbox`,
no `webviewTag`, and the shared right-click menu only.

#### Native view focus

In the desktop app keyboard focus belongs to exactly one child view of the
window, and hiding the focused view does not move it. So when the native view
leaves the screen while it holds focus (an overlay, an inactive tab, a collapsed
panel, close), the manager in `website/electron/browser-view.js` hands focus
back to the dashboard view (`focusHost`), and on window focus `reclaimFocus`
does the same for a hidden view that still holds it. Without this every
dashboard text input would look alive and receive no keystrokes.

### Doctor

`kirocrew doctor` prints one informational row (`_doctor_browser_leftover` in
`doctor_checks/install.py`) when the prefix an earlier version's browser CLI
installer wrote (`_LEFTOVER_BROWSER_CLI_LEAF` under the data home) is still on
disk: it names the directory and says it can be deleted. Doctor never deletes or
modifies it, and the row is silent when the directory is absent.

### Security

| Risk | Control |
|---|---|
| The relay can reach the user's whole Chrome | the agent gets only the listed ops, on tabs it created; the relay never attaches the user's tabs. **Accepted residual:** CDP is browser-wide, so a bug in the tab filter would let the agent act on a tab the user opened; tests pin the filter and the card always shows which tab is live |
| Harness sends data out | five environment fences, a pinned version, a test on the fence; no CLI, MCP server or update check runs |
| Agent claims input | `/api/browser/input/*` and `/api/browser/live/*` refuse the internal secret |
| Agent types a password | `type` and printable `press_key` refuse password fields, resolved through open shadow roots; the frame masks them (boxes clipped to the frame, shadow roots walked) |
| Frames show logged-in pages | owner websockets only; never written to disk except as a requested `screenshot` |
| Another local process uses the relay | loopback, ephemeral port, AND a per-start secret on every path (404 without it); the URL with the secret is given only to the daemon. The sandbox does not fence loopback, so the secret, not the port, is the control |
| Another local process reaches the browser's own debugging endpoint | Kiro's Chromium on macOS/Linux has none: it speaks CDP over `--remote-debugging-pipe`. **Accepted residual, Windows:** Kiro's Chromium keeps `--remote-debugging-port=0` (Python cannot hand a Windows child fds 3 and 4), so any local process can drive it. **Accepted residual, the user's own Chrome/Edge:** its debugging port is the user's, enabled by them; Chrome 144+ raises a fresh "Allow remote debugging?" sheet for every new connection, so a second client needs a second click the person would see |
| The daemon's socket leaves the hidden leaf | a data home too deep for the AF_UNIX path limit is refused, never relocated to system temp |
| Another chat drives the tab a person has taken over | every op binds to the calling session's own tab under one gateway-wide lock; a takeover fences that session's tab, and a tab another session owns is never streamed to this session's card |
| A takeover lands inside an agent op | the claim and the op route share a per-session fence: the claim cancels the op in flight and is confirmed only once the route has left it |
| An agent-written `pip/` or `venv/` package in the gateway's cwd runs on Install | both installer spawns run `python -I` with `PYTHON*`/`PIP_*` scrubbed and the prefix as cwd |
| The relay URL (and its secret) goes to a corporate proxy | `NO_PROXY` always carries the loopback names |
| Agent spells a password one key at a time | `press_key` with a printable key runs the same focused-password-field refusal as `type`; named keys and Ctrl/Meta chords stay allowed so the agent can leave the field |
| Agent reads Kiro's profile cookies or the daemon's IPC credential | `HIDDEN` leaves in every sandbox mode |
| Local addresses | `navigate` keeps the public-only gate plus `dashboard.browser_local_origins`, applied TWICE by one shared predicate (`browser_harness/url_gate.py`): in the shim to the URL asked for, and in the gateway to the URL the navigation (or `back`) committed on, so a public page that redirects to loopback, private or link-local is refused (`redirect_refused`) and the tab moved to `about:blank`. **Accepted residual:** DNS rebinding (a public name resolving to a private address) is not seen by either check; names are not resolved |
| Governance | `capabilities.browse` is checked on every call and a denial never falls back |
| Page content | treated as untrusted input; a URL, instruction or form target read off a page never decides the next navigation |
| Preview tab | native view, deny-all partition, no agent channel, local origins only |

### What this stack replaced

The browser CLI shell path (Playwright's CLI, installed per host by `npm`), the
gateway's `show` live-view supervisor and its `/browser-view` same-origin relay,
the Browser panel's address-bar launcher (`POST /api/browser/open`), the
native-panel command bus (`/api/browser/command`, `/api/browser/command-drain`,
`/api/browser/command-result`) and the Electron control plane behind it, the
per-agent browser-session environment and the stranded-daemon orphan class that
reaped it, the `dashboard.use_builtin_browser` and `dashboard.browser_view_port`
keys, the `browser_event` websocket event nothing emitted, and the daily
banner workflow that checked the CLI's install script are gone. No migration and
no flag: one change removed what it replaced. An installed CLI prefix is left on
disk and reported by doctor; its sandbox leaves went with it (nothing the gateway
executes lives there), while the retired CLI's session-material leaves stay
masked because a leftover file still holds the owner's sessions.

### Related

- [web-browse](../../../src/kiro_crew/builtin_skills/web-browse/SKILL.md) for
  opening a page so the user can see it.
- [web-verify](../../../src/kiro_crew/builtin_skills/web-verify/SKILL.md) for
  screenshotting a front-end change as evidence.
- [web-preview](../../../src/kiro_crew/builtin_skills/web-preview/SKILL.md) for
  the Preview tab.
- [computer-use](computer-use.md) for the screencast pattern the live view
  follows (`computer_use_frame` to owner websockets).
- [config](config.md) for how a retired key is dropped on save.
- [mcp](../../architecture/mcp.md) for MCP registration, transport, and trust
  boundaries.
