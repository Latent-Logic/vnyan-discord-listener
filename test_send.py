import asyncio
import logging

import aiohttp
import discord
import toml
from discord.ext.commands import Bot, CommandError, Context

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s\t%(levelname)-7s\t%(name)s\t%(message)s")
discord_logger = logging.getLogger("discord")
discord_logger.setLevel(logging.INFO)

log = logging.getLogger(__name__)

description = """A bot to translate Discord commands to vnyan websocket commands."""

intents = discord.Intents.default()
intents.message_content = True
intents.bans = False
intents.voice_states = False
intents.typing = False

bot = Bot(command_prefix="?", description=description, intents=intents)


@bot.event
async def on_ready():
    log.debug(f"Logged in as {bot.user.name}, {bot.user.id}")
    log.debug("------")
    for guild in bot.guilds:
        guild: discord.Guild
        log.debug(f"\tGuild {guild.name}, {guild.id} with {guild.member_count} members")
        if str(guild.id) not in SETTINGS["guilds"]:
            log.info(f"\t Not listening in {guild.name} as I don't have a config for it")
            continue
        guild_settings = SETTINGS["guilds"][str(guild.id)]
        for channel_id in guild_settings["channels"]:
            channel = guild.get_channel(channel_id)
            if channel is None:
                raise ValueError(f"Unexpected channel id {channel_id} not in the guild")
            if not isinstance(channel, discord.TextChannel):
                raise ValueError(f"channel {channel} is not a Text Channel!")
        for role_id in guild_settings["roles"]:
            role: discord.Role = guild.get_role(role_id)
            if role is None:
                raise ValueError(f"Unexpected role id {role_id} not in the guild")
    log.debug("\n".join([repr(g) for g in bot.guilds]))


@bot.event
async def on_command_error(ctx: Context, exception: Exception):
    """Run whenever a CommandError is thrown"""
    user_msg = f"{ctx.author.mention}, `{ctx.message.content}` gave error \n> {exception}"
    await ctx.send(user_msg, delete_after=30)
    reason = exception.__cause__
    if not reason and not exception.__suppress_context__:
        reason = exception.__context__
    if reason:
        log.exception(
            f"`{ctx.author}` sent `{ctx.message.content}` which raised error `{exception}`",
            exc_info=reason,
        )
    else:
        log.error(f"`{ctx.author}` sent `{ctx.message.content}` which raised error `{exception}`")


def perm_check(guild: discord.Guild, channel: discord.TextChannel, author: discord.Member):
    if not guild:
        raise CommandError(f"This command has to run in a server context")
    if str(guild.id) not in SETTINGS["guilds"]:
        raise CommandError("No config known for this server")
    guild_settings = SETTINGS["guilds"][str(guild.id)]
    if not set(guild_settings["roles"]) & set(r.id for r in author.roles):
        raise CommandError("This command only for approved roles")
    if channel.id not in guild_settings["channels"]:
        raise CommandError("This command only works in specific channels")


@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot or not message.content.startswith("="):
        return  # Only listening to non-bot users and =commands
    try:
        perm_check(message.guild, message.channel, message.author)
    except CommandError as err:
        log.debug(f"Ignoring {message} because {err}")
        return
    cmd, *rest = message.content[1:].split(maxsplit=1)  # Strip the = from the start of the message
    if cmd not in SETTINGS["commands"]:
        await message.channel.send(f"Unknown command {cmd}, check `?ws_list`", delete_after=30)
        return
    value = SETTINGS["commands"][cmd]
    if isinstance(value, str):  # Help string, nothing more to do here
        pass
    elif isinstance(value, dict):
        cmd = value.get("ws", cmd)  # Allow config to change command sent to web socket
        arg_type = value.get("arg")
        if arg_type == "<int>":
            if not rest:
                await message.channel.send(f"No argument found for {cmd} (should be a number)", delete_after=30)
                return
            try:
                int(rest[0])
            except ValueError:
                await message.channel.send(f"Argument for {cmd} must be an number", delete_after=30)
                return
            cmd = f"{cmd} {rest[0]}"
        elif arg_type == "<str>":
            if not rest:
                await message.channel.send(f"No argument found for {cmd}", delete_after=30)
                return
            cmd = f"{cmd} {rest}"
        else:
            if arg_type is not None:
                ValueError(f"Command {cmd} has unknown arg {arg_type}")
    else:
        raise ValueError(f"Unknown configuration for command {cmd}")
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SETTINGS["bot"]["vnyan_socket"]) as ws:
            await ws.send_str(cmd)
    await message.add_reaction("✅")


@bot.command()
async def ws_list(ctx: Context):
    """List all configured websocket commands"""
    commands = SETTINGS["commands"]
    out_str = "\n".join(
        f"`={k}`: {v}" if isinstance(v, str) else f'`={k} {v.get("arg", "")}`: {v.get("help", "No Help Provided")}'
        for k, v in sorted(commands.items(), key=lambda x: x[0])
    )
    await ctx.send(out_str)


@bot.command()
async def ws_del(ctx: Context, cmd: str):
    """Temporarily remove a =command in the currently running program

    Any changes will need to be manually updated in settings.toml before next run"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    commands = SETTINGS["commands"]
    if cmd not in commands:
        raise CommandError(f"Failed to delete as {cmd} not found in commands")
    del commands[cmd]
    log.info(f"User {ctx.author} has removed ={cmd} from the running instance")
    await ctx.message.add_reaction("✅")


@bot.command()
async def ws_add(ctx: Context, cmd: str, ws_str: str):
    """Temporarily add a =command in the currently running program

    Any changes will need to be manually updated in settings.toml before next run"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    commands = SETTINGS["commands"]
    if cmd in commands:
        raise CommandError(f"Can't add {cmd} as it already exists, use `?ws_del {cmd}` if you want to change it")
    if cmd == ws_str:
        commands[cmd] = f"Temporarily added command `={cmd}` which sends `{ws_str}` to the web socket"
    else:
        commands[cmd] = {
            "ws": ws_str,
            "help": f"Temporarily added command `={cmd}` which sends `{ws_str}` to the web socket",
        }
    log.info(f"User {ctx.author} has added ={cmd} to send {ws_str} to the running instance")
    await ctx.message.add_reaction("✅")


@bot.command()
async def ws_cat(ctx: Context, data: str):
    """Echo message out to the websocket"""
    perm_check(ctx.guild, ctx.channel, ctx.author)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SETTINGS["bot"]["vnyan_socket"]) as ws:
            await ws.send_str(data)
    await ctx.message.add_reaction("✅")


async def main(settings: dict):
    try:
        async with bot:
            await bot.start(settings["bot"]["token"])
    finally:
        log.debug("Shutting down bot")


if __name__ == "__main__":
    SETTINGS = toml.load("settings.toml")

    asyncio.run(main(SETTINGS))
