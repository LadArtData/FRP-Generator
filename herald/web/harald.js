/* HARALD shared client: session, API, nav, and small view helpers.
 * Loaded by every workspace. The FRP Studio uses frp-rest-bridge.js, which
 * delegates its auth to this same session store. */
(function (global) {
  "use strict";

  var TOKEN_KEY = "harald.token";
  var USER_KEY = "harald.user";

  var session = {
    token: function () { try { return global.sessionStorage.getItem(TOKEN_KEY); } catch (e) { return null; } },
    user: function () {
      try { return JSON.parse(global.sessionStorage.getItem(USER_KEY) || "null"); }
      catch (e) { return null; }
    },
    set: function (data) {
      try {
        global.sessionStorage.setItem(TOKEN_KEY, data.token);
        global.sessionStorage.setItem(USER_KEY, JSON.stringify({
          username: data.username, display_name: data.display_name, role: data.role
        }));
      } catch (e) { /* storage unavailable: the session lives for this page only */ }
    },
    clear: function () {
      try {
        global.sessionStorage.removeItem(TOKEN_KEY);
        global.sessionStorage.removeItem(USER_KEY);
      } catch (e) { /* nothing to clear */ }
    },
    is: function (role) {
      var user = session.user();
      var rank = { contributor: 1, reviewer: 2, approver: 3 };
      return !!user && (rank[user.role] || 0) >= (rank[role] || 99);
    }
  };

  function headers(extra) {
    var base = { "Content-Type": "application/json" };
    var token = session.token();
    if (token) base["X-Harald-Token"] = token;
    return Object.assign(base, extra || {});
  }

  async function request(path, options) {
    options = options || {};
    var response = await fetch(path, {
      method: options.method || "GET",
      headers: headers(options.headers),
      body: options.body
    });
    return handle(response, path);
  }

  async function upload(path, formData) {
    var init = { method: "POST", body: formData, headers: {} };
    var token = session.token();
    if (token) init.headers["X-Harald-Token"] = token;
    return handle(await fetch(path, init), path);
  }

  async function handle(response, path) {
    var text = await response.text();
    var data = null;
    if (text) {
      try { data = JSON.parse(text); }
      catch (e) { throw new Error("Unreadable response from " + path + " (HTTP " + response.status + ")"); }
    }
    if (!response.ok) {
      if (response.status === 401) {
        session.clear();
        signInDialog();
      }
      var message = (data && (data.message || data.detail)) || ("HTTP " + response.status);
      var error = new Error(message);
      error.status = response.status;
      error.detail = data && data.detail;
      throw error;
    }
    return data;
  }

  var api = {
    get: function (path) { return request(path); },
    post: function (path, body) { return request(path, { method: "POST", body: JSON.stringify(body || {}) }); },
    put: function (path, body) { return request(path, { method: "PUT", body: JSON.stringify(body || {}) }); },
    patch: function (path, body) { return request(path, { method: "PATCH", body: JSON.stringify(body || {}) }); },
    upload: upload,
    download: function (path) {
      // Blob endpoints need the token too, so fetch and hand the browser a blob URL.
      return fetch(path, { headers: headers() }).then(function (response) {
        if (!response.ok) throw new Error("Download failed (HTTP " + response.status + ")");
        var disposition = response.headers.get("Content-Disposition") || "";
        var match = /filename="([^"]+)"/.exec(disposition);
        return response.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var link = document.createElement("a");
          link.href = url;
          link.download = match ? match[1] : "download";
          document.body.appendChild(link);
          link.click();
          link.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
        });
      });
    }
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function bytes(size) {
    if (!size) return "";
    var units = ["B", "KB", "MB", "GB"];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return size.toFixed(size < 10 && index > 0 ? 1 : 0) + " " + units[index];
  }

  function modal(html, onOpen) {
    close();
    var backdrop = document.createElement("div");
    backdrop.className = "h-modal-bg";
    backdrop.id = "h-modal";
    backdrop.innerHTML = '<div class="h-modal">' + html + "</div>";
    backdrop.addEventListener("click", function (event) {
      if (event.target === backdrop) close();
    });
    document.body.appendChild(backdrop);
    var first = backdrop.querySelector("input, textarea, select");
    if (first) setTimeout(function () { first.focus(); }, 40);
    if (onOpen) onOpen(backdrop);
    return backdrop;
  }

  function close() {
    var existing = document.getElementById("h-modal");
    if (existing) existing.remove();
  }

  function toast(message, kind) {
    var node = document.createElement("div");
    node.className = "h-toast" + (kind ? " " + kind : "");
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(function () { node.classList.add("out"); }, 3600);
    setTimeout(function () { node.remove(); }, 4200);
  }

  async function signInDialog() {
    var users = [];
    try { users = await fetch("/api/users").then(function (r) { return r.json(); }); }
    catch (e) { users = []; }

    var options = users.map(function (u) {
      return '<option value="' + escapeHtml(u.username) + '" data-role="' + u.role + '">' +
        escapeHtml(u.display_name || u.username) + " (" + u.role + ")</option>";
    }).join("");

    modal(
      '<div class="h-modal-head">Sign in to HARALD</div>' +
      '<div class="h-modal-body">' +
      '<div class="h-field"><label>Who are you?</label>' +
      '<select class="h-input" id="h-user">' + options + "</select></div>" +
      '<div class="h-hint">Pick your name. Pricing and final approval stay with ' +
      "the approver role — no extra passphrase.</div></div>" +
      '<div class="h-modal-foot"><button class="h-btn" id="h-signin">Sign in</button></div>',
      function (root) {
        var select = root.querySelector("#h-user");
        root.querySelector("#h-signin").addEventListener("click", async function () {
          var button = root.querySelector("#h-signin");
          button.disabled = true;
          button.textContent = "Signing in...";
          try {
            var result = await fetch("/api/signin", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ username: select.value })
            }).then(async function (response) {
              var data = await response.json();
              if (!response.ok) throw new Error(data.message || "Sign-in failed");
              return data;
            });
            session.set(result);
            close();
            global.location.reload();
          } catch (error) {
            button.disabled = false;
            button.textContent = "Sign in";
            toast(error.message, "error");
          }
        });
      }
    );
  }

  function nav(active) {
    var user = session.user();
    var links = [
      ["/", "Drafting"],
      ["/opportunities", "Bids &amp; Compliance"],
      ["/answers", "Answer Library"],
      ["/packages", "Packages"],
      ["/admin", "Admin"]
    ];
    var items = links.map(function (link) {
      var on = link[0] === active ? " on" : "";
      return '<a class="h-navlink' + on + '" href="' + link[0] + '">' + link[1] + "</a>";
    }).join("");

    var right = user
      ? '<span class="h-user" title="' + escapeHtml(user.role) + '">' +
        escapeHtml(user.display_name || user.username) +
        '<span class="h-role ' + user.role + '">' + user.role + "</span></span>" +
        '<button class="h-btn ghost sm" id="h-signout">Sign out</button>'
      : '<button class="h-btn sm" id="h-signin-btn">Sign in</button>';

    return '<div class="h-topbar"><div class="h-logo">FRP <em>Studio</em></div>' +
      items + '<div class="h-spacer"></div>' + right + "</div>";
  }

  function bindNav() {
    var out = document.getElementById("h-signout");
    if (out) {
      out.addEventListener("click", function () {
        session.clear();
        global.location.reload();
      });
    }
    var into = document.getElementById("h-signin-btn");
    if (into) into.addEventListener("click", signInDialog);
  }

  function requireSession() {
    if (!session.token()) {
      signInDialog();
      return false;
    }
    return true;
  }

  global.Harald = {
    session: session, api: api, escapeHtml: escapeHtml, bytes: bytes,
    modal: modal, closeModal: close, toast: toast, nav: nav, bindNav: bindNav,
    signIn: signInDialog, requireSession: requireSession
  };
})(window);
