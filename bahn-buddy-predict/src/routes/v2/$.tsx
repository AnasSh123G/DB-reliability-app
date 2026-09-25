import { createFileRoute, redirect } from "@tanstack/react-router";

// /v2/train/5 -> /train/5 etc.: the v2 views moved to the root when they
// became the default interface.
export const Route = createFileRoute("/v2/$")({
  beforeLoad: ({ params }) => {
    throw redirect({ href: `/${params._splat ?? ""}`, replace: true });
  },
});
