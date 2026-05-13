## Sayo ML Dev

Локальные средства для подготовки каркаса моделей распознавания речи (STT), сборки образов Docker и запуска небольшого **gRPC-стенда** для интеграционных проверок.

Англоязычная версия руководства: [`README.md`](README.md).

### Содержание

- Назначение репозитория
- Требования к окружению
- Структура репозитория
- Каталог модели (`models/<name>/`)
- Окружение Python и uv
- Мастер создания модели (wizard)
- Протоколы буферов: пересборка заглушек
- Образы Docker и порядок сборки
- Справочник по `model_build.py`
- Команды Makefile
- Запуск стенда
- Параметры сервера стенда
- Кратко о поведении gRPC
- Тестовый клиент
- Публикация и получение образа модели
- Дополнительные утилиты
- Устранение неполадок
- Связанные документы

---

### Назначение репозитория

| Компонент | Назначение |
|-----------|------------|
| **Мастер** (`python -m wizard.cli`) | Создаёт каталог `models/<name>/` (конфигурация, заготовки зависимостей, `weights/`) и при необходимости новый адаптер в `model_repository/adapters/`. |
| **Репозиторий моделей** | Класс `ModelRepository` читает `model.yaml`, вычисляет пути к весам, импортирует `model_repository.adapters.<adapter>` и создаёт экземпляр подкласса `BaseSTTModel`. |
| **Стенд** (`stand/server.py`) | Сервер gRPC с одной моделью: методы `HealthCheck`, `StreamingRecognize`. |
| **Клиент** (`stand/client.py`) | Проверочный клиент в стиле интеграции: `HealthCheck` → поток из файла, микрофона или тишины; ресемплинг и нарезка под дескриптор модели. |
| **Сборка** (`model_build.py`, `Makefile`) | Сборка образов `base` → `model` → `stand`; публикация и получение образа модели; извлечение файлов обратно в репозиторий. |

---

### Требования к окружению

| Инструмент | Примечание |
|------------|------------|
| **Python** | `>= 3.12` (см. `pyproject.toml`). |
| **uv** | Рекомендуется для установки зависимостей (`uv sync`). |
| **Docker** | Нужен для сборки образов и для извлечения при `pull-model`. |
| **GNU Make** (по желанию) | Используется `Makefile`. В Windows удобен Git Bash; встроенный `make` может отличаться. |
| **Графический ускоритель** (по желанию) | Цель `make run-stand` вызывает `docker run --gpus all`. При отсутствии набора NVIDIA / драйверов используйте `--device cpu` или измените команду запуска. |

---

### Структура репозитория

| Путь | Назначение |
|------|------------|
| `wizard/` | Каркас `models/<name>/` и заглушка адаптера (`templates/`). |
| `model_repository/` | `ModelRepository`, базовые типы `BaseSTTModel` / `STTConfig` / `STTResult`, адаптеры в `adapters/`. |
| `models/<name>/` | Пакет одной модели: `model.yaml`, файлы зависимостей, веса. |
| `stand/server.py` | Служба gRPC `SayoService`: `HealthCheck`, `StreamingRecognize`. |
| `stand/client.py` | Тестовый клиент (файл / микрофон / тишина). |
| `proto/sayo.proto` | Контракт API; Python-заглушки в `proto/` (пересобрать после правок; образ стенда пересобирает заглушки внутри образа). |
| `model_build.py` | Сборка, публикация и получение образов Docker. |
| `Docker.base`, `Docker.model`, `Docker.stand` | Описания образов (слои: база → модель → стенд). |
| `Makefile` | Сокращения для сборки, стенда, клиента, push/pull. |
| `utils/record_waw.py` | По желанию: запись WAV для тестов. |

---

### Каталог модели (`models/<name>/`)

Каждая модель — это каталог `models/<name>/`, где `<name>` совпадает с аргументом `model_build.py model <name>` и с параметром `--model <name>` у стенда.

| Файл или каталог | Назначение |
|------------------|------------|
| **`model.yaml`** | Основная конфигурация. **Обязательные поля:** `id` (строка, логический идентификатор для клиентов), `adapter` (строка — имя модуля Python в `model_repository/adapters/`). Часто задаются: `description`, `language_code`, `sample_rate`, `latency`, `runtime`, `weights`. Любые другие поля верхнего уровня передаются в адаптер как часть `STTConfig.extra`. |
| **`runtime`** (необязательно, словарь) | Подсказки для клиентов через `HealthCheck` / `ModelDescriptor`: например `chunk_duration_ms` (по умолчанию `560`), `audio_quantization` (`pcm_f32le` или `pcm_s16le`), `supports_interim_results` (по умолчанию `true`). |
| **`weights`** (необязательно) | В блоке `weights:` перечисляются `artifacts` с `- path: "weights/..."` относительно каталога модели. `ModelRepository` может проверить наличие файлов через `validate_files()`. |
| **`requirements.lock`** | Пакеты Python, устанавливаемые **внутри образа модели** (`uv pip install --system`). |
| **`system-packages.txt`** | Необязательные пакеты Debian (по одному в строке, допускаются комментарии `#`); ставятся в образ модели. |
| **`weights/`** | Файлы весов (например `.nemo`); копируются в образ модели. |

**Правила имён в мастере:** имя модели и имя адаптера должны удовлетворять шаблону `^[a-z0-9_-]+$` (строчные латинские буквы, цифры, дефис, подчёркивание).

Пример для модели `nemo`:

```text
models/nemo/
  model.yaml
  requirements.lock
  system-packages.txt
  weights/
    sayo.nemo
```

---

### Окружение Python и uv

Из корня репозитория:

```bash
uv sync
uv sync --group dev   # grpc_tools, ruff, sounddevice/soundfile для клиента и protoc
```

Группы `dev` достаточно для локальной пересборки заглушек и для `stand/client.py` (файл и микрофон).

Для **полностью зафиксированного** набора зависимостей (включая `grpcio` и др.) можно использовать экспорт в `requirements.txt` и установку через pip:

```bash
pip install -r requirements.txt
```

---

### Мастер создания модели (wizard)

```bash
python -m wizard.cli --help
```

**Интерактивный режим (рекомендуется):**

```bash
python -m wizard.cli
# интерфейс мастера на русском:
python -m wizard.cli --lang ru
```

**Без диалога:**

```bash
python -m wizard.cli --name whisper-large --adapter whisper
```

Мастер создаёт при отсутствии: `models/<name>/model.yaml`, `requirements.lock`, `system-packages.txt`, `weights/`, а также файл `model_repository/adapters/<adapter>.py`, если такого адаптера ещё нет.

---

### Протоколы буферов: пересборка заглушек

После правок в `proto/sayo.proto` пересоберите Python-заглушки в корне репозитория:

```bash
uv run python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/sayo.proto
```

**Образ стенда** при сборке запускает `grpc_tools.protoc`, чтобы версии сгенерированного кода совпадали с библиотеками внутри образа. Локальные заглушки всё равно нужны для подсветки в редакторе и для запуска `stand/server.py` вне Docker без ошибок импорта.

---

### Образы Docker и порядок сборки

Образы строятся слоями:

1. **`sayo-base:latest`** (из `Docker.base`) — базовый Python, `uv`, средства сборки.
2. **`sayo-model-<name>:latest`** (из `Docker.model`) — установка `system-packages.txt` и `requirements.lock`, копирование `models/<name>/` (включая веса), копирование **одного** файла адаптера по полю `adapter:` в `model.yaml`, плюс общие модули `model_repository`.
3. **`sayo-stand-<name>:latest`** (из `Docker.stand`) — поверх образа модели добавляются `stand/` и `proto/`, в образе генерируется код gRPC, точка входа: `python -m stand.server`.

**Пересборка:** при изменении только `model.yaml`, кода адаптера или весов обычно достаточно пересобрать **model**, затем **stand**. Образ **base** пересобирают реже — только при изменении `Docker.base`.

---

### Справочник по `model_build.py`

Команды выполняются из корня репозитория (где лежат файлы `Docker.*`).

| Команда | Действие |
|---------|----------|
| `python model_build.py base` | Сборка базового образа. Параметр `--base-image` (по умолчанию `sayo-base:latest`). |
| `python model_build.py model <имя>` | Сборка образа модели. Параметры `--base-image`, `--model-image` (по умолчанию `sayo-model-<имя>:latest`). Поле `adapter` читается из `models/<имя>/model.yaml`. |
| `python model_build.py stand <имя>` | Сборка образа стенда. Параметры `--model-image` (по умолчанию `sayo-model-<имя>:latest`), `--stand-image` (по умолчанию `sayo-stand-<имя>:latest`). |
| `python model_build.py push-model <имя> --to <адрес>` | Перетегировать локальный образ модели и выполнить `docker push`. Необязательно `--from` для другого локального тега. |
| `python model_build.py pull-model --from <ссылка>` | При необходимости `docker pull` (если не указан `--no-pull`), необязательный повторный тег `sayo-model-<имя>:latest`, извлечение `models/<имя>/` и `model_repository/adapters/<adapter>.py`. Флаги см. ниже. |

**Флаги `pull-model`:**

| Флаг | Смысл |
|------|--------|
| `--from <ссылка>` | **Обязательно.** Образ в реестре, digest или локальный идентификатор/тег. |
| `--as <имя>` | Имя каталога под `/app/models`, если в образе несколько моделей. |
| `--local-tag <тег>` | Заменить тег по умолчанию `sayo-model-<имя>:latest`. |
| `--no-retag` | Не создавать локальный тег `sayo-model-*` (тогда при сборке стенда укажите `--model-image`). |
| `--overwrite` | Заменить существующие `models/<имя>/` и файл адаптера. |
| `--no-pull` | Не вызывать `docker pull`; ссылка должна быть доступна локально. |

---

### Команды Makefile

```bash
make help
```

| Цель | Назначение |
|------|------------|
| `make build-base` | вызов `model_build.py base` |
| `make build-model` | `model_build.py model` с переменными `MODEL`, `BASE_IMAGE`, `MODEL_IMAGE` |
| `make build-stand` | `model_build.py stand` с `MODEL`, `MODEL_IMAGE`, `STAND_IMAGE` |
| `make build-all` | последовательно base, model, stand |
| `make run-stand` | `docker run` с GPU, проброс `PORT` → `50051`, запуск с `--device cuda` |
| `make run-client-mic` / `make run-client-file` | запуск `stand/client.py` через `uv run` |

**Переменные:** `MODEL` (по умолчанию `nemo`), `BASE_IMAGE`, `MODEL_IMAGE`, `STAND_IMAGE`, `PORT`, `HOST`, `FILE`, `TO`, `FROM`, `EXTRACT_MODEL`, `OVERWRITE`, `NO_RETAG`, `NO_PULL`, `LOCAL_TAG`.

**Windows и GNU Make:** для `pull-model` используйте **`EXTRACT_MODEL=...`**, а не **`AS=...`**: в GNU Make переменная `AS` зарезервирована под ассемблер `as`, из-за чего `AS=nemo` превращается в ошибочный `--as as`.

---

### Запуск стенда

**Docker (после `build-all` или эквивалента):**

```bash
docker run --rm --gpus all \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

**Пример только на центральном процессоре:**

```bash
docker run --rm -p 50051:50051 sayo-stand-nemo:latest --model nemo --device cpu
```

**Через Make:**

```bash
make run-stand          # по умолчанию MODEL=nemo
make run-stand MODEL=gigaam PORT=50052
```

---

### Параметры сервера стенда

Точка входа образа стенда: `python -m stand.server` (см. `Docker.stand`).

| Аргумент командной строки | Переменная окружения | Значение по умолчанию | Описание |
|---------------------------|----------------------|------------------------|----------|
| `--model` / `-m` | `STAND_MODEL` | `nemo` | Подкаталог в `models/` (должен содержать `model.yaml`). |
| `--device` / `-d` | `STAND_DEVICE` | `cpu` | Устройство для адаптера (например `cuda`, `cuda:0`). |
| `--port` / `-p` | `STAND_PORT` | `50051` | Порт прослушивания gRPC внутри контейнера; снаружи пробрасывайте `-p хост:50051`. |
| `--models-dir` | `STAND_MODELS_DIR` | `<корень репоз>/models` | Корень каталогов моделей. |

Стенд загружает **ровно одну** модель. Клиенты обязаны передать в `StreamingConfig` поле `sample_rate_hertz`, совпадающее с `sample_rate` модели в `model.yaml`; иначе сервер отклонит запрос и укажет на необходимость ресемплинга на стороне клиента.

---

### Кратко о поведении gRPC

- **`HealthCheck`:** возвращает готовность и один дескриптор `ModelDescriptor` (частота дискретизации, длительность чанка, формат импульсно-кодовой модуляции, признак промежуточных результатов и т.д.) из `model.yaml` / блока `runtime`.
- **`StreamingRecognize`:** двунаправленный поток. Первое сообщение — **`StreamingConfig`**; далее — сырые монофонические фрагменты **PCM** в поле `audio_chunk`. Сервер переводит их в формат с плавающей точкой и передаёт в потоковый интерфейс адаптера.

Полное описание полей — в файле `proto/sayo.proto`.

---

### Тестовый клиент

Цепочка: подключение → `HealthCheck` → настройки потока → `StreamingRecognize` (сначала конфигурация, затем аудио).

```bash
uv run python stand/client.py --host 127.0.0.1 --port 50051 --audio path/to.wav
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic   # остановка: Ctrl+C
```

```bash
make run-client-mic
make run-client-file FILE=path/to.wav    # при необходимости HOST=... PORT=...
```

| Параметр | Переменная окружения | Описание |
|----------|----------------------|----------|
| `--host` | `STAND_HOST` | Хост сервера (по умолчанию `localhost`). |
| `--port` | `STAND_PORT` | Порт (по умолчанию `50051`). |
| `--model` / `-m` | `STAND_MODEL` | Если задано, должно совпадать с `model_id` из `HealthCheck`. |
| `--audio` / `-a` | — | Путь к аудиофайлу (чтение через **soundfile**, ресемплинг под частоту модели). |
| `--mic` | — | Поток с микрофона до прерывания. |
| `--mic-device` | — | Индекс устройства PortAudio или подстрока имени, если устройство по умолчанию не подходит. |
| `--send-delay-ms` | — | Пауза между чанками в режиме файла и тишины (имитация медленной отправки). |

Если **не** указаны ни `--audio`, ни `--mic`, клиент отправляет **5 секунд тишины** и в конце выводит краткую сводку.

---

### Публикация и получение образа модели

Внутри **образа модели**:

- дерево модели: `/app/models/<имя>/` (включая `model.yaml` и `weights/`);
- один файл адаптера: `/app/model_repository/adapters/<adapter>.py`.

**Публикация** (перетегировать локальный `sayo-model-<имя>:latest` и отправить в реестр):

```bash
python model_build.py push-model nemo --to ghcr.io/myorg/sayo-model-nemo:1.0.0
# при необходимости: --from sayo-model-nemo:other
```

```bash
make push-model MODEL=nemo TO=ghcr.io/myorg/sayo-model-nemo:1.0.0
```

**Получение** (при необходимости скачивание из реестра, повторный тег, извлечение в репозиторий):

```bash
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --as nemo
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --overwrite
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-retag
python model_build.py stand nemo --model-image ghcr.io/myorg/sayo-model-nemo:1.0.0
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-pull
python model_build.py pull-model --from abc123def456 --no-pull --as gigaam
```

```bash
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 EXTRACT_MODEL=nemo OVERWRITE=1
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 NO_PULL=1
```

---

### Дополнительные утилиты

Запись WAV в каталог `test_audio/` (удобно для короткого фрагмента и проверки через `run-client-file`):

```bash
uv run python utils/record_waw.py
```

---

### Устранение неполадок

| Проявление | Что проверить |
|------------|----------------|
| Ошибка **`sample_rate_hertz`** со стороны стенда | Клиент должен привести частоту к `sample_rate` из `model.yaml`; тестовый клиент делает это по `HealthCheck`. |
| Клиент с микрофоном не открывает устройство | Параметр `--mic-device`; список устройств: `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| **`make run-stand`** не работает с GPU | Установите набор NVIDIA Container Toolkit или запускайте `docker run` без `--gpus` и с `--device cpu`. |
| **`pull-model`** не перезаписывает файлы | Укажите `--overwrite` или удалите конфликтующие `models/<имя>/` и файл адаптера. |
| Ошибки импорта **`proto`** | Выполните `uv sync --group dev` и пересоберите заглушки после изменений в `sayo.proto`. |
| Устаревшие заглушки в Docker | Образ стенда пересобирает заглушки при сборке; пересоберите **stand** после правок протокола. |

---

### Связанные документы

- [`README.md`](README.md) — англоязычная версия этого руководства.
- [`SayoOverview.md`](SayoOverview.md) — обзор проекта Sayo (по желанию).
- [`SAYO_ML_ROADMAP.md`](SAYO_ML_ROADMAP.md) — заметки по подготовке данных и обучению (по желанию).
