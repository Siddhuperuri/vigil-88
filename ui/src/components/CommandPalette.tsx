import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

export interface Command {
  id: string;
  label: string;
  /** Extra words that should find this command, such as a view's old name. */
  keywords?: string;
  run: () => void;
}

/** Every word typed must start a word of the label or keywords: "inc des" finds "Incident desk". */
export function matches(command: Command, query: string): boolean {
  const words = `${command.label} ${command.keywords ?? ""}`.toLowerCase().split(/\s+/);
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((part) => words.some((w) => w.startsWith(part)));
}

/**
 * Jump to a view or run an action by typing (Ctrl+K). It only lists what the person may do now, and
 * it is a shortcut to controls that exist elsewhere, never the only way to reach one.
 */
export function CommandPalette({
  commands,
  open,
  onClose,
}: {
  commands: Command[];
  open: boolean;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLUListElement>(null);
  const [query, setQuery] = useState("");
  const titleId = useId();
  const found = useMemo(() => commands.filter((c) => matches(c, query)), [commands, query]);

  useEffect(() => {
    const dialog = ref.current;
    if (dialog === null) return;
    if (open && !dialog.open) {
      dialog.showModal();
      input.current?.focus();
    }
    if (!open && dialog.open) dialog.close();
  }, [open]);

  const choose = (command: Command) => {
    onClose();
    setQuery("");
    command.run();
  };

  const buttons = (): HTMLButtonElement[] =>
    Array.from(list.current?.querySelectorAll<HTMLButtonElement>("button") ?? []);

  const fromInput = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      buttons()[0]?.focus();
    } else if (event.key === "Enter") {
      event.preventDefault();
      const first = found[0];
      if (first !== undefined) choose(first);
    }
  };

  const fromList = (event: KeyboardEvent<HTMLUListElement>) => {
    const all = buttons();
    const at = all.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      all[Math.min(all.length - 1, at + 1)]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (at <= 0) input.current?.focus();
      else all[at - 1]?.focus();
    }
  };

  return (
    <dialog
      ref={ref}
      className="palette"
      aria-labelledby={titleId}
      onClose={() => {
        setQuery("");
        onClose();
      }}
    >
      <h2 id={titleId} className="sr-only">
        Command palette
      </h2>
      <label htmlFor={`${titleId}-q`}>Go to or do</label>
      <input
        id={`${titleId}-q`}
        ref={input}
        value={query}
        autoComplete="off"
        placeholder="Type a view or an action"
        onChange={(e) => {
          setQuery(e.target.value);
        }}
        onKeyDown={fromInput}
      />
      <p role="status" className="hint">
        {found.length === 0
          ? "Nothing matches that."
          : `${String(found.length)} ${found.length === 1 ? "match" : "matches"}. Enter runs the first; Down moves into the list.`}
      </p>
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- arrow keys move focus between the buttons inside */}
      <ul ref={list} className="palette-list" onKeyDown={fromList}>
        {found.map((c) => (
          <li key={c.id}>
            <button
              type="button"
              onClick={() => {
                choose(c);
              }}
            >
              {c.label}
            </button>
          </li>
        ))}
      </ul>
    </dialog>
  );
}
