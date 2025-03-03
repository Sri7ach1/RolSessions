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
import sqlite3

# ===================================================================
# CONFIGURACIÓN DE LOGGING Y CONSTANTES
# ===================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# Constante para fichero de base de datos
DB_FILE = 'sessions.db'

# Configuración inicial del bot
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents, description="Bot para gestión de sesiones y eventos")

# ===================================================================
# CLASES DE INTERFAZ DE USUARIO (UI)
# ===================================================================
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

class NewSessionAfterEndView(View):
    def __init__(self, previous_session, timeout=None):
        super().__init__(timeout=timeout)
        self.previous_session = previous_session
    
    @discord.ui.button(label="Crear nueva sesión", style=discord.ButtonStyle.primary, emoji="📅", custom_id="new_session_after_end")
    async def new_session_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verificar que solo el creador pueda usar este botón
        if interaction.user.id != self.previous_session['creator_id']:
            await interaction.response.send_message("Solo el creador de la sesión anterior puede usar este botón.", ephemeral=True)
            return
        
        # Abrir modal para crear nueva sesión
        modal = NewSessionModal()
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="cancel_new_session")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.previous_session['creator_id']:
            await interaction.response.send_message("Solo el creador de la sesión anterior puede usar este botón.", ephemeral=True)
            return
        
        await interaction.message.delete()

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
            title=get_text('timeout_error', interaction.guild.id),
            description=get_text('timeout_error', interaction.guild.id),
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
            title=get_text('timeout_error', interaction.guild.id),
            description=get_text('timeout_error', interaction.guild.id),
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
    
    @discord.ui.button(label="Duración", style=discord.ButtonStyle.primary, emoji="⏱️", row=0)
    async def edit_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DurationModal(self.session)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="Grupo", style=discord.ButtonStyle.primary, emoji="👥", row=0)
    async def edit_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(get_text('new_session_group', interaction.guild.id), ephemeral=True)
        role_view = RoleSelectView(interaction.guild, self.session[4])
        role_msg = await interaction.followup.send(view=role_view, wait=True, ephemeral=True)
        
        await role_view.wait()
        if role_view.value:
            session_data = convert_db_to_session(self.session)
            session_data['group'] = role_view.value
            
            if SessionManager.save_session(session_data):
                role = interaction.guild.get_role(int(role_view.value))
                embed = discord.Embed(
                    title=get_text('success_title', interaction.guild.id),
                    description=f"El grupo se ha actualizado a: {role.mention}",
                    color=discord.Color.green()
                )
                await role_msg.edit(embed=embed, view=None)
                await update_session_message(session_data)
            else:
                await role_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
        else:
            await role_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
    @discord.ui.button(label="Canal", style=discord.ButtonStyle.primary, emoji="📢", row=0)
    async def edit_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(get_text('new_session_channel', interaction.guild.id), ephemeral=True)
        channel_view = ChannelSelectView(interaction.guild, self.session[5])
        channel_msg = await interaction.followup.send(view=channel_view, wait=True, ephemeral=True)
        
        await channel_view.wait()
        if channel_view.value:
            session_data = convert_db_to_session(self.session)
            session_data['channel'] = channel_view.value
            
            if SessionManager.save_session(session_data):
                channel = interaction.guild.get_channel(int(channel_view.value))
                embed = discord.Embed(
                    title=get_text('success_title', interaction.guild.id),
                    description=f"El canal se ha actualizado a: {channel.mention}",
                    color=discord.Color.green()
                )
                await channel_msg.edit(embed=embed, view=None)
                await update_session_message(session_data)
            else:
                await channel_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
        else:
            await channel_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
    
    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, emoji="❌", row=1)
    async def cancel_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=get_text('timeout_error', interaction.guild.id),
            description=get_text('timeout_error', interaction.guild.id),
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
                    title=get_text('success_title', interaction.guild.id),
                    description=f"La fecha y hora se han actualizado a: {new_datetime.strftime('%d-%m-%Y %H:%M')}",
                    color=discord.Color.green()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # Actualizar el mensaje de la sesión si existe
                await update_session_message(session_data)
            else:
                await interaction.response.send_message(get_text('error_title', interaction.guild.id), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(get_text('new_session_datetime_error', interaction.guild.id), ephemeral=True)

class DurationModal(Modal, title="Editar Duración"):
    def __init__(self, session):
        super().__init__()
        self.session = session
        
        # Obtener duración actual, por defecto 120 minutos
        current_duration = "120"
        if len(session) > 12 and session[12] is not None:
            current_duration = str(session[12])
            
        self.duration_input = TextInput(
            label="Nueva duración (en minutos)",
            placeholder="Ej: 120 (2 horas)",
            default=current_duration,
            required=True
        )
        self.add_item(self.duration_input)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validar duración
            try:
                new_duration = int(self.duration_input.value)
                if new_duration <= 0:
                    await interaction.response.send_message(get_text('prevtime_error', interaction.guild.id), ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message(get_text('prevtime_error', interaction.guild.id), ephemeral=True)
                return
            
            session_data = convert_db_to_session(self.session)
            session_data['duration'] = new_duration
            
            if SessionManager.save_session(session_data):
                embed = discord.Embed(
                    title=get_text('success_title', interaction.guild.id),
                    description=f"La duración se ha actualizado a: {format_duration(new_duration)}",
                    color=discord.Color.green()
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                
                # Actualizar el mensaje de la sesión si existe
                await update_session_message(session_data)
            else:
                await interaction.response.send_message(get_text('error_title', interaction.guild.id), ephemeral=True)
        except Exception as e:
            logger.error(f"Error al actualizar duración: {str(e)}")
            await interaction.response.send_message(get_text('error_title', interaction.guild.id), ephemeral=True)

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
# Modificar NewSessionModal para usar los nuevos selectores y añadir duración
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
        self.duration_input = TextInput(
            label="Duración en minutos",
            placeholder="Ej: 120 (2 horas)",
            default="120",
            required=True
        )
        self.add_item(self.name_input)
        self.add_item(self.datetime_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # Validar fecha
            try:
                session_datetime = datetime.strptime(self.datetime_input.value, "%d-%m-%Y %H:%M")
            except ValueError:
                await interaction.response.send_message(get_text('new_session_datetime_error', interaction.guild.id), ephemeral=True)
                return
            
            # Validar duración
            try:
                duration = int(self.duration_input.value)
                if duration <= 0:
                    await interaction.response.send_message(get_text('prevtime_error', interaction.guild.id), ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message(get_text('prevtime_error', interaction.guild.id), ephemeral=True)
                return
            # Verificar si es futura
            server_config = SessionManager.load_config(interaction.guild.id)
            time_diff = calculate_time_difference(session_datetime, server_config['timezone'])
            if time_diff <= 0:
                await interaction.response.send_message(get_text('new_session_datetime_error', interaction.guild.id), ephemeral=True)
                return

            # Solicitar rol
            await interaction.response.send_message(get_text('new_session_group', interaction.guild.id), ephemeral=True)
            role_view = RoleSelectView(interaction.guild)
            role_msg = await interaction.followup.send(view=role_view, wait=True, ephemeral=True)
            await role_view.wait()
            
            if not role_view.value:
                await role_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
                return
            
            # Solicitar canal
            channel_view = ChannelSelectView(interaction.guild)
            channel_msg = await interaction.followup.send(get_text('new_session_channel', interaction.guild.id), view=channel_view, ephemeral=True)
            await channel_view.wait()
            
            if not channel_view.value:
                await channel_msg.edit(content=get_text('error_title', interaction.guild.id), view=None)
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
                "duration": duration,
                "status": {
                    "ready": [],
                    "not_ready": []
                }
            }
            if SessionManager.save_session(session_data):
                role = interaction.guild.get_role(int(role_view.value))
                channel = interaction.guild.get_channel(int(channel_view.value))
                
                embed = discord.Embed(
                    title=get_text('success_title', interaction.guild.id),
                    description=f"Se ha creado la sesión **{self.name_input.value}**\n"
                               f"📅 Fecha: {session_datetime.strftime('%d-%m-%Y %H:%M')}\n"
                               f"⏱️ Duración: {format_duration(duration)}\n"
                               f"👥 Grupo: {role.mention}\n"
                               f"📢 Canal: {channel.mention}",
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(get_text('error_title', interaction.guild.id), ephemeral=True)

        except Exception as e:
            logger.error(f"Error en NewSessionModal.on_submit: {str(e)}")
            await interaction.followup.send(get_text('error_title', interaction.guild.id), ephemeral=True)

# ===================================================================
# GESTIÓN DE BASE DE DATOS Y SESIONES
# ===================================================================
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
            
            # Tabla de sesiones con campo de duración
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
                    message_id TEXT,
                    duration INTEGER DEFAULT 120,
                    end_notification_sent INTEGER DEFAULT 0
                )
            ''')
            
            conn.commit()
            conn.close()
        except sqlite3.OperationalError:
            # La columna ya existe, ignorar el error
            pass
        except Exception as e:
            logger.error(f"Error en setup_database: {str(e)}")

    @staticmethod
    def clean_old_sessions():
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            # Calcular la fecha de corte (24 horas atrás)
            cutoff_date = datetime.now() - timedelta(days=1)
            
            # Obtener todas las sesiones
            c.execute('SELECT session_id, datetime FROM sessions')
            sessions = c.fetchall()
            
            # Filtrar y eliminar solo las sesiones que ya ocurrieron
            sessions_to_delete = []
            for session in sessions:
                session_id, session_datetime_str = session
                try:
                    session_datetime = datetime.strptime(session_datetime_str, "%d-%m-%Y %H:%M")
                    # Comparar correctamente las fechas como objetos datetime
                    if session_datetime < cutoff_date:
                        sessions_to_delete.append(session_id)
                except ValueError:
                    # Si hay un error al parsear la fecha, registrarlo
                    logger.error(f"Error al parsear fecha de sesión {session_id}: {session_datetime_str}")
                # Eliminar las sesiones identificadas
            if sessions_to_delete:
                placeholders = ','.join(['?'] * len(sessions_to_delete))
                c.execute(f'DELETE FROM sessions WHERE session_id IN ({placeholders})', sessions_to_delete)
                deleted_count = c.rowcount
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Limpieza automática: {deleted_count} sesiones antiguas eliminadas")
            else:
                logger.debug("Limpieza automática: No hay sesiones antiguas para eliminar")
                
            conn.close()
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
                creator_id, created_at, notified, ready_users, not_ready_users, message_id, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                message_id or session_data.get('message_id'),
                session_data.get('duration', 120)  # Valor por defecto de 120 minutos
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

# ===================================================================
# FUNCIONES AUXILIARES Y UTILIDADES
# ===================================================================
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
   # Obtener la duración con valor predeterminado de 120 minutos si no está definida
   duration = 120
   if len(db_result) > 12 and db_result[12] is not None:
       duration = int(db_result[12])
   
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
       "message_id": db_result[11],
       "duration": duration
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

def format_duration(minutes):
   """Formatea la duración en minutos a un formato legible (horas y minutos)"""
   if minutes < 60:
       return f"{minutes} minutos"
   else:
       hours = minutes // 60
       mins = minutes % 60
       if mins == 0:
           return f"{hours} hora{'s' if hours > 1 else ''}"
       else:
           return f"{hours}h {mins}m"

def create_session_embed(session, guild, time_diff):
   """Crea un embed mejorado para la sesión"""
   role = guild.get_role(int(session['group']))
   role_name = role.name if role else session['group']
   
   # Obtener la duración de la sesión (por defecto 120 minutos)
   duration = session.get('duration', 120)
   
   # Determinar color y estado según el tiempo
   if time_diff <= 0 and time_diff > -duration:
       status_message = f"{get_text('session_in_progress', session['guild_id'])}"
       color = discord.Color.green()
       status_emoji = "🔴 "
   elif time_diff <= -duration:
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
   
   # Añadir información de duración
   embed.add_field(
       name="⏱️ Duración",
       value=format_duration(duration),
       inline=True
   )
   
   embed.add_field(
       name="👥 Grupo",
       value=role_name,
       inline=False
   )
   
   # Participantes
   ready_users = ', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else get_text('active_sessions_none', session['guild_id'])
   not_ready_users = ', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else get_text('active_sessions_none', session['guild_id'])
   
   embed.add_field(
       name=f"✅ {get_text('session_ready', session['guild_id'])}",
       value=ready_users,
       inline=False
   )
   
   embed.add_field(
       name=f"❌ {get_text('session_not_ready', session['guild_id'])}",
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

# ===================================================================
# FUNCIONES DE MANEJO DE SESIONES
# ===================================================================
async def handle_availability(interaction, session_id, status):
   try:
       # Cargar información de la sesión
       conn = sqlite3.connect(DB_FILE)
       c = conn.cursor()
       c.execute('SELECT * FROM sessions WHERE session_id = ?', (session_id,))
       result = c.fetchone()
       conn.close()
       
       if not result:
           await interaction.response.send_message(get_text('active_sessions_none', interaction.guild.id), ephemeral=True)
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
       await interaction.response.send_message(get_text('error_title', interaction.guild.id), ephemeral=True)

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
       
       message_id, channel_id = result if result else (None, None)
       
       # Eliminar la sesión
       if SessionManager.delete_session(session_id):
           embed = discord.Embed(
               title=get_text('success_title', interaction.guild.id),
               description=get_text('purge_sessions_result', interaction.guild.id, 1),
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
               title=get_text('error_title', interaction.guild.id),
               description=get_text('error_title', interaction.guild.id),
               color=discord.Color.red()
           )
           await interaction.response.edit_message(embed=embed, view=None)
   
   except Exception as e:
       logger.error(f"Error en delete_session_confirmed: {str(e)}")
       await interaction.response.send_message(get_text('error_title', interaction.guild.id), ephemeral=True)

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
            
            # Verificar si la sesión acaba de finalizar
            # Consideramos que una sesión acaba de finalizar si el tiempo restante es negativo
            # y además es menor que la duración negativa (es decir, ha pasado la duración completa)
            duration = session_data.get('duration', 120)
            
            # Si la sesión acaba de finalizar (margen de 5 minutos para evitar mensajes repetidos)
            if time_diff <= -duration and time_diff > -(duration + 5):
                # Verificar si ya se envió el mensaje de fin de sesión
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                c.execute('SELECT end_notification_sent FROM sessions WHERE session_id = ?', (session_data['session_id'],))
                result = c.fetchone()
                
                if not result or not result[0]:  # Si no se ha enviado notificación
                    creator = guild.get_member(session_data['creator_id'])
                    if creator:
                        # Crear embed informativo
                        end_embed = discord.Embed(
                            title="🏁 Sesión Finalizada",
                            description=f"La sesión **{session_data['name']}** ha finalizado.\n\n"
                                      f"¿Deseas programar una nueva sesión?",
                            color=discord.Color.blue()
                        )
                        
                        # Creamos vista con botones que solo el creador podrá utilizar
                        end_view = NewSessionAfterEndView(session_data)
                        
                        try:
                            # Buscar el último mensaje de la sesión
                            last_message = await channel.fetch_message(int(message_id))
                            
                            # Enviar mensaje como respuesta al último mensaje de la sesión
                            await last_message.reply(
                                content=f"{creator.mention}",
                                embed=end_embed,
                                view=end_view,
                                allowed_mentions=discord.AllowedMentions(users=[creator])
                            )
                            
                            # Marcar que ya se envió la notificación
                            c.execute('UPDATE sessions SET end_notification_sent = 1 WHERE session_id = ?', (session_data['session_id'],))
                            conn.commit()
                        except Exception as e:
                            logger.error(f"Error enviando notificación de fin de sesión: {str(e)}")
                
                conn.close()
            
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

# ===================================================================
# COMANDOS SLASH (APLICACIÓN)
# ===================================================================
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
       if time_diff <= 0 and time_diff > -session_data['duration']:
           status = "🔴 En curso"
       elif time_diff <= -session_data['duration']:
           status = "⚫ Finalizada"
       elif time_diff <= 15:
           status = "🟠 Inminente"
       else:
           status = "🟡 Programada"
       embed.add_field(
           name=f"{status} | {session_data['name']}",
           value=f"📅 {get_text('active_sessions_date', interaction.guild.id)} {session_data['datetime']}\n"
                 f"⏰ En: {format_time_remaining(time_diff)}\n"
                 f"⏱️ Duración: {format_duration(session_data['duration'])}\n"
                 f"👥 {get_text('active_sessions_group', interaction.guild.id)} {role_name}\n"
                 f"📢 {get_text('active_sessions_channel', interaction.guild.id)} {channel_name}\n"
                 f"✅ {get_text('session_ready', interaction.guild.id)} {len(session_data['status']['ready'])}",
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
       await interaction.response.send_message(get_text('active_sessions_none', interaction.guild.id))
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
       await interaction.response.send_message(get_text('active_sessions_none', interaction.guild.id))
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
           "/newsession": "Crea una nueva sesión. Podrás establecer nombre, fecha, grupo, canal y duración.",
           "/activesessions": "Muestra todas las sesiones activas en el servidor.",
           "/editsession": "Permite modificar una sesión existente (fecha, duración, grupo, canal).",
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

# ===================================================================
# COMANDOS DE CONFIGURACIÓN
# ===================================================================
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
           title=get_text('success_title', interaction.guild.id),
           description=f"{get_text('timezone_success', interaction.guild.id)} {timezone}",
           color=discord.Color.green()
       )
       await interaction.response.send_message(embed=embed)
       
   except pytz.exceptions.UnknownTimeZoneError:
       embed = discord.Embed(
           title=get_text('error_title', interaction.guild.id),
           description=get_text('timezone_error', interaction.guild.id),
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
       title=get_text('success_title', interaction.guild.id),
       description=f"{get_text('lang_success', interaction.guild.id)} {'Español' if language == 'es' else 'English'}",
       color=discord.Color.green()
   )
   await interaction.response.send_message(embed=embed)

# ===================================================================
# TAREAS PROGRAMADAS Y EVENTOS
# ===================================================================
# Tarea programada para gestionar sesiones
# Esta tarea se ejecuta cada minuto y realiza las siguientes funciones:
# 1. Limpia sesiones antiguas automáticamente
# 2. Verifica las sesiones próximas y envía notificaciones
# 3. Actualiza los mensajes de sesiones existentes
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

# Evento que se ejecuta cuando el bot está listo y conectado
# Realiza las siguientes acciones:
# 1. Configura archivos y base de datos
# 2. Recrea los mensajes de sesiones existentes
# 3. Inicia la tarea programada de gestión de sesiones
# 4. Sincroniza los comandos slash con Discord
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