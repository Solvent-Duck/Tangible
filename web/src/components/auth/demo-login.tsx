"use client";

import { FlaskConical } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/input";
import { storeTenant } from "@/lib/auth-context";
import { type DemoSession, setDemoSession } from "@/lib/demo-session";

/** The shared demo login: a username and password handed out to testers (seeded data only). */
export function DemoLoginForm() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/demo-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
        cache: "no-store",
      });
      if (!res.ok) {
        setError(res.status === 401 ? "Wrong username or password." : `Sign-in failed (${res.status}).`);
        return;
      }
      storeTenant(null);
      setDemoSession((await res.json()) as DemoSession);
      router.push("/app/dashboard");
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="w-full max-w-[400px] p-5">
      <div className="mb-3 flex items-center gap-2 text-[14px] font-semibold">
        <FlaskConical className="size-4 text-brand" /> Demo access
      </div>
      <p className="mb-4 text-[13px] text-muted">
        Were you given a demo username and password? Sign in here to try the credit-admin view on sample data.
      </p>
      <form onSubmit={submit} className="space-y-3">
        <Field label="Username" htmlFor="demo-username">
          <Input
            id="demo-username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </Field>
        <Field label="Password" htmlFor="demo-password" error={error}>
          <Input
            id="demo-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        <Button type="submit" variant="primary" className="w-full" disabled={busy}>
          {busy ? "Signing in…" : "Sign in to the demo"}
        </Button>
      </form>
    </Card>
  );
}
