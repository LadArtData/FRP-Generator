/* frp-rest-bridge.js
 *
 * Defines window.FRP for the FRP Studio. The Studio HTML is unchanged from the
 * team's design; this bridge is the seam that points it at the HARALD container.
 *
 * Auth: HARALD requires a signed session token on every state-changing call. The
 * token is issued by /api/signin and held by harald.js. This bridge reads it from
 * the same store, so the Studio and the other workspaces share one session.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "harald.token";

  function token() {
    try { return window.sessionStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }

  function headers(extra) {
    var base = { "Content-Type": "application/json" };
    var value = token();
    if (value) base["X-Harald-Token"] = value;
    return Object.assign(base, extra || {});
  }

  async function call(path, options) {
    options = options || {};
    var response = await fetch(path, {
      method: options.method || "GET",
      headers: options.form ? authOnly() : headers(),
      body: options.body
    });
    var text = await response.text();
    var data = null;
    if (text) {
      try { data = JSON.parse(text); }
      catch (e) {
        throw new Error("Unreadable response from " + path + " (HTTP " + response.status + ")");
      }
    }
    if (!response.ok) {
      if (response.status === 401) signIn();
      throw new Error((data && (data.message || data.detail)) || ("HTTP " + response.status));
    }
    return data;
  }

  function authOnly() {
    var out = {};
    var value = token();
    if (value) out["X-Harald-Token"] = value;
    return out;
  }

  function signIn() {
    if (window.Harald && window.Harald.signIn) window.Harald.signIn();
    else window.location.href = "/opportunities";
  }

  window.FRP = {
    /* library */
    listLibrary: function (options) {
      options = options || {};
      var params = new URLSearchParams();
      if (options.status && options.status !== "all") params.set("status", options.status);
      if (options.q) params.set("q", options.q);
      if (options.limit) params.set("limit", options.limit);
      return call("/api/library?" + params.toString());
    },
    libraryStats: function () {
      return call("/api/library/stats").then(function (stats) {
        // The Studio's rail reads total_docs / total_chunks / by_status.
        return {
          total_docs: stats.total_docs,
          total_chunks: stats.total_chunks,
          by_status: stats.by_status
        };
      });
    },
    getDoc: function (docId) {
      return call("/api/docs/" + encodeURIComponent(docId)).then(function (doc) {
        return {
          doc_id: doc.doc_id, filename: doc.filename, deal_status: doc.outcome,
          chunk_count: doc.chunk_count, size_bytes: doc.size_bytes
        };
      });
    },
    uploadToLibrary: function (file, dealStatus) {
      var form = new FormData();
      form.append("file", file);
      form.append("deal_status", dealStatus || "in_progress");
      return call("/api/library/upload", { method: "POST", body: form, form: true });
    },
    startScan: function () {
      // The library is populated by upload rather than a bucket crawl, so there is
      // no background scan job to schedule. Report that honestly instead of
      // pretending a job was queued.
      return Promise.resolve({
        job_name: "UPLOAD",
        status: "Library is loaded by upload. Use the upload control in the rail."
      });
    },

    /* proposals (an opportunity in HARALD) */
    createProposal: function (clientName, rfpDocId, dueDate) {
      return call("/api/proposals", {
        method: "POST",
        body: JSON.stringify({ client_name: clientName, rfp_doc_id: rfpDocId, due_date: dueDate })
      });
    },
    getProposal: function (id) { return call("/api/proposals/" + encodeURIComponent(id)); },
    listProposals: function (n) { return call("/api/proposals?limit=" + (n || 100)); },
    updateProposal: function (id, payload) {
      return call("/api/proposals/" + encodeURIComponent(id), {
        method: "PUT", body: JSON.stringify(payload || {})
      });
    },
    attachDoc: function (id, docId, role) {
      return call("/api/proposals/" + encodeURIComponent(id) + "/attach", {
        method: "POST", body: JSON.stringify({ doc_id: docId, role: role })
      });
    },
    generateProposal: function (id) {
      return call("/api/proposals/" + encodeURIComponent(id) + "/generate", {
        method: "POST", body: "{}"
      });
    },

    /* solicitation parsing and the assistant */
    parseRfp: function (docId) {
      return call("/api/rfp/parse", {
        method: "POST", body: JSON.stringify({ doc_id: docId })
      });
    },
    copilotAsk: function (payload) {
      return call("/api/copilot", {
        method: "POST",
        body: JSON.stringify({
          message: (payload && payload.message) || "",
          conversationId: payload && payload.conversationId
        })
      }).then(function (result) {
        return { reply: result.reply, conversation_id: result.conversation_id };
      });
    }
  };

  console.log("[FRP] bridge loaded, pointing at the HARALD container");
})();
