import type { EvidenceAnnotations } from "../api/types";
import { Notice } from "./bits";

const KIND_WORDS: Record<string, string> = {
  stream_restarted: "the camera connection was restarted",
  frame_gap: "the camera stopped sending pictures",
};

const seconds = (ms: number): string => `${(ms / 1000).toFixed(1)} s`;

/** Says so when the clips are not one continuous recording, instead of letting a jump look like
 * continuous video. Absent when the capture recorded nothing about continuity. */
export function EvidenceContinuity({ notes }: { notes: EvidenceAnnotations | null | undefined }) {
  if (notes === null || notes === undefined) return null;
  const shortfall = notes.preroll_shortfall_ms;
  const short = shortfall !== null && shortfall >= 500;
  if (!notes.stream_interrupted && !short) return null;
  return (
    <Notice kind="warning" title="This recording has a gap">
      {notes.stream_interrupted && (
        <>
          <p>
            The footage is not continuous. Times are measured from when the event was first noticed
            (negative is before).
          </p>
          <ul>
            {notes.gaps.map((g) => (
              <li key={`${g.kind}:${String(g.start_ms)}`}>
                From {seconds(g.start_ms)} to {seconds(g.end_ms)}:{" "}
                {KIND_WORDS[g.kind] ?? g.kind.replaceAll("_", " ")}.
              </li>
            ))}
          </ul>
        </>
      )}
      {short && (
        <p>
          The clip starts {seconds(shortfall)} later than the time before the event that was asked
          for, because that footage was not available.
        </p>
      )}
    </Notice>
  );
}
