/**
 * src/gmail/summarizer.js
 *
 * Pure-function layer that transforms raw MessageMeta[] into a
 * structured JSON summary report for consumption by an LLM.
 *
 * No I/O, no API calls — just data transformation.
 */

// ─── Keyword sets for urgency / action detection ──────────────────────────────

const URGENT_KEYWORDS = [
  'urgent', 'asap', 'immediately', 'action required', 'critical',
  'deadline', 'overdue', 'past due', 'final notice', 'alert',
  'warning', 'attention', 'important:', 'respond by',
];

const ACTION_KEYWORDS = [
  'please review', 'please respond', 'follow up', 'follow-up',
  'reply by', 'confirm', 'approval needed', 'sign', 'review and',
  'your action', 'next steps', 'schedule', 'meeting request',
  'invitation', 'rsvp', 'invoice', 'payment', 'verify',
];

/**
 * Normalises a subject line for clustering:
 *   - Strips Re:/Fwd: prefixes
 *   - Lower-cases
 *   - Collapses whitespace
 *
 * @param {string} subject
 * @returns {string}
 */
function normalizeSubject(subject) {
  return subject
    .replace(/^(re|fwd?|fw)\s*:\s*/gi, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Very lightweight "topic" extractor.
 * Uses the normalised subject as the topic key — good enough for clustering
 * without requiring an NLP library.
 *
 * @param {string} subject
 * @returns {string}
 */
function extractTopic(subject) {
  return normalizeSubject(subject) || 'no subject';
}

/**
 * Returns true if the combined subject+snippet text suggests urgency.
 *
 * @param {string} text
 * @returns {boolean}
 */
function isUrgent(text) {
  const lower = text.toLowerCase();
  return URGENT_KEYWORDS.some((kw) => lower.includes(kw));
}

/**
 * Returns true if the combined subject+snippet text requests an action.
 *
 * @param {string} text
 * @returns {boolean}
 */
function isActionable(text) {
  const lower = text.toLowerCase();
  return ACTION_KEYWORDS.some((kw) => lower.includes(kw));
}

/**
 * Extracts the display name (or email address) from a "From" header value.
 * e.g. "Jane Doe <jane@example.com>" → "Jane Doe"
 *      "<jane@example.com>" → "jane@example.com"
 *
 * @param {string} from
 * @returns {string}
 */
function parseSenderName(from) {
  const nameMatch = from.match(/^"?([^"<]+)"?\s*</);
  if (nameMatch) return nameMatch[1].trim();
  const emailMatch = from.match(/<([^>]+)>/);
  if (emailMatch) return emailMatch[1];
  return from.trim();
}

/**
 * Clusters messages by normalised subject.
 * Returns an array of thread clusters sorted by message count (desc).
 *
 * @param {import('./fetcher.js').MessageMeta[]} messages
 * @returns {Array<{ topic: string, count: number, senders: string[], hasUrgent: boolean, hasActionable: boolean, latestSnippet: string }>}
 */
function clusterBySubject(messages) {
  /** @type {Map<string, typeof messages>} */
  const clusters = new Map();

  for (const msg of messages) {
    const key = normalizeSubject(msg.subject);
    if (!clusters.has(key)) clusters.set(key, []);
    clusters.get(key).push(msg);
  }

  return Array.from(clusters.entries())
    .map(([topic, msgs]) => {
      const combined = msgs.map((m) => `${m.subject} ${m.snippet}`).join(' ');
      const uniqueSenders = [...new Set(msgs.map((m) => parseSenderName(m.from)))];
      // Use the snippet of the most recent message in the cluster
      const latest = msgs[0];
      return {
        topic,
        count: msgs.length,
        senders: uniqueSenders,
        hasUrgent: isUrgent(combined),
        hasActionable: isActionable(combined),
        latestSnippet: latest.snippet,
      };
    })
    .sort((a, b) => b.count - a.count);
}

/**
 * Builds the complete summary report.
 *
 * @param {import('./fetcher.js').MessageMeta[]} messages
 * @param {{ daysBack: number, priority: string }} opts
 * @returns {object} JSON-serialisable report object
 */
export function buildSummaryReport(messages, { daysBack, priority }) {
  if (messages.length === 0) {
    return {
      generatedAt: new Date().toISOString(),
      daysBack,
      priority,
      totalEmails: 0,
      unreadCount: 0,
      importantCount: 0,
      starredCount: 0,
      uniqueSenders: 0,
      urgentItems: [],
      actionableItems: [],
      topSenders: [],
      threadClusters: [],
      summary: 'No emails found for the specified time range and filter.',
    };
  }

  // ── Aggregate counts ───────────────────────────────────────────────────────
  const unreadCount = messages.filter((m) => m.isUnread).length;
  const importantCount = messages.filter((m) => m.isImportant).length;
  const starredCount = messages.filter((m) => m.isStarred).length;

  // ── Sender frequency ───────────────────────────────────────────────────────
  const senderFreq = new Map();
  for (const msg of messages) {
    const sender = parseSenderName(msg.from);
    senderFreq.set(sender, (senderFreq.get(sender) ?? 0) + 1);
  }
  const topSenders = [...senderFreq.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 10)
    .map(([sender, count]) => ({ sender, count }));

  // ── Urgent / actionable items ──────────────────────────────────────────────
  const urgentItems = messages
    .filter((m) => isUrgent(`${m.subject} ${m.snippet}`) || m.isImportant || m.isStarred)
    .slice(0, 20)
    .map((m) => ({
      subject: m.subject,
      from: parseSenderName(m.from),
      date: m.date,
      isUnread: m.isUnread,
      // Snippet only — no full body
      preview: m.snippet,
    }));

  const actionableItems = messages
    .filter((m) => isActionable(`${m.subject} ${m.snippet}`))
    .slice(0, 20)
    .map((m) => ({
      subject: m.subject,
      from: parseSenderName(m.from),
      date: m.date,
      isUnread: m.isUnread,
      preview: m.snippet,
    }));

  // ── Thread clusters ────────────────────────────────────────────────────────
  const threadClusters = clusterBySubject(messages).slice(0, 30);

  // ── Top topics (unique subject clusters) ──────────────────────────────────
  const topTopics = threadClusters.slice(0, 10).map((c) => c.topic);

  return {
    generatedAt: new Date().toISOString(),
    daysBack,
    priority,
    totalEmails: messages.length,
    unreadCount,
    importantCount,
    starredCount,
    uniqueSenders: senderFreq.size,
    topSenders,
    topTopics,
    urgentItems,
    actionableItems,
    threadClusters,
    // Human-readable one-liner for the LLM to use as context
    summary: `Found ${messages.length} email(s) over the last ${daysBack} day(s). ` +
      `${unreadCount} unread, ${urgentItems.length} urgent, ${actionableItems.length} actionable. ` +
      `Top topics: ${topTopics.slice(0, 3).join('; ') || 'none'}.`,
  };
}
