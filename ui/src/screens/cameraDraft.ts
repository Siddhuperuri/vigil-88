import type { CameraConfig, SourceInput } from "../api/types";

export type Kind = "webcam" | "rtsp" | "video_file";

/** Only the sources an operator can set up from the console. `image` and `synthetic` are for tests
 * and stay out of the list so nobody is offered a camera that is not one. */
export const KINDS: { value: Kind; label: string }[] = [
  { value: "webcam", label: "Webcam or capture card" },
  { value: "rtsp", label: "Network camera (RTSP)" },
  { value: "video_file", label: "Video file (replay)" },
];

export const ID_PATTERN = /^[a-z0-9][a-z0-9_-]{0,38}$/;

export interface Draft {
  id: string;
  name: string;
  kind: Kind;
  deviceIndex: string;
  url: string;
  credentialRef: string;
  username: string;
  password: string;
  path: string;
  loop: boolean;
  priority: string;
  fps: string;
  enabled: boolean;
}

export function draftOf(camera: CameraConfig | null): Draft {
  const s = camera?.source;
  const kind = s?.kind === "rtsp" || s?.kind === "video_file" ? s.kind : "webcam";
  return {
    id: camera?.camera_id ?? "",
    name: camera?.name ?? "",
    kind,
    deviceIndex: String(s?.device_index ?? 0),
    url: s?.url ?? "",
    credentialRef: s?.credential_ref ?? "",
    username: "",
    password: "",
    path: s?.path ?? "",
    loop: s?.loop ?? true,
    priority: String(camera?.priority ?? 5),
    fps: String(camera?.target_inference_fps ?? 5),
    enabled: camera?.enabled ?? true,
  };
}

export function sourceOf(d: Draft): SourceInput {
  if (d.kind === "webcam") return { kind: "webcam", device_index: Number(d.deviceIndex) || 0 };
  if (d.kind === "rtsp") {
    return {
      kind: "rtsp",
      url: d.url.trim(),
      ...(d.credentialRef.trim() === "" ? {} : { credential_ref: d.credentialRef.trim() }),
    };
  }
  return { kind: "video_file", path: d.path.trim(), loop: d.loop, realtime: true };
}

/** What is wrong with a draft, by field, in words. The engine re-checks everything. */
export function problemsOf(d: Draft, creating: boolean): Record<string, string> {
  const out: Record<string, string> = {};
  if (creating && !ID_PATTERN.test(d.id))
    out["id"] =
      "Use lower-case letters, digits, '-' or '_' (up to 39), starting with a letter or digit.";
  if (creating && d.id === "new") out["id"] = "'new' is reserved. Pick another name.";
  if (d.kind === "rtsp" && !/^rtsps?:\/\/\S+$/i.test(d.url.trim()))
    out["url"] = "Enter an address that starts with rtsp:// or rtsps://.";
  if (d.kind === "rtsp" && /^rtsps?:\/\/[^/]*@/i.test(d.url.trim()))
    out["url"] = "Do not put the username or password in the address. Use the credential fields.";
  if (
    d.kind === "rtsp" &&
    d.credentialRef.trim() !== "" &&
    !ID_PATTERN.test(d.credentialRef.trim())
  )
    out["credential"] = "A credential name is a short slug such as 'gate-camera'.";
  if (d.kind === "video_file" && d.path.trim() === "")
    out["path"] = "Enter the path to a video file.";
  if (d.password !== "" && d.credentialRef.trim() === "")
    out["credential"] = "Name this credential so it can be stored.";
  const priority = Number(d.priority);
  if (!Number.isInteger(priority) || priority < 0 || priority > 9)
    out["priority"] = "Whole number 0 to 9.";
  if (!(Number(d.fps) > 0)) out["fps"] = "A number above 0.";
  return out;
}
