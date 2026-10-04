# AI Usage API v1 contract

`schema_version: solo-empire.ai-usage.v1`

API อ่าน snapshot ที่ผ่านการกรองของ Claude และ Codex เท่านั้น
ไม่อ่านประวัติสนทนา, provider credential หรือฐานข้อมูลหลัก

## Routes และ authorization

| Method | Path | ผลลัพธ์ |
| --- | --- | --- |
| `GET` | `/health` | liveness และ schema version |
| `GET` | `/ready` | projection และ owner auth พร้อมหรือไม่ |
| `GET` | `/api/v1/ai-usage` | provider usage, ค่าสมาชิก และ alerts |
| `OPTIONS` | routes | CORS preflight |

production ต้องใช้ owner-scoped RS256 JWT: `aud=ai-usage-api`,
`scope=ai-usage:read`, `tenant_id=owner`, subject ที่อนุญาต และ lifetime ไม่เกิน 120 วินาที
ค่า auth ใช้ `AI_USAGE_API_AUTH_*` ดู [runtime configuration](../README.md)
`401` คือ token ไม่ถูกต้อง, `403` คือ owner/tenant/scope ไม่ตรง,
`503 reads_disabled` คือ auth ไม่พร้อม รูปแบบ error คือ `{"error":{"code","message"}}`
คำสั่งเขียนใน usage route ตอบ `405`; ไม่มี route แก้ไข snapshot
static token และการอ่านไม่ใช้ JWT อนุญาตเฉพาะ development ที่ loopback

`/ready` ส่งเฉพาะ `status=ready|not_ready`, `source=available|unavailable`,
`auth=ready|development|disabled` การขาด provider snapshot ไม่ทำให้ service ปิด
จึงต้องอ่าน `providers[].status` เพื่อตรวจการเชื่อมต่อและความสดของ provider

## Response

- `schema_version`: `solo-empire.ai-usage.v1`
- `generated_at`: เวลา UTC ISO 8601 ที่คำนวณ response ใหม่ในแต่ละ request
- `providers`: มี Claude และ Codex เสมอ ตามรูปแบบข้างล่าง
- `summary`: `connected` นับ provider ที่ `ready|stale`; `monthly_fee_usd` เป็นผลรวมเมื่อรู้ราคาทั้งสองเจ้า มิฉะนั้น `null`; `fees_complete` เป็น boolean

### Provider

| Field | รูปแบบและความหมาย |
| --- | --- |
| `id` / `name` | `claude` / `Claude` หรือ `codex` / `Codex` |
| `status` | `ready`, `stale`, `not_connected`, `error` |
| `source` | `claude_statusline`, `codex_app_server`, `not_connected` |
| `collected_at` | UTC ISO timestamp ของข้อมูลที่รับ หรือ `null` |
| `plan` | label ที่อยู่ใน allowlist หรือ `null` |
| `monthly_fee_usd` | finite nonnegative number หรือ `null`; ไม่อนุมานจาก plan |
| `windows` | รายการ quota windows ที่ตรวจสอบแล้ว |
| `session` | `estimated_cost_usd`, `input_tokens`, `output_tokens`; ทุกฟิลด์ nullable |
| `api_billed_cost_usd` | `null` เสมอ: ยังไม่เชื่อม billed API spend |
| `alerts` | alert จาก fresh active windows เท่านั้น |
| `next_action` | `connect_provider`, `check_source`, `refresh_usage`, `monitor`, `quota_reached`, `quota_low`, `use_before_reset` |

Claude plan labels: `Free`, `Pro`, `Max`, `Team`, `Enterprise`
Codex plan labels: `Free`, `Go`, `Plus`, `Pro`, `Pro Lite`, `Pro Max`, `Team`, `Business`, `Enterprise`, `Edu`
snapshot ที่ไม่มี usable percentage ใน window ใดเลยมีสถานะ `error`
snapshot เก่ากว่า 900 วินาทีมีสถานะ `stale` และไม่มี alert
snapshot ที่ collected time อยู่ในอนาคตเกิน 60 วินาที, schema/provider/source ไม่ตรง
หรืออ่านไม่ได้มีสถานะ `error`; ไม่ส่ง error จากแหล่งข้อมูลออกมา

### Quota window

`id` คือ `five_hour|seven_day` สำหรับ Claude หรือ `primary|secondary` สำหรับ Codex
`label` สร้างจาก allowlist ของ API โดยไม่ใช้ข้อความดิบจาก provider
ฟิลด์อื่นคือ `used_percent`, `remaining_percent`, `resets_at`, `window_minutes`,
`status=active|expired` เปอร์เซ็นต์และเวลาอาจเป็น `null` หากไม่ทราบหรือไม่ถูกต้อง

`used_percent` ต้องเป็น finite nonnegative number และ clamp สูงสุด 100
`remaining_percent` คำนวณจาก used ที่ตรวจสอบแล้ว ไม่เชื่อค่า remaining จาก snapshot
เมื่อ `resets_at<=generated_at` window เป็น `expired`, remaining เป็น `null` และไม่มี alert
`window_minutes` เป็น positive integer ไม่เกิน 525600 หรือ `null`
id ที่ซ้ำจะถูกตัดออกทุกแถว เพื่อไม่เลือกข้อมูลขัดแย้งเป็นค่าที่เชื่อถือได้
window id ที่ไม่รู้จักถูกตัดออก การไม่ทราบ reset ไม่ถือเป็นหลักฐานว่ารอบหมดอายุ

### Alert

ฟิลด์คือ `id`, `kind=use_before_reset|quota_low|quota_reached`, `window_id`,
`message` ภาษาไทย, `resets_at` UTC ISO และ `remaining_percent`
`id` มีรูปแบบ `{provider}:{window_id}:{kind}:{resets_at}` เพื่อ deduplicate ใน browser

ใช้ threshold 85% สำหรับ `quota_low` และ 100% สำหรับ `quota_reached`
`use_before_reset` ต้องเหลืออย่างน้อย 30%, reset อยู่ในอนาคตและภายใน 6 ชั่วโมง
เมื่อ `window_minutes<=360` หรือภายใน 24 ชั่วโมงเมื่อ `window_minutes>=10080`
ระยะรอบอื่นหรือ unknown duration ไม่สร้างคำแนะนำใช้ก่อน reset
unknown reset ไม่สร้าง alert ทุกชนิด backend ไม่ส่ง notification ภายนอก

## Snapshot และ settings boundary

อ่านเฉพาะ `claude.json`, `codex.json`, `settings.json` ใน `AI_USAGE_STATE_DIR`
หรือ `~/.local/state/solo-empire/ai-usage` เปิดเฉพาะ regular file ขนาดไม่เกิน 1 MiB
ปฏิเสธ symlink ทุก path component, FIFO, JSON ที่ซ้ำ key และ payload ที่ไม่ใช่ object
API ไม่มี raw passthrough; ไม่ส่ง arbitrary error, identity, path, label หรือ raw fields

provider snapshot ใช้ `schema_version=solo-empire.ai-usage.snapshot.v1` และ
`provider`, `collected_at`, `source`, `plan`, `windows`, `session`
เก็บเฉพาะ metadata ที่ allowlist ไม่เก็บ prompt, response หรือ account identity
`settings.json` ใช้ `monthly_fees_usd` mapping ของ `claude` และ `codex`
environment fees override settings แม้ค่าที่ override จะไม่ถูกต้อง ซึ่งจะแสดง unknown
`estimated_cost_usd` ไม่ใช่ billed API spend และไม่ใช่ตัวชี้วัดความคุ้มค่ารายเดือนที่พิสูจน์แล้ว
