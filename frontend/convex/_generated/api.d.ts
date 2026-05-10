/* eslint-disable */
/**
 * Generated `api` utility.
 *
 * THIS CODE IS AUTOMATICALLY GENERATED.
 *
 * To regenerate, run `npx convex dev`.
 * @module
 */

import type * as campaignRuns from "../campaignRuns.js";
import type * as mentions from "../mentions.js";
import type * as runs from "../runs.js";
import type * as screenshots from "../screenshots.js";
import type * as sessions from "../sessions.js";
import type * as steps from "../steps.js";
import type * as supervisorEvents from "../supervisorEvents.js";

import type {
  ApiFromModules,
  FilterApi,
  FunctionReference,
} from "convex/server";

declare const fullApi: ApiFromModules<{
  campaignRuns: typeof campaignRuns;
  mentions: typeof mentions;
  runs: typeof runs;
  screenshots: typeof screenshots;
  sessions: typeof sessions;
  steps: typeof steps;
  supervisorEvents: typeof supervisorEvents;
}>;

/**
 * A utility for referencing Convex functions in your app's public API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = api.myModule.myFunction;
 * ```
 */
export declare const api: FilterApi<
  typeof fullApi,
  FunctionReference<any, "public">
>;

/**
 * A utility for referencing Convex functions in your app's internal API.
 *
 * Usage:
 * ```js
 * const myFunctionReference = internal.myModule.myFunction;
 * ```
 */
export declare const internal: FilterApi<
  typeof fullApi,
  FunctionReference<any, "internal">
>;

export declare const components: {};
