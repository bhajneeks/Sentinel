import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export const PLATFORM = v.union(
  v.literal("reddit"),
  v.literal("x"),
  v.literal("linkedin"),
  v.literal("tiktok"),
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
    /** iMessage participant (phone number / Apple ID) the session belongs to.
     * Used to scope the dashboard view to the active conversation. */
    participant: v.optional(v.string()),
    /** Self-healing energy level (0-100). Depletes when the agent is
     * stuck / confused / hits a captcha; refills when a judge-approved
     * FOUND lands. At 0, the supervisor diagnoses and redirects. */
    energy: v.optional(v.number()),
    /** How many times this session has been auto-revived. Capped at 3
     * before the session is closed. */
    restartCount: v.optional(v.number()),
    /** Last LLM-generated diagnosis explaining why energy hit 0. */
    lastDiagnosis: v.optional(v.string()),
  })
    .index("by_run", ["runId"])
    .index("by_status", ["status"])
    .index("by_browser_status", ["browserBacked", "status"])
    .index("by_participant_status", ["participant", "status"]),

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

  campaignRuns: defineTable({
    /** iMessage participant whose request triggered the campaign. */
    participant: v.string(),
    /** The original brief Rachel forwarded to the pipeline. */
    brief: v.string(),
    brandName: v.optional(v.string()),
    /** Lifecycle. `subagent` lets the dashboard show fine-grained
     * progress ("running competitor intel...", "synthesizing campaign...",
     * "publishing to notion..."). */
    status: v.union(
      v.literal("building"),
      v.literal("ready"),
      v.literal("error"),
    ),
    subagent: v.optional(v.string()),
    notionPageUrl: v.optional(v.string()),
    campaignName: v.optional(v.string()),
    error: v.optional(v.string()),
    startedAt: v.number(),
    completedAt: v.optional(v.number()),
  })
    .index("by_participant_status", ["participant", "status"])
    .index("by_participant_startedAt", ["participant", "startedAt"]),

  supervisorEvents: defineTable({
    /** iMessage participant the event belongs to (for tab-scoped views). */
    participant: v.optional(v.string()),
    runId: v.optional(v.id("agentRuns")),
    sessionId: v.optional(v.id("scraperSessions")),
    platform: v.optional(PLATFORM),
    /** What happened. */
    kind: v.union(
      v.literal("spawn"),
      v.literal("hit"),
      v.literal("revive"),
      v.literal("give_up"),
      v.literal("close"),
    ),
    /** LLM-generated explanation for revive/give_up; spawn/close use a brief
     * human-readable text. */
    diagnosis: v.optional(v.string()),
    /** LLM-generated rationale for the chosen revive strategy. */
    plan: v.optional(v.string()),
    /** Snapshot of the task text before/after a revive. */
    taskBefore: v.optional(v.string()),
    taskAfter: v.optional(v.string()),
    /** Energy at the moment of the event. */
    energy: v.optional(v.number()),
    restartCount: v.optional(v.number()),
    ts: v.number(),
  })
    .index("by_session_ts", ["sessionId", "ts"])
    .index("by_participant_ts", ["participant", "ts"])
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
