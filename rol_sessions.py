import json
import os
from datetime import datetime, timedelta
import pytz
from discord.ext import commands, tasks
import discord
from translations import TEXTS
from config import TOKEN, PAYPAL_LINK, DEFAULT_ALERT_TIME, DEFAULT_TIMEZONE
import logging
import asyncio
import sqlite3

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# Constantes para rutas
CONFIG_DIR = 'config'
SESSIONS_DIR = 'sessions'
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
DB_FILE = 'sessions.db'

# Configuración inicial del bot
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    description="Bot para gestión de sesiones y eventos",
    help_command=commands.DefaultHelpCommand(
        no_category='Comandos disponibles'
    )
)

class DatabaseManager:
    @staticmethod
    def setup_database():
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            # Tabla de configuración
            c.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    guild_id TEXT PRIMARY KEY,
                    prevtime INTEGER,
                    timezone TEXT,
                    lang TEXT
                )
            ''')
            
            # Tabla de sesiones con nuevo campo message_id
            c.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id TEXT,
                    name TEXT,
                    datetime TEXT,
                    group_id TEXT,
                    channel_id TEXT,
                    creator_id TEXT,
                    created_at TEXT,
                    notified INTEGER,
                    ready_users TEXT,
                    not_ready_users TEXT,
                    message_id TEXT
                )
            ''')
            
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error en setup_database: {str(e)}")

    @staticmethod
    def clean_old_sessions():
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            # Eliminar sesiones más antiguas de 24 horas
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%d-%m-%Y %H:%M")
            c.execute('DELETE FROM sessions WHERE datetime < ?', (yesterday,))
            
            deleted_count = c.rowcount
            conn.commit()
            conn.close()
            
            if deleted_count > 0:
                logger.info(f"Limpieza automática: {deleted_count} sesiones antiguas eliminadas")
                
        except Exception as e:
            logger.error(f"Error en clean_old_sessions: {str(e)}")

    @staticmethod
    async def recreate_session_messages(bot):
        """Recrea los mensajes de sesiones activas al reiniciar el bot"""
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT * FROM sessions WHERE notified = 1')
            sessions = c.fetchall()
            conn.close()

            for session in sessions:
                try:
                    guild = bot.get_guild(int(session[1]))  # guild_id
                    if not guild:
                        continue

                    channel = guild.get_channel(int(session[5]))  # channel_id
                    if not channel:
                        continue

                    # No eliminamos el mensaje anterior, solo lo actualizamos
                    message_id = session[11]  # message_id
                    old_message = None
                    
                    if message_id:
                        try:
                            old_message = await channel.fetch_message(int(message_id))
                            if old_message:
                                # Actualizar el mensaje existente en lugar de crear uno nuevo
                                role = guild.get_role(int(session[4]))  # group_id
                                role_name = role.name if role else session[4]
                                
                                server_config = SessionManager.load_config(guild.id)
                                time_diff = calculate_time_difference(
                                    datetime.strptime(session[3], "%d-%m-%Y %H:%M"),
                                    server_config['timezone']
                                )

                                embed = discord.Embed(
                                    title=f"{get_text('session_alert_title', guild.id)} {session[2]}",
                                    description=f"{get_text('session_alert_in_minutes', guild.id, int(time_diff))}\n"
                                              f"{get_text('active_sessions_group', guild.id)} {role_name}\n\n"
                                              f"{get_text('session_ready', guild.id)}\n"
                                              f"{', '.join([f'<@{x}>' for x in session[9].split(',') if x]) if session[9] else 'Ninguno'}\n\n"
                                              f"{get_text('session_not_ready', guild.id)}\n"
                                              f"{', '.join([f'<@{x}>' for x in session[10].split(',') if x]) if session[10] else 'Ninguno'}",
                                    color=discord.Color.gold()
                                )
                                
                                await old_message.edit(embed=embed)
                                continue
                        except discord.NotFound:
                            pass

                    # Si no se encontró el mensaje, crear uno nuevo
                    session_data = {
                        "name": session[2],
                        "datetime": session[3],
                        "group": session[4],
                        "guild_id": int(session[1]),
                        "channel": session[5],
                        "creator_id": int(session[6]),
                        "created_at": session[7],
                        "notified": bool(session[8]),
                        "status": {
                            "ready": [int(x) for x in session[9].split(',') if x],
                            "not_ready": [int(x) for x in session[10].split(',') if x]
                        }
                    }

                    server_config = SessionManager.load_config(guild.id)
                    time_diff = calculate_time_difference(
                        datetime.strptime(session_data['datetime'], "%d-%m-%Y %H:%M"),
                        server_config['timezone']
                    )

                    if time_diff > 0:
                        new_message = await send_session_notification(session_data, guild, channel, time_diff)
                        if new_message:
                            conn = sqlite3.connect(DB_FILE)
                            c = conn.cursor()
                            c.execute('''
                                UPDATE sessions 
                                SET message_id = ? 
                                WHERE session_id = ?
                            ''', (str(new_message.id), session[0]))
                            conn.commit()
                            conn.close()

                except Exception as e:
                    logger.error(f"Error recreando mensaje para sesión {session[2]}: {str(e)}")

        except Exception as e:
            logger.error(f"Error en recreate_session_messages: {str(e)}")

class SessionManager:
    @staticmethod
    def setup_files():
        try:
            DatabaseManager.setup_database()
        except Exception as e:
            logger.error(f"Error en setup_files: {str(e)}")

    @staticmethod
    def load_config(guild_id):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT * FROM config WHERE guild_id = ?', (str(guild_id),))
            result = c.fetchone()
            conn.close()
            
            if result:
                return {
                    "prevtime": result[1],
                    "timezone": result[2],
                    "lang": result[3]
                }
            return {"prevtime": DEFAULT_ALERT_TIME, "timezone": DEFAULT_TIMEZONE, "lang": "es"}
        except Exception as e:
            logger.error(f"Error cargando configuración: {str(e)}")
            return {"prevtime": DEFAULT_ALERT_TIME, "timezone": DEFAULT_TIMEZONE, "lang": "es"}

    @staticmethod
    def save_config(guild_id, config_data):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO config (guild_id, prevtime, timezone, lang)
                VALUES (?, ?, ?, ?)
            ''', (str(guild_id), config_data['prevtime'], config_data['timezone'], config_data['lang']))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error guardando configuración: {str(e)}")
            return False

    @staticmethod
    def save_session(session_data, message_id=None):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            session_id = f"{session_data['guild_id']}_{session_data['name'].lower().replace(' ', '_')}"
            
            c.execute('''
                INSERT OR REPLACE INTO sessions 
                (session_id, guild_id, name, datetime, group_id, channel_id, 
                creator_id, created_at, notified, ready_users, not_ready_users, message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                session_id,
                str(session_data['guild_id']),
                session_data['name'],
                session_data['datetime'],
                session_data['group'],
                session_data['channel'],
                str(session_data['creator_id']),
                session_data['created_at'],
                1 if session_data.get('notified', False) else 0,
                ','.join(map(str, session_data['status']['ready'])),
                ','.join(map(str, session_data['status']['not_ready'])),
                message_id
            ))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error guardando sesión: {str(e)}")
            return False

    @staticmethod
    def load_sessions():
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT * FROM sessions')
            results = c.fetchall()
            conn.close()
            
            sessions = []
            for result in results:
                sessions.append({
                    "name": result[2],
                    "datetime": result[3],
                    "group": result[4],
                    "channel": result[5],
                    "creator_id": int(result[6]),
                    "guild_id": int(result[1]),
                    "created_at": result[7],
                    "notified": bool(result[8]),
                    "status": {
                        "ready": [int(x) for x in result[9].split(',') if x],
                        "not_ready": [int(x) for x in result[10].split(',') if x]
                    }
                })
            return sessions
        except Exception as e:
            logger.error(f"Error cargando sesiones: {str(e)}")
            return []

    @staticmethod
    def delete_session(guild_id, session_name):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            session_id = f"{guild_id}_{session_name.lower().replace(' ', '_')}"
            c.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
            deleted = c.rowcount > 0
            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.error(f"Error eliminando sesión: {str(e)}")
            return False

# Funciones auxiliares
def get_text(key, guild_id, *args):
    config = SessionManager.load_config(guild_id)
    lang = config.get('lang', 'es')
    text = TEXTS[lang][key]
    if args:
        return text.format(*args)
    return text

def calculate_time_difference(session_time, guild_timezone):
    try:
        tz = pytz.timezone(guild_timezone)
        current_time = datetime.now(tz)
        session_time = tz.localize(session_time)
        
        time_diff = (session_time - current_time).total_seconds() / 60
        return time_diff
    except pytz.exceptions.UnknownTimeZoneError as e:
        logger.error(f"Error de zona horaria: {str(e)}")
        tz = pytz.timezone(DEFAULT_TIMEZONE)
        current_time = datetime.now(tz)
        session_time = tz.localize(session_time)
        return (session_time - current_time).total_seconds() / 60

# Comandos originales
@bot.group(invoke_without_command=True)
async def configure(ctx):
    embed = discord.Embed(
        title=get_text('config_title', ctx.guild.id),
        description=get_text('config_desc', ctx.guild.id),
        color=discord.Color.blue()
    )
    embed.add_field(
        name="!configure timezone", 
        value=get_text('config_timezone_desc', ctx.guild.id), 
        inline=False
    )
    embed.add_field(
        name="!configure lang", 
        value=get_text('config_lang_desc', ctx.guild.id), 
        inline=False
    )
    await ctx.send(embed=embed)

@configure.command()
async def timezone(ctx):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    embed = discord.Embed(
        title=get_text('timezone_title', ctx.guild.id),
        description=f"{get_text('timezone_examples', ctx.guild.id)}\n" +
                   "- Europe/Madrid\n" +
                   "- America/New_York\n" +
                   "- Asia/Tokyo",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)
    await ctx.send(get_text('timezone_input', ctx.guild.id))

    try:
        timezone_msg = await bot.wait_for('message', timeout=60.0, check=check)
        new_timezone = timezone_msg.content

        try:
            pytz.timezone(new_timezone)
            config = SessionManager.load_config(ctx.guild.id)
            config['timezone'] = new_timezone
            SessionManager.save_config(ctx.guild.id, config)
            
            embed = discord.Embed(
                title=get_text('success_title', ctx.guild.id),
                description=f"{get_text('timezone_success', ctx.guild.id)} {new_timezone}",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            
        except pytz.exceptions.UnknownTimeZoneError:
            embed = discord.Embed(
                title=get_text('error_title', ctx.guild.id),
                description=get_text('timezone_error', ctx.guild.id),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
    
    except asyncio.TimeoutError:
        await ctx.send(get_text('timeout_error', ctx.guild.id))

@configure.command()
async def lang(ctx):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    embed = discord.Embed(
        title=get_text('lang_title', ctx.guild.id),
        description=f"{get_text('lang_desc', ctx.guild.id)}\n" +
                   "- es (Español)\n" +
                   "- en (English)\n\n" +
                   get_text('lang_input', ctx.guild.id),
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

    try:
        lang_msg = await bot.wait_for('message', timeout=60.0, check=check)
        language = lang_msg.content.lower()

        if language in ['es', 'en']:
            config = SessionManager.load_config(ctx.guild.id)
            config['lang'] = language
            SessionManager.save_config(ctx.guild.id, config)

            embed = discord.Embed(
                title=get_text('success_title', ctx.guild.id),
                description=f"{get_text('lang_success', ctx.guild.id)} {'Español' if language == 'es' else 'English'}",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(
                title=get_text('error_title', ctx.guild.id),
                description=get_text('lang_error', ctx.guild.id),
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

    except asyncio.TimeoutError:
        await ctx.send(get_text('timeout_error', ctx.guild.id))

@bot.command()
async def donate(ctx):
    try:
        await ctx.author.send(get_text('donate_dm', ctx.guild.id, PAYPAL_LINK))
        await ctx.send(get_text('donate_response', ctx.guild.id))
    except discord.Forbidden:
        await ctx.send(get_text('donate_error', ctx.guild.id))

@bot.command()
async def newSession(ctx):
    def check(m):
        return m.author == ctx.author and m.channel == ctx.channel

    def parse_datetime(date_str):
        """Intenta parsear la fecha en múltiples formatos comunes"""
        formats = [
            "%d-%m-%Y %H:%M",
            "%d/%m/%Y %H:%M"
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None

    # Nombre de la sesión
    while True:
        await ctx.send(get_text('new_session_name', ctx.guild.id))
        try:
            name_msg = await bot.wait_for('message', timeout=60.0, check=check)
            if name_msg.content.strip():  # Verificar que no esté vacío
                break
            await ctx.send("El nombre no puede estar vacío. Intenta nuevamente.")
        except asyncio.TimeoutError:
            await ctx.send(get_text('timeout_error', ctx.guild.id))
            return
    
    # Fecha y hora
    while True:
        embed = discord.Embed(
            title="📅 Fecha y Hora",
            description="Introduce la fecha y hora de la sesión.\n\n"
                       "**Formato requerido:**\n"
                       "DD-MM-YYYY HH:MM\n"
                       "o\n"
                       "DD/MM/YYYY HH:MM\n\n"
                       "**Ejemplos:**\n"
                       "15-02-2024 14:30\n"
                       "15/02/2024 14:30",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)

        try:
            datetime_msg = await bot.wait_for('message', timeout=60.0, check=check)
            session_datetime = parse_datetime(datetime_msg.content)
            
            if session_datetime is None:
                error_embed = discord.Embed(
                    title="❌ Error de Formato",
                    description="El formato debe ser DD-MM-YYYY HH:MM o DD/MM/YYYY HH:MM\n"
                               "Por ejemplo: 15-02-2024 14:30",
                    color=discord.Color.red()
                )
                await ctx.send(embed=error_embed)
                continue
                
            # Verificar si la fecha es futura
            server_config = SessionManager.load_config(ctx.guild.id)
            time_diff = calculate_time_difference(session_datetime, server_config['timezone'])
            if time_diff <= 0:
                error_embed = discord.Embed(
                    title="⚠️ Fecha Inválida",
                    description="La fecha y hora deben ser futuras.",
                    color=discord.Color.orange()
                )
                await ctx.send(embed=error_embed)
                continue
            break
            
        except asyncio.TimeoutError:
            await ctx.send(get_text('timeout_error', ctx.guild.id))
            return

    # Grupo
    while True:
        await ctx.send(get_text('new_session_group', ctx.guild.id))
        try:
            group_msg = await bot.wait_for('message', timeout=60.0, check=check)
            group_id = ''.join(filter(str.isdigit, group_msg.content))
            
            # Verificar que el rol existe
            if not group_id or not ctx.guild.get_role(int(group_id)):
                await ctx.send("Rol no válido o no encontrado. Intenta nuevamente.")
                continue
            break
        except (ValueError, asyncio.TimeoutError):
            await ctx.send(get_text('timeout_error', ctx.guild.id))
            return
    
    # Canal
    while True:
        await ctx.send(get_text('new_session_channel', ctx.guild.id))
        try:
            channel_msg = await bot.wait_for('message', timeout=60.0, check=check)
            logger.info(f"Canal recibido: {channel_msg.content}")
            
            # Primero intentamos obtener el canal por mención
            if channel_msg.channel_mentions:
                channel = channel_msg.channel_mentions[0]
                channel_id = str(channel.id)
                logger.info(f"Canal encontrado por mención: {channel.name} ({channel_id})")
                break
            
            # Si no hay mención, intentamos extraer el ID
            channel_id = ''.join(filter(str.isdigit, channel_msg.content))
            logger.info(f"ID de canal extraído: {channel_id}")
            
            if not channel_id:
                await ctx.send("Por favor, menciona un canal (#canal) o proporciona su ID.")
                continue
            
            # Intentar obtener el canal
            try:
                channel = ctx.guild.get_channel(int(channel_id))
                if channel:
                    logger.info(f"Canal encontrado por ID: {channel.name}")
                    break
                else:
                    await ctx.send("No se encontró el canal. Por favor, menciona un canal válido (#canal).")
                    continue
            except ValueError:
                logger.error(f"Error al convertir ID de canal: {channel_id}")
                await ctx.send("ID de canal no válido. Por favor, menciona un canal (#canal).")
                continue
                
        except asyncio.TimeoutError:
            await ctx.send(get_text('timeout_error', ctx.guild.id))
            return
        except Exception as e:
            logger.error(f"Error inesperado al procesar canal: {str(e)}")
            await ctx.send("Ocurrió un error al procesar el canal. Por favor, intenta nuevamente.")
            continue

    # Confirmación de que se recibió el canal
    await ctx.send(f"✅ Canal seleccionado: <#{channel_id}>")

    session_data = {
        "name": name_msg.content,
        "datetime": session_datetime.strftime("%d-%m-%Y %H:%M"),
        "group": group_id,
        "channel": channel_id,
        "creator_id": ctx.author.id,
        "guild_id": ctx.guild.id,
        "created_at": datetime.now().strftime("%d-%m-%Y %H:%M"),
        "notified": False,
        "status": {
            "ready": [],
            "not_ready": []
        }
    }

    if SessionManager.save_session(session_data):
        embed = discord.Embed(
            title="✅ Sesión Creada",
            description=f"Se ha creado la sesión **{name_msg.content}**\n"
                       f"📅 Fecha: {session_datetime.strftime('%d-%m-%Y %H:%M')}\n"
                       f"👥 Grupo: <@&{group_id}>\n"
                       f"📢 Canal: <#{channel_id}>",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
    else:
        error_embed = discord.Embed(
            title="❌ Error",
            description="No se pudo crear la sesión. Por favor, inténtalo nuevamente.",
            color=discord.Color.red()
        )
        await ctx.send(embed=error_embed)

@bot.command()
async def activeSessions(ctx):
    sessions = SessionManager.load_sessions()
    if not sessions:
        await ctx.send(get_text('active_sessions_none', ctx.guild.id))
        return

    # Filtrar sesiones solo para este servidor
    server_sessions = [session for session in sessions if session.get('guild_id') == ctx.guild.id]

    if not server_sessions:
        await ctx.send(get_text('active_sessions_none', ctx.guild.id))
        return

    embed = discord.Embed(
        title=get_text('active_sessions_title', ctx.guild.id),
        color=discord.Color.blue()
    )
    
    for session in server_sessions:
        role = ctx.guild.get_role(int(session['group']))
        channel = ctx.guild.get_channel(int(session['channel']))
        
        role_name = role.name if role else session['group']
        channel_name = channel.name if channel else session['channel']

        embed.add_field(
            name=session['name'],
            value=f"{get_text('active_sessions_date', ctx.guild.id)} {session['datetime']}\n"
                  f"{get_text('active_sessions_group', ctx.guild.id)} {role_name}\n"
                  f"{get_text('active_sessions_channel', ctx.guild.id)} {channel_name}",
            inline=False
        )
    
    await ctx.send(embed=embed)

# Nuevos comandos
@bot.command(help="Elimina una sesión específica")
async def deleteSession(ctx):
    # Cargar sesiones activas del servidor
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE guild_id = ?', (str(ctx.guild.id),))
    results = c.fetchall()
    conn.close()

    if not results:
        await ctx.send("No hay sesiones activas para eliminar.")
        return

    # Crear embed con las sesiones
    embed = discord.Embed(
        title="🗑️ Eliminar Sesión",
        description="Reacciona con el número correspondiente para eliminar una sesión:\n\n",
        color=discord.Color.red()
    )

    # Emojis numerados del 1 al 9
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    session_map = {}  # Mapeo de emoji -> session_id

    for idx, result in enumerate(results[:9]):  # Limitamos a 9 sesiones
        session_name = result[2]  # El nombre está en el índice 2
        session_datetime = result[3]  # La fecha está en el índice 3
        session_id = result[0]  # El session_id está en el índice 0
        
        role = ctx.guild.get_role(int(result[4]))  # group_id está en el índice 4
        channel = ctx.guild.get_channel(int(result[5]))  # channel_id está en el índice 5
        
        role_name = role.name if role else "Rol no encontrado"
        channel_name = channel.name if channel else "Canal no encontrado"

        embed.add_field(
            name=f"{number_emojis[idx]} {session_name}",
            value=f"📅 Fecha: {session_datetime}\n"
                  f"👥 Grupo: {role_name}\n"
                  f"📢 Canal: {channel_name}",
            inline=False
        )
        session_map[number_emojis[idx]] = session_id

    embed.set_footer(text="❌ Cancelar")
    message = await ctx.send(embed=embed)

    # Añadir reacciones
    for idx in range(len(results[:9])):
        await message.add_reaction(number_emojis[idx])
    await message.add_reaction("❌")

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in [*number_emojis[:len(results)], "❌"]

    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)
        
        if str(reaction.emoji) == "❌":
            embed.description = "Operación cancelada."
            embed.color = discord.Color.blue()
            await message.edit(embed=embed)
            return

        session_id = session_map[str(reaction.emoji)]
        
        # Confirmar eliminación
        confirm_embed = discord.Embed(
            title="🗑️ Confirmar Eliminación",
            description=f"¿Estás seguro de que quieres eliminar esta sesión?\n\n"
                        "✅ - Confirmar eliminación\n"
                        "❌ - Cancelar",
            color=discord.Color.red()
        )
        confirm_msg = await ctx.send(embed=confirm_embed)
        await confirm_msg.add_reaction('✅')
        await confirm_msg.add_reaction('❌')

        def confirm_check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in ['✅', '❌'] and reaction.message == confirm_msg

        try:
            confirm_reaction, confirm_user = await bot.wait_for('reaction_add', timeout=30.0, check=confirm_check)
            
            if str(confirm_reaction.emoji) == '✅':
                c = sqlite3.connect(DB_FILE)
                cursor = c.cursor()
                cursor.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
                c.commit()
                c.close()
                
                confirm_embed.description = "✅ Sesión eliminada correctamente."
                confirm_embed.color = discord.Color.green()
            else:
                confirm_embed.description = "Eliminación cancelada."
                confirm_embed.color = discord.Color.blue()
            
            await confirm_msg.edit(embed=confirm_embed)
            
        except asyncio.TimeoutError:
            confirm_embed.description = "⏰ Tiempo de espera agotado. Operación cancelada."
            confirm_embed.color = discord.Color.grey()
            await confirm_msg.edit(embed=confirm_embed)

    except asyncio.TimeoutError:
        embed.description = "⏰ Tiempo de espera agotado. Operación cancelada."
        embed.color = discord.Color.grey()
        await message.edit(embed=embed)

@bot.command(help="Edita una sesión existente")
async def editSession(ctx):
    # Cargar sesiones activas del servidor
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE guild_id = ?', (str(ctx.guild.id),))
    results = c.fetchall()
    conn.close()

    if not results:
        await ctx.send("No hay sesiones activas para editar.")
        return

    # Crear embed con las sesiones
    embed = discord.Embed(
        title="✏️ Editar Sesión",
        description="Reacciona con el número correspondiente para editar una sesión:\n\n",
        color=discord.Color.blue()
    )

    # Emojis numerados del 1 al 9
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    session_map = {}  # Mapeo de emoji -> session_data

    for idx, result in enumerate(results[:9]):
        session_name = result[2]
        session_datetime = result[3]
        session_id = result[0]
        
        role = ctx.guild.get_role(int(result[4]))
        channel = ctx.guild.get_channel(int(result[5]))
        
        role_name = role.name if role else "Rol no encontrado"
        channel_name = channel.name if channel else "Canal no encontrado"

        embed.add_field(
            name=f"{number_emojis[idx]} {session_name}",
            value=f"📅 Fecha: {session_datetime}\n"
                  f"👥 Grupo: {role_name}\n"
                  f"📢 Canal: {channel_name}",
            inline=False
        )
        session_map[number_emojis[idx]] = result

    embed.set_footer(text="❌ Cancelar")
    message = await ctx.send(embed=embed)

    # Añadir reacciones
    for idx in range(len(results[:9])):
        await message.add_reaction(number_emojis[idx])
    await message.add_reaction("❌")

    def check(reaction, user):
        return user == ctx.author and str(reaction.emoji) in [*number_emojis[:len(results)], "❌"]

    try:
        reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)
        
        if str(reaction.emoji) == "❌":
            embed.description = "Operación cancelada."
            embed.color = discord.Color.blue()
            await message.edit(embed=embed)
            return

        selected_session = session_map[str(reaction.emoji)]
        
        # Mostrar opciones de edición
        options_embed = discord.Embed(
            title="✏️ Opciones de Edición",
            description="Selecciona qué quieres editar:\n\n"
                       "1️⃣ Fecha y hora\n"
                       "2️⃣ Grupo\n"
                       "3️⃣ Canal\n\n"
                       "❌ Cancelar",
            color=discord.Color.blue()
        )
        options_msg = await ctx.send(embed=options_embed)
        
        # Añadir reacciones para las opciones
        await options_msg.add_reaction("1️⃣")
        await options_msg.add_reaction("2️⃣")
        await options_msg.add_reaction("3️⃣")
        await options_msg.add_reaction("❌")

        def options_check(reaction, user):
            return user == ctx.author and str(reaction.emoji) in ["1️⃣", "2️⃣", "3️⃣", "❌"]

        try:
            option_reaction, option_user = await bot.wait_for('reaction_add', timeout=30.0, check=options_check)
            option = str(option_reaction.emoji)

            if option == "❌":
                options_embed.description = "Edición cancelada."
                options_embed.color = discord.Color.blue()
                await options_msg.edit(embed=options_embed)
                return

            session_data = {
                "name": selected_session[2],
                "datetime": selected_session[3],
                "group": selected_session[4],
                "channel": selected_session[5],
                "creator_id": int(selected_session[6]),
                "guild_id": int(selected_session[1]),
                "created_at": selected_session[7],
                "notified": bool(selected_session[8]),
                "status": {
                    "ready": [int(x) for x in selected_session[9].split(',') if x],
                    "not_ready": [int(x) for x in selected_session[10].split(',') if x]
                }
            }

            def msg_check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            # Manejar la edición según la opción seleccionada
            if option == "1️⃣":  # Fecha y hora
                date_embed = discord.Embed(
                    title="📅 Nueva Fecha y Hora",
                    description="Introduce la nueva fecha y hora en formato:\n"
                               "DD-MM-YYYY HH:MM o DD/MM/YYYY HH:MM\n\n"
                               "Ejemplo: 15-02-2024 14:30",
                    color=discord.Color.blue()
                )
                await ctx.send(embed=date_embed)

                try:
                    date_msg = await bot.wait_for('message', timeout=60.0, check=msg_check)
                    new_datetime = datetime.strptime(date_msg.content, "%d-%m-%Y %H:%M")
                    session_data['datetime'] = new_datetime.strftime("%d-%m-%Y %H:%M")
                except ValueError:
                    await ctx.send("❌ Formato de fecha inválido.")
                    return

            elif option == "2️⃣":  # Grupo
                group_embed = discord.Embed(
                    title="👥 Nuevo Grupo",
                    description="Menciona el nuevo rol para la sesión (@rol)",
                    color=discord.Color.blue()
                )
                await ctx.send(embed=group_embed)

                try:
                    group_msg = await bot.wait_for('message', timeout=60.0, check=msg_check)
                    group_id = ''.join(filter(str.isdigit, group_msg.content))
                    if not group_id or not ctx.guild.get_role(int(group_id)):
                        await ctx.send("❌ Rol no válido.")
                        return
                    session_data['group'] = group_id
                except ValueError:
                    await ctx.send("❌ Rol no válido.")
                    return

            elif option == "3️⃣":  # Canal
                channel_embed = discord.Embed(
                    title="📢 Nuevo Canal",
                    description="Menciona el nuevo canal para la sesión (#canal)",
                    color=discord.Color.blue()
                )
                await ctx.send(embed=channel_embed)

                try:
                    channel_msg = await bot.wait_for('message', timeout=60.0, check=msg_check)
                    if channel_msg.channel_mentions:
                        session_data['channel'] = str(channel_msg.channel_mentions[0].id)
                    else:
                        await ctx.send("❌ Canal no válido.")
                        return
                except Exception:
                    await ctx.send("❌ Canal no válido.")
                    return

            # Guardar cambios
            if SessionManager.save_session(session_data):
                success_embed = discord.Embed(
                    title="✅ Sesión Actualizada",
                    description="Los cambios se han guardado correctamente.",
                    color=discord.Color.green()
                )
                await ctx.send(embed=success_embed)

                # Actualizar el mensaje de la sesión si existe
                if selected_session[11]:  # message_id
                    channel = ctx.guild.get_channel(int(session_data['channel']))
                    if channel:
                        server_config = SessionManager.load_config(ctx.guild.id)
                        time_diff = calculate_time_difference(
                            datetime.strptime(session_data['datetime'], "%d-%m-%Y %H:%M"),
                            server_config['timezone']
                        )
                        await update_session_embed(session_data, ctx.guild, channel, time_diff)
            else:
                await ctx.send("❌ Error al guardar los cambios.")

        except asyncio.TimeoutError:
            options_embed.description = "⏰ Tiempo de espera agotado."
            options_embed.color = discord.Color.grey()
            await options_msg.edit(embed=options_embed)

    except asyncio.TimeoutError:
        embed.description = "⏰ Tiempo de espera agotado."
        embed.color = discord.Color.grey()
        await message.edit(embed=embed)

# Tarea combinada para verificar y actualizar sesiones
@tasks.loop(minutes=1)
async def manage_sessions():
    try:
        # Limpiar sesiones antiguas automáticamente
        DatabaseManager.clean_old_sessions()
        
        current_time = datetime.now()
        sessions = SessionManager.load_sessions()
        
        for session in sessions:
            try:
                guild = bot.get_guild(int(session['guild_id']))
                if not guild:
                    continue

                channel = guild.get_channel(int(session['channel']))
                if not channel:
                    continue

                # Obtener zona horaria del servidor
                server_config = SessionManager.load_config(session['guild_id'])
                server_timezone = server_config.get('timezone', DEFAULT_TIMEZONE)
                
                # Verificar tiempo y actualizar
                session_time = datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M")
                time_diff = calculate_time_difference(session_time, server_timezone)
                
                if time_diff <= 60 and not session.get('notified', False):
                    await send_session_notification(session, guild, channel, time_diff)
                elif session.get('notified', False):
                    await update_session_embed(session, guild, channel, time_diff)

            except Exception as e:
                logger.error(f"Error procesando sesión {session.get('name', 'unknown')}: {str(e)}")
                continue

    except Exception as e:
        logger.error(f"Error en manage_sessions: {str(e)}")

# Funciones para notificaciones y actualizaciones
async def send_session_notification(session, guild, channel, time_diff):
    try:
        role = guild.get_role(int(session['group']))
        role_name = role.name if role else session['group']

        # Añadir mensaje de aviso con mención al rol
        if role:
            await channel.send(f"¡Hey {role.mention}! Vuestra sesión de **{session['name']}** comenzará en {int(time_diff)} minutos!")
        
        embed = discord.Embed(
            title=f"{get_text('session_alert_title', session['guild_id'])} {session['name']}",
            description=f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}\n"
                      f"{get_text('active_sessions_group', session['guild_id'])} {role_name}\n\n"
                      f"{get_text('session_ready', session['guild_id'])}\n"
                      f"{', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else 'Ninguno'}\n\n"
                      f"{get_text('session_not_ready', session['guild_id'])}\n"
                      f"{', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else 'Ninguno'}",
            color=discord.Color.gold()
        )
        
        message = await channel.send(embed=embed)
        await message.add_reaction('✅')
        await message.add_reaction('❌')
        
        session['notified'] = True
        SessionManager.save_session(session, str(message.id))
        
        return message  # Devolver el mensaje creado
        
    except Exception as e:
        logger.error(f"Error en send_session_notification: {str(e)}")
        return None

async def update_session_embed(session, guild, channel, time_diff):
    try:
        if not isinstance(session, dict):
            logger.error(f"Sesión inválida: {session}")
            return

        # Buscar el mensaje usando el message_id de la sesión
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        session_id = f"{session['guild_id']}_{session['name'].lower().replace(' ', '_')}"
        c.execute('SELECT message_id FROM sessions WHERE session_id = ?', (session_id,))
        result = c.fetchone()
        conn.close()

        if not result or not result[0]:
            logger.error(f"No se encontró message_id para la sesión {session['name']}")
            return

        try:
            message = await channel.fetch_message(int(result[0]))
            if not message:
                logger.error(f"No se encontró el mensaje para la sesión {session['name']}")
                return

            # Actualizar el embed con la información más reciente
            role = guild.get_role(int(session['group']))
            role_name = role.name if role else session['group']

            ready_users = session['status']['ready']
            not_ready_users = session['status']['not_ready']

            ready_mentions = ['Ninguno'] if not ready_users else [f'<@{uid}>' for uid in ready_users]
            not_ready_mentions = ['Ninguno'] if not not_ready_users else [f'<@{uid}>' for uid in not_ready_users]

            embed = discord.Embed(
                title=f"{get_text('session_alert_title', guild.id)} {session['name']}",
                description=f"{get_text('session_alert_in_minutes', guild.id, int(time_diff))}\n"
                          f"{get_text('active_sessions_group', guild.id)} {role_name}\n\n"
                          f"✅ {get_text('session_ready', guild.id)}\n"
                          f"{' '.join(ready_mentions)}\n\n"
                          f"❌ {get_text('session_not_ready', guild.id)}\n"
                          f"{' '.join(not_ready_mentions)}",
                color=discord.Color.gold()
            )

            # Actualizar el mensaje existente
            await message.edit(embed=embed)
            
        except discord.NotFound:
            logger.error(f"Mensaje no encontrado para la sesión {session['name']}")
        except discord.Forbidden:
            logger.error(f"Sin permisos para editar mensaje de la sesión {session['name']}")
        except Exception as e:
            logger.error(f"Error al actualizar mensaje de sesión {session['name']}: {str(e)}")

    except Exception as e:
        logger.error(f"Error en update_session_embed: {str(e)}")

@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user:
        return

    message = reaction.message
    if not message.embeds:
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            SELECT * FROM sessions 
            WHERE message_id = ? AND notified = 1
        ''', (str(message.id),))
        result = c.fetchone()
        conn.close()

        if not result:
            return

        session = {
            "name": result[2],
            "datetime": result[3],
            "group": result[4],
            "channel": result[5],
            "creator_id": int(result[6]),
            "guild_id": int(result[1]),
            "created_at": result[7],
            "notified": bool(result[8]),
            "status": {
                "ready": [int(x) for x in result[9].split(',') if x],
                "not_ready": [int(x) for x in result[10].split(',') if x]
            }
        }

        emoji = str(reaction.emoji)
        updated = False

        if emoji == '✅':
            if user.id in session['status']['not_ready']:
                session['status']['not_ready'].remove(user.id)
                await reaction.message.remove_reaction('❌', user)
            if user.id not in session['status']['ready']:
                session['status']['ready'].append(user.id)
                updated = True

        elif emoji == '❌':
            if user.id in session['status']['ready']:
                session['status']['ready'].remove(user.id)
                await reaction.message.remove_reaction('✅', user)
            if user.id not in session['status']['not_ready']:
                session['status']['not_ready'].append(user.id)
                updated = True

        if updated:
            # Actualizar la base de datos
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''
                UPDATE sessions 
                SET ready_users = ?, not_ready_users = ? 
                WHERE message_id = ?
            ''', (
                ','.join(map(str, session['status']['ready'])),
                ','.join(map(str, session['status']['not_ready'])),
                str(message.id)
            ))
            conn.commit()
            conn.close()

            # Actualizar el embed
            server_config = SessionManager.load_config(session['guild_id'])
            time_diff = calculate_time_difference(
                datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M"),
                server_config['timezone']
            )
            await update_session_embed(session, message.guild, message.channel, time_diff)

    except Exception as e:
        logger.error(f"Error en on_reaction_add: {str(e)}")
        return

@bot.event
async def on_reaction_remove(reaction, user):
    if user == bot.user:
        return

    message = reaction.message
    if not message.embeds or not message.embeds[0].title.startswith(get_text('session_alert_title', message.guild.id)):
        return

    try:
        # Buscar la sesión por message_id en lugar de por nombre
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM sessions WHERE message_id = ?', (str(message.id),))
        result = c.fetchone()
        conn.close()
        
        if not result:
            return

        # Convertir los resultados de la base de datos en un diccionario de sesión
        session = {
            "name": result[2],
            "datetime": result[3],
            "group": result[4],
            "channel": result[5],
            "creator_id": int(result[6]),
            "guild_id": int(result[1]),
            "created_at": result[7],
            "notified": bool(result[8]),
            "status": {
                "ready": [int(x) for x in result[9].split(',') if x],
                "not_ready": [int(x) for x in result[10].split(',') if x]
            }
        }

        emoji = str(reaction.emoji)
        updated = False
        
        if emoji == '✅' and user.id in session['status']['ready']:
            session['status']['ready'].remove(user.id)
            updated = True
        elif emoji == '❌' and user.id in session['status']['not_ready']:
            session['status']['not_ready'].remove(user.id)
            updated = True

        if updated:
            # Guardar cambios en la base de datos
            SessionManager.save_session(session)
            
            # Actualizar el embed
            server_config = SessionManager.load_config(session['guild_id'])
            time_diff = calculate_time_difference(
                datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M"),
                server_config['timezone']
            )
            await update_session_embed(session, message.guild, message.channel, time_diff)

    except Exception as e:
        logger.error(f"Error en on_reaction_remove: {str(e)}")

@bot.event
async def on_ready():
    logger.info(f'Bot conectado como {bot.user.name}')
    SessionManager.setup_files()
    await DatabaseManager.recreate_session_messages(bot)
    manage_sessions.start()

# Ejecutar el bot
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"Error crítico al iniciar el bot: {str(e)}")
