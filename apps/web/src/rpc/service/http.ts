import { OpenAPIHandler } from "@orpc/openapi/fetch";
import type { Db } from "@turf-tools/db";
import { authenticateServiceToken } from "~/lib/service-auth";
import { serviceRouter } from "..";

export const SERVICE_API_PREFIX = "/api/service";

// Plain JSON over REST-ish paths (each procedure's `.route({ path })`), so
// the caller needs no oRPC client.
const handler = new OpenAPIHandler(serviceRouter);

// One request through the service API: authenticate the bearer token, leave
// an audit line, dispatch. Server-to-server only, so no CORS handling.
export async function handleServiceRequest(request: Request, db: Db): Promise<Response> {
  const context = await authenticateServiceToken(db, request.headers);
  if (!context) return Response.json({ error: "unauthorized" }, { status: 401 });

  const procedure = new URL(request.url).pathname.slice(SERVICE_API_PREFIX.length) || "/";
  // Only the org slug is read out of the body — never emails or other fields.
  console.info(
    JSON.stringify({
      event: "service.rpc",
      token: context.token.name,
      procedure,
      orgSlug: await orgSlugFromBody(request),
    }),
  );

  const { response } = await handler.handle(request, { prefix: SERVICE_API_PREFIX, context });
  return response ?? Response.json({ error: "not_found" }, { status: 404 });
}

async function orgSlugFromBody(request: Request): Promise<string | null> {
  if (request.method === "GET" || request.method === "HEAD") return null;
  try {
    const body: unknown = await request.clone().json();
    if (typeof body !== "object" || body === null) return null;
    const { orgSlug, slug } = body as { orgSlug?: unknown; slug?: unknown };
    const value = orgSlug ?? slug;
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}
