import discord
from discord.ext import commands, tasks
import os
import random
import string
import asyncio
import json
import time
import re
from datetime import datetime, timezone

# ================= CONFIG =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CLIENT_ID = "1546323834561634324"
MASTER_USER_ID = 1414107360099831808

BRAND_NAME = "Perc"
EMBED_COLOR = discord.Color.gold()

DATA_DIR = os.environ.get("DATA_DIR", ".")  # point this at a mounted persistent volume!
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "configs.json")
LICENSE_FILE = os.path.join(DATA_DIR, "licenses.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
SWITCH_FILE = os.path.join(DATA_DIR, "switch_requests.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOCKDOWN_DIR = os.path.join(DATA_DIR, "lockdowns")
CONFIRM_TIMEOUT = 30
SETUP_TIMEOUT = 300

RAID_WINDOW_SECONDS = 10
RAID_BAN_THRESHOLD = 3
RAID_KICK_THRESHOLD = 3
RAID_CHANNEL_DELETE_THRESHOLD = 2
RAID_ROLE_DELETE_THRESHOLD = 2

JOIN_RAID_WINDOW = 30
JOIN_RAID_THRESHOLD = 10
MASS_JOIN_COOLDOWN = 300
# ============================================

os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(LOCKDOWN_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.bans = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# =================================================================
# EMBED HELPERS
# =================================================================

def brand_embed(title, description=None, color=EMBED_COLOR):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"{BRAND_NAME} Security System")
    return embed

async def log_embed(owner_id, guild, title, description, color=EMBED_COLOR, fields=None):
    cfg = configs.get(str(owner_id), {})
    log_channel_id = cfg.get("log_channel_id")
    channel = guild.get_channel(log_channel_id) if log_channel_id else None

    embed = brand_embed(title, description, color)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

    if channel:
        try:
            await channel.send(embed=embed)
        except Exception:
            pass
    else:
        fallback = guild.system_channel
        if fallback:
            try:
                await fallback.send(embed=embed)
            except Exception:
                pass

    try:
        user = await bot.fetch_user(owner_id)
        await user.send(embed=embed)
    except Exception:
        pass

# =================================================================
# STORAGE
# =================================================================

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

configs = load_json(CONFIG_FILE, {})
licenses = load_json(LICENSE_FILE, {"keys": {}, "activations": {}})
blacklist = set(load_json(BLACKLIST_FILE, []))
switch_requests = load_json(SWITCH_FILE, {"next_id": 1, "requests": {}})

def save_configs():
    save_json(CONFIG_FILE, configs)

def save_licenses():
    save_json(LICENSE_FILE, licenses)

def save_blacklist():
    save_json(BLACKLIST_FILE, list(blacklist))

def save_switch_requests():
    save_json(SWITCH_FILE, switch_requests)

guild_to_owner = {}

def rebuild_guild_index():
    guild_to_owner.clear()
    for owner_id, cfg in configs.items():
        if cfg.get("setup_complete") and cfg.get("guild_id"):
            guild_to_owner[cfg["guild_id"]] = int(owner_id)

rebuild_guild_index()

def get_config(user_id):
    return configs.get(str(user_id))

def get_owner_id_for_guild(guild_id):
    return guild_to_owner.get(guild_id)

def is_blacklisted(user_id):
    return user_id in blacklist

def is_master(user_id):
    return user_id == MASTER_USER_ID

def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def gen_license_key():
    part = lambda: ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{part()}-{part()}-{part()}-{part()}"

def valid_discord_id(s):
    return s.isdigit() and 15 <= len(s) <= 20

def backup_path(guild_id):
    return os.path.join(BACKUP_DIR, f"{guild_id}.json")

def lockdown_path(guild_id):
    return os.path.join(LOCKDOWN_DIR, f"{guild_id}.json")

# =================================================================
# LICENSE HELPERS
# =================================================================

def parse_duration(token):
    token = token.lower().strip()
    if token in ("lifetime", "life", "permanent", "perm"):
        return "lifetime", None
    m = re.match(r"^(\d+)([dwmy])$", token)
    if not m:
        return None, None
    num, unit = int(m.group(1)), m.group(2)
    days_per_unit = {"d": 1, "w": 7, "m": 30, "y": 365}
    return f"{num}{unit}", num * days_per_unit[unit]

def get_license_status(user_id):
    entry = licenses.get("activations", {}).get(str(user_id))
    if not entry:
        return None
    expires_at = entry.get("expires_at")
    valid = expires_at is None or time.time() < expires_at
    expires_display = (
        "Never (Lifetime) ♾️" if expires_at is None
        else f"<t:{int(expires_at)}:F> (<t:{int(expires_at)}:R>)"
    )
    return {"valid": valid, "expires_at": expires_at, "expires_display": expires_display, "key": entry.get("key")}

def is_activated(user_id):
    status = get_license_status(user_id)
    return status is not None and status["valid"]

def is_protection_active(owner_id):
    return is_activated(owner_id)

# =================================================================
# GLOBAL BLACKLIST GATE
# =================================================================

@bot.check
async def globally_block_blacklisted(ctx):
    return not is_blacklisted(ctx.author.id)

# =================================================================
# PENDING STATE (in-memory)
# =================================================================

setup_sessions = {}
pending_switch_context = {}

def start_session(user_id, stage):
    setup_sessions[user_id] = {"stage": stage, "data": {}, "expires": time.time() + SETUP_TIMEOUT}

def touch_session(user_id):
    if user_id in setup_sessions:
        setup_sessions[user_id]["expires"] = time.time() + SETUP_TIMEOUT

def is_exempt(guild_id, user_id):
    owner_id = get_owner_id_for_guild(guild_id)
    if owner_id is None:
        return True
    if user_id == owner_id or user_id == bot.user.id:
        return True
    cfg = configs[str(owner_id)]
    return user_id in cfg.get("trusted", [])

async def get_audit_actor(guild, action, target_id=None):
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            if target_id is None or (entry.target and getattr(entry.target, "id", None) == target_id):
                return entry.user
    except Exception:
        pass
    return None

# =================================================================
# LICENSE SYSTEM COMMANDS
# =================================================================

@bot.command(name="activate")
async def activate(ctx, key: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return

    status = get_license_status(ctx.author.id)
    if status and status["valid"]:
        embed = brand_embed("✅ Already Activated", color=discord.Color.green())
        embed.add_field(name="Expires", value=status["expires_display"], inline=False)
        embed.add_field(name="Next step", value="Run `!setup` to configure your server.", inline=False)
        return await ctx.send(embed=embed)

    if not key:
        embed = brand_embed(f"🔒 {BRAND_NAME} Activation", "Enter your license key to unlock protection.", EMBED_COLOR)
        embed.add_field(name="Usage", value="`!activate YOUR-KEY-HERE`", inline=False)
        return await ctx.send(embed=embed)

    key = key.strip().upper()
    entry = licenses["keys"].get(key)
    if not entry:
        return await ctx.send(embed=brand_embed("❌ Invalid Key", "That license key doesn't exist.", discord.Color.red()))

    if entry["bound_user_id"] != ctx.author.id:
        pending_switch_context[ctx.author.id] = {"key": key, "time": time.time()}
        embed = brand_embed(
            "🔐 Key Bound to a Different Account",
            "This key is registered to a different Discord account and can't be redeemed here.\n\n"
            "**Switching accounts?** Run:\n`!useridswitch <reason>`\n\n"
            "⚠️ In your reason, make sure to include the **User ID of your OLD account** "
            "(the one this key is currently bound to) so it can be verified.",
            discord.Color.orange()
        )
        return await ctx.send(embed=embed)

    if entry["used"]:
        return await ctx.send(embed=brand_embed("❌ Key Already Used", "This key has already been redeemed.", discord.Color.red()))

    now = time.time()
    days = entry["duration_days"]
    expires_at = None if days is None else now + days * 86400

    entry["used"] = True
    entry["used_by"] = ctx.author.id
    entry["used_at"] = now
    licenses.setdefault("activations", {})[str(ctx.author.id)] = {
        "key": key, "expires_at": expires_at, "duration_label": entry["duration_label"],
    }
    save_licenses()

    expires_display = "Never (Lifetime) ♾️" if expires_at is None else f"<t:{int(expires_at)}:F>"
    embed = brand_embed(f"🎉 Welcome to {BRAND_NAME}", color=discord.Color.green())
    embed.add_field(name="License", value=entry["duration_label"].capitalize(), inline=True)
    embed.add_field(name="Expires", value=expires_display, inline=True)
    embed.add_field(name="Next step", value="Run `!setup` to configure your server for protection.", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def mylicense(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    status = get_license_status(ctx.author.id)
    if not status:
        return await ctx.send(embed=brand_embed("No License Found", "Run `!activate <key>` to get started.", discord.Color.orange()))
    embed = brand_embed(f"📋 Your {BRAND_NAME} License", color=discord.Color.green() if status["valid"] else discord.Color.red())
    embed.add_field(name="Status", value="✅ Active" if status["valid"] else "❌ Expired", inline=True)
    embed.add_field(name="Expires", value=status["expires_display"], inline=True)
    await ctx.send(embed=embed)

@bot.command(name="generate", aliases=["genk"])
async def generate_keys(ctx, duration: str = None, user_id: str = None, count: int = 1):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return

    if duration is None or user_id is None:
        embed = brand_embed(f"🔑 {BRAND_NAME} License Generator", "Generate license keys locked to a specific customer's Discord account.", EMBED_COLOR)
        embed.add_field(name="Usage", value="`!genk <duration> <user_id> [count]`", inline=False)
        embed.add_field(
            name="Duration options",
            value=(
                "`7d` — 7 days\n`30d` — 30 days\n`90d` — 90 days\n"
                "`1m` — 1 month\n`6m` — 6 months\n`1y` — 1 year\n"
                "`lifetime` — never expires\n"
                "*(any `<number>d/w/m/y` works, e.g. `45d`, `2y`)*"
            ),
            inline=False
        )
        embed.add_field(
            name="Examples",
            value=(
                "`!genk 30d 123456789012345678` → 1 key, 30 days, locked to that user\n"
                "`!genk lifetime 123456789012345678` → 1 lifetime key for that user\n"
                "`!genk 1y 123456789012345678 2` → 2 backup keys, same user, 1 year each"
            ),
            inline=False
        )
        return await ctx.send(embed=embed)

    if not valid_discord_id(user_id):
        return await ctx.send(embed=brand_embed("❌ Invalid User ID", "Should be 15–20 digits.", discord.Color.red()))

    label, days = parse_duration(duration)
    if label is None:
        return await ctx.send(embed=brand_embed("❌ Invalid Duration", "Run `!genk` with no arguments to see valid options.", discord.Color.red()))

    bound_id = int(user_id)
    count = max(1, min(count, 10))
    new_keys = []
    for _ in range(count):
        key = gen_license_key()
        licenses["keys"][key] = {
            "duration_label": label, "duration_days": days,
            "bound_user_id": bound_id,
            "used": False, "used_by": None,
            "created_at": time.time(), "used_at": None,
        }
        new_keys.append(key)
    save_licenses()

    duration_display = "Lifetime ♾️" if days is None else f"{label} ({days} days)"
    embed = brand_embed(f"🔑 {count} License Key{'s' if count > 1 else ''} Generated", color=discord.Color.green())
    embed.add_field(name="Duration", value=duration_display, inline=True)
    embed.add_field(name="Bound to", value=f"`{bound_id}`", inline=True)
    embed.add_field(name="Keys", value="\n".join(f"`{k}`" for k in new_keys), inline=False)
    await ctx.send(embed=embed)

@bot.command(name="bl")
async def blacklist_user(ctx, user_id: int = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not user_id:
        return await ctx.send(embed=brand_embed("Usage", "`!bl <user_id>`", discord.Color.orange()))
    blacklist.add(user_id)
    save_blacklist()
    await ctx.send(embed=brand_embed("🚫 User Blacklisted", f"`{user_id}` can no longer use {BRAND_NAME}.", discord.Color.red()))

@bot.command(name="unbl")
async def unblacklist_user(ctx, user_id: int = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not user_id:
        return await ctx.send(embed=brand_embed("Usage", "`!unbl <user_id>`", discord.Color.orange()))
    blacklist.discard(user_id)
    save_blacklist()
    await ctx.send(embed=brand_embed("✅ User Unblacklisted", f"`{user_id}` has regained access.", discord.Color.green()))

# =================================================================
# ACCOUNT SWITCH REQUEST SYSTEM
# =================================================================

@bot.command()
async def useridswitch(ctx, *, reason: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return

    if not reason:
        embed = brand_embed(
            "🔄 Account Switch Request",
            "Usage: `!useridswitch <reason>`\n\n"
            "⚠️ Include the **User ID of your OLD account** (the one your key is currently bound to) "
            "in your reason, so it can be verified.\n\n"
            "You must first attempt `!activate <key>` with the key in question before running this.",
            EMBED_COLOR
        )
        return await ctx.send(embed=embed)

    context = pending_switch_context.get(ctx.author.id)
    if not context:
        return await ctx.send(embed=brand_embed(
            "⚠️ No Pending Attempt Found",
            "Run `!activate <your key>` first (it will fail since it's bound to another account), then run `!useridswitch <reason>`.",
            discord.Color.orange()
        ))

    key = context["key"]
    entry = licenses["keys"].get(key)
    if not entry:
        del pending_switch_context[ctx.author.id]
        return await ctx.send(embed=brand_embed("❌ Key No Longer Exists", "Please contact support.", discord.Color.red()))

    req_id = str(switch_requests["next_id"])
    switch_requests["next_id"] += 1
    switch_requests["requests"][req_id] = {
        "key": key,
        "old_user_id": entry["bound_user_id"],
        "new_user_id": ctx.author.id,
        "reason": reason,
        "status": "pending",
        "created_at": time.time(),
    }
    save_switch_requests()

    await ctx.send(embed=brand_embed(
        "✅ Switch Request Submitted",
        f"Request `#{req_id}` has been sent for review. You'll be notified once it's approved or denied.",
        discord.Color.green()
    ))

    try:
        master = await bot.fetch_user(MASTER_USER_ID)
        alert = brand_embed("🔔 New Account Switch Request", color=discord.Color.orange())
        alert.add_field(name="Request ID", value=req_id, inline=True)
        alert.add_field(name="Key", value=f"`{key}`", inline=True)
        alert.add_field(name="Old User ID (bound)", value=f"`{entry['bound_user_id']}`", inline=False)
        alert.add_field(name="New User ID (requester)", value=f"`{ctx.author.id}` ({ctx.author})", inline=False)
        alert.add_field(name="Reason given", value=reason, inline=False)
        alert.add_field(name="To approve", value=f"`!approveswitch {req_id}`", inline=True)
        alert.add_field(name="To deny", value=f"`!denyswitch {req_id} <reason>`", inline=True)
        await master.send(embed=alert)
    except Exception:
        pass

@bot.command()
async def approveswitch(ctx, req_id: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not req_id or req_id not in switch_requests["requests"]:
        return await ctx.send(embed=brand_embed("Usage", "`!approveswitch <request_id>`", discord.Color.orange()))

    req = switch_requests["requests"][req_id]
    if req["status"] != "pending":
        return await ctx.send(embed=brand_embed("Already Resolved", f"That request is already `{req['status']}`.", discord.Color.orange()))

    old_id, new_id, key = req["old_user_id"], req["new_user_id"], req["key"]

    licenses["keys"][key]["bound_user_id"] = new_id

    if str(old_id) in licenses.get("activations", {}):
        licenses["activations"][str(new_id)] = licenses["activations"].pop(str(old_id))
        licenses["activations"][str(new_id)]["key"] = key

    migrated_config = False
    if str(old_id) in configs:
        configs[str(new_id)] = configs.pop(str(old_id))
        migrated_config = True

    save_licenses()
    save_configs()
    rebuild_guild_index()

    req["status"] = "approved"
    req["resolved_at"] = time.time()
    save_switch_requests()
    pending_switch_context.pop(new_id, None)

    await ctx.send(embed=brand_embed(
        "✅ Switch Approved",
        f"Key `{key}` and license now bound to `{new_id}`." + (" Server config migrated too." if migrated_config else ""),
        discord.Color.green()
    ))

    try:
        new_user = await bot.fetch_user(new_id)
        embed = brand_embed(
            "✅ Account Switch Approved",
            "Your license" + (" and full server setup have" if migrated_config else " has") + " been transferred to this account.\n\n"
            + ("Run `!status` to confirm everything's in order." if migrated_config else "Run `!activate <your key>` to finish."),
            discord.Color.green()
        )
        await new_user.send(embed=embed)
    except Exception:
        pass

@bot.command()
async def denyswitch(ctx, req_id: str = None, *, reason: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not req_id or req_id not in switch_requests["requests"]:
        return await ctx.send(embed=brand_embed("Usage", "`!denyswitch <request_id> <reason>`", discord.Color.orange()))

    req = switch_requests["requests"][req_id]
    if req["status"] != "pending":
        return await ctx.send(embed=brand_embed("Already Resolved", f"That request is already `{req['status']}`.", discord.Color.orange()))

    req["status"] = "denied"
    req["deny_reason"] = reason or "No reason given."
    req["resolved_at"] = time.time()
    save_switch_requests()

    await ctx.send(embed=brand_embed("❌ Request Denied", f"Request `{req_id}` denied.", discord.Color.red()))

    try:
        new_user = await bot.fetch_user(req["new_user_id"])
        embed = brand_embed("❌ Account Switch Denied", f"Reason: {req['deny_reason']}\n\nContact support if you believe this is a mistake.", discord.Color.red())
        await new_user.send(embed=embed)
    except Exception:
        pass

@bot.command()
async def switchrequests(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    pending = {k: v for k, v in switch_requests["requests"].items() if v["status"] == "pending"}
    if not pending:
        return await ctx.send(embed=brand_embed("🔄 Pending Switch Requests", "None right now.", EMBED_COLOR))
    embed = brand_embed("🔄 Pending Switch Requests", color=EMBED_COLOR)
    for rid, req in pending.items():
        embed.add_field(
            name=f"Request #{rid}",
            value=f"Key: `{req['key']}`\nOld: `{req['old_user_id']}` → New: `{req['new_user_id']}`\nReason: {req['reason']}",
            inline=False
        )
    await ctx.send(embed=embed)

# =================================================================
# SETUP WIZARD
# =================================================================

async def setup_owner_role(guild, member):
    role = discord.utils.get(guild.roles, name="OWNER")
    if not role:
        try:
            role = await guild.create_role(
                name="OWNER", color=discord.Color.red(),
                permissions=discord.Permissions(administrator=True),
                hoist=True, reason=f"Auto-created during {BRAND_NAME} setup",
            )
        except Exception as e:
            return None, False, str(e)
    try:
        await member.add_roles(role, reason="Initial owner assignment during setup")
        return role, True, None
    except Exception as e:
        return role, False, str(e)

async def create_log_channel(guild, owner_member, cfg=None):
    # Replace any existing log channel with a brand new one
    old_id = cfg.get("log_channel_id") if cfg else None
    to_remove = []
    if old_id:
        ch = guild.get_channel(old_id)
        if ch:
            to_remove.append(ch)
    for ch in guild.text_channels:
        if ch.name == "perc-logs" and ch not in to_remove:
            to_remove.append(ch)
    for ch in to_remove:
        try:
            await ch.delete(reason=f"{BRAND_NAME}: replacing log channel with a fresh one")
        except Exception:
            pass

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        owner_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    try:
        channel = await guild.create_text_channel(
            "perc-logs", overwrites=overwrites,
            reason=f"{BRAND_NAME}: auto-created security log channel",
            topic=f"🛡️ {BRAND_NAME} security logs — raid detection, lockdowns, and recovery events."
        )
        try:
            await channel.send(embed=brand_embed(
                f"🛡️ {BRAND_NAME} Logs Initialized",
                "This channel shows real-time security events: raid detection, lockdowns, bans, and recovery actions.",
                EMBED_COLOR
            ))
        except Exception:
            pass
        return channel, None
    except Exception as e:
        return None, str(e)

@bot.command()
async def setup(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_activated(ctx.author.id):
        return await ctx.send(embed=brand_embed("🔒 License Required", "Purchase a key, then run `!activate YOUR-KEY-HERE`.", discord.Color.orange()))
    existing = get_config(ctx.author.id)
    if existing and existing.get("setup_complete"):
        return await ctx.send(embed=brand_embed("✅ Already Configured", "Use `!resetup` to change it, or `!myconfig` to view settings.", discord.Color.green()))

    start_session(ctx.author.id, "await_user_id")
    embed = brand_embed(
        f"👋 Welcome to {BRAND_NAME} Setup",
        "**Step 1 of 3 — Confirm Your User ID**\n\n"
        "Enable Developer Mode: `User Settings → Advanced → Developer Mode` (toggle ON)\n\n"
        "Then right-click your own name/avatar and click **Copy User ID**.\n\n"
        f"Paste that ID here to confirm. (You have {SETUP_TIMEOUT//60} minutes)",
        EMBED_COLOR
    )
    await ctx.send(embed=embed)

@bot.command()
async def resetup(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_activated(ctx.author.id):
        return await ctx.send(embed=brand_embed("🔒 License Required", "Run `!activate YOUR-KEY-HERE`.", discord.Color.orange()))
    existing = get_config(ctx.author.id)
    if not existing:
        return await ctx.send(embed=brand_embed("No Config Found", "Use `!setup` to create one.", discord.Color.orange()))
    start_session(ctx.author.id, "await_user_id")
    await ctx.send(embed=brand_embed("🔄 Re-Running Setup", "**Step 1 of 3 — Confirm Your User ID**\nPaste your Discord User ID.", EMBED_COLOR))

@bot.command()
async def myconfig(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    cfg = get_config(ctx.author.id)
    if not cfg or not cfg.get("setup_complete"):
        return await ctx.send(embed=brand_embed("No Config Found", "Use `!setup` to create one.", discord.Color.orange()))
    guild = bot.get_guild(cfg["guild_id"])
    log_ch = guild.get_channel(cfg.get("log_channel_id")) if guild and cfg.get("log_channel_id") else None
    embed = brand_embed("📋 Your Configuration", color=EMBED_COLOR)
    embed.add_field(name="Server", value=f"{guild.name if guild else 'Unknown'} (`{cfg['guild_id']}`)", inline=False)
    embed.add_field(name="Owner Role", value=cfg["owner_role_name"], inline=True)
    embed.add_field(name="Log Channel", value=log_ch.mention if log_ch else "Missing — run !fixlogs", inline=True)
    embed.add_field(name="Invite Link", value=cfg["invite_link"], inline=False)
    embed.add_field(name="Trusted Users", value=str(len(cfg.get("trusted", []))), inline=True)
    await ctx.send(embed=embed)

async def handle_setup_message(message):
    user_id = message.author.id
    session = setup_sessions.get(user_id)
    if not session:
        return False

    if time.time() > session["expires"]:
        del setup_sessions[user_id]
        await message.channel.send(embed=brand_embed("⏱️ Setup Timed Out", "Run `!setup` to start again.", discord.Color.orange()))
        return True

    touch_session(user_id)
    content = message.content.strip()
    stage = session["stage"]
    data = session["data"]

    if stage == "await_user_id":
        if not content.isdigit() or int(content) != user_id:
            await message.channel.send(embed=brand_embed("⚠️ Mismatch", "That doesn't match your account. Copy YOUR OWN ID and try again.", discord.Color.orange()))
            return True
        data["user_id"] = user_id
        session["stage"] = "await_guild_id"
        invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot"
        embed = brand_embed(
            "✅ Identity Confirmed",
            "**Step 2 of 3 — Server ID**\n\n"
            f"Invite me to your server first if you haven't:\n[Click here to invite]({invite_bot_url})\n\n"
            "Then right-click your **server icon** and click **Copy Server ID**. Paste it here.",
            discord.Color.green()
        )
        await message.channel.send(embed=embed)
        return True

    if stage == "await_guild_id":
        if not content.isdigit():
            await message.channel.send(embed=brand_embed("⚠️ Invalid ID", "Not a valid Server ID. Try again.", discord.Color.orange()))
            return True
        guild_id = int(content)
        guild = bot.get_guild(guild_id)
        if not guild:
            invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot"
            await message.channel.send(embed=brand_embed("Not Found", f"I'm not in that server yet.\n[Invite me]({invite_bot_url})\nThen resend the ID.", discord.Color.orange()))
            return True
        member = guild.get_member(user_id)
        if not member:
            await message.channel.send(embed=brand_embed("Not a Member", "You're not a member of that server. Join it, then try again.", discord.Color.orange()))
            return True
        if not (member.guild_permissions.administrator or guild.owner_id == member.id):
            await message.channel.send(embed=brand_embed("❌ Permission Denied", "You must be the server owner or an Administrator.", discord.Color.red()))
            return True
        existing_owner = get_owner_id_for_guild(guild_id)
        if existing_owner and existing_owner != user_id:
            await message.channel.send(embed=brand_embed("⚠️ Already Configured", "This server is already configured by someone else. Setup cancelled.", discord.Color.orange()))
            del setup_sessions[user_id]
            return True

        data["guild_id"] = guild_id
        session["stage"] = "await_invite"
        embed = brand_embed(
            "✅ Server Verified",
            "**Step 3 of 3 — Permanent Invite Link**\n\n"
            "Create an invite with **Expire After: Never** and **Max Uses: No limit**, then paste it here.",
            discord.Color.green()
        )
        await message.channel.send(embed=embed)
        return True

    if stage == "await_invite":
        if not content.startswith("https://discord.gg/") and not content.startswith("https://discord.com/invite/"):
            await message.channel.send(embed=brand_embed("⚠️ Invalid Link", "That doesn't look like a valid invite link. Try again.", discord.Color.orange()))
            return True

        guild = bot.get_guild(data["guild_id"])
        member = guild.get_member(user_id)
        existing_cfg = get_config(user_id) or {}
        role, assigned, err = await setup_owner_role(guild, member)
        log_channel, log_err = await create_log_channel(guild, member, existing_cfg)

        configs[str(user_id)] = {
            "guild_id": data["guild_id"],
            "owner_role_name": "OWNER",
            "invite_link": content,
            "trusted": [],
            "log_channel_id": log_channel.id if log_channel else None,
            "setup_complete": True,
        }
        save_configs()
        rebuild_guild_index()
        del setup_sessions[user_id]

        role_status = "✅ Created and assigned to you." if (role and assigned) else (
            "⚠️ Created, but couldn't assign — move my role above OWNER in Server Settings → Roles, then run `!owner`."
            if role else f"❌ Failed ({err}). Run `!resetup` after fixing my permissions."
        )
        log_status = f"✅ {log_channel.mention}" if log_channel else f"⚠️ Failed ({log_err}). Run `!fixlogs` later."

        embed = brand_embed(f"🎉 {BRAND_NAME} Setup Complete", color=discord.Color.green())
        embed.add_field(name="OWNER Role", value=role_status, inline=False)
        embed.add_field(name="Log Channel", value=log_status, inline=False)
        embed.add_field(
            name="Next Steps",
            value="Keep my role at the **top** of Server Settings → Roles.\nRun `!backup` now.\nType `!help` to see everything I can do.",
            inline=False
        )
        await message.channel.send(embed=embed)
        return True

    return False

def require_setup():
    async def predicate(ctx):
        if not isinstance(ctx.channel, discord.DMChannel):
            return False
        if not is_activated(ctx.author.id):
            status = get_license_status(ctx.author.id)
            if status and not status["valid"]:
                await ctx.send(embed=brand_embed("⏳ License Expired", "Purchase a new key and run `!activate` to renew.", discord.Color.orange()))
            else:
                await ctx.send(embed=brand_embed("🔒 License Required", "Run `!activate <key>`.", discord.Color.orange()))
            return False
        cfg = get_config(ctx.author.id)
        if not cfg or not cfg.get("setup_complete"):
            await ctx.send(embed=brand_embed("Setup Required", "Type `!setup` to begin.", discord.Color.orange()))
            return False
        return True
    return commands.check(predicate)

# =================================================================
# RECOVERY COMMANDS
# =================================================================

@bot.command()
@require_setup()
async def invite(ctx):
    cfg = get_config(ctx.author.id)
    await ctx.send(embed=brand_embed("🔗 Server Invite", cfg['invite_link'], EMBED_COLOR))

@bot.command()
@require_setup()
async def owner(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    member = guild.get_member(ctx.author.id) if guild else None
    if not member:
        return await ctx.send(embed=brand_embed("Not in Server", "Join first with `!invite`.", discord.Color.orange()))
    role = discord.utils.get(guild.roles, name=cfg["owner_role_name"])
    if not role:
        return await ctx.send(embed=brand_embed("Role Missing", f"'{cfg['owner_role_name']}' not found. Use `!resetup`.", discord.Color.red()))
    try:
        await member.add_roles(role, reason="Owner recovery")
        await ctx.send(embed=brand_embed("✅ Owner Role Restored", color=discord.Color.green()))
    except discord.Forbidden:
        await ctx.send(embed=brand_embed("❌ Permission Denied", "Move my role above OWNER in Server Settings → Roles.", discord.Color.red()))

@bot.command()
@require_setup()
async def unban(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    try:
        user = await bot.fetch_user(ctx.author.id)
        await guild.unban(user, reason="Manual owner unban")
        await ctx.send(embed=brand_embed("✅ Unbanned", f"Rejoin: {cfg['invite_link']}", discord.Color.green()))
    except discord.NotFound:
        await ctx.send(embed=brand_embed("Not Banned", "You're not currently banned.", EMBED_COLOR))
    except discord.Forbidden:
        await ctx.send(embed=brand_embed("❌ Permission Denied", "I lack Ban Members permission there.", discord.Color.red()))

@bot.command()
@require_setup()
async def trust(ctx, member_id: int):
    cfg = get_config(ctx.author.id)
    if member_id not in cfg["trusted"]:
        cfg["trusted"].append(member_id)
        save_configs()
    await ctx.send(embed=brand_embed("✅ Trusted", f"`{member_id}` added to trusted list.", discord.Color.green()))

@bot.command()
@require_setup()
async def untrust(ctx, member_id: int):
    cfg = get_config(ctx.author.id)
    if member_id in cfg["trusted"]:
        cfg["trusted"].remove(member_id)
        save_configs()
    await ctx.send(embed=brand_embed("✅ Untrusted", f"`{member_id}` removed from trusted list.", discord.Color.green()))

@bot.command()
@require_setup()
async def fixlogs(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    member = guild.get_member(ctx.author.id)
    channel, err = await create_log_channel(guild, member, cfg)
    if channel:
        cfg["log_channel_id"] = channel.id
        save_configs()
        await ctx.send(embed=brand_embed("✅ Log Channel Refreshed", f"Fresh channel created: {channel.mention}", discord.Color.green()))
    else:
        await ctx.send(embed=brand_embed("❌ Failed", f"{err}\nCheck Manage Channels permission.", discord.Color.red()))

# =================================================================
# BACKUP / RESTORE
# =================================================================

def serialize_overwrites(channel):
    result = {}
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = ow.pair()
            result[f"role:{target.name}"] = {"allow": allow.value, "deny": deny.value}
    return result

async def do_backup(guild):
    data = {"roles": [], "categories": [], "channels": []}
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default() or role >= guild.me.top_role:
            continue
        data["roles"].append({
            "name": role.name, "color": role.color.value,
            "permissions": role.permissions.value,
            "hoist": role.hoist, "mentionable": role.mentionable,
            "position": role.position,
        })
    for cat in guild.categories:
        data["categories"].append({"name": cat.name, "position": cat.position, "overwrites": serialize_overwrites(cat)})
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        entry = {
            "name": ch.name, "type": str(ch.type),
            "category": ch.category.name if ch.category else None,
            "position": ch.position, "overwrites": serialize_overwrites(ch),
        }
        if isinstance(ch, discord.TextChannel):
            entry.update(topic=ch.topic, nsfw=ch.nsfw, slowmode_delay=ch.slowmode_delay)
        elif isinstance(ch, discord.VoiceChannel):
            entry.update(bitrate=ch.bitrate, user_limit=ch.user_limit)
        data["channels"].append(entry)
    data["_backed_up_at"] = time.time()
    with open(backup_path(guild.id), "w") as f:
        json.dump(data, f, indent=2)
    return data

def build_overwrites(saved, role_map):
    result = {}
    for key, val in saved.items():
        if key.startswith("role:"):
            role = role_map.get(key.split("role:", 1)[1])
            if role:
                result[role] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(val["allow"]), discord.Permissions(val["deny"])
                )
    return result

async def do_restore(guild, dm):
    path = backup_path(guild.id)
    if not os.path.exists(path):
        return await dm.send(embed=brand_embed("❌ No Backup Found", color=discord.Color.red()))
    with open(path) as f:
        data = json.load(f)

    await dm.send(embed=brand_embed("🔧 Restoring Roles...", color=EMBED_COLOR))
    role_map = {}
    for r in sorted(data["roles"], key=lambda x: x["position"]):
        existing = discord.utils.get(guild.roles, name=r["name"])
        if existing:
            role_map[r["name"]] = existing
            continue
        try:
            role_map[r["name"]] = await guild.create_role(
                name=r["name"], color=discord.Color(r["color"]),
                permissions=discord.Permissions(r["permissions"]),
                hoist=r["hoist"], mentionable=r["mentionable"], reason="Restore",
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            await dm.send(embed=brand_embed("⚠️ Role Failed", f"'{r['name']}': {e}", discord.Color.orange()))

    await dm.send(embed=brand_embed("🔧 Restoring Categories...", color=EMBED_COLOR))
    category_map = {}
    for c in sorted(data["categories"], key=lambda x: x["position"]):
        try:
            ow = build_overwrites(c["overwrites"], role_map)
            category_map[c["name"]] = await guild.create_category(c["name"], overwrites=ow, reason="Restore")
            await asyncio.sleep(0.5)
        except Exception as e:
            await dm.send(embed=brand_embed("⚠️ Category Failed", f"'{c['name']}': {e}", discord.Color.orange()))

    await dm.send(embed=brand_embed("🔧 Restoring Channels...", color=EMBED_COLOR))
    for ch in sorted(data["channels"], key=lambda x: x["position"]):
        try:
            ow = build_overwrites(ch["overwrites"], role_map)
            cat = category_map.get(ch["category"])
            if ch["type"] == "text":
                await guild.create_text_channel(ch["name"], category=cat, overwrites=ow,
                    topic=ch.get("topic"), nsfw=ch.get("nsfw", False),
                    slowmode_delay=ch.get("slowmode_delay", 0), reason="Restore")
            elif ch["type"] == "voice":
                await guild.create_voice_channel(ch["name"], category=cat, overwrites=ow,
                    bitrate=ch.get("bitrate", 64000), user_limit=ch.get("user_limit", 0), reason="Restore")
            await asyncio.sleep(0.5)
        except Exception as e:
            await dm.send(embed=brand_embed("⚠️ Channel Failed", f"'{ch['name']}': {e}", discord.Color.orange()))

    await dm.send(embed=brand_embed("✅ Restore Complete", "Ordering/pins/per-member overwrites may need manual fixing.", discord.Color.green()))

@bot.command()
@require_setup()
async def backup(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    await do_backup(guild)
    await ctx.send(embed=brand_embed("✅ Backup Saved", color=discord.Color.green()))

@bot.command()
@require_setup()
async def restore(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    await do_restore(guild, ctx)

# =================================================================
# LOCKDOWN
# =================================================================

lockdown_active_map = {}

async def do_lockdown(guild):
    everyone = guild.default_role
    state = {}
    count = 0
    for channel in guild.text_channels:
        try:
            ow = channel.overwrites_for(everyone)
            state[str(channel.id)] = {"type": "text", "value": ow.send_messages}
            ow.send_messages = False
            await channel.set_permissions(everyone, overwrite=ow, reason="Lockdown")
            count += 1
            await asyncio.sleep(0.3)
        except Exception:
            pass
    for channel in guild.voice_channels:
        try:
            ow = channel.overwrites_for(everyone)
            state[str(channel.id)] = {"type": "voice", "value": ow.connect}
            ow.connect = False
            await channel.set_permissions(everyone, overwrite=ow, reason="Lockdown")
            count += 1
            await asyncio.sleep(0.3)
        except Exception:
            pass
    with open(lockdown_path(guild.id), "w") as f:
        json.dump(state, f)
    lockdown_active_map[guild.id] = True
    return count

async def do_unlock(guild):
    path = lockdown_path(guild.id)
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        state = json.load(f)
    everyone = guild.default_role
    count = 0
    for channel_id_str, info in state.items():
        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            continue
        try:
            ow = channel.overwrites_for(everyone)
            if info["type"] == "text":
                ow.send_messages = info["value"]
            else:
                ow.connect = info["value"]
            await channel.set_permissions(everyone, overwrite=ow, reason="Unlock")
            count += 1
            await asyncio.sleep(0.3)
        except Exception:
            pass
    os.remove(path)
    lockdown_active_map[guild.id] = False
    return count

@bot.command()
@require_setup()
async def lockdown(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    if lockdown_active_map.get(guild.id):
        return await ctx.send(embed=brand_embed("⚠️ Already Locked", color=discord.Color.orange()))
    count = await do_lockdown(guild)
    await log_embed(ctx.author.id, guild, "🔒 Lockdown Engaged",
        f"Manually activated by the owner. **{count}** channels restricted (exact prior state saved).",
        discord.Color.red())

@bot.command()
@require_setup()
async def unlock(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    if not lockdown_active_map.get(guild.id) and not os.path.exists(lockdown_path(guild.id)):
        return await ctx.send(embed=brand_embed("Not Locked", color=EMBED_COLOR))
    count = await do_unlock(guild)
    await log_embed(ctx.author.id, guild, "🔓 Lockdown Lifted",
        f"Manually lifted by the owner. **{count}** channels restored to their exact original state.",
        discord.Color.green())

# =================================================================
# KILL SWITCH
# =================================================================

pending_kills = {}

@bot.command()
@require_setup()
async def kill(ctx):
    code = gen_code()
    pending_kills[ctx.author.id] = {"stage": 1, "code": code}
    embed = brand_embed(
        "⚠️ EMERGENCY WIPE",
        f"Deletes every channel and role. Irreversible without `!restore`.\n\n"
        f"Reply within {CONFIRM_TIMEOUT}s with exactly:\n`{code}`\n\nType `!cancel` to abort anytime.",
        discord.Color.red()
    )
    await ctx.send(embed=embed)
    async def expire(uid, stage, c):
        await asyncio.sleep(CONFIRM_TIMEOUT)
        e = pending_kills.get(uid)
        if e and e["stage"] == stage and e["code"] == c:
            del pending_kills[uid]
            await ctx.send(embed=brand_embed("⏱️ Timed Out", "Cancelled.", discord.Color.orange()))
    bot.loop.create_task(expire(ctx.author.id, 1, code))

@bot.command()
@require_setup()
async def cancel(ctx):
    if ctx.author.id in pending_kills:
        del pending_kills[ctx.author.id]
        await ctx.send(embed=brand_embed("✅ Cancelled", "Kill sequence cancelled.", discord.Color.green()))
    else:
        await ctx.send(embed=brand_embed("Nothing Pending", color=EMBED_COLOR))

async def execute_kill(guild, dm):
    deleted_c = failed_c = deleted_r = failed_r = 0
    for channel in list(guild.channels):
        try:
            await channel.delete(reason="Emergency kill")
            deleted_c += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed_c += 1
    for role in list(guild.roles):
        if role.is_default() or role >= guild.me.top_role:
            continue
        try:
            await role.delete(reason="Emergency kill")
            deleted_r += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed_r += 1
    embed = brand_embed("✅ Wipe Complete", color=discord.Color.green())
    embed.add_field(name="Channels", value=f"{deleted_c} deleted / {failed_c} failed", inline=True)
    embed.add_field(name="Roles", value=f"{deleted_r} deleted / {failed_r} failed", inline=True)
    embed.add_field(name="Next Step", value="Run `!restore` to rebuild.", inline=False)
    await dm.send(embed=embed)

# =================================================================
# STATUS / HELP
# =================================================================

@bot.command()
@require_setup()
async def status(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    perms = guild.me.guild_permissions
    backup_exists = os.path.exists(backup_path(guild.id))
    backup_age = "Never"
    if backup_exists:
        with open(backup_path(guild.id)) as f:
            ts = json.load(f).get("_backed_up_at")
        if ts:
            backup_age = f"{int((time.time()-ts)//60)}m ago"
    status_info = get_license_status(ctx.author.id)
    log_ch = guild.get_channel(cfg.get("log_channel_id")) if cfg.get("log_channel_id") else None

    embed = brand_embed(f"🩺 {BRAND_NAME} Status", guild.name, EMBED_COLOR)
    embed.add_field(name="License", value=status_info['expires_display'] if status_info else 'Unknown', inline=False)
    embed.add_field(name="Last Backup", value=backup_age, inline=True)
    embed.add_field(name="Log Channel", value=(log_ch.mention if log_ch else "❌ Missing — !fixlogs"), inline=True)
    embed.add_field(name="Lockdown", value=("🔒 Active" if lockdown_active_map.get(guild.id) else "🔓 Inactive"), inline=True)
    embed.add_field(name="Top Role", value=guild.me.top_role.name, inline=True)
    perms_text = (
        f"Ban Members: {'✅' if perms.ban_members else '❌'}\n"
        f"Manage Roles: {'✅' if perms.manage_roles else '❌'}\n"
        f"Manage Channels: {'✅' if perms.manage_channels else '❌'}\n"
        f"View Audit Log: {'✅' if perms.view_audit_log else '❌'}\n"
        f"Administrator: {'✅' if perms.administrator else '❌'}"
    )
    embed.add_field(name="Permissions", value=perms_text, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def custom_help(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    embed = brand_embed(f"🛡️ {BRAND_NAME} — Command Center", color=EMBED_COLOR)
    embed.add_field(name="🚀 Getting Started", value=(
        "`!activate <key>` — activate your license\n"
        "`!mylicense` — check license status\n"
        "`!setup` / `!resetup` — configure your server\n"
        "`!myconfig` — view your settings\n"
        "`!useridswitch <reason>` — move key to a new account"
    ), inline=False)
    embed.add_field(name="🔑 Recovery", value=(
        "`!invite` — get invite link\n"
        "`!owner` — restore OWNER role\n"
        "`!unban` — unban yourself"
    ), inline=False)
    embed.add_field(name="💾 Backup", value="`!backup` / `!restore`", inline=False)
    embed.add_field(name="💥 Emergency", value="`!kill` (2-step confirm) / `!cancel`", inline=False)
    embed.add_field(name="🔐 Server Control", value="`!lockdown` / `!unlock`", inline=False)
    embed.add_field(name="🤝 Trust", value="`!trust <id>` / `!untrust <id>`", inline=False)
    embed.add_field(name="📜 Logs", value="`!fixlogs` — refresh the security log channel", inline=False)
    embed.add_field(name="📊 Status", value="`!status`", inline=False)
    embed.add_field(name="🛡️ Automatic Protection", value=(
        "• Auto-unban if you're banned\n"
        "• Anti-nuke: detects & neutralizes mass bans/kicks/deletions\n"
        "• Mass-join raid detection with auto-lockdown\n"
        "• All events logged to your private #perc-logs channel"
    ), inline=False)
    if is_master(ctx.author.id):
        embed.add_field(name="🔑 Admin Only", value=(
            "`!genk <duration> <user_id> [count]`\n"
            "`!bl <id>` / `!unbl <id>`\n"
            "`!switchrequests` / `!approveswitch <id>` / `!denyswitch <id> <reason>`"
        ), inline=False)
    await ctx.send(embed=embed)

# =================================================================
# MESSAGE ROUTER
# =================================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if is_blacklisted(message.author.id):
        return

    await bot.process_commands(message)
    if not isinstance(message.channel, discord.DMChannel):
        return

    if await handle_setup_message(message):
        return

    entry = pending_kills.get(message.author.id)
    if not entry:
        return
    content = message.content.strip()

    if entry["stage"] == 1 and content == entry["code"]:
        code2 = gen_code()
        pending_kills[message.author.id] = {"stage": 2, "code": code2}
        embed = brand_embed("🔴 FINAL CONFIRMATION", f"Reply within {CONFIRM_TIMEOUT}s:\n`{code2}`\n(!cancel to abort)", discord.Color.red())
        await message.channel.send(embed=embed)
        async def expire2():
            await asyncio.sleep(CONFIRM_TIMEOUT)
            e = pending_kills.get(message.author.id)
            if e and e["stage"] == 2 and e["code"] == code2:
                del pending_kills[message.author.id]
                await message.channel.send(embed=brand_embed("⏱️ Timed Out", "Cancelled.", discord.Color.orange()))
        bot.loop.create_task(expire2())

    elif entry["stage"] == 2 and content == entry["code"]:
        del pending_kills[message.author.id]
        cfg = get_config(message.author.id)
        guild = bot.get_guild(cfg["guild_id"])
        await message.channel.send(embed=brand_embed("💾 Backing Up...", color=EMBED_COLOR))
        await do_backup(guild)
        await message.channel.send(embed=brand_embed("💥 Executing Wipe...", color=discord.Color.red()))
        await execute_kill(guild, message.channel)

# =================================================================
# ANTI-NUKE EVENTS
# =================================================================

recent_bans, recent_kicks, recent_ch_del, recent_role_del = {}, {}, {}, {}
recent_ban_victims = {}
neutralized_actors = set()
pending_raid_welcomes = set()
recent_joins_map = {}
last_mass_join_map = {}

def _prune(lst, window):
    now = time.time()
    return [t for t in lst if now - t <= window]

async def try_unban_owner(guild, owner_id, cfg, reason):
    try:
        user = await bot.fetch_user(owner_id)
    except Exception:
        return
    try:
        await guild.fetch_ban(user)
    except discord.NotFound:
        try:
            embed = brand_embed("⚠️ Left the Server", f"You left/were removed from **{guild.name}** ({reason}), but you're not banned.\n\n[Rejoin here]({cfg['invite_link']})", discord.Color.orange())
            await user.send(embed=embed)
        except discord.Forbidden:
            pass
        return
    except Exception:
        return
    try:
        await guild.unban(user, reason=f"Auto-unban ({reason})")
        embed = brand_embed("🚨 Auto-Unban Triggered", f"You were banned from **{guild.name}** ({reason}) — automatically unbanned.\n\n[Rejoin here]({cfg['invite_link']})", discord.Color.green())
        await user.send(embed=embed)
        await log_embed(owner_id, guild, "🚨 Owner Auto-Restored", f"The server owner was banned ({reason}) and has been automatically unbanned.", discord.Color.green())
    except Exception:
        pass

async def handle_raid(guild, owner_id, cfg, actor, action_desc, victim_ids=None):
    member = guild.get_member(actor.id)
    neutralized = False
    method = "None"
    if member:
        try:
            await guild.ban(member, reason=f"Anti-nuke: {action_desc}")
            neutralized = True
            method = "Banned"
        except Exception:
            try:
                roles = [r for r in member.roles if not r.is_default()]
                if roles:
                    await member.remove_roles(*roles, reason="Anti-nuke role strip")
                neutralized = True
                method = "Roles stripped"
            except Exception:
                method = "Failed"

    await log_embed(
        owner_id, guild, "🚨 Raid Detected & Response Executed",
        "Suspicious activity was detected and Perc responded automatically.",
        discord.Color.orange() if neutralized else discord.Color.red(),
        fields=[
            ("Actor", f"{actor} (`{actor.id}`)", False),
            ("Trigger", action_desc, False),
            ("Response", method, True),
            ("Status", "✅ Neutralized" if neutralized else "❌ Manual action needed!", True),
            ("Victims to Restore", str(len(victim_ids) if victim_ids else 0), True),
        ]
    )

    if victim_ids:
        for uid in victim_ids:
            await asyncio.sleep(0.5)
            try:
                u = await bot.fetch_user(uid)
                await guild.unban(u, reason="Anti-nuke restore")
            except discord.NotFound:
                pass
            except Exception:
                continue
            pending_raid_welcomes.add(uid)
            try:
                embed = brand_embed(
                    "⚠️ You Were Affected by a Raid",
                    f"You were removed from **{guild.name}** during a raid. The attacker has been neutralized and you've been automatically restored.\n\n[Click here to rejoin]({cfg['invite_link']})",
                    discord.Color.orange()
                )
                await u.send(embed=embed)
            except Exception:
                pass

@bot.event
async def on_member_join(member):
    guild = member.guild
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None or not is_protection_active(owner_id):
        return
    cfg = configs[str(owner_id)]

    if member.id in pending_raid_welcomes:
        pending_raid_welcomes.discard(member.id)
        await log_embed(owner_id, guild, "✅ Member Restored", f"{member.mention} rejoined after a raid. Welcome back!", discord.Color.green())

    now = time.time()
    joins = recent_joins_map.setdefault(guild.id, [])
    joins.append(now)
    recent_joins_map[guild.id] = [t for t in joins if now - t <= JOIN_RAID_WINDOW]

    last_trigger = last_mass_join_map.get(guild.id, 0)
    if (len(recent_joins_map[guild.id]) >= JOIN_RAID_THRESHOLD
            and now - last_trigger > MASS_JOIN_COOLDOWN
            and not lockdown_active_map.get(guild.id)):
        last_mass_join_map[guild.id] = now
        await log_embed(
            owner_id, guild, "🚨 Mass Join Raid Detected",
            f"**{len(recent_joins_map[guild.id])}** accounts joined within {JOIN_RAID_WINDOW}s — this matches a coordinated raid pattern. Auto-locking the server.",
            discord.Color.red()
        )
        await do_lockdown(guild)

@bot.event
async def on_member_ban(guild, user):
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None or not is_protection_active(owner_id):
        return
    cfg = configs[str(owner_id)]

    if user.id == owner_id:
        await asyncio.sleep(1)
        await try_unban_owner(guild, owner_id, cfg, "ban event detected")
        return

    await asyncio.sleep(1)
    actor = await get_audit_actor(guild, discord.AuditLogAction.ban, user.id)
    if not actor or is_exempt(guild.id, actor.id):
        return

    recent_bans.setdefault(actor.id, [])
    recent_bans[actor.id].append(time.time())
    recent_bans[actor.id] = _prune(recent_bans[actor.id], RAID_WINDOW_SECONDS)
    recent_ban_victims.setdefault(actor.id, []).append(user.id)

    if len(recent_bans[actor.id]) >= RAID_BAN_THRESHOLD and actor.id not in neutralized_actors:
        neutralized_actors.add(actor.id)
        await handle_raid(guild, owner_id, cfg, actor, "mass banning", recent_ban_victims.get(actor.id, []))

@bot.event
async def on_member_remove(member):
    guild = member.guild
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None or not is_protection_active(owner_id):
        return
    cfg = configs[str(owner_id)]

    if member.id == owner_id:
        await asyncio.sleep(2)
        await try_unban_owner(guild, owner_id, cfg, "left/removed from server")
        return

    await asyncio.sleep(1)
    actor = await get_audit_actor(guild, discord.AuditLogAction.kick, member.id)
    if not actor or is_exempt(guild.id, actor.id):
        return

    recent_kicks.setdefault(actor.id, [])
    recent_kicks[actor.id].append(time.time())
    recent_kicks[actor.id] = _prune(recent_kicks[actor.id], RAID_WINDOW_SECONDS)

    if len(recent_kicks[actor.id]) >= RAID_KICK_THRESHOLD and actor.id not in neutralized_actors:
        neutralized_actors.add(actor.id)
        await handle_raid(guild, owner_id, cfg, actor, "mass kicking", [member.id])

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None or not is_protection_active(owner_id):
        return
    cfg = configs[str(owner_id)]
    await asyncio.sleep(1)
    actor = await get_audit_actor(guild, discord.AuditLogAction.channel_delete, channel.id)
    if not actor or is_exempt(guild.id, actor.id):
        return
    recent_ch_del.setdefault(actor.id, [])
    recent_ch_del[actor.id].append(time.time())
    recent_ch_del[actor.id] = _prune(recent_ch_del[actor.id], RAID_WINDOW_SECONDS)
    if len(recent_ch_del[actor.id]) >= RAID_CHANNEL_DELETE_THRESHOLD and actor.id not in neutralized_actors:
        neutralized_actors.add(actor.id)
        await handle_raid(guild, owner_id, cfg, actor, "mass channel deletion")

@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None or not is_protection_active(owner_id):
        return
    cfg = configs[str(owner_id)]
    await asyncio.sleep(1)
    actor = await get_audit_actor(guild, discord.AuditLogAction.role_delete, role.id)
    if not actor or is_exempt(guild.id, actor.id):
        return
    recent_role_del.setdefault(actor.id, [])
    recent_role_del[actor.id].append(time.time())
    recent_role_del[actor.id] = _prune(recent_role_del[actor.id], RAID_WINDOW_SECONDS)
    if len(recent_role_del[actor.id]) >= RAID_ROLE_DELETE_THRESHOLD and actor.id not in neutralized_actors:
        neutralized_actors.add(actor.id)
        await handle_raid(guild, owner_id, cfg, actor, "mass role deletion")

# =================================================================
# BACKGROUND: LICENSE EXPIRY REMINDERS
# =================================================================

@tasks.loop(hours=24)
async def check_expirations():
    now = time.time()
    for user_id_str, entry in licenses.get("activations", {}).items():
        expires_at = entry.get("expires_at")
        if expires_at is None:
            continue
        remaining = expires_at - now

        if 0 < remaining <= 3 * 86400 and not entry.get("warned_3d"):
            try:
                user = await bot.fetch_user(int(user_id_str))
                embed = brand_embed(f"⏳ {BRAND_NAME} License Expiring Soon", f"Your license expires <t:{int(expires_at)}:R>. Renew soon.", discord.Color.orange())
                await user.send(embed=embed)
            except Exception:
                pass
            entry["warned_3d"] = True
            save_licenses()

        elif remaining <= 0 and not entry.get("warned_expired"):
            try:
                user = await bot.fetch_user(int(user_id_str))
                embed = brand_embed(f"🔒 {BRAND_NAME} License Expired", "Protection is paused. Purchase a new key and run `!activate`.", discord.Color.red())
                await user.send(embed=embed)
            except Exception:
                pass
            entry["warned_expired"] = True
            save_licenses()

@check_expirations.before_loop
async def before_check():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Serving {len(configs)} configured server(s). Data dir: {DATA_DIR}")
    if not check_expirations.is_running():
        check_expirations.start()

bot.run(BOT_TOKEN)
