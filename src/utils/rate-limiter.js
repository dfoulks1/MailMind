/**
 * src/utils/rate-limiter.js
 *
 * Simple sliding-window rate limiter to prevent Gmail API quota exhaustion.
 *
 * Gmail API free quota: ~250 quota units/second.
 * Each messages.list or messages.get costs ~5 units.
 * Default: 20 requests per 10-second window = 10 req/sec (well within limits).
 */

export class RateLimiter {
  /**
   * @param {{ maxRequests: number, windowMs: number }} opts
   */
  constructor({ maxRequests = 20, windowMs = 10_000 } = {}) {
    this.maxRequests = maxRequests;
    this.windowMs = windowMs;
    /** @type {number[]} timestamps of recent requests */
    this._timestamps = [];
  }

  /**
   * Waits until a request slot is available, then claims it.
   * Resolves immediately if under the limit.
   *
   * @returns {Promise<void>}
   */
  async acquire() {
    const now = Date.now();

    // Evict timestamps outside the current window
    this._timestamps = this._timestamps.filter((t) => now - t < this.windowMs);

    if (this._timestamps.length < this.maxRequests) {
      this._timestamps.push(now);
      return;
    }

    // Need to wait until the oldest request in the window falls out
    const oldest = this._timestamps[0];
    const waitMs = this.windowMs - (now - oldest) + 5; // +5ms safety buffer

    process.stderr.write(
      `[rate-limiter] Rate limit reached (${this.maxRequests} req/${this.windowMs}ms). ` +
        `Waiting ${waitMs}ms…\n`
    );

    await new Promise((resolve) => setTimeout(resolve, waitMs));
    await this.acquire(); // Recurse to re-check after waiting
  }

  /** Returns current request count within the window (useful for debugging). */
  get currentLoad() {
    const now = Date.now();
    return this._timestamps.filter((t) => now - t < this.windowMs).length;
  }
}
