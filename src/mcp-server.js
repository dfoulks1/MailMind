#!/usr/bin/env node
/**
 * gmail-mcp-server — MCP Server exposing Gmail inbox summarization to LLM clients.
 *
 * HOW TO RUN:
 *   1. Copy .env.example → .env and fill in your credentials.
 *   2. Run `npm run auth` ONCE to generate your GMAIL_REFRESH_TOKEN (see src/auth/).
 *   3. Start the server: `npm start`
 *   4. In your Ollama / LLM client config, point the MCP server to:
 *        command: "node"
 *        args: ["/absolute/path/to/src/mcp-server.js"]
 *
 * The server communicates over stdio (stdin/stdout) using the MCP protocol.
 * Do NOT write anything to stdout except valid MCP protocol messages.
 */

import 'dotenv/config';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ErrorCode,
  McpError,
} from '@modelcontextprotocol/sdk/types.js';
import { z } from 'zod';

import { buildGmailClient } from './auth/oauth.js';
import { fetchRecentMessages } from './gmail/fetcher.js';
import { buildSummaryReport } from './gmail/summarizer.js';
import { RateLimiter } from './utils/rate-limiter.js';

// ─── Rate limiter: Gmail API free tier = 250 quota units/second ──────────────
// Each messages.list ≈ 5 units, messages.get ≈ 5 units.
// We conservatively cap at 20 API calls / 10s window.
const rateLimiter = new RateLimiter({ maxRequests: 20, windowMs: 10_000 });

// ─── Zod schema for summarize_inbox arguments ────────────────────────────────
const SummarizeInboxArgsSchema = z.object({
  daysBack: z.number().int().min(1).max(30).optional().default(1),
  priority: z.enum(['all', 'high', 'unread']).optional().default('all'),
});

// ─── MCP Server setup ─────────────────────────────────────────────────────────
const server = new Server(
  {
    name: 'gmail-mcp-server',
    version: '1.0.0',
  },
  {
    capabilities: { tools: {} },
  }
);

// ─── Tool registry ────────────────────────────────────────────────────────────
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'summarize_inbox',
      description:
        'Aggregates recent emails (last 24 hours or N days) into a structured summary report including: sender count, topics extracted, urgent items flagged, and actionable items. Returns a JSON report ready for the LLM to present to the user.',
      inputSchema: {
        type: 'object',
        properties: {
          daysBack: {
            type: 'number',
            description: 'Number of days back to scan (1–30). Defaults to 1.',
            default: 1,
          },
          priority: {
            type: 'string',
            enum: ['all', 'high', 'unread'],
            description:
              '"all" = every email, "high" = Gmail Important/Starred, "unread" = only unread. Defaults to "all".',
            default: 'all',
          },
        },
        required: [],
      },
    },
  ],
}));

// ─── Tool handler ─────────────────────────────────────────────────────────────
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: rawArgs } = request.params;

  if (name !== 'summarize_inbox') {
    throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
  }

  // Validate + apply defaults
  const parseResult = SummarizeInboxArgsSchema.safeParse(rawArgs ?? {});
  if (!parseResult.success) {
    throw new McpError(
      ErrorCode.InvalidParams,
      `Invalid arguments: ${parseResult.error.message}`
    );
  }
  const { daysBack, priority } = parseResult.data;

  // Rate-limit guard
  await rateLimiter.acquire();

  try {
    const gmail = await buildGmailClient();
    const messages = await fetchRecentMessages(gmail, { daysBack, priority, rateLimiter });
    const report = buildSummaryReport(messages, { daysBack, priority });

    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify(report, null, 2),
        },
      ],
    };
  } catch (err) {
    // Surface quota / auth errors clearly to the LLM client
    if (err.code === 403) {
      throw new McpError(
        ErrorCode.InternalError,
        'Gmail API quota exceeded (403). Wait a moment and retry.'
      );
    }
    if (err.code === 401) {
      throw new McpError(
        ErrorCode.InternalError,
        'Gmail authentication failed (401). Re-run `npm run auth` to refresh credentials.'
      );
    }
    throw new McpError(
      ErrorCode.InternalError,
      `Gmail API error: ${err.message ?? String(err)}`
    );
  }
});

// ─── Start server ─────────────────────────────────────────────────────────────
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Use stderr for operational logs — stdout is reserved for MCP protocol frames.
  process.stderr.write('[gmail-mcp-server] Server started, listening on stdio.\n');
}

main().catch((err) => {
  process.stderr.write(`[gmail-mcp-server] Fatal: ${err.message}\n`);
  process.exit(1);
});
