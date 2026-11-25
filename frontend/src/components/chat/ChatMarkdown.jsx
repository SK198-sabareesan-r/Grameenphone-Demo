import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Renders an answer as Markdown. Raw HTML in the text is not rendered (react-markdown ignores it), and
// unsafe link addresses such as javascript: are removed by react-markdown's default URL check.
const COMPONENTS = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
  // A wide table scrolls sideways inside the bubble instead of stretching it.
  table: ({ node, ...props }) => (
    <div className="chat-md-table">
      <table {...props} />
    </div>
  ),
};

export default function ChatMarkdown({ text }) {
  return (
    <div className="chat-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
