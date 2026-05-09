import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const PLATFORM = v.union(
  v.literal("reddit"),
  v.literal("x"),
  v.literal("linkedin"),
);

export const RUN_STATUS = v.union(
  v.literal("queued"),
  v.literal("running"),
  v.literal("complete"),
  v.literal("error"),
);

export const SESSION_STATUS = v.union(
  v.literal("starting"),
  v.literal("running"),
  v.literal("complete"),
  v.literal("error"),
);

export const STEP_KIND = v.union(
  v.literal("goto"),
  v.literal("action"),
  v.literal("thinking"),
  v.literal("extract"),
  v.literal("error"),
);

export default defineSchema({
  agentRuns: defineTable({
    participant: v.string(),
    company: v.string(),
    link: v.string(),
    status: RUN_STATUS,
    startedAt: v.number(),
    completedAt: v.optional(v.number()),
    error: v.optional(v.string()),
  })
    .index("by_participant", ["participant"])
    .index("by_status", ["status"]),

  scraperSessions: defineTable({
    runId: v.optional(v.id("agentRuns")),
    platform: PLATFORM,
    query: v.string(),
    status: SESSION_STATUS,
    startedAt: v.number(),
    completedAt: v.optional(v.number()),
    error: v.optional(v.string()),
    /** Whether this session is using a real browser (counts toward 25-concurrency cap). */
    browserBacked: v.boolean(),
    /** Browser-Use Cloud iframe URL when this session is a cloud scroll. */
    liveUrl: v.optional(v.string()),
    /** Opaque Browser-Use Cloud session id (for cross-reference / debugging). */
    cloudSessionId: v.optional(v.string()),
  })
    .index("by_run", ["runId"])
    .index("by_status", ["status"])
    .index("by_browser_status", ["browserBacked", "status"]),

  scraperSteps: defineTable({
    sessionId: v.id("scraperSessions"),
    runId: v.id("agentRuns"),
    ts: v.number(),
    kind: STEP_KIND,
    url: v.optional(v.string()),
    title: v.optional(v.string()),
    text: v.optional(v.string()),
    screenshot: v.optional(v.id("_storage")),
  })
    .index("by_session", ["sessionId"])
    .index("by_run_ts", ["runId", "ts"]),

  mentions: defineTable({
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
    foundAt: v.number(),
  })
    .index("by_run", ["runId"])
    .index("by_session", ["sessionId"])
    .index("by_run_foundAt", ["runId", "foundAt"]),
});
