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
import './styles.css'

// a report the last session could not deliver (the tab died mid-POST) goes out
// now, on the next successful load
flushPendingReports()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <CrashBoundary>
      <CrashTestRenderTrigger />
      <App />
    </CrashBoundary>
  </React.StrictMode>,
)
