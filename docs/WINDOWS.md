# Запуск Marketing Intelligence на Windows 10 и Windows 11

## Что понадобится

- Windows 10 или Windows 11;
- Python 3.11 или новее с сайта [python.org](https://www.python.org/downloads/);
- Яндекс Браузер, Google Chrome или Mozilla Firefox;
- подключение к интернету только для первой установки зависимостей.

При установке Python включите флажок **Add Python to PATH**.

## Подготовка окружения

Откройте PowerShell в папке проекта и выполните:

```powershell
python -m venv .venv
```

Если команда `python` не найдена, попробуйте `py` вместо `python`.

Активируйте окружение:

```powershell
.\.venv\Scripts\Activate.ps1
```

Если PowerShell запрещает запуск сценария, разрешите его только для текущего окна и повторите активацию:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Установка зависимостей

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

## Запуск приложения

Одна команда запуска:

```powershell
python -m uvicorn marketing_intelligence.main:app --host 127.0.0.1 --port 8000
```

Откройте в браузере адрес [http://127.0.0.1:8000](http://127.0.0.1:8000). На странице должны появиться сообщения «Marketing Intelligence запущен» и «Система готова».

При первом запуске приложение само создаст папки `data`, `logs` и локальную базу `data/marketing_intelligence.db`.

## Остановка

Вернитесь в PowerShell и нажмите `Ctrl+C`. Для выхода из виртуального окружения выполните:

```powershell
deactivate
```

## Запуск тестов

```powershell
python -m pytest
```

Тесты используют временную папку и не создают рабочую базу в репозитории.

## Частые ошибки

### Python не найден

Переустановите Python с включённым флажком **Add Python to PATH** или используйте команду `py`.

### Не удаётся активировать окружение

Выполните команду `Set-ExecutionPolicy` из раздела подготовки. Она действует только в текущем окне PowerShell.

### Порт 8000 уже занят

Остановите ранее запущенное приложение сочетанием `Ctrl+C` либо запустите приложение на другом порту:

```powershell
python -m uvicorn marketing_intelligence.main:app --host 127.0.0.1 --port 8001
```

Тогда откройте `http://127.0.0.1:8001`.

### Страница не открывается

Убедитесь, что окно PowerShell с приложением остаётся открытым и в нём нет сообщения об ошибке. Проверьте точный адрес `http://127.0.0.1:8000` и попробуйте другой поддерживаемый браузер.
