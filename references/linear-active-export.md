# Полная выгрузка активной очереди Linear для One Job

Используй этот путь, когда `mcp__linear__list_issues` возвращает слишком большой
ответ, множество страниц завершённых/отменённых автозадач или результат нельзя
надёжно сохранить целиком в JSON.

## Предпочтительный порядок

1. Сначала запрашивай только активные типы состояния: `backlog`, `unstarted`,
   `started`. При MCP — отдельный пагинируемый запрос на каждый тип; не начинай
   с нефильтрованной очереди, если она заведомо загрязнена закрытым шумом.
2. Если MCP-ответы всё равно обрезаются, используй Linear GraphQL API и
   `viewer.assignedIssues` с серверным фильтром по типу состояния.
3. Следуй `pageInfo.hasNextPage/endCursor` до конца. Повторы transient-ошибок
   продолжай с последнего подтверждённого курсора.
4. Не печатай значение `LINEAR_API_KEY`; проверяй только факт наличия.
5. Сохрани нормализованный массив в `/tmp/jarvis-linear-issues.json`, запусти
   `scripts/jarvis.py one-job`, затем удали временный файл.

## GraphQL-запрос

```graphql
query ActiveAssignedIssues($after: String) {
  viewer {
    assignedIssues(
      first: 250
      after: $after
      filter: { state: { type: { in: ["backlog", "unstarted", "started"] } } }
    ) {
      nodes {
        id
        identifier
        title
        priority
        dueDate
        updatedAt
        createdAt
        url
        state { name type }
        project { name }
        team { name key }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```

## Нормализация для ранжировщика

В каждом объекте сохраняй оба идентификатора без подмены:

```json
{
  "linear_id": "immutable Linear UUID from node.id",
  "id": "BRA-123",
  "identifier": "BRA-123",
  "title": "...",
  "priority": 1,
  "dueDate": "2026-09-05",
  "createdAt": "2026-09-01T10:00:00Z",
  "updatedAt": "2026-09-04T10:00:00Z",
  "status": "Todo",
  "statusType": "unstarted",
  "project": "Project name",
  "url": "https://linear.app/..."
}
```

`linear_id` используется для мутаций и должен оставаться UUID. `id` и
`identifier` — человекочитаемый ключ для показа. После выбора всё равно перечитай
карточку через Linear MCP/API перед любым действием.
