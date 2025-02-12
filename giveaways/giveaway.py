import asyncio
import contextlib
import datetime
import logging
import random
import time as _time
import typing

import discord
from redbot.core import commands
from redbot.core.utils.chat_formatting import humanize_list, humanize_timedelta, pagify
from redbot.core.utils.menus import DEFAULT_CONTROLS, menu, start_adding_reactions
from redbot.core.utils.predicates import ReactionPredicate

from .gset import gsettings
from .models import Giveaway, PendingGiveaway, Requirements, SafeMember
from .util import (
    Coordinate,
    Flags,
    TimeConverter,
    WinnerConverter,
    ask_for_answers,
    channel_conv,
    datetime_conv,
    flags_conv,
    is_gwmanager,
    is_lt,
    prizeconverter,
    requirement_conv,
)

log = logging.getLogger("red.ashcogs.giveaways")


class giveaways(gsettings, name="Giveaways"):
    """
    Host embed and reactions based giveaways in your server
    with advanced requirements, customizable embeds
    and much more."""

    __version__ = "1.6.0"
    __author__ = ["crayyy_zee#2900"]

    def __init__(self, bot):
        super().__init__(bot)

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        if not self.giveaway_cache:
            return
        for i in self.giveaway_cache.copy():
            if i.host.id == user_id:
                self.giveaway_cache.remove(i)

    def format_help_for_context(self, ctx: commands.Context) -> str:
        pre_processed = super().format_help_for_context(ctx) or ""
        n = "\n" if "\n\n" not in pre_processed else ""
        text = [
            f"{pre_processed}{n}",
            f"Cog Version: **{self.__version__}**",
            f"Author: {humanize_list(self.__author__)}",
        ]
        return "\n".join(text)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx):
        if ctx.command.qualified_name.lower() == "giveaway start":
            async with self.config.config.guild(ctx.guild).top_managers() as top:
                top[str(ctx.author.id)] = (
                    1 if not str(ctx.author.id) in top else top[str(ctx.author.id)] + 1
                )

    @commands.group(name="giveaway", aliases=["g"], invoke_without_command=True)
    @commands.guild_only()
    async def giveaway(self, ctx):
        """
        Base command for giveaway controls.

        Use given subcommands to start new giveaways,
        end all (or one) giveaway and reroll ended giveaways.
        """
        await ctx.send_help("giveaway")

    @giveaway.command(name="create")
    @commands.max_concurrency(5, per=commands.BucketType.guild, wait=True)
    @commands.bot_has_permissions(embed_links=True)
    @commands.guild_only()
    @is_gwmanager()
    async def giveaway_create(self, ctx: commands.Context):
        """
        Start an interaction step by step questionnaire to create a new giveaway.
        """

        async def _prize(m):
            return m.content

        await ctx.send(
            "The giveaway creation process will start now. If you ever wanna quit just send a `cancel` to end the process."
        )
        questions = [
            (
                "What is the prize for this giveaway?",
                "The prize can be multi worded and can have emojis.\nThis prize will be the embed title.",
                "prize",
                _prize,
            ),
            (
                "How many winners will there be?",
                "The number of winners must be a number less than 20.",
                "winners",
                is_lt(20),
            ),
            (
                "How long/Until when will the giveaway last?",
                "Either send a duration like `1m 45s` (1 minute 45 seconds)\nor a date/time with your timezone like `30 december 2021 1 pm UTC`.",
                "time",
                datetime_conv(ctx),
            ),
            (
                "Are there any requirements to join this giveaways?",
                "These requirements will be in the format explained in `[p]giveaway explain`.\nIf there are none, just send `None`.",
                "requirements",
                requirement_conv(ctx),
            ),
            (
                "What channel will this giveaway be hosted in?",
                "This can be a channel mention or channel ID.",
                "channel",
                channel_conv(ctx),
            ),
            (
                "Do you want to pass flags to this giveaways?",
                "Send the flags you want to associate to this giveaway. Send `None` if you don't want to use any.",
                "flags",
                flags_conv(ctx),
            ),
        ]

        final = await ask_for_answers(ctx, questions, 45)

        if not final:
            return

        time = final.get("time", 30)
        winners = final.get("winners", 1)
        requirements = final.get("requirements")
        prize = final.get("prize", "A new giveaway").split()
        flags = final.get("flags", {})
        channel = final.get("channel", None)
        if channel:
            flags.update({"channel": channel})
            await ctx.send("Successfully created giveaway in channel `{}`.".format(channel))

        start = ctx.bot.get_command("giveaway start")
        await ctx.invoke(
            start, prize=prize, winners=winners, time=time, requirements=requirements, flags=flags
        )  # Lmao no more handling :p

    @giveaway.command(name="start", usage="[time] <winners> [requirements] <prize> [flags]")
    @commands.max_concurrency(5, per=commands.BucketType.guild, wait=True)
    @commands.bot_has_permissions(embed_links=True)
    @commands.guild_only()
    @is_gwmanager()
    async def _start(
        self,
        ctx: commands.Context,
        time: typing.Optional[TimeConverter] = None,
        winners: WinnerConverter = None,
        requirements: typing.Optional[Requirements] = None,
        prize: commands.Greedy[prizeconverter] = None,
        *,
        flags: Flags = {},
    ):
        """Start a giveaway in the current channel with a prize

        The time argument is optional, you can instead use the `--ends-at` flag to
        specify a more accurate time span.

        Requires a manager role set with `[p]gset manager` or
        The bot mod role set with `[p]set addmodrole`
        or manage messages permissions.

        Example:
            `[p]g start 30s 1 my soul`
            `[p]g start 5m 1 someroleid;;another_role[bypass];;onemore[blacklist] Yayyyy new giveaway`
            `[p]giveaway start 1 this giveaway has no time argument --ends-at 30 december 2021 1 pm UTC --msg but has the `--ends-at` flag`
        """
        if winners is None or not prize:
            return await ctx.send_help("giveaway start")

        if not time and not flags.get("ends_at"):
            return await ctx.send(
                "If you dont pass `<time>` in the command invocation, you must pass the `--ends-at` flag.\nSee `[p]giveaway explain` for more info."
            )

        elif flags.get("ends_at"):
            time = flags.get("ends_at")

        if not requirements:
            requirements = await Requirements.convert(
                ctx, "none"
            )  # requirements weren't provided, they are now null

        prize = " ".join(prize)

        if not getattr(self, "amari", None):  # amari token wasn't available.
            requirements.no_amari_available()  # cancel out amari reqs if they were given.

        if time < 15:
            return await ctx.reply("Giveaways have to be longer than 15 seconds.")

        if await self.config.get_guild_autodel(ctx.guild):
            with contextlib.suppress(Exception):
                await ctx.message.delete()

        messagable = ctx.channel
        if channel := flags.get("channel"):
            messagable = channel

        if start_in := flags.get("starts_in"):
            flags.update({"channel": messagable.id})
            pg = PendingGiveaway(
                ctx.bot,
                self,
                ctx.author.id,
                int(start_in + time),
                winners,
                requirements,
                prize,
                flags,
            )
            self.pending_cache.append(pg)
            return await ctx.send(f"Giveaway for `{pg.prize}` will start in <t:{pg.start}:R>")

        emoji = await self.config.get_guild_emoji(ctx.guild)
        endtime = ctx.message.created_at + datetime.timedelta(seconds=time)

        embed = discord.Embed(
            title=prize.center(len(prize) + 4, "*"),
            description=(
                f"React with {emoji} to enter\n"
                f"Host: {ctx.author.mention}\n"
                f"Ends {f'<t:{int(_time.time()+time)}:R>' if not await self.config.get_guild_timer(ctx.guild) else f'in {humanize_timedelta(seconds=time)}'}\n"
            ),
            timestamp=endtime,
        ).set_footer(text=f"Winners: {winners} | ends : ", icon_url=ctx.guild.icon_url)

        message = await self.config.get_guild_msg(ctx.guild)

        # flag handling below!!

        if donor := flags.get("donor"):
            embed.add_field(name="**Donor:**", value=f"{donor.mention}", inline=False)
        ping = flags.get("ping")
        no_multi = flags.get("no_multi")
        no_defaults = flags.get("no_defaults")
        donor_join = not flags.get("no_donor")
        msg = flags.get("msg")
        thank = flags.get("thank")
        if no_defaults:
            requirements = requirements.no_defaults(True)  # ignore defaults.

        if not no_defaults:
            requirements = requirements.no_defaults()  # defaults will be used!!!

        if not requirements.null:
            embed.add_field(name="Requirements:", value=str(requirements), inline=False)

        gembed = await messagable.send(message, embed=embed)
        await gembed.add_reaction(emoji)

        if ping:
            pingrole = await self.config.get_pingrole(ctx.guild)
            ping = (
                pingrole.mention
                if pingrole
                else f"No pingrole set. Use `{ctx.prefix}gset pingrole` to add a pingrole"
            )

        if msg and ping:
            membed = discord.Embed(
                description=f"***Message***: {msg}", color=discord.Color.random()
            )
            await messagable.send(
                ping, embed=membed, allowed_mentions=discord.AllowedMentions(roles=True)
            )
        elif ping and not msg:
            await messagable.send(ping)
        elif msg and not ping:
            membed = discord.Embed(
                description=f"***Message***: {msg}", color=discord.Color.random()
            )
            await messagable.send(embed=membed)
        if thank:
            tmsg: str = await self.config.get_guild_tmsg(ctx.guild)
            embed = discord.Embed(
                description=tmsg.format_map(
                    Coordinate(
                        donor=SafeMember(donor) if donor else SafeMember(ctx.author),
                        prize=prize,
                    )
                ),
                color=0x303036,
            )
            await messagable.send(embed=embed)

        data = {
            "donor": donor.id if donor else None,
            "donor_can_join": donor_join,
            "use_multi": not no_multi,
            "message": gembed.id,
            "emoji": emoji,
            "channel": channel.id if channel else ctx.channel.id,
            "cog": self,
            "time": _time.time() + time,
            "winners": winners,
            "requirements": requirements,
            "prize": prize,
            "host": ctx.author.id,
            "bot": self.bot,
        }
        giveaway = Giveaway(**data)
        self.giveaway_cache.append(giveaway)

    async def message_reply(self, message: discord.Message) -> discord.Message:
        if not message.reference:
            return

        try:
            return await message.channel.fetch_message(message.reference.message_id)

        except:
            return message.reference.resolved

    async def giveaway_from_message_reply(self, message: discord.Message):
        msg = await self.message_reply(message)

        if msg:
            e = list(
                filter(
                    lambda x: x.message_id == msg.id and x.guild == message.guild,
                    self.giveaway_cache.copy(),
                )
            )
            if not e:
                return
        return msg

    @giveaway.command(name="end")
    @is_gwmanager()
    @commands.guild_only()
    async def end(
        self, ctx: commands.Context, giveaway_id: typing.Union[discord.Message, str] = None
    ):
        """End an ongoing giveaway prematurely.

        This will end the giveaway before its original time.
        You can also reply to the giveaway message instead of passing its id"""
        gmsg = giveaway_id or await self.giveaway_from_message_reply(ctx.message)
        if not gmsg:
            return await ctx.send_help("giveaway end")
        activegaw = self.giveaway_cache.copy()
        if not activegaw:
            return await ctx.send("There are no active giveaways.")

        if await self.config.get_guild_autodel(ctx.guild):
            await ctx.message.delete()

        if isinstance(gmsg, str) and gmsg.lower() == "all":
            msg = await ctx.send(
                "Are you sure you want to end all giveaways in your server? This action is irreversible."
            )
            start_adding_reactions(msg, ReactionPredicate.YES_OR_NO_EMOJIS)
            pred = ReactionPredicate.yes_or_no(msg, ctx.author)
            try:
                await self.bot.wait_for("reaction_add", check=pred, timeout=30)
            except asyncio.TimeoutError:
                return await ctx.send("No thank you for wasting my time :/")

            if pred.result:
                for i in activegaw.copy():
                    if i.guild == ctx.guild:
                        await i.end()
                return await ctx.send("All giveaways have been ended.")

            else:
                return await ctx.send("Thanks for saving me from all that hard work lmao :weary:")

        e = list(filter(lambda x: x.message_id == gmsg.id and x.guild == ctx.guild, activegaw))
        if len(e) == 0:
            return await ctx.send("There is no active giveaway with that ID.")

        else:
            await e[0].end()

    @giveaway.command(name="reroll")
    @is_gwmanager()
    @commands.guild_only()
    async def reroll(self, ctx, giveaway_id: discord.Message = None, winners: WinnerConverter = 1):
        """Reroll the winners of a giveaway

        This requires for the giveaway to already have ended.
        This will select new winners for the giveaway.

        You can also reply to the giveaway message instead of passing its id.

        [winners] is the amount of winners to pick. Defaults to 1"""
        gmsg = giveaway_id or await self.message_reply(ctx.message)
        if not gmsg:
            return await ctx.send_help("giveaway reroll")

        if e := list(
            filter(
                lambda x: x.message_id == gmsg.id and x.guild == ctx.guild,
                self.giveaway_cache.copy(),
            )
        ):
            return await ctx.send(
                "That giveaway is currently active. Can't reroll an already active giveaway."
            )

        if await self.config.get_guild_autodel(ctx.guild):
            await ctx.message.delete()

        entrants = await gmsg.reactions[0].users().flatten()
        try:
            entrants.pop(entrants.index(ctx.guild.me))
        except:
            pass
        entrants = await self.config.get_list_multi(ctx.guild, entrants)
        link = gmsg.jump_url

        if winners == 0:
            return await ctx.reply("You cant have 0 winners for a giveaway 🤦‍♂️")

        if len(entrants) == 0:
            await gmsg.reply(
                f"There weren't enough entrants to determine a winner.\nClick on my replied message to jump to the giveaway."
            )
            return

        winner = {random.choice(entrants).mention for i in range(winners)}

        await gmsg.reply(
            f"Congratulations :tada:{humanize_list(winner)}:tada:. You are the new winners for the giveaway below.\n{link}"
        )

    @giveaway.command(name="clear", hidden=True)
    @commands.is_owner()
    async def clear(self, ctx):
        """
        Clear the giveaway cache in the bot.

        This will abandon all ongoing giveaways and leave them as is"""
        self.giveaway_cache.clear()

        await ctx.send("Cleared all giveaway data.")

    async def active_giveaways(self, ctx, per_guild: bool = False):
        data = self.giveaway_cache.copy()
        failed = ""
        final = ""
        for index, i in enumerate(data, 1):
            channel = i.channel
            if per_guild and i.guild != ctx.guild:
                continue

            msg = await i.get_message()

            if not msg:
                failed += f"\nMessage with id `{i.message_id}` was not found. Removing from cache."
                self.giveaway_cache.remove(i)
                continue

            try:
                final += f"""
    {index}. **[{i.prize}]({msg.jump_url})**
    Hosted by <@{i.host.id}> with {i.winners} winners(s)
    in {f'guild {i.guild} ({i.guild.id})' if not per_guild else f'{channel.mention}'}
    Ends <t:{int(i._time)}:R> ({humanize_timedelta(seconds=i.remaining_time)})
    """
            except Exception as e:
                failed += f"There was an error with the giveaway `{i.message_id}` so it was removed:\n{e}\n"
                self.giveaway_cache.remove(i)
                continue

        return final, failed

    @giveaway.command(name="list")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    @commands.max_concurrency(3, commands.BucketType.default, wait=True)
    @commands.bot_has_permissions(embed_links=True)
    async def glist(self, ctx: commands.Context):
        """
        See a list of active giveaway in your server.

        This is a pretty laggy command and can take a while to show the results so please have patience."""
        data = self.giveaway_cache.copy()
        if not data:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("No active giveaways currently")

        embeds = []
        final, failed = await self.active_giveaways(ctx, per_guild=True)

        for page in pagify(final, page_length=2048):
            embed = discord.Embed(
                title="Currently Active Giveaways!", color=discord.Color.blurple()
            )
            embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon_url)
            embed.description = page
            embeds.append(embed)

        if embeds:
            embeds = [
                embed.set_footer(text=f"Page {embeds.index(embed)+1}/{len(embeds)}")
                for embed in embeds
            ]

            if len(embeds) == 1:
                return await ctx.send(embed=embeds[0])
            else:
                await menu(ctx, embeds, DEFAULT_CONTROLS)

        else:
            await ctx.send("No active giveaways in this server.")

        if failed:
            await ctx.send(failed)
            log.warning(f"{failed}")

    @giveaway.command(name="show")
    @commands.is_owner()
    @commands.bot_has_permissions(embed_links=True)
    async def gshow(self, ctx, giveaway: discord.Message = None):
        """
        Shows all active giveaways in all servers.
        You can also check details for a single giveaway by passing a message id.

        This commands is for owners only."""
        data = self.giveaway_cache.copy()
        if not data:
            return await ctx.send("No active giveaways currently")
        if not giveaway and not await self.giveaway_from_message_reply(ctx.message):

            embeds = []
            final, failed = await self.active_giveaways(ctx)
            for page in pagify(final, page_length=2048):
                embed = discord.Embed(
                    title="Currently Active Giveaways!", color=discord.Color.blurple()
                )
                embed.set_author(name=ctx.guild.name, icon_url=ctx.guild.icon_url)
                embed.description = page
                embeds.append(embed)

            if failed:
                await ctx.send("Failed giveaways have been logged to your console.")
                log.warning(f"{failed}")

            if embeds:
                embeds = [
                    embed.set_footer(text=f"Page {embeds.index(embed)+1}/{len(embeds)}")
                    for embed in embeds
                ]

                if len(embeds) == 1:
                    return await ctx.send(embed=embeds[0])
                else:
                    await menu(ctx, embeds, DEFAULT_CONTROLS)

        else:
            gaw = list(filter(lambda x: x.message_id == giveaway.id, data))
            if not gaw:
                return await ctx.send("not a valid giveaway.")

            else:
                gaw = gaw[0]
                channel = gaw["channel"]
                host = gaw["host"]
                requirements = gaw["requirements"]
                prize = gaw["prize"]
                winners = gaw["winners"]
                endsat = gaw.remaining_time
                endsat = humanize_timedelta(seconds=endsat)
                embed = discord.Embed(title="Giveaway Details: ")
                embed.description = f"""
Giveaway Channel: {channel.mention} (`{channel.name}`)
Host: {host} (<@!{host}>)
Requirements: {requirements}
prize: {prize}
Amount of winners: {winners}
Ends at: {endsat}
				"""
                embed.set_thumbnail(url=channel.guild.icon_url)

                await ctx.send(embed=embed)

    @giveaway.command(name="top")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.bot_has_permissions(embed_links=True)
    async def top_mgrs(self, ctx):
        """
        See the users who have performed the most giveaways in your server.
        """
        async with self.config.config.guild(ctx.guild).top_managers() as top:
            if not top:
                return await ctx.send("No giveaways performed here in this server yet.")

            _sorted = {k: v for k, v in sorted(top.items(), key=lambda i: i[1], reverse=True)}

            embed = discord.Embed(
                title=f"Top giveaway managers in **{ctx.guild.name}**",
                description="\n".join(
                    [f"<@{k}> : {v} giveaway(s) performed." for k, v in _sorted.items()]
                ),
            )
            embed.set_footer(text=ctx.guild.name, icon_url=ctx.guild.icon_url)
            return await ctx.send(embed=embed)

    @giveaway.command(name="explain")
    @commands.cooldown(1, 5, commands.BucketType.guild)
    @commands.bot_has_permissions(embed_links=True)
    async def gexplain(self, ctx):
        """Start a paginated embeds session explaining how
        to use the commands of this cog and how it works."""
        embeds = []
        something = f"""
***__Basics:__ ***
    > You can host giveaways with the bot. What this is,
    > is that the bot sends an embed containing information such as the prize,
    > the amount of winners, the requirements and the time it ends.

    > People have to react to an emoji set by you through the `{ctx.prefix}gset emoji` command (defaults to :tada: )
    > and after the time to end has come for the giveaway to end, the bot will choose winners from the list of
    > people who reacted and send their mentions in the channel and edit the original embedded message.

    > You can also set multipliers for roles which increase the chances of people with that role to win in a giveaway. `{ctx.prefix}gset multi`
    > These multipliers stack and a user's entries in a giveaway add up for each role multiplier they have.

    > The format to add multis is:
        `{ctx.prefix}gset multi add <role id or mention> <multi>`

    > And to remove is the same:
        `{ctx.prefix}gset multi remove <role id or mention>`

    > To see all active role multipliers:
        `{ctx.prefix}gset multi`

***__Requirements:__ ***
    > You can set requirements for the people who wish to join the giveaways.
    > These requirements can be either of role requirements or AmariBot level requirements.
    > Requirements are provided after the time and no. of winners like so:
        *{ctx.prefix}g start <time> <no. of winners> <requirements> <prize> [flags]*

    > The format to specify a requirements is as follows:

    > `argument[requirements_type]`

    > The requirements_type are below with their argument types specified in () brackets:
        • required (role) `(role) means either a role name or id or mention`
        • blacklist (role)
        • bypass (role)
        • amari level (number)
        • amari weekly (number)

    > For the required roles, you dont need to use brackets. You can just type a role and it will work.

    > For example, we want a role `rolename` to be required and a role `anotherrole` to be blacklisted.
    > This is how the requirements string will be constructed:
    > `rolename;;anotherrole[blacklist]`

    > Same way if we want amari level and weekly xp requirements, here is what we would do:
    > `10[alevel];;200[aweekly]` Now the giveaway will require 10 amari elvel **AND** 200 amari weekly xp.

    > Here's another more complicated example:

    >    **{ctx.prefix}g start 1h30m 1 somerolemention[bypass];;123456789[blacklist];;12[alvl] [alevel]**

    ***NOTE***:
        Bypass overrides blacklist, so users with even one bypass role specified
        will be able to join the giveaway regardless of the blacklist.

***__Flags:__ ***
    > Flags are extra arguments passed to the giveaway command to modify it.
    > Flags should be prefixed with `--` (two minus signs?)
    > Flags require you to provide an argument after them unless they are marked as `[argless]`.
    > Then tou som't have to provide anything ans you can just type the flag and get on with it.

    **Types of flags**
    > *--no-multi* [argless]
        This flag will disallow role multipliers to determine the giveaway winner.

    > *--donor*
        This sets a donor for the giveaway. This donor name shows up in the giveaway embed and also is used when using the `--amt` flag

    > *--no-donor* [argless]
        This flag will disallow the donor (if given, else the host) to win the giveaway.

    > *--msg*
        This sends a separate embed after the main giveaway one stating a message give by you.

    > *--ping* [argless]
        This flag pings the set role. ({ctx.prefix}gset pingrole)

    > *--thank* [argless]
        This flag also sends a separate embed with a message thanking the donor. The message can be changed with `{ctx.prefix}gset tmsg`

    > *--no-defaults* [argless]
        This disables the default bypass and blacklist roles set by you with the `{ctx.prefix}gset blacklist` and `{ctx.prefix}gset bypass`

    > *--ends-at*/*--end-in*
        This flag allows you to pass a date/time to end the giveaway at or just a duration. This will override the duration you give in the command invocation.
        You can provide your time zone here for more accurate end times but if you don't, it will default to UTC.

    > *--starts-at*/*--start-in*
        This flag delays the giveaway from starting until your given date/time.
        This is useful if you want to start a giveaway at a specific time but you aren't available.

    > *--channel*/*--chan*
        This redirects the giveaway to the provided channel after the flag.

    **NOTE: The below flags will only work if the DonationLogging cog has been loaded!!**

    > *--amt*
        This adds the given amount to the donor's (or the command author if donor is not provided) donation balance.

    > *--bank* or *--category*
        This flag followed with a category name, uses the given category to to add the amount to.
        If not given, the default category, if set, will be used.
        This flag can not be used without using the *--amt* flag.

***__Customization:__ ***
    > Giveaways can be customized to your liking but under a certain limit.
    > There are a bunch of giveaway settings that you can change.

    > **Auto deletion of giveaway commands**
        You can set whether giveaway command invocations get deleted themselves or not. `{ctx.prefix}gset autodelete true`

    > **Giveaway headers**
        The message above the giveaway can also be changed. `{ctx.prefix}gset msg`

    > **Giveaway emoji**
        The emoji to which people must react to enter a giveaway. This defaults to :tada: but can be changed to anything. `{ctx.prefix}gset emoji`

    > **Giveaway pingrole**
        The role that gets pinged when you use the `--ping` flag. `{ctx.prefix}gset pingrole`

    > **Thank message**
        The message sent when you use the `--thank` flag. `{ctx.prefix}gset tmsg`

    > **Ending message**
        The message sent when the giveaway ends containing the winner mentions. `{ctx.prefix}gset endmsg`

    > **Default blacklist**
        The roles that are by default blacklisted from giveaways. `{ctx.prefix}gset blacklist`

    > **Default bypass**
        The roles that are by default able to bypass requirements in giveaways. `{ctx.prefix}gset bypass`
        """
        pages = list(pagify(something, delims=["\n***"], page_length=2000))
        for page in pages:
            embed = discord.Embed(title="Giveaway Explanation!", description=page, color=0x303036)
            embed.set_footer(text=f"Page {pages.index(page) + 1} out of {len(pages)}")
            embeds.append(embed)

        await menu(ctx, embeds, DEFAULT_CONTROLS)
