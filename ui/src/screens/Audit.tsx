import { useEffect, useState } from "react";
import { getJson } from "../api/client";
import type { AuditEntry, ChainReport } from "../api/types";
import { messageOf } from "../api/useFetch";
import { Notice, when } from "../components/bits";
import { saveBlob } from "../download";

const PAGE = 100;

/** Reads the administrators' audit log: who did what, newest first, and whether it has been altered. */
export function Audit() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [action, setAction] = useState("");
  const [applied, setApplied] = useState("");
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState<string | null>(null);
  const [more, setMore] = useState(true);
  const [tick, setTick] = useState(0);
  const [chain, setChain] = useState<ChainReport | "checking" | null>(null);
  const [chainError, setChainError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const query = new URLSearchParams({ limit: String(PAGE) });
    if (applied !== "") query.set("action", applied);
    getJson<AuditEntry[]>(`/api/v1/audit?${query.toString()}`, controller.signal).then(
      (rows) => {
        setEntries(rows);
        setMore(rows.length === PAGE);
        setState("ready");
      },
      (e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(messageOf(e));
        setState("failed");
      },
    );
    return () => {
      controller.abort();
    };
  }, [applied, tick]);

  const older = async () => {
    const last = entries[entries.length - 1];
    if (last === undefined) return;
    const query = new URLSearchParams({ limit: String(PAGE), before_seq: String(last.seq) });
    if (applied !== "") query.set("action", applied);
    try {
      const rows = await getJson<AuditEntry[]>(`/api/v1/audit?${query.toString()}`);
      setEntries((all) => [...all, ...rows]);
      setMore(rows.length === PAGE);
    } catch (e) {
      setError(messageOf(e));
      setState("failed");
    }
  };

  const verify = async () => {
    setChain("checking");
    setChainError(null);
    try {
      setChain(await getJson<ChainReport>("/api/v1/audit/verify"));
    } catch (e) {
      setChain(null);
      setChainError(messageOf(e));
    }
  };

  const exportAs = async (format: "csv" | "json") => {
    try {
      const response = await fetch(`/api/v1/audit/export?format=${format}`, {
        credentials: "same-origin",
        headers: { "X-Vigil-Request": "1" },
      });
      if (!response.ok) throw new Error(`The engine answered ${String(response.status)}.`);
      saveBlob(await response.blob(), `vigil-audit.${format}`);
    } catch (e) {
      setChainError(messageOf(e));
    }
  };

  return (
    <>
      <h1>Audit log</h1>
      <p className="muted">
        Sign-ins, account changes, configuration changes and incident decisions. Each row is chained
        to the one before it, so an edit or a deletion can be detected, though not prevented.
      </p>

      <div className="row actions">
        <button
          type="button"
          disabled={chain === "checking"}
          onClick={() => {
            void verify();
          }}
        >
          {chain === "checking" ? "Checking…" : "Verify the chain"}
        </button>
        <button
          type="button"
          onClick={() => {
            void exportAs("csv");
          }}
        >
          Export CSV
        </button>
        <button
          type="button"
          onClick={() => {
            void exportAs("json");
          }}
        >
          Export JSON
        </button>
      </div>
      <p className="hint">An export holds the newest 1,000 rows.</p>
      {chain !== null && chain !== "checking" && (
        <p role="status" className={chain.ok ? "state-ok" : "field-error"}>
          <span aria-hidden="true">{chain.ok ? "●" : "✕"}</span>{" "}
          {chain.ok
            ? `Intact: ${String(chain.rows)} rows, none altered.`
            : `Broken at row ${String(chain.first_bad_seq)}: that row or an earlier one was changed or removed. ${String(chain.rows)} rows were read.`}
        </p>
      )}
      {chainError !== null && (
        <Notice kind="error" title="Could not finish">
          <p>{chainError}</p>
        </Notice>
      )}

      <form
        className="row"
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          setState("loading");
          setApplied(action.trim());
        }}
      >
        <label htmlFor="audit-action">Action</label>
        <input
          id="audit-action"
          value={action}
          placeholder="e.g. user.created"
          onChange={(e) => {
            setAction(e.target.value);
          }}
        />
        <button type="submit">Filter</button>
        {applied !== "" && (
          <button
            type="button"
            onClick={() => {
              setAction("");
              setApplied("");
              setState("loading");
            }}
          >
            Clear filter
          </button>
        )}
      </form>

      {state === "failed" && (
        <Notice kind="error" title="Could not load the audit log">
          <p>{error}</p>
          <button
            type="button"
            onClick={() => {
              setState("loading");
              setTick((n) => n + 1);
            }}
          >
            Try again
          </button>
        </Notice>
      )}
      {state === "loading" && (
        <p role="status" className="muted">
          Loading…
        </p>
      )}
      {state === "ready" && entries.length === 0 && (
        <p className="muted">
          {applied === "" ? "The audit log is empty." : "No rows match that action."}
        </p>
      )}
      {entries.length > 0 && (
        <table>
          <caption className="sr-only">Audit log, newest first</caption>
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">When</th>
              <th scope="col">Who</th>
              <th scope="col">Action</th>
              <th scope="col">Target</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.seq}>
                <td className="num">{e.seq}</td>
                <td>{when(e.at_utc)}</td>
                <td>{e.username ?? <span className="muted">system</span>}</td>
                <td className="num">{e.action}</td>
                <td>{e.target ?? ""}</td>
                <td>{e.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {more && state === "ready" && (
        <button
          type="button"
          onClick={() => {
            void older();
          }}
        >
          Show older rows
        </button>
      )}
    </>
  );
}
