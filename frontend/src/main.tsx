// ⚠ crashReporter MUST be the first import: its top-level code installs the
// window error/unhandledrejection listeners, and they have to be live before
// anything else — React included — gets a chance to throw. See
// crashReporter.ts for why. Ported from upstream 2026-09-08.
import { flushPendingReports } from './crashReporter'
import React from 'react'
import ReactDOM from 'react-dom/client'
import './mobile'   // D-125: stamp html.mobile before first paint
import App from './App'
import CrashBoundary, { CrashTestRenderTrigger } from './CrashBoundary'
import { installFreezeLog } from './freezelog'
import FreezeLogPage, { isFreezeLogPath } from './FreezeLogPage'
import './styles.css'

// a report the last session could not deliver (the tab died mid-POST) goes out
// now, on the next successful load
flushPendingReports()

// /debug/freezes shows the recorded freeze log INSTEAD of the app; every other
// path runs the app with the recorder installed. The page is deliberately not
// a route inside App: it has to be readable when the app itself is the thing
// that froze, and it reads localStorage written by OTHER tabs — including a
// tab that has since been discarded. See freezelog.ts.
const freezePage = isFreezeLogPath()
if (!freezePage) installFreezeLog()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <CrashBoundary>
      <CrashTestRenderTrigger />
      {freezePage ? <FreezeLogPage /> : <App />}
    </CrashBoundary>
  </React.StrictMode>,
)
