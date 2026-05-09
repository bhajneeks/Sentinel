import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { PLATFORM } from "./schema";

export const add = mutation({
  args: {
    sessionId: v.id("scraperSessions"),
    runId: v.id("agentRuns"),
    platform: PLATFORM,
    postId: v.string(),
    postUrl: v.string(),
    postText: v.string(),
    authorHandle: v.string(),
    authorDisplayName: v.optional(v.string()),
    subreddit: v.optional(v.string()),
    postType: v.optional(v.string()),
    postedAt: v.optional(v.number()),
    likes: v.optional(v.number()),
    reposts: v.optional(v.number()),
    comments: v.optional(v.number()),
    matchedTerms: v.array(v.string()),
  },
  handler: async (ctx, args) => {
    return await ctx.db.insert("mentions", {
      ...args,
      foundAt: Date.now(),
    });
  },
});

export const byRun = query({
  args: { runId: v.id("agentRuns"), limit: v.optional(v.number()) },
  handler: async (ctx, { runId, limit }) => {
    return await ctx.db
      .query("mentions")
      .withIndex("by_run_foundAt", (q) => q.eq("runId", runId))
      .order("desc")
      .take(limit ?? 50);
  },
});

export const byParticipant = query({
  args: { participant: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, { participant, limit }) => {
    const runs = await ctx.db
      .query("agentRuns")
      .withIndex("by_participant", (q) => q.eq("participant", participant))
      .order("desc")
      .take(10);

    const all = (
      await Promise.all(
        runs.map((r) =>
          ctx.db
            .query("mentions")
            .withIndex("by_run", (q) => q.eq("runId", r._id))
            .collect(),
        ),
      )
    ).flat();

    all.sort((a, b) => b.foundAt - a.foundAt);
    return all.slice(0, limit ?? 50);
  },
});
