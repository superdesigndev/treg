# treg for Microsoft 365 Copilot

A declarative agent for Microsoft 365 Copilot whose one action is treg's team MCP server (`/mcp/`).
Copilot discovers the tools at runtime (`tools/list`), so the package never changes when the
catalog or the tool list does.

`appPackage/` holds the sources: the app manifest (Teams schema 1.30), the declarative agent (v1.9),
the MCP plugin (v2.4, `RemoteMCPServer` runtime with `OAuthPluginVault` auth), the instructions and
the two icons. `build.py` turns them into the zip an admin uploads.

## How sign-in works

Microsoft 365 Copilot cannot send an API key to an MCP action; it signs each user in with OAuth and
keeps the tokens in Microsoft's token store. treg is the authorization server, and the agent is the
first-party confidential client `treg-m365-copilot` (`docs/context/architecture/mcp-oauth.md`):

- The client exists only on a deployment with `TREG_M365_CLIENT_SECRET` set. Its one redirect URI is
  Microsoft's `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`.
- Microsoft redeems the code with that secret (HTTP Basic by default) and PKCE, and sends no
  `resource`; the token's audience is then `/mcp/`.
- Each user approves once on treg's consent screen and picks the team the agent spends from.

## 1. Register the OAuth client with Microsoft (once per deployment)

Generate a long random secret and set it as `TREG_M365_CLIENT_SECRET` on the treg deployment. Then,
signed in to the publisher's Microsoft 365 tenant, open the Teams Developer Portal, Tools, OAuth client
registration, New:

| Field | Value |
|---|---|
| Registration name | treg |
| Base URL | `https://<host>/mcp/`, byte-identical to the plugin's `spec.url` |
| Restrict usage by org | Any Microsoft 365 organization |
| Restrict usage by app | Any Teams app (a registration bound to one app id makes every tool call fail) |
| Client ID | `treg-m365-copilot` |
| Client secret | the value of `TREG_M365_CLIENT_SECRET` |
| Authorization endpoint | `https://<host>/oauth/authorize` |
| Token endpoint | `https://<host>/oauth/token` |
| Refresh endpoint | `https://<host>/oauth/token` |
| Scope | `treg:catalog treg:call treg:read` |
| Enable PKCE | on |

Save it and copy the **OAuth client registration ID**: that is the auth reference id below.

## 2. Build the package

```bash
python3 plugins/m365-copilot/build.py --auth-reference-id <registration id>
# a self-hosted registry: add --mcp-url https://<host>/mcp/
```

The zip lands in `dist/treg-m365-copilot.zip`. Bump `version` in `appPackage/manifest.json` for every
package that replaces one already uploaded.

## 3. Install it in a tenant

The tenant needs Microsoft 365 Copilot (or Copilot Chat) and custom app upload allowed.

- **Try it yourself**: Teams, Apps, Manage your apps, Upload an app, Upload a custom app.
- **A customer's organization**: a Microsoft 365 admin opens the admin center, Agents, All agents,
  Add agent, uploads the zip, and chooses who may install it (start with a test group).

Each user then opens treg in Copilot, signs in to treg when asked, and approves on the consent
screen.

## 4. Publish to the Agent Store

Submit the same zip in Partner Center (Microsoft 365 and Copilot program, offer type "Apps and agents
for Microsoft 365 and Copilot"). The review needs a screenshot of the agent working in Copilot, a
test treg account with balance in the certification notes, and tools that answer within the store's
latency limits. See Microsoft's
[Agent Store validation guidelines](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/appsource/prepare/review-copilot-validation-guidelines).
