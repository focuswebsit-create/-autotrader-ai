"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Re-fetches server state periodically so an open tab never silently shows old data. */
export default function AutoRefresh({ seconds }: { seconds: number }) {
  const router = useRouter();
  useEffect(() => {
    const id = window.setInterval(() => router.refresh(), seconds * 1000);
    return () => window.clearInterval(id);
  }, [router, seconds]);
  return null;
}
