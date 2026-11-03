import type { AlertSettings } from "../api/types";
import { useFetch } from "../api/useFetch";
import { Notice } from "../components/bits";
import { href } from "../router";
import { AlertPolicyEditor } from "./AlertPolicyEditor";
import { SettingsTransfer } from "./SettingsTransfer";

/** Settings the console manages. Administrators only: the route is guarded in `App`. */
export function Settings() {
  const [loaded] = useFetch<AlertSettings>("/api/v1/settings/alerts");
  return (
    <>
      <h1>Settings</h1>
      <p>
        <a href={href({ view: "setup" })}>Run the setup guide again</a> to add a camera, draw a zone
        and choose how alerts reach you.
      </p>
      <h2>Alerts</h2>
      {loaded.state === "failed" && (
        <Notice kind="error" title="Could not load the alert settings">
          <p>{loaded.message}</p>
        </Notice>
      )}
      {loaded.state !== "ready" && loaded.state !== "failed" && (
        <p role="status" className="muted">
          Loading alert settings…
        </p>
      )}
      {loaded.state === "ready" && (
        <AlertPolicyEditor initial={loaded.data.policy} channels={loaded.data.channels} editable />
      )}
      <SettingsTransfer />
    </>
  );
}
