# onec-config-mcp

MCP-сервер для поиска по исходному коду конфигураций 1С:Предприятие прямо из [Claude Code](https://claude.ai/code).

Индексирует выгрузки конфигураций (XML + BSL) в локальную SQLite-базу с полнотекстовым поиском (FTS5) и предоставляет 7 инструментов для поиска кода, объектов метаданных, процедур и функций.

## Возможности

- Полнотекстовый поиск по BSL-коду с поддержкой FTS5-синтаксиса (`AND`, `OR`, `NOT`, `"фраза"`)
- Поиск объектов метаданных по имени (общие модули, справочники, документы и т.д.)
- Получение полного кода модуля по имени объекта
- Поиск определения процедуры или функции с номером строки
- Автоматическая переиндексация при изменении файлов (watchdog)
- Поддержка нескольких конфигураций одновременно

## Требования

- Python 3.11+
- Выгруженные конфигурации 1С в формате XML+BSL (через [Конфигуратор](https://v8.1c.ru/platforma/) или [1C:EDT](https://edt.1c.ru/))

## Установка

```bash
git clone https://github.com/ASGDeveloper/onec-config-mcp
cd onec-config-mcp
pip install -e .
```

## Настройка

Отредактируйте `config.json`:

```json
{
  "db_path": "C:/Users/user/Documents/GitHub/onec-config-mcp/index.db",
  "configs": [
    {
      "name": "МояКонфигурация",
      "path": "C:/path/to/exported/config",
      "is_bsl": false,
      "watch": true
    },
    {
      "name": "БСП",
      "path": "C:/path/to/bsl-library",
      "is_bsl": true
    }
  ]
}
```

| Поле | Описание |
|------|----------|
| `db_path` | Путь к файлу базы данных SQLite (будет создан автоматически) |
| `name` | Имя конфигурации (используется как фильтр в инструментах) |
| `path` | Путь к корню выгруженной конфигурации |
| `is_bsl` | `true` — конфигурация является BSL-библиотекой (БСП) |
| `watch` | `true` — автоматически переиндексировать при изменении файлов |

**Важно:** `db_path` не должен находиться в `AppData\Local` — Claude Code работает в UWP-sandbox и перенаправляет этот путь. Используйте папку `Documents` или другое место.

## Индексирование

```bash
# Проиндексировать все конфигурации
python indexer.py

# Проиндексировать только одну конфигурацию
python indexer.py --only МояКонфигурация

# Показать статистику индекса
python indexer.py --stats
```

При повторном запуске данные конфигурации полностью перезаписываются.

## Подключение к Claude Code

Добавьте сервер в глобальный файл `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "onec-config-mcp": {
      "command": "python",
      "args": ["C:/path/to/onec-config-mcp/server.py"]
    }
  }
}
```

Перезапустите Claude Code. Сервер запустится автоматически и будет доступен во всех проектах.

Разрешения для проекта (`.claude/settings.local.json`):

```json
{
  "allowedTools": [
    "mcp__onec-config-mcp__search_code",
    "mcp__onec-config-mcp__find_object",
    "mcp__onec-config-mcp__get_module",
    "mcp__onec-config-mcp__list_objects",
    "mcp__onec-config-mcp__find_procedure",
    "mcp__onec-config-mcp__list_configs",
    "mcp__onec-config-mcp__get_object_metadata"
  ]
}
```

## Инструменты

### `search_code`
Полнотекстовый поиск по BSL-коду. Возвращает сниппеты с контекстом.

```
query       — текст или FTS5-выражение ("ПроверитьПрава" OR "CheckRights")
config_name — фильтр по конфигурации (опционально)
obj_type    — фильтр по типу объекта: CommonModules, Catalogs, Documents, ...
is_bsl      — true/false — фильтр по BSL-библиотеке
limit       — максимум результатов (по умолчанию 20)
```

### `find_procedure`
Найти определение процедуры или функции по имени. Возвращает файл и номер строки.

```
proc_name   — имя процедуры/функции
config_name — конфигурация (опционально)
```

### `get_module`
Получить полный код BSL-модуля. При размере >200 КБ — усекается с предупреждением.

```
obj_name    — имя объекта (например, Доки_Авторизация)
config_name — конфигурация (опционально)
module_type — Module / ObjectModule / ManagerModule / FormModule
form_name   — имя формы (при module_type=FormModule)
```

### `find_object`
Полнотекстовый поиск по именам объектов метаданных. Возвращает xml_summary с синонимом и флагами.

### `list_objects`
Список объектов по типу и/или конфигурации.

### `get_object_metadata`
Метаданные объекта: xml_summary и список модулей с количеством строк.

### `list_configs`
Показать проиндексированные конфигурации с датой и статистикой.

## Структура проекта

```
onec-config-mcp/
  server.py       # MCP-сервер (stdio transport) + watchdog
  indexer.py      # CLI для индексирования
  db.py           # Схема SQLite, FTS5-триггеры
  parser.py       # Парсер XML+BSL
  tools.py        # Обработчики MCP-инструментов
  config.json     # Конфигурация путей (не коммитить!)
  pyproject.toml  # Зависимости
```

## Поддерживаемые типы объектов

`CommonModules`, `Catalogs`, `Documents`, `DataProcessors`, `Reports`,
`InformationRegisters`, `AccumulationRegisters`, `AccountingRegisters`,
`BusinessProcesses`, `Tasks`, `ExchangePlans`, `CommonForms`, `Constants`,
`Enums`, `ChartOfCharacteristicTypes`, `ChartOfAccounts`, `ScheduledJobs`

## Логирование

Сервер пишет лог в `server.log` рядом с `server.py`. Там же отображаются события watchdog и ошибки переиндексации.

```bash
# Следить за логом в реальном времени
Get-Content server.log -Wait -Tail 20
```

## Лицензия

MIT
