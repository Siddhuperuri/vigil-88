import type { Devices } from "../api/types";
import { Field } from "../components/Field";
import type { Draft } from "./cameraDraft";

export function SourceFields({
  d,
  set,
  problems,
  devices,
  credentialKnown,
}: {
  d: Draft;
  set: (patch: Partial<Draft>) => void;
  problems: Record<string, string>;
  devices: Devices | null;
  credentialKnown: boolean | null;
}) {
  if (d.kind === "webcam") {
    const list = devices?.available ? devices.devices : [];
    return (
      <Field
        label="Device"
        error={problems["device"]}
        hint={
          devices === null
            ? "Looking for devices…"
            : devices.available
              ? list.length === 0
                ? "No capture devices were found."
                : undefined
              : `Device names are unavailable: ${devices.reason ?? "unknown reason"}. Enter the device number.`
        }
      >
        {(p) =>
          list.length > 0 ? (
            <select
              {...p}
              value={d.deviceIndex}
              onChange={(e) => {
                set({ deviceIndex: e.target.value });
              }}
            >
              {list.map((x) => (
                <option key={x.index} value={String(x.index)}>
                  {x.name} (device {x.index})
                </option>
              ))}
            </select>
          ) : (
            <input
              {...p}
              type="number"
              min={0}
              value={d.deviceIndex}
              onChange={(e) => {
                set({ deviceIndex: e.target.value });
              }}
            />
          )
        }
      </Field>
    );
  }
  if (d.kind === "video_file") {
    return (
      <>
        <Field
          label="Video file path"
          error={problems["path"]}
          hint="A file on this computer. It is played at its own speed."
        >
          {(p) => (
            <input
              {...p}
              value={d.path}
              onChange={(e) => {
                set({ path: e.target.value });
              }}
            />
          )}
        </Field>
        <label className="check">
          <input
            type="checkbox"
            checked={d.loop}
            onChange={(e) => {
              set({ loop: e.target.checked });
            }}
          />{" "}
          Play it again when it ends
        </label>
      </>
    );
  }
  return (
    <>
      <Field
        label="Stream address"
        error={problems["url"]}
        hint="For example rtsp://192.168.1.20/stream1. Never include the password here."
      >
        {(p) => (
          <input
            {...p}
            inputMode="url"
            autoComplete="off"
            value={d.url}
            onChange={(e) => {
              set({ url: e.target.value });
            }}
          />
        )}
      </Field>
      <fieldset>
        <legend>Sign-in (optional)</legend>
        <Field
          label="Credential name"
          error={problems["credential"]}
          hint={
            credentialKnown === true
              ? "A username and password are stored under this name in Windows Credential Manager."
              : "Where the username and password are kept. They are never written to the camera file."
          }
        >
          {(p) => (
            <input
              {...p}
              autoComplete="off"
              value={d.credentialRef}
              onChange={(e) => {
                set({ credentialRef: e.target.value });
              }}
            />
          )}
        </Field>
        <Field label="Username" hint="Leave blank to keep what is stored.">
          {(p) => (
            <input
              {...p}
              autoComplete="off"
              value={d.username}
              onChange={(e) => {
                set({ username: e.target.value });
              }}
            />
          )}
        </Field>
        <Field label="Password" hint="Write-only. It is sent once and cannot be read back.">
          {(p) => (
            <input
              {...p}
              type="password"
              autoComplete="new-password"
              value={d.password}
              onChange={(e) => {
                set({ password: e.target.value });
              }}
            />
          )}
        </Field>
      </fieldset>
    </>
  );
}
