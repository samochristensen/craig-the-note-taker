Discord Notetaker (Self‑Hosted)

Overview
- Discord bot that records multi‑user audio from a voice channel, transcribes with WhisperX, summarizes with a local Ollama model, and posts the recap back to a text channel.
- Everything runs locally via Docker Compose. No external SaaS.

Services
- discord-bot: Py‑Cord 2.6, joins voice, records per‑user WAVs to disk, calls transcriber, prompts Ollama, posts recap.
- transcriber: FastAPI + WhisperX. Consumes saved WAVs, outputs SRT + transcript text.
- ollama: Local LLM server. Default model: llama3.1:8b (pull separately).

Quick Start
1) Configure .env
   - DISCORD_BOT_TOKEN=…
   - DISCORD_GUILD_ID=… (or DISCORD_GUILD_IDS=comma,separated)
   - DISCORD_POST_CHANNEL_ID=…
   - OLLAMA_HOST=http://127.0.0.1:11434
   - TRANSCRIBER_URL=http://127.0.0.1:8000
   - LLM_MODEL=llama3.1:8b

2) Launch
   - docker compose up -d --build
   - docker compose ps

3) Pull an Ollama model (inside the container)
   - docker exec -it discord_notetaker-ollama-1 ollama pull llama3.1:8b
   - Verify: curl http://127.0.0.1:11434/api/tags

Setup and First Run
1) Prepare environment
   - Copy .env.example to .env and fill values:
     - DISCORD_BOT_TOKEN — rotate in Discord Developer Portal and paste here
     - DISCORD_POST_CHANNEL_ID — a text channel the bot can send to
     - Either DISCORD_GUILD_ID (single) or DISCORD_GUILD_IDS=comma,separated (multi). Omit both for global commands (global may take up to ~1h to appear).
     - Keep OLLAMA_HOST and TRANSCRIBER_URL pointing to localhost as above.
   - Optional logs: set BOT_LOG_LEVEL=DEBUG and VOICE_DEBUG=1

2) Build and start
   - docker compose down
   - docker compose up -d --build
   - docker compose ps (ports 11434 and 8000 should be exposed on host)

3) Pull an Ollama model
   - docker exec -it discord_notetaker-ollama-1 ollama pull llama3.1:8b
   - curl http://127.0.0.1:11434/api/tags → expect a non-empty models list

4) Check transcriber health
   - curl http://127.0.0.1:8000/health → expect {"ok": true}
   - If it fails, rebuild to ensure latest app with /health is in the image.

5) Diagnostics
   - ./diag_discord_notetaker.sh
   - It verifies env, container status, opus load, HTTP reachability, UDP smoke, and optional STUN.
   - If it warns that transcriber is “not published on 8000” but `docker compose ps` shows `0.0.0.0:8000->8000`, update the script (already improved) and re-run.

6) In‑Discord tests
   - Confirm slash commands present in your server(s) (guild‑scoped are instant; global may take up to ~1 hour).
   - Run /hello and /ping.
   - If slash commands don’t appear: ensure the bot was invited with applications.commands scope. Use /invite to get the URL. You can also run /sync to force a re-sync for the current guild.
   - Run /health (gateway + transcriber + ollama). Models should include llama3.1:8b; transcriber /health should be 200.
   - In a voice channel: /joinvoice → /startnotes → speak 20–30s → /stopnotes
   - Expect recap messages and transcript.srt in your post channel.

Diagnostics
- Run the built‑in checks: ./diag_discord_notetaker.sh
- It verifies env, container status, opus load, HTTP reachability, UDP smoke, and optional STUN.

Voice Diagnostics (host + container)
- Run: ./diag_voice_connect.sh
- What it does:
  - Confirms bot container and host networking
  - DNS/HTTPS reachability to discord.com
  - UDP smoketest + STUN reflection (host and inside bot)
  - IPv6 posture, Tailscale hints, and firewall snippets
  - Prints suggested next steps and tcpdump filter to use during /joinvoice

Bot Commands (Slash)
- /startnotes — join your current voice channel and start recording.
- /stopnotes — stop recording; triggers transcription, summarization, and post.
- /hello — quick response test.
- /ping — latency test (gateway + ack RTT).
- /joinvoice — connect to your voice channel (no recording).
- /leavevoice — disconnect from voice.
- /voice_status — report voice client state for the guild.
- /health — combined health (gateway, transcriber, ollama).
  - /self_test — alias of /health with extra details when debug is enabled.
  - /check_setup — verifies permissions for voice and posting to the configured post channel.
- Debug-only (set `EXPOSE_DEBUG_COMMANDS=1` or `VOICE_DEBUG=1`):
  - /stun_check — UDP STUN reflection from inside the bot container.
  - /voice_endpoint — shows last voice endpoint, resolves IPs.
  - /intents — shows gateway intents and VOICE_DEBUG.
 - /sync — force re-sync of application commands.
 - /whoami — prints bot identity and config highlights.
 - /invite — prints OAuth2 invite URL.
 - /self_test — runs quick end-to-end validation.

Troubleshooting Voice Connection
Symptom: “Voice connect timed out.” The bot times out during the voice handshake.

What’s already verified
- Opus loads inside the bot container.
- STUN works from the bot container (UDP reflection returns bytes).
- HTTP reachability to transcriber and Ollama is OK.

Common fixes to try
- Channel Region Override: choose a specific nearby region (not Auto) and retry.
- Keep bot in host networking when possible: discord-bot uses network_mode: host.
- Tailscale overlay: try temporarily relaxing netfilter rules while testing:
  - sudo tailscale up --reset --netfilter-mode=off
  - Test /joinvoice or /startnotes, then revert:
  - sudo tailscale up --netfilter-mode=on
- IPv6 quirk test (temporary):
  - sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
  - sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1
  - Restart the bot, test, then revert both to 0.
- Permissions: ensure the bot role for that channel has Connect and Speak.

Collecting signal
- Enable debug logs: set BOT_LOG_LEVEL=DEBUG in .env and restart the bot, then
  - docker logs -f discord_notetaker-discord-bot-1
- Packet capture during /joinvoice:
  - sudo tcpdump -ni any '(udp port 3478) or (udp portrange 50000-65535)'
  - Look for bidirectional traffic with the Discord voice endpoint.

Repository Structure
- bot/ — Discord bot code and Dockerfile.
- transcriber/ — FastAPI WhisperX service and Dockerfile.
- data/ — Session outputs (mounted volume).
- prompts/ — Prompt templates for summarization.
- models/ — Ollama model blobs (mounted volume).

License
- Local/private use. No license headers added by default.
