import { useId, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { sendJson } from "../api/client";
import type { Role, User } from "../api/types";
import { messageOf, useFetch } from "../api/useFetch";
import { Notice } from "../components/bits";
import { Field } from "../components/Field";

const ROLES: { value: Role; label: string; what: string }[] = [
  { value: "viewer", label: "Viewer", what: "Can watch the wall and read incidents." },
  {
    value: "operator",
    label: "Operator",
    what: "Viewer, plus acknowledging, dismissing, escalating and resolving incidents, muting and exports.",
  },
  {
    value: "administrator",
    label: "Administrator",
    what: "Everything, including cameras, zones, people and the audit log.",
  },
];

const MIN_PASSWORD = 12;

function AddPerson({ onAdded }: { onAdded: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("operator");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shown, setShown] = useState(false);
  const roleHelp = useId();
  const problems: Record<string, string> = {};
  if (shown && username.trim() === "") problems["username"] = "Enter a username.";
  if (shown && password.length < MIN_PASSWORD)
    problems["password"] =
      `Use at least ${String(MIN_PASSWORD)} characters. A phrase of several words is fine.`;

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault();
    setShown(true);
    setError(null);
    if (username.trim() === "" || password.length < MIN_PASSWORD) return;
    setBusy(true);
    try {
      await sendJson("POST", "/api/v1/users", { username: username.trim(), password, role });
      setUsername("");
      setPassword("");
      setShown(false);
      onAdded();
    } catch (e) {
      setError(messageOf(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={(e) => {
        void submit(e);
      }}
      noValidate
      aria-labelledby="add-person-title"
    >
      <h2 id="add-person-title">Add a person</h2>
      <Field label="Username" error={problems["username"]}>
        {(p) => (
          <input
            {...p}
            autoComplete="off"
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
            }}
          />
        )}
      </Field>
      <Field
        label="Temporary password"
        error={problems["password"]}
        hint="They are asked to change it the first time they sign in."
      >
        {(p) => (
          <input
            {...p}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
            }}
          />
        )}
      </Field>
      <Field label="Role">
        {(p) => (
          <select
            {...p}
            aria-describedby={roleHelp}
            value={role}
            onChange={(e) => {
              setRole(e.target.value as Role);
            }}
          >
            {ROLES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      <p id={roleHelp} className="hint">
        {ROLES.find((r) => r.value === role)?.what}
      </p>
      {error !== null && (
        <Notice kind="error" title="Not added">
          <p>{error}</p>
        </Notice>
      )}
      <button type="submit" className="primary" disabled={busy}>
        {busy ? "Adding…" : "Add person"}
      </button>
    </form>
  );
}

type Action = { kind: "reset" | "delete"; user: User };

function ActionDialog({
  action,
  onClose,
  onDone,
}: {
  action: Action;
  onClose: () => void;
  onDone: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { kind, user } = action;
  const tooShort = kind === "reset" && password.length < MIN_PASSWORD;

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "reset")
        await sendJson("POST", `/api/v1/users/${encodeURIComponent(user.user_id)}/reset-password`, {
          new_password: password,
        });
      else await sendJson("DELETE", `/api/v1/users/${encodeURIComponent(user.user_id)}`);
      onDone();
    } catch (e) {
      setError(messageOf(e));
      setBusy(false);
    }
  };

  return (
    <dialog
      ref={(el) => {
        ref.current = el;
        if (el !== null && !el.open) el.showModal();
      }}
      onClose={onClose}
      aria-labelledby="person-action-title"
    >
      <h2 id="person-action-title">
        {kind === "reset" ? `Reset the password for ${user.username}?` : `Delete ${user.username}?`}
      </h2>
      {kind === "reset" ? (
        <>
          <p>Their sessions end now and they must choose a new password when they next sign in.</p>
          <Field
            label="New temporary password"
            hint={`At least ${String(MIN_PASSWORD)} characters.`}
          >
            {(p) => (
              <input
                {...p}
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                }}
              />
            )}
          </Field>
        </>
      ) : (
        <p>
          Their account and sessions are removed. What they did stays in the audit log and on the
          incidents they handled, under their name. This cannot be undone.
        </p>
      )}
      {error !== null && (
        <Notice kind="error" title="Not done">
          <p>{error}</p>
        </Notice>
      )}
      <div className="row actions">
        <button
          type="button"
          onClick={() => {
            ref.current?.close();
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          className={kind === "delete" ? "danger" : "primary"}
          disabled={busy || tooShort}
          title={tooShort ? `Enter at least ${String(MIN_PASSWORD)} characters first` : undefined}
          onClick={() => {
            void confirm();
          }}
        >
          {busy ? "Working…" : kind === "reset" ? "Reset password" : "Delete account"}
        </button>
      </div>
    </dialog>
  );
}

export function People({ me }: { me: string }) {
  const [list, reload] = useFetch<User[]>("/api/v1/users");
  const [action, setAction] = useState<Action | null>(null);
  const [error, setError] = useState<string | null>(null);
  const users = list.state === "ready" ? list.data : (list.previous ?? []);

  const patch = async (user: User, change: { role?: Role; disabled?: boolean }) => {
    setError(null);
    try {
      await sendJson("PATCH", `/api/v1/users/${encodeURIComponent(user.user_id)}`, change);
      reload();
    } catch (e) {
      setError(messageOf(e));
    }
  };

  return (
    <>
      <h1>People</h1>
      <p className="muted">
        Accounts on this computer. Every change here is written to the audit log with your name.
      </p>
      {list.state === "failed" && (
        <Notice kind="error" title="Could not load the people">
          <p>{list.message}</p>
          <button type="button" onClick={reload}>
            Try again
          </button>
        </Notice>
      )}
      {error !== null && (
        <Notice kind="error" title="Not changed">
          <p>{error}</p>
        </Notice>
      )}
      {list.state === "loading" && users.length === 0 && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {users.length > 0 && (
        <table>
          <thead>
            <tr>
              <th scope="col">Username</th>
              <th scope="col">Role</th>
              <th scope="col">Status</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const self = u.username === me;
              const why = self
                ? "You cannot change your own account here. Ask another administrator."
                : undefined;
              return (
                <tr key={u.user_id}>
                  <th scope="row">
                    {u.username}
                    {self && <span className="muted"> (you)</span>}
                  </th>
                  <td>
                    <select
                      aria-label={`Role for ${u.username}`}
                      value={u.role}
                      disabled={self}
                      title={why}
                      onChange={(e) => {
                        void patch(u, { role: e.target.value as Role });
                      }}
                    >
                      {ROLES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    {u.disabled ? "Disabled" : "Active"}
                    {u.must_change_password && (
                      <span className="muted"> · must change password</span>
                    )}
                  </td>
                  <td>
                    <button
                      type="button"
                      disabled={self}
                      title={why}
                      onClick={() => {
                        void patch(u, { disabled: !u.disabled });
                      }}
                      aria-label={`${u.disabled ? "Enable" : "Disable"} ${u.username}`}
                    >
                      {u.disabled ? "Enable" : "Disable"}
                    </button>{" "}
                    <button
                      type="button"
                      onClick={() => {
                        setAction({ kind: "reset", user: u });
                      }}
                      aria-label={`Reset password for ${u.username}`}
                    >
                      Reset password
                    </button>{" "}
                    <button
                      type="button"
                      className="danger"
                      disabled={self}
                      title={why}
                      onClick={() => {
                        setAction({ kind: "delete", user: u });
                      }}
                      aria-label={`Delete ${u.username}`}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <AddPerson onAdded={reload} />
      {action !== null && (
        <ActionDialog
          action={action}
          onClose={() => {
            setAction(null);
          }}
          onDone={() => {
            setAction(null);
            reload();
          }}
        />
      )}
    </>
  );
}
