import type { EngineState } from "./supervisor";

const escape = (text: string): string =>
  text.replace(/[&<>"']/g, (c) => `&#${String(c.charCodeAt(0))};`);

function words(state: EngineState): { title: string; detail: string; live: "status" | "alert" } {
  switch (state.kind) {
    case "starting":
      return {
        title: "Starting the engine",
        detail:
          state.attempt > 1 ? `Attempt ${String(state.attempt)}.` : "This takes a few seconds.",
        live: "status",
      };
    case "backoff":
      return {
        title: "The engine stopped",
        detail: `${state.reason}. Trying again in ${String(Math.ceil(state.retryInMs / 1000))} s (attempt ${String(state.attempt)}).`,
        live: "alert",
      };
    case "stopping":
      return {
        title: "Stopping the engine",
        detail: "Releasing the cameras and saving.",
        live: "status",
      };
    case "stopped":
      return { title: "The engine is not running", detail: "It was stopped.", live: "alert" };
    case "running":
      return { title: "The engine is running", detail: "Loading the console.", live: "status" };
  }
}

/**
 * What the window shows while the engine is not available: a local page, so a dead engine is a
 * named condition with a reason and not a blank window. It carries no script and loads nothing.
 */
export function statusPage(state: EngineState): string {
  const { title, detail, live } = words(state);
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>VIGIL-88</title>
<style>
:root{color-scheme:dark light}
body{margin:0;font:13px/1.4 "Segoe UI",system-ui,sans-serif;background:#0a0b0d;color:#e6e9ef}
@media (prefers-color-scheme:light){body{background:#f4f5f7;color:#14171c}}
main{max-width:40rem;margin:0 auto;padding:48px 16px}
h1{font-size:1.4rem;margin:0 0 8px}
p{color:inherit;margin:0}
</style></head><body><main>
<h1>VIGIL-88</h1>
<section role="${live}" aria-labelledby="t"><h2 id="t" style="font-size:1.1rem;margin:16px 0 4px">${escape(title)}</h2>
<p>${escape(detail)}</p></section></main></body></html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}
