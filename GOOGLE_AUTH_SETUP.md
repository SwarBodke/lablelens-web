# LableLens Google Staff Sign-In Setup

LableLens uses Google Identity Services for staff identity and keeps authorization inside the FastAPI backend.

- Google proves who the user is.
- LableLens decides whether that verified account is an `admin` or `officer`.
- Users cannot choose their own role in the browser.
- Guest mode remains available.

## 1. Create a Google Web client

In Google Cloud Console / Google Auth Platform:

1. Create or select a project.
2. Configure the Google Auth Platform branding/audience for your deployment.
3. Go to **Google Auth Platform -> Clients**.
4. Create an OAuth 2.0 Client ID with application type **Web application**.
5. Add the browser origins that will host LableLens under **Authorized JavaScript origins**.

For local testing add:

```text
http://localhost:8000
```

For a deployed site add the exact HTTPS origin, for example:

```text
https://lablelens.example.com
```

This integration uses the Google Identity Services popup/callback ID-token flow, so LableLens needs the Web Client ID. It does not need a Google client secret in the frontend.

## 2. Configure approved staff accounts

Before starting LableLens, export a stable application secret and your Google client ID:

```bash
export LABLELENS_SECRET='replace-with-a-long-random-secret'
export GOOGLE_CLIENT_ID='YOUR_WEB_CLIENT_ID.apps.googleusercontent.com'
```

Add exact-email role allowlists:

```bash
export GOOGLE_ADMIN_EMAILS='admin@gmail.com'
export GOOGLE_OFFICER_EMAILS='officer1@gmail.com,officer2@gmail.com'
```

Exact-email allowlists work for Gmail accounts and Google Workspace accounts.

For a Google Workspace deployment, optional domain-wide role mapping is available:

```bash
export GOOGLE_ADMIN_DOMAINS='admin.example.org'
export GOOGLE_OFFICER_DOMAINS='inspectors.example.org'
```

Domain-wide mapping is accepted only when the verified Google ID token contains a matching hosted-domain (`hd`) claim.

## 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

This adds `google-auth` for backend ID-token verification. Existing OCR dependencies are unchanged.

## 4. Start LableLens

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

Open the workspace/session menu and click the Google sign-in button. The backend will return either an Admin or Officer workspace depending on the verified account allowlist.

## 5. Cloudflare Quick Tunnel note

A Quick Tunnel creates a random `https://...trycloudflare.com` hostname. Google Sign-In checks the browser origin against the authorized JavaScript origins configured for your Web client. If the Quick Tunnel hostname changes, add the new exact origin to the Google Web client before testing staff login.

For a durable staff deployment, use a named Cloudflare Tunnel or your own stable HTTPS domain so the authorized origin does not keep changing.

## 6. Security behavior

- A valid Google login is not enough for staff access; the email/Workspace identity must be allowlisted.
- Admin and Officer roles are assigned server-side.
- Google `sub` is used as the stable staff actor ID rather than the email address.
- Unapproved Google accounts receive HTTP 403.
- Invalid/unverifiable Google ID tokens receive HTTP 401.
- Google auth misconfiguration receives HTTP 503.
- Legacy access-code endpoints remain disabled unless their explicit `ALLOW_DEMO_*` environment flags are enabled; the normal UI no longer exposes them.
