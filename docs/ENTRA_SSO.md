# Single sign-on with Microsoft Entra ID

People open the app and are signed in with their Microsoft work account. If they're already
signed in to Windows, Edge or Microsoft 365, they see no sign-in page at all. Essbase still
applies each person's own roles and filters.

## How it fits together
Essbase 26ai accepts tokens from your **OCI IAM identity domain**, so Entra ID connects
*through* the identity domain: Entra ID becomes the domain's identity provider (SAML
federation). The app doesn't change how it talks to Essbase.

```
person opens the app
   │  not signed in → app sends the browser to the identity domain (auth code + PKCE)
   ▼
identity domain ── IdP policy for this app: "use Entra ID" ──► Microsoft Entra ID
   ▲                                                       already signed in? → no page
   │  SAML assertion (who the person is)  ◄────────────────  else Microsoft sign-in (+ MFA)
   │
identity domain maps the Microsoft user to its own user, issues the code → app
   │
app ──► Essbase 26ai with that person's identity-domain token → their filters apply
```

What people get:
- **No typing** when they already have a Microsoft session; otherwise the familiar Microsoft
  sign-in page, with your organization's MFA and Conditional Access.
- **Access managed in Entra groups.** Groups provisioned to the identity domain receive
  Essbase roles and filters. Joiners and leavers are handled where HR already handles them.

## Try it with the built-in sample
**Admin** → *Who Essbase sees* → **Each person signs in** → *Sign-in page* → **Microsoft Entra ID**
(automatic sign-in switches on) → **Save**, then open the app again:

1. The app sends you to a mock Microsoft account picker. Pick `alice@contoso.com`. In a
   company, Windows usually answers this step for you.
2. You land back in the app as Alice Analyst (East only).
3. Close the tab and reopen the app: you're signed in again with no page in between, because
   "Microsoft" remembers you.
4. `dave@contoso.com` exists in Entra but not in the identity domain, and is refused with an
   explanation (see *Provision users* below).

## Set it up

### A. Microsoft Entra ID: an enterprise app for your identity domain
Oracle's tutorial walks through this with screenshots: *SSO Between OCI and Microsoft Entra ID*
(link below). In short:
1. **Identity domain → Settings → Domain settings**: enable *Configure client access*, then
   download the identity domain's **SAML metadata**.
2. **Entra admin center → Enterprise applications → New application → Create your own**
   (non-gallery) → **Single sign-on: SAML**. Upload the identity domain metadata.
3. **Attributes & claims**: set the *Name ID* to the value that matches the identity domain
   user, typically `user.mail` or the UPN, matched to the domain's username or primary email.
4. **Users and groups**: assign the people or groups who may use Essbase.
5. Download the **Federation Metadata XML**.

### B. OCI identity domain: trust Entra ID and send this app's users there
6. **Security → Identity providers → Add IdP → SAML**: import the Entra metadata. Map the
   *SAML Name ID* to the domain *username* (or primary email, matching step 3). Activate it.
7. **Provision users** (and groups). People must exist in the identity domain for the
   mapping to work. Use SCIM provisioning from Entra (Oracle tutorial: *Identity Lifecycle
   Management Between OCI IAM and Entra ID*) or just-in-time provisioning.
8. **IdP policy**: add Entra ID to the identity provider policy rule that applies to the app's
   confidential application (from [PER_USER_SECURITY.md](PER_USER_SECURITY.md), step 1). If
   that rule offers only Entra ID, people go straight to Microsoft. If it offers several
   providers, they see a chooser first.

### C. Essbase
9. Grant Essbase roles and filters to the **groups** that come from Entra (e.g.
   *Essbase-Finance-East* → read filter on `@IDESCENDANTS("East")`). Access then follows
   Entra group membership.

### D. This app
10. **Admin** → *Each person signs in* (with the confidential app details from
    PER_USER_SECURITY.md) → *Sign-in page*: **Microsoft Entra ID** →
    **Sign people in automatically** (on by default for Entra).
11. *Also sign out of* (optional): leave blank so people stay signed in to Microsoft after
    leaving the app (the usual choice). To end the single sign-on session too, enter your
    identity domain's OpenID Connect logout endpoint (`end_session_endpoint` in
    `https://<domain>/.well-known/openid-configuration`).

### E. Verify
Open the app in a browser that's signed in to Microsoft 365: you should land in the app,
signed in, without a sign-in page. Then try two people in different Entra groups with the
same question.

## How the app behaves
| Situation | What happens |
|---|---|
| First visit, signed in to Microsoft | Redirects through identity domain → Entra → back, signed in. No page shown. |
| First visit, not signed in to Microsoft | Microsoft sign-in page (with your MFA), then back to the app |
| App session expired (idle hours, app restarted) | Signs in again automatically the next time the page needs it |
| Person clicks **Sign out** | App session ends; the app doesn't sign them straight back in. Their Microsoft session stays unless *Also sign out of* is set |
| Sign-in fails (e.g. not provisioned) | One automatic attempt per minute; then a **Sign in with Microsoft** button, never a redirect loop |

## Alternative: the app signs in with Microsoft directly
OCI IAM can also exchange a Microsoft token for an identity-domain token (*JWT assertion*
grant, or *token exchange* with an Identity Propagation Trust). The app would talk to
Entra itself and skip the SAML federation. It isn't the default here because:
- Oracle documents it for Fusion and OCI APIs, not for the Essbase MCP server.
- The JWT-assertion path needs Microsoft's signing certificates imported into the identity
  domain and kept current as Microsoft rotates them.
- Federation is the setup Oracle provides a tutorial for, and many organizations already
  have it for the OCI console.

If your identity team prefers token exchange, `auth/user_sessions.py` is the place to add it:
sign in with Microsoft, exchange the token at `/oauth2/v1/token`, and keep the rest unchanged.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| People see the identity domain's own sign-in page | The IdP policy rule for this app doesn't route to Entra ID (step 8) |
| People see a chooser between providers | The rule allows more than one provider; leave only Entra ID |
| "Signed in with Microsoft, but has no user in the identity domain" | Not provisioned (step 7), or Name ID doesn't match the domain username/email (steps 3, 6) |
| Signed in, but questions fail | No Essbase role for the person's groups (step 9) |
| Sign-in button keeps coming back | Check the identity domain's sign-in error. The app stops retrying after one automatic attempt per minute |
