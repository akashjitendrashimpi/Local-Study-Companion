# Local Study Companion

A mobile-friendly web dashboard for the browser-based study assistant. Visitors create an account with a username, date of birth, and password. Dates display as `MM/DD/YYYY`; compact digit entries are formatted automatically, and the database stores canonical ISO dates. Sign-in remains active on that device until logout. Groq API keys are managed centrally by the administrator; visitors are never asked to provide one.

## Run locally

1. Install the dependencies and browser:

   ```powershell
   python -m pip install -r requirements.txt
   python -m playwright install chromium
   ```

2. Copy `.env.example` to `.env`. Set `APP_SECRET` to a random value of at least 32 characters and choose a strong `ADMIN_PASSWORD`. You can generate the secret with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. Start the server:

   ```powershell
   python web_app.py
   ```

4. Sign in as `admin` (or the configured `ADMIN_USERNAME`), open **Admin**, and add Groq keys, one per line. Keys are encrypted in the database and are not displayed again. If a key is rejected or the Groq request fails, the current run tries another configured key before stopping.

5. Visitors open the site, create an account, and use the assignment URL. Their browser automation session stays isolated. Runs stop before final submission; review and submit manually.

The local database is stored under `APP_DATA_DIR` (default: `data`). Keep this directory private and back it up securely. Do not change `APP_SECRET` after adding encrypted API keys unless you first re-enter those keys.

## Supabase database

For a hosted service with 100+ accounts, use a Supabase Postgres database rather than relying on a container's local SQLite file:

1. Create a Supabase project and copy its Postgres connection string from **Connect**. Prefer the Supavisor pooler connection string for hosted app traffic.
2. Set it as `SUPABASE_DATABASE_URL` in the server's environment (Render supports this in the service settings). Keep it secret; never put it in browser JavaScript or commit it to Git.
3. Restart the service. The app creates its tables, enables row-level security, and revokes direct access for Supabase `anon` and `authenticated` roles. The app accesses data only through its server-side database connection.

When `SUPABASE_DATABASE_URL` is set, accounts, DOB, salted password hashes, sessions, and encrypted API keys are stored in Supabase. The companion app handles username/password authentication server-side; it does not store or expose readable passwords. Without the URL, local SQLite remains available. Admins can view usernames, DOB, and account creation dates, and replace a password; they cannot retrieve the existing one. Password reset revokes the user's existing sessions.

Switching an existing installation from SQLite to Supabase does not copy its existing accounts or keys automatically. Back up the SQLite database and arrange a one-time migration before switching if it already contains data.

## Assignment credits

In **Admin → Users**, configure the default assignment credits and expiry for new accounts. Defaults apply only to accounts created after the change; existing accounts are not modified. You can also allocate credits to an individual account, choosing an expiry in days or `0` for no expiry. Expired credits are not available to users. A credit is reserved when a user starts a run, consumed only when the assistant reaches the review-ready stage, and returned to its original grant if the run fails or is stopped early. A reservation does not expire mid-run, but a refund remains subject to that grant's expiry. Cashfree payment verification is not integrated yet; do not treat uploaded screenshots as proof of payment.

## Mobile OTP sign-in on study sites

For Sunstone URLs, the dashboard offers an optional phone-number field. The number is used only to fill and submit the phone field on `hub.sunstone.in` or `www.hub.sunstone.in`; it is not saved in the account or database. The user enters the OTP directly in the browser view on their phone (or opens the view in a separate tab). The per-account hosted browser context is retained so Sunstone can stay signed in across later app logins. Do not send OTPs to the app or automate reading SMS messages.

## Hosted deployment

The Render service uses Browserbase for isolated hosted browsers. Configure these environment variables in Render:

- `BROWSERBASE_API_KEY`
- `APP_SECRET` (random, at least 32 characters)
- `ADMIN_PASSWORD` (strong, unique password)
- `ADMIN_USERNAME` (optional; defaults to `admin`)
- `SUPABASE_DATABASE_URL` (recommended for hosted accounts)

If you use SQLite instead, its data on an ephemeral container filesystem can be lost when a hosted service restarts or redeploys; attach persistent storage and set `APP_DATA_DIR` before inviting users. Browserbase usage and Groq API usage are billed to their respective accounts.

## Payments

Cashfree checkout, payment verification, and screenshot review are intentionally not included yet. Add these only when the payment flow is ready; do not treat uploaded screenshots as proof of a verified payment.

The standalone command-line runner remains available with `python agent.py`.
