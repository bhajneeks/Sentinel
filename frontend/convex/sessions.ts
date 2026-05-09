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
    participant: v.optional(v.string()),
  },
  handler: async (ctx, { platform, query, liveUrl, cloudSessionId, participant }) => {
    return await ctx.db.insert("scraperSessions", {
      platform,
      query,
      status: "running",
      startedAt: Date.now(),
      browserBacked: true,
      liveUrl,
      cloudSessionId,
      ...(participant ? { participant } : {}),
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

/** Patch the supervised-agent self-healing state on a session. All
 * fields optional — pass only what changed. The harvest loop calls
 * this every ~10s with new energy levels, and on revive with a new
 * diagnosis + bumped restartCount. */
export const patchSupervised = mutation({
  args: {
    sessionId: v.id("scraperSessions"),
    energy: v.optional(v.number()),
    restartCount: v.optional(v.number()),
    lastDiagnosis: v.optional(v.string()),
  },
  handler: async (ctx, { sessionId, energy, restartCount, lastDiagnosis }) => {
    const patch: Record<string, unknown> = {};
    if (energy !== undefined) patch.energy = energy;
    if (restartCount !== undefined) patch.restartCount = restartCount;
    if (lastDiagnosis !== undefined) patch.lastDiagnosis = lastDiagnosis;
    if (Object.keys(patch).length === 0) return;
    await ctx.db.patch(sessionId, patch);
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

/** Currently-live cloud sessions (those exposing a live_url).
 *
 * If `participant` is provided, returns only sessions for that iMessage
 * conversation. Otherwise returns every live cloud session.
 */
export const activeCloud = query({
  args: { participant: v.optional(v.string()) },
  handler: async (ctx, { participant }) => {
    let running, starting;
    if (participant !== undefined) {
      running = await ctx.db
        .query("scraperSessions")
        .withIndex("by_participant_status", (q) =>
          q.eq("participant", participant).eq("status", "running"),
        )
        .collect();
      starting = await ctx.db
        .query("scraperSessions")
        .withIndex("by_participant_status", (q) =>
          q.eq("participant", participant).eq("status", "starting"),
        )
        .collect();
    } else {
      running = await ctx.db
        .query("scraperSessions")
        .withIndex("by_status", (q) => q.eq("status", "running"))
        .collect();
      starting = await ctx.db
        .query("scraperSessions")
        .withIndex("by_status", (q) => q.eq("status", "starting"))
        .collect();
    }
    return [...starting, ...running]
      .filter((s) => typeof s.liveUrl === "string" && s.liveUrl.length > 0)
      .map((s) => ({
        _id: s._id,
        platform: s.platform,
        query: s.query,
        liveUrl: s.liveUrl as string,
        startedAt: s.startedAt,
        participant: s.participant ?? null,
        cloudSessionId: s.cloudSessionId ?? null,
        energy: s.energy ?? null,
        restartCount: s.restartCount ?? 0,
        lastDiagnosis: s.lastDiagnosis ?? null,
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

/** Mark every running session for `participant` as `complete`. Returns the
 * cloudSessionIds the backend should also stop on Browser-Use Cloud. */
export const stopByParticipant = mutation({
  args: { participant: v.string() },
  handler: async (ctx, { participant }) => {
    const running = await ctx.db
      .query("scraperSessions")
      .withIndex("by_participant_status", (q) =>
        q.eq("participant", participant).eq("status", "running"),
      )
      .collect();
    const starting = await ctx.db
      .query("scraperSessions")
      .withIndex("by_participant_status", (q) =>
        q.eq("participant", participant).eq("status", "starting"),
      )
      .collect();
    const all = [...running, ...starting];
    const cloudSessionIds: string[] = [];
    for (const s of all) {
      await ctx.db.patch(s._id, {
        status: "complete",
        completedAt: Date.now(),
      });
      if (s.cloudSessionId) cloudSessionIds.push(s.cloudSessionId);
    }
    return { stopped: all.length, cloudSessionIds };
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
