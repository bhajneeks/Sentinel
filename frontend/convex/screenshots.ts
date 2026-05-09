import { mutation } from "./_generated/server";

/** Backend calls this once to get a short-lived upload URL, then PUTs PNG bytes. */
export const generateUploadUrl = mutation({
  args: {},
  handler: async (ctx) => ctx.storage.generateUploadUrl(),
});
