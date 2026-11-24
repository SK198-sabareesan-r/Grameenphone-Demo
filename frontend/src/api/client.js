import { API } from "./endpoints";

const BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

async function request(path, { method = "GET", body } = {}) {
  const response = await fetch(BASE + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      /* body was not JSON */
    }
    throw new Error(detail);
  }
  return response.json();
}

export function panelStreamUrl() {
  const origin = BASE || window.location.origin;
  return origin.replace(/^http/, "ws") + API.panelStream;
}

export const api = {
  softphoneToken: () => request(API.softphoneToken),
  endCall: () => request(API.endCall, { method: "POST" }),
  addTranscriptLine: (speaker, text) =>
    request(API.transcript, { method: "POST", body: { speaker, text } }),
  analyze: () => request(API.analyze, { method: "POST" }),
  runAnswer: () => request(API.answers, { method: "POST" }),
  generateSummary: () => request(API.summary, { method: "POST" }),
  ask: (question) => request(API.ask, { method: "POST", body: { question } }),
  useSuggestion: (id) => request(API.useSuggestion(id), { method: "POST" }),
  ticketCategories: () => request(API.ticketCategories),
  createTicket: (ticket) => request(API.tickets, { method: "POST", body: ticket }),
  dashboard: () => request(API.dashboard),
  customers: () => request(API.customers),
  activeCustomer: () => request(API.activeCustomer),
  setActiveCustomer: (customerId) =>
    request(API.activeCustomer, { method: "PUT", body: { customer_id: customerId } }),
  resetData: () => request(API.resetData, { method: "POST" }),
};
