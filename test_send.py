import asyncio
import logging

import aiohttp
import discord
import toml
from discord.ext import commands
from discord.ext.commands import CommandError, Context

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

bot = commands.Bot(command_prefix="?", description=description, intents=intents)


@bot.event
async def on_ready():
    log.debug(f"Logged in as {bot.user.name}, {bot.user.id}")
    log.debug("------")
    for guild in bot.guilds:
        guild: discord.Guild
        log.debug(f"\tGuild {guild.name}, {guild.id} with {guild.member_count} members")
        if guild.id not in SETTINGS["guilds"]:
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


def perm_check(ctx: Context):
    if not ctx.guild:
        raise CommandError(f"This command has to run in a server context")
    if ctx.guild.id not in SETTINGS["guilds"]:
        raise CommandError("No config known for this server")
    guild_settings = SETTINGS["guilds"][str(ctx.guild.id)]
    if not set(guild_settings["roles"]) & set(r.id for r in ctx.author.roles):
        raise CommandError("This command only for approved roles")
    if ctx.channel.id not in guild_settings["channels"]:
        raise CommandError("This command only works in specific channels")


@bot.command()
async def ws_cat(ctx: Context, data: str):
    """Echo message out to the websocket"""
    perm_check(ctx)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SETTINGS["bot"]["vnyan_socket"]) as ws:
            await ws.send_str(data)
    await ctx.message.add_reaction("✅")


async def main(settings: dict):
    try:
        async with bot:
            await bot.start(settings["bot"]["token"])
    finally:
        log.debug("side_shutdown shutting down sql")


if __name__ == "__main__":
    SETTINGS = toml.load("settings.toml")

    asyncio.run(main(SETTINGS))
