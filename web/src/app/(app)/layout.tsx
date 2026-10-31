import { Suspense } from "react";
import { AppShell } from "@/components/app-shell";

// The shell and pages read the URL (usePathname/useParams), which only exists at
// request time; with cacheComponents on they must stream inside a Suspense boundary.
export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <Suspense fallback={<div className="min-h-dvh" aria-busy="true" />}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
