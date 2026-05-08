# Wiring the real APEX user identity into the iframe

The Studio Copilot stores conversation history per `user_id`. Right now
the iframe falls back to `'unknown@iteria.us'` because APEX hasn't told
the iframe who's logged in. That means every chat from every user
currently lands in the same "unknown" thread.

## The fix — one character in the iframe src

In APEX → Page Designer → your Studio page → the static-content
region that contains the iframe.

**Find the region's source HTML**:

```html
<iframe src="#APP_FILES#FRP_Studio_v5_apex.html"
        allow="clipboard-read;clipboard-write"></iframe>
```

**Change to**:

```html
<iframe src="#APP_FILES#FRP_Studio_v5_apex.html?user_id=&APP_USER."
        allow="clipboard-read;clipboard-write"></iframe>
```

Save → Run → hard-refresh.

That's it. The HTML already has fallback logic in `getCopilotUserId()`
that reads the `?user_id=` query parameter. APEX substitutes
`&APP_USER.` with the logged-in user's identifier (typically their
email if SSO is set up that way) at render time, so each user lands
in their own chat thread automatically.

## Verifying it worked

1. Run the page logged in as yourself
2. Open the chat sidebar → send a message
3. Check the DB:

```sql
SELECT user_id, conversation_id, message_count
  FROM iteria_ai.frp_copilot_conversations
 ORDER BY updated_at DESC FETCH FIRST 3 ROWS ONLY;
```

You should see your real APEX username (or email) in `user_id`,
not `unknown@iteria.us`.

## If APEX uses a UUID or numeric ID instead of email

`&APP_USER.` returns whatever APEX has as the authenticated user
identifier — usually the email, but could be a username or numeric ID
depending on your authentication scheme. Whatever it is, that's the
key the copilot uses. If you need it to match the iteria email
specifically (e.g., for `FRP_USER` Anthropic-key lookups), add a
mapping in your APEX page's `Pre-Rendering` process:

```plsql
APEX_UTIL.SET_SESSION_STATE('APP_USER',
  -- map the auth user to their iteria email here
  apex_authentication.get_username || '@iteria.us');
```

Or pass the email as a separate query param and update
`getCopilotUserId()` in the HTML to read it.

## Why a query param instead of accessing parent window

The iframe and APEX are same-origin (both served from the same ORDS
host), so technically `window.parent` access would work. But query
params are more robust:

- Works whether the page is iframed or opened directly
- Survives page reload / bookmark
- Visible in browser DevTools so you can verify the value passed
- No flaky "is the parent ready yet" timing issues
