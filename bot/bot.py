import os, io, json, time, asyncio, aiohttp, requests
import discord
from discord.ext import commands

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
except Exception as e:
    print("Warning: could not load Opus:", e)

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

@bot.slash_command(guild_ids=[GUILD_ID], description="Join your voice channel and start recording.")
async def startnotes(ctx: discord.ApplicationContext):
    if not ctx.author.voice or not ctx.author.voice.channel:
        return await ctx.respond("Join a voice channel first.", ephemeral=True)

    guild = ctx.guild
    if guild.id in sessions:
        return await ctx.respond("Already recording in this server.", ephemeral=True)

    # Let the user know we’re working
    await ctx.respond("Connecting to voice…", ephemeral=True)

    try:
        vc = await ctx.author.voice.channel.connect(timeout=30.0, reconnect=True)
    except asyncio.TimeoutError:
        return await _reply(
            ctx,
            "⏱️ Voice connect timed out.\n"
            "• Try again in a few seconds.\n"
            "• If it persists, run the bot with `network_mode: host` and ensure outbound UDP isn’t blocked.",
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
    channel = bot.get_channel(post_channel_id)

    # Save per-user WAV files
    for user_id, audio in sink.audio_data.items():
        out_path = os.path.join(session_dir, f"user_{user_id}.wav")
        with open(out_path, "wb") as f:
            f.write(audio.file.read())

    # Disconnect voice
    try:
        await sink.vc.disconnect(force=True)
    except Exception:
        pass

    # Clear session mapping
    try:
        del sessions[guild_id]
    except Exception:
        pass

    if channel is None:
        # Fallback: inform owner if post channel missing
        app_info = await bot.application_info()
        try:
            await app_info.owner.send(f"Captured audio for session `{sid}`, but POST_CHANNEL_ID was invalid.")
        except Exception:
            pass
        return

    await channel.send(f"✅ Audio captured for session `{sid}`. Transcribing…")

    # Call transcriber API
    async with aiohttp.ClientSession() as http:
        try:
            async with http.post(f"{TRANSCRIBER_URL}/transcribe", json={"session_id": sid}) as resp:
                if resp.status != 200:
                    return await channel.send(f"❌ Transcriber error: {resp.status}")
                result = await resp.json()
        except Exception as e:
            return await channel.send(f"❌ Could not reach transcriber at {TRANSCRIBER_URL}: {e}")

    transcript_text = result.get("transcript_text", "")
    try:
        with open("/app/prompts/recap_prompt.txt", "r") as pf:
            recap_prompt = pf.read().strip()
    except Exception:
        recap_prompt = "Summarize this game session."

    await channel.send("🧠 Generating summary…")

    # Chunk transcript for the model
    chunks = chunk_text(transcript_text, 12000)
    outlines = []
    for i, chunk in enumerate(chunks, 1):
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
            outlines.append(f"[Chunk {i} summarization failed: {e}]")

    outlines_str = "\n\n".join(outlines)
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
    except Exception as e:
        final = f"[Merge step failed contacting LLM at {OLLAMA_HOST}: {e}]"

    # Post recap (respect Discord 2000-char limit)
    for part in split_discord(final, 1900):
        await channel.send(part)

    # Attach SRT if present
    srt_path = os.path.join(session_dir, "transcript.srt")
    if os.path.exists(srt_path):
        await channel.send(file=discord.File(srt_path, filename=f"{sid}_transcript.srt"))

# ── Bot ready ─────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} — slash commands registered for guild {GUILD_ID}")

# ── Entry ─────────────────────────────────────────────────────────────────────
bot.run(TOKEN)
