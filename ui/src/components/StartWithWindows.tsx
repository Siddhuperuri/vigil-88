import { useEffect, useState } from "react";

interface Bridge {
  autostart?: () => Promise<{ enabled: boolean } | null>;
  setAutostart?: (enabled: boolean) => Promise<{ enabled: boolean } | null>;
}
const bridge = (): Bridge | undefined => (window as unknown as { vigilShell?: Bridge }).vigilShell;

/** Whether the desktop app starts when Windows does. Off until the person turns it on, and when it
 * does start that way it stays in the tray and watches instead of opening a window. */
export function StartWithWindows() {
  const shell = bridge();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const available = shell?.autostart !== undefined && shell.setAutostart !== undefined;

  useEffect(() => {
    if (shell?.autostart === undefined) return;
    void shell.autostart().then(
      (state) => {
        setEnabled(state === null ? null : state.enabled);
      },
      () => {
        setEnabled(null);
      },
    );
  }, [shell]);

  if (!available)
    return (
      <p className="muted">Starting with Windows is a setting of the desktop app, not this page.</p>
    );

  const change = async (next: boolean) => {
    setError(null);
    try {
      const state = await shell.setAutostart?.(next);
      if (state === null || state === undefined) throw new Error("The app would not change it.");
      setEnabled(state.enabled);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The setting could not be changed.");
    }
  };

  return (
    <>
      <label className="check">
        <input
          type="checkbox"
          checked={enabled === true}
          disabled={enabled === null}
          onChange={(e) => {
            void change(e.target.checked);
          }}
        />{" "}
        Start VIGIL-88 when I sign in to Windows
      </label>
      <p className="hint">
        It starts in the tray, without opening a window, and monitoring begins at once.
        {enabled === null ? " The current setting is unavailable." : ""}
      </p>
      {error !== null && (
        <p role="alert" className="field-error">
          {error}
        </p>
      )}
    </>
  );
}
