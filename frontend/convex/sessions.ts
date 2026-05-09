import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import { PLATFORM, SESSION_STATUS } from "./schema";

export const start = mutation({
  args: {
    runId: v.id("agentRuns"),
    platform: PLATFORM,
    query: v.string(),
    browserBacked: v.boolean(),
  },
  handler: async (ctx, { runId, platform, query, browserBacked }) => {
    return await ctx.db.insert("scraperSessions", {
      runId,
      platform,
      query,
      status: "running",
      startedAt: Date.now(),
      browserBacked,
    });
  },
});

export const startCloud = mutation({
  args: {
    platform: PLATFORM,
    query: v.string(),
    liveUrl: v.string(),
    cloudSessionId: v.string(),
  },
  handler: async (ctx, { platform, query, liveUrl, cloudSessionId }) => {
    return await ctx.db.insert("scraperSessions", {
      platform,
      query,
      status: "running",
      startedAt: Date.now(),
      browserBacked: true,
      liveUrl,
      cloudSessionId,
    });
  },
});

export const finish = mutation({
  args: {
    sessionId: v.id("scraperSessions"),
    status: SESSION_STATUS,
    error: v.optional(v.string()),
  },
  handler: async (ctx, { sessionId, status, error }) => {
    await ctx.db.patch(sessionId, {
      status,
      completedAt: Date.now(),
      ...(error ? { error } : {}),
    });
  },
});

/** Update the `query` of a running session — used when a supervised
 * agent's task is redirected mid-flight so the dashboard tooltip reflects
 * the current task. */
export const updateQuery = mutation({
  args: {
    sessionId: v.id("scraperSessions"),
    query: v.string(),
  },
  handler: async (ctx, { sessionId, query }) => {
    await ctx.db.patch(sessionId, { query });
  },
});

export const byRun = query({
  args: { runId: v.id("agentRuns") },
  handler: async (ctx, { runId }) => {
    return await ctx.db
      .query("scraperSessions")
      .withIndex("by_run", (q) => q.eq("runId", runId))
      .collect();
  },
});

/** Currently-live cloud sessions (those exposing a live_url). */
export const activeCloud = query({
  args: {},
  handler: async (ctx) => {
    const running = await ctx.db
      .query("scraperSessions")
      .withIndex("by_status", (q) => q.eq("status", "running"))
      .collect();
    const starting = await ctx.db
      .query("scraperSessions")
      .withIndex("by_status", (q) => q.eq("status", "starting"))
      .collect();
    return [...starting, ...running]
      .filter((s) => typeof s.liveUrl === "string" && s.liveUrl.length > 0)
      .map((s) => ({
        _id: s._id,
        platform: s.platform,
        query: s.query,
        liveUrl: s.liveUrl as string,
        startedAt: s.startedAt,
      }));
  },
});

/** How many real-browser sessions are currently in flight. */
export const activeBrowserCount = query({
  args: {},
  handler: async (ctx) => {
    const running = await ctx.db
      .query("scraperSessions")
      .withIndex("by_browser_status", (q) =>
        q.eq("browserBacked", true).eq("status", "running"),
      )
      .collect();
    const starting = await ctx.db
      .query("scraperSessions")
      .withIndex("by_browser_status", (q) =>
        q.eq("browserBacked", true).eq("status", "starting"),
      )
      .collect();
    return running.length + starting.length;
  },
});

/** Mark every running session as errored. Used to recover the concurrency
 * cap after a backend restart leaves orphaned rows. */
export const abortAllRunning = mutation({
  args: { reason: v.optional(v.string()) },
  handler: async (ctx, { reason }) => {
    const running = await ctx.db
      .query("scraperSessions")
      .withIndex("by_status", (q) => q.eq("status", "running"))
      .collect();
    const starting = await ctx.db
      .query("scraperSessions")
      .withIndex("by_status", (q) => q.eq("status", "starting"))
      .collect();
    const all = [...running, ...starting];
    for (const s of all) {
      await ctx.db.patch(s._id, {
        status: "error",
        completedAt: Date.now(),
        error: reason ?? "aborted",
      });
    }
    return all.length;
  },
});
