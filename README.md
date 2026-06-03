# Video Library

Самохостное веб-приложение для управления видеотекой с сортировкой по тегам, автоматическим транскодированием и виртуальными папками.

Проверялся запуск только через Docker.

## Быстрый старт

```bash
git clone https://github.com/VoidEmptiness/Video-Library.git && cd Video-Library && docker compose up -d --build
```

Открыть: `http://localhost:7880` или `http://локальный ip компа/сервера:7880`

Логин/пароль по умолчанию: `admin` / `admin` Можно поменять в docker-compose.yml переменные ADMIN_USERNAME/ADMIN_PASSWORD

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `HOST_PORT` | `7880` | Порт на хосте |
| `DATABASE_URL` | `sqlite:////data/app.db` | Строка подключения к БД. Для PostgreSQL: `postgresql+psycopg://user:pass@host:5432/db` |
| `VIDEO_DIR` | `/data/videos` | Директория для видеофайлов |
| `THUMB_DIR` | `/data/thumbs` | Директория для миниатюр |
| `AUTH_ENABLED` | `true` | Включить/выключить авторизацию |
| `ADMIN_USERNAME` | `admin` | Логин |
| `ADMIN_PASSWORD` | `admin` | Пароль |
| `SECRET_KEY` | `change-me-in-production` | Ключ подписи сессионных токенов |
| `APP_TITLE` | `Video Library` | Название приложения в интерфейсе |
| `TRANSCODE_ENABLED` | `true` | Включить фоновое транскодирование в H.264 |
| `TRANSCODE_CRF` | `23` | CRF для FFmpeg (ниже = качественнее) |
| `TRANSCODE_PRESET` | `medium` | Пресет FFmpeg (`ultrafast`…`veryslow`) |
| `TRANSCODE_TIMEOUT_SECONDS` | `3600` | Таймаут транскодирования одного файла |
| `TRANSCODE_DOWNSCALE_HEIGHT` | `720` | Высота видео после транскодирования |
| `TRANSCODE_DOWNSCALE_MAX_HEIGHT` | `1080` | Порог высоты для запуска транскодирования |
| `TRANSCODE_DOWNSCALE_FPS` | `24` | Ограничение FPS для транскодированного видео |
| `THUMBNAIL_TIMEOUT_SECONDS` | `30` | Таймаут генерации миниатюры |

## Страницы

| Маршрут | Описание |
|---|---|
| `/` | Главная — сетка видео, загрузка, поиск и фильтр по тегам |
| `/videos/{id}` | Страница видео — плеер, метаданные, управление тегами |
| `/tags` | Управление тегами — создание и удаление |
| `/folders` | Виртуальные папки — создание/редактирование/удаление, вложенность |
| `/folders/{id}` | Просмотр папки — видео, соответствующие тегам папки |
| `/upload/tags?ids=...` | Назначение тегов свежезагруженным видео |
| `/settings` | Настройки приложения — включение/отключение транскодирования |

## Структура хранилища

```
/data/
├── videos/        # Загруженные видеофайлы
├── thumbs/        # Сгенерированные JPG-миниатюры
└── app.db         # SQLite (если не используется PostgreSQL)
```

## Структура проекта

```
Video-Library/
├── app/
│   ├── main.py              # FastAPI приложение и роуты
│   ├── models.py            # SQLAlchemy ORM модели
│   ├── database.py          # Настройка БД и сессий
│   ├── services/
│   │   ├── auth.py          # Сессии и авторизация
│   │   ├── storage.py       # Работа с файлами
│   │   ├── thumbnails.py    # Генерация миниатюр через FFmpeg
│   │   ├── transcoding.py   # Транскодирование в H.264
│   │   └── settings.py      # Настройки приложения (JSON)
│   ├── static/              # CSS, JS, favicon
│   └── templates/           # Jinja2 шаблоны
├── docker-compose.yml
├── Dockerfile
└── requirements.txt