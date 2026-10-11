import { SignIn } from "@clerk/nextjs";
import { Suspense } from "react";

import { AuthShell } from "@/components/auth/auth-card";
import { DevPicker } from "@/components/auth/dev-picker";

export const metadata = { title: "Sign in" };

export default function SignInPage() {
  return (
    <AuthShell>
      {process.env.NEXT_PUBLIC_AUTH_MODE === "clerk" ? (
        <SignIn fallbackRedirectUrl="/app" signUpUrl="/sign-up" />
      ) : (
        <Suspense>
          <DevPicker />
        </Suspense>
      )}
    </AuthShell>
  );
}
