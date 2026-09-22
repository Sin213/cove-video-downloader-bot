# Discord application setup (Cove)

## Privileged intents

Cove watches channel messages for media URLs. Enable **Message Content Intent** on the bot page:

1. Open [Discord Developer Portal](https://discord.com/developers/applications) → your application → **Bot**
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**
3. Save changes

Without this, the process exits immediately with `PrivilegedIntentsRequired`.

Presence and Server Members intents are **not** required.

## Invite

Scopes: `bot` + `applications.commands`

Suggested permission integer: **2147601472**

| Permission | Why |
|---|---|
| View Channels | See channels where links are posted |
| Send Messages | Reply with the downloaded media |
| Embed Links | Occasional rewrite / metadata replies |
| Attach Files | Upload video/audio/images |
| Add Reactions | Progress / status reactions |
| Read Message History | Context for replies |
| Use Application Commands | Slash commands (`/download`, `/audio`, …) |

Friend-server mode also needs **Manage Messages** (add `8192` → **2147610000**) so Cove can delete the original link message.

Example invite URL (replace `CLIENT_ID`):

```
https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=2147601472&scope=bot%20applications.commands
```

## Environment

| Variable | Required | Notes |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token; never commit |
| `GUILD_ID` | yes | Primary guild for slash-command sync |
| `FRIEND_GUILD_ID` | no | Friend-server mode (`0` disables) |
| `COVE_DATA_DIR` | no | Persist cookies/cache/settings (Docker uses `/data`) |
