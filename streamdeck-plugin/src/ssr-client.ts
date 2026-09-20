import streamDeck from "@elgato/streamdeck";

export type SSRStatus = {
  paused?: boolean;
  foreground?: string;
  rule?: string;
  state?: Record<string, string>;
  obs_connected?: boolean;
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
    throw new Error(String(payload.error ?? `HTTP ${response.status}`));
  }
  return payload;
}

async function waitForCommand(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const requestId = String(payload.request_id ?? "");
  if (!requestId) {
    return payload;
  }
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const status = await request(`/requests/${encodeURIComponent(requestId)}`);
    const state = String(status.status ?? "");
    if (state === "completed") {
      return status;
    }
    if (state === "failed") {
      throw new Error(String(status.error ?? "Commande SSR échouée"));
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Commande SSR toujours en cours après 10 s (${requestId})`);
}

async function command(path: string, body: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return waitForCommand(await request(path, body));
}

export const ssrClient = {
  status: async () => request("/status") as Promise<SSRStatus>,
  pause: async (paused: boolean) => request("/pause", { paused }),
  auto: async () => request("/auto", {}),
  reapply: async () => command("/reapply"),
  applyLayout: async (name: string) => command("/layout/apply", { name }),
  previewLayout: async (name: string) => command("/layout/preview", { name }),
  cancelPreview: async () => command("/layout/cancel-preview"),
  undoLayout: async () => command("/layout/undo"),
};
