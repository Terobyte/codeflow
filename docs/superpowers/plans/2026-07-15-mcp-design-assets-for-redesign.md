# MCP-серверы для дизайна и ассетов — ресурсы для редизайна

**Статус:** СПРАВОЧНИК (parked 2026-07-15)
**Контекст:** Ресерч MCP-серверов для professional SVG/иконок/ассетов в рамках редизайна CodeFlow (Синапс UI v3+)

---

## Зачем

При редизайне UI нужен пайплайн: генерация SVG-иконок, экспорт ассетов из Figma, создание логотипов и визуальных элементов. Claude Code может писать SVG вручную, но для professional-уровня нужны MCP-серверы.

---

## SVG Генерация

| Сервер | Описание | Установка |
|--------|----------|-----------|
| **[svg-mcp](https://github.com/georgeharker/svg-mcp)** | Полноценное создание SVG — примитивы, градиенты, текст на путях, рендер через resvg. Итеративный loop: create → build → render → see → export | `claude mcp add svg-mcp -- uv run svg-mcp` |
| **[Clearly](https://www.clearly.sh/mcp)** | Hosted AI-генерация SVG (иконки, логотипы, стикеры) + растровых изображений по промпту. Биллинг per-generation | `claude mcp add --transport http clearly https://relay.clearly.sh/mcp --header "Authorization: Bearer ck_mcp_..."` |
| **[QuiverAI MCP](https://quiver.ai/blog/introducing-quiverai-mcp)** | Структурированная генерация SVG, hosted | Hosted MCP |

---

## Figma → Код + Ассеты

| Сервер | Описание |
|--------|----------|
| **[Figma MCP (официальный)](https://github.com/figma/mcp-server-guide)** | Чтение дизайна, экспорт SVG/PNG, запись обратно в Figma. Работает через Dev Mode |
| **[Plumb MCP](https://github.com/tathagat22/plumb-mcp)** | Двунаправленный: design→code + prompt→design. Верификация pixel-perfect (ΔE2000). Работает на Free-плане Figma без REST rate limits |
| **[Figma Unified MCP](https://github.com/sso-ss/figma-unified-mcp)** | 106 инструментов: чтение, создание, экспорт, генерация CSS/React/SwiftUI/Tailwind. Без плагина для read-операций |
| **[Figma Asset Downloader](https://github.com/Aakash-02/figma-mcp)** | Пакетный скачка ассетов, AI-driven выбор, design tokens, 95% efficiency gain |

---

## UI/Дизайн Системы

| Сервер | Описание |
|--------|----------|
| **[UI Architect](https://github.com/foduucom/UI-Architect-MCP)** | 16 инструментов: полный пайплайн agency-quality UI. Реальные фото (Unsplash/Pexels) + Lucide SVG иконки. Без API ключей из коробки |
| **[Generative UI MCP](https://github.com/op7418/generative-ui-mcp)** | SVG иллюстрации, диаграммы, графики, UI мокапы. Загружает guidelines on-demand (экономит токены) |
| **[logoMCP](https://github.com/gofastercloud/logoMCP)** | Полный brand design system локально на Apple Silicon — логотипы, цвета, типографика, 47 ассетов (PNG/JPEG/SVG). ~3 минуты от brief'а до assets |

---

## Рекомендация для CodeFlow

Для редизайна Синапс UI v3+:

1. **svg-mcp** — генерация SVG-иконок и элементов прямо в коде (бесплатно, локально)
2. **Clearly** — AI-генерация иконок/логотипов по промпту (hosted, быстро)
3. **Plumb MCP** — если дизайн в Figma, двунаправленная синхронизация с верификацией
4. **UI Architect** — для генерации целых страниц с реальными ассетами

### Установка (одна строка на каждый)

```bash
# SVG-генерация
claude mcp add svg-mcp -- uv run svg-mcp

# AI SVG/растровые ассеты (hosted)
claude mcp add --transport http clearly https://relay.clearly.sh/mcp \
  --header "Authorization: Bearer <YOUR_KEY>"

# Figma integration (двунаправленный)
npm install -g plumb-mcp
claude mcp add plumb -- node plumb-mcp
```

---

## Связь с планами

- **[Синапс UI v3 — редизайн](./2026-07-13-synapse-ui-v3-redesign.md)** — текущий план редизайна
- MCP-серверы могут быть полезны для Task 2 (иконки в сайдбаре), Task 5 (статусные иконки), general UI polish
