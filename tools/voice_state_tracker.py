#!/usr/bin/env python3
"""
Voice State Tracker / Connection Monitor

Purpose
  - Connects to Discord using your .env token
  - Tracks main gateway READY session_id and voice VOICE_* events
  - Attempts voice connects to a target voice channel, watching for stability
  - Writes a structured timeline JSON to ./data/voice_tracker_YYYYmmdd_HHMMSS.json

Usage
  1) Stop the running bot container (avoid two instances of the same token):
       docker compose stop discord-bot
  2) Run listing to find a voice channel id:
       python tools/voice_state_tracker.py --list
  3) Run a monitored connect attempt:
       python tools/voice_state_tracker.py --channel 123456789012345678 --attempts 3 --linger 2.0

Notes
  - Requires py-cord (already in repo).
  - Reads .env at repo root for DISCORD_BOT_TOKEN and DISCORD_GUILD_ID.
"""

from __future__ import annotations

import argparse
import math
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands


# ── Env helpers ────────────────────────────────────────────────────────────────
def load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        print("ERROR: .env not found. Copy .env.example and fill it first.")
        sys.exit(2)


# ── Data model ────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class TimelineEvent:
    t: float
    kind: str
    data: Dict[str, Any]

    def asdict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        # Add human readable timestamp
        d["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.t))
        return d


class VoiceStateTracker(commands.Bot):
    def __init__(self, *, intents: discord.Intents, linger: float = 1.5):
        super().__init__(command_prefix="!", intents=intents)
        self.timeline: List[TimelineEvent] = []
        self._gateway_session_id: Optional[str] = None
        self._linger = float(linger)
        self._self_id: Optional[int] = None

    # Helper to append timeline entries
    def _log(self, kind: str, **data: Any) -> None:
        ev = TimelineEvent(t=time.time(), kind=kind, data=data)
        self.timeline.append(ev)
        logging.getLogger("voice_tracker").debug(f"{kind}: {data}")

    async def _await_voice_stable(self, vc: discord.VoiceClient, timeout: float = 6.0) -> bool:
        end = time.monotonic() + timeout
        stable_until: Optional[float] = None
        while time.monotonic() < end:
            try:
                connected = bool(getattr(vc, "is_connected", lambda: False)())
            except Exception:
                connected = False
            if connected:
                if stable_until is None:
                    stable_until = time.monotonic() + self._linger
                if time.monotonic() >= stable_until:
                    return True
            else:
                stable_until = None
            await asyncio.sleep(0.25)
        return False

    # ── Event hooks ───────────────────────────────────────────────────────
    async def on_ready(self):
        self._self_id = self.user.id if self.user else None  # type: ignore[assignment]
        self._log("READY", user=str(self.user), id=self._self_id)
        logging.getLogger("voice_tracker").info(f"Logged in as {self.user} ({self._self_id})")

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            if self.user and member.id == self.user.id:
                self._log(
                    "VOICE_STATE_EVT",
                    guild_id=member.guild.id,
                    before_ch=getattr(before.channel, "id", None),
                    after_ch=getattr(after.channel, "id", None),
                )
        except Exception:
            pass

    async def on_socket_response(self, payload):  # type: ignore[override]
        try:
            t = payload.get("t")
            d = payload.get("d", {})
            if t == "READY":
                sess = d.get("session_id")
                if sess:
                    self._gateway_session_id = str(sess)
                    self._log("GATEWAY_READY", session_id=self._gateway_session_id)
            elif t == "RESUMED":
                self._log("GATEWAY_RESUMED", details="ok")
            elif t == "VOICE_SERVER_UPDATE":
                self._log(
                    "VOICE_SERVER_UPDATE",
                    guild_id=d.get("guild_id"),
                    endpoint=d.get("endpoint"),
                    token_present=bool(d.get("token")),
                )
            elif t == "VOICE_STATE_UPDATE":
                # Track only the bot's own voice session_id
                if self._self_id is None or int(d.get("user_id", 0)) != self._self_id:
                    return
                self._log(
                    "VOICE_STATE_UPDATE",
                    guild_id=d.get("guild_id"),
                    channel_id=d.get("channel_id"),
                    session_id=d.get("session_id"),
                )
        except Exception as e:
            logging.getLogger("voice_tracker").warning(f"on_socket_response error: {e}")

    async def attempt_connect(self, channel: discord.VoiceChannel, attempts: int = 1, join_timeout: float = 45.0) -> bool:
        """Try connecting to voice and verify the ws remains up briefly."""
        ok = False
        for i in range(1, max(1, attempts) + 1):
            self._log("CONNECT_ATTEMPT", attempt=i, channel_id=channel.id, guild_id=channel.guild.id)
            # Clean any existing vc first
            try:
                existing = discord.utils.get(self.voice_clients, guild=channel.guild)
                if existing:
                    await existing.disconnect(force=True)
                    await asyncio.sleep(0.6)
                    self._log("PREV_VC_DISCONNECTED", guild_id=channel.guild.id)
            except Exception as e:
                self._log("PREV_VC_DISCONNECT_ERR", err=str(e))

            try:
                vc = await channel.connect(timeout=join_timeout, reconnect=True)
            except Exception as e:
                self._log("CONNECT_EXCEPTION", attempt=i, err=type(e).__name__, msg=str(e))
                await asyncio.sleep(0.6)
                continue

            stable = await self._await_voice_stable(vc)
            # latency can be inf/None before audio starts; guard conversion
            lat = getattr(vc, 'latency', None)
            latency_ms: Optional[int] = None
            try:
                if isinstance(lat, (int, float)) and math.isfinite(lat):
                    latency_ms = int(lat * 1000)
            except Exception:
                latency_ms = None
            self._log("CONNECT_RESULT", attempt=i, stable=stable, latency_ms=latency_ms)

            if stable:
                ok = True
                break
            # Unstable: disconnect and backoff
            try:
                await vc.disconnect(force=True)
                self._log("UNSTABLE_DISCONNECT", attempt=i)
            except Exception:
                pass
            await asyncio.sleep(0.8)
        return ok


async def main_async(args):
    # Logging
    root_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Turn up internal debug if requested
    if args.verbose:
        for name in ("discord", "discord.voice_client", "discord.gateway", "discord.state", "discord.http", "aiohttp.client"):  # noqa: E501
            try:
                logging.getLogger(name).setLevel(logging.DEBUG)
            except Exception:
                pass

    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN missing in .env")
        return 2
    guild_id = int(args.guild or os.environ.get("DISCORD_GUILD_ID", "0") or 0)
    if guild_id <= 0:
        print("ERROR: Provide --guild or set DISCORD_GUILD_ID in .env")
        return 2

    intents = discord.Intents.default()
    intents.guilds = True
    intents.voice_states = True
    bot = VoiceStateTracker(intents=intents, linger=args.linger)

    async def after_login():
        # Optionally list voice channels and exit
        guild = bot.get_guild(guild_id)
        if not guild:
            print(f"ERROR: Bot is not in guild {guild_id}")
            await bot.close()
            return
        if args.list:
            print(f"Guild {guild.name} ({guild.id}) voice channels:")
            for ch in guild.voice_channels:
                print(f"  - {ch.name} id={ch.id}")
            await bot.close()
            return

        # Resolve channel
        channel: Optional[discord.VoiceChannel] = None
        if args.channel:
            ch = guild.get_channel(int(args.channel))
            if isinstance(ch, discord.VoiceChannel):
                channel = ch
        else:
            # Pick the first joinable voice channel
            for ch in guild.voice_channels:
                channel = ch
                break
        if not channel:
            print("ERROR: Could not resolve a target voice channel. Use --channel <id> or ensure the guild has voice channels.")
            await bot.close()
            return

        logging.getLogger("voice_tracker").info(
            f"Attempting voice connect to {channel.name} ({channel.id}) in guild {guild.name} ({guild.id})"
        )
        ok = await bot.attempt_connect(channel, attempts=args.attempts, join_timeout=args.timeout)
        # Disconnect if still connected
        try:
            vc = discord.utils.get(bot.voice_clients, guild=guild)
            if vc and vc.is_connected():
                await vc.disconnect(force=True)
        except Exception:
            pass

        # Persist timeline
        os.makedirs("data", exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = args.output or f"data/voice_tracker_{ts}.json"
        with open(out_path, "w") as f:
            json.dump([ev.asdict() for ev in bot.timeline], f, indent=2)
        print(f"\nSaved timeline: {out_path}")
        # Simple summary
        v_ready = [e for e in bot.timeline if e.kind == "GATEWAY_READY"]
        v_ssu = [e for e in bot.timeline if e.kind == "VOICE_SERVER_UPDATE"]
        v_vsu = [e for e in bot.timeline if e.kind == "VOICE_STATE_UPDATE"]
        v_err = [e for e in bot.timeline if e.kind in ("CONNECT_EXCEPTION", "PREV_VC_DISCONNECT_ERR")]
        v_res = [e for e in bot.timeline if e.kind == "CONNECT_RESULT"]
        print("Summary:")
        print(f"  READY session_id seen: {len(v_ready)}")
        print(f"  VOICE_SERVER_UPDATE events: {len(v_ssu)}")
        print(f"  VOICE_STATE_UPDATE events: {len(v_vsu)} (bot user only)")
        if v_res:
            last = v_res[-1].data
            print(f"  Final connect result: stable={last.get('stable')} latency_ms={last.get('latency_ms')}")
        if v_err:
            print(f"  Exceptions captured: {len(v_err)}")
        await bot.close()

    @bot.event
    async def on_connect():  # first WS connect
        bot._log("GATEWAY_CONNECT", info="ws open")

    @bot.event
    async def on_disconnect():  # gateway disconnected
        bot._log("GATEWAY_DISCONNECT", info="ws closed")

    # Kick off flow after ready
    async def _ready_gate():
        await bot.wait_until_ready()
        await after_login()

    bot.loop.create_task(_ready_gate())
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Track Discord voice state transitions and connection stability")
    p.add_argument("--guild", type=int, default=None, help="Guild ID (defaults to DISCORD_GUILD_ID)")
    p.add_argument("--channel", type=int, default=None, help="Voice channel ID to test (if omitted, picks first)")
    p.add_argument("--attempts", type=int, default=2, help="Number of connect attempts")
    p.add_argument("--timeout", type=float, default=45.0, help="Connect timeout seconds")
    p.add_argument("--linger", type=float, default=1.5, help="Seconds to deem connection stable after connect")
    p.add_argument("--list", action="store_true", help="List voice channels and exit")
    p.add_argument("--output", type=str, default=None, help="Output timeline JSON path")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose internal logging")
    args = p.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
