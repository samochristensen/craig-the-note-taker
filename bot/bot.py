import os, io, json, time, asyncio, aiohttp, requests, logging, socket, struct
import discord
from discord.ext import commands

# ── Logging Setup ───────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("discord_bot")

# Optional: turn up internal discord.py/py-cord voice/gateway logs when VOICE_DEBUG=1
if os.environ.get("VOICE_DEBUG", "").strip() == "1":
    for name in ("discord", "discord.voice_client", "discord.gateway", "discord.state", "discord.http"):
        try:
            logging.getLogger(name).setLevel(logging.DEBUG)
        except Exception:
            pass

# ── Env ─────────────────────────────────────────────────────────────────────────
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
POST_CHANNEL_ID = int(os.environ["DISCORD_POST_CHANNEL_ID"])
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.1:8b")
TRANSCRIBER_URL = os.environ.get("TRANSCRIBER_URL", "http://127.0.0.1:8000")

# ── Discord setup (Pycord) ─────────────────────────────────────────────────────
INTENTS = discord.Intents.default()
INTENTS.voice_states = True
INTENTS.guilds = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)

# guild_id -> {"vc": VoiceClient, "session_id": str}
sessions = {}

# Track last seen voice endpoint/session per guild to aid diagnostics.
_voice_meta = {}  # guild_id -> {"endpoint": str | None, "session_id": str | None, "ts": float}

# ── Utils ──────────────────────────────────────────────────────────────────────
def new_session_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")

def split_discord(text: str, limit: int):
    parts, buf = [], []
    count = 0
    for line in text.splitlines():
        if count + len(line) + 1 > limit:
            parts.append("\n".join(buf))
            buf, count = [], 0
        buf.append(line)
        count += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts

def stream_ollama(resp) -> str:
    out = []
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
            if "response" in obj:
                out.append(obj["response"])
        except Exception:
            pass
    return "".join(out)

def chunk_text(t: str, n: int):
    return [t[i:i + n] for i in range(0, len(t), n)]

# Ensure Opus is loaded (Pycord voice)
try:
    if not discord.opus.is_loaded():
        discord.opus.load_opus('libopus.so.0')  # provided by libopus0
    logger.info("Opus codec loaded successfully")
except Exception as e:
    logger.warning(f"Could not load Opus codec: {e}")

# ── Slash Commands (Pycord style) ──────────────────────────────────────────────
def _ctx_replied(ctx: discord.ApplicationContext) -> bool:
    # Pycord helper: .responded is True after ctx.respond(...), otherwise False
    return getattr(ctx, "responded", False)

async def _reply(ctx: discord.ApplicationContext, content: str, ephemeral: bool = False):
    # Reply safely whether we've already responded or not
    if not _ctx_replied(ctx):
        return await ctx.respond(content, ephemeral=ephemeral)
    else:
        return await ctx.send_followup(content, ephemeral=ephemeral)

@bot.slash_command(guild_ids=[GUILD_ID], description="Hello test to verify bot responds.")
async def hello(ctx: discord.ApplicationContext):
    await ctx.respond(
        "Hello! The bot is alive.\n"
        f"Guild: {ctx.guild.id}\n"
        f"Py-Cord: {discord.__version__}",
        ephemeral=True,
    )

@bot.slash_command(guild_ids=[GUILD_ID], description="Gateway ping and basic info.")
async def ping(ctx: discord.ApplicationContext):
    t0 = time.perf_counter()
    await ctx.respond("Pinging…", ephemeral=True)
    t1 = time.perf_counter()
    gw_ms = int((bot.latency or 0) * 1000)
    await ctx.send_followup(
        content=(
            f"Pong!\nGateway latency: {gw_ms} ms\n"
            f"Ack RTT: {int((t1 - t0)*1000)} ms"
        ),
        ephemeral=True,
    )

@bot.slash_command(guild_ids=[GUILD_ID], description="Join your current voice channel without recording.")
async def joinvoice(ctx: discord.ApplicationContext):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.respond("Join a voice channel first.", ephemeral=True)
    ch = ctx.author.voice.channel
    # Pre-check permissions before attempting to connect
    try:
        perms = ch.permissions_for(ctx.guild.me)  # type: ignore[attr-defined]
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.connect:
            missing.append("Connect")
        if not perms.speak:
            missing.append("Speak")
        if missing:
            return await ctx.respond(
                "I’m missing required voice permissions here:\n" +
                " • " + "\n • ".join(missing) +
                "\nFix: Edit Channel → Permissions → add the bot (or its role) and Allow View/Connect/Speak.",
                ephemeral=True,
            )
    except Exception:
        pass

    await ctx.respond("Connecting to voice…", ephemeral=True)
    try:
        vc = await ctx.author.voice.channel.connect(timeout=45.0, reconnect=True)
    except asyncio.TimeoutError:
        diag = _voice_diag_summary(ctx.guild.id)
        hint = (
            "⏱️ Voice connect timed out.\n"
            f"Last VOICE events: {diag}\n"
            "Tips: set a specific channel region (not Auto); ensure outbound UDP; if on Tailscale, test netfilter off; try IPv6-off test."
        )
        return await _reply(ctx, hint, ephemeral=True)
    except discord.Forbidden:
        return await _reply(ctx, "❌ Missing permission to join or speak in that channel.", ephemeral=True)
    except discord.HTTPException as e:
        return await _reply(ctx, f"❌ HTTP error while joining voice: {e.status}", ephemeral=True)
    except discord.ClientException as e:
        return await _reply(ctx, f"❌ Client error: {e}", ephemeral=True)
    except Exception as e:
        return await _reply(ctx, f"❌ Could not join voice: `{type(e).__name__}`", ephemeral=True)
    await _reply(ctx, f"✅ Connected to {ctx.author.voice.channel.name}.", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Leave the current voice channel.")
async def leavevoice(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if vc and vc.is_connected():
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        await ctx.send_followup(content="👋 Disconnected from voice.", ephemeral=True)
    else:
        await ctx.send_followup(content="Not connected to voice.", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Report voice connection status for this guild.")
async def voice_status(ctx: discord.ApplicationContext):
    vc = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not vc:
        return await ctx.respond("No voice client for this guild.", ephemeral=True)
    details = {
        "is_connected": vc.is_connected(),
        "channel": getattr(vc.channel, 'name', None),
        "guild_id": vc.guild.id,
        "latency_ms": int((vc.latency or 0) * 1000) if hasattr(vc, 'latency') else None,
    }
    await ctx.respond(f"Voice status: {details}", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Show the bot's permissions in your current voice channel.")
async def voice_perms(ctx: discord.ApplicationContext):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.respond("Join a voice channel first.", ephemeral=True)
    ch = ctx.author.voice.channel
    try:
        perms = ch.permissions_for(ctx.guild.me)  # type: ignore[attr-defined]
        await ctx.respond(
            "Permissions in this voice channel:\n"
            f"• View Channel: {'yes' if perms.view_channel else 'no'}\n"
            f"• Connect: {'yes' if perms.connect else 'no'}\n"
            f"• Speak: {'yes' if perms.speak else 'no'}\n"
            f"• Mute Members: {'yes' if perms.mute_members else 'no'}\n"
            f"• Move Members: {'yes' if perms.move_members else 'no'}",
            ephemeral=True,
        )
    except Exception as e:
        await ctx.respond(f"Could not compute perms: {e}", ephemeral=True)

async def _collect_health(include_stun: bool = False) -> list[str]:
    """Collect service health summaries."""
    results: list[str] = []
    gw_ms = int((bot.latency or 0) * 1000)
    results.append(f"Gateway latency: {gw_ms} ms")
    # Transcriber
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{TRANSCRIBER_URL}/health", timeout=5) as r:
                ok = r.status == 200
        results.append(f"Transcriber /health: {'ok' if ok else 'bad'} ({r.status})")
    except Exception as e:
        results.append(f"Transcriber error: {e}")
    # Ollama
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{OLLAMA_HOST}/api/tags", timeout=5) as r:
                tags = await r.json()
                models = [m.get('model') for m in tags.get('models', [])]
        results.append(f"Ollama models: {len(models)}")
    except Exception as e:
        results.append(f"Ollama error: {e}")
    # Optional STUN
    if include_stun:
        try:
            bytes_len = await asyncio.to_thread(_stun_probe_once)
            results.append(f"STUN: {'ok' if bytes_len else 'no response'}")
        except Exception as e:
            results.append(f"STUN error: {e}")
    return results

@bot.slash_command(guild_ids=[GUILD_ID], description="Combined health: gateway, transcriber, ollama.")
async def health(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    include_stun = os.environ.get("EXPOSE_DEBUG_COMMANDS", "0").strip() == "1" or os.environ.get("VOICE_DEBUG", "").strip() == "1"
    results = await _collect_health(include_stun=include_stun)
    await ctx.send_followup("\n".join(results), ephemeral=True)

def _stun_probe_once(host='stun.l.google.com', port=19302, timeout=2):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    tid = os.urandom(12)
    msg = struct.pack('!HHI12s', 0x0001, 0, 0x2112A442, tid)
    s.sendto(msg, (host, port))
    try:
        data, _ = s.recvfrom(2048)
        return len(data)
    except Exception:
        return 0

def _voice_diag_summary(guild_id: int) -> str:
    """Summarize last captured VOICE_* events for diagnostics."""
    meta = _voice_meta.get(guild_id, {})
    endpoint = meta.get("endpoint")
    session_id = meta.get("session_id")
    ts = meta.get("ts", 0.0)
    age = int(time.time() - ts) if ts else None
    parts = []
    if endpoint:
        parts.append(f"endpoint={endpoint}")
    if session_id:
        parts.append("have_session_id=1")
    if age is not None:
        parts.append(f"age_s={age}")
    if not parts:
        return "no VOICE_* events captured"
    return ", ".join(parts)

EXPOSE_DEBUG_CMDS = os.environ.get("EXPOSE_DEBUG_COMMANDS", "0").strip() == "1" or os.environ.get("VOICE_DEBUG", "").strip() == "1"
if EXPOSE_DEBUG_CMDS:
    @bot.slash_command(guild_ids=[GUILD_ID], description="UDP STUN reflection check from the bot container.")
    async def stun_check(ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        bytes_len = await asyncio.to_thread(_stun_probe_once)
        if bytes_len:
            await ctx.send_followup(content=f"STUN OK ({bytes_len} bytes)", ephemeral=True)
        else:
            await ctx.send_followup(content="STUN failed (no UDP response)", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Force re-sync of application commands.")
async def sync(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    try:
        await bot.sync_commands()
        await ctx.send_followup("✅ Commands synced. If commands are global, allow up to ~1 hour to propagate.", ephemeral=True)
    except Exception as e:
        await ctx.send_followup(f"❌ Sync failed: {e}", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Show bot identity and current config highlights.")
async def whoami(ctx: discord.ApplicationContext):
    app_info = await bot.application_info()
    await ctx.respond(
        f"User: {bot.user} (id={bot.user.id})\n"
        f"App ID: {app_info.id}\n"
        f"Guild scope: {GUILD_ID}\n"
        f"LLM: {LLM_MODEL}\n"
        f"Transcriber: {TRANSCRIBER_URL}",
        ephemeral=True,
    )

if EXPOSE_DEBUG_CMDS:
    @bot.slash_command(guild_ids=[GUILD_ID], description="Show gateway intents and voice debug state.")
    async def intents(ctx: discord.ApplicationContext):
        i = bot.intents
        await ctx.respond(
            "Intents:"\
            f" voice_states={i.voice_states} guilds={i.guilds}\n"\
            f"VOICE_DEBUG={os.environ.get('VOICE_DEBUG', '')}",
            ephemeral=True,
        )

@bot.slash_command(guild_ids=[GUILD_ID], description="Show OAuth2 invite URL for this bot.")
async def invite(ctx: discord.ApplicationContext):
    app_info = await bot.application_info()
    # Recommend minimal perms needed for this bot's functions
    recommended = discord.Permissions(
        view_channel=True,
        send_messages=True,
        attach_files=True,
        connect=True,
        speak=True,
    ).value
    url = (
        f"https://discord.com/api/oauth2/authorize?client_id={app_info.id}"
        f"&permissions={recommended}&scope=bot%20applications.commands"
    )
    await ctx.respond(f"Invite URL (owner/admin only):\n{url}", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="End-to-end self test (alias of /health).")
async def self_test(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)
    include_stun = os.environ.get("EXPOSE_DEBUG_COMMANDS", "0").strip() == "1" or os.environ.get("VOICE_DEBUG", "").strip() == "1"
    results = await _collect_health(include_stun=include_stun)
    try:
        perms = ctx.channel.permissions_for(ctx.guild.me)  # type: ignore[attr-defined]
        results.insert(1, f"Send perms here: {'yes' if perms.send_messages else 'no'}")
    except Exception:
        pass
    await ctx.send_followup("\n".join(results), ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Join your voice channel and start recording.")
async def startnotes(ctx: discord.ApplicationContext):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.respond("Join a voice channel first.", ephemeral=True)

    guild = ctx.guild
    if guild.id in sessions:
        return await ctx.respond("Already recording in this server.", ephemeral=True)

    # Permission pre-check
    ch = ctx.author.voice.channel
    try:
        perms = ch.permissions_for(ctx.guild.me)  # type: ignore[attr-defined]
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.connect:
            missing.append("Connect")
        if not perms.speak:
            missing.append("Speak")
        if missing:
            return await ctx.respond(
                "I’m missing required voice permissions here:\n" +
                " • " + "\n • ".join(missing) +
                "\nFix: Edit Channel → Permissions → add the bot (or its role) and Allow View/Connect/Speak.",
                ephemeral=True,
            )
    except Exception:
        pass

    # Let the user know we’re working
    await ctx.respond("Connecting to voice…", ephemeral=True)

    try:
        vc = await ctx.author.voice.channel.connect(timeout=60.0, reconnect=True)
    except asyncio.TimeoutError:
        diag = _voice_diag_summary(ctx.guild.id)
        return await _reply(
            ctx,
            "⏱️ Voice connect timed out.\n"
            f"Last VOICE events: {diag}\n"
            "Tips: set a specific channel region (not Auto); ensure outbound UDP; if on Tailscale, test netfilter off; try IPv6-off test.",
            ephemeral=True,
        )
    except Exception as e:
        return await _reply(ctx, f"❌ Could not join voice: `{type(e).__name__}`", ephemeral=True)

    sid = time.strftime("%Y%m%d_%H%M%S")
    session_dir = f"/app/data/sessions/{sid}"
    os.makedirs(session_dir, exist_ok=True)

    vc.start_recording(
        discord.sinks.WaveSink(),   # per-user WAVs in memory
        finished_callback,          # called on stop
        POST_CHANNEL_ID, guild.id, sid, session_dir
    )

    sessions[guild.id] = {"vc": vc, "session_id": sid}
    await _reply(ctx, f"🎙️ Recording started (session `{sid}`). Use `/stopnotes` to finish.", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID], description="Stop recording and generate summary.")
async def stopnotes(ctx: discord.ApplicationContext):
    await ctx.defer(ephemeral=True)

    guild = ctx.guild
    if guild.id not in sessions:
        return await ctx.edit_original_response(content="No active recording.")

    vc = sessions[guild.id]["vc"]
    # This triggers finished_callback(...)
    vc.stop_recording()
    await ctx.edit_original_response(content="🛑 Stopping recording…")

# ── Recording finished callback ────────────────────────────────────────────────
async def finished_callback(sink: discord.sinks.Sink, post_channel_id: int, guild_id: int, sid: str, session_dir: str):
    """After stop_recording(): save WAVs, transcribe, summarize, post, disconnect."""
    logger.info(f"Processing finished recording for session {sid}")
    channel = bot.get_channel(post_channel_id)

    # Save per-user WAV files
    for user_id, audio in sink.audio_data.items():
        out_path = os.path.join(session_dir, f"user_{user_id}.wav")
        # CRITICAL: Reset file pointer to start before reading
        # BytesIO objects from discord.sinks have their pointer at EOF after recording
        audio.file.seek(0)
        with open(out_path, "wb") as f:
            bytes_written = f.write(audio.file.read())
            logger.debug(f"Saved {bytes_written} bytes for user {user_id}")

    # Disconnect voice
    try:
        await sink.vc.disconnect(force=True)
        logger.info(f"Disconnected from voice channel for guild {guild_id}")
    except Exception as e:
        logger.error(f"Failed to disconnect from voice: {e}")

    # Clear session mapping
    try:
        del sessions[guild_id]
    except Exception as e:
        logger.warning(f"Failed to clear session mapping: {e}")

    if channel is None:
        # Fallback: inform owner if post channel missing
        logger.error(f"POST_CHANNEL_ID {post_channel_id} is invalid or bot lacks access")
        app_info = await bot.application_info()
        try:
            await app_info.owner.send(f"Captured audio for session `{sid}`, but POST_CHANNEL_ID was invalid.")
        except Exception as e:
            logger.error(f"Could not notify owner: {e}")
        return

    await channel.send(f"✅ Audio captured for session `{sid}`. Transcribing…")
    logger.info(f"Starting transcription for session {sid}")

    # Call transcriber API
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(f"{TRANSCRIBER_URL}/transcribe", json={"session_id": sid}) as resp:
                if resp.status != 200:
                    error_msg = f"Transcriber returned status {resp.status}"
                    logger.error(error_msg)
                    return await channel.send(f"❌ Transcriber error: {resp.status}")
                result = await resp.json()
                logger.info(f"Transcription completed for session {sid}")
        except Exception as e:
            logger.error(f"Failed to reach transcriber at {TRANSCRIBER_URL}: {e}")
            return await channel.send(f"❌ Could not reach transcriber at {TRANSCRIBER_URL}: {e}")

    transcript_text = result.get("transcript_text", "")
    try:
        with open("/app/prompts/recap_prompts.txt", "r") as pf:
            recap_prompt = pf.read().strip()
        logger.debug("Loaded custom recap prompt")
    except Exception as e:
        logger.warning(f"Could not load recap prompt file, using fallback: {e}")
        recap_prompt = "Summarize this game session."

    await channel.send("🧠 Generating summary…")
    logger.info(f"Starting LLM summarization for session {sid}")

    # Chunk transcript for the model
    chunks = chunk_text(transcript_text, 12000)
    logger.debug(f"Split transcript into {len(chunks)} chunks for processing")
    outlines = []
    for i, chunk in enumerate(chunks, 1):
        logger.debug(f"Processing chunk {i}/{len(chunks)}")
        payload = {
            "model": LLM_MODEL,
            "prompt": (
                f"{recap_prompt}\n\n[TRANSCRIPT CHUNK {i}/{len(chunks)}]\n"
                f"{chunk}\n\nReturn only the requested sections."
            ),
            "options": {"temperature": 0.2}
        }
        try:
            r = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, stream=True, timeout=120)
            outlines.append(stream_ollama(r))
        except Exception as e:
            error_msg = f"[Chunk {i} summarization failed: {e}]"
            logger.error(error_msg)
            outlines.append(error_msg)

    outlines_str = "\n\n".join(outlines)
    logger.debug("Merging chunk summaries into final recap")
    merge_payload = {
        "model": LLM_MODEL,
        "prompt": (
            "Combine the following chunked notes into a single well-structured session recap "
            "with the same sections, removing duplicates and keeping the best details:\n\n"
            + outlines_str
        ),
        "options": {"temperature": 0.2}
    }
    try:
        final = stream_ollama(
            requests.post(f"{OLLAMA_HOST}/api/generate", json=merge_payload, stream=True, timeout=180)
        )
        logger.info(f"Summary generation completed for session {sid}")
    except Exception as e:
        error_msg = f"[Merge step failed contacting LLM at {OLLAMA_HOST}: {e}]"
        logger.error(error_msg)
        final = error_msg

    # Post recap (respect Discord 2000-char limit)
    for i, part in enumerate(split_discord(final, 1900), 1):
        await channel.send(part)
        logger.debug(f"Posted summary part {i}")

    # Attach SRT if present
    srt_path = os.path.join(session_dir, "transcript.srt")
    if os.path.exists(srt_path):
        await channel.send(file=discord.File(srt_path, filename=f"{sid}_transcript.srt"))
        logger.info(f"Posted SRT transcript for session {sid}")
    else:
        logger.warning(f"No SRT file found at {srt_path}")
    
    logger.info(f"Session {sid} processing complete")

# ── Bot ready ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    logger.info(f"Bot logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Slash commands registered for guild {GUILD_ID}")
    logger.info(f"Using LLM: {LLM_MODEL} at {OLLAMA_HOST}")
    logger.info(f"Transcriber endpoint: {TRANSCRIBER_URL}")
    # Try syncing commands to ensure visibility
    try:
        await bot.sync_commands()
        logger.debug("Commands synced successfully")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

# Always capture VOICE_* events; only print when VOICE_DEBUG=1
@bot.event
async def on_socket_response(payload):  # type: ignore[override]
    try:
        t = payload.get("t")
        if t in ("VOICE_SERVER_UPDATE", "VOICE_STATE_UPDATE"):
            d = payload.get("d", {})
            guild_id = d.get("guild_id")
            if guild_id:
                meta = _voice_meta.setdefault(int(guild_id), {"endpoint": None, "session_id": None, "ts": 0.0})
                if t == "VOICE_SERVER_UPDATE":
                    meta["endpoint"] = d.get("endpoint")
                    logger.debug(f"VOICE_SERVER_UPDATE: endpoint={meta['endpoint']}, guild={guild_id}")
                if t == "VOICE_STATE_UPDATE":
                    meta["session_id"] = d.get("session_id")
                    logger.debug(f"VOICE_STATE_UPDATE: session_id={meta['session_id']}, guild={guild_id}")
                meta["ts"] = time.time()

            if os.environ.get("VOICE_DEBUG", "").strip() == "1":
                if t == "VOICE_SERVER_UPDATE":
                    print(f"[VOICE_DEBUG] SERVER_UPDATE endpoint={d.get('endpoint')} guild_id={d.get('guild_id')}")
                if t == "VOICE_STATE_UPDATE":
                    print(
                        f"[VOICE_DEBUG] STATE_UPDATE guild_id={d.get('guild_id')} channel_id={d.get('channel_id')} user_id={d.get('user_id')} session_id={d.get('session_id')}"
                    )
    except Exception as e:
        logger.error(f"Error processing socket response: {e}")


# Extra visibility: high-level voice state transitions (fires if VOICE_STATE_UPDATE is delivered)
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    try:
        if os.environ.get("VOICE_DEBUG", "").strip() == "1" and bot.user and member.id == bot.user.id:
            bch = getattr(before.channel, 'id', None)
            ach = getattr(after.channel, 'id', None)
            logger.debug(
                f"on_voice_state_update(self): guild={member.guild.id} before_ch={bch} after_ch={ach}"
            )
    except Exception:
        pass

def _resolve_ips(host: str) -> list[str]:
    ips = []
    try:
        for family in (socket.AF_INET, socket.AF_INET6):
            try:
                ai = socket.getaddrinfo(host, None, family, socket.SOCK_DGRAM)
            except Exception:
                continue
            for entry in ai:
                addr = entry[4][0]
                if addr not in ips:
                    ips.append(addr)
    except Exception:
        pass
    return ips

if EXPOSE_DEBUG_CMDS:
    @bot.slash_command(guild_ids=[GUILD_ID], description="Show last voice endpoint and resolved IPs for this guild.")
    async def voice_endpoint(ctx: discord.ApplicationContext):
        meta = _voice_meta.get(ctx.guild.id)
        if not meta or not meta.get("endpoint"):
            return await ctx.respond("No endpoint captured yet. Try /joinvoice to trigger VOICE_SERVER_UPDATE.", ephemeral=True)
        endpoint = str(meta["endpoint"])  # e.g., region.discord.media:443
        host = endpoint.split(":", 1)[0]
        ips = await asyncio.to_thread(_resolve_ips, host)
        await ctx.respond(
            f"Endpoint: {endpoint}\nHost: {host}\nIPs: {', '.join(ips) if ips else '(none)'}",
            ephemeral=True,
        )

# ── Entry ─────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
