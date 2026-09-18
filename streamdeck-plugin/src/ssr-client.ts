const BASE_URL = "http://127.0.0.1:8765";

export type SSRStatus = {
  paused?: boolean;
  foreground?: string;
  rule?: string;
  state?: Record<string, string>;
  obs_connected?: boolean;
};

async function request(path: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
  const response = await fetch(`${BASE_URL}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(1500),
  });
  const payload = await response.json() as Record<string, unknown>;
  if (!response.ok || payload.ok === false) {
    throw new Error(String(payload.error ?? `HTTP ${response.status}`));
  }
  return payload;
}

export const ssrClient = {
  status: async () => request("/status") as Promise<SSRStatus>,
  pause: async (paused: boolean) => request("/pause", { paused }),
  auto: async () => request("/auto", {}),
  reapply: async () => request("/reapply", {}),
  applyLayout: async (name: string) => request("/layout/apply", { name }),
  previewLayout: async (name: string) => request("/layout/preview", { name }),
  cancelPreview: async () => request("/layout/cancel-preview", {}),
  undoLayout: async () => request("/layout/undo", {}),
};
