import { createFileRoute, redirect } from "@tanstack/react-router";

// v2 was served under /v2 before it became the default at /; keep those URLs
// working.
export const Route = createFileRoute("/v2/")({
  beforeLoad: () => {
    throw redirect({ to: "/", replace: true });
  },
});
