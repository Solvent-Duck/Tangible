import { SignIn } from "@clerk/nextjs";
import { Suspense } from "react";

import { AuthShell } from "@/components/auth/auth-card";
import { DemoLoginForm } from "@/components/auth/demo-login";
import { DevPicker } from "@/components/auth/dev-picker";

export const metadata = { title: "Sign in" };

export default function SignInPage() {
  return (
    <AuthShell>
      {process.env.NEXT_PUBLIC_AUTH_MODE === "clerk" ? (
        <div className="flex w-full flex-col items-center gap-6">
          <SignIn fallbackRedirectUrl="/app" signUpUrl="/sign-up" />
          {process.env.NEXT_PUBLIC_DEMO_LOGIN === "1" && <DemoLoginForm />}
        </div>
      ) : (
        <Suspense>
          <DevPicker />
        </Suspense>
      )}
    </AuthShell>
  );
}
