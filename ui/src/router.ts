import { useCallback, useEffect, useState } from "react";

/** A deliberately tiny hash router: the engine serves one static page, so there is nothing to
 * configure server-side, and a reload or a bookmark lands on the same view. */
export type Route =
  | { view: "wall"; camera: string | null }
  | { view: "incidents"; id: string | null }
  | { view: "cameras"; id: string | null }
  | { view: "zones"; camera: string | null }
  | { view: "system" };

/** A malformed escape in a hand-edited URL must not take the console down. */
function decode(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

export function parse(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decode);
  switch (parts[0]) {
    case "incidents":
      return { view: "incidents", id: parts[1] ?? null };
    case "cameras":
      return { view: "cameras", id: parts[1] ?? null };
    case "zones":
      return { view: "zones", camera: parts[1] ?? null };
    case "system":
      return { view: "system" };
    default:
      return { view: "wall", camera: parts[0] === "wall" ? (parts[1] ?? null) : null };
  }
}

export function href(route: Route): string {
  switch (route.view) {
    case "wall":
      return route.camera === null ? "#/wall" : `#/wall/${encodeURIComponent(route.camera)}`;
    case "incidents":
      return route.id === null ? "#/incidents" : `#/incidents/${encodeURIComponent(route.id)}`;
    case "cameras":
      return route.id === null ? "#/cameras" : `#/cameras/${encodeURIComponent(route.id)}`;
    case "zones":
      return route.camera === null ? "#/zones" : `#/zones/${encodeURIComponent(route.camera)}`;
    case "system":
      return "#/system";
  }
}

export function useRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));
  useEffect(() => {
    const onChange = () => {
      setRoute(parse(window.location.hash));
    };
    window.addEventListener("hashchange", onChange);
    return () => {
      window.removeEventListener("hashchange", onChange);
    };
  }, []);
  const go = useCallback((next: Route) => {
    window.location.hash = href(next);
  }, []);
  return [route, go];
}
