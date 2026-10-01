import assert from "node:assert/strict";
import test from "node:test";

import { waitForCommandStatus } from "../src/command-tracking.ts";

test("tracking survives transient status-read failures without losing request identity", async () => {
  const requestIds = [];
  let call = 0;
  const status = await waitForCommandStatus(
    "media-123",
    async (requestId) => {
      requestIds.push(requestId);
      call += 1;
      if (call === 1) {
        throw new Error("status response lost");
      }
      if (call === 2) {
        return { request_id: requestId, status: "running" };
      }
      return { request_id: requestId, status: "completed", success: true };
    },
    {
      sleep: async () => {},
    },
  );

  assert.equal(status.status, "completed");
  assert.deepEqual(requestIds, ["media-123", "media-123", "media-123"]);
});

test("tracking only releases on a proven terminal status", async () => {
  let call = 0;
  const status = await waitForCommandStatus(
    "media-456",
    async () => {
      call += 1;
      if (call < 4) {
        return { status: call === 1 ? "queued" : "running" };
      }
      return { status: "uncertain", success: false };
    },
    {
      sleep: async () => {},
    },
  );

  assert.equal(call, 4);
  assert.equal(status.status, "uncertain");
});

test("a definite missing request may terminate tracking", async () => {
  const missing = new Error("request_not_found");

  await assert.rejects(
    waitForCommandStatus(
      "media-gone",
      async () => {
        throw missing;
      },
      {
        sleep: async () => {},
        isDefinitelyMissing: (error) => error === missing,
      },
    ),
    missing,
  );
});
