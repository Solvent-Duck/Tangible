import { SignUp } from "@clerk/nextjs";
import { redirect } from "next/navigation";

import { AuthShell } from "@/components/auth/auth-card";

export const metadata = { title: "Create account" };

export default function SignUpPage() {
  // Staff are added by an admin and businesses arrive through an invite link; in dev mode there
  // is nothing to sign up for.
  if (process.env.NEXT_PUBLIC_AUTH_MODE !== "clerk") redirect("/sign-in");
  return (
    <AuthShell>
      <SignUp fallbackRedirectUrl="/app" signInUrl="/sign-in" />
    </AuthShell>
  );
}
