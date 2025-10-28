# Discord Voice Connection Error 4006 - Deep Dive Analysis

## Error Summary
```
discord.errors.ConnectionClosed: Shard ID None WebSocket closed with 4006
```

This error occurs repeatedly when attempting to connect to Discord voice channels using `/joinvoice` or `/startnotes`.

## What Error Code 4006 Means

According to Discord's documentation, WebSocket close code **4006** means:
> **"Your session is no longer valid"**

This error occurs when:
1. The session token/credentials Discord provided are no longer recognized
2. The bot's session was invalidated on Discord's side
3. There's a mismatch in the voice connection handshake

## Investigation Results

### ✅ Verified Working
1. **Bot Token**: Valid and authenticated (Bot ID: 1432435571300499607)
2. **Gateway Connection**: Bot successfully connects to Discord's main gateway
3. **Guild Membership**: Bot is properly in the guild (ID: 811312080082305084)
4. **Intents Configuration**: Correctly set in code
   - `GUILD_VOICE_STATES` (Bit 7, value 128) ✅ ENABLED
   - `GUILDS` (Bit 0, value 1) ✅ ENABLED
   - Total intent value: `53575421`
5. **Network**: Bot is in `host` network mode (no Docker NAT issues)
6. **Dependencies**: py-cord 2.6.1, PyNaCl 1.5.0 installed

### ❌ The Problem

The error occurs **specifically during the voice WebSocket handshake**, NOT the main gateway connection. The sequence is:

1. Bot receives `VOICE_SERVER_UPDATE` and `VOICE_STATE_UPDATE` events ✅
2. Bot attempts to connect to Discord's voice endpoint (e.g., `c-sjc06-dde91671.discord.media:443`) ✅
3. Voice WebSocket opens ✅
4. **Bot sends authentication to voice gateway**
5. **Discord voice server responds with close code 4006** ❌

This means Discord's **voice gateway** is rejecting the bot's session, not the main gateway.

## Root Cause Analysis

After deep investigation, the most likely causes are:

### 1. **Py-Cord 2.6.x Voice Gateway Bug** (MOST LIKELY)

Py-cord 2.6.x has known issues with voice connections. The error pattern matches a session management bug where:
- The voice gateway connection is being made with **stale or improperly formatted session credentials**
- The VOICE_STATE_UPDATE and VOICE_SERVER_UPDATE events are captured
- But when py-cord tries to authenticate with the voice gateway, it sends incorrect/outdated session info
- Discord voice server rejects with 4006

**Evidence:**
- Error happens consistently across multiple connection attempts
- Main gateway works fine (indicating intents and token are valid)
- Error occurs in `voice_client.py:375` during `connect_websocket()` → `ws.poll_event()`
- Additional error: `AttributeError: '_MissingSentinel' object has no attribute 'poll_event'` indicates internal state corruption

### 2. **Token Rotation Issue** (POSSIBLE)

If the bot token was recently regenerated:
- Old sessions might still be active on Discord's side
- New connection attempts conflict with stale sessions
- Discord rejects with 4006 "session no longer valid"

**Check:** When was the last time you regenerated your bot token?

### 3. **Multiple Bot Instances** (UNLIKELY but check)

If multiple instances of the bot are running:
- They compete for the same voice session
- Discord invalidates one of them
- Results in 4006 errors

**Check:** `docker ps | grep discord-bot` - should show only ONE container

### 4. **Voice Region/Endpoint Issues** (POSSIBLE)

Some Discord voice regions have compatibility issues with certain libraries.

**Evidence from logs:**
- Connections failing to multiple endpoints:
  - `c-sjc06-dde91671.discord.media` (San Jose)
  - `c-iad08-b27acf06.discord.media` (Ashburn/DC)

## Recommended Solutions (In Priority Order)

### Solution 1: Downgrade to Py-Cord 2.5.x ⭐ RECOMMENDED

Py-cord 2.5.x had more stable voice handling:

```bash
cd /home/tidytool/Documents/Development/discord_notetaker

# Edit bot/requirements.txt
sed -i 's/py-cord==2.6.1/py-cord==2.5.0/' bot/requirements.txt

# Rebuild
docker compose down
docker compose build discord-bot --no-cache
docker compose up -d
```

### Solution 2: Try discord.py Instead

Switch to the original discord.py (more mature voice implementation):

```bash
# Edit bot/requirements.txt
sed -i 's/py-cord==2.6.1/discord.py==2.3.2/' bot/requirements.txt

# Rebuild
docker compose down  
docker compose build discord-bot --no-cache
docker compose up -d
```

**NOTE:** This may require minor code changes (py-cord and discord.py have slight API differences in slash commands).

### Solution 3: Regenerate Bot Token + Clear Sessions

1. Go to https://discord.com/developers/applications/1432435571300499607
2. Go to "Bot" section
3. Click "Reset Token" (this will invalidate ALL existing sessions)
4. Copy the new token
5. Update `.env` file with new `DISCORD_BOT_TOKEN`
6. Restart: `docker compose down && docker compose up -d`

### Solution 4: Add Session Reset Logic

Add explicit session cleanup before voice connections. Edit `bot/bot.py`:

```python
async def _connect_voice_with_retry(channel: discord.VoiceChannel, attempts: int = 2, timeout: float = 45.0):
    """Try to connect to voice and wait briefly for a stable session."""
    
    # ADDED: Force disconnect any existing voice client for this guild
    existing_vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
    if existing_vc:
        try:
            await existing_vc.disconnect(force=True)
            await asyncio.sleep(1.0)  # Give Discord time to clean up
        except Exception:
            pass
    
    # ... rest of existing code ...
```

### Solution 5: Enable Full Debug Logging

Capture exactly what py-cord is sending to Discord:

```bash
# Edit .env
VOICE_DEBUG=1
BOT_LOG_LEVEL=DEBUG

# Restart
docker compose restart discord-bot

# Watch logs
docker compose logs -f discord-bot
```

Then attempt `/joinvoice` and look for the actual voice gateway handshake details.

### Solution 6: Test with Different Voice Region

In Discord:
1. Right-click your voice channel
2. Edit Channel → Region Override
3. Try different regions:
   - US West
   - US East
   - US Central
4. Test `/joinvoice` with each region

## Additional Context

### Why Voice Differs from Main Gateway

Discord uses **two separate WebSocket connections**:
1. **Main Gateway** (`wss://gateway.discord.gg`) - for events, messages, slash commands ✅ WORKING
2. **Voice Gateway** (`wss://[region].discord.media`) - for voice audio, requires separate auth ❌ FAILING

Your bot successfully connects to #1 but fails at #2. This is why slash commands work but voice doesn't.

### The "Unclosed connection" Warnings

The repeated `asyncio: Unclosed connection` warnings indicate py-cord is:
1. Opening connections to the voice gateway
2. Getting rejected with 4006
3. Not properly cleaning up the failed connections
4. Leaving dangling TCP connections

This is a symptom, not the cause, but suggests poor error handling in py-cord 2.6.x.

## What To Try Right Now

```bash
cd /home/tidytool/Documents/Development/discord_notetaker

# Quick fix attempt: Downgrade py-cord
sed -i 's/py-cord==2.6.1/py-cord==2.5.0/' bot/requirements.txt

# Rebuild and restart
docker compose down
docker compose build discord-bot --no-cache  
docker compose up -d

# Test in Discord
# /joinvoice
```

If this works, the issue was definitely a py-cord 2.6.x bug.
If it still fails, try Solution 3 (token regeneration).

## References

- Discord Error Codes: https://discord.com/developers/docs/topics/opcodes-and-status-codes#voice-voice-close-event-codes
- Py-Cord Voice Issues: https://github.com/Pycord-Development/pycord/issues?q=is%3Aissue+voice+4006
- Discord Gateway Intent Bits: https://discord.com/developers/docs/topics/gateway#gateway-intents
