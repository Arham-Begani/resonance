// Inline value editor with live validation through POST /api/units/parse (debounced).
// Enter applies, Escape cancels. The server re-validates when the revision is created.
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { failureMessage } from "../../api/client";
import { endpoints, type Endpoints } from "../../api/endpoints";
import type { Component, EditableComponentId, Revision, ValueParseResult } from "../../api/types";
import { formatSi } from "../../lib/format";
import { useAnnouncer } from "../../state/announcer";
import { Button } from "../ui/primitives";

export const PARSE_DEBOUNCE_MS = 300;

type ParseState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "result"; result: ValueParseResult }
  | { kind: "unavailable"; message: string };

interface ValueEditorProps {
  component: Component;
  templateId: Revision["template_id"];
  applying: boolean;
  /** Server-side rejection from creating the revision (shown inline). */
  serverError: string | null;
  onApply: (componentId: EditableComponentId, text: string) => void;
  onCancel: () => void;
  client?: Pick<Endpoints, "parseValue">;
}

export function ValueEditor({ component, templateId, applying, serverError, onApply, onCancel, client = endpoints }: ValueEditorProps) {
  const id = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState(component.display_value ?? "");
  const [parse, setParse] = useState<ParseState>({ kind: "idle" });
  const [touched, setTouched] = useState(false);
  const componentId = component.id as EditableComponentId;
  const { announce } = useAnnouncer();

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  useEffect(() => {
    if (!touched) return;
    const trimmed = text.trim();
    if (!trimmed) {
      setParse({ kind: "idle" });
      return;
    }
    const controller = new AbortController();
    setParse({ kind: "checking" });
    const timer = window.setTimeout(() => {
      client
        .parseValue({ component_id: componentId, text: trimmed, template_id: templateId }, controller.signal)
        .then((result) => {
          if (!controller.signal.aborted) setParse({ kind: "result", result });
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) setParse({ kind: "unavailable", message: failureMessage(error) });
        });
    }, PARSE_DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [text, touched, componentId, templateId, client]);

  const parsedOk = parse.kind === "result" && parse.result.ok;
  const unchanged =
    parse.kind === "result" && parse.result.ok && parse.result.value_si === component.value_si;
  const invalid = parse.kind === "result" && !parse.result.ok;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (applying) return;
    // Apply stays enabled so keyboard and screen-reader users always learn why it can't proceed.
    if (!text.trim()) return announce(`Enter a value for ${component.label}.`);
    if (!touched || unchanged) return announce(`${component.label} already has this value.`);
    if (invalid) return announce(parse.result.message ?? "This value isn’t valid.");
    if (parse.kind === "checking") return announce("Still checking the value. Try again in a moment.");
    onApply(componentId, text.trim());
  };

  // Escape anywhere inside the editor (input or its buttons) cancels the edit.
  const formRef = useRef<HTMLFormElement>(null);
  const cancelRef = useRef(onCancel);
  cancelRef.current = onCancel;
  useEffect(() => {
    const form = formRef.current;
    if (!form) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      cancelRef.current();
    };
    form.addEventListener("keydown", onKeyDown);
    return () => form.removeEventListener("keydown", onKeyDown);
  }, []);

  const hintId = `${id}-hint`;
  const bounds = component.bounds
    ? `Allowed ${formatSi(component.bounds.min_si, component.bounds.unit)} to ${formatSi(component.bounds.max_si, component.bounds.unit)}.`
    : "";

  let message: { tone: "ok" | "error" | "hint"; text: string };
  if (serverError) message = { tone: "error", text: serverError };
  else if (parse.kind === "checking") message = { tone: "hint", text: "Checking…" };
  else if (parse.kind === "unavailable")
    message = { tone: "hint", text: `Couldn’t check this value right now (${parse.message}). The server will validate it when you apply.` };
  else if (invalid) message = { tone: "error", text: parse.result.message ?? "This value isn’t valid." };
  else if (unchanged) message = { tone: "hint", text: `That is the current value (${component.display_value ?? ""}).` };
  else if (parsedOk) message = { tone: "ok", text: `= ${parse.result.display_value ?? ""}` };
  else message = { tone: "hint", text: `Use an SI prefix, e.g. ${component.unit === "farad" ? "220n or 0.22 µF" : "4.7k or 4.7 kΩ"}. ${bounds}` };

  return (
    <form ref={formRef} className="value-editor" onSubmit={submit} aria-label={`Edit ${component.label} value`}>
      <div className="field">
        <label className="field__label" htmlFor={`${id}-input`}>
          New {component.name} value for {component.label}
        </label>
        <div className="row">
          <input
            ref={inputRef}
            id={`${id}-input`}
            className="input num value-editor__input"
            value={text}
            maxLength={32}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={invalid || Boolean(serverError) || undefined}
            aria-describedby={hintId}
            onChange={(e) => {
              setText(e.target.value);
              setTouched(true);
            }}
          />
          <Button type="submit" variant="primary" loading={applying}>
            Apply
          </Button>
          <Button onClick={onCancel} disabled={applying}>
            Cancel
          </Button>
        </div>
        <p
          id={hintId}
          className={message.tone === "error" ? "field__error" : message.tone === "ok" ? "field__ok num" : "field__hint"}
        >
          {message.text}
        </p>
      </div>
    </form>
  );
}
