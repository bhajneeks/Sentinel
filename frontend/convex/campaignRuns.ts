import { v } from "convex/values";
import { mutation, query } from "./_generated/server";

const STATUS = v.union(
  v.literal("building"),
  v.literal("ready"),
  v.literal("error"),
);

export const start = mutation({
  args: {
    participant: v.string(),
    brief: v.string(),
    brandName: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    return await ctx.db.insert("campaignRuns", {
      ...args,
      status: "building",
      startedAt: Date.now(),
    });
  },
});

/** Update the in-flight subagent label (e.g. "competitor_intel",
 * "synthesizing", "publishing_to_notion"). Optional; the campaign still
 * works without progress events. */
export const setSubagent = mutation({
  args: {
    runId: v.id("campaignRuns"),
    subagent: v.string(),
  },
  handler: async (ctx, { runId, subagent }) => {
    await ctx.db.patch(runId, { subagent });
  },
});

export const finish = mutation({
  args: {
    runId: v.id("campaignRuns"),
    status: STATUS,
    notionPageUrl: v.optional(v.string()),
    campaignName: v.optional(v.string()),
    error: v.optional(v.string()),
  },
  handler: async (ctx, { runId, status, notionPageUrl, campaignName, error }) => {
    const patch: Record<string, unknown> = {
      status,
      completedAt: Date.now(),
      subagent: undefined,
    };
    if (notionPageUrl) patch.notionPageUrl = notionPageUrl;
    if (campaignName) patch.campaignName = campaignName;
    if (error) patch.error = error;
    await ctx.db.patch(runId, patch);
  },
});

/** Live + most-recent-completed campaigns for a participant. Returns
 * the in-flight build first if any, plus the last `limit` finished ones. */
export const forParticipant = query({
  args: {
    participant: v.string(),
    limit: v.optional(v.number()),
  },
  handler: async (ctx, { participant, limit }) => {
    const all = await ctx.db
      .query("campaignRuns")
      .withIndex("by_participant_startedAt", (q) =>
        q.eq("participant", participant),
      )
      .order("desc")
      .take(limit ?? 5);
    return all;
  },
});

/** Returns the SINGLE active build for a participant, or null. Used by
 * the dashboard pill to show 'building...' state. */
export const activeForParticipant = query({
  args: { participant: v.string() },
  handler: async (ctx, { participant }) => {
    const rows = await ctx.db
      .query("campaignRuns")
      .withIndex("by_participant_status", (q) =>
        q.eq("participant", participant).eq("status", "building"),
      )
      .order("desc")
      .take(1);
    return rows[0] ?? null;
  },
});
