# bot.py
import json
import os
from datetime import datetime, timedelta
import pytz
from discord.ext import commands, tasks
import discord
from translations import TEXTS
from config import TOKEN, PAYPAL_LINK, DEFAULT_ALERT_TIME, DEFAULT_TIMEZONE

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

# Estructura de directorios y archivos
def setup_files():
   if not os.path.exists('config'):
       os.makedirs('config')
   if not os.path.exists('sessions'):
       os.makedirs('sessions')
   
   if not os.path.exists('config/config.json'):
       default_config = {
           "default": {
               "prevtime": DEFAULT_ALERT_TIME,
               "timezone": DEFAULT_TIMEZONE,
               "lang": "es"
           }
       }
       with open('config/config.json', 'w') as f:
           json.dump(default_config, f, indent=4)

# Funciones auxiliares
def load_config(guild_id):
   try:
       with open('config/config.json', 'r') as f:
           configs = json.load(f)
       return configs.get(str(guild_id), configs['default'])
   except FileNotFoundError:
       return {"prevtime": DEFAULT_ALERT_TIME, "timezone": DEFAULT_TIMEZONE, "lang": "es"}

def save_config(guild_id, config_data):
   try:
       with open('config/config.json', 'r') as f:
           configs = json.load(f)
       configs[str(guild_id)] = config_data
       with open('config/config.json', 'w') as f:
           json.dump(configs, f, indent=4)
   except FileNotFoundError:
       configs = {str(guild_id): config_data}
       with open('config/config.json', 'w') as f:
           json.dump(configs, f, indent=4)

def get_text(key, guild_id, *args):
   config = load_config(guild_id)
   lang = config.get('lang', 'es')
   text = TEXTS[lang][key]
   if args:
       return text.format(*args)
   return text

def save_session(session_data):
   session_id = f"{session_data['guild_id']}_{session_data['name'].lower().replace(' ', '_')}"
   with open(f'sessions/{session_id}.json', 'w') as f:
       json.dump(session_data, f, indent=4)

def load_sessions():
   sessions = []
   for filename in os.listdir('sessions'):
       if filename.endswith('.json'):
           with open(f'sessions/{filename}', 'r') as f:
               sessions.append(json.load(f))
   return sessions

def calculate_time_difference(session_time, guild_timezone):
   """Calcula la diferencia de tiempo entre ahora y la sesión en minutos"""
   try:
       tz = pytz.timezone(guild_timezone)
       current_time = datetime.now(tz)
       session_time = tz.localize(session_time)
       
       time_diff = (session_time - current_time).total_seconds() / 60
       return time_diff
   except pytz.exceptions.UnknownTimeZoneError:
       # Si hay un error con la zona horaria, usar la zona por defecto
       tz = pytz.timezone(DEFAULT_TIMEZONE)
       current_time = datetime.now(tz)
       session_time = tz.localize(session_time)
       
       time_diff = (session_time - current_time).total_seconds() / 60
       return time_diff
# Comandos de configuración
@bot.group(invoke_without_command=True, help="Comandos de configuración del bot")
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

@configure.command(help="Configura la zona horaria del servidor")
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
           config = load_config(ctx.guild.id)
           config['timezone'] = new_timezone
           save_config(ctx.guild.id, config)
           
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
   
   except TimeoutError:
       await ctx.send(get_text('timeout_error', ctx.guild.id))

@configure.command(help="Configura el idioma del bot (es/en)")
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
           config = load_config(ctx.guild.id)
           config['lang'] = language
           save_config(ctx.guild.id, config)

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

   except TimeoutError:
       await ctx.send(get_text('timeout_error', ctx.guild.id))

@bot.command(help="Muestra información sobre donaciones")
async def donate(ctx):
   try:
       await ctx.author.send(get_text('donate_dm', ctx.guild.id, PAYPAL_LINK))
       await ctx.send(get_text('donate_response', ctx.guild.id))
   except discord.Forbidden:
       await ctx.send(get_text('donate_error', ctx.guild.id))
# Comandos de sesiones
@bot.command(help="Crea una nueva sesión con fecha y hora")
async def newSession(ctx):
   def check(m):
       return m.author == ctx.author and m.channel == ctx.channel

   # Nombre de la sesión
   await ctx.send(get_text('new_session_name', ctx.guild.id))
   name_msg = await bot.wait_for('message', check=check)
   
   # Fecha y hora
   await ctx.send(get_text('new_session_datetime', ctx.guild.id))
   datetime_msg = await bot.wait_for('message', check=check)
   
   try:
       session_datetime = datetime.strptime(datetime_msg.content, "%d-%m-%Y %H:%M")
       
       # Verificar si la fecha es futura
       server_config = load_config(ctx.guild.id)
       time_diff = calculate_time_difference(session_datetime, server_config['timezone'])
       if time_diff <= 0:
           await ctx.send("La fecha y hora deben ser futuras.")
           return
           
   except ValueError:
       await ctx.send(get_text('new_session_datetime_error', ctx.guild.id))
       return

   # Grupo
   await ctx.send(get_text('new_session_group', ctx.guild.id))
   group_msg = await bot.wait_for('message', check=check)
   group_id = ''.join(filter(str.isdigit, group_msg.content))
   
   # Canal
   await ctx.send(get_text('new_session_channel', ctx.guild.id))
   channel_msg = await bot.wait_for('message', check=check)
   channel_id = ''.join(filter(str.isdigit, channel_msg.content))

   session_data = {
       "name": name_msg.content,
       "datetime": session_datetime.strftime("%d-%m-%Y %H:%M"),
       "group": group_id,
       "channel": channel_id,
       "creator_id": ctx.author.id,
       "guild_id": ctx.guild.id,  # ID del servidor
       "created_at": datetime.now().strftime("%d-%m-%Y %H:%M"),
       "notified": False,
       "status": {
           "ready": [],
           "not_ready": []
       }
   }

   save_session(session_data)
   await ctx.send(get_text('new_session_success', ctx.guild.id))

@bot.command(help="Muestra todas las sesiones activas")
async def activeSessions(ctx):
   sessions = load_sessions()
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
       # Obtener el rol y el canal por ID
       role = ctx.guild.get_role(int(session['group']))
       channel = ctx.guild.get_channel(int(session['channel']))
       
       # Obtener los nombres (o usar el ID si no se encuentra el objeto)
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

@bot.command(help="Elimina las sesiones más antiguas de 1 día")
async def purgeSessions(ctx):
   purged = 0
   current_time = datetime.now()
   
   for filename in os.listdir('sessions'):
       if filename.endswith('.json'):
           file_path = f'sessions/{filename}'
           with open(file_path, 'r') as f:
               session = json.load(f)
           
           session_time = datetime.strptime(session['created_at'], "%d-%m-%Y %H:%M")
           if (current_time - session_time) > timedelta(days=1):
               os.remove(file_path)
               purged += 1
   
   await ctx.send(get_text('purge_sessions_result', ctx.guild.id, purged))
# Sistema de comprobación de sesiones y envío de avisos
@tasks.loop(minutes=1)
async def check_sessions():
   current_time = datetime.now()
   sessions = load_sessions()
   
   for session in sessions:
       try:
           if session.get('notified', False):
               continue
               
           # Convertir string a datetime
           session_time = datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M")
           
           # Obtener zona horaria del servidor
           server_config = load_config(session['guild_id'])
           server_timezone = server_config.get('timezone', DEFAULT_TIMEZONE)
           
           # Calcular diferencia de tiempo
           time_diff = calculate_time_difference(session_time, server_timezone)
           
           # Si falta una hora o menos y no se ha notificado
           # O si se creó con menos de una hora de antelación y ya pasó 1 minuto desde su creación
           created_time = datetime.strptime(session['created_at'], "%d-%m-%Y %H:%M")
           time_since_creation = (current_time - created_time).total_seconds() / 60
           
           should_notify = (time_diff <= 60 and time_diff > 0) or \
                         (time_diff <= 60 and time_since_creation >= 1 and time_diff > 0)
           
           if should_notify:
               # Obtener el servidor correcto
               guild = bot.get_guild(int(session['guild_id']))
               if guild:
                   channel = guild.get_channel(int(session['channel']))
                   if channel:
                       # Obtener el rol
                       role = guild.get_role(int(session['group']))
                       role_name = role.name if role else session['group']

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
                       
                       # Marcar como notificado
                       session['notified'] = True
                       save_session(session)

       except Exception as e:
           print(f"Error procesando sesión {session.get('name', 'unknown')}: {str(e)}")
           continue

@tasks.loop(minutes=1)
async def update_session_embeds():
   current_time = datetime.now()
   sessions = load_sessions()
   
   for session in sessions:
       try:
           # Obtener el servidor correcto
           guild = bot.get_guild(int(session['guild_id']))
           if guild:
               channel = guild.get_channel(int(session['channel']))
               if channel:
                   # Buscar el mensaje de la sesión
                   async for message in channel.history(limit=100):
                       if message.embeds and message.embeds[0].title.startswith(get_text('session_alert_title', session['guild_id'])):
                           session_name = message.embeds[0].title.split(': ')[1]
                           if session_name == session['name']:
                               # Calcular diferencia de tiempo
                               session_time = datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M")
                               server_config = load_config(session['guild_id'])
                               server_timezone = server_config.get('timezone', DEFAULT_TIMEZONE)
                               time_diff = calculate_time_difference(session_time, server_timezone)
                               
                               # Obtener el rol para el mensaje actualizado
                               role = guild.get_role(int(session['group']))
                               role_name = role.name if role else session['group']
                               
                               embed = discord.Embed(
                                   title=message.embeds[0].title,
                                   description=f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}\n"
                                              f"{get_text('active_sessions_group', message.guild.id)} {role_name}\n\n"
                                              f"{get_text('session_ready', message.guild.id)}\n"
                                              f"{', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else 'Ninguno'}\n\n"
                                              f"{get_text('session_not_ready', message.guild.id)}\n"
                                              f"{', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else 'Ninguno'}",
                                   color=discord.Color.gold()
                               )
                               await message.edit(embed=embed)
                               break
       except Exception as e:
           print(f"Error actualizando sesión {session.get('name', 'unknown')}: {str(e)}")
           continue

@bot.event
async def on_reaction_add(reaction, user):
   if user == bot.user:
       return

   message = reaction.message
   if not message.embeds or not message.embeds[0].title.startswith(get_text('session_alert_title', message.guild.id)):
       return

   session_name = message.embeds[0].title.split(': ')[1]
   session_file = f'sessions/{message.guild.id}_{session_name.lower().replace(" ", "_")}.json'
   
   if not os.path.exists(session_file):
       return

   with open(session_file, 'r') as f:
       session = json.load(f)

   emoji = str(reaction.emoji)
   
   # Si el usuario añade ✅
   if emoji == '✅':
       # Remover de no listos si estaba ahí
       if user.id in session['status']['not_ready']:
           session['status']['not_ready'].remove(user.id)
       # Añadir a listos si no estaba
       if user.id not in session['status']['ready']:
           session['status']['ready'].append(user.id)
       # Remover la reacción ❌ si existe
       for r in message.reactions:
           if str(r.emoji) == '❌':
               await r.remove(user)
   
   # Si el usuario añade ❌
   elif emoji == '❌':
       # Remover de listos si estaba ahí
       if user.id in session['status']['ready']:
           session['status']['ready'].remove(user.id)
       # Añadir a no listos si no estaba
       if user.id not in session['status']['not_ready']:
           session['status']['not_ready'].append(user.id)
       # Remover la reacción ✅ si existe
       for r in message.reactions:
           if str(r.emoji) == '✅':
               await r.remove(user)

   save_session(session)

   # Obtener el rol para el mensaje actualizado
   role = message.guild.get_role(int(session['group']))
   role_name = role.name if role else session['group']

   # Calcular diferencia de tiempo
   session_time = datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M")
   server_config = load_config(session['guild_id'])
   server_timezone = server_config.get('timezone', DEFAULT_TIMEZONE)
   time_diff = calculate_time_difference(session_time, server_timezone)
   
   embed = discord.Embed(
       title=message.embeds[0].title,
       description=f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}\n"
                  f"{get_text('active_sessions_group', message.guild.id)} {role_name}\n\n"
                  f"{get_text('session_ready', message.guild.id)}\n"
                  f"{', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else 'Ninguno'}\n\n"
                  f"{get_text('session_not_ready', message.guild.id)}\n"
                  f"{', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else 'Ninguno'}",
       color=discord.Color.gold()
   )
   await message.edit(embed=embed)

@bot.event
async def on_reaction_remove(reaction, user):
   if user == bot.user:
       return

   message = reaction.message
   if not message.embeds or not message.embeds[0].title.startswith(get_text('session_alert_title', message.guild.id)):
       return

   session_name = message.embeds[0].title.split(': ')[1]
   session_file = f'sessions/{message.guild.id}_{session_name.lower().replace(" ", "_")}.json'
   
   if not os.path.exists(session_file):
       return

   with open(session_file, 'r') as f:
       session = json.load(f)

   emoji = str(reaction.emoji)
   
   # Si el usuario remueve ✅
   if emoji == '✅' and user.id in session['status']['ready']:
       session['status']['ready'].remove(user.id)
   # Si el usuario remueve ❌
   elif emoji == '❌' and user.id in session['status']['not_ready']:
       session['status']['not_ready'].remove(user.id)

   save_session(session)

   # Obtener el rol para el mensaje actualizado
   role = message.guild.get_role(int(session['group']))
   role_name = role.name if role else session['group']

   # Calcular diferencia de tiempo
   session_time = datetime.strptime(session['datetime'], "%d-%m-%Y %H:%M")
   server_config = load_config(session['guild_id'])
   server_timezone = server_config.get('timezone', DEFAULT_TIMEZONE)
   time_diff = calculate_time_difference(session_time, server_timezone)
   
   embed = discord.Embed(
       title=message.embeds[0].title,
       description=f"{get_text('session_alert_in_minutes', session['guild_id'], int(time_diff))}\n"
                  f"{get_text('active_sessions_group', message.guild.id)} {role_name}\n\n"
                  f"{get_text('session_ready', message.guild.id)}\n"
                  f"{', '.join([f'<@{user_id}>' for user_id in session['status']['ready']]) if session['status']['ready'] else 'Ninguno'}\n\n"
                  f"{get_text('session_not_ready', message.guild.id)}\n"
                  f"{', '.join([f'<@{user_id}>' for user_id in session['status']['not_ready']]) if session['status']['not_ready'] else 'Ninguno'}",
       color=discord.Color.gold()
   )
   await message.edit(embed=embed)

@bot.event
async def on_ready():
   print(f'Bot conectado como {bot.user.name}')
   setup_files()
   check_sessions.start()
   update_session_embeds.start()

# Ejecutar el bot
if __name__ == "__main__":
   bot.run(TOKEN)