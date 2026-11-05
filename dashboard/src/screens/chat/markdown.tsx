import type { ReactNode } from "react";

/**
 * Small markdown renderer for model output. It builds React elements only: raw HTML in the text is shown as text,
 * and links keep only http, https and mailto addresses.
 */

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

export function safeHref(url: string): string | null {
  const trimmed = url.trim();
  return SAFE_LINK.test(trimmed) ? trimmed : null;
}

const INLINE = /(`+)([\s\S]+?)\1|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^\s*][\s\S]*?)\*|(?<![A-Za-z0-9_])_([^\s_][\s\S]*?)_(?![A-Za-z0-9_])|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

export function renderInline(text: string, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > last) out.push(text.slice(last, index));
    const key = `${keyPrefix}-${n++}`;
    const [, , code, bold, bold2, strike, em, em2, label, href, bare] = match;
    if (code !== undefined) out.push(<code key={key}>{code.trim()}</code>);
    else if (bold !== undefined || bold2 !== undefined) out.push(<strong key={key}>{renderInline((bold ?? bold2)!, key)}</strong>);
    else if (strike !== undefined) out.push(<del key={key}>{renderInline(strike, key)}</del>);
    else if (em !== undefined || em2 !== undefined) out.push(<em key={key}>{renderInline((em ?? em2)!, key)}</em>);
    else if (label !== undefined && href !== undefined) {
      const safe = safeHref(href);
      out.push(
        safe ? (
          <a key={key} href={safe} target="_blank" rel="noopener noreferrer">
            {renderInline(label, key)}
          </a>
        ) : (
          <span key={key}>{label}</span>
        ),
      );
    } else if (bare !== undefined) {
      out.push(
        <a key={key} href={bare} target="_blank" rel="noopener noreferrer">
          {bare}
        </a>,
      );
    }
    last = index + match[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const ORDERED = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function isBlockStart(line: string): boolean {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || BULLET.test(line) || ORDERED.test(line);
}

export function renderBlocks(source: string): ReactNode[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i]!;
    if (line.trim() === "") {
      i += 1;
      continue;
    }
    const key = `b${k++}`;
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1]!;
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i]!.trim().startsWith(marker)) {
        body.push(lines[i]!);
        i += 1;
      }
      i += 1; // closing fence (missing while streaming)
      out.push(
        <pre key={key} className="md-code" data-lang={fence[2] || undefined}>
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1]!.length;
      const Tag = `h${Math.min(level + 1, 6)}` as "h2" | "h3" | "h4" | "h5" | "h6";
      out.push(<Tag key={key}>{renderInline(heading[2]!, key)}</Tag>);
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push(<hr key={key} />);
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i]!)) {
        body.push(QUOTE.exec(lines[i]!)![1]!);
        i += 1;
      }
      out.push(<blockquote key={key}>{renderBlocks(body.join("\n"))}</blockquote>);
      continue;
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      const ordered = ORDERED.test(line) && !BULLET.test(line);
      const items: string[][] = [];
      const start = ordered ? Number(ORDERED.exec(line)![2]) : 1;
      while (i < lines.length) {
        const current = lines[i]!;
        const bullet = ordered ? ORDERED.exec(current) : BULLET.exec(current);
        if (bullet) {
          items.push([ordered ? bullet[3]! : bullet[2]!]);
          i += 1;
        } else if (current.trim() !== "" && /^\s{2,}\S/.test(current) && items.length > 0) {
          items[items.length - 1]!.push(current.trim());
          i += 1;
        } else break;
      }
      const children = items.map((parts, n) => <li key={n}>{renderInline(parts.join(" "), `${key}-${n}`)}</li>);
      out.push(ordered ? <ol key={key} start={start}>{children}</ol> : <ul key={key}>{children}</ul>);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]!) && lines[i + 1]!.includes("-")) {
      const head = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i]!.trim() !== "" && lines[i]!.includes("|")) {
        rows.push(splitRow(lines[i]!));
        i += 1;
      }
      out.push(
        <div key={key} className="md-table">
          <table>
            <thead>
              <tr>
                {head.map((cell, n) => (
                  <th key={n}>{renderInline(cell, `${key}-h${n}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {head.map((_, n) => (
                    <td key={n}>{renderInline(row[n] ?? "", `${key}-${r}-${n}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    const para: string[] = [line];
    i += 1;
    while (i < lines.length && lines[i]!.trim() !== "" && !isBlockStart(lines[i]!)) {
      para.push(lines[i]!);
      i += 1;
    }
    const nodes: ReactNode[] = [];
    para.forEach((text, n) => {
      if (n > 0) nodes.push(<br key={`br${n}`} />);
      nodes.push(...renderInline(text.trimEnd(), `${key}-${n}`));
    });
    out.push(<p key={key}>{nodes}</p>);
  }
  return out;
}

export function Markdown({ text }: { text: string }) {
  return <div className="md">{renderBlocks(text)}</div>;
}
