/**
 * src/auth/oauth.js
 *
 * Builds an authenticated Gmail API client using OAuth2 with a stored refresh token.
 * The refresh token is read from the environment — never hardcoded.
 *
 * Required environment variables (set in .env):
 *   GMAIL_CLIENT_ID       — OAuth2 Client ID from Google Cloud Console
 *   GMAIL_CLIENT_SECRET   — OAuth2 Client Secret
 *   GMAIL_REFRESH_TOKEN   — Long-lived refresh token (generate once via `npm run auth`)
 */

import { google } from 'googleapis';
import { OAuth2Client } from 'google-auth-library';

/**
 * Validates that all required OAuth env vars are present.
 * Throws a clear error at startup rather than at first API call.
 */
function validateEnv() {
  const required = ['GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET', 'GMAIL_REFRESH_TOKEN'];
  const missing = required.filter((k) => !process.env[k]);
  if (missing.length > 0) {
    throw new Error(
      `Missing required environment variables: ${missing.join(', ')}\n` +
        'Copy .env.example → .env and fill in your credentials, then run `npm run auth`.'
    );
  }
}

/**
 * Returns an authenticated googleapis Gmail client.
 * The OAuth2Client automatically refreshes access tokens using the stored refresh token.
 *
 * @returns {import('googleapis').gmail_v1.Gmail}
 */
export async function buildGmailClient() {
  validateEnv();

  const oauth2Client = new OAuth2Client(
    process.env.GMAIL_CLIENT_ID,
    process.env.GMAIL_CLIENT_SECRET,
    // Redirect URI — 'urn:ietf:wg:oauth:2.0:oob' is used for CLI/desktop flows
    'urn:ietf:wg:oauth:2.0:oob'
  );

  oauth2Client.setCredentials({
    refresh_token: process.env.GMAIL_REFRESH_TOKEN,
  });

  // Proactively refresh to surface auth errors early
  try {
    await oauth2Client.getAccessToken();
  } catch (err) {
    throw Object.assign(
      new Error(`OAuth2 token refresh failed: ${err.message}`),
      { code: 401 }
    );
  }

  return google.gmail({ version: 'v1', auth: oauth2Client });
}
