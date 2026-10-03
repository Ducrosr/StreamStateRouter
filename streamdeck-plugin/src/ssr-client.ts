import streamDeck from "@elgato/streamdeck";
import { waitForCommandStatus } from "./command-tracking";

export type SSRStatus = {
  paused?: boolean;
  foreground?: string;
  rule?: string;
  state?: Record<string, string>;
  obs_connected?: boolean;
  media?: {
    running?: boolean;
    state?: {
      connected?: boolean;
      stale?: boolean;
      playback_state?: string;
      title?: string;
    };
  };
};

export type SSRConnectionSettings = {
  port?: number;
  token?: string;
};

async function connectionSettings(): Promise<{ baseUrl: string; token: string }> {
  const settings = await streamDeck.settings.getGlobalSettings<SSRConnectionSettings>();
  const rawPort = Number(settings.port ?? 8765);
  const port = Number.isInteger(rawPort) && rawPort >= 1 && rawPort <= 65535 ? rawPort : 8765;
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    token: String(settings.token ?? ""),
  };
}

export async function saveConnectionSettings(settings: SSRConnectionSettings): Promise<void> {
  const port = Number(settings.port ?? 8765);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Port SSR invalide");
  }
  await streamDeck.settings.setGlobalSettings({
    port,
    token: String(settings.token ?? ""),
  });
}

class SSRRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "SSRRequestError";
    this.status = status;
  }
}

async function request(path: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
  const connection = await connectionSettings();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (connection.token) {
    headers.Authorization = `Bearer ${connection.token}`;
  }
  const response = await fetch(`${connection.baseUrl}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(1500),
  });
  const payload = await response.json() as Record<string, unknown>;
  if (!response.ok || payload.ok === false) {
    throw new SSRRequestError(
      String(payload.error ?? `HTTP ${response.status}`),
      response.status,
    );
  }
  return payload;
}

async function waitForCommand(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const requestId = String(payload.request_id ?? "");
  if (!requestId) {
    return payload;
  }

  const status = await waitForCommandStatus(
    requestId,
    (id) => request(`/requests/${encodeURIComponent(id)}`),
    {
      pollIntervalMs: 100,
      retryIntervalMs: 250,
      isDefinitelyMissing: (error) =>
        error instanceof SSRRequestError && error.status === 404,
    },
  );
  const state = String(status.status ?? "");
  if (state === "completed") {
    return status;
  }
  throw new Error(
    String(
      status.error ??
      (state === "expired"
        ? "Résultat de commande SSR expiré"
        : state === "uncertain"
          ? "Résultat incertain : ne répétez pas immédiatement la commande"
          : "Commande SSR échouée")
    )
  );
}

async function command(path: string, body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return waitForCommand(await request(path, body));
}

export const ssrClient = {
  status: async () => request("/status") as Promise<SSRStatus>,
  pause: async (paused: boolean) => request("/pause", { paused }),
  auto: async () => request("/auto", {}),
  reapply: async () => command("/reapply"),
  setControlVariable: async (name: string, value: string) =>
    command("/control/set", { name, value }),
  applyLayout: async (name: string) => command("/layout/apply", { name }),
  previewLayout: async (name: string) => command("/layout/preview", { name }),
  cancelPreview: async () => command("/layout/cancel-preview"),
  undoLayout: async () => command("/layout/undo"),
  mediaPlay: async () => command("/media/play"),
  mediaPause: async () => command("/media/pause"),
  mediaStop: async () => command("/media/stop"),
  mediaNext: async () => command("/media/next"),
  mediaPrevious: async () => command("/media/previous"),
};
