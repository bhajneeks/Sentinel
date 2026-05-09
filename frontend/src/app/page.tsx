import Link from "next/link";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default async function Home() {
  const res = await fetch(`${API_URL}/api/hello`);
  const { message } = (await res.json()) as { message: string };

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-zinc-50 p-8 font-sans dark:bg-black">
      <h1 className="text-3xl font-semibold text-black dark:text-zinc-50">
        Next.js + FastAPI
      </h1>
      <p className="rounded-md bg-white px-4 py-2 text-lg text-zinc-700 shadow-sm dark:bg-zinc-900 dark:text-zinc-300">
        Backend says: <span className="font-mono">{message}</span>
      </p>
      <Link
        href="/dashboard"
        className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-50 shadow-sm hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
      >
        Open live iMessage dashboard →
      </Link>
      <Link
        href="/monitor"
        className="rounded-md bg-violet-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-violet-700"
      >
        🛰️ Always-On Brand Monitor →
      </Link>
    </main>
  );
}
