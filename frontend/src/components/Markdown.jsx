import ReactMarkdown from "react-markdown";

// USSD codes such as *121*6*6*3# contain asterisks that Markdown reads as italics.
// Wrap them in backticks so they show exactly as written.
function protectUssd(text) {
  return text.replace(/(?<![`*\d])\*\d[\d*]*#/g, (code) => `\`${code}\``);
}

// Renders AI answer text as Markdown (bold, lists, etc.).
export default function Markdown({ children }) {
  return (
    <div className="answer answer-md">
      <ReactMarkdown>{protectUssd(children || "")}</ReactMarkdown>
    </div>
  );
}
