# Account Setup: Telegram, Meta, Twitch

This guide fills in every missing `.env` key and ends with a passing `python scripts/cp3_dry_run.py`.
Do it in this order:

1. Telegram (~10 min)
2. Meta (~1 h)
3. Twitch (~10 min)
4. `.env` and the dry run
5. Then **2e Go Live** in the Meta app, before 19 Nov.

Never paste a token into a chat, an issue or a commit. Tokens belong in `.env` only, which is
gitignored, and this repository is public.

---

## 1. Telegram (no permissions or scopes)

1. In Telegram, open **@BotFather** (the one with the blue tick) and send `/newbot`.
   - Display name: anything.
   - Username: must end in `bot`.
2. BotFather replies with a token like `123456789:AA...`. That is `TELEGRAM_BOT_TOKEN`.
3. Optional: send `/setjoingroups`, pick the bot and choose **Disable**, so nobody can add it to a
   group. The pipeline ignores button taps from any chat but yours anyway.
4. Open your bot's chat, press **Start**, and send any message.
5. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates` and find
   `"chat":{"id":123456789`. That number is `TELEGRAM_CHAT_ID` (a private chat's ID is positive).
   If `result` is `[]`, send another message and refresh.
   - Do this **before** you run the runner or the dry run. Both read pending updates, so the message
     would be gone.

The bot can only message you after you've pressed Start.

---

## 2. Meta

### 2a. Instagram professional account
In the IG app: Profile → ☰ → **Account type and tools** → **Switch to professional account**. Pick a
category such as Video Creator, then choose **Business**.

### 2b. Facebook Page, linked to the Instagram account
1. Create the Page from your personal Facebook account at <https://www.facebook.com/pages/create>.
2. If you're asked about a business portfolio, skip it. Pages managed through a business portfolio
   need extra `ads_*` permissions.
3. On desktop, switch into the Page: **Settings & privacy → Settings → Linked accounts → Instagram →
   Connect account**.
   - Linking only in Accounts Center is **not** enough for the API.
   - The dry run fails `ig_linked_to_page` until this is done.

The person who creates the Page has full control of it. That covers the `CREATE_CONTENT` task
(Reels) and the `ANALYZE` task (Reel insights).

### 2c. Developer account and app
1. At <https://developers.facebook.com>, choose **Get started** and register as a developer.
2. **My Apps → Create app**:
   - **Name:** leave out *Facebook, Instagram, Meta, FB, IG* and *Insta*; Meta rejects names
     containing them.
   - **Use cases:** choose **Manage messaging & content on Instagram** and **Manage everything on
     your Page**. If the screen won't combine them, choose **Other → Business** instead.
   - **Business portfolio:** "I don't want to connect a business portfolio yet".
3. **Use cases → Manage messaging & content on Instagram → Customize → API setup with Facebook login
   → Add all required permissions**. Then add `instagram_manage_insights`.
4. **Use cases → Manage everything on your Page → Customize**: make sure every `pages_*` permission
   in the table below and `read_insights` are added.
5. **App settings → Basic** (Meta needs these before 2e):
   - **Privacy Policy URL:** `https://github.com/Manas236/GTA6-Creativity/blob/main/PRIVACY.md`
   - **User data deletion → Data deletion instructions URL:**
     `https://github.com/Manas236/GTA6-Creativity/blob/main/PRIVACY.md#data-deletion`
   - **Category:** e.g. Entertainment.
   - **App icon:** 1024×1024.
   - If Meta won't accept a GitHub page, use the raw file instead:
     `https://raw.githubusercontent.com/Manas236/GTA6-Creativity/main/PRIVACY.md`

#### The nine permissions (all on one token)

| Permission | What the pipeline uses it for |
|---|---|
| `instagram_basic` | IG account lookup, recent `/media` (recovery after a crash) |
| `instagram_content_publish` | IG container → rupload → `media_publish` |
| `instagram_manage_insights` | IG metric snapshots at 24 h / 72 h |
| `business_management` | added automatically by the Instagram use case; needed if the Page is ever in a business portfolio |
| `pages_show_list` | `me/accounts`; required by Page Reels publishing |
| `pages_read_engagement` | required by IG publishing and by Page Reels publishing |
| `pages_manage_posts` | Facebook Page Reels (`video_reels` start / finish) |
| `read_insights` | Facebook Reel metric snapshots (`video_insights`) |
| `pages_manage_engagement` | Facebook Reel metric snapshots (`video_insights`) |

- Only add `ads_management` and `ads_read` if a publish error asks for them. Meta requires them
  only when your Page access comes through a business portfolio role.
- Meta's use-case page spells the publish permission `instagram_content_publishing`. The name the
  Explorer shows, and the dry run checks, is `instagram_content_publish`.
- The list lives in code as `REQUIRED_SCOPES` in [scripts/cp3_dry_run.py](scripts/cp3_dry_run.py).

### 2d. The token
1. Open the [Graph API Explorer](https://developers.facebook.com/tools/explorer/). Select your app,
   choose **User Token**, add all nine permissions, then **Generate Access Token**.
2. In the login dialog, **tick your new Page and your Instagram account**. If you've run it before,
   use **Edit previous settings** to get the selection screen back.
3. Paste that token into the [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/),
   click **Debug**, then **Extend Access Token** at the bottom. This gives a 60-day user token.
4. Put the long-lived token back in the Explorer's **Access Token** field and run:
   `me/accounts?fields=id,name,access_token,instagram_business_account{id,username}`
5. From your Page's entry, copy:
   - `id` → `FB_PAGE_ID`
   - `instagram_business_account.id` → `IG_USER_ID` (a long number, not the @handle)
   - `access_token` → `IG_ACCESS_TOKEN`

   If `instagram_business_account` is missing, either 2b step 3 wasn't done or the IG account
   wasn't ticked in step 2.
6. Paste the Page token into the debugger. Expect **Type: Page**, **Expires: Never** and all nine
   scopes.

**One token covers both platforms. Leave `FB_PAGE_ACCESS_TOKEN` blank.** Page Reels need a Page
token, and with Facebook Login, Instagram publishing also uses one. Only set `FB_PAGE_ACCESS_TOKEN`
if you deliberately split the permissions over two Page tokens for the same Page. The dry run then
checks the Instagram token for `instagram_*`, `business_management`, `pages_show_list` and
`pages_read_engagement`, and the Facebook token for the five `pages_*` / `read_insights`
permissions.

**Keeping the token alive**
- If the debugger shows a **Data access expires** date, set a reminder a week before it and redo 2d.
  It takes about 5 minutes.
- Changing your Facebook password, removing the app, or losing admin on the Page also kills the
  token.
- The pipeline alerts on Telegram when Meta refuses it.

### 2e. Go Live (after section 4 passes, before 19 Nov)
While the app is in Development mode, Meta shows its posts only to people with a role on the app.
In practice, nobody but you would see the Reels.

1. Make sure everything in 2c step 5 is filled in (privacy policy URL, data deletion URL, category,
   icon).
2. Switch the app from **Development** to **Live** (the App Mode toggle, or **Publish** in the newer
   dashboard).
3. Keep every permission at **Standard Access**. Don't request Advanced Access or submit App Review.
   If going Live says any of the nine needs App Review, **stop and report what it says**. Meta's
   docs don't clearly cover this case for Business apps.
4. Run `python scripts/cp3_dry_run.py` again and confirm the Meta rows still pass.
5. Anything posted while in Development mode becomes public once the app is Live. Still, check the
   first real post in a logged-out browser window.

---

## 3. Twitch (no scopes)

1. Turn on two-factor authentication: twitch.tv → **Settings → Security and Privacy**. Twitch won't
   let you register an app without it.
2. At <https://dev.twitch.tv/console>, choose **Applications → Register Your Application**:
   - **Name:** must be unique and can't contain "Twitch".
   - **OAuth Redirect URLs:** `http://localhost`. The pipeline never uses it (it uses client
     credentials), but the field is required.
   - **Category:** Application Integration.
   - **Client Type:** **Confidential**.
3. Click **Manage**:
   - Copy the **Client ID** into `TWITCH_CLIENT_ID`.
   - Click **New Secret** and copy it straight into `TWITCH_CLIENT_SECRET`. It's shown once, and
     making a new one cancels the old one.
4. Test it now. Twitch only switches itself on at 2026-11-19 UTC. From the project folder:
   ```
   python -c "import requests; from pipeline import config as c; i,s=c.secret('TWITCH_CLIENT_ID'),c.secret('TWITCH_CLIENT_SECRET'); t=requests.post('https://id.twitch.tv/oauth2/token',data={'client_id':i,'client_secret':s,'grant_type':'client_credentials'}).json()['access_token']; print(requests.get('https://api.twitch.tv/helix/games',params={'name':'Grand Theft Auto VI'},headers={'Client-Id':i,'Authorization':'Bearer '+t}).json())"
   ```
   - Expect `{'data': [{'id': '...', 'name': 'Grand Theft Auto VI', ...}]}`.
   - `'data': []` means the category name on Twitch differs from `sources.twitch.game_name` in
     `config.yaml`.

---

## 4. `.env` and the dry run

```
TELEGRAM_BOT_TOKEN=...       # section 1
TELEGRAM_CHAT_ID=...         # section 1
IG_USER_ID=...               # 2d
IG_ACCESS_TOKEN=...          # 2d: the PAGE token
FB_PAGE_ID=...               # 2d
FB_PAGE_ACCESS_TOKEN=        # leave blank
TWITCH_CLIENT_ID=...         # section 3
TWITCH_CLIENT_SECRET=...     # section 3
```
Use no quotes and no spaces around `=`. Then run:

```
python scripts/cp3_dry_run.py
```

It has passed when:
- one approval card arrives in Telegram;
- the summary shows `debug_token ... type=PAGE, valid=True, expires=never, ... 9/9 required permissions`,
  then `ig_user`, `ig_publishing_limit`, `fb_page`, `ig_linked_to_page` and `posting_dry_run`, all
  `OK`;
- it exits with code 0.

Any problem below makes it exit with code 1:

| FAIL line says | Fix |
|---|---|
| `debug_token ... MISSING n permission(s): ...` (each also listed on its own line) | Add them to the app (2c), then make a new token from 2d step 1 |
| `... a USER token, not a PAGE token` | You copied the user token. Use the Page's `access_token` from `me/accounts` (2d steps 4-5) |
| `... a token for Page X, not for FB_PAGE_ID Y` | The token and `FB_PAGE_ID` belong to different Pages |
| `... the token is not valid` | Make a new token (2d) |
| `ig_linked_to_page ... has no Instagram account linked` | 2b step 3, or tick the IG account in 2d step 2 |
| `ig_linked_to_page ... linked to Instagram account X, but IG_USER_ID is Y` | Set `IG_USER_ID` to X |
| `telegram ... missing from .env` | Section 1 |

Nothing is published by the dry run. When it passes, do **2e Go Live**.

---

## Sources (Meta docs, read 2026-11-10)
- [Video Insights](https://developers.facebook.com/docs/graph-api/reference/video/video_insights/): requires `read_insights` and `pages_manage_engagement`, and a Page token from someone with the `ANALYZE` task
- [Reels Publishing](https://developers.facebook.com/docs/video-api/guides/reels-publishing): a Page token with `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`; 3-90 s, 9:16, 30 API posts per 24 h
- [Instagram content publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing): with Facebook Login, a Page access token with `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`
- [App modes](https://developers.facebook.com/docs/development/build-and-test/app-modes): "Any data generated while an app is in Development mode, such as test posts, can only be seen by role users."
- [Instagram use case setup](https://developers.facebook.com/documentation/development/create-an-app/instagram-use-case)
