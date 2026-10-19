# Slack app for the `slack` source

`collect_slack.py` reads bug reports with a Slack **bot token** and posts one thread reply
per filed card. No events, no socket mode, no server: it only calls the Web API when
`/super-collect` runs, so it works under the headless scheduler too.

## 1. Create the app from this manifest

api.slack.com/apps → **Create New App** → **From a manifest** → pick the workspace → paste:

```yaml
display_information:
  name: super-collect
  description: Reads bug reports for the super-board Backlog and replies with the GitHub issue link.
features:
  bot_user:
    display_name: super-collect
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:history   # read messages in public bug channels
      - groups:history     # read messages in private bug channels
      - channels:read      # resolve #names, list the channels the bot is in
      - groups:read        # the same for private channels
      - reactions:read     # see the bug reaction on a message
      - chat:write         # the one thread reply: "Filed as <issue url>"
      - users:read         # show names instead of user IDs (optional: IDs without it)
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

## 2. Install it and keep the token out of git

1. **Install to Workspace** (Install App page) and allow.
2. Copy the **Bot User OAuth Token** (`xoxb-…`) from OAuth & Permissions.
3. Add it to the env file `python3 .claude/skills/super-collect/scripts/collect_common.py secret-file`
   prints (`.env.local` whenever `.env` is tracked by git), confirmed ignored with
   `git check-ignore .env.local`:

   ```
   SLACK_BOT_TOKEN=xoxb-…
   ```

   Never paste the token into chat, the config or a command line. A scheduler or cloud routine
   gets it as an environment variable instead.

## 3. Invite the bot and set the config

1. In each bug channel: `/invite @super-collect`. The reaction scan only sees channels the bot
   is in, so invite it wherever people tag messages with the reaction too.
2. In the project's super-board config:

   ```json
   "collect": {
     "sources": ["sentry", "slack", "github"],
     "slack": {
       "channels": ["#bugs", "C0123ABCD"],
       "reaction": "bug"
     }
   }
   ```

   `channels`: IDs or `#names`; every top-level message there is a report. `reaction`
   (default `bug`, written without colons): a message carrying it in any channel the bot is
   in is a report too. Optional caps: `max_channels` (50 channels in the reaction scan),
   `max_pages` (5 pages of 200 messages per channel), `max_candidates` (50). A capped scan is
   reported `truncated`. `reply: false` turns the thread reply off.
3. Prove it: `python3 .claude/skills/super-collect/scripts/collect_slack.py ping` checks the
   token (`auth.test`) and reads one message from every configured channel.

## Errors

| Message | Fix |
|---|---|
| `Missing SLACK_BOT_TOKEN` | step 2 |
| `SLACK_BOT_TOKEN rejected (invalid_auth)` | copy the current Bot User OAuth Token again |
| `lacks scope <scope>` | add the scope, reinstall the app, copy the new token |
| `not in channel` | `/invite @super-collect` in that channel |
| `channel … not found` | a typo, or a private channel the bot is not in |
