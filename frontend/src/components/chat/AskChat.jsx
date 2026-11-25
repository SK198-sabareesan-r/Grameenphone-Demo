import { useRef, useState } from "react";
import { api } from "../../api/client";
import { assistantMessage, failedMessage, historyForRequest, userMessage } from "../../state/chatMemory";
import ChatPopup from "./ChatPopup";

function BotIcon() {
  return (
    <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="4" y="8" width="16" height="11" rx="3" />
      <path d="M12 8V5" />
      <circle cx="12" cy="4" r="1" />
      <circle cx="9" cy="13" r="1.2" fill="currentColor" stroke="none" />
      <circle cx="15" cy="13" r="1.2" fill="currentColor" stroke="none" />
      <path d="M9.5 16.5h5" />
      <path d="M2 12v3M22 12v3" />
    </svg>
  );
}

// Ask Copilot as a bot button in the bottom right corner that opens a chat window.
//
// The chat is kept here, in the page: closing the window keeps it, switching tabs keeps it, and refreshing
// the page clears it. It is never written to the browser storage or to the server. Each question is sent
// with the last few messages so a follow-up can be understood.
export default function AskChat() {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [pending, setPending] = useState(false);
  const nextId = useRef(1);

  async function send(text) {
    const history = historyForRequest(messages); // the chat before this question
    const userId = nextId.current++;
    setMessages((current) => [...current, userMessage(userId, text)]);
    setPending(true);
    try {
      const reply = await api.ask(text, history);
      setMessages((current) => [...current, assistantMessage(nextId.current++, reply)]);
    } catch (error) {
      setMessages((current) => [...current, failedMessage(nextId.current++, error.message || "Ask Copilot could not answer.")]);
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      {open && (
        <ChatPopup
          messages={messages}
          pending={pending}
          onSend={send}
          onNewChat={() => setMessages([])}
          onClose={() => setOpen(false)}
        />
      )}
      <button
        type="button"
        className={`bot-fab ${open ? "open" : ""}`}
        onClick={() => setOpen(!open)}
        aria-label="Ask Copilot"
        aria-expanded={open}
        title="Ask Copilot"
      >
        <BotIcon />
      </button>
    </>
  );
}
