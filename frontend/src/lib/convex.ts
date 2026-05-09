"use client";

import { ConvexReactClient } from "convex/react";

const url = process.env.NEXT_PUBLIC_CONVEX_URL;

if (!url) {
  throw new Error(
    "NEXT_PUBLIC_CONVEX_URL is not set — add it to frontend/.env.local",
  );
}

export const convex = new ConvexReactClient(url);
