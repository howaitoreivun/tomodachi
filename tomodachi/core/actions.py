#  Copyright (c) 2020 — present, howaitoreivun.
#
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
#  Heavily inspired by https://github.com/Rapptz/RoboDanny <

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional, TypedDict, Union
from datetime import datetime

import attr
import ujson

from tomodachi.utils import helpers
from tomodachi.core.enums import ActionType

if TYPE_CHECKING:
    from tomodachi.core.bot import Tomodachi

__all__ = ["Action", "Actions"]


class ReminderExtras(TypedDict):
    content: str


class InfractionExtras(TypedDict):
    target_id: int
    reason: str


def convert_sort(val: Any) -> ActionType:
    if isinstance(val, ActionType):
        return val
    return ActionType(val)


def convert_extra(val: Any) -> dict:
    if isinstance(val, dict):
        return val
    return ujson.loads(val)


@attr.s(slots=True)
class Action:
    author_id = attr.ib(type=int)
    channel_id = attr.ib(type=int)
    message_id = attr.ib(type=int)

    id = attr.ib(type=Optional[int], default=None)
    sort = attr.ib(type=ActionType, converter=convert_sort, default=ActionType.REMINDER)
    created_at = attr.ib(type=datetime, factory=helpers.utcnow)
    trigger_at = attr.ib(type=datetime, factory=helpers.utcnow)
    guild_id = attr.ib(type=Optional[int], default=int)
    extra = attr.ib(type=Union[ReminderExtras, InfractionExtras], converter=convert_extra, default=None)

    @property
    def raw_sort(self):
        return self.sort.name

    @property
    def raw_extra(self):
        return ujson.dumps(self.extra)


class Actions:
    def __init__(self, bot: Tomodachi):
        self.bot = bot
        self.cond = asyncio.Condition()
        self.task = asyncio.create_task(self.dispatcher())
        self.active: Optional[Action] = None

    async def dispatcher(self):
        async with self.cond:
            action = self.active = await self.get_action()

            if not action:
                await self.cond.wait()
                await self.reschedule()

            now = helpers.utcnow()
            if action.trigger_at >= now:
                delta = (action.trigger_at - now).total_seconds()
                await asyncio.sleep(delta)

            await self.trigger_action(action)
            await self.reschedule()

    async def reschedule(self):
        if not self.task.cancelled() or self.task.done():
            self.task.cancel()

        self.task = asyncio.create_task(self.dispatcher())

        async with self.cond:
            self.cond.notify_all()

    async def get_action(self):
        async with self.bot.pool.acquire() as conn:
            query = """SELECT * 
                FROM actions 
                WHERE (CURRENT_TIMESTAMP + '28 days'::interval) > actions.trigger_at
                ORDER BY actions.trigger_at 
                LIMIT 1;"""
            stmt = await conn.prepare(query)
            record = await stmt.fetchrow()

        if not record:
            return None

        return Action(**record)

    async def create_action(self, a: Action):
        now = helpers.utcnow()
        delta = (a.trigger_at - now).total_seconds()

        if delta <= 60:
            asyncio.create_task(self.trigger_short_action(delta, a))
            return a

        async with self.bot.db.pool.acquire() as conn:
            await conn.set_type_codec("jsonb", encoder=ujson.dumps, decoder=ujson.loads, schema="pg_catalog")

            query = """INSERT INTO actions (sort, trigger_at, author_id, guild_id, channel_id, message_id, extra)
                VALUES ($1, $2, $3, $4, $5, $6, $7) 
                RETURNING *;"""
            stmt = await conn.prepare(query)
            record = await stmt.fetchrow(
                a.sort.name,
                a.trigger_at,
                a.author_id,
                a.guild_id,
                a.channel_id,
                a.message_id,
                a.extra,
            )

        a = Action(**record)
        # Once the new action created dispatcher has to be restarted
        # but only if the currently active action happens later than new
        if (self.active and self.active.trigger_at >= a.trigger_at) or self.active is None:
            asyncio.create_task(self.reschedule())

        return a

    async def trigger_action(self, action: Action):
        await self.bot.pool.execute("DELETE FROM actions WHERE id = $1;", action.id)
        self.bot.dispatch("triggered_action", action=action)

    async def trigger_short_action(self, seconds, action: Action):
        await asyncio.sleep(seconds)
        self.bot.dispatch("triggered_action", action=action)
