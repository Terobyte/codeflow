# Vendored pipecat JS SDK

| пакет | версия | лицензия |
|---|---|---|
| @pipecat-ai/client-js | 1.12.0 | BSD-2-Clause |
| @pipecat-ai/small-webrtc-transport | 1.10.5 | BSD-2-Clause |
| esbuild (только сборка) | 0.25.5 | MIT |

`pipecat.mjs` — самодостаточный ESM-бандл (0 bare-импортов), собран один раз
`tools/vendor_pipecat.sh` и закоммичен: сырые npm-бандлы импортируют
bowser/events/uuid/@daily-co/daily-js/lodash bare-specifier'ами и в браузере
без сборки не работают (проба 2026-07-13). Апгрейд = поднять пины в скрипте,
перегенерировать, прогнать `tests/test_ui_client.py` и живой голос-смоук.
Экспорты: `PipecatClient`, `RTVIEvent`, `SmallWebRTCTransport`.
