/** The Git workspace's window chrome, hosted on THIS tree's modal shell.
 *
 *  Upstream renders the workspace inside `canvas/modalpin`'s `PinFrame`: a
 *  movable, pinnable, pop-out-able surface. That component is the entry point
 *  to a UI shell this tree does not have — modalpin pulls in popout, pins,
 *  pinSnap, windowlife, icons and mobile, and popout in turn pulls lightbox
 *  and the rest of upstream's surface machinery. Porting the closure meant
 *  adopting upstream's whole window system, which is a much larger decision
 *  than adopting Git.
 *
 *  So the frame is reimplemented here against the `.overlay` / `.settings`
 *  shell every other modal in this tree already uses (see canvas/modals.tsx).
 *  The PROP SHAPE is upstream's exactly, so `GitWorkspace.tsx` keeps calling
 *  `<PinFrame kind title panel close onEsc>` unchanged and stays diffable
 *  against upstream. What is deliberately dropped: pinning, dragging and
 *  popping out to a separate window. `pinnable`, `dialogLabel`, `onPanelClick`
 *  and `overlayClass` are accepted and ignored rather than removed, so a later
 *  port of modalpin is a one-line import swap back.
 */
import type { ReactNode } from 'react'
import { useEsc } from '../canvas/shared'

export function PinFrame({ title, panel, overlayClass, close, children, onEsc,
  backdropClose = true, dialogLabel }: {
  kind: string; title?: ReactNode; panel?: string; overlayClass?: string
  close: () => void; children?: ReactNode; onEsc?: () => void
  backdropClose?: boolean; onPanelClick?: () => void; pinnable?: boolean
  dialogLabel?: string
}) {
  // upstream's onEsc owns the "close the open popover first" step, so it wins
  // over the bare close when the caller supplies one
  useEsc(onEsc ?? close)
  // ⚠ `panel` REPLACES this tree's `.settings` rather than joining it. The
  // panel class is the one the ported stylesheet dresses (`.git-workspace`
  // sets its own width, height and `padding:0!important`), and `.settings`
  // carries a competing width and padding of its own — with both applied the
  // winner would depend on which stylesheet the bundler emitted last.
  //
  // `title` is deliberately NOT drawn. Upstream paints it in PinFrame's
  // window titlebar; here the panel IS the window, and the workspace already
  // renders its own `.git-head` with the repository selector and the close
  // button. Drawing it again would both duplicate the name and eat a row of
  // a flex column whose height the stylesheet fixes. It labels the dialog
  // instead, so the name still reaches assistive technology.
  const label = dialogLabel ?? (typeof title === 'string' ? title : undefined)
  return (
    <div className={'overlay' + (overlayClass ? ' ' + overlayClass : '')}
      onClick={backdropClose ? close : undefined}
      onPointerDown={(e) => e.stopPropagation()}>
      <div className={panel ?? 'settings'} role="dialog" aria-modal="true"
        aria-label={label} onClick={(e) => e.stopPropagation()}>
        {children}
      </div>
    </div>
  )
}

/** Upstream's popout gives each surface the document it is rendered into, so
 *  a popped-out window measures against its OWN document. With no popout in
 *  this tree every surface lives in the main document, and that is the honest
 *  answer rather than a stub: the viewport observer in GitWorkspace.tsx reads
 *  `.defaultView` off this to attach its resize listener. */
export const useSurfaceDocument = (): Document => document
