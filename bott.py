import discord
from discord.ext import commands
import os
import random
import string
import asyncio
import json
import time

# ================= CONFIG =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CLIENT_ID = "YOUR_BOT_CLIENT_ID_HERE"  # from Developer Portal, used to build invite links
CONFIG_FILE = "configs.json"
BACKUP_DIR = "backups"
LOCKDOWN_DIR = "lockdowns"
CONFIRM_TIMEOUT = 30
SETUP_TIMEOUT = 300  # 5 min per setup step

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

# ---------- Config storage ----------

def load_configs():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_configs():
    with open(CONFIG_FILE, "w") as f:
        json.dump(configs, f, indent=2)

configs = load_configs()
guild_to_owner = {}  # guild_id (int) -> owner_id (int)

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

# ---------- Setup session state ----------

setup_sessions = {}  # user_id -> {"stage": str, "data": {...}, "expires": ts}

def start_session(user_id, stage):
    setup_sessions[user_id] = {"stage": stage, "data": {}, "expires": time.time() + SETUP_TIMEOUT}

def touch_session(user_id):
    if user_id in setup_sessions:
        setup_sessions[user_id]["expires"] = time.time() + SETUP_TIMEOUT

# ---------- Helpers ----------

def gen_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def is_exempt(guild_id, user_id):
    owner_id = get_owner_id_for_guild(guild_id)
    if owner_id is None:
        return True  # not configured, ignore entirely
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

async def log_and_dm(owner_id, guild, message):
    channel = guild.system_channel
    if channel:
        try:
            await channel.send(message)
        except Exception:
            pass
    try:
        user = await bot.fetch_user(owner_id)
        await user.send(message)
    except Exception:
        pass

def backup_path(guild_id):
    return os.path.join(BACKUP_DIR, f"{guild_id}.json")

def lockdown_path(guild_id):
    return os.path.join(LOCKDOWN_DIR, f"{guild_id}.json")


# =================================================================
# SETUP WIZARD
# =================================================================

@bot.command()
async def setup(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    existing = get_config(ctx.author.id)
    if existing and existing.get("setup_complete"):
        return await ctx.send(
            "✅ You already have a server configured. Use `!resetup` if you need to change it, "
            "or `!myconfig` to view your current settings."
        )

    start_session(ctx.author.id, "await_user_id")
    await ctx.send(
        "👋 **Welcome to setup!**\n\n"
        "**Step 1/4 — Confirm your User ID**\n"
        "First, make sure Developer Mode is on:\n"
        "`User Settings → Advanced → Developer Mode` (toggle ON)\n\n"
        "Then right-click your own name/avatar anywhere and click **Copy User ID**.\n\n"
        f"Paste that ID here to confirm it matches this account. (You have {SETUP_TIMEOUT//60} minutes)"
    )

@bot.command()
async def resetup(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    existing = get_config(ctx.author.id)
    if not existing:
        return await ctx.send("You don't have a config yet. Use `!setup` to create one.")
    start_session(ctx.author.id, "await_user_id")
    await ctx.send(
        "🔄 **Re-running setup.**\n\n"
        "**Step 1/4 — Confirm your User ID**\nPaste your Discord User ID to confirm (Developer Mode → right-click yourself → Copy User ID)."
    )

@bot.command()
async def myconfig(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    cfg = get_config(ctx.author.id)
    if not cfg or not cfg.get("setup_complete"):
        return await ctx.send("No config found. Use `!setup` to create one.")
    guild = bot.get_guild(cfg["guild_id"])
    await ctx.send(
        f"**Your configuration:**\n"
        f"Server: {guild.name if guild else 'Unknown'} (`{cfg['guild_id']}`)\n"
        f"Owner role: {cfg['owner_role_name']}\n"
        f"Invite link: {cfg['invite_link']}\n"
        f"Trusted users: {len(cfg.get('trusted', []))}"
    )


async def handle_setup_message(message):
    user_id = message.author.id
    session = setup_sessions.get(user_id)
    if not session:
        return False

    if time.time() > session["expires"]:
        del setup_sessions[user_id]
        await message.channel.send("⏱️ Setup timed out. Run `!setup` to start again.")
        return True

    touch_session(user_id)
    content = message.content.strip()
    stage = session["stage"]
    data = session["data"]

    # ---- Step 1: confirm user ID ----
    if stage == "await_user_id":
        if not content.isdigit():
            await message.channel.send("That doesn't look like a valid ID (should be all numbers). Try again.")
            return True
        if int(content) != user_id:
            await message.channel.send(
                "⚠️ That ID doesn't match the account you're DMing me from. "
                "Make sure you copied YOUR OWN ID, not someone else's. Try again."
            )
            return True
        data["user_id"] = user_id
        session["stage"] = "await_guild_id"
        invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot"
        await message.channel.send(
            "✅ Confirmed.\n\n"
            "**Step 2/4 — Server ID**\n"
            f"If you haven't already, invite me to your server first:\n{invite_bot_url}\n\n"
            "Then right-click your **server icon** (with Developer Mode on) and click **Copy Server ID**.\n\n"
            "Paste that ID here."
        )
        return True

    # ---- Step 2: guild ID ----
    if stage == "await_guild_id":
        if not content.isdigit():
            await message.channel.send("That doesn't look like a valid Server ID. Try again.")
            return True
        guild_id = int(content)
        guild = bot.get_guild(guild_id)
        if not guild:
            invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot"
            await message.channel.send(
                f"I'm not in that server yet. Invite me first:\n{invite_bot_url}\n"
                "Then paste the Server ID again."
            )
            return True

        member = guild.get_member(user_id)
        if not member:
            await message.channel.send("You don't appear to be a member of that server. Join it, then try again.")
            return True
        if not (member.guild_permissions.administrator or guild.owner_id == member.id):
            await message.channel.send("❌ You must be the server owner or an Administrator to set this up.")
            return True

        existing_owner = get_owner_id_for_guild(guild_id)
        if existing_owner and existing_owner != user_id:
            await message.channel.send(
                "⚠️ This server is already configured by a different user. "
                "If that's wrong, contact support. Setup cancelled."
            )
            del setup_sessions[user_id]
            return True

        data["guild_id"] = guild_id
        session["stage"] = "await_role_name"
        await message.channel.send(
            "✅ Server found.\n\n"
            "**Step 3/4 — Recovery role name**\n"
            "What should the recovery role be called? This is the role I'll give you back if it's ever stripped.\n"
            "Type a name, or type `default` to use **Owner**."
        )
        return True

    # ---- Step 3: role name ----
    if stage == "await_role_name":
        role_name = "Owner" if content.lower() == "default" else content
        guild = bot.get_guild(data["guild_id"])
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(
                    name=role_name,
                    permissions=discord.Permissions(administrator=True),
                    reason="Created during bot setup"
                )
                await message.channel.send(f"Role '{role_name}' didn't exist, so I created it with Administrator permissions.")
            except Exception as e:
                await message.channel.send(f"⚠️ Couldn't create the role: {e}\nMake sure my role is above where this role needs to sit. Try a different name or fix permissions, then resend.")
                return True

        # make sure it's assigned to the setup-er now, as a starting point
        member = guild.get_member(user_id)
        try:
            await member.add_roles(role, reason="Initial owner role assignment during setup")
        except Exception:
            pass

        data["owner_role_name"] = role_name
        session["stage"] = "await_invite"
        await message.channel.send(
            "✅ Role set.\n\n"
            "**Step 4/4 — Permanent invite link**\n"
            "Go to any channel → **Create Invite** → set **Expire After: Never** and **Max Uses: No limit** → copy the link.\n\n"
            "Paste it here."
        )
        return True

    # ---- Step 4: invite link ----
    if stage == "await_invite":
        if not content.startswith("https://discord.gg/") and not content.startswith("https://discord.com/invite/"):
            await message.channel.send("That doesn't look like a valid Discord invite link. Try again.")
            return True

        data["invite_link"] = content

        configs[str(user_id)] = {
            "guild_id": data["guild_id"],
            "owner_role_name": data["owner_role_name"],
            "invite_link": data["invite_link"],
            "trusted": [],
            "setup_complete": True,
        }
        save_configs()
        rebuild_guild_index()
        del setup_sessions[user_id]

        await message.channel.send(
            "🎉 **Setup complete!**\n\n"
            "Your protection is now active. Type `!help` to see everything I can do for you.\n"
            "Recommended: run `!backup` now to save your current server structure."
        )
        return True

    return False


# =================================================================
# AUTHORIZATION WRAPPER FOR REGULAR COMMANDS
# =================================================================

def require_setup():
    async def predicate(ctx):
        if not isinstance(ctx.channel, discord.DMChannel):
            return False
        cfg = get_config(ctx.author.id)
        if not cfg or not cfg.get("setup_complete"):
            await ctx.send("You haven't set up the bot yet. Type `!setup` to begin.")
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
    await ctx.send(f"Server invite: {cfg['invite_link']}")

@bot.command()
@require_setup()
async def owner(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    member = guild.get_member(ctx.author.id) if guild else None
    if not member:
        return await ctx.send("Join the server first with !invite.")
    role = discord.utils.get(guild.roles, name=cfg["owner_role_name"])
    if not role:
        return await ctx.send(f"Role '{cfg['owner_role_name']}' not found — it may have been deleted. Use !resetup to recreate it.")
    try:
        await member.add_roles(role, reason="Owner recovery")
        await ctx.send("✅ Owner role restored.")
    except discord.Forbidden:
        await ctx.send("❌ I lack permission — check my role position in that server.")

@bot.command()
@require_setup()
async def unban(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    try:
        user = await bot.fetch_user(ctx.author.id)
        await guild.unban(user, reason="Manual owner unban")
        await ctx.send(f"✅ Unbanned. Rejoin: {cfg['invite_link']}")
    except discord.NotFound:
        await ctx.send("You're not currently banned.")
    except discord.Forbidden:
        await ctx.send("❌ I lack Ban Members permission there.")

@bot.command()
@require_setup()
async def trust(ctx, member_id: int):
    cfg = get_config(ctx.author.id)
    if member_id not in cfg["trusted"]:
        cfg["trusted"].append(member_id)
        save_configs()
    await ctx.send(f"✅ `{member_id}` added to trusted list (exempt from anti-nuke).")

@bot.command()
@require_setup()
async def untrust(ctx, member_id: int):
    cfg = get_config(ctx.author.id)
    if member_id in cfg["trusted"]:
        cfg["trusted"].remove(member_id)
        save_configs()
    await ctx.send(f"✅ `{member_id}` removed from trusted list.")


# =================================================================
# BACKUP / RESTORE (per-guild files)
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
        return await dm.send("❌ No backup found for your server.")
    with open(path) as f:
        data = json.load(f)

    await dm.send("🔧 Restoring roles...")
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
            await dm.send(f"⚠️ Role '{r['name']}' failed: {e}")

    await dm.send("🔧 Restoring categories...")
    category_map = {}
    for c in sorted(data["categories"], key=lambda x: x["position"]):
        try:
            ow = build_overwrites(c["overwrites"], role_map)
            category_map[c["name"]] = await guild.create_category(c["name"], overwrites=ow, reason="Restore")
            await asyncio.sleep(0.5)
        except Exception as e:
            await dm.send(f"⚠️ Category '{c['name']}' failed: {e}")

    await dm.send("🔧 Restoring channels...")
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
            await dm.send(f"⚠️ Channel '{ch['name']}' failed: {e}")

    await dm.send("✅ Restore complete. Ordering/pins/per-member overwrites may need manual fixing.")

@bot.command()
@require_setup()
async def backup(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    await do_backup(guild)
    await ctx.send("✅ Backup saved.")

@bot.command()
@require_setup()
async def restore(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    await do_restore(guild, ctx)


# =================================================================
# LOCKDOWN (per-guild, exact-state save/restore)
# =================================================================

lockdown_active_map = {}  # guild_id -> bool

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
        return await ctx.send("⚠️ Already in lockdown.")
    count = await do_lockdown(guild)
    await ctx.send(f"🔒 Lockdown engaged. {count} channels restricted (exact prior state saved).")

@bot.command()
@require_setup()
async def unlock(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    if not lockdown_active_map.get(guild.id) and not os.path.exists(lockdown_path(guild.id)):
        return await ctx.send("Not currently in lockdown.")
    count = await do_unlock(guild)
    await ctx.send(f"🔓 Lockdown lifted. {count} channels restored to their exact original state.")


# =================================================================
# KILL SWITCH
# =================================================================

pending_kills = {}  # user_id -> {"stage": int, "code": str}

@bot.command()
@require_setup()
async def kill(ctx):
    code = gen_code()
    pending_kills[ctx.author.id] = {"stage": 1, "code": code}
    await ctx.send(
        f"⚠️ **EMERGENCY WIPE** ⚠️\nDeletes every channel and role in your server. Irreversible without !restore.\n"
        f"Reply within {CONFIRM_TIMEOUT}s with exactly:\n`{code}`\n\nType !cancel to abort anytime."
    )
    async def expire(uid, stage, c):
        await asyncio.sleep(CONFIRM_TIMEOUT)
        e = pending_kills.get(uid)
        if e and e["stage"] == stage and e["code"] == c:
            del pending_kills[uid]
            await ctx.send("⏱️ Timed out. Cancelled.")
    bot.loop.create_task(expire(ctx.author.id, 1, code))

@bot.command()
@require_setup()
async def cancel(ctx):
    if ctx.author.id in pending_kills:
        del pending_kills[ctx.author.id]
        await ctx.send("✅ Kill sequence cancelled.")
    else:
        await ctx.send("Nothing pending.")

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
    await dm.send(f"✅ Wipe complete. Channels: {deleted_c}/{deleted_c+failed_c}. Roles: {deleted_r}/{deleted_r+failed_r}.\nRun !restore to rebuild.")


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
    await ctx.send(
        f"**🩺 Status for {guild.name}**\n"
        f"Last backup: {backup_age}\n"
        f"Lockdown active: {'Yes 🔒' if lockdown_active_map.get(guild.id) else 'No'}\n"
        f"Top role: {guild.me.top_role.name}\n"
        f"Ban Members: {'✅' if perms.ban_members else '❌'}\n"
        f"Manage Roles: {'✅' if perms.manage_roles else '❌'}\n"
        f"Manage Channels: {'✅' if perms.manage_channels else '❌'}\n"
        f"View Audit Log: {'✅' if perms.view_audit_log else '❌'}\n"
        f"Administrator: {'✅' if perms.administrator else '❌'}"
    )

@bot.command(name="help")
async def custom_help(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    await ctx.send(
        "**🛠 Server Guardian Bot — Commands**\n\n"
        "**Getting started**\n"
        "`!setup` — first-time setup wizard\n"
        "`!resetup` — reconfigure your server\n"
        "`!myconfig` — view your current settings\n\n"
        "**Recovery**\n"
        "`!invite` / `!owner` / `!unban`\n\n"
        "**Backup**\n"
        "`!backup` / `!restore`\n\n"
        "**Emergency**\n"
        "`!kill` (2-step confirm) / `!cancel`\n\n"
        "**Server control**\n"
        "`!lockdown` / `!unlock`\n\n"
        "**Trust management**\n"
        "`!trust <user_id>` / `!untrust <user_id>`\n\n"
        "**Status**\n"
        "`!status`\n\n"
        "**Automatic protection**: anti-nuke (mass ban/kick/delete detection), auto-unban if banned, mass-join raid lockdown."
    )


# =================================================================
# MESSAGE ROUTER (setup wizard + kill confirmation)
# =================================================================

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.author.bot or not isinstance(message.channel, discord.DMChannel):
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
        await message.channel.send(f"🔴 FINAL CONFIRMATION. Reply within {CONFIRM_TIMEOUT}s:\n`{code2}`\n(!cancel to abort)")
        async def expire2():
            await asyncio.sleep(CONFIRM_TIMEOUT)
            e = pending_kills.get(message.author.id)
            if e and e["stage"] == 2 and e["code"] == code2:
                del pending_kills[message.author.id]
                await message.channel.send("⏱️ Timed out. Cancelled.")
        bot.loop.create_task(expire2())

    elif entry["stage"] == 2 and content == entry["code"]:
        del pending_kills[message.author.id]
        cfg = get_config(message.author.id)
        guild = bot.get_guild(cfg["guild_id"])
        await message.channel.send("💾 Backing up before wipe...")
        await do_backup(guild)
        await message.channel.send("💥 Executing wipe...")
        await execute_kill(guild, message.channel)


# =================================================================
# ANTI-NUKE EVENTS (guild-aware via reverse index)
# =================================================================

recent_bans, recent_kicks, recent_ch_del, recent_role_del = {}, {}, {}, {}
recent_ban_victims = {}
neutralized_actors = set()
pending_raid_welcomes = set()
recent_joins_map = {}   # guild_id -> [timestamps]
last_mass_join_map = {} # guild_id -> ts

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
            await user.send(f"⚠️ You left/were removed ({reason}), but you're not banned. Rejoin: {cfg['invite_link']}")
        except discord.Forbidden:
            pass
        return
    except Exception:
        return
    try:
        await guild.unban(user, reason=f"Auto-unban ({reason})")
        await user.send(f"🚨 You were banned ({reason}) — auto-unbanned. Rejoin: {cfg['invite_link']}")
    except Exception:
        pass

async def handle_raid(guild, owner_id, cfg, actor, action_desc, victim_ids=None):
    await log_and_dm(owner_id, guild, f"🚨 RAID DETECTED: {actor} ({actor.id}) — {action_desc}. Responding...")
    neutralized = False
    member = guild.get_member(actor.id)
    if member:
        try:
            await guild.ban(member, reason=f"Anti-nuke: {action_desc}")
            neutralized = True
        except Exception:
            try:
                roles = [r for r in member.roles if not r.is_default()]
                if roles:
                    await member.remove_roles(*roles, reason="Anti-nuke role strip")
                neutralized = True
            except Exception:
                pass
    try:
        user = await bot.fetch_user(owner_id)
        await user.send(f"Neutralized: {'Yes' if neutralized else 'NO — manual action needed!'}")
    except Exception:
        pass
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
                await u.send(f"⚠️ You were removed during a raid. The attacker was neutralized and you were auto-restored.\nRejoin: {cfg['invite_link']}")
            except Exception:
                pass

@bot.event
async def on_member_join(member):
    guild = member.guild
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None:
        return
    cfg = configs[str(owner_id)]

    if member.id in pending_raid_welcomes:
        pending_raid_welcomes.discard(member.id)
        await log_and_dm(owner_id, guild, f"✅ {member.mention} rejoined after a raid. Welcome back!")

    now = time.time()
    joins = recent_joins_map.setdefault(guild.id, [])
    joins.append(now)
    recent_joins_map[guild.id] = [t for t in joins if now - t <= JOIN_RAID_WINDOW]

    last_trigger = last_mass_join_map.get(guild.id, 0)
    if (len(recent_joins_map[guild.id]) >= JOIN_RAID_THRESHOLD
            and now - last_trigger > MASS_JOIN_COOLDOWN
            and not lockdown_active_map.get(guild.id)):
        last_mass_join_map[guild.id] = now
        await log_and_dm(owner_id, guild, f"🚨 Mass join raid detected ({len(recent_joins_map[guild.id])} joins). Auto-locking.")
        await do_lockdown(guild)

@bot.event
async def on_member_ban(guild, user):
    owner_id = get_owner_id_for_guild(guild.id)
    if owner_id is None:
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
    if owner_id is None:
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
    if owner_id is None:
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
    if owner_id is None:
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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Serving {len(configs)} configured server(s).")

bot.run(BOT_TOKEN)