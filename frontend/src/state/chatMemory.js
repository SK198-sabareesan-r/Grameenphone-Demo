// Short-term memory of the Ask Copilot chat. Kept free of React so it can be tested with `node --test`.
//
// The whole chat lives in the page (React state): nothing is written to the browser's storage and the
// server keeps nothing, so refreshing the page clears it. Each question carries the last few messages,
// and the backend uses them to rewrite a follow-up ("and the price?") into a standalone question.

// How many earlier messages are sent with a question: the last three exchanges.
export const MEMORY_MESSAGES = 6;

// A message is { id, role: "user" | "assistant", content, sources?, searchedFor?, failed? }.
// The history for the next question: the last few messages that are real turns. A failed attempt is
// shown on screen but is not part of the conversation.
export function historyForRequest(messages, limit = MEMORY_MESSAGES) {
  return messages
    .filter((m) => !m.failed && typeof m.content === "string" && m.content.trim())
    .slice(-limit)
    .map((m) => ({ role: m.role, content: m.content.trim() }));
}

export function userMessage(id, content) {
  return { id, role: "user", content };
}

export function assistantMessage(id, reply) {
  return {
    id,
    role: "assistant",
    content: reply.answer,
    sources: reply.sources || [],
    searchedFor: reply.searched_for || "",
  };
}

export function failedMessage(id, text) {
  return { id, role: "assistant", content: text, failed: true };
}
