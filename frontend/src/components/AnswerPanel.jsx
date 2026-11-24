import { useState } from "react";
import { api } from "../api/client";

const CONFIDENCE_LABEL = { high: "Clear match", medium: "Partial match", low: "Weak match" };

function Sources({ sources }) {
  if (!sources.length) return null;
  return (
    <p className="sources small">
      Source:{" "}
      {sources.map((s) => (
        <a key={s.id} href={s.source_url.startsWith("http") ? s.source_url : undefined} target="_blank" rel="noreferrer">
          {s.title}
          {s.type === "mock" ? " (mock)" : ""}
        </a>
      ))}
    </p>
  );
}

function AnswerCard({ entry, latest }) {
  const searchOnly = entry.mode === "search_only";
  return (
    <div className={`answer-card ${latest ? "latest" : ""}`}>
      <p className="answer-question">“{entry.question}”</p>
      {searchOnly ? (
        <p className="muted small">The AI answer is not available. These articles match the question:</p>
      ) : (
        entry.answer && <p className="answer">{entry.answer}</p>
      )}
      {entry.clarifying_question && (
        <p className="answer-clarify">
          <strong>Ask:</strong> {entry.clarifying_question}
        </p>
      )}
      <div className="answer-tags">
        {!searchOnly && (
          <span className={`pill pill-${entry.confidence === "high" ? "ok" : entry.confidence === "medium" ? "warn" : "danger"}`}>
            {CONFIDENCE_LABEL[entry.confidence]}
          </span>
        )}
        {entry.used_customer_data && <span className="pill">Uses customer data</span>}
      </div>
      <Sources sources={entry.sources} />
    </div>
  );
}

export default function AnswerPanel({ answers, pending, callActive, callEnded }) {
  const [error, setError] = useState("");
  const latest = answers[answers.length - 1];
  const earlier = answers.slice(0, -1).reverse();

  async function lookUpNow() {
    setError("");
    try {
      await api.runAnswer();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Suggested answer</h2>
        <button className="btn btn-ghost btn-small" onClick={lookUpNow} disabled={!callActive || callEnded}>
          Look up latest
        </button>
      </div>
      {pending && (
        <p className="looking-up">
          <span className="spinner" aria-hidden="true" /> Looking up “{pending.question}”…
        </p>
      )}
      {!latest && !pending && (
        <p className="empty">When the customer asks something, the answer from the approved knowledge appears here.</p>
      )}
      {latest && <AnswerCard entry={latest} latest />}
      {earlier.length > 0 && (
        <details className="answer-history">
          <summary>Earlier answers ({earlier.length})</summary>
          {earlier.map((e) => (
            <AnswerCard key={e.turn_id} entry={e} />
          ))}
        </details>
      )}
      {error && <p className="error">{error}</p>}
      <p className="muted small">The agent decides. Copilot only suggests.</p>
    </div>
  );
}
