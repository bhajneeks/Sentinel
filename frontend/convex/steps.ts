import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { STEP_KIND } from "./schema";

export const add = mutation({
  args: {
    sessionId: v.id("scraperSessions"),
    runId: v.id("agentRuns"),
    kind: STEP_KIND,
    url: v.optional(v.string()),
    title: v.optional(v.string()),
    text: v.optional(v.string()),
    screenshot: v.optional(v.id("_storage")),
  },
  handler: async (ctx, args) => {
    const { sessionId, runId, kind, url, title, text, screenshot } = args;
    return await ctx.db.insert("scraperSteps", {
      sessionId,
      runId,
      ts: Date.now(),
      kind,
      ...(url ? { url } : {}),
      ...(title ? { title } : {}),
      ...(text ? { text } : {}),
      ...(screenshot ? { screenshot } : {}),
    });
  },
});

export const byRun = query({
  args: { runId: v.id("agentRuns"), limit: v.optional(v.number()) },
  handler: async (ctx, { runId, limit }) => {
    const steps = await ctx.db
      .query("scraperSteps")
      .withIndex("by_run_ts", (q) => q.eq("runId", runId))
      .order("desc")
      .take(limit ?? 100);

    return await Promise.all(
      steps.map(async (s) => ({
        ...s,
        screenshotUrl: s.screenshot
          ? await ctx.storage.getUrl(s.screenshot)
          : null,
      })),
    );
  },
});

export const bySession = query({
  args: { sessionId: v.id("scraperSessions") },
  handler: async (ctx, { sessionId }) => {
    const steps = await ctx.db
      .query("scraperSteps")
      .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
      .order("asc")
      .collect();
    return await Promise.all(
      steps.map(async (s) => ({
        ...s,
        screenshotUrl: s.screenshot
          ? await ctx.storage.getUrl(s.screenshot)
          : null,
      })),
    );
  },
});
