import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import random
import string
import asyncio
import json
import time
import re
import asyncpg
import httpx
from datetime import datetime, timezone

# ================= CONFIG =================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BOT_CLIENT_ID = "1546323834561634324"
MASTER_USER_ID = 1414107360099831808

BRAND_NAME = "Perc"
EMBED_COLOR = discord.Color.gold()

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

PRICE_MAP = {
    "30d": 7.99,
    "lifetime": 19.99,
}

LTC_ORDER_TIMEOUT = 1800  # 30 minutes
LTC_POLL_INTERVAL = 30    # seconds
LTC_MIN_CONFIRMATIONS = 1

MAX_PLANS_PER_OWNER = 20
# ============================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.bans = True

# =================================================================
# IN-MEMORY CACHES (backed by Postgres — loaded on startup)
# =================================================================

configs = {}
licenses = {"keys": {}, "activations": {}}
blacklist = set()
switch_requests = {"requests": {}}
guild_to_owner = {}
lockdown_active_map = {}
shop_settings = {"ltc_address": None}
shop_plans = {}  # str(owner_id) -> {plan_key: {"label":..., "days":..., "price":..., "position":...}}
pool = None


class PercBot(commands.Bot):
    async def setup_hook(self):
        global pool
        pool = await asyncpg.create_pool(DATABASE_URL)
        await init_db()
        await load_all_from_db()
        self.add_view(ShopPanelView())
        await self.tree.sync()
        if not check_expirations.is_running():
            check_expirations.start()
        if not poll_ltc_orders.is_running():
            poll_ltc_orders.start()


bot = PercBot(command_prefix="!", intents=intents, help_command=None)

# =================================================================
# DATABASE LAYER
# =================================================================

async def init_db():
    async with pool.acquire() as conn:
        # --- configs ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS configs (
                owner_id BIGINT PRIMARY KEY,
                guild_id BIGINT,
                owner_role_name TEXT,
                invite_link TEXT,
                trusted BIGINT[],
                log_channel_id BIGINT,
                log_category_id BIGINT,
                shop_channel_id BIGINT,
                shop_message_id BIGINT,
                setup_complete BOOLEAN
            );
        """)
        # Safe on existing tables — patches in any columns added after the table first existed.
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS guild_id BIGINT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS owner_role_name TEXT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS invite_link TEXT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS trusted BIGINT[];")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS log_channel_id BIGINT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS log_category_id BIGINT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS shop_channel_id BIGINT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS shop_message_id BIGINT;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS setup_complete BOOLEAN;")
        await conn.execute("ALTER TABLE configs ADD COLUMN IF NOT EXISTS ltc_address TEXT;")

        # --- license_keys ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS license_keys (
                key TEXT PRIMARY KEY,
                duration_label TEXT,
                duration_days INTEGER,
                bound_user_id BIGINT,
                used BOOLEAN,
                used_by BIGINT,
                created_at DOUBLE PRECISION,
                used_at DOUBLE PRECISION
            );
        """)

        # --- activations ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS activations (
                user_id BIGINT PRIMARY KEY,
                key TEXT,
                expires_at DOUBLE PRECISION,
                duration_label TEXT,
                warned_3d BOOLEAN DEFAULT FALSE,
                warned_expired BOOLEAN DEFAULT FALSE
            );
        """)
        await conn.execute("ALTER TABLE activations ADD COLUMN IF NOT EXISTS warned_3d BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE activations ADD COLUMN IF NOT EXISTS warned_expired BOOLEAN DEFAULT FALSE;")

        # --- blacklist ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id BIGINT PRIMARY KEY
            );
        """)

        # --- switch_requests ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS switch_requests (
                id SERIAL PRIMARY KEY,
                key TEXT,
                old_user_id BIGINT,
                new_user_id BIGINT,
                reason TEXT,
                status TEXT,
                created_at DOUBLE PRECISION,
                resolved_at DOUBLE PRECISION,
                deny_reason TEXT
            );
        """)

        # --- backups ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                guild_id BIGINT PRIMARY KEY,
                data TEXT,
                backed_up_at DOUBLE PRECISION
            );
        """)

        # --- lockdowns ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS lockdowns (
                guild_id BIGINT PRIMARY KEY,
                state TEXT
            );
        """)

        # --- sales ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                duration_label TEXT,
                price NUMERIC(10,2),
                method TEXT DEFAULT 'manual',
                created_at DOUBLE PRECISION
            );
        """)
        await conn.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS method TEXT DEFAULT 'manual';")
        await conn.execute("ALTER TABLE sales ADD COLUMN IF NOT EXISTS reseller_id BIGINT;")

        # --- raid_events ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS raid_events (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT,
                owner_id BIGINT,
                actor_id BIGINT,
                action_desc TEXT,
                neutralized BOOLEAN,
                created_at DOUBLE PRECISION
            );
        """)

        # --- shop_settings (global fallback LTC address) ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                ltc_address TEXT
            );
        """)

        # --- shop_plans (per-owner customizable plans) ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shop_plans (
                owner_id BIGINT NOT NULL,
                plan_key TEXT NOT NULL,
                label TEXT,
                days INTEGER,
                price NUMERIC(10,2),
                position INTEGER DEFAULT 0,
                PRIMARY KEY (owner_id, plan_key)
            );
        """)

        # --- ltc_orders ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ltc_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                duration_label TEXT,
                usd_price NUMERIC(10,2),
                ltc_amount NUMERIC(16,8),
                address TEXT,
                status TEXT DEFAULT 'pending',
                created_at DOUBLE PRECISION,
                expires_at DOUBLE PRECISION,
                completed_at DOUBLE PRECISION,
                tx_hash TEXT
            );
        """)
        await conn.execute("ALTER TABLE ltc_orders ADD COLUMN IF NOT EXISTS owner_id BIGINT;")
        await conn.execute("ALTER TABLE ltc_orders ADD COLUMN IF NOT EXISTS duration_days INTEGER;")
        await conn.execute("ALTER TABLE ltc_orders ADD COLUMN IF NOT EXISTS plan_label TEXT;")

        # --- gift_orders ---
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                duration_label TEXT,
                usd_price NUMERIC(10,2),
                code TEXT,
                status TEXT DEFAULT 'pending',
                created_at DOUBLE PRECISION,
                resolved_at DOUBLE PRECISION,
                deny_reason TEXT
            );
        """)
        await conn.execute("ALTER TABLE gift_orders ADD COLUMN IF NOT EXISTS owner_id BIGINT;")
        await conn.execute("ALTER TABLE gift_orders ADD COLUMN IF NOT EXISTS duration_days INTEGER;")
        await conn.execute("ALTER TABLE gift_orders ADD COLUMN IF NOT EXISTS plan_label TEXT;")


async def load_all_from_db():
    async with pool.acquire() as conn:
        for r in await conn.fetch("SELECT * FROM configs"):
            row = dict(r)
            configs[str(row["owner_id"])] = {
                "guild_id": row.get("guild_id"),
                "owner_role_name": row.get("owner_role_name"),
                "invite_link": row.get("invite_link"),
                "trusted": list(row["trusted"]) if row.get("trusted") else [],
                "log_channel_id": row.get("log_channel_id"),
                "log_category_id": row.get("log_category_id"),
                "shop_channel_id": row.get("shop_channel_id"),
                "shop_message_id": row.get("shop_message_id"),
                "setup_complete": row.get("setup_complete"),
                "ltc_address": row.get("ltc_address"),
            }

        for r in await conn.fetch("SELECT * FROM license_keys"):
            row = dict(r)
            licenses["keys"][row["key"]] = {
                "duration_label": row.get("duration_label"),
                "duration_days": row.get("duration_days"),
                "bound_user_id": row.get("bound_user_id"),
                "used": row.get("used"),
                "used_by": row.get("used_by"),
                "created_at": row.get("created_at"),
                "used_at": row.get("used_at"),
            }

        for r in await conn.fetch("SELECT * FROM activations"):
            row = dict(r)
            licenses["activations"][str(row["user_id"])] = {
                "key": row.get("key"),
                "expires_at": row.get("expires_at"),
                "duration_label": row.get("duration_label"),
                "warned_3d": row.get("warned_3d"),
                "warned_expired": row.get("warned_expired"),
            }

        for r in await conn.fetch("SELECT user_id FROM blacklist"):
            blacklist.add(r["user_id"])

        for r in await conn.fetch("SELECT * FROM switch_requests"):
            row = dict(r)
            switch_requests["requests"][str(row["id"])] = {
                "key": row.get("key"), "old_user_id": row.get("old_user_id"), "new_user_id": row.get("new_user_id"),
                "reason": row.get("reason"), "status": row.get("status"), "created_at": row.get("created_at"),
                "resolved_at": row.get("resolved_at"), "deny_reason": row.get("deny_reason"),
            }

        for r in await conn.fetch("SELECT guild_id FROM lockdowns"):
            lockdown_active_map[r["guild_id"]] = True

        for r in await conn.fetch("SELECT * FROM shop_plans ORDER BY owner_id, position"):
            row = dict(r)
            shop_plans.setdefault(str(row["owner_id"]), {})[row["plan_key"]] = {
                "label": row.get("label"),
                "days": row.get("days"),
                "price": float(row["price"]) if row.get("price") is not None else None,
                "position": row.get("position") or 0,
            }

        row = await conn.fetchrow("SELECT ltc_address FROM shop_settings WHERE id=1")
        if row:
            shop_settings["ltc_address"] = row["ltc_address"]

    rebuild_guild_index()
    plan_count = sum(len(v) for v in shop_plans.values())
    print(f"[DB] Loaded {len(configs)} configs, {len(licenses['keys'])} keys, {len(licenses['activations'])} activations, {plan_count} shop plans.")


async def db_upsert_config(owner_id, cfg):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO configs (owner_id, guild_id, owner_role_name, invite_link, trusted, log_channel_id, log_category_id, shop_channel_id, shop_message_id, setup_complete, ltc_address)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (owner_id) DO UPDATE SET
                guild_id=$2, owner_role_name=$3, invite_link=$4, trusted=$5,
                log_channel_id=$6, log_category_id=$7, shop_channel_id=$8, shop_message_id=$9, setup_complete=$10, ltc_address=$11
        """, owner_id, cfg["guild_id"], cfg["owner_role_name"], cfg["invite_link"],
             cfg.get("trusted", []), cfg.get("log_channel_id"), cfg.get("log_category_id"),
             cfg.get("shop_channel_id"), cfg.get("shop_message_id"), cfg.get("setup_complete", False),
             cfg.get("ltc_address"))


async def db_delete_config(owner_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM configs WHERE owner_id=$1", owner_id)


async def db_upsert_license_key(key, entry):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO license_keys (key, duration_label, duration_days, bound_user_id, used, used_by, created_at, used_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (key) DO UPDATE SET
                duration_label=$2, duration_days=$3, bound_user_id=$4, used=$5, used_by=$6, created_at=$7, used_at=$8
        """, key, entry["duration_label"], entry["duration_days"], entry["bound_user_id"],
             entry["used"], entry.get("used_by"), entry["created_at"], entry.get("used_at"))


async def db_upsert_activation(user_id, entry):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO activations (user_id, key, expires_at, duration_label, warned_3d, warned_expired)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (user_id) DO UPDATE SET
                key=$2, expires_at=$3, duration_label=$4, warned_3d=$5, warned_expired=$6
        """, user_id, entry["key"], entry.get("expires_at"), entry.get("duration_label"),
             entry.get("warned_3d", False), entry.get("warned_expired", False))


async def db_delete_activation(user_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM activations WHERE user_id=$1", user_id)


async def db_add_blacklist(user_id):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO blacklist (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)


async def db_remove_blacklist(user_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM blacklist WHERE user_id=$1", user_id)


async def db_insert_switch_request(key, old_user_id, new_user_id, reason):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO switch_requests (key, old_user_id, new_user_id, reason, status, created_at)
            VALUES ($1,$2,$3,$4,'pending',$5) RETURNING id
        """, key, old_user_id, new_user_id, reason, time.time())
        return row["id"]


async def db_approve_switch_request(req_id, resolved_at):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE switch_requests SET status='approved', resolved_at=$2 WHERE id=$1",
                            int(req_id), resolved_at)


async def db_deny_switch_request(req_id, reason, resolved_at):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE switch_requests SET status='denied', deny_reason=$2, resolved_at=$3 WHERE id=$1",
                            int(req_id), reason, resolved_at)


async def db_upsert_plan(owner_id, plan_key, label, days, price, position):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shop_plans (owner_id, plan_key, label, days, price, position)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (owner_id, plan_key) DO UPDATE SET
                label=$3, days=$4, price=$5, position=$6
        """, owner_id, plan_key, label, days, price, position)


async def db_delete_plan(owner_id, plan_key):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shop_plans WHERE owner_id=$1 AND plan_key=$2", owner_id, plan_key)

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
# SMALL HELPERS
# =================================================================

def rebuild_guild_index():
    guild_to_owner.clear()
    for owner_id, cfg in configs.items():
        if cfg.get("setup_complete") and cfg.get("guild_id"):
            guild_to_owner[cfg["guild_id"]] = int(owner_id)


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


def humanize_duration(label, days):
    """Turns a parsed duration key like '60d' into a friendly default label like '60 Days'."""
    if days is None:
        return "Lifetime"
    m = re.match(r"^(\d+)([dwmy])$", label)
    if not m:
        return label
    num, unit = m.group(1), m.group(2)
    unit_names = {"d": "Day", "w": "Week", "m": "Month", "y": "Year"}
    name = unit_names.get(unit, unit)
    if num != "1":
        name += "s"
    return f"{num} {name}"


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


# ---- per-owner shop plan helpers ----

DEFAULT_PLANS = [
    ("30d", "30 Days", 30, 7.99),
    ("lifetime", "Lifetime", None, 19.99),
]


def get_owner_plans(owner_id):
    plans = shop_plans.get(str(owner_id), {})
    return sorted(
        [{"key": k, **v} for k, v in plans.items()],
        key=lambda p: p["position"]
    )


async def seed_default_plans(owner_id):
    if shop_plans.get(str(owner_id)):
        return
    for i, (key, label, days, price) in enumerate(DEFAULT_PLANS):
        shop_plans.setdefault(str(owner_id), {})[key] = {"label": label, "days": days, "price": price, "position": i}
        await db_upsert_plan(owner_id, key, label, days, price, i)


async def deliver_key(user_id, duration_label, duration_days, price, method, reseller_id=None):
    """Generates + binds a key, DMs it, and logs the sale. Used by both LTC and gift-card success paths."""
    key = gen_license_key()
    entry = {
        "duration_label": duration_label, "duration_days": duration_days,
        "bound_user_id": user_id,
        "used": False, "used_by": None,
        "created_at": time.time(), "used_at": None,
    }
    licenses["keys"][key] = entry
    await db_upsert_license_key(key, entry)

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sales (user_id, duration_label, price, method, created_at, reseller_id) VALUES ($1,$2,$3,$4,$5,$6)",
            user_id, duration_label, price, method, time.time(), reseller_id
        )

    duration_display = "Lifetime ♾️" if duration_days is None else f"{duration_label} ({duration_days} days)"
    try:
        user = await bot.fetch_user(user_id)
        dm_embed = brand_embed(f"🎉 Payment Confirmed — Thanks for picking up {BRAND_NAME}", color=discord.Color.green())
        dm_embed.add_field(name="Your key", value=f"`{key}`", inline=False)
        dm_embed.add_field(name="Plan", value=duration_display, inline=True)
        dm_embed.add_field(name="Next step", value="Reply here with `!activate <key>` to get set up.", inline=False)
        await user.send(embed=dm_embed)
    except Exception:
        pass

    return key


async def notify_reseller_sale(owner_id, buyer_id, plan_label, price, method):
    """Lets a reseller (server owner running their own /shop) know when their panel makes a sale."""
    if not owner_id:
        return
    cfg = get_config(owner_id)
    if not cfg:
        return
    desc = f"Your `/shop` panel just sold a **{plan_label}** plan via **{method.upper()}** for **${price:.2f}**.\nBuyer: `{buyer_id}`"
    guild = bot.get_guild(cfg.get("guild_id")) if cfg.get("guild_id") else None
    if guild:
        await log_embed(owner_id, guild, "💰 Sale Made From Your Shop", desc, discord.Color.green())
    else:
        try:
            owner_user = await bot.fetch_user(owner_id)
            await owner_user.send(embed=brand_embed("💰 Sale Made From Your Shop", desc, discord.Color.green()))
        except Exception:
            pass


@bot.check
async def globally_block_blacklisted(ctx):
    return not is_blacklisted(ctx.author.id)

# =================================================================
# PENDING STATE (ephemeral, in-memory only — fine to lose on restart)
# =================================================================

setup_sessions = {}
pending_switch_context = {}
pending_kills = {}


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
# SHOP PANEL — BUTTONS, MODALS, LTC + GIFT CARD FLOW
# =================================================================

async def get_ltc_usd_price():
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "litecoin", "vs_currencies": "usd"}
        )
        resp.raise_for_status()
        return resp.json()["litecoin"]["usd"]


async def create_ltc_order(owner_id, buyer_id, plan):
    cfg = get_config(owner_id) or {}
    address = cfg.get("ltc_address") or shop_settings.get("ltc_address")
    if not address:
        return None, "LTC payments aren't set up for this shop yet. Contact the seller."

    try:
        ltc_price = await get_ltc_usd_price()
    except Exception:
        return None, "Couldn't fetch the current LTC price. Try again in a minute."

    base_amount = plan["price"] / ltc_price
    # unique 6th-decimal offset so concurrent orders never collide on the same address
    offset = random.randint(1, 999) / 100_000_000
    ltc_amount = round(base_amount + offset, 8)

    now = time.time()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO ltc_orders (user_id, owner_id, duration_label, duration_days, plan_label, usd_price, ltc_amount, address, status, created_at, expires_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending',$9,$10) RETURNING id
        """, buyer_id, owner_id, plan["key"], plan["days"], plan["label"], plan["price"], ltc_amount, address, now, now + LTC_ORDER_TIMEOUT)
        order_id = row["id"]

    return order_id, ltc_amount


async def create_gift_order(owner_id, buyer_id, plan, code):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO gift_orders (user_id, owner_id, duration_label, duration_days, plan_label, usd_price, code, status, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',$8) RETURNING id
        """, buyer_id, owner_id, plan["key"], plan["days"], plan["label"], plan["price"], code, time.time())
        return row["id"]


class PlanSelect(discord.ui.Select):
    def __init__(self, owner_id, method, plans):
        self.owner_id = owner_id
        self.method = method
        self.plans_by_key = {p["key"]: p for p in plans}
        options = [
            discord.SelectOption(
                label=f"{p['label']} — ${p['price']:.2f}",
                value=p["key"],
                description=("Lifetime access" if p["days"] is None else f"{p['days']} days of access")
            ) for p in plans
        ]
        super().__init__(placeholder="Choose a plan...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        plan = self.plans_by_key[self.values[0]]

        if self.method == "ltc":
            await interaction.response.defer(ephemeral=True, thinking=True)
            order_id, result = await create_ltc_order(self.owner_id, interaction.user.id, plan)
            if order_id is None:
                return await interaction.followup.send(embed=brand_embed("❌ Unavailable", result, discord.Color.red()), ephemeral=True)

            ltc_amount = result
            cfg = get_config(self.owner_id) or {}
            address = cfg.get("ltc_address") or shop_settings.get("ltc_address")
            embed = brand_embed(
                "🪙 Send Exactly This Amount of LTC",
                f"Order `#{order_id}` — expires in {LTC_ORDER_TIMEOUT // 60} minutes.\n\n"
                "**Send exactly the amount shown below — not more, not less.** "
                "This exact amount is how we match your payment to your order.",
                discord.Color.gold()
            )
            embed.add_field(name="Address", value=f"`{address}`", inline=False)
            embed.add_field(name="Amount", value=f"`{ltc_amount} LTC`", inline=False)
            embed.add_field(name="Plan", value=plan["label"], inline=True)
            embed.add_field(name="Status", value="⏳ Waiting for payment...", inline=True)
            embed.set_footer(text=f"{BRAND_NAME} auto-checks every 30s. You'll get a DM the moment it's confirmed.")
            await interaction.followup.send(embed=embed, ephemeral=True)

        elif self.method == "gift":
            await interaction.response.send_modal(GiftCodeModal(self.owner_id, plan))


class DurationSelectView(discord.ui.View):
    """Shown after clicking Buy with LTC / Buy with Gift Card — lists the seller's configured plans."""
    def __init__(self, owner_id, method, plans):
        super().__init__(timeout=120)
        self.add_item(PlanSelect(owner_id, method, plans))


class GiftCodeModal(discord.ui.Modal, title="Rewarable Gift Card"):
    def __init__(self, owner_id, plan):
        super().__init__()
        self.owner_id = owner_id
        self.plan = plan

    code = discord.ui.TextInput(label="Gift Card Code", placeholder="Enter your Rewarable code", required=True, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        order_id = await create_gift_order(self.owner_id, interaction.user.id, self.plan, str(self.code))

        embed = brand_embed(
            "🎁 Gift Card Submitted",
            f"Order `#{order_id}` is pending manual verification. You'll get a DM once it's approved and your key is sent — this usually doesn't take long.",
            discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

        try:
            master = await bot.fetch_user(MASTER_USER_ID)
            alert = brand_embed("🔔 New Gift Card Order", color=discord.Color.orange())
            alert.add_field(name="Order ID", value=str(order_id), inline=True)
            alert.add_field(name="Buyer", value=f"{interaction.user} (`{interaction.user.id}`)", inline=True)
            alert.add_field(name="Seller (Reseller)", value=f"`{self.owner_id}`", inline=True)
            alert.add_field(name="Plan", value=f"{self.plan['label']} — ${self.plan['price']:.2f}", inline=True)
            alert.add_field(name="Code Submitted", value=f"`{self.code}`", inline=False)
            alert.add_field(name="To approve", value=f"`!approvegift {order_id}`", inline=True)
            alert.add_field(name="To deny", value=f"`!denygift {order_id} <reason>`", inline=True)
            await master.send(embed=alert)
        except Exception:
            pass


class ShopPanelView(discord.ui.View):
    """Persistent view — the main panel buttons. Registered once in setup_hook so it survives restarts."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy with LTC", emoji="🪙", style=discord.ButtonStyle.primary, custom_id="perc_buy_ltc")
    async def buy_ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_id = get_owner_id_for_guild(interaction.guild_id)
        if owner_id is None:
            return await interaction.response.send_message(embed=brand_embed("❌ Unavailable", "This shop isn't linked to an active seller.", discord.Color.red()), ephemeral=True)
        plans = get_owner_plans(owner_id)
        if not plans:
            return await interaction.response.send_message(embed=brand_embed("❌ No Plans Available", "The seller hasn't configured any plans yet.", discord.Color.red()), ephemeral=True)
        embed = brand_embed("🪙 Buy with Litecoin", "Choose a plan:", EMBED_COLOR)
        await interaction.response.send_message(embed=embed, view=DurationSelectView(owner_id, "ltc", plans), ephemeral=True)

    @discord.ui.button(label="Buy with Gift Card", emoji="🎁", style=discord.ButtonStyle.secondary, custom_id="perc_buy_gift")
    async def buy_gift(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_id = get_owner_id_for_guild(interaction.guild_id)
        if owner_id is None:
            return await interaction.response.send_message(embed=brand_embed("❌ Unavailable", "This shop isn't linked to an active seller.", discord.Color.red()), ephemeral=True)
        plans = get_owner_plans(owner_id)
        if not plans:
            return await interaction.response.send_message(embed=brand_embed("❌ No Plans Available", "The seller hasn't configured any plans yet.", discord.Color.red()), ephemeral=True)
        embed = brand_embed("🎁 Buy with Rewarable Gift Card", "Choose a plan:", EMBED_COLOR)
        await interaction.response.send_message(embed=embed, view=DurationSelectView(owner_id, "gift", plans), ephemeral=True)


async def create_shop_panel(guild, cfg, owner_id):
    """Replaces any existing shop panel channel with a fresh one, same pattern as logs."""
    old_channel_id = cfg.get("shop_channel_id") if cfg else None
    channels_to_remove = []

    if old_channel_id:
        ch = guild.get_channel(old_channel_id)
        if ch:
            channels_to_remove.append(ch)
    for ch in guild.text_channels:
        if ch.name == "perc-shop" and ch not in channels_to_remove:
            channels_to_remove.append(ch)

    for ch in channels_to_remove:
        try:
            await ch.delete(reason=f"{BRAND_NAME}: replacing shop panel")
        except Exception:
            pass

    try:
        channel = await guild.create_text_channel(
            "perc-shop", reason=f"{BRAND_NAME}: shop panel",
            topic=f"🛒 Buy {BRAND_NAME} protection — LTC or Rewarable gift card."
        )
        plans = get_owner_plans(owner_id)
        if plans:
            plan_lines = "\n".join(f"💵 **{p['label']}** — ${p['price']:.2f}" for p in plans)
        else:
            plan_lines = "⚠️ No plans configured yet — run `!addplan` in DMs with me to add some."
        embed = brand_embed(
            f"🛒 {BRAND_NAME} Shop",
            f"Pick a payment method below to get protected.\n\n**Plans:**\n{plan_lines}",
            EMBED_COLOR
        )
        msg = await channel.send(embed=embed, view=ShopPanelView())
        return channel, msg, None
    except Exception as e:
        return None, None, str(e)

# =================================================================
# BACKGROUND: LTC ORDER POLLING
# =================================================================

@tasks.loop(seconds=LTC_POLL_INTERVAL)
async def poll_ltc_orders():
    now = time.time()
    async with pool.acquire() as conn:
        pending = await conn.fetch("SELECT * FROM ltc_orders WHERE status='pending'")

        for order in pending:
            if now > order["expires_at"]:
                await conn.execute("UPDATE ltc_orders SET status='expired' WHERE id=$1", order["id"])
                continue

            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(f"https://api.blockcypher.com/v1/ltc/main/addrs/{order['address']}")
                    resp.raise_for_status()
                    data = resp.json()
            except Exception:
                continue  # transient network issue, try again next cycle

            target_amount = float(order["ltc_amount"])
            txs = data.get("txrefs", []) + data.get("unconfirmed_txrefs", [])

            for tx in txs:
                tx_value_ltc = tx.get("value", 0) / 1e8
                confirmations = tx.get("confirmations", 0)
                if abs(tx_value_ltc - target_amount) < 0.000001 and confirmations >= LTC_MIN_CONFIRMATIONS:
                    await conn.execute(
                        "UPDATE ltc_orders SET status='paid', completed_at=$2, tx_hash=$3 WHERE id=$1",
                        order["id"], time.time(), tx.get("tx_hash", "unknown")
                    )
                    await deliver_key(order["user_id"], order["plan_label"], order["duration_days"], float(order["usd_price"]), "ltc", reseller_id=order["owner_id"])
                    await notify_reseller_sale(order["owner_id"], order["user_id"], order["plan_label"], float(order["usd_price"]), "ltc")
                    break


@poll_ltc_orders.before_loop
async def before_poll():
    await bot.wait_until_ready()

# =================================================================
# GIFT CARD APPROVAL COMMANDS (admin only, manual verification)
# =================================================================

@bot.command(name="giftorders")
async def giftorders(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM gift_orders WHERE status='pending' ORDER BY created_at ASC")
    if not rows:
        return await ctx.send(embed=brand_embed("🎁 Pending Gift Orders", "None right now.", EMBED_COLOR))
    embed = brand_embed("🎁 Pending Gift Orders", color=EMBED_COLOR)
    for r in rows:
        embed.add_field(
            name=f"Order #{r['id']}",
            value=f"Buyer: `{r['user_id']}`\nSeller: `{r['owner_id']}`\nPlan: {r['plan_label']} — ${r['usd_price']}\nCode: `{r['code']}`",
            inline=False
        )
    await ctx.send(embed=embed)


@bot.command(name="approvegift")
async def approvegift(ctx, order_id: int = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not order_id:
        return await ctx.send(embed=brand_embed("Usage", "`!approvegift <order_id>`", discord.Color.orange()))

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM gift_orders WHERE id=$1", order_id)
        if not row:
            return await ctx.send(embed=brand_embed("❌ Not Found", color=discord.Color.red()))
        if row["status"] != "pending":
            return await ctx.send(embed=brand_embed("Already Resolved", f"Status is `{row['status']}`.", discord.Color.orange()))

        await conn.execute("UPDATE gift_orders SET status='approved', resolved_at=$2 WHERE id=$1", order_id, time.time())

    await deliver_key(row["user_id"], row["plan_label"], row["duration_days"], float(row["usd_price"]), "gift", reseller_id=row["owner_id"])
    await notify_reseller_sale(row["owner_id"], row["user_id"], row["plan_label"], float(row["usd_price"]), "gift")
    await ctx.send(embed=brand_embed("✅ Approved & Delivered", f"Key sent to `{row['user_id']}`.", discord.Color.green()))


@bot.command(name="denygift")
async def denygift(ctx, order_id: int = None, *, reason: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return
    if not order_id:
        return await ctx.send(embed=brand_embed("Usage", "`!denygift <order_id> <reason>`", discord.Color.orange()))

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM gift_orders WHERE id=$1", order_id)
        if not row:
            return await ctx.send(embed=brand_embed("❌ Not Found", color=discord.Color.red()))
        if row["status"] != "pending":
            return await ctx.send(embed=brand_embed("Already Resolved", f"Status is `{row['status']}`.", discord.Color.orange()))

        deny_reason = reason or "Invalid or already-used code."
        await conn.execute("UPDATE gift_orders SET status='denied', resolved_at=$2, deny_reason=$3 WHERE id=$1",
                            order_id, time.time(), deny_reason)

    await ctx.send(embed=brand_embed("❌ Denied", f"Order `{order_id}` denied.", discord.Color.red()))
    try:
        user = await bot.fetch_user(row["user_id"])
        embed = brand_embed("❌ Gift Card Rejected", f"Your gift card code couldn't be verified.\nReason: {deny_reason}\n\nContact support if you think this is a mistake.", discord.Color.red())
        await user.send(embed=embed)
    except Exception:
        pass

# =================================================================
# SLASH COMMANDS
# =================================================================

@bot.tree.command(name="setltc", description="Set the FALLBACK LTC address used by sellers who haven't set their own.")
@app_commands.describe(address="Your Litecoin payout address")
async def setltc(interaction: discord.Interaction, address: str):
    if not is_master(interaction.user.id):
        return await interaction.response.send_message("Not authorized.", ephemeral=True)

    shop_settings["ltc_address"] = address.strip()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shop_settings (id, ltc_address) VALUES (1, $1)
            ON CONFLICT (id) DO UPDATE SET ltc_address=$1
        """, address.strip())

    await interaction.response.send_message(
        embed=brand_embed("✅ Fallback LTC Address Set", f"`{address.strip()}`\n\nSellers who haven't run `!setmyltc` will route payments here.", discord.Color.green()),
        ephemeral=True
    )


@bot.tree.command(name="shop", description="Post or refresh your Perc shop panel in this server.")
async def shop_command(interaction: discord.Interaction):
    owner_id = get_owner_id_for_guild(interaction.guild_id)
    if owner_id is None or owner_id != interaction.user.id:
        return await interaction.response.send_message("Only the configured server owner can do this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    cfg = get_config(owner_id)
    channel, msg, err = await create_shop_panel(interaction.guild, cfg, owner_id)
    if channel:
        cfg["shop_channel_id"] = channel.id
        cfg["shop_message_id"] = msg.id
        await db_upsert_config(owner_id, cfg)
        await interaction.followup.send(embed=brand_embed("✅ Shop Panel Live", channel.mention, discord.Color.green()), ephemeral=True)
    else:
        await interaction.followup.send(embed=brand_embed("❌ Failed", err, discord.Color.red()), ephemeral=True)

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
            "⚠️ In your reason, include the **User ID of your OLD account** "
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
    activation = {"key": key, "expires_at": expires_at, "duration_label": entry["duration_label"],
                  "warned_3d": False, "warned_expired": False}
    licenses.setdefault("activations", {})[str(ctx.author.id)] = activation

    await db_upsert_license_key(key, entry)
    await db_upsert_activation(ctx.author.id, activation)

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


@bot.command(name="bought")
async def bought(ctx, user_id: str = None, duration: str = None, price: float = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return

    if not user_id or not duration:
        embed = brand_embed(f"💰 {BRAND_NAME} Sale Fulfillment", "Generates a key, sends it to the buyer, and logs the sale.", EMBED_COLOR)
        embed.add_field(name="Usage", value="`!bought <user_id> <duration> [price]`", inline=False)
        embed.add_field(
            name="Examples",
            value=(
                "`!bought 123456789012345678 30d` → uses default $7.99\n"
                "`!bought 123456789012345678 lifetime` → uses default $19.99\n"
                "`!bought 123456789012345678 90d 14.99` → custom price for a plan with no default"
            ),
            inline=False
        )
        return await ctx.send(embed=embed)

    if not valid_discord_id(user_id):
        return await ctx.send(embed=brand_embed("❌ Invalid User ID", "Should be 15–20 digits.", discord.Color.red()))

    label, days = parse_duration(duration)
    if label is None:
        return await ctx.send(embed=brand_embed("❌ Invalid Duration", "Try `lifetime`, `30d`, `6m`, `1y`, etc.", discord.Color.red()))

    final_price = price if price is not None else PRICE_MAP.get(label)
    if final_price is None:
        return await ctx.send(embed=brand_embed(
            "❌ No Price Set For This Plan",
            f"There's no default price for `{label}`. Specify one:\n`!bought {user_id} {duration} <price>`",
            discord.Color.orange()
        ))

    bound_id = int(user_id)

    try:
        user = await bot.fetch_user(bound_id)
    except discord.NotFound:
        return await ctx.send(embed=brand_embed("❌ No Such User", "That ID doesn't match a real Discord account.", discord.Color.red()))
    except Exception as e:
        return await ctx.send(embed=brand_embed("❌ Error", str(e), discord.Color.red()))

    key = gen_license_key()
    entry = {
        "duration_label": label, "duration_days": days,
        "bound_user_id": bound_id,
        "used": False, "used_by": None,
        "created_at": time.time(), "used_at": None,
    }
    licenses["keys"][key] = entry
    await db_upsert_license_key(key, entry)

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sales (user_id, duration_label, price, method, created_at) VALUES ($1,$2,$3,$4,$5)",
            bound_id, label, final_price, "manual", time.time()
        )

    duration_display = "Lifetime ♾️" if days is None else f"{label} ({days} days)"

    dm_embed = brand_embed(f"🎉 Thanks for picking up {BRAND_NAME}", color=discord.Color.green())
    dm_embed.add_field(name="Your key", value=f"`{key}`", inline=False)
    dm_embed.add_field(name="Plan", value=duration_display, inline=True)
    dm_embed.add_field(name="Next step", value="Reply here with `!activate <key>` to get set up.", inline=False)

    try:
        await user.send(embed=dm_embed)
        confirm = brand_embed("✅ Sent", f"Key delivered to `{bound_id}`.\n`{key}` — {duration_display} — **${final_price:.2f}**", discord.Color.green())
    except discord.Forbidden:
        confirm = brand_embed(
            "⚠️ Couldn't DM Them",
            f"Key was still created and the sale was logged, but their DMs are closed. Send it manually:\n`{key}` — {duration_display}",
            discord.Color.orange()
        )
    await ctx.send(embed=confirm)


@bot.command(name="generate", aliases=["genk"])
async def generate_keys(ctx, duration: str = None, user_id: str = None, count: int = 1):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return

    if duration is None or user_id is None:
        embed = brand_embed(f"🔑 {BRAND_NAME} License Generator", "Generate keys locked to a specific customer's account without sending them.", EMBED_COLOR)
        embed.add_field(name="Usage", value="`!genk <duration> <user_id> [count]`", inline=False)
        embed.add_field(
            name="Duration options",
            value="`7d`, `30d`, `90d`, `1m`, `6m`, `1y`, `lifetime` — any `<number>d/w/m/y` works.",
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
        entry = {
            "duration_label": label, "duration_days": days,
            "bound_user_id": bound_id,
            "used": False, "used_by": None,
            "created_at": time.time(), "used_at": None,
        }
        licenses["keys"][key] = entry
        await db_upsert_license_key(key, entry)
        new_keys.append(key)

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
    await db_add_blacklist(user_id)
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
    await db_remove_blacklist(user_id)
    await ctx.send(embed=brand_embed("✅ User Unblacklisted", f"`{user_id}` has regained access.", discord.Color.green()))


@bot.command()
async def stats(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    if not is_master(ctx.author.id):
        return

    async with pool.acquire() as conn:
        revenue_row = await conn.fetchrow("SELECT COALESCE(SUM(price),0) AS total, COUNT(*) AS count FROM sales")
        by_plan = await conn.fetch("""
            SELECT duration_label, COUNT(*) AS count, COALESCE(SUM(price),0) AS total
            FROM sales GROUP BY duration_label ORDER BY total DESC
        """)
        by_method = await conn.fetch("""
            SELECT method, COUNT(*) AS count, COALESCE(SUM(price),0) AS total
            FROM sales GROUP BY method ORDER BY total DESC
        """)
        top_resellers = await conn.fetch("""
            SELECT reseller_id, COUNT(*) AS count, COALESCE(SUM(price),0) AS total
            FROM sales WHERE reseller_id IS NOT NULL
            GROUP BY reseller_id ORDER BY total DESC LIMIT 5
        """)
        total_raids = await conn.fetchval("SELECT COUNT(*) FROM raid_events")
        neutralized_raids = await conn.fetchval("SELECT COUNT(*) FROM raid_events WHERE neutralized = true")

    active_licenses = sum(
        1 for e in licenses["activations"].values()
        if e.get("expires_at") is None or e["expires_at"] > time.time()
    )
    protected_servers = sum(1 for c in configs.values() if c.get("setup_complete"))

    embed = brand_embed(f"📊 {BRAND_NAME} — Universal Stats", "Numbers across every customer and server.", EMBED_COLOR)
    embed.add_field(name="💰 Total Revenue", value=f"${revenue_row['total']:.2f} from {revenue_row['count']} sale(s)", inline=False)

    if by_plan:
        breakdown = "\n".join(f"**{r['duration_label']}** — {r['count']} sold — ${r['total']:.2f}" for r in by_plan)
        embed.add_field(name="Sales by Plan", value=breakdown, inline=False)

    if by_method:
        method_breakdown = "\n".join(f"**{r['method']}** — {r['count']} sale(s) — ${r['total']:.2f}" for r in by_method)
        embed.add_field(name="Sales by Method", value=method_breakdown, inline=False)

    if top_resellers:
        reseller_breakdown = "\n".join(f"`{r['reseller_id']}` — {r['count']} sale(s) — ${r['total']:.2f}" for r in top_resellers)
        embed.add_field(name="🏆 Top Resellers", value=reseller_breakdown, inline=False)

    embed.add_field(name="🛡️ Raids Stopped", value=f"{neutralized_raids} neutralized / {total_raids} detected", inline=True)
    embed.add_field(name="🔑 Active Licenses", value=str(active_licenses), inline=True)
    embed.add_field(name="🌐 Protected Servers", value=str(protected_servers), inline=True)
    await ctx.send(embed=embed)

# =================================================================
# ACCOUNT SWITCH REQUESTS
# =================================================================

@bot.command()
async def useridswitch(ctx, *, reason: str = None):
    if not isinstance(ctx.channel, discord.DMChannel):
        return

    if not reason:
        embed = brand_embed(
            "🔄 Account Switch Request",
            "Usage: `!useridswitch <reason>`\n\n"
            "⚠️ Include the **User ID of your OLD account** (the one your key is currently bound to).\n\n"
            "You must first attempt `!activate <key>` with the key in question before running this.",
            EMBED_COLOR
        )
        return await ctx.send(embed=embed)

    context = pending_switch_context.get(ctx.author.id)
    if not context:
        return await ctx.send(embed=brand_embed(
            "⚠️ No Pending Attempt Found",
            "Run `!activate <your key>` first, then run `!useridswitch <reason>`.",
            discord.Color.orange()
        ))

    key = context["key"]
    entry = licenses["keys"].get(key)
    if not entry:
        del pending_switch_context[ctx.author.id]
        return await ctx.send(embed=brand_embed("❌ Key No Longer Exists", "Please contact support.", discord.Color.red()))

    req_id = await db_insert_switch_request(key, entry["bound_user_id"], ctx.author.id, reason)
    switch_requests["requests"][str(req_id)] = {
        "key": key, "old_user_id": entry["bound_user_id"], "new_user_id": ctx.author.id,
        "reason": reason, "status": "pending", "created_at": time.time(),
    }

    await ctx.send(embed=brand_embed(
        "✅ Switch Request Submitted",
        f"Request `#{req_id}` has been sent for review. You'll be notified once it's approved or denied.",
        discord.Color.green()
    ))

    try:
        master = await bot.fetch_user(MASTER_USER_ID)
        alert = brand_embed("🔔 New Account Switch Request", color=discord.Color.orange())
        alert.add_field(name="Request ID", value=str(req_id), inline=True)
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
    await db_upsert_license_key(key, licenses["keys"][key])

    if str(old_id) in licenses.get("activations", {}):
        activation = licenses["activations"].pop(str(old_id))
        activation["key"] = key
        licenses["activations"][str(new_id)] = activation
        await db_delete_activation(old_id)
        await db_upsert_activation(new_id, activation)

    migrated_config = False
    if str(old_id) in configs:
        cfg = configs.pop(str(old_id))
        configs[str(new_id)] = cfg
        await db_delete_config(old_id)
        await db_upsert_config(new_id, cfg)
        migrated_config = True

        if str(old_id) in shop_plans:
            plans = shop_plans.pop(str(old_id))
            shop_plans[str(new_id)] = plans
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM shop_plans WHERE owner_id=$1", old_id)
            for pk, p in plans.items():
                await db_upsert_plan(new_id, pk, p["label"], p["days"], p["price"], p["position"])

    rebuild_guild_index()

    now = time.time()
    req["status"] = "approved"
    req["resolved_at"] = now
    await db_approve_switch_request(req_id, now)
    pending_switch_context.pop(new_id, None)

    await ctx.send(embed=brand_embed(
        "✅ Switch Approved",
        f"Key `{key}` and license now bound to `{new_id}`." + (" Server config and shop migrated too." if migrated_config else ""),
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

    now = time.time()
    req["status"] = "denied"
    req["deny_reason"] = reason or "No reason given."
    req["resolved_at"] = now
    await db_deny_switch_request(req_id, req["deny_reason"], now)

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
    old_channel_id = cfg.get("log_channel_id") if cfg else None
    old_category_id = cfg.get("log_category_id") if cfg else None

    channels_to_remove, categories_to_remove = [], []

    if old_channel_id:
        ch = guild.get_channel(old_channel_id)
        if ch:
            channels_to_remove.append(ch)
    if old_category_id:
        cat = guild.get_channel(old_category_id)
        if cat:
            categories_to_remove.append(cat)

    for ch in guild.text_channels:
        if ch.name == "perc-logs" and ch not in channels_to_remove:
            channels_to_remove.append(ch)
    for cat in guild.categories:
        if cat.name == "Perc Security" and cat not in categories_to_remove:
            categories_to_remove.append(cat)

    for ch in channels_to_remove:
        try:
            await ch.delete(reason=f"{BRAND_NAME}: replacing log setup")
        except Exception:
            pass
    for cat in categories_to_remove:
        try:
            await cat.delete(reason=f"{BRAND_NAME}: replacing log setup")
        except Exception:
            pass

    everyone = guild.default_role
    overwrites = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        owner_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }

    try:
        category = await guild.create_category("Perc Security", overwrites=overwrites, reason=f"{BRAND_NAME}: log category")
        try:
            await category.edit(position=len(guild.categories) + 10)
        except Exception:
            pass

        channel = await guild.create_text_channel(
            "perc-logs", category=category, overwrites=overwrites,
            reason=f"{BRAND_NAME}: log channel",
            topic=f"🛡️ {BRAND_NAME} security logs — raids, lockdowns, and recovery events."
        )
        try:
            await channel.send(embed=brand_embed(
                f"🛡️ {BRAND_NAME} Logs Are Live",
                "This is where you'll see everything happen in real time — raids getting shut down, lockdowns, bans, all of it.",
                EMBED_COLOR
            ))
        except Exception:
            pass
        return channel, category, None
    except Exception as e:
        return None, None, str(e)


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
    shop_ch = guild.get_channel(cfg.get("shop_channel_id")) if guild and cfg.get("shop_channel_id") else None
    embed = brand_embed("📋 Your Configuration", color=EMBED_COLOR)
    embed.add_field(name="Server", value=f"{guild.name if guild else 'Unknown'} (`{cfg['guild_id']}`)", inline=False)
    embed.add_field(name="Owner Role", value=cfg["owner_role_name"], inline=True)
    embed.add_field(name="Log Channel", value=log_ch.mention if log_ch else "Missing — run !fixlogs", inline=True)
    embed.add_field(name="Shop Channel", value=shop_ch.mention if shop_ch else "Not created — run `/shop`", inline=True)
    embed.add_field(name="Your LTC Address", value=(f"`{cfg['ltc_address']}`" if cfg.get("ltc_address") else "Not set — using platform default (`!setmyltc` to set your own)"), inline=False)
    embed.add_field(name="Plans Configured", value=str(len(get_owner_plans(ctx.author.id))), inline=True)
    embed.add_field(name="Invite Link", value=cfg["invite_link"], inline=False)
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
        invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot%20applications.commands"
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
            invite_bot_url = f"https://discord.com/oauth2/authorize?client_id={BOT_CLIENT_ID}&permissions=8&scope=bot%20applications.commands"
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
        log_channel, log_category, log_err = await create_log_channel(guild, member, existing_cfg)

        new_cfg = {
            "guild_id": data["guild_id"],
            "owner_role_name": "OWNER",
            "invite_link": content,
            "trusted": [],
            "log_channel_id": log_channel.id if log_channel else None,
            "log_category_id": log_category.id if log_category else None,
            "shop_channel_id": None,
            "shop_message_id": None,
            "setup_complete": True,
            "ltc_address": existing_cfg.get("ltc_address"),
        }
        configs[str(user_id)] = new_cfg
        await db_upsert_config(user_id, new_cfg)
        await seed_default_plans(user_id)
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
            value=(
                "Keep my role at the **top** of Server Settings → Roles.\n"
                "Default plans (30 Days $7.99, Lifetime $19.99) are ready — customize with `!addplan`/`!removeplan`, view with `!plans`.\n"
                "Run `!setmyltc <address>` so LTC payments come straight to you.\n"
                "Run `/shop` in your server to post the buy panel.\n"
                "Run `!backup` now.\n"
                "Type `!help` to see everything I can do."
            ),
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
async def fixlogs(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    member = guild.get_member(ctx.author.id)
    channel, category, err = await create_log_channel(guild, member, cfg)
    if channel:
        cfg["log_channel_id"] = channel.id
        cfg["log_category_id"] = category.id if category else None
        await db_upsert_config(ctx.author.id, cfg)
        await ctx.send(embed=brand_embed("✅ Log Channel Refreshed", f"Fresh channel created: {channel.mention}", discord.Color.green()))
    else:
        await ctx.send(embed=brand_embed("❌ Failed", f"{err}\nCheck Manage Channels permission.", discord.Color.red()))

# =================================================================
# SHOP PLAN MANAGEMENT (per-owner customization)
# =================================================================

@bot.command(name="addplan")
@require_setup()
async def addplan(ctx, duration: str = None, price: float = None, *, label: str = None):
    if not duration or price is None:
        embed = brand_embed(f"🛒 Add / Update a Plan", "Add a new plan to your `/shop` panel, or update an existing one (same duration = overwrite).", EMBED_COLOR)
        embed.add_field(name="Usage", value="`!addplan <duration> <price> [custom label]`", inline=False)
        embed.add_field(
            name="Examples",
            value=(
                "`!addplan 60d 12.99` → auto-labeled \"60 Days\"\n"
                "`!addplan 60d 12.99 Two Month Pass` → custom label\n"
                "`!addplan lifetime 24.99 Forever Access`"
            ),
            inline=False
        )
        embed.add_field(name="Duration formats", value="`<number>d/w/m/y` (e.g. `7d`, `6m`, `1y`) or `lifetime`.", inline=False)
        return await ctx.send(embed=embed)

    dur_key, days = parse_duration(duration)
    if dur_key is None:
        return await ctx.send(embed=brand_embed("❌ Invalid Duration", "Try `30d`, `60d`, `6m`, `1y`, `lifetime`, etc.", discord.Color.red()))

    if price <= 0:
        return await ctx.send(embed=brand_embed("❌ Invalid Price", "Must be greater than 0.", discord.Color.red()))

    owner_plans = shop_plans.setdefault(str(ctx.author.id), {})
    existed = dur_key in owner_plans
    if not existed and len(owner_plans) >= MAX_PLANS_PER_OWNER:
        return await ctx.send(embed=brand_embed("❌ Too Many Plans", f"Max {MAX_PLANS_PER_OWNER} plans per shop. Remove one with `!removeplan` first.", discord.Color.red()))

    display_label = label or humanize_duration(dur_key, days)
    position = owner_plans[dur_key]["position"] if existed else len(owner_plans)
    final_price = round(price, 2)

    owner_plans[dur_key] = {"label": display_label, "days": days, "price": final_price, "position": position}
    await db_upsert_plan(ctx.author.id, dur_key, display_label, days, final_price, position)

    dur_display = "Lifetime ♾️" if days is None else f"{days} days"
    await ctx.send(embed=brand_embed(
        f"✅ Plan {'Updated' if existed else 'Added'}",
        f"**{display_label}** (`{dur_key}`) — ${final_price:.2f} — {dur_display}\n\nRun `/shop` again in your server to refresh the live panel.",
        discord.Color.green()
    ))


@bot.command(name="removeplan")
@require_setup()
async def removeplan(ctx, duration: str = None):
    if not duration:
        return await ctx.send(embed=brand_embed("Usage", "`!removeplan <duration>` — e.g. `!removeplan 60d`. Run `!plans` to see your keys.", discord.Color.orange()))

    dur_key, _ = parse_duration(duration)
    if dur_key is None:
        return await ctx.send(embed=brand_embed("❌ Invalid Duration", "Run `!plans` to see your current plan keys.", discord.Color.red()))

    owner_plans = shop_plans.get(str(ctx.author.id), {})
    if dur_key not in owner_plans:
        return await ctx.send(embed=brand_embed("❌ Not Found", f"No plan with key `{dur_key}`. Run `!plans` to see what you've got.", discord.Color.red()))

    del owner_plans[dur_key]
    await db_delete_plan(ctx.author.id, dur_key)
    await ctx.send(embed=brand_embed("🗑️ Plan Removed", f"`{dur_key}` removed. Run `/shop` again to refresh your live panel.", discord.Color.green()))


@bot.command(name="plans")
@require_setup()
async def plans_cmd(ctx):
    owner_plans = get_owner_plans(ctx.author.id)
    if not owner_plans:
        return await ctx.send(embed=brand_embed(
            "🛒 Your Plans",
            "No plans configured yet. Run `!resetplans` for defaults, or `!addplan <duration> <price>` to add your own.",
            discord.Color.orange()
        ))
    embed = brand_embed("🛒 Your Shop Plans", "These are what customers see on your `/shop` panel.", EMBED_COLOR)
    for p in owner_plans:
        dur_display = "Lifetime ♾️" if p["days"] is None else f"{p['days']} days"
        embed.add_field(name=f"{p['label']}  (`{p['key']}`)", value=f"${p['price']:.2f} — {dur_display}", inline=False)
    embed.set_footer(text="Use !addplan / !removeplan to customize. Run /shop again after changes to refresh the live panel.")
    await ctx.send(embed=embed)


@bot.command(name="resetplans")
@require_setup()
async def resetplans(ctx):
    shop_plans[str(ctx.author.id)] = {}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shop_plans WHERE owner_id=$1", ctx.author.id)
    await seed_default_plans(ctx.author.id)
    await ctx.send(embed=brand_embed("✅ Plans Reset", "Restored to defaults: 30 Days ($7.99) and Lifetime ($19.99). Run `/shop` again to refresh your panel.", discord.Color.green()))


@bot.command(name="setmyltc")
@require_setup()
async def setmyltc(ctx, address: str = None):
    if not address:
        return await ctx.send(embed=brand_embed(
            "Usage", "`!setmyltc <your LTC address>`\n\nLTC payments made through your `/shop` panel will be sent directly here instead of the platform default.",
            discord.Color.orange()
        ))
    cfg = get_config(ctx.author.id)
    cfg["ltc_address"] = address.strip()
    await db_upsert_config(ctx.author.id, cfg)
    await ctx.send(embed=brand_embed("✅ Your LTC Address Set", f"`{address.strip()}`\n\nAll LTC payments from your shop now go here.", discord.Color.green()))


@bot.command(name="mysales")
@require_setup()
async def mysales(ctx):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(*) AS count, COALESCE(SUM(price),0) AS total FROM sales WHERE reseller_id=$1", ctx.author.id)
    embed = brand_embed(
        "💰 Your Shop Sales",
        f"**{row['count']}** sale(s) totaling **${row['total']:.2f}** through your `/shop` panel.",
        EMBED_COLOR
    )
    await ctx.send(embed=embed)

# =================================================================
# BACKUP / RESTORE  (Postgres-backed)
# =================================================================

def serialize_overwrites(channel):
    result = {}
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role):
            allow, deny = ow.pair()
            result[f"role:{target.name}"] = {"allow": allow.value, "deny": deny.value}
    return result


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

    backed_up_at = time.time()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO backups (guild_id, data, backed_up_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (guild_id) DO UPDATE SET data=$2, backed_up_at=$3
        """, guild.id, json.dumps(data), backed_up_at)
    return data


async def do_restore(guild, dm):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT data FROM backups WHERE guild_id=$1", guild.id)
    if not row:
        return await dm.send(embed=brand_embed("❌ No Backup Found", color=discord.Color.red()))
    data = json.loads(row["data"])

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
# LOCKDOWN  (Postgres-backed)
# =================================================================

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

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO lockdowns (guild_id, state) VALUES ($1,$2)
            ON CONFLICT (guild_id) DO UPDATE SET state=$2
        """, guild.id, json.dumps(state))

    lockdown_active_map[guild.id] = True
    return count


async def do_unlock(guild):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state FROM lockdowns WHERE guild_id=$1", guild.id)
    if not row:
        return 0
    state = json.loads(row["state"])
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

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM lockdowns WHERE guild_id=$1", guild.id)

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
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM lockdowns WHERE guild_id=$1", guild.id)
    if not lockdown_active_map.get(guild.id) and not row:
        return await ctx.send(embed=brand_embed("Not Locked", color=EMBED_COLOR))
    count = await do_unlock(guild)
    await log_embed(ctx.author.id, guild, "🔓 Lockdown Lifted",
        f"Manually lifted by the owner. **{count}** channels restored to their exact original state.",
        discord.Color.green())

# =================================================================
# KILL SWITCH
# =================================================================

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
# STATUS
# =================================================================

@bot.command()
@require_setup()
async def status(ctx):
    cfg = get_config(ctx.author.id)
    guild = bot.get_guild(cfg["guild_id"])
    perms = guild.me.guild_permissions

    async with pool.acquire() as conn:
        backup_row = await conn.fetchrow("SELECT backed_up_at FROM backups WHERE guild_id=$1", guild.id)
    backup_age = "Never"
    if backup_row and backup_row["backed_up_at"]:
        backup_age = f"{int((time.time() - backup_row['backed_up_at']) // 60)}m ago"

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

# =================================================================
# HELP
# =================================================================

@bot.command(name="help")
async def custom_help(ctx):
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    embed = brand_embed(f"🛡️ {BRAND_NAME}", "Here's everything I can do for you.", EMBED_COLOR)
    embed.add_field(name="🔑 License & Setup", value=(
        "`!activate <key>` — plug in your key and you're live\n"
        "`!mylicense` — see how much time you've got left\n"
        "`!setup` / `!resetup` — hook me up to your server\n"
        "`!myconfig` — check what's currently configured\n"
        "`!useridswitch <reason>` — moved accounts? bring your key with you"
    ), inline=False)
    embed.add_field(name="🛒 Customize Your Shop", value=(
        "`/shop` — post or refresh your buy panel in your server\n"
        "`!addplan <duration> <price> [label]` — add/update a plan (e.g. `!addplan 60d 12.99 Two Month Pass`)\n"
        "`!removeplan <duration>` — remove a plan\n"
        "`!plans` — view your current plans\n"
        "`!resetplans` — restore the default 30-day/lifetime plans\n"
        "`!setmyltc <address>` — send YOUR shop's LTC payments straight to your own wallet\n"
        "`!mysales` — see how much your shop has sold"
    ), inline=False)
    embed.add_field(name="🚑 If Something Goes Wrong", value=(
        "`!invite` — grab your server's invite link\n"
        "`!owner` — get your OWNER role back\n"
        "`!unban` — unban yourself if you got hit"
    ), inline=False)
    embed.add_field(name="💾 Backups", value="`!backup` to save the server's current state, `!restore` to bring it back later.", inline=False)
    embed.add_field(name="💥 The Nuclear Option", value="`!kill` wipes everything — it'll double-check with you twice before doing it. `!cancel` backs out at any point.", inline=False)
    embed.add_field(name="🔐 Locking Things Down", value="`!lockdown` freezes the server, `!unlock` puts it back exactly how it was.", inline=False)
    embed.add_field(name="📜 Logs", value="`!fixlogs` — rebuilds your log channel fresh if it ever breaks or disappears.", inline=False)
    embed.add_field(name="📊 Health Check", value="`!status` — a quick look at what's working and what isn't.", inline=False)
    embed.add_field(name="🛡️ What I Do Without Being Asked", value=(
        "If you get banned, I unban you.\n"
        "If someone starts mass-banning or mass-deleting, I stop them and reverse it.\n"
        "If a bunch of accounts join at once, I lock things down before it turns into a mess.\n"
        "Everything gets logged so you're never left wondering what happened."
    ), inline=False)
    if is_master(ctx.author.id):
        embed.add_field(name="🔑 Just for You", value=(
            "`/setltc <address>` — set the FALLBACK LTC address (used by sellers without their own)\n"
            "`!bought <user_id> <duration> [price]` — sell a key and send it in one step\n"
            "`!stats` — revenue, sales breakdown, top resellers, raids stopped, active licenses\n"
            "`!genk <duration> <user_id> [count]` — generate without sending\n"
            "`!giftorders` — see pending gift card orders\n"
            "`!approvegift <id>` / `!denygift <id> <reason>`\n"
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

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO raid_events (guild_id, owner_id, actor_id, action_desc, neutralized, created_at)
            VALUES ($1,$2,$3,$4,$5,$6)
        """, guild.id, owner_id, actor.id, action_desc, neutralized, time.time())

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
    for user_id_str, entry in list(licenses.get("activations", {}).items()):
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
            await db_upsert_activation(int(user_id_str), entry)

        elif remaining <= 0 and not entry.get("warned_expired"):
            try:
                user = await bot.fetch_user(int(user_id_str))
                embed = brand_embed(f"🔒 {BRAND_NAME} License Expired", "Protection is paused. Purchase a new key and run `!activate`.", discord.Color.red())
                await user.send(embed=embed)
            except Exception:
                pass
            entry["warned_expired"] = True
            await db_upsert_activation(int(user_id_str), entry)


@check_expirations.before_loop
async def before_check():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}. Serving {len(configs)} configured server(s).")


bot.run(BOT_TOKEN)
