// Small pages around the landing route: the workbench loading state (shown while the lazily
// loaded workbench chunk arrives), a load-failure boundary, and the not-found page.
import { Component, type ReactNode } from "react";
import { Footer, SkipLink, Wordmark } from "./Landing";
import "./landing.css";

/** Suspense fallback while the workbench bundle loads. Uses the workbench's dark surface. */
export function LabLoading() {
  return (
    <div className="l-boot" role="status">
      <span className="l-mark" aria-hidden="true" />
      <span className="spinner" aria-hidden="true" />
      <span>Opening the lab…</span>
    </div>
  );
}

interface BoundaryState {
  failed: boolean;
}

/** If the workbench chunk cannot be fetched (offline, stale deploy), say so and offer a reload. */
export class LabLoadBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { failed: false };

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="l-boot l-boot--error" id="main">
        <h1 className="l-boot__title">The lab could not be loaded</h1>
        <p>Check your connection, then reload the page.</p>
        <div className="l-actions">
          <button type="button" className="btn btn--primary l-btn" onClick={() => window.location.reload()}>
            Reload
          </button>
          <a className="btn l-btn" href="/">
            Go to the home page
          </a>
        </div>
      </main>
    );
  }
}

export function NotFound({ path }: { path: string }) {
  return (
    <div className="landing">
      <SkipLink />
      <header className="l-nav">
        <div className="l-container l-nav__inner">
          <a className="l-wordmark" href="/" aria-label="Resonance home">
            <Wordmark />
          </a>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="l-section l-notfound">
        <div className="l-container">
          <p className="eyebrow">404</p>
          <h1 className="l-section__title">Page not found</h1>
          <p className="l-body">
            There is no page at <code className="mono">{path}</code>.
          </p>
          <div className="l-actions">
            <a className="btn btn--primary l-btn" href="/lab">
              Open the lab
            </a>
            <a className="btn l-btn" href="/">
              Go to the home page
            </a>
          </div>
        </div>
      </main>
      <Footer />
    </div>
  );
}
