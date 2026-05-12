## Sayo ML Dev

Локальные инструменты для создания каркаса STT-моделей, сборки Docker-образов и запуска небольшого **gRPC-стенда** для интеграционных тестов.

### Структура репозитория


| Путь                | Назначение                                              |
| ------------------- | ------------------------------------------------------- |
| `wizard/`           | Каркас `models/<name>/` и заглушка адаптера             |
| `model_repository/` | `ModelRepository`, адаптеры (например NeMo)             |
| `stand/server.py`   | gRPC `SayoService`: `HealthCheck`, `StreamingRecognize` |
| `stand/client.py`   | Тестовый gRPC-клиент (файл / микрофон)                  |
| `proto/sayo.proto`  | Контракт API; сгенерированные заглушки в `proto/`       |
| `model_build.py`    | Сборка / push / pull образов (`base` \| `model` \| `stand` \| `push-model` \| `pull-model`) |
| `Makefile`          | Команды сборки и `run-stand`                            |


### Каталог модели (`models/<name>/`)

- `model.yaml` — `id`, `adapter`, `language_code`, `sample_rate`, `latency`, опционально `runtime` (например `chunk_duration_ms`, `audio_quantization`, `supports_interim_results`), `weights.artifacts`
- `requirements.lock`, `system-packages.txt`
- `weights/` — веса модели (запекаются в образ модели)

### Окружение

```bash
uv sync
uv sync --group dev   # grpc_tools, ruff, звуковые библиотеки для клиента / protoc
```

Для полностью зафиксированного runtime (включая `grpcio`) при необходимости используйте `requirements.txt`.

### Каркас новой модели
```bash
python -m wizard.cli --help
```
```bash
python -m wizard.cli --lang ru
```

Перегенерация Python-заглушек после правок в `proto/sayo.proto`:

```bash
uv run python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/sayo.proto
```

### Сборка и запуск стенда (Docker)

```bash
python model_build.py base
python model_build.py model <имя>
python model_build.py stand <имя>
```

Пример (`nemo`):

```bash
python model_build.py base
python model_build.py model nemo
python model_build.py stand nemo
```

Через Make: `make build-all`, затем `make run-stand` (по умолчанию `MODEL=nemo`).

```bash
docker run --gpus all \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

### Push / pull образа модели (registry)

Веса лежат в `models/<name>/` и копируются в **образ модели** в `/app/models/<name>/`. В образе также есть один файл адаптера: `/app/model_repository/adapters/<adapter>.py`.

**Push** (перетегировать локальный `sayo-model-<name>:latest` и отправить в registry):

```bash
python model_build.py push-model nemo --to ghcr.io/myorg/sayo-model-nemo:1.0.0
# при необходимости: --from sayo-model-nemo:other
```

```bash
make push-model MODEL=nemo TO=ghcr.io/myorg/sayo-model-nemo:1.0.0
```

**Pull** (скачать, при необходимости повесить тег `sayo-model-<name>:latest`, распаковать в репозиторий `models/<name>/` и `model_repository/adapters/`):

```bash
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0
# если в образе больше одной папки под /app/models — укажите имя:
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --as nemo
# перезаписать уже существующие файлы в репозитории:
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --overwrite
# не создавать локальный тег sayo-model-*; тогда укажите образ при сборке стенда:
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-retag
python model_build.py stand nemo --model-image ghcr.io/myorg/sayo-model-nemo:1.0.0
# образ уже есть локально (без docker pull):
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-pull
# локальный образ без тега: укажите IMAGE ID или digest, либо сначала docker tag <id> repo/name:tag
python model_build.py pull-model --from abc123def456 --no-pull --as gigaam
```

```bash
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 EXTRACT_MODEL=nemo OVERWRITE=1
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 NO_PULL=1
```

В **Make** используйте **`EXTRACT_MODEL=...`**, а не `AS=...`: в GNU Make переменная `AS` зарезервирована (ассемблер `as`), из‑за этого `AS=nemo` превращается в `--as as`.

Стенд загружает одну модель из `model.yaml`. **Ресемплируйте аудио на клиенте**, чтобы `StreamingConfig.sample_rate_hertz` совпадал с моделью (см. `HealthCheck` / `ModelDescriptor`).

### Тестовый клиент (`stand/client.py`)

Один канал на запуск: `HealthCheck`, затем `StreamingRecognize` с параметрами из дескриптора модели (частота, длина чанка, квантизация). Режимы файла и микрофона приводят аудио к этим настройкам на клиенте.

```bash
uv run python stand/client.py --host 127.0.0.1 --port 50051 --audio path/to.wav
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic   # стоп: Ctrl+C
```

```bash
make run-client-mic
make run-client-file FILE=path/to.wav    # при необходимости HOST=... PORT=...
```

`--model` / `-m` при указании должен совпадать с `model_id` стенда. `--mic-device` — индекс или подстрока имени входа PortAudio, если устройство по умолчанию не подходит. `--send-delay-ms` — пауза между чанками для файла и режима тишины по умолчанию. Вместо `--host`, `--port` и `--model` можно задать `STAND_HOST`, `STAND_PORT` и `STAND_MODEL`.

Без `--audio` и `--mic` отправляется 5 секунд тишины; в конце печатается краткая сводка.

По желанию: запись WAV в `test_audio/` через `utils/record_waw.py`.
