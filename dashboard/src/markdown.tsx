/**
 * P171: a small Markdown renderer for the Docs view.
 *
 * Builds React elements directly — no HTML string is ever injected, so nothing
 * in a document can run in the dashboard, which sits next to the agents' keys.
 * Covers what Emporia's docs use: headings, paragraphs, lists (nested by
 * indent), fenced code, tables, blockquotes, rules, and inline code, bold,
 * italic and links. Links to another bundled doc open in the Docs view; web
 * links open in a new tab; anything else (a source path) is shown as text.
 */
import type { ReactNode } from "react";

export type LinkTarget =
  | { kind: "doc"; id: string; anchor?: string }
  | { kind: "anchor"; anchor: string }
  | { kind: "web"; href: string }
  | { kind: "none" };

export interface RenderOptions {
  /** How a link's href resolves: another doc, this page, the web, or nothing. */
  resolveLink: (href: string) => LinkTarget;
  onDocLink: (id: string, anchor?: string) => void;
  onAnchor: (anchor: string) => void;
}

/** GitHub-style heading slug, so `#some-heading` links land. */
export function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/`/g, "")
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .trim()
    .replace(/\s+/g, "-");
}

// -- inline -------------------------------------------------------------------

const INLINE = /(`+)([\s\S]*?)\1|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\s][^*]*)\*|(?<![\w])_([^_\s][^_]*)_(?![\w])|\[([^\]]+)\]\(([^)\s]+)\)/g;

export function renderInline(text: string, opts: RenderOptions, keyBase = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const key = `${keyBase}-${n++}`;
    if (m[1]) out.push(<code key={key}>{m[2]}</code>);
    else if (m[3] || m[4]) out.push(<strong key={key}>{renderInline(m[3] || m[4], opts, key)}</strong>);
    else if (m[5] || m[6]) out.push(<em key={key}>{renderInline(m[5] || m[6], opts, key)}</em>);
    else if (m[7]) out.push(renderLink(m[7], m[8], opts, key));
    last = at + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderLink(label: string, href: string, opts: RenderOptions, key: string): ReactNode {
  const target = opts.resolveLink(href);
  const children = renderInline(label, opts, key);
  if (target.kind === "web") {
    return <a key={key} href={target.href} target="_blank" rel="noopener noreferrer">{children}</a>;
  }
  if (target.kind === "doc" || target.kind === "anchor") {
    const go = (e: { preventDefault: () => void }) => {
      e.preventDefault();
      if (target.kind === "doc") opts.onDocLink(target.id, target.anchor);
      else opts.onAnchor(target.anchor);
    };
    return <a key={key} href="#" onClick={go} className="e-doc-link">{children}</a>;
  }
  // A path to a file the dashboard does not carry: say where it is, plainly.
  return <span key={key} className="e-doc-path" title={href}>{children}</span>;
}

// -- blocks -------------------------------------------------------------------

interface ListItem { text: string; children: string[] }

function splitRow(line: string): string[] {
  const body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  // `\|` inside a cell is a literal bar, not a column break.
  return body.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

export function renderMarkdown(md: string, opts: RenderOptions): ReactNode[] {
  const lines = md.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  const key = () => `b-${k++}`;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    const fence = line.match(/^(\s*)(```+|~~~+)\s*([\w-]*)/);
    if (fence) {
      const close = fence[2];
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      out.push(<pre key={key()} className="e-doc-code"><code>{body.join("\n")}</code></pre>);
      continue;
    }

    const h = line.match(/^(#{1,6})\s+(.*?)\s*#*$/);
    if (h) {
      const level = h[1].length;
      const text = h[2];
      const Tag = (`h${Math.min(level + 1, 6)}`) as "h2";   // the doc title sits under the view's own heading
      out.push(<Tag key={key()} id={slugify(text)} className={`e-doc-h e-doc-h${level}`}>{renderInline(text, opts)}</Tag>);
      i++;
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push(<hr key={key()} />); i++; continue; }

    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(splitRow(lines[i++]));
      out.push(
        <div key={key()} className="e-doc-table-wrap">
          <table className="e-doc-table">
            <thead><tr>{head.map((c, j) => <th key={j}>{renderInline(c, opts, `th${j}`)}</th>)}</tr></thead>
            <tbody>{rows.map((r, ri) => (
              <tr key={ri}>{head.map((_, j) => <td key={j}>{renderInline(r[j] ?? "", opts, `td${ri}-${j}`)}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (line.startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) body.push(lines[i++].replace(/^>\s?/, ""));
      out.push(<blockquote key={key()} className="e-doc-quote">{renderMarkdown(body.join("\n"), opts)}</blockquote>);
      continue;
    }

    const li = line.match(LIST_ITEM);
    if (li) {
      const indent = li[1].length;
      const ordered = /\d/.test(li[2]);
      const items: ListItem[] = [];
      while (i < lines.length) {
        const cur = lines[i];
        const m = cur.match(LIST_ITEM);
        if (m && m[1].length === indent) {
          items.push({ text: m[3], children: [] });
          i++;
        } else if (cur.trim() && (cur.match(/^\s*/)?.[0].length ?? 0) > indent && items.length) {
          const last = items[items.length - 1];
          // A deeper list or a wrapped line belongs to the item above.
          if (LIST_ITEM.test(cur) || last.children.length) last.children.push(cur);
          else last.text += " " + cur.trim();
          i++;
        } else if (!cur.trim() && i + 1 < lines.length && (lines[i + 1].match(/^\s*/)?.[0].length ?? 0) > indent
                   && items.length) {
          items[items.length - 1].children.push("");
          i++;
        } else break;
      }
      const ListTag = ordered ? "ol" : "ul";
      out.push(
        <ListTag key={key()} className="e-doc-list">
          {items.map((it, j) => {
            const minIndent = Math.min(...it.children.filter((c) => c.trim())
              .map((c) => c.match(/^\s*/)?.[0].length ?? 0));
            const nested = it.children.map((c) => c.slice(Number.isFinite(minIndent) ? minIndent : 0)).join("\n");
            return (
              <li key={j}>
                {renderInline(it.text, opts, `li${k}-${j}`)}
                {nested.trim() ? renderMarkdown(nested, opts) : null}
              </li>
            );
          })}
        </ListTag>,
      );
      continue;
    }

    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|~~~|>|\s*\|)/.test(lines[i])
           && !LIST_ITEM.test(lines[i])) {
      para.push(lines[i++].trim());
    }
    if (!para.length) { para.push(lines[i++].trim()); }
    out.push(<p key={key()}>{renderInline(para.join(" "), opts)}</p>);
  }
  return out;
}
