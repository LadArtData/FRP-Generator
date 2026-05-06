/* ═══════════════════════════════════════════════════════════════════
 * frp-rest-bridge.js
 *
 * Loads inside FRP_Studio_v5_apex.html (served as a Static App File).
 * Exposes window.FRP with the same shape the Studio's app code expects,
 * but talks directly to ORDS REST endpoints under /ords/admin/frp-hooks/
 * via fetch() — no apex.server.process, no parent window.
 *
 * Upload alongside the Studio HTML in Shared Components → Static
 * Application Files. The HTML loads it via:
 *   <script src="#APP_FILES#frp-rest-bridge.js"></script>
 *
 * Same-origin: the static file URL and the ORDS URL are on the same
 * host, so fetch() works without CORS and sends the auth cookie if any.
 * If api_configuration.api_key is populated, set window.FRP_API_KEY
 * before the Studio's app code runs (e.g. in a later <script>) and
 * every call will append ?api_key=… automatically.
 * ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // Default to the relative path so it just works wherever the file is
  // served from. Override by setting window.FRP_BASE before this script
  // loads if you ever need to point at a different ORDS host.
  const BASE =
    (typeof window.FRP_BASE === 'string' && window.FRP_BASE) ||
    '/ords/admin/frp-hooks';

  function buildUrl(path, params) {
    const u = new URL(BASE + path, window.location.origin);
    if (window.FRP_API_KEY) u.searchParams.set('api_key', window.FRP_API_KEY);
    if (params) {
      for (const k in params) {
        if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
          u.searchParams.set(k, params[k]);
        }
      }
    }
    return u.toString();
  }

  async function call(method, path, opts) {
    opts = opts || {};
    const url = buildUrl(path, opts.query);
    const init = { method: method, headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body);
    }
    const res = await fetch(url, init);
    const text = await res.text();
    let payload;
    try { payload = text ? JSON.parse(text) : {}; }
    catch (e) { throw new Error(method + ' ' + path + ': non-JSON response — ' + text.slice(0, 200)); }
    if (!res.ok || payload.ok === false) {
      const msg = payload.error || res.statusText || 'request failed';
      const err = new Error(method + ' ' + path + ': ' + msg);
      err.status = res.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  const FRP = {
    /** Sanity ping — returns {ok, docs, chunks, proposals, now}. */
    health() { return call('GET', '/health'); },

    /**
     * List documents for the left rail.
     * @param {object} [opts]
     * @param {string} [opts.status]  'all' | 'won' | 'lost' | 'rfp' | 'reference' | …
     * @param {string} [opts.q]       free-text search across filename + raw_text
     * @param {number} [opts.limit]   max rows (default 200, cap 1000)
     * @param {number} [opts.offset]  pagination offset (default 0)
     */
    listLibrary(opts) {
      opts = opts || {};
      return call('GET', '/library', { query: {
        status: opts.status,
        q: opts.q,
        limit: opts.limit,
        offset: opts.offset
      }});
    },

    /** Total counts + per-status histogram for the rail footer / tab badges. */
    libraryStats() { return call('GET', '/library/stats'); },

    /** Schedule FRP_INGEST.scan_all_prefixes as a background job. */
    startScan() { return call('POST', '/library/scan'); },

    /** Single doc detail + chunk count + 1500-char preview. */
    getDoc(docId) { return call('GET', '/library/' + encodeURIComponent(docId)); },

    /**
     * Top-K cosine search across FRP_CHUNKS.
     * @param {string} q
     * @param {number} [topK=8]
     * @param {string} [dealStatus]
     */
    search(q, topK, dealStatus) {
      return call('POST', '/search', {
        body: { q: q, top_k: topK || 8, deal_status: dealStatus || null }
      });
    },

    /** Recent proposals, newest-updated first. */
    listProposals(limit) {
      return call('GET', '/proposals', { query: { limit: limit } });
    },

    /** Create a draft proposal. Returns {ok, proposal_id}. */
    createProposal(clientName, rfpDocId, dueDateISO) {
      return call('POST', '/proposals', {
        body: {
          client_name: clientName || null,
          rfp_doc_id: rfpDocId || null,
          due_date: dueDateISO || null
        }
      });
    },

    /** Full state including attachments. */
    getProposal(proposalId) {
      return call('GET', '/proposals/' + encodeURIComponent(proposalId));
    },

    /**
     * Kick FRP_PIPELINE.run_proposal as a background job.
     * Returns {ok, proposal_id, job_name, status:"generating"}.
     * Poll FRP.getProposal(id) to watch status flip back to "draft" / "ready".
     */
    generateProposal(proposalId) {
      return call('POST', '/proposals/' + encodeURIComponent(proposalId) + '/generate');
    },

    /** Link a library doc to a proposal. */
    attachDoc(proposalId, docId, role) {
      return call('POST', '/proposals/' + encodeURIComponent(proposalId) + '/attachments', {
        body: { doc_id: docId, role: role || 'reference' }
      });
    },

    /** Per-prefix histogram for the admin Connect / Ingest page. */
    bucketFolders() { return call('GET', '/bucket/folders'); },

    /**
     * Parse an RFP doc with Llama + vector-match against past proposals.
     * Returns {ok, doc_id, filename, parsed_fields, match_data, elapsed_ms}.
     * parsed_fields is the raw JSON CLOB string returned by the parser.
     * match_data is an object with {match_count, matches[]}.
     */
    parseRfp(docId) {
      return call('POST', '/rfp/parse', { body: { doc_id: docId } });
    }
  };

  // Expose for the inline Studio script.
  window.FRP = FRP;
  window.FRP_BRIDGE_READY = true;
  window.dispatchEvent(new CustomEvent('frp-bridge-ready'));
})();
