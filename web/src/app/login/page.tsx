"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { supabaseBrowser } from "@/lib/supabase/client";

/** Resolve `next` against this origin and only follow it if it stays here
 * ("//evil.com", "/\\evil.com" and "/\t/evil.com" all resolve elsewhere and fall back to "/"). */
function safeNext(raw: string | null): string {
  if (!raw) return "/";
  try {
    const url = new URL(raw, window.location.origin);
    return url.origin === window.location.origin ? url.pathname + url.search + url.hash : "/";
  } catch {
    return "/";
  }
}

function LoginForm() {
  const router = useRouter();
  const raw = useSearchParams().get("next");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { error } = await supabaseBrowser().auth.signInWithPassword({ email, password });
    setBusy(false);
    if (error) {
      setError(error.message === "Invalid login credentials" ? "That email and password don't match." : error.message);
      return;
    }
    router.replace(safeNext(raw));
    router.refresh();
  }

  return (
    <form onSubmit={submit} className="panel w-full max-w-sm space-y-5 p-7 sm:p-8">
      <div>
        <h1 className="text-3xl leading-tight font-semibold tracking-tight">Sign in to your hisaab</h1>
        <p className="mt-1 text-muted">Your Paytm payments, sorted by day and by person.</p>
      </div>
      <label className="block space-y-1.5">
        <span className="text-sm text-muted">Email</span>
        <input
          className="field"
          type="email"
          name="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label className="block space-y-1.5">
        <span className="text-sm text-muted">Password</span>
        <input
          className="field"
          type="password"
          name="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {error ? (
        <p role="alert" className="text-sm text-rose">
          {error}
        </p>
      ) : null}
      <button type="submit" className="btn btn-primary w-full py-3" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="grid min-h-dvh place-items-center px-4 py-10">
      <div className="flex w-full flex-col items-center gap-10">
        <p className="text-center text-4xl leading-none font-medium tracking-[-0.02em] text-text">
          Hisaab Bolo
        </p>
        <Suspense>
          <LoginForm />
        </Suspense>
      </div>
    </main>
  );
}
