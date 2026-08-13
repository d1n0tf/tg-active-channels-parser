## TG Active Channels Parser

Рабочий инструмент для поиска и первичного аудита активных публичных Telegram-каналов под закуп рекламы.

Проект состоит из:

- Telegram-бота на `aiogram`;
- сборщика каналов через пользовательский Telegram API (`Telethon`);
- SQLite-хранилища для фильтров, истории сканов и отчетов;
- CLI для запуска поиска/аудита без бота;
- CSV-экспорта результатов.

Важно: Telegram публично не отдает пол и возраст аудитории каналов. В проекте демография является эвристической оценкой по названию, описанию и ключевым словам. Для финального медиаплана это стоит проверять через владельца канала, рекламную статистику или внешние каталоги.

### Возможности

- поиск публичных broadcast-каналов по нескольким запросам;
- discovery от донорского канала через комментарии и bio комментаторов;
- пресеты для финансовой вертикали, женской ЦА и маркетплейсов;
- компактная панель фильтров по разделам: подписчики, свежесть, просмотры, score, тип канала, ЦА, возраст и сортировка;
- отсев личных блогов в дефолтном режиме `тематические`, чтобы искать каналы, магазины, агентства и сообщества;
- пользовательские пресеты фильтров с сохранением по имени, быстрым применением и удалением;
- сортировка по score, просмотрам, реакциям, комментариям, подписчикам или свежести;
- аудит конкретного канала через `/check @channel`;
- история сканов с сохранением фильтров;
- экспорт последнего результата в CSV;
- CLI для cron/server usage.

### Быстрый запуск

1. Установи зависимости:

```bash
uv sync --dev
```

2. Создай `.env`:

```bash
cp .env.example .env
```

3. Заполни `.env`:

- `BOT_TOKEN` - токен бота из BotFather;
- `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` - данные приложения с https://my.telegram.org;
- `TELEGRAM_PHONE` - номер аккаунта, который будет искать каналы;
- `DATABASE_PATH` - путь к SQLite-базе.
- `PROXY_URL` - опциональный прокси для aiogram и Telethon.

4. Авторизуй пользовательскую Telegram-сессию:

```bash
uv run tg-active-channels-login
```

5. Запусти бота:

```bash
uv run tg-active-channels-bot
```

### Прокси

Один общий прокси задается через `.env`:

```env
PROXY_URL=socks5://127.0.0.1:1080
```

Поддерживаются `http://`, `https://`, `socks4://`, `socks4a://`, `socks5://`, `socks5h://`, включая логин/пароль:

```env
PROXY_URL=http://user:pass@127.0.0.1:8080
```

Если Bot API и пользовательский Telegram API нужно развести по разным прокси:

```env
BOT_PROXY_URL=http://127.0.0.1:8080
TELEGRAM_PROXY_URL=socks5://127.0.0.1:1080
```

Для Shadowsocks запусти локальный клиент, например `ss-local`, и укажи его локальный SOCKS/HTTP endpoint:

```env
PROXY_URL=socks5://127.0.0.1:1080
```

`ss://...` напрямую не используется: ни `aiogram`, ни `Telethon` не работают с Shadowsocks URI как с транспортом без локального клиента.

### Команды бота

- `/start` - меню;
- `/find семейный бюджет, финансы для женщин` - поиск по своим ключам;
- `/discover @source_channel 200` - поиск каналов через комментарии донорского канала;
- `/discover @source_channel 200 comments off profile on gifts off subs 100 300` - discovery с выбором источников и диапазоном ПДП;
- `/check @channel` - аудит конкретного канала;
- `/filters` - панель фильтров с разделами;
- `/savefilter Малые женские` - сохранить текущие фильтры в свой пресет;
- `/filterpresets` - список своих пресетов фильтров;
- `/set subs 100 300` - точная настройка фильтров;
- `/reset` - сброс фильтров;
- `/presets` - готовые наборы запросов;
- `/latest` - последние результаты;
- `/history` - история сканов;
- `/export` - CSV последнего скана.

Примеры `/set`:

```text
/set subs 100 300
/set subs any
/set days 7
/set views 100
/set views any
/set score 35
/set type commercial
/set audience female
/set age 25-34
/set sort views
```

### Discovery Через Комментарии

Команда:

```text
/discover @source_channel 200
/discover @source_channel 200 comments off profile on gifts off subs 100 300
```

Бот берет последние `200` постов донорского канала, находит discussion-связку поста, просматривает комментарии, собирает ссылки `t.me/...` и `@username` из текста комментариев и bio комментаторов, personal channel из профиля, а также каналы, от имени которых оставляли комментарии. Если у комментатора не найден канал в bio или personal channel, бот дополнительно смотрит его публичные сохраненные подарки и добавляет кандидатом публичный канал, если подарок был отправлен от имени канала.

Источники можно включать и выключать: `comments on|off`, `profile on|off`, `gifts on|off`. Диапазон подписчиков задается отдельно для discovery: `subs 100 300`, `subs from 1000`, `subs to 50000` или `subs any`.

Для discovery используется широкий профиль фильтров: тематика, ЦА, возраст и средние просмотры не отсекают результат. Подписчики по умолчанию тоже любые, но если указан `subs`, применяется заданный диапазон. Остается фильтр активности: свежесть последнего поста и минимальный score, чтобы в список попадали любые живые каналы, включая личные каналы из профилей комментаторов.

Во время discovery появляется кнопка `Остановить поиск`: бот завершит обход на ближайшей безопасной точке и отдаст уже собранные результаты.

Лимиты нагрузки в `.env`:

```env
DISCOVERY_COMMENTS_PER_POST=100
DISCOVERY_PROFILE_LIMIT=500
DISCOVERY_CANDIDATE_LIMIT=300
DISCOVERY_GIFT_LIMIT=10
```

`DISCOVERY_GIFT_LIMIT` - сколько публичных подарков смотреть на один профиль без найденного канала. `0` полностью отключает проверку подарков.

### CLI

Поиск:

```bash
uv run tg-active-channels search "женская одежда" "магазин женской одежды" --subs-min 1000 --subs-max 50000 --channel-kind commercial --audience female --csv exports/channels.csv
```

Discovery от донорского канала:

```bash
uv run tg-active-channels discover @source_channel --posts 200 --comments-per-post 100 --profile-limit 500 --gift-limit 10 --csv exports/discovered.csv
```

У CLI для `discover` дефолты такие же широкие: `--channel-kind any`, `--audience any`, `--subs-min 0`, `--subs-max 0`. Если нужен жесткий диапазон, его можно передать явно.

Аудит одного канала:

```bash
uv run tg-active-channels check @channel_username --csv exports/channel.csv
```

История сканов:

```bash
uv run tg-active-channels history
```

Для CLI `--subs-min 0` или `--subs-max 0` отключает соответствующую границу.

### Score

Score считается из:

- свежести последнего поста;
- количества постов за 24 часа и 7 дней;
- среднего числа просмотров последних постов;
- реакций;
- комментариев.

По умолчанию фильтр ищет тематические каналы от 1000 до 50000 подписчиков, с последним постом не старше 7 дней и преимущественно женской оценочной аудиторией. Режим `Тип канала: тематические` отсекает личные дневники и блоги без явной темы, магазина, агентства или сообщества.

### Практический workflow для закупа

1. Выбери фильтр подписчиков, например `1k-5k`, `5k-20k` или `20k-50k`.
2. Оставь `Женская ЦА` и свежесть `Пост <= 7д`.
3. Запусти пресет `Женская одежда` или `Агентства и студии`.
4. Экспортируй CSV.
5. Перед закупом проверь понравившиеся каналы через `/check @channel`.
6. Запрашивай у админа статистику рекламного поста и демографию, потому что публичный API Telegram эти данные не раскрывает.

Панель `/filters` показывает текущие значения сразу на кнопках. Для магазинов одежды, агентств и студий можно поставить `Тип канала: коммерческие`. Чтобы не собирать фильтр заново, нажми `Сохранить как пресет`, отправь название одним сообщением и потом применяй набор через `/filterpresets`.

### Session reliability

The pool gives one scan an exclusive lease for one `.session`, and also holds a cross-process lock beside that file. Do not run a second bot instance, the login CLI, or another process against the same session file.

- long `FloodWait` puts the account into cooldown and continues the job on a free session;
- an unauthorized or revoked session is removed from the pool and the job switches to a spare when available;
- idle sessions are checked on `ACCOUNT_HEALTH_CHECK_SECONDS`; the result appears in My accounts; and
- completed scan reports are persisted before delivery; interrupted scans are marked on restart.

Create an encrypted **local** backup after adding or changing each session:

```bash
# SESSION_BACKUP_KEY belongs in protected environment configuration, never git
uv run tg-active-channels-backup create --name acc1
uv run tg-active-channels-backup verify --name acc1
uv run tg-active-channels-backup list
```

A backup protects against file loss, disk corruption and bad deployments. It does not restore a Telegram authorization that has been revoked server-side. Local restoration requires an explicit overwrite:

```bash
uv run tg-active-channels-backup restore --name acc1 --overwrite
```

#### Provisioning boundary

A separate metadata-only registry is prepared for future owner-controlled onboarding. It stores only an account ID, a masked phone hint, and flags for 2FA/recovery email -- never a full number, OTP, password, email credential, or session string.

```bash
uv run tg-active-channels-provision draft --name acc3 --phone-hint '+7999***1234' --has-2fa --has-recovery-email
uv run tg-active-channels-provision state --name acc3 --value awaiting_operator
uv run tg-active-channels-provision list
```

Actual authorization remains manual through `tg-active-channels-login`; the boundary is isolated so a future legitimate interactive provisioning flow can be added without changing the UI or scan runtime.

### Ограничения

- Ищутся только публичные каналы с username.
- Приватные каналы и invite-ссылки не поддерживаются.
- Пол/возраст - оценка, не факт из Telegram Ads или кабинета канала.
- При агрессивном поиске Telegram может вернуть `FloodWait`; короткие паузы бот переждет сам, длинные пропустит и запишет в историю скана.
