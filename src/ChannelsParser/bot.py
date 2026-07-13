from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

TELEGRAM_CAPTION_LIMIT = 1024

from ChannelsParser.collector import TelegramChannelCollector
from ChannelsParser.commands import (
    SET_HELP,
    VALID_AGES,
    VALID_CHANNEL_KINDS,
    VALID_SORTS,
    apply_set_command,
    parse_queries,
)
from ChannelsParser.config import AppSettings, ConfigError
from ChannelsParser.formatting import (
    RESULTS_PAGE_SIZE,
    format_compact_results_page,
    format_discovery_stats,
    format_filter_presets,
    format_filters,
    format_report,
    format_reports,
    format_scan_done,
    format_scan_history,
    reports_to_csv,
    source_label_from_scan,
)
from ChannelsParser.models import (
    DiscoveryOptions,
    FilterPreset,
    SearchFilters,
    discovery_filters,
)
from ChannelsParser.presets import QUERY_PRESETS, get_preset
from ChannelsParser.proxy import aiogram_proxy
from ChannelsParser.scoring import matches_filters
from ChannelsParser.storage import ChannelStorage


@dataclass
class DiscoverWizard:
    identifier: str | None = None
    post_limit: int = 200
    min_subscribers: int | None = None
    max_subscribers: int | None = None
    include_comment_links: bool = True
    include_profile_refs: bool = True
    include_gifts: bool = True


class BotState:
    def __init__(self, storage: ChannelStorage) -> None:
        self._storage = storage
        self.scan_lock = asyncio.Lock()
        self._scan_cancel_requested = asyncio.Event()
        self._scan_finish_collection_requested = asyncio.Event()
        self._pending_input: dict[int, str] = {}
        self._discover_wizards: dict[int, DiscoverWizard] = {}

    def filters(self, user_id: int) -> SearchFilters:
        return self._storage.get_user_filters(user_id)

    def update_filters(self, user_id: int, filters: SearchFilters) -> SearchFilters:
        self._storage.save_user_filters(user_id, filters)
        return filters

    def reset_filters(self, user_id: int) -> SearchFilters:
        return self._storage.reset_user_filters(user_id)

    def save_filter_preset(self, user_id: int, title: str) -> FilterPreset:
        return self._storage.save_filter_preset(user_id, title, self.filters(user_id))

    def filter_presets(self, user_id: int) -> list[FilterPreset]:
        return self._storage.list_filter_presets(user_id)

    def apply_filter_preset(self, user_id: int, preset_id: int) -> FilterPreset | None:
        preset = self._storage.get_filter_preset(user_id, preset_id)
        if preset is None:
            return None
        self.update_filters(user_id, preset.filters)
        return preset

    def delete_filter_preset(self, user_id: int, preset_id: int) -> bool:
        return self._storage.delete_filter_preset(user_id, preset_id)

    def request_input(self, user_id: int, kind: str) -> None:
        self._pending_input[user_id] = kind

    def pending_input(self, user_id: int) -> str | None:
        return self._pending_input.get(user_id)

    def clear_pending_input(self, user_id: int) -> None:
        self._pending_input.pop(user_id, None)

    def request_filter_preset_title(self, user_id: int) -> None:
        self.request_input(user_id, "filter_preset_title")

    def is_waiting_for_filter_preset_title(self, user_id: int) -> bool:
        return self.pending_input(user_id) == "filter_preset_title"

    def clear_filter_preset_title_request(self, user_id: int) -> None:
        if self.is_waiting_for_filter_preset_title(user_id):
            self.clear_pending_input(user_id)

    def reset_scan_cancel(self) -> None:
        self._scan_cancel_requested.clear()
        self._scan_finish_collection_requested.clear()

    def finish_scan_collection(self) -> None:
        """Soft stop: end post/comment browsing, still inspect collected candidates."""
        self._scan_finish_collection_requested.set()

    def cancel_scan(self) -> None:
        """Hard stop: abort browsing and candidate inspection ASAP."""
        self._scan_cancel_requested.set()
        self._scan_finish_collection_requested.set()

    def scan_cancelled(self) -> bool:
        return self._scan_cancel_requested.is_set()

    def scan_finish_collection_requested(self) -> bool:
        return self._scan_finish_collection_requested.is_set()

    def start_discover_wizard(self, user_id: int) -> DiscoverWizard:
        wizard = DiscoverWizard()
        self._discover_wizards[user_id] = wizard
        return wizard

    def discover_wizard(self, user_id: int) -> DiscoverWizard | None:
        return self._discover_wizards.get(user_id)

    def clear_discover_wizard(self, user_id: int) -> None:
        self._discover_wizards.pop(user_id, None)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(run_bot())
    except (ConfigError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


async def run_bot() -> None:
    settings = AppSettings.from_env(require_bot_token=True)
    storage = ChannelStorage(settings.database_path)
    storage.init()

    collector = TelegramChannelCollector(settings)
    await collector.connect()

    bot: Bot | None = None
    try:
        bot = Bot(
            settings.bot_token or "",
            session=AiohttpSession(proxy=aiogram_proxy(settings.bot_proxy_url)),
        )
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(collector, storage, settings))
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        await collector.close()
        if bot is not None:
            await bot.session.close()


def build_router(
    collector: TelegramChannelCollector,
    storage: ChannelStorage,
    settings: AppSettings,
) -> Router:
    router = Router()
    state = BotState(storage)
    access = AccessControl(settings, storage)
    router.message.middleware(AccessMiddleware(access))
    router.callback_query.middleware(AccessMiddleware(access))

    @router.message(Command("allow"))
    async def allow_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        if not access.is_admin(message.from_user.id):
            await send_branded(message, format_access_denied(message.from_user.id))
            return
        try:
            target_ids, days = parse_allow_args(command.args or "")
        except ValueError as exc:
            await send_branded(message, 
                "Формат:\n"
                "/allow 123456789\n"
                "/allow 123456789 30\n"
                "/allow 111 222 7\n\n"
                f"{exc}"
            )
            return
        term = "бессрочно" if days is None else f"на {days} дн."
        lines = [f"✅ Доступ ({term}):"]
        for target_id in target_ids:
            if access.is_admin(target_id):
                lines.append(f"• {target_id} — уже админ (доступ всегда)")
                continue
            try:
                status = storage.grant_access(
                    target_id, granted_by=message.from_user.id, days=days
                )
            except ValueError as exc:
                lines.append(f"• {target_id} — {exc}")
                continue
            if status == "created":
                lines.append(f"• {target_id} — добавлен, {term}")
            else:
                lines.append(f"• {target_id} — обновлён, {term}")
        await send_branded(message, "\n".join(lines))

    @router.message(Command("disallow"))
    async def disallow_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        if not access.is_admin(message.from_user.id):
            await send_branded(message, format_access_denied(message.from_user.id))
            return
        try:
            target_ids = _parse_user_ids(command.args or "")
        except ValueError as exc:
            await send_branded(message, f"Формат: /disallow 123456789\n\n{exc}")
            return
        lines = ["🚫 Доступ отозван:"]
        for target_id in target_ids:
            if access.is_admin(target_id):
                lines.append(
                    f"• {target_id} — админ, доступ из .env (нельзя забрать командой)"
                )
                continue
            removed = storage.revoke_access(target_id)
            if removed:
                lines.append(f"• {target_id} — удалён")
            else:
                lines.append(f"• {target_id} — не было в списке")
        await send_branded(message, "\n".join(lines))

    @router.message(Command("allowlist"))
    async def allowlist_message(message: Message) -> None:
        if not message.from_user:
            return
        if not access.is_admin(message.from_user.id):
            await send_branded(message, format_access_denied(message.from_user.id))
            return
        await send_branded(message, format_allowlist(settings, storage))

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        storage.save_user_filters(
            message.from_user.id, state.filters(message.from_user.id)
        )
        await send_branded(message, format_main_menu(), reply_markup=main_keyboard())

    @router.message(Command("help"))
    async def help_message(message: Message) -> None:
        await send_branded(message, format_help(), reply_markup=support_keyboard())

    @router.message(Command("filters"))
    async def filters_message(message: Message) -> None:
        if not message.from_user:
            return
        filters = state.filters(message.from_user.id)
        await send_branded(message, 
            format_filter_dashboard(filters), reply_markup=filters_keyboard(filters)
        )

    @router.message(Command("set"))
    async def set_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        try:
            filters, confirmation = apply_set_command(
                state.filters(message.from_user.id), command.args or ""
            )
        except ValueError as exc:
            await send_branded(message, str(exc))
            return
        state.update_filters(message.from_user.id, filters)
        await send_branded(message, 
            f"{confirmation}\n\n{format_filter_dashboard(filters)}",
            reply_markup=filters_keyboard(filters),
        )

    @router.message(Command("reset"))
    async def reset_message(message: Message) -> None:
        if not message.from_user:
            return
        filters = state.reset_filters(message.from_user.id)
        await send_branded(message, 
            f"Фильтры сброшены.\n\n{format_filter_dashboard(filters)}",
            reply_markup=filters_keyboard(filters),
        )

    @router.message(Command("presets"))
    async def presets_message(message: Message) -> None:
        await send_branded(message, 
            "Готовые наборы запросов:", reply_markup=presets_keyboard()
        )

    @router.message(Command("savefilter", "savefilters"))
    async def save_filter_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        try:
            preset = state.save_filter_preset(message.from_user.id, command.args or "")
        except ValueError as exc:
            await send_branded(message, str(exc))
            return
        await send_branded(message, 
            f"✅ Пресет фильтров сохранён: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filter_presets_keyboard(
                state.filter_presets(message.from_user.id)
            ),
        )

    @router.message(Command("filterpresets", "filterpreset"))
    async def filter_presets_message(message: Message) -> None:
        if not message.from_user:
            return
        presets = state.filter_presets(message.from_user.id)
        await send_branded(message, 
            format_filter_presets(presets),
            reply_markup=filter_presets_keyboard(presets),
        )

    @router.message(Command("latest"))
    async def latest_message(message: Message) -> None:
        if not message.from_user:
            return
        scan_id = storage.latest_scan_id(
            user_id=message.from_user.id, only_done=True, require_reports=True
        )
        if scan_id is None:
            await send_branded(message, 
                "Пока нет сохранённых результатов.", reply_markup=database_keyboard()
            )
            return
        text, markup = build_results_page(
            storage, scan_id, page=1, user_id=message.from_user.id
        )
        if text is None:
            await send_branded(message, 
                "Пока нет сохранённых результатов.", reply_markup=database_keyboard()
            )
            return
        await send_branded(message, text, reply_markup=markup)

    @router.message(Command("history"))
    async def history_message(message: Message) -> None:
        if not message.from_user:
            return
        scans = storage.list_scans(user_id=message.from_user.id, limit=10)
        await send_branded(message, 
            format_scan_history(scans), reply_markup=history_keyboard(bool(scans))
        )

    @router.message(Command("export"))
    async def export_message(message: Message) -> None:
        if not message.from_user:
            return
        await export_latest(message, storage, message.from_user.id)

    @router.message(Command("find"))
    async def find_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        queries = parse_queries(command.args or "")
        if not queries:
            await send_branded(message, 
                "🔎 Для поиска нужны ключевые слова.\n\n"
                "Примеры:\n"
                "/find женский блог, канал про моду\n"
                "/find рецепты и кулинария",
                reply_markup=parsing_keyboard(),
            )
            return
        await run_scan(
            message,
            queries,
            state,
            collector,
            storage,
            settings,
            user_id=message.from_user.id,
        )

    @router.message(Command("discover"))
    async def discover_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        try:
            identifier, post_limit, discovery_options = parse_discover_args(
                command.args or ""
            )
        except ValueError as exc:
            await send_branded(message, str(exc))
            return
        await run_discovery(
            message,
            identifier,
            post_limit,
            state,
            collector,
            storage,
            settings,
            user_id=message.from_user.id,
            discovery_options=discovery_options,
        )

    @router.message(Command("check"))
    async def check_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        identifier = (command.args or "").strip()
        if not identifier:
            await send_branded(message, 
                "Нужен канал: /check @channel или /check https://t.me/channel"
            )
            return
        await run_audit(
            message, identifier, state, collector, storage, user_id=message.from_user.id
        )

    @router.message(F.text)
    async def pending_input_message(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        user_id = message.from_user.id
        pending = state.pending_input(user_id)
        if not pending:
            return

        text = message.text.strip()
        if text.startswith("/"):
            state.clear_pending_input(user_id)
            await send_branded(message, "Ок, ввод отменён.", reply_markup=main_keyboard())
            return

        if pending == "filter_preset_title":
            try:
                preset = state.save_filter_preset(user_id, text)
            except ValueError as exc:
                await send_branded(message, 
                    str(exc), reply_markup=filter_preset_name_keyboard()
                )
                return
            state.clear_pending_input(user_id)
            presets = state.filter_presets(user_id)
            await send_branded(message, 
                f"✅ Пресет фильтров сохранён: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
                reply_markup=filter_presets_keyboard(presets),
            )
            return

        if pending == "find":
            state.clear_pending_input(user_id)
            queries = parse_queries(text)
            if not queries:
                await send_branded(message, 
                    "Нужны ключевые слова через запятую.\nПример: женский блог, канал про моду",
                    reply_markup=parsing_keyboard(),
                )
                return
            await run_scan(
                message, queries, state, collector, storage, settings, user_id=user_id
            )
            return

        if pending == "check":
            state.clear_pending_input(user_id)
            if not text:
                await send_branded(message, 
                    "Нужен канал: @channel или https://t.me/channel",
                    reply_markup=parsing_keyboard(),
                )
                return
            await run_audit(message, text, state, collector, storage, user_id=user_id)
            return

        if pending == "discover":
            # Legacy one-shot: full command line still supported.
            state.clear_pending_input(user_id)
            try:
                identifier, post_limit, discovery_options = parse_discover_args(text)
            except ValueError as exc:
                await send_branded(message, str(exc), reply_markup=parsing_keyboard())
                return
            await run_discovery(
                message,
                identifier,
                post_limit,
                state,
                collector,
                storage,
                settings,
                user_id=user_id,
                discovery_options=discovery_options,
            )
            return

        if pending == "discover_channel":
            wizard = state.discover_wizard(user_id) or state.start_discover_wizard(
                user_id
            )
            try:
                from ChannelsParser.collector import normalize_channel_identifier

                normalize_channel_identifier(text)
            except ValueError as exc:
                await send_branded(message, 
                    f"{exc}\n\nПришли @channel или t.me/…",
                    reply_markup=cancel_input_keyboard(),
                )
                return
            wizard.identifier = text.strip()
            state.clear_pending_input(user_id)
            await send_branded(message, 
                format_discover_wizard_posts_step(wizard),
                reply_markup=discover_wizard_posts_keyboard(),
            )
            return

        if pending == "discover_posts_custom":
            wizard = state.discover_wizard(user_id)
            if wizard is None or not wizard.identifier:
                state.clear_pending_input(user_id)
                await send_branded(message, 
                    "Визард устарел. Начни discovery заново.",
                    reply_markup=parsing_keyboard(),
                )
                return
            try:
                posts = int(text.strip())
            except ValueError:
                await send_branded(message, 
                    "Нужно число от 1 до 500.", reply_markup=cancel_input_keyboard()
                )
                return
            if posts < 1 or posts > 500:
                await send_branded(message, 
                    "Лимит постов: 1–500.", reply_markup=cancel_input_keyboard()
                )
                return
            wizard.post_limit = posts
            state.clear_pending_input(user_id)
            await send_branded(message, 
                format_discover_wizard_subs_step(wizard),
                reply_markup=discover_wizard_subs_keyboard(),
            )
            return

        if pending == "discover_subs_custom":
            wizard = state.discover_wizard(user_id)
            if wizard is None or not wizard.identifier:
                state.clear_pending_input(user_id)
                await send_branded(message, 
                    "Визард устарел. Начни discovery заново.",
                    reply_markup=parsing_keyboard(),
                )
                return
            try:
                min_s, max_s = _parse_subs_freeform(text)
            except ValueError as exc:
                await send_branded(message, str(exc), reply_markup=cancel_input_keyboard())
                return
            wizard.min_subscribers = min_s
            wizard.max_subscribers = max_s
            state.clear_pending_input(user_id)
            await send_branded(message, 
                format_discover_wizard_sources_step(wizard),
                reply_markup=discover_wizard_sources_keyboard(wizard),
            )
            return

        state.clear_pending_input(user_id)

    @router.callback_query(F.data == "filters")
    async def filters_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.filters(callback.from_user.id)
        await _edit_or_answer(
            message,
            format_filter_dashboard(filters),
            reply_markup=filters_keyboard(filters),
        )
        await callback.answer()

    @router.callback_query(F.data == "filters:dashboard")
    async def filters_dashboard_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.filters(callback.from_user.id)
        await edit_branded(message, 
            format_filter_dashboard(filters), reply_markup=filters_keyboard(filters)
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("filters:section:"))
    async def filters_section_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        section = callback.data.removeprefix("filters:section:")
        filters = state.filters(callback.from_user.id)
        try:
            text = format_filter_section(section, filters)
            keyboard = filter_section_keyboard(section, filters)
        except ValueError:
            await callback.answer("Не найдено", show_alert=True)
            return
        await edit_branded(message, text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data == "menu:main")
    async def main_menu_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(message, format_main_menu(), reply_markup=main_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "parsing")
    async def parsing_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(
            message, format_parsing_menu(), reply_markup=parsing_keyboard()
        )
        await callback.answer()

    @router.callback_query(F.data == "presets")
    async def presets_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(
            message,
            "🧩 Готовые наборы запросов\n\nВыбери вертикаль — поиск стартует сразу с текущими фильтрами.",
            reply_markup=presets_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "prompt:find")
    async def prompt_find_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.request_input(callback.from_user.id, "find")
        await _edit_or_answer(
            message,
            "🔎 Свой поиск\n\n"
            "Отправь ключевые слова одним сообщением — через запятую или с новой строки.\n\n"
            "Пример:\nженская одежда, шоурум, wildberries",
            reply_markup=cancel_input_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "prompt:check")
    async def prompt_check_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.request_input(callback.from_user.id, "check")
        await _edit_or_answer(
            message,
            "🧪 Проверка канала\n\n"
            "Отправь @username или ссылку t.me/...\n\n"
            "Пример:\n@durov",
            reply_markup=cancel_input_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "prompt:discover")
    async def prompt_discover_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.start_discover_wizard(callback.from_user.id)
        state.request_input(callback.from_user.id, "discover_channel")
        await _edit_or_answer(
            message,
            "🔗 Discovery · шаг 1/4\n\n"
            "Отправь юз или ссылку донорского канала.\n\n"
            "Пример: @source_channel",
            reply_markup=cancel_input_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "input:cancel")
    async def input_cancel_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.clear_pending_input(callback.from_user.id)
        state.clear_discover_wizard(callback.from_user.id)
        await _edit_or_answer(
            message, format_parsing_menu(), reply_markup=parsing_keyboard()
        )
        await callback.answer("Отменено")

    @router.callback_query(F.data.startswith("dw:posts:"))
    async def discover_wizard_posts_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        wizard = state.discover_wizard(callback.from_user.id)
        if wizard is None or not wizard.identifier:
            await callback.answer("Визард устарел, начни заново", show_alert=True)
            return
        raw = callback.data.removeprefix("dw:posts:")
        if raw == "custom":
            state.request_input(callback.from_user.id, "discover_posts_custom")
            await _edit_or_answer(
                message,
                "🔗 Discovery · посты\n\nОтправь число постов от 1 до 500.",
                reply_markup=cancel_input_keyboard(),
            )
            await callback.answer()
            return
        try:
            wizard.post_limit = int(raw)
        except ValueError:
            await callback.answer("Некорректный лимит", show_alert=True)
            return
        if wizard.post_limit < 1 or wizard.post_limit > 500:
            await callback.answer("Лимит 1–500", show_alert=True)
            return
        state.clear_pending_input(callback.from_user.id)
        await _edit_or_answer(
            message,
            format_discover_wizard_subs_step(wizard),
            reply_markup=discover_wizard_subs_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("dw:subs:"))
    async def discover_wizard_subs_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        wizard = state.discover_wizard(callback.from_user.id)
        if wizard is None or not wizard.identifier:
            await callback.answer("Визард устарел, начни заново", show_alert=True)
            return
        raw = callback.data.removeprefix("dw:subs:")
        if raw == "custom":
            state.request_input(callback.from_user.id, "discover_subs_custom")
            await _edit_or_answer(
                message,
                "🔗 Discovery · подписчики\n\n"
                "Отправь диапазон: `100 5000` или `100-5000` или `any`.",
                reply_markup=cancel_input_keyboard(),
            )
            await callback.answer()
            return
        try:
            wizard.min_subscribers, wizard.max_subscribers = _parse_subs_callback(raw)
        except ValueError:
            await callback.answer("Некорректный диапазон", show_alert=True)
            return
        state.clear_pending_input(callback.from_user.id)
        await _edit_or_answer(
            message,
            format_discover_wizard_sources_step(wizard),
            reply_markup=discover_wizard_sources_keyboard(wizard),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("dw:src:"))
    async def discover_wizard_sources_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        wizard = state.discover_wizard(callback.from_user.id)
        if wizard is None or not wizard.identifier:
            await callback.answer("Визард устарел, начни заново", show_alert=True)
            return
        key = callback.data.removeprefix("dw:src:")
        if key == "comments":
            wizard.include_comment_links = not wizard.include_comment_links
        elif key == "profile":
            wizard.include_profile_refs = not wizard.include_profile_refs
        elif key == "gifts":
            wizard.include_gifts = not wizard.include_gifts
        elif key == "start":
            if not (
                wizard.include_comment_links
                or wizard.include_profile_refs
                or wizard.include_gifts
            ):
                await callback.answer("Включи хотя бы один источник", show_alert=True)
                return
            options = DiscoveryOptions(
                include_comment_links=wizard.include_comment_links,
                include_profile_refs=wizard.include_profile_refs,
                include_gifts=wizard.include_gifts,
                min_subscribers=wizard.min_subscribers,
                max_subscribers=wizard.max_subscribers,
            )
            identifier = wizard.identifier
            post_limit = wizard.post_limit
            state.clear_discover_wizard(callback.from_user.id)
            state.clear_pending_input(callback.from_user.id)
            await callback.answer("Запускаю discovery")
            await run_discovery(
                message,
                identifier or "",
                post_limit,
                state,
                collector,
                storage,
                settings,
                user_id=callback.from_user.id,
                discovery_options=options,
            )
            return
        else:
            await callback.answer("Неизвестно", show_alert=True)
            return
        await edit_branded(message, 
            format_discover_wizard_sources_step(wizard),
            reply_markup=discover_wizard_sources_keyboard(wizard),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("results:page:"))
    async def results_page_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        # results:page:{scan_id}:{page}
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Некорректная страница", show_alert=True)
            return
        scan_id, page_raw = parts[2], parts[3]
        try:
            page = int(page_raw)
        except ValueError:
            await callback.answer("Некорректная страница", show_alert=True)
            return
        text, markup = build_results_page(
            storage, scan_id, page=page, user_id=callback.from_user.id
        )
        if text is None:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        await edit_branded(message, text, reply_markup=markup)
        await callback.answer()

    @router.callback_query(F.data.startswith("results:del:"))
    async def results_delete_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        scan_id = callback.data.removeprefix("results:del:")
        if not storage.delete_scan(scan_id, user_id=callback.from_user.id):
            await callback.answer("Не удалось удалить", show_alert=True)
            return
        await edit_branded(message, "🗑 Запись удалена.", reply_markup=database_keyboard())
        await callback.answer("Удалено")

    @router.callback_query(F.data == "database")
    async def database_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        scans = storage.list_scans(user_id=callback.from_user.id, limit=1)
        reports = storage.latest_reports(user_id=callback.from_user.id, limit=1)
        await _edit_or_answer(
            message,
            format_database_menu(has_history=bool(scans), has_results=bool(reports)),
            reply_markup=database_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "accounts")
    async def accounts_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(
            message,
            format_accounts_menu(settings.telegram_session),
            reply_markup=accounts_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "subscription")
    async def subscription_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(
            message, format_subscription_menu(), reply_markup=main_keyboard()
        )
        await callback.answer()

    @router.callback_query(F.data == "support")
    async def support_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await _edit_or_answer(message, format_help(), reply_markup=support_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "filterpresets")
    async def filter_presets_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        presets = state.filter_presets(callback.from_user.id)
        await _edit_or_answer(
            message,
            format_filter_presets(presets),
            reply_markup=filter_presets_keyboard(presets),
        )
        await callback.answer()

    @router.callback_query(F.data == "filterpreset:save:auto")
    async def filter_preset_save_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        preset = state.save_filter_preset(
            callback.from_user.id, _auto_filter_preset_title()
        )
        presets = state.filter_presets(callback.from_user.id)
        await edit_branded(message, 
            f"✅ Пресет фильтров сохранён: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filter_presets_keyboard(presets),
        )
        await callback.answer("Пресет сохранён")

    @router.callback_query(F.data == "filterpreset:save:named")
    async def filter_preset_save_named_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.request_filter_preset_title(callback.from_user.id)
        await edit_branded(message, 
            "Название пресета\n\nОтправь короткое название, например: каналы 100-300.",
            reply_markup=filter_preset_name_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "filterpreset:save:cancel")
    async def filter_preset_save_cancel_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.clear_filter_preset_title_request(callback.from_user.id)
        filters = state.filters(callback.from_user.id)
        await edit_branded(message, 
            format_filter_dashboard(filters), reply_markup=filters_keyboard(filters)
        )
        await callback.answer("Отменено")

    @router.callback_query(F.data.startswith("filterpreset:apply:"))
    async def filter_preset_apply_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        preset_id = _callback_int(callback.data, "filterpreset:apply:")
        if preset_id is None:
            await callback.answer("Некорректный пресет", show_alert=True)
            return
        preset = state.apply_filter_preset(callback.from_user.id, preset_id)
        if preset is None:
            await callback.answer("Не найдено", show_alert=True)
            return
        await edit_branded(message, 
            f"✅ Применён пресет: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filters_keyboard(preset.filters),
        )
        await callback.answer("Пресет применён")

    @router.callback_query(F.data.startswith("filterpreset:delete:"))
    async def filter_preset_delete_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        preset_id = _callback_int(callback.data, "filterpreset:delete:")
        if preset_id is None:
            await callback.answer("Некорректный пресет", show_alert=True)
            return
        if not state.delete_filter_preset(callback.from_user.id, preset_id):
            await callback.answer("Не найдено", show_alert=True)
            return
        presets = state.filter_presets(callback.from_user.id)
        await edit_branded(message, 
            format_filter_presets(presets),
            reply_markup=filter_presets_keyboard(presets),
        )
        await callback.answer("Пресет удален")

    @router.callback_query(F.data.startswith("preset:"))
    async def preset_scan(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        preset_key = callback.data.split(":", 1)[1]
        preset = get_preset(preset_key)
        if not preset:
            await callback.answer("Не найдено", show_alert=True)
            return
        await callback.answer("Запускаю поиск")
        await run_scan(
            message,
            list(preset.queries),
            state,
            collector,
            storage,
            settings,
            user_id=callback.from_user.id,
            title=preset.title,
        )

    @router.callback_query(F.data == "latest")
    async def latest_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        scan_id = storage.latest_scan_id(
            user_id=callback.from_user.id, only_done=True, require_reports=True
        )
        if scan_id is None:
            await _edit_or_answer(
                message,
                "Пока нет сохранённых результатов.",
                reply_markup=database_keyboard(),
            )
            await callback.answer()
            return
        text, markup = build_results_page(
            storage, scan_id, page=1, user_id=callback.from_user.id
        )
        if text is None:
            await _edit_or_answer(
                message,
                "Пока нет сохранённых результатов.",
                reply_markup=database_keyboard(),
            )
        else:
            await _edit_or_answer(message, text, reply_markup=markup)
        await callback.answer()

    @router.callback_query(F.data == "history")
    async def history_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        scans = storage.list_scans(user_id=callback.from_user.id, limit=10)
        await send_branded(message, 
            format_scan_history(scans), reply_markup=history_keyboard(bool(scans))
        )
        await callback.answer()

    @router.callback_query(F.data == "export:last")
    async def export_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        await export_latest(message, storage, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data == "scan:finish")
    async def scan_finish_callback(callback: CallbackQuery) -> None:
        if not state.scan_lock.locked():
            await callback.answer("Активного поиска нет")
            return
        state.finish_scan_collection()
        await callback.answer(
            "Завершаю обход постов, дальше обработаю найденных кандидатов"
        )

    @router.callback_query(F.data == "scan:cancel")
    async def scan_cancel_callback(callback: CallbackQuery) -> None:
        if not state.scan_lock.locked():
            await callback.answer("Активного поиска нет")
            return
        state.cancel_scan()
        await callback.answer("Останавливаю полностью")

    @router.callback_query(F.data == "filters:reset")
    async def filters_reset_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.reset_filters(callback.from_user.id)
        await edit_branded(message, 
            f"Фильтры сброшены.\n\n{format_filter_dashboard(filters)}",
            reply_markup=filters_keyboard(filters),
        )
        await callback.answer("Фильтры сброшены")

    @router.callback_query(F.data.startswith("subs:"))
    async def subscribers_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            _, min_raw, max_raw = callback.data.split(":")
            min_value = None if min_raw == "none" else int(min_raw)
            max_value = None if max_raw == "none" else int(max_raw)
        except ValueError:
            await callback.answer("Некорректный диапазон подписчиков", show_alert=True)
            return
        filters = replace(
            state.filters(callback.from_user.id),
            min_subscribers=min_value,
            max_subscribers=max_value,
        )
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("subs", filters),
            reply_markup=filter_section_keyboard("subs", filters),
        )
        await callback.answer("Подписчики обновлены")

    @router.callback_query(F.data.startswith("active:"))
    async def active_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            days = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректный срок активности", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), max_last_post_days=days)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("fresh", filters),
            reply_markup=filter_section_keyboard("fresh", filters),
        )
        await callback.answer("Активность обновлена")

    @router.callback_query(F.data.startswith("views:"))
    async def views_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            raw_value = callback.data.split(":")[1]
            min_views = None if raw_value == "none" else int(raw_value)
        except (IndexError, ValueError):
            await callback.answer("Некорректный порог просмотров", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), min_avg_views=min_views)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("views", filters),
            reply_markup=filter_section_keyboard("views", filters),
        )
        await callback.answer("Просмотры обновлены")

    @router.callback_query(F.data.startswith("scoremin:"))
    async def score_min_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = float(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректный скор активности", show_alert=True)
            return
        filters = replace(
            state.filters(callback.from_user.id), min_activity_score=value
        )
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("score", filters),
            reply_markup=filter_section_keyboard("score", filters),
        )
        await callback.answer("Скор активности обновлен")

    @router.callback_query(F.data.startswith("kind:"))
    async def channel_kind_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректный тип канала", show_alert=True)
            return
        if value not in VALID_CHANNEL_KINDS:
            await callback.answer("Некорректный тип канала", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), channel_kind=value)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("kind", filters),
            reply_markup=filter_section_keyboard("kind", filters),
        )
        await callback.answer("Тип канала обновлен")

    @router.callback_query(F.data.startswith("audience:"))
    async def audience_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректная аудитория", show_alert=True)
            return
        if value not in {"any", "female", "male"}:
            await callback.answer("Некорректная аудитория", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), audience_bias=value)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("audience", filters),
            reply_markup=filter_section_keyboard("audience", filters),
        )
        await callback.answer("Аудитория обновлена")

    @router.callback_query(F.data.startswith("age:"))
    async def age_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректный возраст", show_alert=True)
            return
        if value not in VALID_AGES:
            await callback.answer("Некорректный возраст", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), age_group=value)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("age", filters),
            reply_markup=filter_section_keyboard("age", filters),
        )
        await callback.answer("Возраст обновлен")

    @router.callback_query(F.data.startswith("sort:"))
    async def sort_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректная сортировка", show_alert=True)
            return
        if value not in VALID_SORTS:
            await callback.answer("Некорректная сортировка", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), sort_by=value)
        state.update_filters(callback.from_user.id, filters)
        await edit_branded(message, 
            format_filter_section("sort", filters),
            reply_markup=filter_section_keyboard("sort", filters),
        )
        await callback.answer("Сортировка обновлена")

    return router


async def run_scan(
    message: Message,
    queries: list[str],
    state: BotState,
    collector: TelegramChannelCollector,
    storage: ChannelStorage,
    settings: AppSettings,
    *,
    user_id: int,
    title: str | None = None,
) -> None:
    if state.scan_lock.locked():
        await send_branded(message, 
            "Сейчас уже идет поиск. Дождись завершения или нажми «Завершить досрочно»."
        )
        return

    await state.scan_lock.acquire()
    state.reset_scan_cancel()
    try:
        filters = state.filters(user_id)
        scan_id = uuid.uuid4().hex
        storage.create_scan(
            scan_id, user_id=user_id, mode="search", queries=queries, filters=filters
        )

        query_preview = ", ".join(queries[:5])
        if len(queries) > 5:
            query_preview += f" и ещё {len(queries) - 5}"
        label = f"{title}\n" if title else ""
        await send_branded(message, 
            format_scan_progress_card(
                title="🔎 Поиск каналов",
                progress_label="запросы",
                processed=0,
                total=len(queries),
                found=0,
                started_at=datetime.now(timezone.utc),
                details=f"{label}{query_preview}\n\n{format_filters(filters)}",
            ),
            reply_markup=scan_cancel_keyboard(),
        )

        try:
            # For keyword search there is no separate "inspect candidates" phase:
            # soft finish and hard cancel both stop remaining queries.
            result = await collector.search_channels(
                queries,
                filters,
                should_stop=lambda: (
                    state.scan_cancelled() or state.scan_finish_collection_requested()
                ),
            )
            reports = result.reports
            storage.save_reports(scan_id, reports)
            storage.finish_scan(
                scan_id,
                total_candidates=result.total_candidates,
                total_reports=len(reports),
            )
        except Exception as exc:
            storage.fail_scan(scan_id, error=str(exc))
            await send_branded(message, f"Ошибка поиска: {exc}\nscan_id: {scan_id[:8]}")
            return

        summary = format_scan_done(
            scan_id, result.total_candidates, len(reports), result.errors
        )
        await answer_long(
            message,
            f"{summary}\n\n{format_reports(reports, limit=_scan_results_limit(reports, settings))}",
            reply_markup=results_keyboard(bool(reports)),
        )
    finally:
        state.reset_scan_cancel()
        state.scan_lock.release()


async def run_discovery(
    message: Message,
    identifier: str,
    post_limit: int,
    state: BotState,
    collector: TelegramChannelCollector,
    storage: ChannelStorage,
    settings: AppSettings,
    *,
    user_id: int,
    discovery_options: DiscoveryOptions | None = None,
) -> None:
    if state.scan_lock.locked():
        await send_branded(message, 
            "Сейчас уже идет поиск. Дождись завершения или нажми «Завершить досрочно»."
        )
        return

    await state.scan_lock.acquire()
    state.reset_scan_cancel()
    try:
        discovery_options = discovery_options or DiscoveryOptions()
        filters = discovery_filters(state.filters(user_id), discovery_options)
        scan_id = uuid.uuid4().hex
        queries = [
            identifier,
            f"posts:{post_limit}",
            *_discovery_option_tokens(discovery_options),
        ]
        storage.create_scan(
            scan_id, user_id=user_id, mode="discover", queries=queries, filters=filters
        )
        gift_limit = (
            settings.discovery_gift_limit if discovery_options.include_gifts else 0
        )
        started_at = datetime.now(timezone.utc)

        progress_message = await send_branded(message, 
            format_discovery_progress_card(
                identifier=identifier,
                post_limit=post_limit,
                stats={},
                started_at=started_at,
                discovery_options=discovery_options,
                gift_limit=gift_limit,
            ),
            reply_markup=scan_cancel_keyboard(),
        )
        last_progress_update = 0.0

        async def update_progress(stats: dict[str, int]) -> None:
            nonlocal last_progress_update
            now = time.monotonic()
            if now - last_progress_update < 2.0:
                return
            last_progress_update = now
            if not hasattr(progress_message, "edit_text"):
                return
            try:
                await edit_branded(progress_message, 
                    format_discovery_progress_card(
                        identifier=identifier,
                        post_limit=post_limit,
                        stats=stats,
                        started_at=started_at,
                        discovery_options=discovery_options,
                        gift_limit=gift_limit,
                    ),
                    reply_markup=scan_cancel_keyboard(),
                )
            except Exception:
                logging.debug(
                    "Could not edit discovery progress message", exc_info=True
                )

        try:
            result = await collector.discover_channels_from_comments(
                identifier,
                filters,
                post_limit=post_limit,
                comments_per_post=settings.discovery_comments_per_post,
                profile_limit=settings.discovery_profile_limit,
                candidate_limit=settings.discovery_candidate_limit,
                gift_limit=gift_limit,
                include_comment_links=discovery_options.include_comment_links,
                include_profile_refs=discovery_options.include_profile_refs,
                should_stop=state.scan_cancelled,
                should_finish_collection=state.scan_finish_collection_requested,
                progress_callback=update_progress,
            )
            reports = result.reports
            storage.save_reports(scan_id, reports)
            storage.finish_scan(
                scan_id,
                total_candidates=result.total_candidates,
                total_reports=len(reports),
            )
        except Exception as exc:
            storage.fail_scan(scan_id, error=str(exc))
            await send_branded(message, f"Ошибка discovery: {exc}\nscan_id: {scan_id[:8]}")
            return

        if hasattr(progress_message, "edit_text"):
            try:
                await edit_branded(progress_message, 
                    format_discovery_progress_card(
                        identifier=identifier,
                        post_limit=post_limit,
                        stats=result.stats or {},
                        started_at=started_at,
                        discovery_options=discovery_options,
                        gift_limit=gift_limit,
                        done=True,
                    )
                )
            except Exception:
                logging.debug(
                    "Could not finalize discovery progress message", exc_info=True
                )

        # Compact paginated results (like reference UI). Funnel stats only if errors/warnings.
        text, markup = build_results_page(storage, scan_id, page=1, user_id=user_id)
        if text is None:
            await send_branded(message, "Скан завершён, но запись не найдена.")
        else:
            note = ""
            if result.errors:
                note = "\n\n⚠️ " + "; ".join(result.errors[:2])
            await send_branded(message, text + note, reply_markup=markup)
    finally:
        state.reset_scan_cancel()
        state.scan_lock.release()


async def run_audit(
    message: Message,
    identifier: str,
    state: BotState,
    collector: TelegramChannelCollector,
    storage: ChannelStorage,
    *,
    user_id: int,
) -> None:
    if state.scan_lock.locked():
        await send_branded(message, 
            "Сейчас уже идет поиск. Дождись завершения или нажми «Завершить досрочно»."
        )
        return

    await state.scan_lock.acquire()
    try:
        filters = state.filters(user_id)
        scan_id = uuid.uuid4().hex
        storage.create_scan(
            scan_id,
            user_id=user_id,
            mode="audit",
            queries=[identifier],
            filters=filters,
        )
        await send_branded(message, f"🧪 Проверяю {identifier}…")

        try:
            report = await collector.inspect_channel_identifier(identifier)
            storage.save_reports(scan_id, [report])
            storage.finish_scan(scan_id, total_candidates=1, total_reports=1)
        except Exception as exc:
            storage.fail_scan(scan_id, error=str(exc), total_candidates=1)
            await send_branded(message, f"❌ Ошибка проверки: {exc}\nscan_id: {scan_id[:8]}")
            return

        passes = matches_filters(report, filters)
        filter_status = (
            "✅ Проходит текущие фильтры" if passes else "⚠️ Не проходит текущие фильтры"
        )
        await send_branded(message, 
            f"{filter_status}\nscan_id: {scan_id[:8]}\n\n{format_report(report)}",
            reply_markup=results_keyboard(True),
        )
    finally:
        state.scan_lock.release()


async def export_latest(
    message: Message, storage: ChannelStorage, user_id: int
) -> None:
    scan_id = storage.latest_scan_id(
        user_id=user_id, only_done=True, require_reports=True
    )
    if scan_id is None:
        await send_branded(message, 
            "Нет сохраненных результатов. Сначала запусти /search, /discover или /check."
        )
        return
    reports = storage.latest_reports(scan_id=scan_id, limit=500)
    if not reports:
        await send_branded(message, 
            "Нет сохраненных результатов. Сначала запусти /search, /discover или /check."
        )
        return
    payload = reports_to_csv(reports)
    caption = f"📊 CSV · {len(reports)} каналов · scan {scan_id[:8]}"
    logo = logo_input_file()
    if logo is not None and hasattr(message, "answer_photo"):
        try:
            await message.answer_photo(logo, caption=caption)
            caption = None  # already shown under logo
        except Exception:
            logging.debug("Could not attach logo to CSV export", exc_info=True)
    await message.answer_document(
        BufferedInputFile(payload, filename=f"telegram_channels_{scan_id[:8]}.csv"),
        caption=caption,
    )


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def parse_discover_args(raw: str) -> tuple[str, int, DiscoveryOptions]:
    tokens = raw.strip().split()
    if not tokens:
        raise ValueError("Нужен канал: /discover @channel 200")
    identifier = tokens[0]
    post_limit = 100
    index = 1
    if len(tokens) > 1 and _looks_int(tokens[1]):
        index = 2
        try:
            post_limit = int(tokens[1])
        except ValueError as exc:
            raise ValueError(
                "Некорректный лимит постов. Пример: /discover @channel 200"
            ) from exc
    if post_limit < 1 or post_limit > 500:
        raise ValueError("Лимит постов для discovery должен быть от 1 до 500")
    options = DiscoveryOptions()
    while index < len(tokens):
        key, value, consumed = _option_token(tokens, index)
        key = key.lower().lstrip("-")
        if key in {"comments", "comment", "comment-links", "комменты", "комментарии"}:
            options.include_comment_links = _parse_on_off(value, key)
        elif key in {
            "profile",
            "profiles",
            "attached",
            "personal",
            "профиль",
            "профили",
            "описание",
        }:
            options.include_profile_refs = _parse_on_off(value, key)
        elif key in {"gifts", "gift", "подарки", "подарок"}:
            options.include_gifts = _parse_on_off(value, key)
        elif key in {"subs", "subscribers", "пдп", "подписчики"}:
            min_subs, max_subs, consumed = _parse_discovery_subs(
                tokens, index, value, consumed
            )
            if min_subs is not None and max_subs is not None and min_subs > max_subs:
                raise ValueError("Минимум подписчиков не может быть больше максимума")
            options.min_subscribers = min_subs
            options.max_subscribers = max_subs
        else:
            raise ValueError(
                "Не знаю такую опцию discovery. Пример: /discover @channel 200 comments off gifts on subs 100 300"
            )
        index += consumed
    if not (
        options.include_comment_links
        or options.include_profile_refs
        or options.include_gifts
    ):
        raise ValueError("Включи хотя бы один источник: comments, profile или gifts")
    return identifier, post_limit, options


def _option_token(tokens: list[str], index: int) -> tuple[str, str | None, int]:
    token = tokens[index]
    if ":" in token:
        key, value = token.split(":", 1)
        return key, value, 1
    value = tokens[index + 1] if index + 1 < len(tokens) else None
    return token, value, 2


def _parse_on_off(value: str | None, key: str) -> bool:
    if value is None:
        raise ValueError(f"Для {key} нужно on/off")
    normalized = value.lower()
    if normalized in {"on", "yes", "true", "1", "да", "вкл"}:
        return True
    if normalized in {"off", "no", "false", "0", "нет", "выкл"}:
        return False
    raise ValueError(f"Для {key} нужно on/off")


def _parse_discovery_subs(
    tokens: list[str],
    index: int,
    first_value: str | None,
    consumed: int,
) -> tuple[int | None, int | None, int]:
    if first_value is None:
        raise ValueError("Для subs нужно any или пример: subs 100 300")
    value = first_value.lower()
    if value in {"any", "all", "любые", "все"}:
        return None, None, consumed
    if "-" in value:
        left, right = value.split("-", 1)
        return (
            _parse_optional_count(left, "subs"),
            _parse_optional_count(right, "subs"),
            consumed,
        )
    if value in {"from", "от"}:
        if index + consumed >= len(tokens):
            raise ValueError("Для subs от нужно число: subs от 1000")
        return _parse_count(tokens[index + consumed], "subs"), None, consumed + 1
    if value in {"to", "до"}:
        if index + consumed >= len(tokens):
            raise ValueError("Для subs до нужно число: subs до 50000")
        return None, _parse_count(tokens[index + consumed], "subs"), consumed + 1
    min_subs = _parse_count(first_value, "subs")
    if index + consumed >= len(tokens):
        raise ValueError("Для подписчиков нужны два числа или одно: subs 100 300")
    max_subs = _parse_count(tokens[index + consumed], "subs")
    return min_subs, max_subs, consumed + 1


def _parse_count(value: str, label: str) -> int:
    normalized = value.lower().replace("_", "").replace(" ", "")
    if normalized.endswith("k"):
        normalized = normalized[:-1] + "000"
    if not normalized.isdigit():
        raise ValueError(f"{label} должно быть числом")
    return int(normalized)


def _parse_optional_count(value: str, label: str) -> int | None:
    if value.lower() in {"", "none", "any", "любой"}:
        return None
    return _parse_count(value, label)


def _looks_int(value: str) -> bool:
    return value.isdigit()


def _discovery_option_tokens(options: DiscoveryOptions) -> list[str]:
    tokens = [
        f"comments:{_on_off(options.include_comment_links)}",
        f"profile:{_on_off(options.include_profile_refs)}",
        f"gifts:{_on_off(options.include_gifts)}",
    ]
    if options.min_subscribers is not None or options.max_subscribers is not None:
        tokens.append(
            f"subs:{options.min_subscribers or 'none'}:{options.max_subscribers or 'none'}"
        )
    return tokens


def _discovery_sources_label(options: DiscoveryOptions) -> str:
    labels: list[str] = []
    if options.include_comment_links:
        labels.append("комментарии")
    if options.include_profile_refs:
        labels.append("профиль")
    if options.include_gifts:
        labels.append("подарки")
    return ", ".join(labels) if labels else "нет"


def _discovery_subs_label(options: DiscoveryOptions) -> str:
    if options.min_subscribers is None and options.max_subscribers is None:
        return "любые"
    if options.min_subscribers is None:
        return f"до {_short_num(options.max_subscribers)}"
    if options.max_subscribers is None:
        return f"от {_short_num(options.min_subscribers)}"
    return (
        f"{_short_num(options.min_subscribers)}-{_short_num(options.max_subscribers)}"
    )


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _scan_results_limit(reports: Sequence[object], settings: AppSettings) -> int:
    return max(len(reports), settings.top_results)


def _callback_int(data: str, prefix: str) -> int | None:
    try:
        return int(data.removeprefix(prefix))
    except ValueError:
        return None


def _auto_filter_preset_title() -> str:
    return datetime.now().strftime("Пресет %d.%m %H:%M:%S")


def resolve_logo_path() -> Path | None:
    """Locate logo.jpg in CWD or project root (next to pyproject / package parents)."""
    candidates = [
        Path.cwd() / "logo.jpg",
        Path(__file__).resolve().parents[2] / "logo.jpg",  # src/ChannelsParser -> repo root
        Path(__file__).resolve().parents[1] / "logo.jpg",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def logo_input_file() -> FSInputFile | None:
    path = resolve_logo_path()
    if path is None:
        logging.warning("logo.jpg not found; messages will be sent without logo")
        return None
    return FSInputFile(path)


async def send_branded(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    **kwargs: Any,
) -> Any:
    """Send text with logo.jpg attached (photo + caption when it fits)."""
    logo = logo_input_file()
    if logo is not None and hasattr(message, "answer_photo"):
        try:
            if len(text) <= TELEGRAM_CAPTION_LIMIT:
                return await message.answer_photo(
                    logo,
                    caption=text,
                    reply_markup=reply_markup,
                    **kwargs,
                )
            # Caption limit: logo first, then full text body.
            await message.answer_photo(logo)
        except Exception:
            logging.debug("Could not send logo photo, falling back to text", exc_info=True)
    return await message.answer(text, reply_markup=reply_markup, **kwargs)


async def edit_branded(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Any:
    """Edit a branded (photo) or plain message in place."""
    if getattr(message, "photo", None) and hasattr(message, "edit_caption"):
        try:
            caption = (
                text
                if len(text) <= TELEGRAM_CAPTION_LIMIT
                else text[: TELEGRAM_CAPTION_LIMIT - 1] + "…"
            )
            return await message.edit_caption(caption=caption, reply_markup=reply_markup)
        except Exception:
            logging.debug("Could not edit photo caption", exc_info=True)
    if hasattr(message, "edit_text"):
        try:
            return await edit_branded(message, text, reply_markup=reply_markup)
        except Exception:
            logging.debug("Could not edit text message, sending new branded", exc_info=True)
    return await send_branded(message, text, reply_markup=reply_markup)


async def answer_long(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    chunk_size: int = 3800,
) -> None:
    chunks = split_text(text, chunk_size=chunk_size)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        if index == 0:
            await send_branded(message, chunk, reply_markup=markup)
        else:
            # Avoid repeating the logo on every long chunk.
            await message.answer(chunk, reply_markup=markup)


async def _edit_or_answer(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await edit_branded(message, text, reply_markup=reply_markup)
        return
    except Exception:
        logging.debug("Could not edit branded message, falling back to answer", exc_info=True)
    await send_branded(message, text, reply_markup=reply_markup)


def split_text(
    text: str, *, chunk_size: int | None = None, limit: int | None = None
) -> list[str]:
    size = (
        limit if limit is not None else (chunk_size if chunk_size is not None else 3800)
    )
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        block_len = len(block) + (2 if current else 0)
        if block_len > size:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(_split_long_block(block, chunk_size=size))
            continue

        if current and current_len + block_len > size:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += block_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_long_block(block: str, *, chunk_size: int) -> list[str]:
    lines = block.splitlines()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + (1 if current else 0)
        if line_len > chunk_size:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.extend(
                line[i : i + chunk_size] for i in range(0, len(line), chunk_size)
            )
            continue

        if current and current_len + line_len > chunk_size:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def format_main_menu() -> str:
    return (
        "🚀 BatMan Parser\n\n"
        "Поиск и аудит активных Telegram-каналов под закуп рекламы.\n\n"
        "• 🧻 Парсинг — пресеты, свой поиск, discovery, check\n"
        "• 💾 База — результаты, история, CSV\n"
        "• ⚙️ Фильтры — подписчики, ЦА, score и сортировка\n\n"
        "Подсказка: /find ключи, /discover @channel 200, /check @channel"
    )


def format_parsing_menu() -> str:
    return (
        "🧻 Парсинг\n\n"
        "Выбери сценарий:\n"
        "• пресет по вертикали — сразу запускает поиск\n"
        "• свой поиск — вводишь ключевые слова\n"
        "• discovery — каналы из комментариев донора\n"
        "• check — карточка одного канала"
    )


def format_database_menu(*, has_history: bool, has_results: bool) -> str:
    history = "есть" if has_history else "пока пусто"
    results = "есть" if has_results else "пока пусто"
    return (
        "💾 База данных\n\n"
        f"Последние результаты: {results}\n"
        f"История сканов: {history}\n\n"
        "Можно открыть топ каналов, историю или скачать CSV."
    )


def format_accounts_menu(session_path: object) -> str:
    return (
        "🧾 Мои аккаунты\n\n"
        "Парсер ходит в Telegram через пользовательскую сессию Telethon.\n"
        f"Сессия: `{session_path}`\n\n"
        "Чтобы сменить аккаунт — перелогинься через login CLI."
    )


def format_subscription_menu() -> str:
    return (
        "💎 Подписка\n\n"
        "Пока все функции открыты без оплаты:\n"
        "поиск, discovery, фильтры, пресеты и CSV-экспорт.\n\n"
        "Тарифы появятся здесь позже."
    )


def format_help() -> str:
    return (
        "🎧 Поддержка и команды\n\n"
        "🔎 Поиск\n"
        "/find запрос1, запрос2\n"
        "/presets — готовые вертикали\n"
        "/discover @channel 200 — или визард в меню «Парсинг»\n"
        "/discover @channel 200 gifts off subs 100 5000\n"
        "/check @channel\n\n"
        "⚙️ Фильтры\n"
        "/filters — панель\n"
        "/set — точная настройка\n"
        "/reset — сброс\n"
        "/savefilter Название — пресет фильтров\n"
        "/filterpresets — мои пресеты\n\n"
        "💾 База\n"
        "/latest · /history · /export\n\n"
        f"{SET_HELP}"
    )


class AccessControl:
    def __init__(self, settings: AppSettings, storage: ChannelStorage) -> None:
        self._settings = settings
        self._storage = storage

    def is_admin(self, user_id: int) -> bool:
        return self._settings.is_admin(user_id)

    def has_access(self, user_id: int) -> bool:
        return self.is_admin(user_id) or self._storage.is_user_allowed(user_id)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, access: AccessControl) -> None:
        self._access = access

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Admin access commands are handled in their handlers with own checks,
        # but still need to pass through for admins; for non-admins deny early.
        if self._access.has_access(user.id):
            return await handler(event, data)

        # Let /allow /disallow /allowlist reach handlers only for admins (already denied here).
        # Non-admins get a polished denial for any touch.
        if isinstance(event, Message):
            await send_branded(event, format_access_denied(user.id))
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Нет доступа", show_alert=True)
            if isinstance(event.message, Message):
                try:
                    await send_branded(event.message, format_access_denied(user.id))
                except Exception:
                    logging.debug("Could not send access denied message", exc_info=True)
        return None


def format_access_denied(user_id: int) -> str:
    return (
        "🔒 BatMan Parser\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Доступ к боту закрыт.\n\n"
        "Парсер доступен только по приглашению.\n"
        "Чтобы получить доступ напиши администратору: @maxxkireev\n"
        "И передай свой ID:\n"
        f"🆔  {user_id}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_allowlist(settings: AppSettings, storage: ChannelStorage) -> str:
    admins = sorted(settings.admin_user_ids)
    allowed = storage.list_allowed_users()
    lines = [
        "🔐 Список доступа",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👑 Админы ({len(admins)}):",
    ]
    if admins:
        for admin_id in admins:
            lines.append(f"• {admin_id}")
    else:
        lines.append("• — не заданы в .env (ADMIN_USER_IDS)")

    lines.extend(["", f"✅ Выданный доступ ({len(allowed)}):"])
    if allowed:
        for user_id, granted_by, created_at, expires_at in allowed:
            by = f" · от {granted_by}" if granted_by else ""
            when = created_at.strftime("%d.%m.%Y")
            if expires_at is None:
                term = "бессрочно"
            else:
                term = f"до {expires_at.strftime('%d.%m.%Y')}"
            lines.append(f"• {user_id}{by} · {when} · {term}")
    else:
        lines.append("• пока никого")

    lines.extend(
        [
            "",
            "Команды:",
            "/allow 123456789",
            "/allow 123456789 30",
            "/disallow 123456789",
        ]
    )
    return "\n".join(lines)


def parse_allow_args(raw: str) -> tuple[list[int], int | None]:
    """Parse `/allow id [id…] [days]`.

    Examples:
      123456789          → ([123456789], None) permanent
      123456789 30       → ([123456789], 30)
      111 222            → ([111, 222], None) permanent
      111 222 7d         → ([111, 222], 7)
    """
    tokens = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    if not tokens:
        raise ValueError("Укажи user id, опционально срок в днях")

    days: int | None = None
    last = tokens[-1].lower().replace("д", "d")
    if len(tokens) >= 2:
        if last.endswith("d") and last[:-1].isdigit():
            candidate = int(last[:-1])
            if candidate < 1 or candidate > 3650:
                raise ValueError("Срок доступа: от 1 до 3650 дней")
            days = candidate
            tokens = tokens[:-1]
        elif len(tokens) == 2 and last.isdigit() and tokens[0].lstrip("@").isdigit():
            # /allow {id} {days} — plain days only when id looks like a real TG user id
            # (avoids treating `/allow 111 222` as "111 for 222 days").
            # For small ids use explicit suffix: /allow 5000 30d
            first_id = int(tokens[0].lstrip("@"))
            candidate = int(last)
            if first_id >= 10_000 and 1 <= candidate <= 3650:
                days = candidate
                tokens = tokens[:-1]

    ids = _parse_user_ids(" ".join(tokens))
    return ids, days


def _parse_user_ids(raw: str) -> list[int]:
    tokens = [part.strip() for part in raw.replace(",", " ").split() if part.strip()]
    if not tokens:
        raise ValueError("Укажи хотя бы один Telegram user id")
    ids: list[int] = []
    for token in tokens:
        token = token.lstrip("@")
        # strip trailing d if someone passes only days by mistake
        if not token.isdigit():
            raise ValueError(f"Некорректный id: {token}")
        value = int(token)
        if value <= 0:
            raise ValueError(f"id должен быть положительным: {token}")
        if value not in ids:
            ids.append(value)
    return ids


def format_scan_progress_card(
    *,
    title: str,
    progress_label: str,
    processed: int,
    total: int,
    found: int,
    started_at: datetime,
    details: str | None = None,
    done: bool = False,
) -> str:
    elapsed = _elapsed_seconds(started_at)
    rate = processed / elapsed if elapsed > 0 else 0.0
    status = f"{title} · готово" if done else title
    lines = [
        status,
        "",
        _progress_bar(processed, total),
        f"Прогресс: {processed}/{total} {progress_label.lower()}",
        f"Найдено каналов: {found}",
        f"Время: {_format_compact_duration(elapsed)} ({rate:.1f} шт./с)",
        f"Осталось примерно: {_format_eta(processed, total, elapsed)}",
    ]
    if details:
        lines.extend(["", details])
    return "\n".join(lines)


def format_discovery_progress_card(
    *,
    identifier: str,
    post_limit: int,
    stats: dict[str, int],
    started_at: datetime,
    discovery_options: DiscoveryOptions,
    gift_limit: int | None,
    done: bool = False,
) -> str:
    """Compact live progress: one bar for current phase + a few key numbers."""
    del discovery_options, gift_limit  # kept in signature for call-site compatibility
    phase = int(stats.get("phase", 1))
    processed = int(stats.get("posts_processed", 0))
    posts_seen = int(stats.get("posts_seen", 0))
    posts_total = posts_seen if posts_seen > 0 else max(post_limit, 1)
    found = int(stats.get("reports_found", 0))
    cand_total = int(stats.get("candidates_total", 0))
    cand_done = int(stats.get("candidates_done", 0))
    elapsed = _elapsed_seconds(started_at)

    if done:
        title = "🔗 Discovery · готово"
    elif phase >= 2:
        title = "🔗 Discovery · кандидаты"
    elif int(stats.get("collection_finished_early", 0)):
        title = "🔗 Discovery · посты готовы"
    else:
        title = "🔗 Discovery · посты"

    if phase >= 2 or cand_total > 0:
        bar = _progress_bar(cand_done, max(cand_total, 1))
        progress_line = f"{cand_done}/{cand_total} кандидатов"
        eta = _format_eta(cand_done, cand_total, elapsed)
    else:
        bar = _progress_bar(processed, posts_total)
        progress_line = f"{processed}/{posts_total} постов"
        eta = _format_eta(processed, posts_total, elapsed)

    lines = [
        title,
        bar,
        f"{progress_line} · ETA {eta}",
        f"Найдено: {found} · {_format_compact_duration(elapsed)}",
        identifier,
    ]
    return "\n".join(lines)


def _progress_bar(processed: int, total: int, *, width: int = 12) -> str:
    if total <= 0:
        filled = 0
    else:
        filled = min(width, max(0, round(width * processed / total)))
    empty = width - filled
    percent = 0 if total <= 0 else min(100, int(100 * processed / total))
    return f"[{'█' * filled}{'░' * empty}] {percent}%"


def _elapsed_seconds(started_at: datetime) -> float:
    now = datetime.now(started_at.tzinfo or timezone.utc)
    return max(0.001, (now - started_at).total_seconds())


def _format_eta(processed: int, total: int, elapsed: float) -> str:
    if processed <= 0 or total <= processed:
        return "—"
    remaining = total - processed
    rate = processed / elapsed
    if rate <= 0:
        return "—"
    return _format_compact_duration(remaining / rate)


def _format_compact_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч. {minutes} м. {secs} с."
    if minutes:
        return f"{minutes} м. {secs} с."
    return f"{secs} с."


def _enabled_label(value: bool) -> str:
    return "да" if value else "нет"


def format_filter_dashboard(filters: SearchFilters) -> str:
    return (
        "Фильтры поиска\n\n"
        f"Подписчики: {_subs_label(filters)}\n"
        f"Посты: {_fresh_label(filters)}\n"
        f"Просмотры: {_views_label(filters)}\n"
        f"Score: {_score_label(filters)}\n"
        f"Тип: {_channel_kind_label(filters)}\n"
        f"ЦА: {_audience_label(filters)}\n"
        f"Возраст: {_age_label(filters)}\n"
        f"Сортировка: {_sort_label(filters)}\n\n"
        "Нажми раздел, чтобы изменить значение."
    )


def format_filter_section(section: str, filters: SearchFilters) -> str:
    labels = {
        "subs": ("👥 Подписчики", _subs_label(filters)),
        "fresh": ("⏱ Свежесть поста", _fresh_label(filters)),
        "views": ("👁 Средние просмотры", _views_label(filters)),
        "score": ("⚡ Score активности", _score_label(filters)),
        "kind": ("🏷 Тип канала", _channel_kind_label(filters)),
        "audience": ("🎯 Аудитория", _audience_label(filters)),
        "age": ("📅 Возраст", _age_label(filters)),
        "sort": ("↕️ Сортировка", _sort_label(filters)),
    }
    title, value = labels.get(section, ("Фильтр", "-"))
    if section not in labels:
        raise ValueError(f"Unknown filter section: {section}")
    return f"{title}\n\nСейчас: {value}\n\nВыбери новое значение:"


def main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧻 Парсинг", callback_data="parsing")
    builder.button(text="💾 База данных", callback_data="database")
    builder.button(text="🧾 Мои аккаунты", callback_data="accounts")
    builder.button(text="💎 Подписка", callback_data="subscription")
    builder.button(text="🎧 Поддержка", callback_data="support")
    builder.adjust(1, 2, 2)
    return builder.as_markup()


def parsing_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧩 Пресеты запросов", callback_data="presets")
    builder.button(text="🔎 Свой поиск", callback_data="prompt:find")
    builder.button(text="🔗 Discovery", callback_data="prompt:discover")
    builder.button(text="🧪 Check канала", callback_data="prompt:check")
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="💾 Мои пресеты фильтров", callback_data="filterpresets")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def presets_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for preset in QUERY_PRESETS.values():
        builder.button(text=preset.title, callback_data=f"preset:{preset.key}")
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="💾 Мои пресеты фильтров", callback_data="filterpresets")
    builder.button(text="⬅️ К парсингу", callback_data="parsing")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def database_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Последние результаты", callback_data="latest")
    builder.button(text="🗂 История", callback_data="history")
    builder.button(text="📥 Скачать CSV", callback_data="export:last")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def accounts_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="💾 Мои пресеты фильтров", callback_data="filterpresets")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def support_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧻 Парсинг", callback_data="parsing")
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def cancel_input_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Отмена", callback_data="input:cancel")
    builder.adjust(1)
    return builder.as_markup()


def filters_keyboard(filters: SearchFilters) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"Подписчики: {_subs_label(filters)}", callback_data="filters:section:subs"
    )
    builder.button(
        text=f"Посты: {_fresh_label(filters)}", callback_data="filters:section:fresh"
    )
    builder.button(
        text=f"Просмотры: {_views_label(filters)}",
        callback_data="filters:section:views",
    )
    builder.button(
        text=f"Score: {_score_label(filters)}", callback_data="filters:section:score"
    )
    builder.button(
        text=f"Тип: {_channel_kind_label(filters)}",
        callback_data="filters:section:kind",
    )
    builder.button(
        text=f"ЦА: {_audience_label(filters)}", callback_data="filters:section:audience"
    )
    builder.button(
        text=f"Возраст: {_age_label(filters)}", callback_data="filters:section:age"
    )
    builder.button(
        text=f"Сортировка: {_sort_label(filters)}", callback_data="filters:section:sort"
    )
    builder.button(text="💾 Мои пресеты фильтров", callback_data="filterpresets")
    builder.button(
        text="📌 Сохранить как пресет", callback_data="filterpreset:save:named"
    )
    builder.button(text="♻️ Сбросить фильтры", callback_data="filters:reset")
    builder.adjust(2, 2, 2, 2, 1, 2)
    return builder.as_markup()


def filter_section_keyboard(
    section: str, filters: SearchFilters
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if section == "subs":
        options = [
            ("100-300", "subs:100:300"),
            ("300-1k", "subs:300:1000"),
            ("1k-5k", "subs:1000:5000"),
            ("5k-20k", "subs:5000:20000"),
            ("20k-50k", "subs:20000:50000"),
            ("От 50k", "subs:50000:none"),
            ("Любые", "subs:none:none"),
        ]
        selected = {
            "subs:100:300": filters.min_subscribers == 100
            and filters.max_subscribers == 300,
            "subs:300:1000": filters.min_subscribers == 300
            and filters.max_subscribers == 1000,
            "subs:1000:5000": filters.min_subscribers == 1000
            and filters.max_subscribers == 5000,
            "subs:5000:20000": filters.min_subscribers == 5000
            and filters.max_subscribers == 20000,
            "subs:20000:50000": filters.min_subscribers == 20000
            and filters.max_subscribers == 50000,
            "subs:50000:none": filters.min_subscribers == 50000
            and filters.max_subscribers is None,
            "subs:none:none": filters.min_subscribers is None
            and filters.max_subscribers is None,
        }
    elif section == "fresh":
        options = [
            ("<= 1 день", "active:1"),
            ("<= 3 дня", "active:3"),
            ("<= 7 дней", "active:7"),
            ("<= 14 дней", "active:14"),
            ("<= 30 дней", "active:30"),
        ]
        selected = {
            f"active:{days}": filters.max_last_post_days == days
            for days in (1, 3, 7, 14, 30)
        }
    elif section == "views":
        options = [
            ("Любые", "views:none"),
            ("от 100", "views:100"),
            ("от 500", "views:500"),
            ("от 1k", "views:1000"),
            ("от 5k", "views:5000"),
            ("от 10k", "views:10000"),
        ]
        selected = {
            "views:none": filters.min_avg_views is None,
            "views:100": filters.min_avg_views == 100,
            "views:500": filters.min_avg_views == 500,
            "views:1000": filters.min_avg_views == 1000,
            "views:5000": filters.min_avg_views == 5000,
            "views:10000": filters.min_avg_views == 10000,
        }
    elif section == "score":
        options = [
            (">= 0", "scoremin:0"),
            (">= 20", "scoremin:20"),
            (">= 35", "scoremin:35"),
            (">= 50", "scoremin:50"),
            (">= 70", "scoremin:70"),
        ]
        selected = {
            f"scoremin:{value:g}": filters.min_activity_score == float(value)
            for value in (0, 20, 35, 50, 70)
        }
    elif section == "kind":
        options = [
            ("Тематические", "kind:thematic"),
            ("Коммерческие", "kind:commercial"),
            ("Любые", "kind:any"),
        ]
        selected = {
            f"kind:{value}": filters.channel_kind == value
            for value in VALID_CHANNEL_KINDS
        }
    elif section == "audience":
        options = [
            ("Женская", "audience:female"),
            ("Мужская", "audience:male"),
            ("Любая", "audience:any"),
        ]
        selected = {
            "audience:female": filters.audience_bias == "female",
            "audience:male": filters.audience_bias == "male",
            "audience:any": filters.audience_bias == "any",
        }
    elif section == "age":
        ages = sorted(VALID_AGES)
        options = [("Любой" if age == "any" else age, f"age:{age}") for age in ages]
        selected = {f"age:{age}": filters.age_group == age for age in ages}
    elif section == "sort":
        options = [
            ("Score", "sort:score"),
            ("Просмотры", "sort:views"),
            ("Реакции", "sort:reactions"),
            ("Комменты", "sort:comments"),
            ("Подписчики", "sort:subscribers"),
            ("Свежесть", "sort:fresh"),
        ]
        selected = {f"sort:{value}": filters.sort_by == value for value in VALID_SORTS}
    else:
        options = []
        selected = {}

    for title, callback_data in options:
        mark = "✓ " if selected.get(callback_data) else ""
        # Keep exact labels for tests when nothing is selected on that option
        builder.button(text=f"{mark}{title}", callback_data=callback_data)
    builder.button(text="К фильтрам", callback_data="filters:dashboard")
    builder.adjust(2)
    return builder.as_markup()


def filter_presets_keyboard(presets: list[FilterPreset]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📌 Сохранить с названием", callback_data="filterpreset:save:named"
    )
    builder.button(text="⚡ Быстро сохранить", callback_data="filterpreset:save:auto")
    for preset in presets:
        title = _short_button_title(preset.title)
        builder.button(
            text=f"✅ {title}", callback_data=f"filterpreset:apply:{preset.preset_id}"
        )
        builder.button(
            text=f"🗑 {title}", callback_data=f"filterpreset:delete:{preset.preset_id}"
        )
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(2 if presets else 1)
    return builder.as_markup()


def filter_preset_name_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚫 Отменить", callback_data="filterpreset:save:cancel")
    builder.adjust(1)
    return builder.as_markup()


def _short_button_title(title: str, *, limit: int = 26) -> str:
    if len(title) <= limit:
        return title
    return f"{title[: limit - 1]}…"


def _subs_label(filters: SearchFilters) -> str:
    if filters.min_subscribers is None and filters.max_subscribers is None:
        return "любые"
    if filters.min_subscribers is None:
        return f"до {_short_num(filters.max_subscribers)}"
    if filters.max_subscribers is None:
        return f"от {_short_num(filters.min_subscribers)}"
    return (
        f"{_short_num(filters.min_subscribers)}-{_short_num(filters.max_subscribers)}"
    )


def _fresh_label(filters: SearchFilters) -> str:
    return f"<= {filters.max_last_post_days} дн."


def _views_label(filters: SearchFilters) -> str:
    if filters.min_avg_views is None:
        return "любые"
    return f"от {_short_num(filters.min_avg_views)}"


def _score_label(filters: SearchFilters) -> str:
    return f">= {filters.min_activity_score:g}"


def _channel_kind_label(filters: SearchFilters) -> str:
    return {
        "thematic": "тематические",
        "commercial": "коммерческие",
        "any": "любые",
    }.get(filters.channel_kind, filters.channel_kind)


def _audience_label(filters: SearchFilters) -> str:
    return {
        "female": "женская",
        "male": "мужская",
        "any": "любая",
    }.get(filters.audience_bias, filters.audience_bias)


def _age_label(filters: SearchFilters) -> str:
    return "любой" if filters.age_group == "any" else filters.age_group


def _sort_label(filters: SearchFilters) -> str:
    return filters.sort_by


def scan_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💾 Завершить досрочно", callback_data="scan:finish")
    builder.button(text="🚫 Отмена", callback_data="scan:cancel")
    builder.adjust(1)
    return builder.as_markup()


# Keep aliases for clarity in discovery progress copy.
def discovery_control_keyboard() -> InlineKeyboardMarkup:
    return scan_cancel_keyboard()


def results_keyboard(has_results: bool) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    if has_results:
        builder.button(text="📥 Скачать CSV", callback_data="export:last")
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="🧻 Парсинг", callback_data="parsing")
    builder.button(text="🗂 История", callback_data="history")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def history_keyboard(has_scans: bool) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    if has_scans:
        builder.button(text="📥 Скачать последний CSV", callback_data="export:last")
        builder.button(text="📋 Последние результаты", callback_data="latest")
    builder.button(text="🧻 Парсинг", callback_data="parsing")
    builder.button(text="⚙️ Фильтры", callback_data="filters")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def _short_num(value: int | None) -> str:
    if value is None:
        return "?"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:g}m"
    if abs(value) >= 1_000:
        return f"{value // 1_000}k" if value % 1_000 == 0 else f"{value / 1_000:.1f}k"
    return str(value)


def format_discover_wizard_posts_step(wizard: DiscoverWizard) -> str:
    return (
        "🔗 Discovery · шаг 2/4\n\n"
        f"Канал: {wizard.identifier}\n"
        "Сколько последних постов смотреть?"
    )


def format_discover_wizard_subs_step(wizard: DiscoverWizard) -> str:
    return (
        "🔗 Discovery · шаг 3/4\n\n"
        f"Канал: {wizard.identifier}\n"
        f"Посты: {wizard.post_limit}\n\n"
        "Выбери диапазон подписчиков (жёсткий фильтр выдачи):"
    )


def format_discover_wizard_sources_step(wizard: DiscoverWizard) -> str:
    return (
        "🔗 Discovery · шаг 4/4\n\n"
        f"Канал: {wizard.identifier}\n"
        f"Посты: {wizard.post_limit}\n"
        f"ПДП: {_wizard_subs_label(wizard)}\n\n"
        "Источники (нажми, чтобы вкл/выкл):\n"
        f"• Комменты: {_on_off_ru(wizard.include_comment_links)}\n"
        f"• Профиль: {_on_off_ru(wizard.include_profile_refs)}\n"
        f"• Подарки: {_on_off_ru(wizard.include_gifts)}\n\n"
        "Когда готово — «Старт»."
    )


def _wizard_subs_label(wizard: DiscoverWizard) -> str:
    if wizard.min_subscribers is None and wizard.max_subscribers is None:
        return "любые"
    if wizard.min_subscribers is None:
        return f"до {_short_num(wizard.max_subscribers)}"
    if wizard.max_subscribers is None:
        return f"от {_short_num(wizard.min_subscribers)}"
    return f"{_short_num(wizard.min_subscribers)}-{_short_num(wizard.max_subscribers)}"


def _on_off_ru(value: bool) -> str:
    return "вкл" if value else "выкл"


def discover_wizard_posts_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in (50, 100, 200, 300, 500):
        builder.button(text=str(n), callback_data=f"dw:posts:{n}")
    builder.button(text="Custom", callback_data="dw:posts:custom")
    builder.button(text="🚫 Отмена", callback_data="input:cancel")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def discover_wizard_subs_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = [
        ("100-300", "100:300"),
        ("300-1k", "300:1000"),
        ("1k-5k", "1000:5000"),
        ("5k-20k", "5000:20000"),
        ("20k-50k", "20000:50000"),
        ("От 50k", "50000:none"),
        ("Любые", "none:none"),
        ("Custom", "custom"),
    ]
    for title, raw in options:
        builder.button(text=title, callback_data=f"dw:subs:{raw}")
    builder.button(text="🚫 Отмена", callback_data="input:cancel")
    builder.adjust(2)
    return builder.as_markup()


def discover_wizard_sources_keyboard(wizard: DiscoverWizard) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'✓ ' if wizard.include_comment_links else ''}Комменты",
        callback_data="dw:src:comments",
    )
    builder.button(
        text=f"{'✓ ' if wizard.include_profile_refs else ''}Профиль",
        callback_data="dw:src:profile",
    )
    builder.button(
        text=f"{'✓ ' if wizard.include_gifts else ''}Подарки",
        callback_data="dw:src:gifts",
    )
    builder.button(text="🚀 Старт", callback_data="dw:src:start")
    builder.button(text="🚫 Отмена", callback_data="input:cancel")
    builder.adjust(1)
    return builder.as_markup()


def _parse_subs_callback(raw: str) -> tuple[int | None, int | None]:
    left, right = raw.split(":", 1)
    min_s = None if left == "none" else int(left)
    max_s = None if right == "none" else int(right)
    return min_s, max_s


def _parse_subs_freeform(text: str) -> tuple[int | None, int | None]:
    cleaned = text.strip().lower().replace("—", "-").replace("–", "-")
    if cleaned in {"any", "all", "любые", "любой"}:
        return None, None
    if "-" in cleaned and " " not in cleaned:
        left, right = cleaned.split("-", 1)
        return _parse_count(left, "subs"), _parse_count(right, "subs")
    parts = cleaned.replace(",", " ").split()
    numbers = []
    for part in parts:
        if part.isdigit() or (
            part.endswith("k") and part[:-1].replace(".", "", 1).isdigit()
        ):
            numbers.append(_parse_count(part, "subs"))
    if len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        return low, high
    if len(numbers) == 1:
        return numbers[0], None
    raise ValueError("Формат: 100 5000 или 100-5000 или any")


def build_results_page(
    storage: ChannelStorage,
    scan_id: str,
    *,
    page: int,
    user_id: int,
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    scan = storage.get_scan(scan_id, user_id=user_id)
    if scan is None:
        return None, None
    total = storage.count_reports(scan_id)
    page_size = RESULTS_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * page_size
    reports = storage.reports_page(scan_id, offset=offset, limit=page_size)
    ordinal = storage.scan_ordinal(scan_id)
    text = format_compact_results_page(
        ordinal=ordinal,
        source_label=source_label_from_scan(scan),
        total_reports=total,
        page=page,
        page_size=page_size,
        reports=reports,
    )
    return text, results_page_keyboard(
        scan_id, page=page, total_pages=total_pages, has_results=total > 0
    )


def results_page_keyboard(
    scan_id: str,
    *,
    page: int,
    total_pages: int,
    has_results: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if page < total_pages:
        builder.button(
            text="След. ➡️", callback_data=f"results:page:{scan_id}:{page + 1}"
        )
    if page > 1:
        builder.button(
            text="⬅️ Пред.", callback_data=f"results:page:{scan_id}:{page - 1}"
        )
    if has_results:
        builder.button(text="📥 CSV", callback_data="export:last")
    builder.button(text="Удалить запись", callback_data=f"results:del:{scan_id}")
    builder.button(text="⬅️ Назад", callback_data="database")
    builder.button(text="🏠 В меню", callback_data="menu:main")
    # row layout: next/prev, csv+delete, back+menu
    if page < total_pages and page > 1:
        builder.adjust(2, 1, 1, 2)
    elif page < total_pages or page > 1:
        builder.adjust(1, 1, 1, 2)
    else:
        builder.adjust(1, 1, 2)
    return builder.as_markup()
