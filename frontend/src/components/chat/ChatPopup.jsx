import { useEffect, useRef, useState } from "react";
import ChatMarkdown from "./ChatMarkdown";

function Sources({ sources }) {
  if (!sources?.length) return null;
  return (
    <p className="sources small">
      Source:{" "}
      {sources.map((s) => (
        <a key={s.id} href={s.source_url?.startsWith("http") ? s.source_url : undefined} target="_blank" rel="noreferrer noopener">
          {s.title}
          {s.type === "mock" ? " (mock)" : ""}
        </a>
      ))}
    </p>
  );
}

function Message({ message }) {
  if (message.role === "user") {
    return <div className="chat-bubble chat-user">{message.content}</div>;
  }
  if (message.failed) {
    return <div className="chat-bubble chat-assistant chat-failed">{message.content}</div>;
  }
  return (
    <div className="chat-bubble chat-assistant">
      <ChatMarkdown text={message.content} />
      {message.searchedFor && <p className="chat-searched small">Searched for: {message.searchedFor}</p>}
      <Sources sources={message.sources} />
    </div>
  );
}

// The chat window: the messages, a box to type in, and the buttons to start a new chat or close it.
// It only draws what it is given. The chat itself is kept by AskChat.
export default function ChatPopup({ messages, pending, onSend, onNewChat, onClose }) {
  const [draft, setDraft] = useState("");
  const endRef = useRef(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, pending]);

  useEffect(() => {
    const onKey = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function submit(event) {
    event.preventDefault();
    const text = draft.trim();
    if (text.length < 2 || pending) return;
    setDraft("");
    onSend(text);
  }

  // Enter sends, Shift+Enter adds a line. Not while a Bangla or other IME is still composing a word.
  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent?.isComposing) submit(event);
  }

  return (
    <section className="chat-popup" role="dialog" aria-label="Ask Copilot">
      <header className="chat-head">
        <strong>Ask Copilot</strong>
        <span className="chat-head-actions">
          <button type="button" className="chat-head-button" onClick={onNewChat} disabled={pending || messages.length === 0}>
            New chat
          </button>
          <button type="button" className="chat-head-button chat-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </span>
      </header>
      <div className="chat-messages" aria-live="polite">
        {messages.length === 0 && !pending && (
          <p className="empty">
            Ask about packs, roaming, SIM, bills and more. Answers come from the approved knowledge, with the source. You
            can ask in English, Bangla or Banglish, and follow up on an earlier answer.
          </p>
        )}
        {messages.map((m) => (
          <Message key={m.id} message={m} />
        ))}
        {pending && (
          <p className="looking-up">
            <span className="spinner" aria-hidden="true" /> Thinking&hellip;
          </p>
        )}
        <div ref={endRef} />
      </div>
      <form className="chat-input" onSubmit={submit}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          maxLength={500}
          placeholder="Ask a question&hellip;"
          aria-label="Your question"
          autoFocus
        />
        <button className="btn btn-primary" type="submit" disabled={pending || draft.trim().length < 2}>
          Send
        </button>
      </form>
      <p className="chat-footnote muted small">This chat is remembered only until you refresh the page. The agent decides.</p>
    </section>
  );
}
