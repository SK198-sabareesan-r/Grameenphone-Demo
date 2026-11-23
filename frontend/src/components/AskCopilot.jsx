import { useState } from "react";
import { api } from "../api/client";

export default function AskCopilot() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    if (question.trim().length < 2) return;
    setBusy(true);
    setError("");
    try {
      setResult(await api.ask(question.trim()));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2>Ask Copilot</h2>
      <form className="ask-form" onSubmit={submit}>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about packs, roaming, SIM, bills…"
        />
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "…" : "Ask"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {result && (
        <div className="ask-result">
          <p className="answer">{result.answer}</p>
          {result.sources.length > 0 && (
            <p className="sources small">
              Source:{" "}
              {result.sources.map((s) => (
                <a key={s.id} href={s.source_url} target="_blank" rel="noreferrer">
                  {s.title}
                  {s.type === "mock" ? " (mock)" : ""}
                </a>
              ))}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
