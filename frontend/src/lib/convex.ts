"use client";

import { ConvexReactClient } from "convex/react";

const url = process.env.NEXT_PUBLIC_CONVEX_URL;

export const convex = url ? new ConvexReactClient(url) : null;
