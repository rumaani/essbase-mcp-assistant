# Settings, secrets and deployment

## Where settings come from
Precedence, lowest to highest:

1. Built-in defaults (mock mode, viewer profile)
2. Environment variables / `.env` (see `.env.example`); handy for containers and CI
3. `data/settings.json`, written by the **Admin** tab

The Admin tab always wins, so settings saved there stick across restarts. Delete
`data/settings.json` to fall back to `.env`. The CLI reads the same files.
`emcp --mock …` / `emcp --remote …` override the mode for a single command.

## Secrets
- The client secret, access token and LLM API key are encrypted in `data/settings.json` with
  Fernet (AES-128-CBC + HMAC-SHA256).
- The key comes from `EMCP_SECRET_KEY`, or is generated once into `data/secret.key` (owner-only
  permissions). If the key changes, saved secrets can't be decrypted and must be re-entered.
- The API never returns secrets, only `*_set: true/false`. In the form, a blank secret field
  keeps the saved value; "clear saved" removes it.
- `data/` is git-ignored. Never commit it.

## Protecting the app
| Control | Default | Notes |
|---|---|---|
| Bind address | `127.0.0.1` | `emcp web --host 0.0.0.0` exposes it on the network; only do that behind TLS and a login |
| Admin password | off | Set `EMCP_ADMIN_PASSWORD` to require it for `/api/admin/*` (the UI prompts once per page load) |
| CSRF | on | Non-GET `/api/*` calls must send `X-Requested-With: essbase-mcp-assistant` |
| Essbase profile | viewer | The MCP server hides tools above the profile; calculations are never run from the UI |

## Per-user sign-in
**Who Essbase sees → Each person signs in** makes every person sign in with their own
identity-domain account, so Essbase applies their filters. See [PER_USER_SECURITY.md](PER_USER_SECURITY.md).
In that mode, everything except the Admin page requires sign-in.

## Hardening for a shared deployment (beyond this reference)
- Put it behind a reverse proxy with TLS and SSO (e.g. Entra ID or OCI IAM via OIDC), and pass the user identity through.
- Keep `EMCP_SECRET_KEY` in a vault (OCI Vault, Azure Key Vault, AWS Secrets Manager).
- Send logs to your SIEM, and record who asked what and which tool ran.
