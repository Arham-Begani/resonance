// A deliberately small, safe Markdown subset for AI-written text: paragraphs, bullet and numbered
// lists, **bold**, *italic* and `code`. It builds React elements from plain strings, so any HTML in
// the model's text stays literal text (React escapes it); there is no dangerouslySetInnerHTML and
// no links, images or raw HTML. Unmatched markers are shown as typed. Result IDs are resolved to
// workbench labels (readableIdText) before parsing, so raw IDs never reach the learner.
import { Fragment, type ReactNode } from "react";

export type MdInline = { kind: "text"; text: string } | { kind: "strong" | "em" | "code"; children: MdInline[] | string };
export type MdBlock = { kind: "p"; lines: MdInline[][] } | { kind: "ul" | "ol"; items: MdInline[][]; start: number } | { kind: "heading"; inline: MdInline[] };

const BULLET = /^\s{0,3}[-*+•]\s+(.*)$/;
const NUMBERED = /^\s{0,3}(\d{1,3})[.)]\s+(.*)$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;

/** Inline spans. Emphasis needs a non-space just inside each marker, like CommonMark. */
export function parseInline(text: string): MdInline[] {
  const out: MdInline[] = [];
  let buf = "";
  const flush = () => {
    if (buf) out.push({ kind: "text", text: buf });
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i]!;
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ kind: "code", children: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    if ((ch === "*" || ch === "_") && text[i + 1] === ch) {
      const marker = ch + ch;
      const end = findClose(text, marker, i + 2);
      if (end > 0) {
        flush();
        out.push({ kind: "strong", children: parseInline(text.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }
    // Single "*" only: "_" inside snake_case words is far more common than "_italic_" in model text.
    if (ch === "*" && text[i + 1] !== "*") {
      const end = findClose(text, "*", i + 1);
      if (end > 0) {
        flush();
        out.push({ kind: "em", children: parseInline(text.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }
    buf += ch;
    i += 1;
  }
  flush();
  return out;
}

function findClose(text: string, marker: string, from: number): number {
  if (from >= text.length || /\s/.test(text[from]!)) return -1;
  let j = text.indexOf(marker, from);
  while (j > from) {
    // For a single "*", skip over "**" pairs (they belong to a nested strong span).
    if (marker === "*" && text[j + 1] === "*") {
      const inner = text.indexOf("**", j + 2);
      if (inner < 0) return -1;
      j = text.indexOf(marker, inner + 2);
      continue;
    }
    if (!/\s/.test(text[j - 1]!)) return j;
    j = text.indexOf(marker, j + 1);
  }
  return -1;
}

export function parseMarkdown(text: string): MdBlock[] {
  const blocks: MdBlock[] = [];
  let para: MdInline[][] = [];
  let list: Extract<MdBlock, { kind: "ul" | "ol" }> | null = null;
  const endPara = () => {
    if (para.length) blocks.push({ kind: "p", lines: para });
    para = [];
  };
  const endList = () => {
    if (list) blocks.push(list);
    list = null;
  };
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      endPara();
      endList();
      continue;
    }
    const bullet = BULLET.exec(line);
    const numbered = bullet ? null : NUMBERED.exec(line);
    if (bullet || numbered) {
      endPara();
      const kind = bullet ? "ul" : "ol";
      const body = bullet ? bullet[1]! : numbered![2]!;
      if (!list || list.kind !== kind) {
        endList();
        list = { kind, items: [], start: numbered ? Number(numbered[1]) : 1 };
      }
      list.items.push(parseInline(body));
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      endPara();
      endList();
      blocks.push({ kind: "heading", inline: parseInline(heading[1]!) });
      continue;
    }
    if (list && /^\s{2,}\S/.test(raw)) {
      // An indented continuation line belongs to the previous list item.
      const last = list.items[list.items.length - 1]!;
      last.push({ kind: "text", text: " " }, ...parseInline(line.trim()));
      continue;
    }
    endList();
    para.push(parseInline(line.trim()));
  }
  endPara();
  endList();
  return blocks;
}

function renderInline(nodes: MdInline[] | string): ReactNode {
  if (typeof nodes === "string") return nodes;
  return nodes.map((n, i) => {
    switch (n.kind) {
      case "text":
        return <Fragment key={i}>{n.text}</Fragment>;
      case "strong":
        return <strong key={i}>{renderInline(n.children)}</strong>;
      case "em":
        return <em key={i}>{renderInline(n.children)}</em>;
      case "code":
        return (
          <code key={i} className="md__code">
            {renderInline(n.children)}
          </code>
        );
    }
  });
}

/** Renders model-written text as a safe Markdown subset inside a `div.md`. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const blocks = parseMarkdown(text);
  return (
    <div className={className ? `md ${className}` : "md"}>
      {blocks.map((b, i) => {
        if (b.kind === "p") {
          return (
            <p key={i}>
              {b.lines.map((line, j) => (
                <Fragment key={j}>
                  {j > 0 ? <br /> : null}
                  {renderInline(line)}
                </Fragment>
              ))}
            </p>
          );
        }
        if (b.kind === "heading") {
          // Model headings never enter the page outline: a strong paragraph keeps the hierarchy flat.
          return (
            <p key={i} className="md__heading">
              <strong>{renderInline(b.inline)}</strong>
            </p>
          );
        }
        const items = b.items.map((it, j) => <li key={j}>{renderInline(it)}</li>);
        return b.kind === "ul" ? (
          <ul key={i}>{items}</ul>
        ) : (
          <ol key={i} start={b.start !== 1 ? b.start : undefined}>
            {items}
          </ol>
        );
      })}
    </div>
  );
}
