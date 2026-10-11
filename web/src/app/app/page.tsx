"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { homeFor, useMembership } from "@/lib/session";

export default function AppHome() {
  const { membership } = useMembership();
  const router = useRouter();
  useEffect(() => {
    if (membership) router.replace(homeFor(membership.role));
  }, [membership, router]);
  return null;
}
