import { useEffect, useId, useState } from "react";
import type { KeyboardEvent, SyntheticEvent } from "react";
import { ApiError } from "../api/client";
import { Notice } from "../components/bits";
import { useAuth } from "../state/auth";

/** What the Electron shell exposes. In a plain browser there is no bridge and the token is typed. */
interface Shell {
  setupToken: () => Promise<string | null>;
}
const shell = (): Shell | undefined => (window as unknown as { vigilShell?: Shell }).vigilShell;

function PasswordField({
  label,
  value,
  onChange,
  autoComplete,
  hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  hint?: string;
}) {
  const id = useId();
  const [shown, setShown] = useState(false);
  const [caps, setCaps] = useState(false);
  const watchCaps = (event: KeyboardEvent<HTMLInputElement>) => {
    setCaps(event.getModifierState("CapsLock"));
  };
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="row">
        <input
          id={id}
          type={shown ? "text" : "password"}
          value={value}
          autoComplete={autoComplete}
          onChange={(e) => {
            onChange(e.target.value);
          }}
          onKeyUp={watchCaps}
          onKeyDown={watchCaps}
          aria-describedby={`${id}-help`}
          required
        />
        <button
          type="button"
          aria-pressed={shown}
          onClick={() => {
            setShown((s) => !s);
          }}
        >
          {shown ? "Hide" : "Show"}
        </button>
      </div>
      <p id={`${id}-help`} className="hint">
        {caps ? "Caps Lock is on. " : ""}
        {hint ?? ""}
      </p>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  autoComplete?: string;
}) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        autoComplete={autoComplete}
        onChange={(e) => {
          onChange(e.target.value);
        }}
        required
      />
    </div>
  );
}

/** Whole seconds left until `deadline` (epoch ms), or null when there is none or it has passed. */
function useCountdown(deadline: number | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (deadline === null) return;
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 500);
    return () => {
      clearInterval(timer);
    };
  }, [deadline]);
  if (deadline === null || now >= deadline) return null;
  return Math.ceil((deadline - now) / 1000);
}

export function Login() {
  const auth = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deadline, setDeadline] = useState<number | null>(null);
  const remaining = useCountdown(deadline);

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.login(username, password);
    } catch (e) {
      setPassword("");
      if (e instanceof ApiError && e.status === 429 && e.retryAfterS !== null) {
        setDeadline(Date.now() + e.retryAfterS * 1000);
      }
      setError(e instanceof Error ? e.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  const locked = remaining !== null;
  return (
    <main className="gate" aria-labelledby="gate-title">
      <h1 id="gate-title">Sign in to VIGIL-88</h1>
      <form onSubmit={(e) => void submit(e)}>
        {error !== null && (
          <p role="alert" className="form-error">
            {locked ? `Too many attempts. Try again in ${String(remaining)} s.` : error}
          </p>
        )}
        <TextField
          label="Username"
          value={username}
          onChange={setUsername}
          autoComplete="username"
        />
        <PasswordField
          label="Password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
        />
        <button type="submit" className="primary" disabled={busy || locked}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

export function Lock({ username }: { username: string }) {
  const auth = useAuth();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await auth.unlock(password);
    } catch (e) {
      setPassword("");
      setError(e instanceof Error ? e.message : "Could not unlock.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="gate" aria-labelledby="gate-title">
      <h1 id="gate-title">Console locked</h1>
      <p className="muted">Monitoring continues. Enter the password for {username} to continue.</p>
      <form onSubmit={(e) => void submit(e)}>
        {error !== null && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        <PasswordField
          label="Password"
          value={password}
          onChange={setPassword}
          autoComplete="current-password"
        />
        <div className="row">
          <button type="submit" className="primary" disabled={busy}>
            Unlock
          </button>
          <button
            type="button"
            onClick={() => {
              void auth.logout();
            }}
          >
            Sign out
          </button>
        </div>
      </form>
    </main>
  );
}

export function Setup() {
  const auth = useAuth();
  const [token, setToken] = useState("");
  const [fromShell, setFromShell] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void shell()
      ?.setupToken()
      .then((t) => {
        if (t !== null) {
          setToken(t);
          setFromShell(true);
        }
      });
  }, []);

  const submit = async (event: SyntheticEvent) => {
    event.preventDefault();
    if (password !== again) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const done = await auth.setup(token, username, password);
      setCodes(done.recovery_codes);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Setup failed.");
    } finally {
      setBusy(false);
    }
  };

  if (codes !== null) {
    const text = codes.join("\n");
    const download = () => {
      const url = URL.createObjectURL(new Blob([text + "\n"], { type: "text/plain" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "vigil-88-recovery-codes.txt";
      link.click();
      URL.revokeObjectURL(url);
    };
    return (
      <main className="gate" aria-labelledby="gate-title">
        <h1 id="gate-title">Save your recovery codes</h1>
        <p>
          Each code resets a password once. They are shown <strong>only now</strong>. Store them
          like a password; anyone holding one can take the account.
        </p>
        <ul className="codes num" aria-label="Recovery codes">
          {codes.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
        <div className="row">
          <button type="button" onClick={download}>
            Save as a file
          </button>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(text);
            }}
          >
            Copy
          </button>
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={saved}
            onChange={(e) => {
              setSaved(e.target.checked);
            }}
          />
          I have stored these codes somewhere safe
        </label>
        <button type="button" className="primary" disabled={!saved} onClick={auth.refresh}>
          Continue
        </button>
      </main>
    );
  }

  return (
    <main className="gate" aria-labelledby="gate-title">
      <h1 id="gate-title">Set up VIGIL-88</h1>
      <p className="muted">
        Create the first administrator. There is no default account, and this is the only time this
        screen appears.
      </p>
      <form onSubmit={(e) => void submit(e)}>
        {error !== null && (
          <p role="alert" className="form-error">
            {error}
          </p>
        )}
        {!fromShell && (
          <TextField label="Setup token" value={token} onChange={setToken} autoComplete="off" />
        )}
        <TextField
          label="Administrator username"
          value={username}
          onChange={setUsername}
          autoComplete="username"
        />
        <PasswordField
          label="Password"
          value={password}
          onChange={setPassword}
          autoComplete="new-password"
          hint="At least 12 characters. A passphrase of ordinary words is fine."
        />
        <PasswordField
          label="Repeat the password"
          value={again}
          onChange={setAgain}
          autoComplete="new-password"
        />
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Creating…" : "Create administrator"}
        </button>
      </form>
      {!fromShell && (
        <Notice kind="info" title="No setup token?">
          <p>
            Running without the desktop app, the engine printed a one-time token on its console when
            it started.
          </p>
        </Notice>
      )}
    </main>
  );
}
