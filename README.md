# Бот для генерации ТЗ и критериев допуска

## Структура папок

```
tender_bot/
├── bot.py                  # Главный файл бота
├── agent.py                # Логика агента и Claude API
├── docx_generator.py       # Генерация Word-файлов
├── examples_loader.py      # Чтение примеров из папок
├── voice_handler.py        # Распознавание голоса
├── requirements.txt        # Зависимости Python
├── Procfile                # Команда запуска для Railway
├── runtime.txt             # Версия Python для Railway
├── .env.example            # Шаблон переменных окружения
└── examples/
    ├── cleaning/           # ← Сюда ТЗ по клинингу (.docx)
    ├── it/                 # ← Сюда ТЗ по IT (.docx)
    ├── repair/             # ← Сюда ТЗ по ремонту (.docx)
    └── criteria/           # ← Сюда критерии допуска (.docx)
```

## Нужные ключи (токены)

1. TELEGRAM_BOT_TOKEN — от @BotFather в Telegram
2. ANTHROPIC_API_KEY — от console.anthropic.com
3. OPENAI_API_KEY — от platform.openai.com (для голоса)

## Запуск локально (для теста)

```
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=...
set ANTHROPIC_API_KEY=...
set OPENAI_API_KEY=...
python bot.py
```

## Деплой на Railway (постоянная работа 24/7)

Подробная инструкция в чате с Claude.
