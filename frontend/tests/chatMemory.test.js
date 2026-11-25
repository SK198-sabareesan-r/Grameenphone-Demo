import assert from "node:assert/strict";
import { test } from "node:test";
import {
  MEMORY_MESSAGES,
  assistantMessage,
  failedMessage,
  historyForRequest,
  userMessage,
} from "../src/state/chatMemory.js";

test("the history is the last few messages, oldest first, as role and content only", () => {
  const messages = Array.from({ length: 10 }, (_, i) =>
    i % 2 === 0 ? userMessage(i, `question ${i}`) : assistantMessage(i, { answer: `answer ${i}`, sources: [{ id: "a" }] })
  );
  const history = historyForRequest(messages);
  assert.equal(history.length, MEMORY_MESSAGES);
  assert.deepEqual(history[0], { role: "user", content: "question 4" });
  assert.deepEqual(history.at(-1), { role: "assistant", content: "answer 9" });
  assert.deepEqual(Object.keys(history[1]).sort(), ["content", "role"]);
});

test("a chat shorter than the limit is sent whole, and an empty chat sends nothing", () => {
  assert.deepEqual(historyForRequest([]), []);
  const two = [userMessage(1, "hello"), assistantMessage(2, { answer: "hi" })];
  assert.equal(historyForRequest(two).length, 2);
});

test("a failed attempt is shown on screen but is not remembered", () => {
  const messages = [userMessage(1, "first"), failedMessage(2, "Ask Copilot failed"), userMessage(3, "second")];
  assert.deepEqual(historyForRequest(messages).map((m) => m.content), ["first", "second"]);
});

test("blank messages and extra spaces are not sent", () => {
  const messages = [userMessage(1, "  padded  "), { id: 2, role: "assistant", content: "   " }];
  assert.deepEqual(historyForRequest(messages), [{ role: "user", content: "padded" }]);
});

test("an assistant message keeps its sources, and what was searched for when the chat changed the question", () => {
  const reply = assistantMessage(5, { answer: "**BDT 1599**", sources: [{ id: "roaming-malaysia-pack" }], searched_for: "Malaysia roaming pack price" });
  assert.equal(reply.searchedFor, "Malaysia roaming pack price");
  assert.deepEqual(reply.sources, [{ id: "roaming-malaysia-pack" }]);
  assert.equal(assistantMessage(6, { answer: "x" }).searchedFor, "");
  assert.deepEqual(assistantMessage(7, { answer: "x" }).sources, []);
});

test("the chat is not written to the browser storage", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../src/state/chatMemory.js", import.meta.url), "utf8");
  const popup = await readFile(new URL("../src/components/chat/AskChat.jsx", import.meta.url), "utf8");
  for (const text of [source, popup]) {
    assert.doesNotMatch(text, /localStorage|sessionStorage|indexedDB/);
  }
});
