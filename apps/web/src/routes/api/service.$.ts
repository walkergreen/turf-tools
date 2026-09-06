import { createFileRoute } from "@tanstack/react-router";
import { db } from "@turf-tools/db";
import { handleServiceRequest } from "~/rpc/service/http";

// Server-to-server mount for automation (Org Tools' zapctl): bearer-token
// auth, JSON in and out. Browsers never call this surface, so no CORS.
export const Route = createFileRoute("/api/service/$")({
  server: {
    handlers: {
      ANY: ({ request }) => handleServiceRequest(request, db),
    },
  },
});
