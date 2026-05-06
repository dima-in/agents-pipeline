# agents-pipeline

Русскоязычная версия основной документации проекта. Оригинальный [README.md](./README.md) остаётся каноническим источником, если между файлами появятся расхождения.

`agents-pipeline` — это каркас для переносимого multi-agent workflow разработки с прицелом на Windows.

Проект задуман так, чтобы жить в обычном Git-репозитории. Это позволяет:
- хранить саму систему в GitHub
- клонировать её на несколько Windows-устройств
- ставить зависимости повторяемо
- создавать и регистрировать агентов локально
- использовать тот же репозиторий для работы над другими проектами

## Что хранить в Git

Оставляйте в репозитории:
- Python-код
- конфигурацию workflow
- конфиги и промпты агентов
- тесты
- документацию
- `.env.example`

Не коммитьте:
- `venv/`
- `.openclaw/logs/`
- `.openclaw/feedback/`
- `.env`
- локальные кэши

## Структура

- `.openclaw/agents/` — промпты и конфиги агентов
- `.openclaw/config/` — локальные настройки агентов и инструментов
- `.openclaw/feedback/` — заметки QA и обратная связь для повторных запусков
- `.openclaw/logs/` — текстовые и JSON-логи сессий
- `workflow/` — логика оркестрации
- `tests/` — smoke-тесты

## Первый запуск на новом Windows-устройстве

1. Клонируйте репозиторий:

```powershell
git clone <YOUR_GITHUB_URL>
cd agents-pipeline
```

2. Создайте локальный файл окружения:

```powershell
Copy-Item .env.example .env
```

3. Установите зависимости:

```powershell
.\install.bat
```

4. Создайте набор базовых агентов:

```powershell
.\run.bat python manage_agents.py bootstrap
```

5. Если `openclaw` установлен и доступен, зарегистрируйте агентов:

```powershell
.\run.bat python manage_agents.py register-all
```

6. Запустите workflow:

```powershell
.\run.bat --mode interactive
```

## Ежедневная работа на нескольких Windows-устройствах

1. `git pull`
2. Работайте локально
3. `git add .`
4. `git commit -m "..."`
5. `git push`
6. На другом устройстве: `git pull`

## Управление агентами

Показать список агентов:

```powershell
.\run.bat python manage_agents.py list
```

Создать нового агента:

```powershell
.\run.bat python manage_agents.py create ux-reviewer research --description "Review UX and competitor patterns"
```

Зарегистрировать одного агента в `openclaw`:

```powershell
.\run.bat python manage_agents.py register ux-reviewer research
```

Пересоздать все базовые агенты по конфигу:

```powershell
.\run.bat python manage_agents.py bootstrap --force
```

## Примечание

Этот проект был собран из черновика потокового чата. Повреждённые или противоречивые фрагменты были приведены к цельной рабочей базе, а не скопированы буквально.
