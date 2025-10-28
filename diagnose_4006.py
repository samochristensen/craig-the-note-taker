#!/usr/bin/env python3
"""
Diagnostic script for Discord Voice Error 4006
This script checks common causes of the "Session no longer valid" error
"""

import os
import sys
import requests

print("=" * 70)
print("Discord Voice Error 4006 Diagnostic")
print("=" * 70)
print()

# Load environment
try:
    with open('.env', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()
except FileNotFoundError:
    print("ERROR: .env file not found!")
    sys.exit(1)

TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
GUILD_ID = os.environ.get('DISCORD_GUILD_ID', '')

if not TOKEN or not GUILD_ID:
    print("ERROR: DISCORD_BOT_TOKEN and DISCORD_GUILD_ID must be set in .env")
    sys.exit(1)

print(f"Guild ID: {GUILD_ID}")
print(f"Token (first 20 chars): {TOKEN[:20]}...")
print()

# Check 1: Validate bot token format
print("[1] Token Format Check")
if TOKEN.startswith('Bot '):
    print("  ⚠️  WARNING: Token starts with 'Bot ' - this should NOT be in the .env file")
    print("     Remove 'Bot ' prefix from DISCORD_BOT_TOKEN in .env")
elif '.' not in TOKEN:
    print("  ❌ INVALID: Token doesn't appear to be in correct format")
    print("     Expected format: MTxxxxxxx.xxxxxx.xxxxxxxxxxx")
else:
    parts = TOKEN.split('.')
    if len(parts) != 3:
        print(f"  ⚠️  WARNING: Token has {len(parts)} parts, expected 3")
    else:
        print(f"  ✅ Token format looks correct ({len(parts[0])}.{len(parts[1])}.{len(parts[2])} chars)")
print()

# Check 2: Test token validity with Discord API
print("[2] Testing Token Validity...")
headers = {'Authorization': f'Bot {TOKEN}'}

try:
    # Get bot user info
    response = requests.get('https://discord.com/api/v10/users/@me', headers=headers, timeout=10)
    
    if response.status_code == 200:
        bot_user = response.json()
        print(f"  ✅ Token is VALID")
        print(f"     Bot User: {bot_user['username']}#{bot_user['discriminator']}")
        print(f"     Bot ID: {bot_user['id']}")
    elif response.status_code == 401:
        print(f"  ❌ Token is INVALID (401 Unauthorized)")
        print(f"     You need to regenerate your bot token at:")
        print(f"     https://discord.com/developers/applications")
        sys.exit(1)
    else:
        print(f"  ⚠️  Unexpected response: {response.status_code}")
        print(f"     {response.text[:200]}")
except Exception as e:
    print(f"  ❌ ERROR: Could not connect to Discord API: {e}")
    sys.exit(1)

print()

# Check 3: Get application info and check intents
print("[3] Checking Bot Application Configuration...")
try:
    response = requests.get('https://discord.com/api/v10/applications/@me', headers=headers, timeout=10)
    
    if response.status_code == 200:
        app_info = response.json()
        print(f"  Application Name: {app_info.get('name', 'Unknown')}")
        print(f"  Application ID: {app_info.get('id', 'Unknown')}")
        
        # Check flags
        flags = app_info.get('flags', 0)
        print(f"  Application Flags: {flags}")
        
        # Get bot info from application
        bot_info = app_info.get('bot', {})
        if bot_info:
            bot_public = bot_info.get('public', True)
            print(f"  Bot Public: {bot_public}")
        
    else:
        print(f"  ⚠️  Could not get application info: {response.status_code}")
except Exception as e:
    print(f"  ⚠️  Error getting application info: {e}")

print()

# Check 4: Try to get gateway bot info (includes intents check)
print("[4] Checking Gateway Connection & Required Intents...")
try:
    response = requests.get('https://discord.com/api/v10/gateway/bot', headers=headers, timeout=10)
    
    if response.status_code == 200:
        gateway_info = response.json()
        print(f"  ✅ Gateway URL: {gateway_info.get('url', 'Unknown')}")
        print(f"  Sessions Remaining: {gateway_info.get('session_start_limit', {}).get('remaining', 'Unknown')}")
        
        # This is the critical part - if we can connect to gateway/bot endpoint,
        # the token is valid for bot operations
        print()
        print("  ⚠️  CRITICAL: Error 4006 'Session no longer valid' usually means:")
        print("     1. The bot's intents are not properly enabled in Discord Developer Portal")
        print("     2. The voice channel's permissions don't allow the bot to connect")
        print("     3. There's a session conflict (rare)")
        print()
        print("  ACTION REQUIRED:")
        print("     → Go to: https://discord.com/developers/applications")
        print(f"     → Select your application (ID: {app_info.get('id', 'Unknown')})")
        print("     → Click 'Bot' in the left sidebar")
        print("     → Scroll to 'Privileged Gateway Intents'")
        print("     → Ensure these are ENABLED:")
        print("        □ SERVER MEMBERS INTENT (optional but recommended)")
        print("        □ PRESENCE INTENT (optional)")
        print("        □ MESSAGE CONTENT INTENT (optional)")
        print()
        print("     → MOST IMPORTANT: Voice connections require proper intents")
        print("        The bot code already requests GUILD_VOICE_STATES")
        print("        But if intents are restricted at the portal level, it will fail")
        print()
    elif response.status_code == 401:
        print(f"  ❌ Unauthorized - token invalid for gateway operations")
    else:
        print(f"  ⚠️  Unexpected response: {response.status_code}")
        print(f"     {response.text[:200]}")
except Exception as e:
    print(f"  ❌ ERROR: {e}")

print()

# Check 5: Verify bot is in the guild
print(f"[5] Checking Bot Membership in Guild {GUILD_ID}...")
try:
    response = requests.get(
        f'https://discord.com/api/v10/guilds/{GUILD_ID}/members/{bot_user["id"]}',
        headers=headers,
        timeout=10
    )
    
    if response.status_code == 200:
        member_info = response.json()
        print(f"  ✅ Bot IS a member of this guild")
        print(f"     Roles: {len(member_info.get('roles', []))} roles assigned")
    elif response.status_code == 404:
        print(f"  ❌ Bot is NOT a member of guild {GUILD_ID}")
        print(f"     You need to invite the bot to this server")
    elif response.status_code == 403:
        print(f"  ⚠️  Cannot check membership (403 Forbidden)")
        print(f"     Bot might not have access or guild ID is wrong")
    else:
        print(f"  ⚠️  Unexpected response: {response.status_code}")
except Exception as e:
    print(f"  ⚠️  Error: {e}")

print()
print("=" * 70)
print("Diagnosis Complete")
print("=" * 70)
print()
print("NEXT STEPS TO FIX ERROR 4006:")
print()
print("1. Verify Gateway Intents in Discord Developer Portal (most common cause)")
print("   https://discord.com/developers/applications")
print()
print("2. Ensure the bot has Connect + Speak permissions in the voice channel")
print("   Right-click channel → Edit Channel → Permissions → Add bot role")
print()
print("3. Try regenerating the bot token if it's old or was recently modified")
print()
print("4. Make sure you're not running multiple instances of the bot")
print("   (Can cause session conflicts)")
print()
print("5. If on Py-Cord 2.6+, consider trying discord.py instead (compatibility)")
print()
