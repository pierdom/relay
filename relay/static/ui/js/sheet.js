/* Drag-to-dismiss for the mobile bottom sheets.
 *
 * Every modal becomes a bottom sheet at <=768px, so every one of them gets the
 * same grab handle and the same gesture — before this, only the post modal had
 * either, and the other three could be closed on a phone solely by hitting a
 * 22px "x".
 *
 * The sheet tracks the thumb 1:1 while it is being dragged. That is the whole
 * point of the affordance: a handle that does not move when you pull it reads
 * as decoration. The post modal's original swipe *looked* like it did this but
 * did not — the inline `transform` written on every touchmove was discarded and
 * only the release fired.
 *
 * The cause was in the CSS, and it took **two** things together: the entry
 * animation ran with `animation-fill-mode: both`, and its keyframes named an
 * explicit `to { transform: none }`. A forwards fill outranks inline styles, so
 * the sheet was pinned at the origin all the way through the gesture. Dropping
 * either one frees it (verified by mutation: reverting just one keeps the tests
 * green, reverting both turns them red), so the fix does both — `backwards`
 * instead of `both`, and no `to` keyframe, since an omitted one resolves against
 * the element's own style, which is where the drag writes.
 */

const MOBILE = window.matchMedia('(max-width: 768px)');
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');

// Past this much travel, or moving this fast at release, the sheet goes away.
// The velocity path exists so a short flick dismisses without dragging the
// sheet halfway down the screen first.
const DISMISS_PX = 96;
const DISMISS_VELOCITY = 0.45;   // px/ms

const SPRING_BACK = 'transform 0.3s cubic-bezier(0.32, 0.72, 0, 1)';
const SLIDE_OUT = 'transform 0.22s cubic-bezier(0.4, 0, 1, 1)';

/**
 * Wire drag-to-dismiss onto one sheet.
 *
 * @param {HTMLElement} inner    the sheet panel that moves
 * @param {HTMLElement} handle   where the drag may start (the header)
 * @param {HTMLElement} backdrop faded in proportion to the drag
 * @param {() => void}  onDismiss closes the modal; called after the slide-out
 * @param {() => boolean} [canDismiss] veto, run before the slide-out starts —
 *        the edit sheet asks about unsaved changes here, and a "no" springs back
 */
export function attachSheetDismiss({ inner, handle, backdrop, onDismiss, canDismiss }) {
  let startY = 0;
  let delta = 0;
  let lastY = 0;
  let lastT = 0;
  let velocity = 0;
  let dragging = false;

  const reset = () => {
    inner.style.transition = '';
    inner.style.transform = '';
    inner.classList.remove('sheet-dragging', 'sheet-armed');
    if (backdrop) { backdrop.style.transition = ''; backdrop.style.opacity = ''; }
  };

  handle.addEventListener('touchstart', (e) => {
    // Desktop keeps the centred dialog and its click-outside/Escape paths; the
    // gesture would have nothing to grab there.
    if (!MOBILE.matches || e.touches.length !== 1) return;
    dragging = true;
    startY = lastY = e.touches[0].clientY;
    lastT = e.timeStamp;
    delta = velocity = 0;
    inner.style.transition = 'none';
    if (backdrop) backdrop.style.transition = 'none';
    inner.classList.add('sheet-dragging');
  }, { passive: true });

  handle.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    const y = e.touches[0].clientY;
    // Floor the interval at roughly one frame: browsers can deliver a burst of
    // coalesced touchmoves microseconds apart, and dividing by that turns a
    // gentle nudge into a flick fast enough to dismiss.
    const dt = Math.max(e.timeStamp - lastT, 8);
    velocity = (y - lastY) / dt;
    lastY = y;
    lastT = e.timeStamp;

    // Upward drags resist rather than lift the sheet off the bottom edge —
    // there is nothing above it to reveal.
    delta = y - startY;
    const travel = delta < 0 ? delta / 6 : delta;
    inner.style.transform = `translateY(${travel}px)`;
    inner.classList.toggle('sheet-armed', delta > DISMISS_PX);

    // The backdrop lifts with the sheet, so the page behind reappears as you
    // pull. It never goes fully transparent mid-drag: the sheet is still open,
    // and a bare feed under a half-dragged sheet reads as a glitch.
    if (backdrop) {
      const progress = Math.min(Math.max(delta, 0) / (inner.offsetHeight || 1), 1);
      backdrop.style.opacity = String(1 - progress * 0.7);
    }
  }, { passive: true });

  const release = () => {
    if (!dragging) return;
    dragging = false;
    inner.classList.remove('sheet-dragging', 'sheet-armed');

    const wants = delta > DISMISS_PX || (delta > 24 && velocity > DISMISS_VELOCITY);
    if (!wants || (canDismiss && !canDismiss())) {
      if (REDUCED.matches) { reset(); return; }
      inner.style.transition = SPRING_BACK;
      inner.style.transform = '';
      if (backdrop) { backdrop.style.transition = 'opacity 0.3s ease'; backdrop.style.opacity = ''; }
      inner.addEventListener('transitionend', reset, { once: true });
      return;
    }

    if (REDUCED.matches) { reset(); onDismiss(); return; }

    // Carry the gesture out to the bottom edge instead of blinking away from
    // wherever the thumb happened to stop.
    inner.style.transition = SLIDE_OUT;
    inner.style.transform = 'translateY(100%)';
    if (backdrop) { backdrop.style.transition = 'opacity 0.22s ease'; backdrop.style.opacity = '0'; }
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      reset();
      onDismiss();
    };
    inner.addEventListener('transitionend', finish, { once: true });
    // transitionend does not fire if the sheet is already at translateY(100%)
    // (dragged the full height), which would strand the modal open.
    setTimeout(finish, 320);
  };

  handle.addEventListener('touchend', release);
  handle.addEventListener('touchcancel', release);
}
