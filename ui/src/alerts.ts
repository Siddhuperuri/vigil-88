import { asSeverity } from "./api/types";

/** What an arriving incident may do beyond appearing in the list. Pure, so it is tested as a table. */
export interface AlertPlan {
  /** Beeps to sound: frequency in Hz and how many. Empty for anything below HIGH. */
  tone: { hz: number; beeps: number } | null;
  /** Whether to ask the desktop shell for a native notification. */
  notify: boolean;
}

export function planAlert(
  change: "new" | "updated",
  severity: string,
  focused: boolean,
  soundOn: boolean,
): AlertPlan {
  const band = asSeverity(severity);
  const loud = change === "new" && (band === "CRITICAL" || band === "HIGH");
  if (!loud) return { tone: null, notify: false };
  return {
    tone: soundOn ? (band === "CRITICAL" ? { hz: 880, beeps: 3 } : { hz: 660, beeps: 2 }) : null,
    // A person already looking at the console does not need a toast on top of it.
    notify: !focused,
  };
}

/** Sounds a short pattern with the Web Audio API, so no sound file has to ship. Returns false when
 * the browser has no audio, which is a convenience lost and never an error. */
export function playTone(tone: { hz: number; beeps: number }): boolean {
  const audio = window as unknown as {
    AudioContext?: typeof AudioContext;
    webkitAudioContext?: typeof AudioContext;
  };
  const Context = audio.AudioContext ?? audio.webkitAudioContext;
  if (typeof Context !== "function") return false;
  try {
    const ctx = new Context();
    for (let i = 0; i < tone.beeps; i++) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = tone.hz;
      gain.gain.value = 0.15; // an alert, not a siren
      osc.connect(gain).connect(ctx.destination);
      const start = ctx.currentTime + i * 0.28;
      osc.start(start);
      osc.stop(start + 0.18);
    }
    window.setTimeout(
      () => {
        void ctx.close();
      },
      tone.beeps * 280 + 400,
    );
    return true;
  } catch {
    return false;
  }
}

export interface ShellBridge {
  notify?: (alert: unknown) => Promise<boolean>;
}
export const shellBridge = (): ShellBridge | undefined =>
  (window as unknown as { vigilShell?: ShellBridge }).vigilShell;
