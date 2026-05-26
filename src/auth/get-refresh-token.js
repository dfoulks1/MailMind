#!/usr/bin/env node
/**
 * src/auth/get-refresh-token.js
 *
 * Run ONCE to generate a Gmail OAuth2 refresh token:
 *   npm run auth
 *
 * Steps:
 *   1. Opens an authorization URL in your terminal.
 *   2. You paste the URL into a browser, sign in with your Google account,
 *      and grant Gmail read access.
 *   3. Google returns a one-time auth code — paste it back into this terminal.
 *   4. The script prints your GMAIL_REFRESH_TOKEN — copy it into your .env file.
 *
 * This script requires GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET already set in .env.
 */

import 'dotenv/config';
import { OAuth2Client } from 'google-auth-library';
import * as readline from 'node:readline';

const SCOPES = [
  // Read-only scope — this server never modifies your inbox
  'https://www.googleapis.com/auth/gmail.readonly',
];

/**
 * Prompts the user for input using the callback-based readline API.
 * Compatible with Node.js 14+.
 *
 * @param {string} question
 * @returns {Promise<string>}
 */
function prompt(question) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({
      input: process.stdin,
      output: process.stdout,
    });
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim());
    });
  });
}

async function main() {
  const clientId = process.env.GMAIL_CLIENT_ID;
  const clientSecret = process.env.GMAIL_CLIENT_SECRET;

  if (!clientId || !clientSecret) {
    console.error(
      'Error: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env before running this script.'
    );
    process.exit(1);
  }

  const oauth2Client = new OAuth2Client(
    clientId,
    clientSecret,
    'urn:ietf:wg:oauth:2.0:oob'
  );

  const authUrl = oauth2Client.generateAuthUrl({
    access_type: 'offline',
    scope: SCOPES,
    prompt: 'consent', // Force consent screen so Google always returns a refresh_token
  });

  console.log('\n──────────────────────────────────────────────────────────');
  console.log('STEP 1: Open this URL in your browser and authorize access:');
  console.log('\n' + authUrl + '\n');
  console.log('──────────────────────────────────────────────────────────\n');

  const code = await prompt('STEP 2: Paste the authorization code here: ');

  const { tokens } = await oauth2Client.getToken(code);

  if (!tokens.refresh_token) {
    console.error(
      '\nError: No refresh_token returned. This can happen if your Google account\n' +
        'has already authorized this client. Revoke access at:\n' +
        'https://myaccount.google.com/permissions\n' +
        'Then re-run this script.'
    );
    process.exit(1);
  }

  console.log('\n✅ Success! Add this to your .env file:\n');
  console.log(`GMAIL_REFRESH_TOKEN=${tokens.refresh_token}`);
  console.log('\nKeep this token private — it grants read access to your Gmail.\n');
}

main().catch((err) => {
  console.error('Auth error:', err.message);
  process.exit(1);
});
