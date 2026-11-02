# Enterprise SSO setup

This integration uses WorkOS for a customer's SAML or OIDC connection. treg remains responsible for
accounts, team roles, browser sessions, and API keys. The team owner purchases a monthly Stripe
subscription and opens WorkOS setup from **Team → SSO**. The link is generated when clicked and
expires after five minutes. The owner can invite an IT admin by email through WorkOS, or open the
short-lived setup link directly. The owner can copy the employee sign-in link from **Team → SSO**;
on any treg sign-in dialog, employees can choose **Company SSO**, enter their work email, and
select **Continue with SSO**. Email code remains the default sign-in method.
The lookup routes them to a verified company connection. It does not authenticate or join them.
Their first successful sign-in joins the existing team as a member.
The owner can change roles in **Members**.
Team display names may repeat; the link name is the unique slug in the employee sign-in URL
(for example, a second team named `Test` receives `test-2`). Employees can use the owner's exact
link if email discovery returns no match or several teams. When several teams match, the dialog
lists their names and unique link names so the employee can choose. Email is a routing hint, not
access proof. Existing verified treg users sign in normally once before linking their company
identity; treg resumes that pending SSO attempt in the same browser.

## Configure the deployment

1. Apply Alembic migrations (`python -m treg upgrade`); the server refuses to boot on an older schema.
2. In the WorkOS environment, create an API key and find the client ID. Set
   `TREG_WORKOS_API_KEY` and `TREG_WORKOS_CLIENT_ID` on the server. Set a stable
   `TREG_SECRET_KEY` and `TREG_SESSION_SECRET` for the state and browser cookies.
3. Add `<TREG_PUBLIC_URL>/auth/sso/callback` as an allowed WorkOS redirect URI. The URL passed to
   WorkOS comes from `TREG_PUBLIC_URL`, so it must match exactly.
4. For Directory Sync, register `<TREG_PUBLIC_URL>/auth/sso/workos/webhook` as a WorkOS webhook.
   Subscribe it to `dsync.activated`, `dsync.deleted`, and `dsync.user.created`,
   `dsync.user.updated`, `dsync.user.deleted`. Set its signing secret as
   `TREG_WORKOS_WEBHOOK_SECRET`. Without that secret, the endpoint returns 404.
   Run `treg-worker enterprise reconcile` periodically to repair missed webhook deliveries.
   Also run `treg-worker enterprise cleanup` periodically after subscriptions end. It removes
   WorkOS SSO connections and directories; monitor its nonzero exit status and retry failures.
   It reads the complete WorkOS roster before changing access; monitor failures.
5. Configure Stripe's existing `TREG_STRIPE_SECRET_KEY` and `TREG_STRIPE_WEBHOOK_SECRET`. Create
   one reusable USD monthly Stripe Product/Price per offered plan in the same test or live
   account as the secret key. Set `TREG_ENTERPRISE_SSO_BUNDLE_PRICE_ID` for the bundle and,
   only if SSO-only is offered, `TREG_ENTERPRISE_SSO_ONLY_PRICE_ID`. An unset Price hides that
   plan. treg reads the Price to display its amount and refuses an inactive, non-flat, non-USD,
   or non-monthly Price before opening Checkout. If the configured Price changed since the owner
   opened the page, Checkout asks them to refresh. Optional
   `TREG_ENTERPRISE_SSO_BUNDLE_MIN_PRICE_USD` and `TREG_ENTERPRISE_SSO_ONLY_MIN_PRICE_USD`
   reject a Stripe Price below each plan's deployment-specific minimum; both default to zero.
   Existing subscribers can manage SSO when Stripe's Price API is temporarily unavailable.
   A price change requires a new Price ID;
   existing subscriptions keep their old
   Price until explicitly migrated. Send
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, and `customer.subscription.deleted` to the existing
   `/billing/stripe/webhook` endpoint. The same Stripe account can handle tool balance top-ups and
   SSO subscriptions; subscription events do not change the prepaid balance.
6. For an IdP app tile, set the WorkOS application's sign-in endpoint to
   `<TREG_PUBLIC_URL>/auth/sso/idp/start`. WorkOS supplies the connection hint and treg begins
   its normal, state-checked SSO redirect. Test the tile with the customer's IdP.

WorkOS staging and Stripe test mode can be used together. Keep staging keys and test keys in the
same test deployment; production credentials need their own configured redirect and webhook.
The built-in WorkOS Test Identity Provider accepts profiles only for the domain verified on its
WorkOS organization. A profile with another email domain is rejected before treg receives a code;
treg returns the employee to its sign-in dialog with an explanation.

## Owner and employee flow

The team owner chooses **SSO** or **SSO + Directory Sync** at the monthly amount configured in
Stripe. The default
`TREG_ENTERPRISE_SSO_BUNDLE_ONLY=true` displays only the bundle; set it to `false` to offer both.
Each checkout uses the configured reusable Stripe Price rather than creating a new Product and
Price for every team.
After Stripe reports an active subscription, the owner follows the checked setup cards in
**Team → SSO**. **Set up company SSO** opens WorkOS; on the bundle, **Set up Directory Sync**
opens the directory setup. The owner should complete a real test sign-in before clicking
**Require SSO**. That control only becomes available after a successful
WorkOS sign-in.

The owner may enter an IT admin email to have WorkOS send setup instructions; the recipient does
not need a treg owner account. The Admin Portal button remains for owners doing setup themselves.
The SSO tab is always available to the team owner. Other members see it only after a paid SSO
connection is ready; the tab lets them copy the employee sign-in link rather than sending their
already signed-in browser through SSO again. The Team heading also offers the link without
opening SSO settings. Non-owners do not fetch Stripe plan prices.

An employee who signs in through the team's WorkOS organization gets a team membership with the
`member` role. A regular email or social sign-in does not satisfy SSO enforcement for that team,
even if it uses the same company email address. Existing team roles are never overwritten by an
IdP group. SSO creates a new account only when its email matches a verified WorkOS organization
domain. An existing treg account must sign in by its usual method in the same browser, then
complete one company SSO sign-in to link the IdP identity. An SSO assertion alone cannot claim
that global account. Verify domain ownership before marking a WorkOS domain verified; the dashboard
can mark a manually added domain verified without a DNS challenge. Keep WorkOS's
outside-organization profile option disabled. Owners retain
their normal sign-in as a recovery path. Team API keys remain separate
credentials; Directory Sync deactivation removes the affected membership and revokes its keys.
On the SSO-only plan, removing someone from the IdP stops future SSO sign-ins, but treg cannot
promise immediate revocation of an existing team session or key. Use Directory Sync for that.
An SSO browser session and any CLI or MCP OAuth credential it mints are limited to the company
team, even when that person belongs to other treg teams. CLI and OAuth consent omit teams requiring
a different sign-in; approval, token exchange, refresh, and use check the same rule. An existing
OAuth grant made through regular login stops working on that team when SSO becomes required.
When the owner requires SSO, earlier non-owner bearer keys stop authorizing this team, including
Default and agent keys. Employees can sign in with company SSO and issue new CLI or additional
keys. An owner can issue new agent keys. These new credentials work until their key or membership
is revoked; Directory Sync removal revokes both.
Use an ordinary sign-in to access other teams.
If an admin removes an SSO member in treg, that person stays blocked even if the directory still
lists them as active. The owner can allow SSO again from the SSO tab. Directory deactivation and
later reactivation remain automatic unless a team admin explicitly removed the person.
The manual block survives a canceled subscription and a later WorkOS connection replacement.

The owner can schedule subscription cancellation at the end of the current billing period from
the SSO tab. While Stripe retries a `past_due` payment, SSO and Directory Sync remain active.
When the subscription ends, treg lifts enforcement so existing members can use normal sign-in.
SSO-created memberships and their team keys are revoked;
pre-existing members keep their memberships. The cleanup worker then verifies the WorkOS
organization belongs to that team and deletes all its directories and SSO connections, including
ones created before a first sign-in. A failed cleanup is retried; a new subscription waits until
cleanup completes. WorkOS setup is permanently removed, so returning customers may need their IT
admin to configure it again. An active subscription must end before the team can be deleted.
If a canceled team still has WorkOS setup awaiting cleanup, team deletion waits too, so the
resources cannot become orphaned.

## Evidence and personal data

Company sign-ins and failures, setup actions, role changes, enforcement changes, and Directory Sync
access removals write a durable, team-scoped audit row. Access changes and their audit rows commit
together; setup requests are recorded after WorkOS responds. Rows contain the event,
internal user IDs, role transition when relevant, and time, not email addresses, profile payloads,
or WorkOS secrets. Team
deletion removes these rows. Schedule `treg-worker enterprise purge-audit` after the agreed
retention period (default 400 days); choose the final period with the SOC 2 auditor and privacy
counsel. Preserve separate evidence for setup approvals, IT verification, periodic access reviews,
sync failure monitoring, and incident response. These operational controls and WorkOS/Stripe
processing terms are necessary for SOC 2 and GDPR; code and tests alone do not establish compliance.
After cancellation, old WorkOS identity and directory rows are deleted. A manually removed
employee leaves only a team ID, treg user ID, and block time so a new connection cannot readmit
them. The owner can remove the block; team deletion and global user erasure remove it. It is
personal data, so privacy requests must review it alongside the account, directory roster,
WorkOS data, and audit records. An active company directory can sync a deleted roster entry
back; coordinate individual erasure with the customer's identity administrator.

## Verification before production

The repository tests simulate the documented WorkOS profile and directory event payloads and
verify wrong-organization rejection, verified-domain and account-link checks, CLI and OAuth scope,
regular-session denial, webhook signature, and deprovisioning. Before production, perform a staging
sign-in through a WorkOS organization with a verified company domain and a Stripe test subscription,
confirm webhook delivery and cancellation, and
test deactivating a directory user. The simulated tests cannot prove the external dashboard
configuration or provider-specific SAML/OIDC handshake.
