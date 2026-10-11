"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Toaster } from "sonner";

import { AUTH_MODE } from "@/lib/auth-context";
import { ClerkAuthBridge } from "@/lib/clerk-auth";
import { DevAuthProvider } from "@/lib/dev-auth";

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { refetchOnWindowFocus: false, retry: 1 },
        },
      }),
  );
  const Auth = AUTH_MODE === "clerk" ? ClerkAuthBridge : DevAuthProvider;
  return (
    <QueryClientProvider client={client}>
      <Auth>{children}</Auth>
      <Toaster
        position="bottom-right"
        toastOptions={{
          classNames: {
            toast: "!bg-surface !text-ink !border-line !shadow-pop !rounded-xl",
            description: "!text-muted",
          },
        }}
      />
    </QueryClientProvider>
  );
}
