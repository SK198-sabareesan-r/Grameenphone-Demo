import { useState } from "react";
import { api } from "../../api/client";

const CONFIDENCE_LABEL = { high: "Clear match", medium: "Partial match", low: "Weak match" };
const CONFIDENCE_PILL = { high: "ok", medium: "warn", low: "danger" };

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
      <p className="answer-question">&ldquo;{entry.question}&rdquo;</p>
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
          <span className={`pill pill-${CONFIDENCE_PILL[entry.confidence] || "danger"}`}>
            {CONFIDENCE_LABEL[entry.confidence]}
          </span>
        )}
        {entry.used_customer_data && <span className="pill">Uses customer data</span>}
      </div>
      <Sources sources={entry.sources} />
    </div>
  );
}

// The answer to the customer's latest question. It is looked up again by itself when the card or the
// location becomes known, so the same answer improves on screen.
export default function AnswerSection({ call }) {
  const [error, setError] = useState("");
  const latest = call.answers[call.answers.length - 1];
  const earlier = call.answers.slice(0, -1).reverse();

  async function lookUpNow() {
    setError("");
    try {
      await api.runAnswer();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="shell-section">
      <div className="card-head">
        <p className="section-label">Suggested answer</p>
        <button className="btn btn-ghost btn-small" onClick={lookUpNow} disabled={!call.active || call.ended}>
          Look up latest
        </button>
      </div>
      {call.answerPending && (
        <p className="looking-up">
          <span className="spinner" aria-hidden="true" /> Looking up &ldquo;{call.answerPending.question}&rdquo;&hellip;
        </p>
      )}
      {!latest && !call.answerPending && (
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
    </section>
  );
}
