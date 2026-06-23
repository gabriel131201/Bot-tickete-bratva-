import os
import asyncio
import discord
from discord.ext import tasks, commands
from discord import app_commands, Interaction
import datetime
import pytz
from flask import Flask
import threading
from collections import defaultdict

# === ROLURI PERMISE ===
LEADER_ROLE_ID = 1107100643291828224
SECONDARY_LEADER_ROLE_ID = 1515017621127299303
COLEADER_ROLE_ID = 1107099637644529684
ALLOWED_ROLE_IDS = {LEADER_ROLE_ID, SECONDARY_LEADER_ROLE_ID, COLEADER_ROLE_ID}

# === CONFIG BOT / SERVER ===
APPLICATION_ID = 1518778350670319736
TARGET_GUILD_ID = 1107074840378220645
PUBLIC_KEY = "bd7f76a55de869fea03a3e21cdf034cd376095dd9e309544d11d6a92cf98e018"
INVITE_URL = (
    f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
    "&permissions=8&scope=bot%20applications.commands"
)

TOKEN = os.getenv("DISCORD_TOKEN", "")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, application_id=APPLICATION_ID)

DATA_FILE = "backup.txt"
TICKET_DATA = {}
BUCHAREST_TZ = pytz.timezone("Europe/Bucharest")

app = Flask('')

@app.route('/')
def home():
    return "✅ Donul veghează. Botul este online."

def run_flask():
    port = 8080
    app.run(host='0.0.0.0', port=port)

def save_backup():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        for channel_id, tickets in TICKET_DATA.items():
            f.write(f"Canal: {channel_id}\n")
            for t in tickets:
                status = "activ" if not t['expired'] else "inactiv"
                taxa = "plătită" if t['paid'] else "neplătită"
                deleted = "DA" if t.get('deleted') else "NU"
                deleted_by = t.get('deleted_by_name') or "-"
                metas = t.get('emojis_meta') or []
                def fmt(m): 
                    return f"{'a' if m.get('animated') else ''}:{m.get('name')}:{m.get('id')}" if m.get('id') else (m.get('name') or "?")
                emojis_txt = ",".join(fmt(m) for m in metas) if metas else "-"
                f.write(
                    f"Ticket {t['id']}: făcut la {t['start']}, terminat la {t['end']}, creat de {t['author']}, "
                    f"ID: {t['player_id']}, status: {status}, taxă: {taxa}, emojis:[{emojis_txt}], "
                    f"sters:{deleted}, sters_de:{deleted_by}\n"
                )
            f.write("\n")

def get_now(): return datetime.datetime.now(BUCHAREST_TZ)
def format_time(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")
def parse_time(s): return BUCHAREST_TZ.localize(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))
def format_hour_only(s): return parse_time(s).strftime("%H:%M")
def time_remaining(end_str):
    remaining = parse_time(end_str) - get_now()
    if remaining.total_seconds() <= 0: return "expirat"
    h, m = divmod(int(remaining.total_seconds() // 60), 60)
    return f"{h}h {m}m"

def is_leader_or_coleader(member: discord.Member) -> bool:
    return any(r.id in ALLOWED_ROLE_IDS for r in getattr(member, "roles", []))

# === CHECK PERMISIUNI SLASH ===
def role_check(interaction: Interaction) -> bool:
    if isinstance(interaction.user, discord.Member) and is_leader_or_coleader(interaction.user):
        return True
    raise app_commands.CheckFailure("Nu ai permisiunea pentru această comandă.")

# === HANDLER ERORI (permisiuni) ===
@bot.tree.error
async def on_app_command_error(interaction: Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        try:
            await interaction.response.send_message("❌ Nu ai permisiunea pentru această comandă.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send_message("❌ Nu ai permisiunea pentru această comandă.", ephemeral=True)

@bot.event
async def on_ready():
    try:
        if TARGET_GUILD_ID:
            guild_obj = discord.Object(id=TARGET_GUILD_ID)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"✅ Comenzi sincronizate pe guild-ul țintă: {TARGET_GUILD_ID} -> {[c.name for c in synced]}")
        else:
            # fallback: sincronizare pe fiecare guild (fără copy_global_to, ca să nu dublăm)
            for g in bot.guilds:
                synced = await bot.tree.sync(guild=discord.Object(id=g.id))
                print(f"✅ Comenzi sincronizate pe {g.name}: {[c.name for c in synced]}")
    except Exception as e:
        print(f"Eroare la sync: {e}")
    update_ticket_status.start()  # rulează la 10 minute
    print(f"🔑 Application ID: {APPLICATION_ID}")
    print(f"🏠 Target Guild ID: {TARGET_GUILD_ID}")
    print(f"🔐 Public Key set: {'DA' if PUBLIC_KEY else 'NU'}")
    print(f"🔗 Invite URL (admin): {INVITE_URL}")
    print("🤵 Botul mafiot este online!")

# ===== Helpers pentru emoji =====
def meta_from_emoji(e):
    # e poate fi unicode (str) sau discord.Emoji/discord.PartialEmoji
    if isinstance(e, str):
        return {"id": None, "name": e, "animated": False}, e
    if isinstance(e, (discord.Emoji, discord.PartialEmoji)):
        disp = f"<{'a' if getattr(e, 'animated', False) else ''}:{e.name}:{e.id}>" if e.id else (e.name or str(e))
        return {"id": e.id, "name": e.name, "animated": getattr(e, "animated", False)}, disp
    # fallback
    return {"id": None, "name": str(e), "animated": False}, str(e)

async def build_reactions_snapshot(msg: discord.Message, guild: discord.Guild | None):
    """Construiește setul reacțiilor VALIDE (unic pe emoji), din mesaj.

    O reacție e validă doar dacă cel puțin un utilizator care a pus acel emoji
    are unul dintre rolurile permise.
    """
    metas = []
    displays = []
    seen = set()
    for r in msg.reactions:
        has_allowed_reactor = False
        async for user in r.users():
            if user.id == bot.user.id:
                continue
            member = user if isinstance(user, discord.Member) else (guild.get_member(user.id) if guild else None)
            if member and is_leader_or_coleader(member):
                has_allowed_reactor = True
                break

        if not has_allowed_reactor:
            continue

        m, disp = meta_from_emoji(r.emoji)
        key = m["id"] if m["id"] is not None else ("U", m["name"])
        if key in seen:
            continue
        seen.add(key)
        metas.append(m)
        displays.append(disp)
    return metas, displays

async def refresh_ticket_reactions(guild_id: int, channel_id: int, message_id: int, ticket: dict):
    """Citește mesajul și sincronizează exact reacțiile curente în ticket."""
    guild = bot.get_guild(guild_id) if guild_id else None
    channel = bot.get_channel(channel_id)
    if channel is None and guild is not None:
        channel = guild.get_channel(channel_id)
    if channel is None:
        return  # nu putem citi mesajul; lăsăm starea cum e
    try:
        msg = await channel.fetch_message(message_id)
    except Exception:
        return
    metas, displays = await build_reactions_snapshot(msg, guild)
    ticket["emojis_meta"] = metas
    ticket["emojis"] = displays
    ticket["paid"] = bool(displays)  # plătit doar dacă există cel puțin o reacție validă
    save_backup()

# ===== Reacții: sincronizare la ADD/REMOVE/CLEAR =====
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    msg_id = payload.message_id
    ch_id = payload.channel_id
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if ticket.get("message_id") == msg_id and not ticket.get("deleted"):
                await refresh_ticket_reactions(payload.guild_id or 0, ch_id, msg_id, ticket)
                return

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    msg_id = payload.message_id
    ch_id = payload.channel_id
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if ticket.get("message_id") == msg_id and not ticket.get("deleted"):
                await refresh_ticket_reactions(payload.guild_id or 0, ch_id, msg_id, ticket)
                return

@bot.event
async def on_raw_reaction_clear(payload: discord.RawReactionClearEvent):
    msg_id = payload.message_id
    ch_id = payload.channel_id
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if ticket.get("message_id") == msg_id and not ticket.get("deleted"):
                # toate reacțiile au dispărut
                ticket["emojis_meta"] = []
                ticket["emojis"] = []
                ticket["paid"] = False
                save_backup()
                return

@bot.event
async def on_raw_reaction_clear_emoji(payload: discord.RawReactionClearEmojiEvent):
    # o singură reacție (emoji) a fost ștearsă complet; refacem snapshot-ul
    msg_id = payload.message_id
    ch_id = payload.channel_id
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if ticket.get("message_id") == msg_id and not ticket.get("deleted"):
                await refresh_ticket_reactions(payload.guild_id or 0, ch_id, msg_id, ticket)
                return

# ===== Ștergere mesaj: marcăm ticketul ca deleted =====
@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    msg_id = payload.message_id
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if ticket.get("message_id") == msg_id and not ticket.get("deleted"):
                ticket["deleted"] = True
                ticket["deleted_at"] = format_time(get_now())
                ticket["deleted_by_id"] = None
                ticket["deleted_by_name"] = "necunoscut"
                try:
                    if payload.guild_id:
                        guild = bot.get_guild(payload.guild_id)
                        me = getattr(guild, "me", None) or (guild.get_member(bot.user.id) if guild else None)
                        if guild and me and me.guild_permissions.view_audit_log:
                            async for entry in guild.audit_logs(action=discord.AuditLogAction.message_delete, limit=5):
                                ch_ok = getattr(entry.extra, "channel", None)
                                if ch_ok and ch_ok.id == payload.channel_id:
                                    delta = datetime.datetime.now(datetime.timezone.utc) - entry.created_at
                                    if delta.total_seconds() <= 10:
                                        ticket["deleted_by_id"] = entry.user.id
                                        ticket["deleted_by_name"] = entry.user.display_name
                                        break
                except Exception:
                    pass
                save_backup()
                return

# ================= Comenzi =================

@bot.tree.command(name="ticket")
@app_commands.describe(player_id="ID-ul jucătorului")
async def ticket_command(interaction: Interaction, player_id: int):
    now = get_now()
    end = now + datetime.timedelta(hours=3)
    cid = str(interaction.channel_id)
    ticket_id = int(now.timestamp())
    if cid not in TICKET_DATA:
        TICKET_DATA[cid] = []
    ticket = {
        "id": ticket_id,
        "player_id": player_id,
        "start": format_time(now),
        "end": format_time(end),
        "author": interaction.user.name,
        "paid": False,
        "expired": False,
        "deleted": False,
        "deleted_by_id": None,
        "deleted_by_name": None,
        "deleted_at": None,
        "emojis_meta": [],
        "emojis": [],
        "message_id": None,
    }
    TICKET_DATA[cid].append(ticket)
    save_backup()

    embed = discord.Embed(title=f"🎫 Ticket #{ticket_id}", color=0x00ff00)
    embed.add_field(name="👤 Jucător ID", value=str(player_id), inline=True)
    embed.add_field(name="⏱️ Start", value=format_hour_only(ticket['start']), inline=True)
    embed.add_field(name="🕒 Sfârșit", value=format_hour_only(ticket['end']), inline=True)
    embed.add_field(name="🤵‍♂️ Creat de", value=f"**{interaction.user.name}**", inline=False)
    embed.set_footer(text="Status taxă: neplătită • Se iau în calcul doar reacțiile Lider/Lider Secundar/Colider")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    ticket["message_id"] = msg.id
    save_backup()

@bot.tree.command(name="tickets_reset", description="(comandă ascunsă)", extras={"hidden": True})
@app_commands.checks.has_permissions(administrator=True)
async def tickets_reset(interaction: Interaction):
    TICKET_DATA[str(interaction.channel_id)] = []
    save_backup()
    try:
        await interaction.response.send_message("✅", ephemeral=True)
    except:
        pass

@bot.tree.command(name="control")
async def control(interaction: Interaction):
    cid = str(interaction.channel_id)
    active = [t for t in TICKET_DATA.get(cid, []) if not t['expired'] and not t.get('deleted')]
    if not active:
        await interaction.response.send_message("Nu există tickete active.", delete_after=120)
        return
    msg = "**🎟️ Tickete active:**\n"
    for t in active:
        taxa = "✅ plătită" if t['paid'] else "❌ neplătită"
        msg += f"🟢 ID: `{t['player_id']}` | **{t['author']}** | ⏱️ {format_hour_only(t['start'])}-{format_hour_only(t['end'])} | ⌛ {time_remaining(t['end'])} | Taxă: {taxa}\n"
    await interaction.response.send_message(msg, delete_after=120)

@bot.tree.command(name="status")
@app_commands.check(role_check)
async def status(interaction: Interaction):
    cid = str(interaction.channel_id)
    data = [t for t in TICKET_DATA.get(cid, []) if not t.get('deleted')]
    a, i = sum(not t['expired'] for t in data), sum(t['expired'] for t in data)
    await interaction.response.send_message(f"✅ Tickete active: {a}\n❌ Tickete inactive: {i}")

@bot.tree.command(name="today")
async def today(interaction: Interaction):
    cid = str(interaction.channel_id)
    azi = get_now().date()
    today = [t for t in TICKET_DATA.get(cid, []) if (parse_time(t['start']).date() == azi and not t.get('deleted'))]
    if not today:
        await interaction.response.send_message("Niciun ticket creat azi.", delete_after=120)
        return
    msg = "🗓️ **Tickete de azi:**\n"
    for t in today:
        taxa = "✅ plătită" if t['paid'] else "❌ neplătită"
        msg += f"🟢 ID: `{t['player_id']}` | **{t['author']}** | ⏱️ {format_hour_only(t['start'])} - {format_hour_only(t['end'])} | Taxă: {taxa}\n"
    await interaction.response.send_message(msg, delete_after=120)

@bot.tree.command(name="cauta")
@app_commands.describe(player_id="ID-ul jucătorului")
async def cauta(interaction: Interaction, player_id: int):
    cid = str(interaction.channel_id)
    tickets = [t for t in TICKET_DATA.get(cid, []) if t['player_id'] == player_id and not t.get('deleted')]
    if not tickets:
        await interaction.response.send_message(f"Nu am găsit tickete pentru `{player_id}`.", delete_after=120)
        return
    msg = f"🔍 Tickete pentru `{player_id}`:\n"
    for t in tickets:
        s = "✅ plătită" if t['paid'] else "❌ neplătită"
        c = "🟢 activ" if not t['expired'] else "🔴 inactiv"
        msg += f"{c} | ⏱️ {format_hour_only(t['start'])}-{format_hour_only(t['end'])} | 👤 **{t['author']}** | Taxă: {s}\n"
    await interaction.response.send_message(msg, delete_after=120)

@bot.tree.command(name="raport")
@app_commands.check(role_check)
async def raport(interaction: Interaction):
    cid = str(interaction.channel_id)
    stats = defaultdict(lambda: {"platite": 0, "neplatite": 0, "total": 0})
    for t in TICKET_DATA.get(cid, []):
        if t.get('deleted'):
            continue
        a = stats[t['author']]
        a["total"] += 1
        a["platite" if t['paid'] else "neplatite"] += 1

    deletions = defaultdict(int)
    for t in TICKET_DATA.get(cid, []):
        if t.get('deleted'):
            name = t.get('deleted_by_name') or "necunoscut"
            deletions[name] += 1

    msg = "📋 **Raport lideri:**\n"
    if not stats:
        msg += "_Nu există date._\n"
    for user, s in stats.items():
        msg += f"\n👤 **{user}**\n✅ Plătite: {s['platite']}\n❌ Neplatite: {s['neplatite']}\n📦 Total: {s['total']}\n"

    msg += "\n🗑️ **Ștergeri (din canal):**\n"
    if deletions:
        for name, cnt in deletions.items():
            msg += f"• {name}: {cnt}\n"
    else:
        msg += "_Nicio ștergere înregistrată._\n"

    await interaction.response.send_message(msg)

@bot.tree.command(name="bifate", description="Afișează câte tickete au fost bifate cu fiecare emoji (excluzând cele șterse)")
@app_commands.check(role_check)
async def bifate(interaction: Interaction):
    cid = str(interaction.channel_id)
    counts = defaultdict(int)
    for t in TICKET_DATA.get(cid, []):
        if t.get('deleted'):
            continue
        # folosim starea ACTUALĂ a reacțiilor (sincronizată la add/remove)
        for em in set(t.get('emojis', []) or []):
            counts[em] += 1

    if not counts:
        await interaction.response.send_message("Nu există tickete bifate în acest canal.", delete_after=120)
        return

    ordered = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    msg = "🔢 **Bife pe emoji (tickete valide):**\n"
    for em, c in ordered:
        msg += f"{em} x {c}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="resync", description="Forțează sincronizarea comenzilor pe acest server")
@app_commands.check(role_check)
async def resync(interaction: Interaction):
    try:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Această comandă trebuie folosită pe server.", ephemeral=True)
            return
        synced = await bot.tree.sync(guild=discord.Object(id=guild.id))
        await interaction.response.send_message(
            f"✅ Resync ok. Comenzi pe **{guild.name}**: " + ", ".join(c.name for c in synced),
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(f"❌ Eroare la resync: {e}", ephemeral=True)

@bot.tree.command(name="help", description="Afișează toate comenzile disponibile")
async def help_command(interaction: Interaction):
    msg = (
        "📘 **Comenzi disponibile:**\n"
        "\n`/ticket <ID>` - Creează un ticket de muncă pentru 3 ore"
        "\n`/control` - Afișează ticketele active din canal (auto-delete în 2 min)"
        "\n`/status` - (Lider/Colider) Afișează câte tickete sunt active/inactive"
        "\n`/today` - Tickete create în ziua curentă (auto-delete în 2 min)"
        "\n`/cauta <ID>` - Caută tickete după ID (auto-delete în 2 min)"
        "\n`/raport` - (Lider/Colider) Raport complet + ștergeri"
        "\n`/bifate` - (Lider/Lider Secundar/Colider) Număr de tickete bifate pe emoji (numai reacții valide)"
        "\n`/resync` - (Lider/Colider) Forțează sincronizarea comenzilor pe server"
    )
    await interaction.response.send_message(msg)

# rulează la 10 minute
@tasks.loop(minutes=10)
async def update_ticket_status():
    for channel_id, tickets in TICKET_DATA.items():
        for ticket in tickets:
            if not ticket['expired'] and not ticket.get('deleted') and get_now() >= parse_time(ticket['end']):
                ticket['expired'] = True
    save_backup()

# Pornire Flask + Bot
threading.Thread(target=run_flask).start()
bot.run(TOKEN)
