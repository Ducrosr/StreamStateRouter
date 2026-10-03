export type CommandStatusPayload = Record<string, unknown>;

export type CommandStatusFetcher = (
  requestId: string,
) => Promise<CommandStatusPayload>;

export type CommandTrackingOptions = {
  pollIntervalMs?: number;
  retryIntervalMs?: number;
  sleep?: (milliseconds: number) => Promise<void>;
  isDefinitelyMissing?: (error: unknown) => boolean;
};

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "expired",
  "uncertain",
]);

const defaultSleep = async (milliseconds: number): Promise<void> => {
  await new Promise((resolve) => setTimeout(resolve, milliseconds));
};

export async function waitForCommandStatus(
  requestId: string,
  fetchStatus: CommandStatusFetcher,
  options: CommandTrackingOptions = {},
): Promise<CommandStatusPayload> {
  const id = String(requestId ?? "").trim();
  if (!id) {
    throw new Error("request_id SSR requis");
  }

  const pollIntervalMs = Math.max(10, Number(options.pollIntervalMs ?? 100));
  const retryIntervalMs = Math.max(10, Number(options.retryIntervalMs ?? 250));
  const sleep = options.sleep ?? defaultSleep;
  const isDefinitelyMissing = options.isDefinitelyMissing ?? (() => false);

  while (true) {
    let payload: CommandStatusPayload;
    try {
      payload = await fetchStatus(id);
    } catch (error) {
      if (isDefinitelyMissing(error)) {
        throw error;
      }
      // A lost/failed status read does not prove that the accepted command
      // stopped. Keep the same request_id and retry the GET only; never
      // re-emit the original command.
      await sleep(retryIntervalMs);
      continue;
    }

    const status = String(payload.status ?? "").trim().toLowerCase();
    if (TERMINAL_STATUSES.has(status)) {
      return payload;
    }
    await sleep(pollIntervalMs);
  }
}
