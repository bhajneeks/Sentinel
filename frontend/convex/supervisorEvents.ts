import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { PLATFORM } from "./schema";

const KIND = v.union(
  v.literal("spawn"),
  v.literal("hit"),
  v.literal("revive"),
  v.literal("give_up"),
  v.literal("close"),
);

export const log = mutation({
  args: {
    participant: v.optional(v.string()),
    runId: v.optional(v.id("agentRuns")),
    sessionId: v.optional(v.id("scraperSessions")),
    platform: v.optional(PLATFORM),
    kind: KIND,
    diagnosis: v.optional(v.string()),
    plan: v.optional(v.string()),
    taskBefore: v.optional(v.string()),
    taskAfter: v.optional(v.string()),
    energy: v.optional(v.number()),
    restartCount: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    return await ctx.db.insert("supervisorEvents", { ...args, ts: Date.now() });
  },
});

/** Recent events for a single session — used by the agentic revive flow
 * to read prior diagnoses + outcomes before planning a new strategy. */
export const bySession = query({
  args: {
    sessionId: v.id("scraperSessions"),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, { sessionId, limit }) => {
    return await ctx.db
      .query("supervisorEvents")
      .withIndex("by_session_ts", (q) => q.eq("sessionId", sessionId))
      .order("desc")
      .take(limit ?? 25);
  },
});

/** All events for a participant — for a future dashboard timeline view. */
export const byParticipant = query({
  args: {
    participant: v.string(),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, { participant, limit }) => {
    return await ctx.db
      .query("supervisorEvents")
      .withIndex("by_participant_ts", (q) => q.eq("participant", participant))
      .order("desc")
      .take(limit ?? 100);
  },
});
