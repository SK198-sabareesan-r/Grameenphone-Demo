import { useEffect, useReducer, useState } from "react";
import { api, panelStreamUrl } from "../api/client";
import { callReducer, initialCallState } from "../state/callReducer";

const RECONNECT_MS = 2000;

// Keeps one WebSocket open to the backend and folds every event into the call state.
// Each connection uses a fresh one-time ticket. If the ticket request is refused (401), the session
// is over: the client signs out and this hook stops reconnecting.
export function usePanelStream() {
  const [call, dispatch] = useReducer(callReducer, initialCallState);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let socket;
    let timer;
    let stopped = false;

    const retry = () => {
      if (!stopped) timer = setTimeout(open, RECONNECT_MS);
    };

    const open = async () => {
      let ticket;
      try {
        ({ ticket } = await api.wsTicket());
      } catch (error) {
        if (error.status !== 401) retry();
        return;
      }
      if (stopped) return;
      socket = new WebSocket(panelStreamUrl(ticket));
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        try {
          dispatch(JSON.parse(event.data));
        } catch {
          /* ignore a malformed message */
        }
      };
      // Close code 4401 means the ticket was refused; the next attempt asks for a new one, which
      // also re-checks the session.
      socket.onclose = () => {
        setConnected(false);
        retry();
      };
    };

    open();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return { call, connected };
}
