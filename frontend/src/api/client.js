import { API } from "./endpoints";

const BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
// Kept for this browser tab only; cleared on sign-out or when the backend rejects it.
const TOKEN_KEY = "gp-copilot-token";

let token = sessionStorage.getItem(TOKEN_KEY) || "";
let onUnauthorized = () => {};

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export function setToken(value) {
  token = value || "";
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

export function hasToken() {
  return Boolean(token);
}

// Called when a signed-in request gets 401 (expired or revoked session).
export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler || (() => {});
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(BASE + path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      /* body was not JSON */
    }
    if (response.status === 401 && auth) onUnauthorized();
    throw new ApiError(detail, response.status);
  }
  return response.json();
}

// The panel WebSocket authenticates with a one-time ticket (browsers cannot send headers on it).
export function panelStreamUrl(ticket) {
  const origin = BASE || window.location.origin;
  return `${origin.replace(/^http/, "ws")}${API.panelStream}?ticket=${encodeURIComponent(ticket)}`;
}

export const api = {
  login: (username, password) =>
    request(API.login, { method: "POST", body: { username, password }, auth: false }),
  me: () => request(API.me),
  wsTicket: () => request(API.wsTicket, { method: "POST" }),
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
