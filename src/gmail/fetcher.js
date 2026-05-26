/**
 * src/gmail/fetcher.js
 *
 * Fetches email messages from Gmail within a time window.
 * Returns structured metadata — never raw body content.
 *
 * Privacy note: Email body text is never logged or written to stdout.
 * Only extracted metadata (sender, subject, date, label flags) is returned.
 */

/**
 * @typedef {Object} MessageMeta
 * @property {string} id
 * @property {string} threadId
 * @property {string} from
 * @property {string} subject
 * @property {string} date          — ISO 8601
 * @property {boolean} isUnread
 * @property {boolean} isImportant
 * @property {boolean} isStarred
 * @property {string} snippet       — Gmail's auto-generated short preview (≤200 chars)
 */

/**
 * Builds a Gmail search query string from options.
 *
 * @param {{ daysBack: number, priority: 'all' | 'high' | 'unread' }} opts
 * @returns {string}
 */
function buildQuery({ daysBack, priority }) {
  const parts = [`newer_than:${daysBack}d`];
  if (priority === 'unread') parts.push('is:unread');
  if (priority === 'high') parts.push('is:important');
  return parts.join(' ');
}

/**
 * Extracts a header value from a message payload by name (case-insensitive).
 *
 * @param {Array<{name: string, value: string}>} headers
 * @param {string} name
 * @returns {string}
 */
function getHeader(headers, name) {
  return (
    headers?.find((h) => h.name.toLowerCase() === name.toLowerCase())?.value ?? ''
  );
}

/**
 * Fetches recent messages from Gmail.
 *
 * @param {import('googleapis').gmail_v1.Gmail} gmail
 * @param {{ daysBack: number, priority: string, rateLimiter: import('../utils/rate-limiter.js').RateLimiter }} opts
 * @returns {Promise<MessageMeta[]>}
 */
export async function fetchRecentMessages(gmail, { daysBack, priority, rateLimiter }) {
  const query = buildQuery({ daysBack, priority });

  // ── List message IDs ──────────────────────────────────────────────────────
  await rateLimiter.acquire();
  let listResponse;
  try {
    listResponse = await gmail.users.messages.list({
      userId: 'me',
      q: query,
      maxResults: 100, // Cap per call; Gmail returns up to 500 but 100 keeps quota sane
    });
  } catch (err) {
    throw enrichGmailError(err);
  }

  const messageRefs = listResponse.data.messages ?? [];
  if (messageRefs.length === 0) return [];

  // ── Fetch each message's metadata (no full body download) ─────────────────
  const results = [];

  for (const ref of messageRefs) {
    await rateLimiter.acquire();

    let msg;
    try {
      msg = await gmail.users.messages.get({
        userId: 'me',
        id: ref.id,
        // 'metadata' format returns headers + labels only — no body payload
        format: 'metadata',
        metadataHeaders: ['From', 'Subject', 'Date'],
      });
    } catch (err) {
      // Skip individual message failures (e.g. deleted mid-fetch) gracefully
      process.stderr.write(`[fetcher] Skipping message ${ref.id}: ${err.message}\n`);
      continue;
    }

    const { payload, labelIds = [], snippet, threadId } = msg.data;
    const headers = payload?.headers ?? [];

    results.push({
      id: ref.id,
      threadId,
      from: getHeader(headers, 'From'),
      subject: getHeader(headers, 'Subject') || '(no subject)',
      date: getHeader(headers, 'Date'),
      isUnread: labelIds.includes('UNREAD'),
      isImportant: labelIds.includes('IMPORTANT'),
      isStarred: labelIds.includes('STARRED'),
      // Snippet is Gmail's own short preview — safe metadata, not the full body
      snippet: snippet?.slice(0, 200) ?? '',
    });
  }

  return results;
}

/**
 * Attaches a numeric `.code` to Gmail API errors for easier handling upstream.
 *
 * @param {Error & { response?: { status?: number } }} err
 * @returns {Error}
 */
function enrichGmailError(err) {
  const status = err?.response?.status ?? err?.code;
  if (status) err.code = Number(status);
  return err;
}
