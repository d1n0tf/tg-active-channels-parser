from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ChannelsParser.collector import TelegramChannelCollector
from ChannelsParser.commands import SET_HELP, VALID_AGES, VALID_SORTS, apply_set_command, parse_queries
from ChannelsParser.config import AppSettings, ConfigError
from ChannelsParser.formatting import (
    format_filter_presets,
    format_filters,
    format_report,
    format_reports,
    format_scan_done,
    format_scan_history,
    reports_to_csv,
)
from ChannelsParser.models import FilterPreset, SearchFilters
from ChannelsParser.presets import QUERY_PRESETS, get_preset
from ChannelsParser.scoring import matches_filters
from ChannelsParser.storage import ChannelStorage


class BotState:
    def __init__(self, storage: ChannelStorage) -> None:
        self._storage = storage
        self.scan_lock = asyncio.Lock()
        self._pending_filter_preset_titles: set[int] = set()

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

    def request_filter_preset_title(self, user_id: int) -> None:
        self._pending_filter_preset_titles.add(user_id)

    def is_waiting_for_filter_preset_title(self, user_id: int) -> bool:
        return user_id in self._pending_filter_preset_titles

    def clear_filter_preset_title_request(self, user_id: int) -> None:
        self._pending_filter_preset_titles.discard(user_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
        bot = Bot(settings.bot_token or "")
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

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not message.from_user:
            return
        storage.save_user_filters(message.from_user.id, state.filters(message.from_user.id))
        text = (
            "Я ищу активные Telegram-каналы для закупки рекламы.\n\n"
            "Начни с готового поиска или настрой фильтры под закуп.\n"
            "Свои запросы можно запускать так: /find семейный бюджет, финансы для женщин\n\n"
            "Пол и возраст здесь оценочные: Telegram публично не раскрывает демографию каналов."
        )
        await message.answer(text, reply_markup=main_keyboard())

    @router.message(Command("help"))
    async def help_message(message: Message) -> None:
        await message.answer(
            "/find запрос1, запрос2 - поиск каналов\n"
            "/check @channel - аудит конкретного канала\n"
            "/filters - панель настройки фильтров\n"
            "/savefilter Название - сохранить текущие фильтры в свой пресет\n"
            "/filterpresets - мои пресеты фильтров\n"
            "/set - точная настройка фильтров\n"
            "/presets - готовые наборы запросов\n"
            "/latest - последние найденные каналы\n"
            "/history - история сканов\n"
            "/export - CSV последнего скана\n\n"
            f"{SET_HELP}"
        )

    @router.message(Command("filters"))
    async def filters_message(message: Message) -> None:
        if not message.from_user:
            return
        filters = state.filters(message.from_user.id)
        await message.answer(format_filter_dashboard(filters), reply_markup=filters_keyboard(filters))

    @router.message(Command("set"))
    async def set_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        try:
            filters, confirmation = apply_set_command(state.filters(message.from_user.id), command.args or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        state.update_filters(message.from_user.id, filters)
        await message.answer(f"{confirmation}\n\n{format_filter_dashboard(filters)}", reply_markup=filters_keyboard(filters))

    @router.message(Command("reset"))
    async def reset_message(message: Message) -> None:
        if not message.from_user:
            return
        filters = state.reset_filters(message.from_user.id)
        await message.answer(f"Фильтры сброшены.\n\n{format_filter_dashboard(filters)}", reply_markup=filters_keyboard(filters))

    @router.message(Command("presets"))
    async def presets_message(message: Message) -> None:
        await message.answer("Готовые наборы запросов:", reply_markup=presets_keyboard())

    @router.message(Command("savefilter", "savefilters"))
    async def save_filter_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        try:
            preset = state.save_filter_preset(message.from_user.id, command.args or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(
            f"Сохранил пресет фильтров: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filter_presets_keyboard(state.filter_presets(message.from_user.id)),
        )

    @router.message(Command("filterpresets", "filterpreset"))
    async def filter_presets_message(message: Message) -> None:
        if not message.from_user:
            return
        presets = state.filter_presets(message.from_user.id)
        await message.answer(format_filter_presets(presets), reply_markup=filter_presets_keyboard(presets))

    @router.message(Command("latest"))
    async def latest_message(message: Message) -> None:
        if not message.from_user:
            return
        reports = storage.latest_reports(user_id=message.from_user.id, limit=settings.top_results)
        await answer_long(message, format_reports(reports, limit=settings.top_results), reply_markup=results_keyboard(bool(reports)))

    @router.message(Command("history"))
    async def history_message(message: Message) -> None:
        if not message.from_user:
            return
        scans = storage.list_scans(user_id=message.from_user.id, limit=10)
        await message.answer(format_scan_history(scans), reply_markup=history_keyboard(bool(scans)))

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
            await message.answer(
                "Дай ключевые слова после команды, например:\n"
                "/find семейный бюджет, финансы для женщин, деньги в декрете"
            )
            return
        await run_scan(message, queries, state, collector, storage, settings, user_id=message.from_user.id)

    @router.message(Command("check"))
    async def check_message(message: Message, command: CommandObject) -> None:
        if not message.from_user:
            return
        identifier = (command.args or "").strip()
        if not identifier:
            await message.answer("Укажи канал: /check @channel или /check https://t.me/channel")
            return
        await run_audit(message, identifier, state, collector, storage, user_id=message.from_user.id)

    @router.message(F.text)
    async def pending_filter_preset_title_message(message: Message) -> None:
        if not message.from_user or not message.text:
            return
        user_id = message.from_user.id
        if not state.is_waiting_for_filter_preset_title(user_id):
            return
        if message.text.startswith("/"):
            state.clear_filter_preset_title_request(user_id)
            await message.answer("Ок, сохранение пресета отменено.", reply_markup=filters_keyboard(state.filters(user_id)))
            return
        try:
            preset = state.save_filter_preset(user_id, message.text)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=filter_preset_name_keyboard())
            return
        state.clear_filter_preset_title_request(user_id)
        presets = state.filter_presets(user_id)
        await message.answer(
            f"Сохранил пресет фильтров: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filter_presets_keyboard(presets),
        )

    @router.callback_query(F.data == "filters")
    async def filters_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.filters(callback.from_user.id)
        await message.answer(format_filter_dashboard(filters), reply_markup=filters_keyboard(filters))
        await callback.answer()

    @router.callback_query(F.data == "filters:dashboard")
    async def filters_dashboard_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.filters(callback.from_user.id)
        await message.edit_text(format_filter_dashboard(filters), reply_markup=filters_keyboard(filters))
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
            await callback.answer("Раздел не найден", show_alert=True)
            return
        await message.edit_text(text, reply_markup=keyboard)
        await callback.answer()

    @router.callback_query(F.data == "presets")
    async def presets_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if message is None:
            return
        await message.answer("Готовые наборы запросов:", reply_markup=presets_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "filterpresets")
    async def filter_presets_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        presets = state.filter_presets(callback.from_user.id)
        await message.answer(format_filter_presets(presets), reply_markup=filter_presets_keyboard(presets))
        await callback.answer()

    @router.callback_query(F.data == "filterpreset:save:auto")
    async def filter_preset_save_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        preset = state.save_filter_preset(callback.from_user.id, _auto_filter_preset_title())
        presets = state.filter_presets(callback.from_user.id)
        await message.edit_text(
            f"Сохранил пресет фильтров: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filter_presets_keyboard(presets),
        )
        await callback.answer("Пресет сохранен")

    @router.callback_query(F.data == "filterpreset:save:named")
    async def filter_preset_save_named_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        state.request_filter_preset_title(callback.from_user.id)
        await message.edit_text(
            "Как назвать пресет фильтров?\n\nНапиши название одним сообщением, например: Малые женские 100-300.",
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
        await message.edit_text(format_filter_dashboard(filters), reply_markup=filters_keyboard(filters))
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
            await callback.answer("Пресет не найден", show_alert=True)
            return
        await message.edit_text(
            f"Применил пресет фильтров: {preset.title}\n\n{format_filter_dashboard(preset.filters)}",
            reply_markup=filters_keyboard(preset.filters),
        )
        await callback.answer("Фильтры применены")

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
            await callback.answer("Пресет не найден", show_alert=True)
            return
        presets = state.filter_presets(callback.from_user.id)
        await message.edit_text(format_filter_presets(presets), reply_markup=filter_presets_keyboard(presets))
        await callback.answer("Пресет удален")

    @router.callback_query(F.data.startswith("preset:"))
    async def preset_scan(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        preset_key = callback.data.split(":", 1)[1]
        preset = get_preset(preset_key)
        if not preset:
            await callback.answer("Пресет не найден", show_alert=True)
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
        reports = storage.latest_reports(user_id=callback.from_user.id, limit=settings.top_results)
        await answer_long(message, format_reports(reports, limit=settings.top_results), reply_markup=results_keyboard(bool(reports)))
        await callback.answer()

    @router.callback_query(F.data == "history")
    async def history_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        scans = storage.list_scans(user_id=callback.from_user.id, limit=10)
        await message.answer(format_scan_history(scans), reply_markup=history_keyboard(bool(scans)))
        await callback.answer()

    @router.callback_query(F.data == "export:last")
    async def export_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        await export_latest(message, storage, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data == "filters:reset")
    async def filters_reset_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None:
            return
        filters = state.reset_filters(callback.from_user.id)
        await message.edit_text(f"Фильтры сброшены.\n\n{format_filter_dashboard(filters)}", reply_markup=filters_keyboard(filters))
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
            await callback.answer("Некорректный фильтр подписчиков", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), min_subscribers=min_value, max_subscribers=max_value)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("subs", filters), reply_markup=filter_section_keyboard("subs", filters))
        await callback.answer("Фильтр подписчиков обновлен")

    @router.callback_query(F.data.startswith("active:"))
    async def active_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            days = int(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректный фильтр свежести", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), max_last_post_days=days)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("fresh", filters), reply_markup=filter_section_keyboard("fresh", filters))
        await callback.answer("Фильтр свежести обновлен")

    @router.callback_query(F.data.startswith("views:"))
    async def views_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            raw_value = callback.data.split(":")[1]
            min_views = None if raw_value == "none" else int(raw_value)
        except (IndexError, ValueError):
            await callback.answer("Некорректный фильтр просмотров", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), min_avg_views=min_views)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("views", filters), reply_markup=filter_section_keyboard("views", filters))
        await callback.answer("Фильтр просмотров обновлен")

    @router.callback_query(F.data.startswith("scoremin:"))
    async def score_min_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = float(callback.data.split(":")[1])
        except (IndexError, ValueError):
            await callback.answer("Некорректный порог активности", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), min_activity_score=value)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("score", filters), reply_markup=filter_section_keyboard("score", filters))
        await callback.answer("Порог активности обновлен")

    @router.callback_query(F.data.startswith("audience:"))
    async def audience_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректный фильтр аудитории", show_alert=True)
            return
        if value not in {"any", "female", "male"}:
            await callback.answer("Некорректный фильтр аудитории", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), audience_bias=value)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("audience", filters), reply_markup=filter_section_keyboard("audience", filters))
        await callback.answer("Фильтр аудитории обновлен")

    @router.callback_query(F.data.startswith("age:"))
    async def age_callback(callback: CallbackQuery) -> None:
        message = _callback_message(callback)
        if not callback.from_user or message is None or not callback.data:
            return
        try:
            value = callback.data.split(":")[1]
        except IndexError:
            await callback.answer("Некорректный фильтр возраста", show_alert=True)
            return
        if value not in VALID_AGES:
            await callback.answer("Некорректный фильтр возраста", show_alert=True)
            return
        filters = replace(state.filters(callback.from_user.id), age_group=value)
        state.update_filters(callback.from_user.id, filters)
        await message.edit_text(format_filter_section("age", filters), reply_markup=filter_section_keyboard("age", filters))
        await callback.answer("Фильтр возраста обновлен")

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
        await message.edit_text(format_filter_section("sort", filters), reply_markup=filter_section_keyboard("sort", filters))
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
        await message.answer("Сейчас уже идет поиск. Дождись результата и запусти следующий.")
        return

    await state.scan_lock.acquire()
    try:
        filters = state.filters(user_id)
        scan_id = uuid.uuid4().hex
        storage.create_scan(scan_id, user_id=user_id, mode="search", queries=queries, filters=filters)

        query_preview = ", ".join(queries[:5])
        if len(queries) > 5:
            query_preview += f" и еще {len(queries) - 5}"
        label = f"{title}\n" if title else ""
        await message.answer(f"{label}Запустил поиск: {query_preview}\n\n{format_filters(filters)}")

        try:
            result = await collector.search_channels(queries, filters)
            reports = result.reports
            storage.save_reports(scan_id, reports)
            storage.finish_scan(scan_id, total_candidates=result.total_candidates, total_reports=len(reports))
        except Exception as exc:
            storage.fail_scan(scan_id, error=str(exc))
            await message.answer(f"Поиск упал: {exc}\nscan_id: {scan_id[:8]}")
            return

        summary = format_scan_done(scan_id, result.total_candidates, len(reports), result.errors)
        await answer_long(
            message,
            f"{summary}\n\n{format_reports(reports, limit=settings.top_results)}",
            reply_markup=results_keyboard(bool(reports)),
        )
    finally:
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
        await message.answer("Сейчас уже идет поиск. Дождись результата и запусти следующий.")
        return

    await state.scan_lock.acquire()
    try:
        filters = state.filters(user_id)
        scan_id = uuid.uuid4().hex
        storage.create_scan(scan_id, user_id=user_id, mode="audit", queries=[identifier], filters=filters)
        await message.answer(f"Проверяю канал {identifier}...")

        try:
            report = await collector.inspect_channel_identifier(identifier)
            storage.save_reports(scan_id, [report])
            storage.finish_scan(scan_id, total_candidates=1, total_reports=1)
        except Exception as exc:
            storage.fail_scan(scan_id, error=str(exc), total_candidates=1)
            await message.answer(f"Не смог проверить канал: {exc}\nscan_id: {scan_id[:8]}")
            return

        filter_status = "Проходит текущие фильтры" if matches_filters(report, filters) else "Не проходит текущие фильтры"
        await message.answer(f"{filter_status}\nscan_id: {scan_id[:8]}\n\n{format_report(report)}", reply_markup=results_keyboard(True))
    finally:
        state.scan_lock.release()


async def export_latest(message: Message, storage: ChannelStorage, user_id: int) -> None:
    scan_id = storage.latest_scan_id(user_id=user_id, only_done=True, require_reports=True)
    if scan_id is None:
        await message.answer("Пока нечего экспортировать. Сначала запусти поиск или /check.")
        return
    reports = storage.latest_reports(scan_id=scan_id, limit=500)
    if not reports:
        await message.answer("Пока нечего экспортировать. Сначала запусти поиск или /check.")
        return
    payload = reports_to_csv(reports)
    await message.answer_document(
        BufferedInputFile(payload, filename=f"telegram_channels_{scan_id[:8]}.csv"),
        caption="CSV с последними результатами",
    )


def _callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def _callback_int(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    try:
        return int(data[len(prefix) :])
    except ValueError:
        return None


def _auto_filter_preset_title() -> str:
    return datetime.now().strftime("Фильтр %d.%m %H:%M:%S")


async def answer_long(message: Message, text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    chunks = split_text(text)
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)


def split_text(text: str, *, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_block(block, limit=limit))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
    if current:
        chunks.append(current)
    return chunks


def _split_long_block(block: str, *, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in block.splitlines():
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(line[index : index + limit] for index in range(0, len(line), limit))
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks


def format_filter_dashboard(filters: SearchFilters) -> str:
    return (
        "Фильтры поиска\n\n"
        f"Подписчики: {_subs_label(filters)}\n"
        f"Последний пост: {_fresh_label(filters)}\n"
        f"Просмотры: {_views_label(filters)}\n"
        f"Score: {_score_label(filters)}\n"
        f"Аудитория: {_audience_label(filters)}\n"
        f"Возраст: {_age_label(filters)}\n"
        f"Сортировка: {_sort_label(filters)}"
    )


def format_filter_section(section: str, filters: SearchFilters) -> str:
    titles = {
        "subs": ("Подписчики", _subs_label(filters)),
        "fresh": ("Свежесть постов", _fresh_label(filters)),
        "views": ("Средние просмотры", _views_label(filters)),
        "score": ("Активность", _score_label(filters)),
        "audience": ("Аудитория", _audience_label(filters)),
        "age": ("Возраст", _age_label(filters)),
        "sort": ("Сортировка", _sort_label(filters)),
    }
    try:
        title, value = titles[section]
    except KeyError as exc:
        raise ValueError(f"Unknown filter section: {section}") from exc
    return f"{title}\n\nСейчас: {value}"


def main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Готовый поиск: финансы + женская ЦА", callback_data="preset:finance_female")
    builder.button(text="Все готовые поиски", callback_data="presets")
    builder.button(text="Настроить фильтры", callback_data="filters")
    builder.button(text="Мои фильтр-пресеты", callback_data="filterpresets")
    builder.button(text="Последние результаты", callback_data="latest")
    builder.button(text="История", callback_data="history")
    builder.adjust(1)
    return builder.as_markup()


def presets_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for preset in QUERY_PRESETS.values():
        builder.button(text=preset.title, callback_data=f"preset:{preset.key}")
    builder.button(text="Фильтры", callback_data="filters")
    builder.button(text="Мои фильтр-пресеты", callback_data="filterpresets")
    builder.adjust(1)
    return builder.as_markup()


def filters_keyboard(filters: SearchFilters) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Подписчики: {_subs_label(filters)}", callback_data="filters:section:subs")
    builder.button(text=f"Посты: {_fresh_label(filters)}", callback_data="filters:section:fresh")
    builder.button(text=f"Просмотры: {_views_label(filters)}", callback_data="filters:section:views")
    builder.button(text=f"Score: {_score_label(filters)}", callback_data="filters:section:score")
    builder.button(text=f"ЦА: {_audience_label(filters)}", callback_data="filters:section:audience")
    builder.button(text=f"Возраст: {_age_label(filters)}", callback_data="filters:section:age")
    builder.button(text=f"Сортировка: {_sort_label(filters)}", callback_data="filters:section:sort")
    builder.button(text="Мои пресеты фильтров", callback_data="filterpresets")
    builder.button(text="Сохранить как пресет", callback_data="filterpreset:save:named")
    builder.button(text="Сбросить фильтры", callback_data="filters:reset")
    builder.adjust(2, 2, 2, 1, 2, 1)
    return builder.as_markup()


def filter_section_keyboard(section: str, filters: SearchFilters) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    adjust_pattern: tuple[int, ...]
    if section == "subs":
        _add_option_buttons(
            builder,
            [
                ("100-300", "subs:100:300"),
                ("300-1k", "subs:300:1000"),
                ("1k-5k", "subs:1000:5000"),
                ("5k-20k", "subs:5000:20000"),
                ("От 20k", "subs:20000:none"),
                ("Любые", "subs:none:none"),
            ],
        )
        adjust_pattern = (2, 2, 2, 1)
    elif section == "fresh":
        _add_option_buttons(
            builder,
            [
                ("<= 1 день", "active:1"),
                ("<= 3 дня", "active:3"),
                ("<= 7 дней", "active:7"),
                ("<= 14 дней", "active:14"),
                ("<= 30 дней", "active:30"),
            ],
        )
        adjust_pattern = (2, 2, 1, 1)
    elif section == "views":
        _add_option_buttons(
            builder,
            [
                ("Любые", "views:none"),
                (">= 50", "views:50"),
                (">= 100", "views:100"),
                (">= 500", "views:500"),
                (">= 1000", "views:1000"),
            ],
        )
        adjust_pattern = (2, 2, 1, 1)
    elif section == "score":
        _add_option_buttons(
            builder,
            [
                (">= 0", "scoremin:0"),
                (">= 25", "scoremin:25"),
                (">= 35", "scoremin:35"),
                (">= 50", "scoremin:50"),
                (">= 70", "scoremin:70"),
            ],
        )
        adjust_pattern = (2, 2, 1, 1)
    elif section == "audience":
        _add_option_buttons(
            builder,
            [
                ("Женская", "audience:female"),
                ("Мужская", "audience:male"),
                ("Любая", "audience:any"),
            ],
        )
        adjust_pattern = (2, 1, 1)
    elif section == "age":
        _add_option_buttons(
            builder,
            [
                ("Любой", "age:any"),
                ("14-17", "age:14-17"),
                ("18-24", "age:18-24"),
                ("25-34", "age:25-34"),
                ("35+", "age:35+"),
            ],
        )
        adjust_pattern = (2, 2, 1, 1)
    elif section == "sort":
        _add_option_buttons(
            builder,
            [
                ("Score", "sort:score"),
                ("Просмотры", "sort:views"),
                ("Реакции", "sort:reactions"),
                ("Комментарии", "sort:comments"),
                ("Подписчики", "sort:subscribers"),
                ("Свежесть", "sort:fresh"),
            ],
        )
        adjust_pattern = (2, 2, 2, 1)
    else:
        raise ValueError(f"Unknown filter section: {section}")
    builder.button(text="К фильтрам", callback_data="filters:dashboard")
    builder.adjust(*adjust_pattern)
    return builder.as_markup()


def filter_presets_keyboard(presets: list[FilterPreset]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Сохранить с названием", callback_data="filterpreset:save:named")
    builder.button(text="Быстро сохранить", callback_data="filterpreset:save:auto")
    for preset in presets:
        title = _short_button_title(preset.title)
        builder.button(text=f"Применить: {title}", callback_data=f"filterpreset:apply:{preset.preset_id}")
        builder.button(text=f"Удалить: {title}", callback_data=f"filterpreset:delete:{preset.preset_id}")
    builder.button(text="Фильтры", callback_data="filters")
    builder.adjust(1)
    return builder.as_markup()


def filter_preset_name_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отменить", callback_data="filterpreset:save:cancel")
    return builder.as_markup()


def _add_option_buttons(builder: InlineKeyboardBuilder, options: list[tuple[str, str]]) -> None:
    for text, callback_data in options:
        builder.button(text=text, callback_data=callback_data)


def _short_button_title(title: str, *, limit: int = 34) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 3].rstrip() + "..."


def _subs_label(filters: SearchFilters) -> str:
    if filters.min_subscribers is None and filters.max_subscribers is None:
        return "любые"
    if filters.min_subscribers is None:
        return f"до {_short_num(filters.max_subscribers)}"
    if filters.max_subscribers is None:
        return f"от {_short_num(filters.min_subscribers)}"
    return f"{_short_num(filters.min_subscribers)}-{_short_num(filters.max_subscribers)}"


def _fresh_label(filters: SearchFilters) -> str:
    return f"<= {filters.max_last_post_days} дн."


def _views_label(filters: SearchFilters) -> str:
    if filters.min_avg_views is None:
        return "любые"
    return f">= {_short_num(filters.min_avg_views)}"


def _score_label(filters: SearchFilters) -> str:
    return f">= {filters.min_activity_score:.0f}"


def _audience_label(filters: SearchFilters) -> str:
    labels = {
        "female": "женская",
        "male": "мужская",
        "any": "любая",
    }
    return labels.get(filters.audience_bias, filters.audience_bias)


def _age_label(filters: SearchFilters) -> str:
    return "любой" if filters.age_group == "any" else filters.age_group


def _sort_label(filters: SearchFilters) -> str:
    labels = {
        "score": "score",
        "views": "просмотры",
        "reactions": "реакции",
        "comments": "комменты",
        "subscribers": "подписчики",
        "fresh": "свежесть",
    }
    return labels.get(filters.sort_by, filters.sort_by)


def _short_num(value: int | None) -> str:
    if value is None:
        return "?"
    if value >= 1000 and value % 1000 == 0:
        return f"{value // 1000}k"
    return str(value)


def results_keyboard(has_results: bool) -> InlineKeyboardMarkup | None:
    if not has_results:
        return main_keyboard()
    builder = InlineKeyboardBuilder()
    builder.button(text="Экспорт CSV", callback_data="export:last")
    builder.button(text="История", callback_data="history")
    builder.button(text="Фильтры", callback_data="filters")
    builder.button(text="Новый поиск по финансам", callback_data="preset:finance_female")
    builder.adjust(1)
    return builder.as_markup()


def history_keyboard(has_scans: bool) -> InlineKeyboardMarkup | None:
    if not has_scans:
        return main_keyboard()
    builder = InlineKeyboardBuilder()
    builder.button(text="Экспорт последнего CSV", callback_data="export:last")
    builder.button(text="Последние результаты", callback_data="latest")
    builder.button(text="Фильтры", callback_data="filters")
    builder.adjust(1)
    return builder.as_markup()
