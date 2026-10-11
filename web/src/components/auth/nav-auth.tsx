import { Show, SignInButton, SignUpButton, UserButton } from "@clerk/nextjs";
import Link from "next/link";

const btn = "rounded-lg px-3 py-2 text-[14px] font-medium text-muted hover:text-ink";
const primary =
  "hidden rounded-lg bg-brand px-4 py-2 text-[14px] font-medium text-brand-fg shadow-card hover:bg-brand-hover sm:inline-flex";

/** Landing-page auth controls: Clerk's sign-in/up and user button, or the dev sign-in link. */
export function NavAuth() {
  if (process.env.NEXT_PUBLIC_AUTH_MODE !== "clerk") {
    return (
      <Link href="/sign-in" className={btn}>
        Sign in
      </Link>
    );
  }
  return (
    <>
      <Show when="signed-out">
        <SignInButton mode="redirect">
          <button className={btn}>Sign in</button>
        </SignInButton>
        <SignUpButton mode="redirect">
          <button className={primary}>Sign up</button>
        </SignUpButton>
      </Show>
      <Show when="signed-in">
        <Link href="/app" className={btn}>
          Open the workbench
        </Link>
        <UserButton />
      </Show>
    </>
  );
}
