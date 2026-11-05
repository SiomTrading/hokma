import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import "./markdown.css";

/** Links open outside the app; images are not loaded (the panel only talks to the local server). */
const COMPONENTS: Components = {
  a: ({ href, children }) =>
    href && /^(https?:\/\/|mailto:)/i.test(href) ? (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  img: () => null,
};

/**
 * Markdown from the guide, the CHANGELOG and release notes. Raw HTML is never rendered (react-markdown without a
 * raw-HTML plugin turns it into text), so what comes from disk or from the network cannot inject markup.
 */
export function Markdown({ source }: { source: string }) {
  return (
    <div className="md">
      <ReactMarkdown components={COMPONENTS}>{source}</ReactMarkdown>
    </div>
  );
}
