import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { RUN_STATUS } from "./schema";

export const create = mutation({
  args: {
    participant: v.string(),
    company: v.string(),
    link: v.string(),
  },
  handler: async (ctx, { participant, company, link }) => {
    return await ctx.db.insert("agentRuns", {
      participant,
      company,
      link,
      status: "running",
      startedAt: Date.now(),
    });
  },
});

export const finish = mutation({
  args: {
    runId: v.id("agentRuns"),
    status: RUN_STATUS,
    error: v.optional(v.string()),
  },
  handler: async (ctx, { runId, status, error }) => {
    await ctx.db.patch(runId, {
      status,
      completedAt: Date.now(),
      ...(error ? { error } : {}),
    });
  },
});

export const byParticipant = query({
  args: { participant: v.string(), limit: v.optional(v.number()) },
  handler: async (ctx, { participant, limit }) => {
    return await ctx.db
      .query("agentRuns")
      .withIndex("by_participant", (q) => q.eq("participant", participant))
      .order("desc")
      .take(limit ?? 25);
  },
});

export const latestByParticipant = query({
  args: { participant: v.string() },
  handler: async (ctx, { participant }) => {
    return await ctx.db
      .query("agentRuns")
      .withIndex("by_participant", (q) => q.eq("participant", participant))
      .order("desc")
      .first();
  },
});

export const get = query({
  args: { runId: v.id("agentRuns") },
  handler: async (ctx, { runId }) => ctx.db.get(runId),
});
