// Chooses the page from the URL path: "/" is the landing page, "/lab" the workbench. The
// workbench is lazy-loaded so the landing page never mounts it (mounting creates a session and
// runs the opening experiment) and its bundle stays out of the landing download. Navigation
// between the two is ordinary full-page links; the server answers any non-file path with
// index.html, and hash modes (/lab#learn, /lab#investigate) are read by the workbench itself.
import { lazy, Suspense, useEffect } from "react";
import { LabLoadBoundary, LabLoading, NotFound } from "./features/landing/Entry";
import { Landing } from "./features/landing/Landing";

const App = lazy(() => import("./App").then((m) => ({ default: m.App })));

export type Route = "landing" | "lab" | "not-found";

export const TITLES: Record<Route, string> = {
  landing: "Resonance — hear what a circuit does",
  lab: "Resonance — circuit lab",
  "not-found": "Page not found — Resonance",
};

export function routeFor(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "");
  if (path === "" || path === "/index.html") return "landing";
  if (path === "/lab") return "lab";
  return "not-found";
}

export function Root({ pathname = window.location.pathname }: { pathname?: string }) {
  const route = routeFor(pathname);

  useEffect(() => {
    document.title = TITLES[route];
  }, [route]);

  if (route === "lab") {
    return (
      <LabLoadBoundary>
        <Suspense fallback={<LabLoading />}>
          <App />
        </Suspense>
      </LabLoadBoundary>
    );
  }
  if (route === "not-found") return <NotFound path={pathname} />;
  return <Landing />;
}
