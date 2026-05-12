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
| `model_build.py`    | Сборка образов (`base` \| `model` \| `stand`)           |
| `Makefile`          | Команды сборки и `run-stand`                            |


### Каталог модели (`models/<name>/`)

- `model.yaml` — `id`, `adapter`, `language_code`, `sample_rate`, `latency`, опционально `runtime` (например `chunk_duration_ms`, `audio_quantization`, `supports_interim_results`), `weights.artifacts`
- `requirements.lock`, `system-packages.txt`
- `weights/` — локальные веса (монтируются в контейнеры)

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
  -v /path/to/repo/models/nemo/weights:/app/models/nemo/weights \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

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
