# Studio Copilot — implementation spec

Right-sidebar chat agent in FRP Studio. Answers questions about the
current proposal, searches the library, can modify the form on the
user's behalf with confirmation. Built for Jess/Brian — non-technical
users who otherwise interrupt the dev for everything.

**Status:** spec only. Nothing built. Pick up cold from C1.

---

## Architecture decisions (locked)

- **Default model:** OCI Grok (`oci.grok_ocid` already in FRP_CONFIG).
  Same auth as embed/parse — `OCI$RESOURCE_PRINCIPAL`, no per-user keys.
- **Override:** If `FRP_USER.has_anthropic_key(user_id)` returns TRUE,
  use Claude via Anthropic API. Decrypt via `FRP_VAULT.decrypt_key`.
- **UI position:** Right sidebar, collapsible. Default collapsed; click
  the floating chat icon to expand.
- **Capabilities:** Read **and** write. Copilot can suggest form-field
  changes, attach/detach library docs, and propose section rewrites.
  All write actions surface as a "Apply / Reject" button in the chat
  before they touch the form.
- **Conversation persistence:** Per-user, per-proposal. Survives reload.
- **Token budget:** 8K context window for retrieved chunks + form state
  + conversation history. Truncate oldest history first.

---

## Pieces (test each independently — same pattern as B1-B4)

### C1 — `FRP_COPILOT.ask` core function
**Risk:** High (first agentic function, JSON tool-call protocol)
**Time:** 2-3 hours

**Spec:**
```sql
FUNCTION ask(
  p_user_id      IN VARCHAR2,
  p_message      IN CLOB,
  p_form_state   IN CLOB,         -- JSON snapshot of the form
  p_history_json IN CLOB DEFAULT NULL,  -- last N {role,content} turns
  p_conversation_id IN VARCHAR2 DEFAULT NULL
) RETURN CLOB;  -- JSON {answer, citations[], tool_calls[], usage}
```

**What it does:**
1. Embed `p_message` via `FRP_INGEST.embed_one`
2. Vector-search `FRP_CHUNKS` for top-8 relevant chunks (excluding
   `no_bid` / `tool_output`)
3. Build chat prompt:
   - System: "You are FRP Studio's assistant. Help the user with their
     current proposal. Cite library docs by doc_id when relevant. If
     you want to modify the form, emit a tool_call JSON block."
   - Form state injected as JSON
   - Top-K chunks injected as `<library>` excerpts with doc_ids
   - Conversation history (oldest first, truncated to fit budget)
   - User's latest message
4. Pick model:
   - If `FRP_USER.has_anthropic_key(p_user_id)`: Claude via Anthropic
     API (use the user's decrypted key)
   - Else: Grok via OCI `/chat` endpoint (same pattern as `parse_rfp`)
5. Parse response — split into:
   - Plain answer text
   - Citations (chunk doc_ids the model referenced)
   - Tool calls (if any) — JSON blocks the model emitted
6. Record usage in `FRP_USER.record_usage`
7. Return JSON

**Test criteria:**
- "What's our standard payroll module pricing?" → answer cites at least
  one boilerplate chunk
- "What did I put for client name?" → quotes form_state value back
- "Make the scope summary more conservative" → returns tool_call
  suggesting an updated scope_summary value
- Same query with anthropic_key set → uses Claude, response logged with
  `provider=anthropic` in `user_api_usage`

---

### C2 — Conversation persistence
**Risk:** Low
**Time:** 1 hour

**Schema additions** (`apex/10_copilot_schema.sql`):
```sql
CREATE TABLE FRP_COPILOT_CONVERSATIONS (
  conversation_id VARCHAR2(64) DEFAULT LOWER(SYS_GUID()) NOT NULL,
  user_id         VARCHAR2(128) NOT NULL,
  proposal_id     VARCHAR2(64),  -- nullable; chat can be standalone
  title           VARCHAR2(200),
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT pk_copilot_conv PRIMARY KEY (conversation_id)
);

CREATE TABLE FRP_COPILOT_MESSAGES (
  message_id      VARCHAR2(64) DEFAULT LOWER(SYS_GUID()) NOT NULL,
  conversation_id VARCHAR2(64) NOT NULL,
  role            VARCHAR2(16) NOT NULL,  -- user | assistant | tool
  content         CLOB,
  citations_json  CLOB,  -- array of doc_ids
  tool_calls_json CLOB,  -- array of pending tool calls
  applied_json    CLOB,  -- which tool calls were applied (after user approval)
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT pk_copilot_msg  PRIMARY KEY (message_id),
  CONSTRAINT fk_copilot_conv FOREIGN KEY (conversation_id)
              REFERENCES FRP_COPILOT_CONVERSATIONS(conversation_id) ON DELETE CASCADE
);

CREATE INDEX ix_copilot_msg_conv ON FRP_COPILOT_MESSAGES(conversation_id, created_at);
```

**Procs in FRP_COPILOT package:**
- `save_message(conversation_id, role, content, citations, tool_calls)`
- `load_history(conversation_id, limit) RETURN CLOB` — JSON array
- `start_conversation(user_id, proposal_id, first_message) RETURN VARCHAR2`
- `list_conversations(user_id, limit) RETURN CLOB` — JSON array

---

### C3 — Tool definitions (the "write" capability)
**Risk:** Medium (JSON tool-call dispatch is finicky)
**Time:** 2-3 hours

**Tools the copilot can request:**

| Tool name | What it does | Confirmation? |
|---|---|---|
| `set_form_field` | `{field_id, value}` — set a single form field | Yes |
| `set_form_fields` | `{updates: [{field_id, value}, ...]}` — bulk set | Yes |
| `attach_doc` | `{doc_id}` — add a library doc to attached list | Yes |
| `detach_doc` | `{doc_id}` — remove an attached doc | Yes |
| `find_docs` | `{query, top_k, status_filter}` — search library | No (read) |
| `get_doc_text` | `{doc_id, max_chars}` — fetch raw_text excerpt | No (read) |
| `rewrite_text` | `{section_id, instruction}` — rewrite a deliverable section | Yes |

**Prompt addition** to teach the model to emit tool calls:
```
When you want to modify the form, attachments, or rewrite content,
emit a JSON block like:
<tool_call>{"tool":"set_form_field","args":{"field_id":"client-name","value":"City of Madison"}}</tool_call>
The user will see your suggested change and click Apply or Reject.
```

**Backend dispatcher:** PL/SQL parses `<tool_call>` blocks from the
model's response and returns them as structured JSON in the API
response. The frontend renders them as Apply/Reject buttons.

**Read-only tools** (find_docs, get_doc_text) can be auto-resolved
inside the same `ask()` call — model emits tool_call, dispatcher
executes immediately, result fed back into a second model call. (One
round-trip, transparent to the user.)

---

### C4 — ORDS endpoints
**Risk:** Low (boilerplate)
**Time:** 1 hour

```
POST /copilot/ask
  Body: {message, conversation_id?, form_state, proposal_id?}
  Returns: {ok, message_id, answer, citations[], tool_calls[], usage}

POST /copilot/conversations
  Body: {proposal_id?, title?}
  Returns: {ok, conversation_id}

GET  /copilot/conversations
  Returns: {ok, conversations: [{id, title, updated_at, last_message_preview}]}

GET  /copilot/conversations/:id/messages
  Returns: {ok, messages: [{role, content, citations, tool_calls, applied, created_at}]}

DELETE /copilot/conversations/:id
  Returns: {ok}

POST /copilot/messages/:id/apply_tool_calls
  Body: {applied: [tool_call_index, ...]}  -- which the user approved
  Returns: {ok, results: [{tool, status, error?}]}
```

User auth: same `api_key` pattern as the rest of `/frp-hooks/*`.

---

### C5 — Frontend (right sidebar UI)
**Risk:** Medium (CSS/UX detail)
**Time:** 3-4 hours

**Layout:**
- Floating chat button bottom-right (fixed position, ~48px circle)
- Click → slides out a 400px-wide right drawer
- Drawer contents:
  - Header: conversation title + close button + new-chat button
  - Conversation list (collapsible)
  - Message thread (scrollable, auto-scroll on new message)
  - Tool-call cards inline with messages: "Copilot wants to set Client name to 'City of Madison'. [Apply] [Reject]"
  - Citations as small pills under assistant messages, click to scroll the library rail to that doc
  - Input box at bottom + send button
  - Loading state ("Copilot is thinking…") with spinner

**Form scraping helper** (shared with B4):
```js
function scrapeFormState() {
  return {
    client_name: document.getElementById('frp-field-client-name')?.value,
    industry:    document.getElementById('frp-field-industry')?.value,
    contact:     document.getElementById('frp-field-contact')?.value,
    budget:      document.getElementById('frp-field-budget')?.value,
    erp:         document.getElementById('frp-field-erp')?.value,
    rfp_number:  document.getElementById('frp-field-rfp-number')?.value,
    due_date:    document.getElementById('frp-field-due-date')?.value,
    attachments: Array.from(_frpAttached)
  };
}
```

**Tool-call apply handler:**
```js
function applyToolCall(tc) {
  switch (tc.tool) {
    case 'set_form_field': document.getElementById('frp-field-' + tc.args.field_id).value = tc.args.value; break;
    case 'attach_doc':     attachAndParse(tc.args.doc_id, ...); break;
    case 'detach_doc':     removeAttachment(tc.args.doc_id); break;
    // ...
  }
  // notify backend that user approved
  FRP.applyToolCalls(messageId, [tc.index]);
}
```

---

### C6 — Claude override path
**Risk:** Medium (cross-provider auth, JSON shape differs)
**Time:** 1-2 hours

**Flow inside `FRP_COPILOT.ask`:**
1. Check `FRP_USER.has_anthropic_key(p_user_id)`.
2. If TRUE: `l_key := FRP_USER.get_anthropic_key(p_user_id);` then call
   Anthropic Messages API directly via `DBMS_CLOUD.SEND_REQUEST`
   (`https://api.anthropic.com/v1/messages`, header
   `x-api-key: <l_key>`, header `anthropic-version: 2023-06-01`).
3. If FALSE or call fails: fall back to Grok via OCI.
4. After call: `FRP_USER.record_usage(p_user_id, provider, model,
   action_desc, context_ref, input_tokens, output_tokens, cost_usd)`.
5. `FRP_USER.touch_last_used(p_user_id)` so the user-profile UI can
   show "last used X minutes ago".

**Anthropic request shape** (different from OCI's GenericChatRequest):
```json
{
  "model": "claude-opus-4-7",
  "max_tokens": 1500,
  "system": "...",
  "messages": [{"role":"user","content":"..."}, ...]
}
```

Response shape: `{content: [{type:"text", text:"..."}, ...], usage: {input_tokens, output_tokens}}`.

**Network:** Anthropic's API is on the public internet. ADB needs an
ACL / network allow rule for `api.anthropic.com`. If
`DBMS_CLOUD.SEND_REQUEST` errors with `ORA-29024` or similar on first
test, that's the reason — fix is a one-shot
`DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE` call as ADMIN.

---

## Suggested order of work

1. **C2 schema** first (10 min) — so C1 can write to the tables as it builds.
2. **C1 with Grok only**, no tools yet — prove the chat → retrieve → answer loop works.
3. **C3 read-only tools** (`find_docs`, `get_doc_text`) — proves tool dispatch.
4. **C4 endpoints** + a curl test.
5. **C5 frontend MVP** — read-only chat with citations.
6. **C3 write tools** + Apply/Reject UI.
7. **C6 Claude override** last (so the Grok path is solid before adding a fallback).

After step 5 you have a usable read-only copilot. Steps 6-7 are
incremental from there.

---

## Open questions for the user before starting

1. **Identity** — `p_user_id` should be what? APEX session user? An
   email? A UUID? Affects FRP_USER queries. Right now FRP_USER takes
   `VARCHAR2` user_id with no enforced format.
2. **Conversation scope** — one conversation per proposal, or many?
   Spec assumes many (proposal_id is nullable).
3. **History budget** — keep the last N turns? N tokens? Default I'll
   use: last 10 turns capped at 4000 tokens.
4. **Confirmation UI flair** — show diffs ("client_name: '' →
   'Madison'") or just "set client_name to 'Madison'"? Diffs nicer for
   set_form_field; not applicable for attach_doc.

---

## What's NOT in scope (intentionally)

- File upload from chat ("here's a new RFP, parse it") — use the
  existing library-click flow instead
- Multi-user / multiplayer chat — single-user per conversation
- Streaming responses — return complete answers (simpler; can add SSE later)
- Voice input
- Mobile-specific layout

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| OCI Grok response shape differs from sample we tested with chat | Medium | First task in C1: smoke-test Grok with a simple "say hello" before building the prompt machinery |
| Tool-call JSON parsing brittle (model emits malformed JSON) | Medium | Validate with `JSON_VALUE`; on parse failure, treat as plain text and skip tool dispatch |
| Anthropic API ACL not granted on the ADB | Medium | Document the ACL fix in C6; expect first call to fail and prompt user for ADMIN access |
| Cost: Grok calls add up if every form interaction triggers retrieval | Low | Only call ask() when user sends a message; no implicit calls |
| Right-sidebar layout fights with the existing 3-column grid | Low | Use position:fixed + transform; doesn't reflow main layout |

---

## Estimated total

**MVP (steps 1-5):** ~7 hours, single focused session
**Full read+write (steps 1-7):** ~12 hours, two sessions

If picking up cold tomorrow, start with C2 schema + C1 Grok smoke test.
That's ~30 min to first working response.
