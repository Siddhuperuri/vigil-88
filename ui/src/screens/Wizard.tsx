import { useState } from "react";
import type { AdminCamera, Role } from "../api/types";
import { useFetch } from "../api/useFetch";
import { Notice } from "../components/bits";
import { href } from "../router";
import { usePending } from "../state/pending";
import { CameraForm } from "./CameraForm";

const STEPS = ["Welcome", "A camera", "Zones", "Alerts", "Finish"] as const;

/**
 * The setup guide: what to do first, in order, with a way to stop at any step. Every step uses the
 * screens that already exist, so what the guide saves is exactly what those screens save, and
 * nothing here applies until the last step says so.
 */
export function Wizard({ role }: { role: Role }) {
  const [step, setStep] = useState(0);
  const [adding, setAdding] = useState(false);
  const [cameras, reloadCameras] = useFetch<AdminCamera[]>("/api/v1/config/cameras");
  const pending = usePending();
  const list = cameras.state === "ready" ? cameras.data : [];
  const hasCamera = list.length > 0;

  if (role !== "administrator") {
    return (
      <>
        <h1>Setup guide</h1>
        <p>The setup guide changes how monitoring is set up, so it needs an administrator.</p>
      </>
    );
  }

  return (
    <>
      <h1>Setup guide</h1>
      <nav aria-label="Setup steps">
        <ol className="row wrap">
          {STEPS.map((name, i) => (
            <li key={name} aria-current={i === step ? "step" : undefined}>
              <span className={i === step ? "num" : "muted"}>
                {i < step && <span aria-hidden="true">✓ </span>}
                {i + 1}. {name}
              </span>
            </li>
          ))}
        </ol>
      </nav>
      <p role="status" className="sr-only">
        Step {step + 1} of {STEPS.length}: {STEPS[step]}
      </p>

      {step === 0 && (
        <section aria-labelledby="w0">
          <h2 id="w0">Welcome</h2>
          <p>
            This takes a few minutes. You will add a camera, optionally mark the areas that matter,
            and choose how alerts reach you. Nothing starts until the last step, and you can stop at
            any step: what you have saved stays saved.
          </p>
          <p className="muted">
            VIGIL-88 finds events and tells people. It does not decide that something is wrong, and
            it can be wrong. A person looks at every alert.
          </p>
        </section>
      )}

      {step === 1 && (
        <section aria-labelledby="w1">
          <h2 id="w1">Add a camera</h2>
          {hasCamera && !adding && (
            <p role="status" className="state-ok">
              <span aria-hidden="true">●</span> {list.length}{" "}
              {list.length === 1 ? "camera is" : "cameras are"} set up:{" "}
              {list.map((c) => c.config.camera_id).join(", ")}.
            </p>
          )}
          {(!hasCamera || adding) && (
            <CameraForm
              existing={null}
              onDone={() => {
                setAdding(false);
                reloadCameras();
                pending.refresh();
              }}
              onCancel={() => {
                setAdding(false);
              }}
            />
          )}
          {hasCamera && !adding && (
            <button
              type="button"
              onClick={() => {
                setAdding(true);
              }}
            >
              Add another camera
            </button>
          )}
        </section>
      )}

      {step === 2 && (
        <section aria-labelledby="w2">
          <h2 id="w2">Zones</h2>
          <p>
            A zone is an area that matters, such as a door or a fence line. Without zones the camera
            is still watched for people who fall or objects left behind, but intrusion needs an area
            to guard.
          </p>
          {hasCamera ? (
            <ul>
              {list.map((c) => (
                <li key={c.config.camera_id}>
                  <a href={href({ view: "zones", camera: c.config.camera_id })}>
                    Draw zones for {c.config.camera_id}
                  </a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">Add a camera first, or skip this step.</p>
          )}
          <p className="hint">
            You can skip this and come back later from Zones. Open a link above and return to the
            setup guide with the browser’s back button.
          </p>
        </section>
      )}

      {step === 3 && (
        <section aria-labelledby="w3">
          <h2 id="w3">Alerts</h2>
          <p>
            Choose from which severity alerts interrupt you, and whether there are hours you do not
            want to be disturbed. CRITICAL always gets through, and every incident is recorded
            either way.
          </p>
          <p>
            <a href={href({ view: "settings" })}>Open alert settings</a>
          </p>
          <p className="hint">The defaults are sensible; you can leave this as it is.</p>
        </section>
      )}

      {step === 4 && (
        <section aria-labelledby="w4">
          <h2 id="w4">Finish</h2>
          {pending.pending?.pending === true ? (
            <Notice kind="info" title="Saved changes are waiting">
              <p>
                {pending.pending.changes.length === 1
                  ? "1 change is"
                  : `${String(pending.pending.changes.length)} changes are`}{" "}
                saved and not yet in use. Monitoring has to restart to start using them; every
                camera pauses for a few seconds.
              </p>
              <button
                type="button"
                className="primary"
                disabled={!pending.pending.can_restart || pending.phase === "applying"}
                onClick={() => {
                  void pending.apply();
                }}
              >
                {pending.phase === "applying" ? "Restarting…" : "Restart monitoring now"}
              </button>
            </Notice>
          ) : (
            <p role="status">
              Everything you saved is in use.{" "}
              <a href={href({ view: "wall", camera: null })}>Go to the live wall</a>.
            </p>
          )}
        </section>
      )}

      <div className="row actions">
        <button
          type="button"
          disabled={step === 0}
          onClick={() => {
            setStep(step - 1);
          }}
        >
          Back
        </button>
        {step < STEPS.length - 1 ? (
          <button
            type="button"
            className="primary"
            onClick={() => {
              setStep(step + 1);
            }}
          >
            {step === 2 || step === 3 ? "Next (or skip)" : "Next"}
          </button>
        ) : (
          <a href={href({ view: "wall", camera: null })}>Done</a>
        )}
      </div>
    </>
  );
}
