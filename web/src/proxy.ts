import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

// Clerk mode: signed-out visitors to the app or onboarding are sent to sign in. The invite page
// and the landing page stay public. Dev mode: nothing to do here (the dev picker is client-side).
const isProtected = createRouteMatcher(["/app(.*)", "/onboarding(.*)"]);

// A shared demo login (NEXT_PUBLIC_DEMO_LOGIN=1) is not a Clerk session: its cookie only lets the
// page load; the API checks the demo token itself.
const demoLogin = process.env.NEXT_PUBLIC_DEMO_LOGIN === "1";

const clerk = clerkMiddleware(async (auth, req) => {
  if (demoLogin && req.cookies.get("tangible_demo")?.value) return;
  if (isProtected(req)) await auth.protect();
});

export default process.env.NEXT_PUBLIC_AUTH_MODE === "clerk" ? clerk : () => NextResponse.next();

export const config = {
  // Pages only: /api/* is the workbench, which checks the Bearer token itself.
  matcher: [
    "/((?!api|_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Clerk's auto-proxy path (Frontend API through this origin).
    "/__clerk/:path*",
  ],
};
