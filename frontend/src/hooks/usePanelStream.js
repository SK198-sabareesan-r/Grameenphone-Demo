import { useEffect, useReducer, useState } from "react";
import { panelStreamUrl } from "../api/client";
import { callReducer, initialCallState } from "../state/callReducer";

const RECONNECT_MS = 2000;

// Keeps one WebSocket open to the backend and folds every event into the call state.
export function usePanelStream() {
  const [call, dispatch] = useReducer(callReducer, initialCallState);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let socket;
    let timer;
    let stopped = false;

    const open = () => {
      socket = new WebSocket(panelStreamUrl());
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => dispatch(JSON.parse(event.data));
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) timer = setTimeout(open, RECONNECT_MS);
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
