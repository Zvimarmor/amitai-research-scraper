// Proxies /api/* to the Mac mini and attaches the bearer token server-side, so
// visitors need no credentials and the token never reaches the browser.
//
// Netlify environment variables:
//   API_AUTH_TOKEN  must match API_AUTH_TOKEN in the backend .env  (secret)
//   API_ORIGIN      optional override of the tunnel hostname below (not secret)
//
// API_ORIGIN falls back to origin.ts, which scripts/start_production.sh rewrites
// on every launch, so a new quick-tunnel hostname ships with the next push
// without anyone touching the dashboard.

import { API_ORIGIN as FALLBACK_ORIGIN } from "./origin.ts";

export default async (request: Request): Promise<Response> => {
  const origin = (Deno.env.get("API_ORIGIN") || FALLBACK_ORIGIN || "").replace(/\/+$/, "");
  const token = Deno.env.get("API_AUTH_TOKEN") || "";

  if (!origin) {
    return json(503, {
      error: "API_ORIGIN is not configured",
      hint: "Set API_ORIGIN in Netlify, or run scripts/start_production.sh to publish the tunnel hostname.",
    });
  }
  if (!token) {
    return json(503, {
      error: "API_AUTH_TOKEN is not configured",
      hint: "Netlify -> Site configuration -> Environment variables -> add API_AUTH_TOKEN, then redeploy.",
    });
  }

  const incoming = new URL(request.url);
  const target = origin + incoming.pathname + incoming.search;

  // Rebuild the headers rather than forwarding them wholesale: Host would point
  // at the Netlify domain, and any Authorization the client sent is replaced so
  // a visitor cannot override the token.
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  headers.set("authorization", `Bearer ${token}`);
  headers.set("accept", request.headers.get("accept") || "*/*");

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    });

    // Stream the response through untouched, so the CSV export keeps its
    // Content-Disposition and UTF-8 BOM.
    const out = new Headers(upstream.headers);
    out.delete("content-encoding");
    out.delete("content-length");
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch (err) {
    return json(502, {
      error: "Backend unreachable",
      detail: String(err),
      hint: "The Mac mini or the cloudflared tunnel is probably down.",
    });
  }
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

export const config = { path: "/api/*" };
