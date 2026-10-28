// Rewritten by scripts/start_production.sh on every launch. Not a secret: the
// tunnel hostname is public, and the bearer token is what protects the API.
// Must stay outside netlify/edge-functions/ (see start_production.sh).
export const API_ORIGIN = "https://ought-conference-camcorders-prayer.trycloudflare.com";
