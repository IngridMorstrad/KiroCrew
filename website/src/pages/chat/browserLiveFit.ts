import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react'

import type { FrameSize } from '../../hooks/useBrowserLive.input'

/**
 * Fit the live-browser frame inside the part of the chat the person can see.
 *
 * The frame box takes the frame's own aspect ratio, is as wide as the chat
 * column, and is never taller than the transcript's visible height minus the
 * card's own chrome (header, notices, step strip). When the height cap bites,
 * the box narrows to keep the ratio and centres, so the whole page shows at
 * once with no inner or outer scroll. The canvas inside still draws with
 * `object-fit: contain`, so takeover input keeps mapping from the drawn rect
 * (`drawnRect` in `useBrowserLive.input`) whatever the box ends up being.
 */

/** Before the first frame: the 16:10 the card always used. */
export const DEFAULT_FRAME: FrameSize = { w: 16, h: 10 }
/** The shortest frame worth showing; below this the card scrolls instead. */
export const MIN_FRAME_PX = 160
/** Room left above and below the card inside the transcript. */
export const FIT_MARGIN_PX = 16
/** The CSS fallback when no scrolling ancestor can be measured. */
export const FALLBACK_MAX_HEIGHT = 'calc(100dvh - 280px)'

/**
 * The tallest the frame box may be: the scroller's visible height minus the
 * card's chrome and a margin, never below `MIN_FRAME_PX`. `null` when there is
 * nothing to measure (no scroller, a zero-size host such as the test DOM).
 */
export function frameMaxHeight(scrollerHeight: number, chromeHeight: number, margin = FIT_MARGIN_PX): number | null {
  if (!(scrollerHeight > 0)) return null
  return Math.max(MIN_FRAME_PX, Math.floor(scrollerHeight - Math.max(0, chromeHeight) - margin))
}

/** The frame box's inline style for a frame size and a height cap. */
export function frameBoxStyle(frame: FrameSize | null, maxHeight: number | null): CSSProperties {
  const f = frame && frame.w > 0 && frame.h > 0 ? frame : DEFAULT_FRAME
  const ratio = f.w / f.h
  const cap = maxHeight === null ? FALLBACK_MAX_HEIGHT : `${maxHeight}px`
  return {
    aspectRatio: `${f.w} / ${f.h}`,
    // Width-bound to the column, then narrowed so height = width / ratio <= cap.
    width: '100%',
    maxWidth: `calc(${cap} * ${ratio.toFixed(6)})`,
    maxHeight: cap,
    marginInline: 'auto',
  }
}

function scrollParent(el: HTMLElement | null): HTMLElement | null {
  let n = el?.parentElement ?? null
  while (n) {
    const oy = getComputedStyle(n).overflowY
    if ((oy === 'auto' || oy === 'scroll') && n.clientHeight > 0) return n
    n = n.parentElement
  }
  return null
}

/**
 * The frame box's height cap, kept current as the transcript or the card
 * resizes (a phone rotating, the composer growing, a notice appearing).
 * `cardRef` is the card's root; `boxRef` the frame box inside it.
 */
export function useFrameMaxHeight(cardRef: RefObject<HTMLElement>, boxRef: RefObject<HTMLElement>): number | null {
  const [max, setMax] = useState<number | null>(null)
  useLayoutEffect(() => {
    const card = cardRef.current
    const box = boxRef.current
    if (!card || !box) return
    const scroller = scrollParent(card)
    if (!scroller) return
    const measure = () => {
      const chrome = card.getBoundingClientRect().height - box.getBoundingClientRect().height
      const next = frameMaxHeight(scroller.clientHeight, chrome)
      setMax(prev => (prev === next ? prev : next))
    }
    measure()
    if (typeof ResizeObserver !== 'function') return
    const ro = new ResizeObserver(measure)
    ro.observe(scroller)
    ro.observe(card)
    return () => ro.disconnect()
  }, [cardRef, boxRef])
  return max
}
