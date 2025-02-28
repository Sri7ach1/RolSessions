import json
import os
from datetime import datetime, timedelta
import pytz
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View, Select, Modal, TextInput
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
DB_FILE = 'sessions.db'

# Configuración inicial del bot
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, description="Bot para gestión de sesiones y eventos")

# Clases de UI
class ReadyView(View):
    def __init__(self, session_id, timeout=None):
        super().__init__(timeout=timeout)
        self.session_id = session_id
    
    @discord.ui.button(label="Listo", style=discord.ButtonStyle.primary, emoji="✅", custom_id="ready")
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_availability(interaction, self.session_id, "ready")
    
    @discord.ui.button(label="No disponible", style=discord.ButtonStyle.primary, emoji="❌", custom_id="not_ready")
    async def not_ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_availability(interaction, self.session_id, "not_ready")

class SessionSelectView(View):
    def __init__(self, sessions, action_type, timeout=None):
        super().__init__(timeout=timeout)
        self.sessions = sessions
        self.action_type = action_type
        
        # Crear menú desplegable con las sesiones
        options = []
        for idx, session in enumerate(sessions[:25]):  # Limitar a 25 opciones
            options.append(discord.SelectOption(
                label=session[2],  # Nombre de la sesión
                description=f"Fecha: {session[3]}",
                value=str(idx)
            ))
            
        select = Select(
            placeholder="Selecciona una sesión",
            options=options,
            custom_id="session_select"
        )
        select.callback = self.session_selected
        self.add_item(select)
        
        # Botón de cancelar
        cancel_button = Button(label="Cancelar", style=discord.ButtonStyle.secondary, custom_id="cancel")
        cancel_button.callback = self.cancel_action
        self.add_item(cancel_button)
    
    async def session_selected(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        session = self.sessions[idx]
        
        if self.action_type == "delete":
            await show_delete_confirmation(interaction, session)
        elif self.action_type == "edit":
            await show_edit_options(interaction, session)
    
    async def cancel_action(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Operación Cancelada",
            description="Has cancelado la acción.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=None)

class ConfirmView(View):
    def __init__(self, session_id, action_type, timeout=None):
        super().__init__(timeout=timeout)
        self.session_id = session_id
        self.action_type = action_type
    
    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success, emoji="✅", custom_id="confirm")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action_type == "delete":
            await delete_session_confirmed(interaction, self.session_id)
    
    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Operación Cancelada",
            description="Has cancelado la acción.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=None)

class EditOptionsView(View):
    def __init__(self, session, timeout=None):
        super().__init__(timeout=timeout)
        self.session = session
    
    @discord.ui.button(label="Fecha y Hora", style=discord.ButtonStyle.primary, emoji="📅", row=0)
    async def edit_datetime(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DateTimeModal(self.session)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Grupo", style=discord.ButtonStyle.primary, emoji="👥", row=0)
    async def edit_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Selecciona el nuevo rol:", ephemeral=True)
        role_view = RoleSelectView(interaction.guild, self.session[4])
        role_msg = await interaction.followup.send(view=role_view, wait=True, ephemeral=True)
        
        await role_view.wait()
        if role_view.value:
            session_data = convert_db_to_session(self.session)
            session_data['group'] = role_view.value
            
            if SessionManager.save_session(session_data):
                role = interaction.guild.get_role(int(role_view.value))
                embed = discord.Embed(
                    title="✅ Sesión Actualizada",
                    description=f"El grupo se ha actualizado a: {role.mention}",
                    color=discord.Color.green()
                )
                await role_msg.edit(embed=embed, view=None)
                await update_session_message(session_data)
            else:
                await role_msg.edit(content="❌ Error al guardar los cambios.", view=None)
        else:
            await role_msg.edit(content="❌ No se seleccionó ningún rol.", view=None)

    @discord.ui.button(label="Canal", style=discord.ButtonStyle.primary, emoji="📢", row=0)
    async def edit_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Selecciona el nuevo canal:", ephemeral=True)
        channel_view = ChannelSelectView(interaction.guild, self.session[5])
        channel_msg = await interaction.followup.send(view=channel_view, wait=True, ephemeral=True)
        
        await channel_view.wait()
        if channel_view.value:
            session_data = convert_db_to_session(self.session)
            session_data['channel'] = channel_view.value
            
            if SessionManager.save_session(session_data):
                channel = interaction.guild.get_channel(int(channel_view.value))
                embed = discord.Embed(
                    title="✅ Sesión Actualizada",
                    description=f"El canal se ha actualizado a: {channel.mention}",
                    color=discord.Color.green()
                )
                await channel_msg.edit(embed=embed, view=None)
                await update_session_message(session_data)
            else:
                await channel_msg.edit(content="❌ Error al guardar los cambios.", view=None)
        else:
            await channel_msg.edit(content="❌ No se seleccionó ningún canal.", view=None)
    
    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="❌", row=1)
    async def cancel_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Edición Cancelada",
            description="Has cancelado la edición.",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=None)

class DateTimeModal(Modal, title="Editar Fecha y Hora"):
    def __init__(self, session):
        super().__init__()
        self.session = session
        self.datetime_input = TextInput(
            label="Nueva fecha y hora",
            placeholder="DD-MM-YYYY HH:MM",
            default=self.session[3],
            required=True
        )
        self.add_item(self.datetime_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_datetime = datetime.strptime(self.datetime_input.value, "%d-%m-%Y %H:%M")
            session_data = convert_db_to_session(self.session)
            session_data['datetime'] = new_datetime.strftime("%d-%m-%Y %H:%M")
            
            if SessionManager.save_session(session_data):
                embed = discord.Embed(
                    title="✅ Sesión Actualizada",
                    description=f"La fecha y hora se han actualizado a: {new_datetime.strftime('%d-%m-%Y %H:%M')}",
                    color=discord.Color.green()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # Actualizar el mensaje de la sesión si existe
                await update_session_message(session_data)
            else:
                await interaction.response.send_message("❌ Error al guardar los cambios.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Formato de fecha inválido. Usa DD-MM-YYYY HH:MM", ephemeral=True)

class SessionTypeModal(Modal, title="Editar Tipo de Sesión"):
    def __init__(self, session):
        super().__init__()
        self.session = session
        session_type = self.session[12] if len(self.session) > 12 else "default"
        self.type_input = TextInput(
            label="Nuevo tipo de sesión",
            placeholder="default, raid, pvp, event",
            default=session_type,
            required=True
        )
        self.add_item(self.type_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            session_type = self.type_input.value.lower()
            if session_type not in ["default", "raid", "pvp", "event"]:
                session_type = "default"
                
            session_data = convert_db_to_session(self.session)
            session_data['session_type'] = session_type
            
            if SessionManager.save_session(session_data):
                embed = discord.Embed(
                    title="✅ Sesión Actualizada",
                    description=f"El tipo de sesión se ha actualizado a: **{session_type}**",
                    color=discord.Color.green()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # Actualizar el mensaje de la sesión si existe
                await update_session_message(session_data)
            else:
                await interaction.response.send_message("❌ Error al guardar los cambios.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error en SessionTypeModal.on_submit: {str(e)}")
            await interaction.response.send_message("❌ Error al actualizar el tipo de sesión.", ephemeral=True)

class RoleSelectView(View):
    def __init__(self, guild, selected_role=None):
        super().__init__()
        self.value = None
        
        # Crear opciones para el menú desplegable
        options = []
        for role in guild.roles:
            if not role.is_default():  # Excluir el rol @everyone
                options.append(
                    discord.SelectOption(
                        label=role.name,
                        value=str(role.id),
                        default=(selected_role and str(role.id) == selected_role)
                    )
                )
        
        # Dividir en grupos de 25 si hay más roles
        for i in range(0, len(options), 25):
            select = Select(
                placeholder="Selecciona un rol",
                options=options[i:i+25],
                row=i//25
            )
            select.callback = self.select_callback
            self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        self.value = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()

class ChannelSelectView(View):
    def __init__(self, guild, selected_channel=None):
        super().__init__()
        self.value = None
        
        # Crear opciones para el menú desplegable
        options = []
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel):  # Solo canales de texto
                options.append(
                    discord.SelectOption(
                        label=channel.name,
                        value=str(channel.id),
                        default=(selected_channel and str(channel.id) == selected_channel)
                    )
                )
        
        # Dividir en grupos de 25 si hay más canales
        for i in range(0, len(options), 25):
            select = Select(
                placeholder="Selecciona un canal",
                options=options[i:i+25],
                row=i//25
            )
            select.callback = self.select_callback
            self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        self.value = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()

# Modificar NewSessionModal para usar los nuevos selectores
class NewSessionModal(Modal, title="Crear Nueva Sesión"):
    def __init__(self):
        super().__init__()
        self.name_input = TextInput(
            label="Nombre de la sesión",
            placeholder="Ej: Raid semanal",
            required=True
        )
        self.datetime_input = TextInput(
            label="Fecha y hora (DD-MM-YYYY HH:MM)",
            placeholder="Ej: 15-02-2024 14:30",
            required=True
        )
        self.add_item(self.name_input)
        self.add_item(self.datetime_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validar fecha
            try:
                session_datetime = datetime.strptime(self.datetime_input.value, "%d-%m-%Y %H:%M")
            except ValueError:
                await interaction.response.send_message("❌ Formato de fecha inválido. Usa DD-MM-YYYY HH:MM", ephemeral=True)
                return
            
            # Verificar si es futura
            server_config = SessionManager.load_config(interaction.guild.id)
            time_diff = calculate_time_difference(session_datetime, server_config['timezone'])
            if time_diff <= 0:
                await interaction.response.send_message("❌ La fecha y hora deben ser futuras.", ephemeral=True)
                return

            # Solicitar rol
            await interaction.response.send_message("Selecciona el rol para la sesión:", ephemeral=True)
            role_view = RoleSelectView(interaction.guild)
            role_msg = await interaction.followup.send(view=role_view, wait=True, ephemeral=True)
            await role_view.wait()
            
            if not role_view.value:
                await role_msg.edit(content="❌ No se seleccionó ningún rol.", view=None)
                return
            
            # Solicitar canal
            channel_view = ChannelSelectView(interaction.guild)
            channel_msg = await interaction.followup.send("Selecciona el canal para la sesión:", view=channel_view, ephemeral=True)
            await channel_view.wait()
            
            if not channel_view.value:
                await channel_msg.edit(content="❌ No se seleccionó ningún canal.", view=None)
                return

            # Crear sesión
            session_data = {
                "name": self.name_input.value,
                "datetime": session_datetime.strftime("%d-%m-%Y %H:%M"),
                "group": role_view.value,
                "channel": channel_view.value,
                "creator_id": interaction.user.id,
                "guild_id": interaction.guild.id,
                "created_at": datetime.now().strftime("%d-%m-%Y %H:%M"),
                "notified": False,
                "status": {
                    "ready": [],
                    "not_ready": []
                }
            }
            
            if SessionManager.save_session(session_data):
                role = interaction.guild.get_role(int(role_view.value))
                channel = interaction.guild.get_channel(int(channel_view.value))
                
                embed = discord.Embed(
                    title="✅ Sesión Creada",
                    description=f"Se ha creado la sesión **{self.name_input.value}**\n"
                               f"📅 Fecha: {session_datetime.strftime('%d-%m-%Y %H:%M')}\n"
                               f"👥 Grupo: {role.mention}\n"
                               f"📢 Canal: {channel.mention}",
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Error al crear la sesión.", ephemeral=True)

        except Exception as e:
            logger.error(f"Error en NewSessionModal.on_submit: {str(e)}")
            await interaction.followup.send("❌ Ocurrió un error al crear la sesión.", ephemeral=True)

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
            
            # Tabla de sesiones (sin session_type)
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
    async def recreate_session_messages(bot_instance):
        """Recrea los mensajes de sesiones activas al reiniciar el bot"""
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('SELECT * FROM sessions WHERE notified = 1')
            sessions = c.fetchall()
            conn.close()

            for session in sessions:
                try:
                    guild = bot_instance.get_guild(int(session[1]))  # guild_id
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
                                session_data = convert_db_to_session(session)
                                server_config = SessionManager.load_config(guild.id)
                                time_diff = calculate_time_difference(
                                    datetime.strptime(session_data['datetime'], "%d-%m-%Y %H:%M"),
                                    server_config['timezone']
                                )

                                embed = create_session_embed(session_data, guild, time_diff)
                                view = ReadyView(session[0], timeout=None)
                                await old_message.edit(embed=embed, view=view)
                                continue
                        except discord.NotFound:
                            pass

                    # Si no se encontró el mensaje, crear uno nuevo
                    session_data = convert_db_to_session(session)
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
            # Crear directorios si no existen
            os.makedirs(CONFIG_DIR, exist_ok=True)
            os.makedirs(SESSIONS_DIR, exist_ok=True)
            
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
                message_id or session_data.get('message_id')
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
                sessions.append(convert_db_to_session(result))
            return sessions
        except Exception as e:
            logger.error(f"Error cargando sesiones: {str(e)}")
            return []

    @staticmethod
    def delete_session(session_id):
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
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

def convert_db_to_session(db_result):
    """Convierte un resultado de base de datos en un objeto de sesión"""
    return {
        "name": db_result[2],
        "datetime": db_result[3],
        "group": db_result[4],
        "channel": db_result[5],
        "creator_id": int(db_result[6]),
        "guild_id": int(db_result[1]),
        "created_at": db_result[7],
        "notified": bool(db_result[8]),
        "status": {
            "ready": [int(x) for x in db_result[9].split(',') if x],
            "not_ready": [int(x) for x in db_result[10].split(',') if x]
        },
        "session_id": db_result[0],
        "message_id": db_result[11]
    }

def format_time_remaining(minutes):
    """Formatea el tiempo restante en un formato legible"""
    if minutes < 0:
        return "Finalizada"
    elif minutes < 60:
        return f"{int(minutes)} minutos"
    else:
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        return f"{hours}h {mins}m"

def create_session_embed(session, guild, time_diff):
    """Crea un embed mejorado para la sesión"""
    role = guild.get_role(int(session['group']))
    role_name = role.name if role else session['group']
    
    # Determinar color y estado según el tiempo
    if time_diff <= 0 and time_diff > -150:
        status_message = f"{get_text('session_in_progress', session['guild_id'])}"
        color = discord.Color.green()
        status_emoji = "🔴 "
    elif time_diff <= -150:
        status_message = f"{get_text('session_ended', session['guild_id'])}"
        color = discord.Color.red()
        status_emoji = "⚫ "
    elif time_diff <= 15:
        status_message = f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}"
        color = discord.Color.orange()
        status_emoji = "🟠 "
    else:
        status_message = f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}"
        color = discord.Color.gold()
        status_emoji = "🟡 "
    
    # Crear barra de progreso
    progress_bar = ""
    if time_diff > 0:
        progress_bar = create_progress_bar(time_diff, 60)
    
    # Crear embed con diseño mejorado
    embed = discord.Embed(
        title=f"{status_emoji}{session['name']}",
        description=f"**{status_message}**\n\n{progress_bar}",
        color=color
    )
    
    # Detalles de la sesión
    embed.add_field(
        name="📅 Fecha y Hora",
        value=session['datetime'],
        inline=True
    )
    
    embed.add_field(
        name="⏰ Tiempo restante",
        value=format_time_remaining(time_diff),
        inline=True
    )
    
    embed.add_field(
        name="👥 Grupo",
        value=role_name,
        inline=False
    )
    
    # Participantes
    ready_users = ', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else 'Ninguno'
    not_ready_users = ', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else 'Ninguno'
    
    embed.add_field(
        name="✅ Participantes confirmados",
        value=ready_users,
        inline=False
    )
    
    embed.add_field(
        name="❌ No disponibles",
        value=not_ready_users,
        inline=False
    )
    
    # Metadata en footer (solo nombre del creador)
    try:
        creator = guild.get_member(session['creator_id'])
        creator_name = creator.display_name if creator else "Usuario desconocido"
        creator_avatar = creator.display_avatar.url if creator else None
        
        embed.set_footer(
            text=f"Creada por {creator_name}",
            icon_url=creator_avatar
        )
    except:
        embed.set_footer(
            text="Creada por Usuario desconocido"
        )
    
    return embed

def create_progress_bar(minutes_left, total_minutes=60):
    """Crea una barra de progreso visual"""
    if minutes_left <= 0:
        return ""
    
    max_length = 20
    progress = min(1.0, (total_minutes - min(minutes_left, total_minutes)) / total_minutes)
    filled_length = int(max_length * progress)
    
    bar = "🟩" * filled_length + "⬜" * (max_length - filled_length)
    return f"{bar} {int(progress * 100)}%"

# Manejadores para interacciones
async def handle_availability(interaction, session_id, status):
    try:
        # Cargar información de la sesión
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT * FROM sessions WHERE session_id = ?', (session_id,))
        result = c.fetchone()
        conn.close()
        
        if not result:
            await interaction.response.send_message("Esta sesión ya no existe.", ephemeral=True)
            return
        
        session = convert_db_to_session(result)
        user_id = interaction.user.id
        updated = False
        
        if status == "ready":
            if user_id in session['status']['not_ready']:
                session['status']['not_ready'].remove(user_id)
            if user_id not in session['status']['ready']:
                session['status']['ready'].append(user_id)
                updated = True
        elif status == "not_ready":
            if user_id in session['status']['ready']:
                session['status']['ready'].remove(user_id)
            if user_id not in session['status']['not_ready']:
                session['status']['not_ready'].append(user_id)
                updated = True
        
        if updated:
            # Actualizar la base de datos
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('''
                UPDATE sessions 
                SET ready_users = ?, not_ready_users = ? 
                WHERE session_id = ?
            ''', (
                ','.join(map(str, session['status']['ready'])),
                ','.join(map(str, session['status']['not_ready'])),
                session_id
            ))
            conn.commit()
            conn.close()
            
            # Actualizar el embed
            server_config = SessionManager.load_config(session['guild_id'])
            time_diff = calculate_time_difference(
                datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M"),
                server_config['timezone']
            )
            
            embed = create_session_embed(session, interaction.guild, time_diff)
            await interaction.response.edit_message(embed=embed)
            
            # Mensaje de confirmación
            status_text = "disponible" if status == "ready" else "no disponible"
            await interaction.followup.send(f"Has marcado que estás {status_text} para esta sesión.", ephemeral=True)
        else:
            # Ya tenía ese estado
            await interaction.response.defer()
    
    except Exception as e:
        logger.error(f"Error en handle_availability: {str(e)}")
        await interaction.response.send_message("Ocurrió un error al procesar tu respuesta.", ephemeral=True)

async def show_delete_confirmation(interaction, session):
    session_id = session[0]  # session_id está en la primera posición
    
    embed = discord.Embed(
        title="🗑️ Confirmar Eliminación",
        description=f"¿Estás seguro de que quieres eliminar la sesión **{session[2]}**?\n\n"
                   f"Esta acción no se puede deshacer.",
        color=discord.Color.red()
    )
    
    view = ConfirmView(session_id, "delete")
    await interaction.response.edit_message(embed=embed, view=view)

async def delete_session_confirmed(interaction, session_id):
    try:
        # Obtener mensaje_id antes de eliminar
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT message_id, channel_id FROM sessions WHERE session_id = ?', (session_id,))
        result = c.fetchone()
        conn.close()
        
        message_id, channel_id = result if result else (None, None)
        
        # Eliminar la sesión
        if SessionManager.delete_session(session_id):
            embed = discord.Embed(
                title="✅ Sesión Eliminada",
                description="La sesión ha sido eliminada correctamente.",
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            
            # Eliminar mensaje de la sesión si existe
            if message_id and channel_id:
                try:
                    channel = interaction.guild.get_channel(int(channel_id))
                    if channel:
                        message = await channel.fetch_message(int(message_id))
                        if message:
                            await message.delete()
                except:
                    pass
        else:
            embed = discord.Embed(
                title="❌ Error",
                description="No se pudo eliminar la sesión.",
                color=discord.Color.red()
            )
            await interaction.response.edit_message(embed=embed, view=None)
    
    except Exception as e:
        logger.error(f"Error en delete_session_confirmed: {str(e)}")
        await interaction.response.send_message("Ocurrió un error al eliminar la sesión.", ephemeral=True)

async def show_edit_options(interaction, session):
    embed = discord.Embed(
        title="✏️ Editar Sesión",
        description=f"**Sesión:** {session[2]}\n"
                   f"**Fecha:** {session[3]}\n\n"
                   "Selecciona qué quieres editar:",
        color=discord.Color.blue()
    )
    
    view = EditOptionsView(session)
    await interaction.response.edit_message(embed=embed, view=view)

async def update_session_message(session_data):
    """Actualiza el mensaje de una sesión existente"""
    try:
        message_id = session_data.get('message_id')
        if not message_id:
            return
        
        guild = bot.get_guild(int(session_data['guild_id']))
        if not guild:
            return
            
        channel = guild.get_channel(int(session_data['channel']))
        if not channel:
            return
            
        try:
            message = await channel.fetch_message(int(message_id))
            if not message:
                return
                
            server_config = SessionManager.load_config(session_data['guild_id'])
            time_diff = calculate_time_difference(
                datetime.strptime(session_data['datetime'], "%d-%m-%Y %H:%M"),
                server_config['timezone']
            )
            
            embed = create_session_embed(session_data, guild, time_diff)
            view = ReadyView(session_data['session_id'])
            
            await message.edit(embed=embed, view=view)
        except discord.NotFound:
            logger.error(f"Mensaje no encontrado para sesión {session_data['name']}")
        except Exception as e:
            logger.error(f"Error actualizando mensaje: {str(e)}")
    
    except Exception as e:
        logger.error(f"Error en update_session_message: {str(e)}")

# Función para notificaciones
async def send_session_notification(session, guild, channel, time_diff):
    try:
        role = guild.get_role(int(session['group']))
        
        # Añadir mensaje de aviso con mención al rol solo si la sesión aún no ha comenzado
        if time_diff > 0 and role:
            await channel.send(f"¡Hey {role.mention}! Vuestra sesión de **{session['name']}** comenzará en {int(time_diff)} minutos!")
        
        embed = create_session_embed(session, guild, time_diff)
        view = ReadyView(session.get('session_id'), timeout=None)
        message = await channel.send(embed=embed, view=view)
        
        session['notified'] = True
        SessionManager.save_session(session, str(message.id))
        
        return message

    except Exception as e:
        logger.error(f"Error en send_session_notification: {str(e)}")
        return None

# SLASH COMMANDS
@bot.tree.command(name="newsession", description="Crea una nueva sesión")
async def new_session(interaction: discord.Interaction):
    modal = NewSessionModal()
    await interaction.response.send_modal(modal)

@bot.tree.command(name="activesessions", description="Muestra las sesiones activas")
async def active_sessions(interaction: discord.Interaction):
    # Cargar sesiones activas del servidor
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE guild_id = ?', (str(interaction.guild.id),))
    results = c.fetchall()
    conn.close()

    if not results:
        await interaction.response.send_message(get_text('active_sessions_none', interaction.guild.id))
        return

    embed = discord.Embed(
        title=get_text('active_sessions_title', interaction.guild.id),
        description="Lista de todas las sesiones programadas en este servidor:",
        color=discord.Color.blue()
    )
    
    for session in results:
        session_data = convert_db_to_session(session)
        role = interaction.guild.get_role(int(session_data['group']))
        channel = interaction.guild.get_channel(int(session_data['channel']))
        
        role_name = role.name if role else session_data['group']
        channel_name = channel.name if channel else session_data['channel']
        
        server_config = SessionManager.load_config(interaction.guild.id)
        time_diff = calculate_time_difference(
            datetime.strptime(session_data['datetime'], "%d-%m-%Y %H:%M"),
            server_config['timezone']
        )
        
        # Determinar estado
        if time_diff <= 0 and time_diff > -150:
            status = "🔴 En curso"
        elif time_diff <= -150:
            status = "⚫ Finalizada"
        elif time_diff <= 15:
            status = "🟠 Inminente"
        else:
            status = "🟡 Programada"
        
        embed.add_field(
            name=f"{status} | {session_data['name']}",
            value=f"📅 Fecha: {session_data['datetime']}\n"
                  f"⏰ En: {format_time_remaining(time_diff)}\n"
                  f"👥 Grupo: {role_name}\n"
                  f"📢 Canal: {channel_name}\n"
                  f"✅ Confirmados: {len(session_data['status']['ready'])}",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="deletesession", description="Elimina una sesión existente")
async def delete_session(interaction: discord.Interaction):
    # Cargar sesiones activas del servidor
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE guild_id = ?', (str(interaction.guild.id),))
    results = c.fetchall()
    conn.close()

    if not results:
        await interaction.response.send_message("No hay sesiones activas para eliminar.")
        return

    embed = discord.Embed(
        title="🗑️ Eliminar Sesión",
        description="Selecciona la sesión que deseas eliminar:",
        color=discord.Color.red()
    )
    
    view = SessionSelectView(results, "delete")
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="editsession", description="Edita una sesión existente")
async def edit_session(interaction: discord.Interaction):
    # Cargar sesiones activas del servidor
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE guild_id = ?', (str(interaction.guild.id),))
    results = c.fetchall()
    conn.close()

    if not results:
        await interaction.response.send_message("No hay sesiones activas para editar.")
        return

    embed = discord.Embed(
        title="✏️ Editar Sesión",
        description="Selecciona la sesión que deseas modificar:",
        color=discord.Color.blue()
    )
    
    view = SessionSelectView(results, "edit")
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="donate", description="Muestra información para donaciones")
async def donate_cmd(interaction: discord.Interaction):
    try:
        await interaction.user.send(get_text('donate_dm', interaction.guild.id, PAYPAL_LINK))
        await interaction.response.send_message(get_text('donate_response', interaction.guild.id))
    except discord.Forbidden:
        await interaction.response.send_message(get_text('donate_error', interaction.guild.id))

@bot.tree.command(name="help", description="Muestra la ayuda del bot")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Ayuda del Bot",
        description="Lista de comandos disponibles:",
        color=discord.Color.blue()
    )
    
    commands_info = {
        "Gestión de Sesiones": {
            "/newsession": "Crea una nueva sesión. Podrás establecer nombre, fecha, grupo y canal.",
            "/activesessions": "Muestra todas las sesiones activas en el servidor.",
            "/editsession": "Permite modificar una sesión existente (fecha, grupo, canal).",
            "/deletesession": "Elimina una sesión existente."
        },
        "Configuración": {
            "/config timezone": "Configura la zona horaria del servidor (Ej: Europe/Madrid).",
            "/config lang": "Configura el idioma del bot (Español/English)."
        },
        "Otros": {
            "/help": "Muestra este mensaje de ayuda.",
            "/donate": "Muestra información sobre donaciones."
        }
    }
    
    for category, commands in commands_info.items():
        field_text = ""
        for cmd, desc in commands.items():
            field_text += f"**{cmd}**\n{desc}\n\n"
        embed.add_field(
            name=f"📌 {category}",
            value=field_text,
            inline=False
        )
    
    embed.set_footer(text="Tip: Para confirmar asistencia, usa los botones ⭐ y ⛔ en los mensajes de sesión")
    
    await interaction.response.send_message(embed=embed)

# Grupo de configuración
config_group = app_commands.Group(name="config", description="Comandos de configuración del bot")
bot.tree.add_command(config_group)

@config_group.command(name="timezone", description="Configura la zona horaria del servidor")
@app_commands.describe(timezone="Zona horaria (Ej: Europe/Madrid, America/New_York)")
async def config_timezone(interaction: discord.Interaction, timezone: str):
    try:
        pytz.timezone(timezone)
        config = SessionManager.load_config(interaction.guild.id)
        config['timezone'] = timezone
        SessionManager.save_config(interaction.guild.id, config)
        
        embed = discord.Embed(
            title="✅ Configuración Exitosa",
            description=f"Zona horaria configurada a: {timezone}",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)
        
    except pytz.exceptions.UnknownTimeZoneError:
        embed = discord.Embed(
            title="❌ Error",
            description="Zona horaria no válida. Por favor, usa un formato válido como 'Europe/Madrid'.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)

@config_group.command(name="lang", description="Configura el idioma del bot")
@app_commands.describe(language="Idioma (es: Español, en: English)")
@app_commands.choices(language=[
    app_commands.Choice(name="Español", value="es"),
    app_commands.Choice(name="English", value="en")
])
async def config_lang(interaction: discord.Interaction, language: str):
    config = SessionManager.load_config(interaction.guild.id)
    config['lang'] = language
    SessionManager.save_config(interaction.guild.id, config)

    embed = discord.Embed(
        title="✅ Configuración Exitosa",
        description=f"Idioma configurado a: {'Español' if language == 'es' else 'English'}",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

# COMANDOS DE PREFIJO (COMPATIBILIDAD)
@bot.command()
async def newSession(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Ahora puedes usar el nuevo comando slash `/newsession`\n\n¿Quieres crear una sesión ahora?",
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    button = discord.ui.Button(label="Crear nueva sesión", style=discord.ButtonStyle.primary)
    
    async def button_callback(interaction):
        modal = NewSessionModal()
        await interaction.response.send_modal(modal)
    
    button.callback = button_callback
    view.add_item(button)
    
    await ctx.send(embed=embed, view=view)

@bot.command()
async def activeSessions(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para ver todas las sesiones usa el nuevo comando slash `/activesessions`",
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    button = discord.ui.Button(label="Ver sesiones activas", style=discord.ButtonStyle.primary)
    
    async def button_callback(interaction):
        await active_sessions(interaction)
    
    button.callback = button_callback
    view.add_item(button)
    
    await ctx.send(embed=embed, view=view)

@bot.command()
async def deleteSession(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para eliminar sesiones usa el nuevo comando slash `/deletesession`",
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    button = discord.ui.Button(label="Eliminar sesión", style=discord.ButtonStyle.danger)
    
    async def button_callback(interaction):
        await delete_session(interaction)
    
    button.callback = button_callback
    view.add_item(button)
    
    await ctx.send(embed=embed, view=view)

@bot.command()
async def editSession(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para editar sesiones usa el nuevo comando slash `/editsession`",
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    button = discord.ui.Button(label="Editar sesión", style=discord.ButtonStyle.primary)
    
    async def button_callback(interaction):
        await edit_session(interaction)
    
    button.callback = button_callback
    view.add_item(button)
    
    await ctx.send(embed=embed, view=view)

@bot.command()
async def donate(ctx):
    try:
        await ctx.author.send(get_text('donate_dm', ctx.guild.id, PAYPAL_LINK))
        await ctx.send(get_text('donate_response', ctx.guild.id))
    except discord.Forbidden:
        await ctx.send(get_text('donate_error', ctx.guild.id))

@bot.group(invoke_without_command=True)
async def configure(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para la configuración usa los nuevos comandos slash:\n\n`/config timezone` - Configura la zona horaria\n`/config lang` - Configura el idioma",
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    timezone_button = discord.ui.Button(label="Configurar zona horaria", style=discord.ButtonStyle.primary, row=0)
    lang_button = discord.ui.Button(label="Configurar idioma", style=discord.ButtonStyle.primary, row=0)
    
    async def timezone_callback(interaction):
        # Modal de zona horaria
        class TimezoneModal(Modal, title="Configurar Zona Horaria"):
            def __init__(self):
                super().__init__()
                self.timezone_input = TextInput(
                    label="Zona horaria",
                    placeholder="Ej: Europe/Madrid, America/New_York",
                    required=True
                )
                self.add_item(self.timezone_input)
            
            async def on_submit(self, interaction):
                await config_timezone(interaction, self.timezone_input.value)
        
        await interaction.response.send_modal(TimezoneModal())
    
    async def lang_callback(interaction):
        embed = discord.Embed(
            title="Selecciona el idioma",
            description="Elige el idioma para el bot:",
            color=discord.Color.blue()
        )
        
        lang_view = discord.ui.View()
        es_button = discord.ui.Button(label="Español", style=discord.ButtonStyle.primary, row=0)
        en_button = discord.ui.Button(label="English", style=discord.ButtonStyle.primary, row=0)
        
        async def es_callback(interaction):
            await config_lang(interaction, "es")
        
        async def en_callback(interaction):
            await config_lang(interaction, "en")
        
        es_button.callback = es_callback
        en_button.callback = en_callback
        
        lang_view.add_item(es_button)
        lang_view.add_item(en_button)
        
        await interaction.response.send_message(embed=embed, view=lang_view, ephemeral=True)
    
    timezone_button.callback = timezone_callback
    lang_button.callback = lang_callback
    
    view.add_item(timezone_button)
    view.add_item(lang_button)
    
    await ctx.send(embed=embed, view=view)

@configure.command()
async def timezone(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para configurar la zona horaria usa el nuevo comando slash `/config timezone`",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

@configure.command()
async def lang(ctx):
    embed = discord.Embed(
        title="Método Actualizado",
        description="Para configurar el idioma usa el nuevo comando slash `/config lang`",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)

# Tarea programada para gestionar sesiones
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
                    await update_session_message(session)

            except Exception as e:
                logger.error(f"Error procesando sesión {session.get('name', 'unknown')}: {str(e)}")
                continue

    except Exception as e:
        logger.error(f"Error en manage_sessions: {str(e)}")

@bot.event
async def on_ready():
    logger.info(f'Bot conectado como {bot.user.name}')
    logger.info(f'discord.py version: {discord.__version__}')
    
    # Configurar archivos y base de datos
    SessionManager.setup_files()
    
    # Recrear mensajes de sesiones
    await DatabaseManager.recreate_session_messages(bot)
    
    # Iniciar tarea de gestión de sesiones
    manage_sessions.start()
    
    # Sincronizar comandos con Discord
    try:
        logger.info("Sincronizando comandos slash...")
        synced = await bot.tree.sync()
        logger.info(f"Comandos sincronizados correctamente: {len(synced)} comandos")
    except Exception as e:
        logger.error(f"Error sincronizando comandos: {str(e)}")

    logger.info("Bot listo y operativo")

# Ejecutar el bot
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"Error crítico al iniciar el bot: {str(e)}")