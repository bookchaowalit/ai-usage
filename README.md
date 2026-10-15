# ai-usage

ย้ายจาก solo-empire `services/ai-usage-api` เมื่อ 2026-10-11 พร้อมประวัติ (subtree split).
เป็น API + collector ที่รันในเครื่อง (local agent) อ่านโควตา Claude/Codex ให้ `owner-web`.
`vendor/service-auth` คือสำเนาของ `bookchaowalit/platform-auth-sdk`.

## systemd (user units) บนเครื่องนี้

`~/.config/systemd/user/solo-ai-usage-api.service` และ `solo-ai-usage-collector.service` ชี้มาที่ checkout นี้
(`.../private/ai-usage`, `PYTHONPATH=<checkout>/vendor/service-auth`). ตัวอย่างใน `systemd/` ของ repo นี้ (แก้ `/opt/solo-ai-usage` เป็น path ของ checkout).
เปลี่ยนที่ตั้งแล้วให้รัน `systemctl --user daemon-reload && systemctl --user restart solo-ai-usage-api solo-ai-usage-collector`.
Claude statusLine hook (`.claude/settings.local.json` ของ solo-empire) เรียก `<checkout>/collector.py claude-statusline`.

Test: `PYTHONPATH=vendor/service-auth:. python3 -m pytest tests`

---

# Solo Empire AI Usage API

`ai-usage-api` เป็น backend อ่านข้อมูลสำหรับหน้า AI Usage ใน
`owner-web` (`bookchaowalit/life-dashboard-web`) แสดงโควตา Claude และ Codex,
เวลาที่รีเซ็ต, ค่าสมาชิกที่ผู้ใช้ตั้งไว้ และการแจ้งเตือนจากข้อมูลล่าสุด
ดูรายละเอียดฟิลด์ใน [AI Usage v1 contract](contracts/ai-usage.v1.md)

## เริ่มใช้งานในเครื่อง

รันจาก root ของ repo นี้ ด้วย Python ที่มี `vendor/service-auth` และ dependencies:

```bash
PYTHONPATH=vendor/service-auth .venv/bin/python server.py
```

ค่าปกติคือ `http://127.0.0.1:3520` ปรับได้ด้วย `AI_USAGE_API_HOST`,
`AI_USAGE_API_PORT`, `AI_USAGE_API_ENV` และ `AI_USAGE_API_CORS_ORIGINS`
ตัว listener รองรับ PaaS `PORT`; `--port` และ `AI_USAGE_API_PORT` มีลำดับความสำคัญสูงกว่า

```text
GET /health
GET /ready
GET /api/v1/ai-usage
```

API อ่านเฉพาะ `claude.json`, `codex.json` และ `settings.json` ใน
`AI_USAGE_STATE_DIR` หรือ `~/.local/state/solo-empire/ai-usage` โดยไม่แก้ไฟล์เหล่านี้
ไม่อ่านฐานข้อมูลหลัก, credential หรือประวัติการสนทนา ไม่ติดตั้ง provider CLI

## เก็บข้อมูล Claude และ Codex

collector ใช้ข้อมูล `statusLine` ของ Claude Code และ
`account/rateLimits/read` ผ่าน Codex app-server โดยอาศัยการล็อกอินที่ provider CLI
จัดการอยู่แล้ว และไม่เริ่มงาน AI เพื่อใช้โควตา

```bash
.venv/bin/python collector.py codex
.venv/bin/python collector.py codex --interval 300
.venv/bin/python collector.py install-claude --settings-path "$PWD/.claude/settings.local.json" --passthrough-settings "$HOME/.claude/settings.json"
```

คำสั่งแรกเก็บ Codex หนึ่งครั้ง คำสั่งที่สอง polling ทุก 5 นาที
การติดตั้ง Claude เป็น project-local hook; collector คงคำสั่ง statusLine เดิมที่สืบทอดมา
โดยไม่แก้ global settings ทั้งยังรองรับ `claude-statusline` สำหรับรับ payload ทาง stdin
หาก Claude ยังไม่ได้ส่ง callback หรือไม่มีโควตาใน callback จะยังไม่แสดงเป็นข้อมูลพร้อมใช้
polling ต้องรันต่อเนื่องจึงได้ข้อมูลใหม่ และ hook มีผลใน project ที่ติดตั้งไว้

ในเครื่องนี้ API และ Codex polling รันเป็น user services
`solo-ai-usage-api.service` และ `solo-ai-usage-collector.service`
ภายใต้ `~/.config/systemd/user` โดย API รับเฉพาะ loopback และ collector อ่านทุก 5 นาที
ตรวจสถานะหรือหยุดได้ด้วย `systemctl --user status` / `stop` ตามชื่อ service
services เริ่มเมื่อ user session เริ่มทำงาน; ไม่ได้ตั้งให้ทำงานหลัง logout
หน้า Solo Web ต้องเปิดอยู่เพื่อส่ง browser notification และต้องกดเปิดสิทธิ์ในหน้าเว็บ

## ค่าสมาชิกและขอบเขตค่า API

กำหนดค่าสมาชิกจริงด้วย `AI_USAGE_CLAUDE_MONTHLY_FEE_USD` และ
`AI_USAGE_CODEX_MONTHLY_FEE_USD` หรือบันทึกใน `settings.json` ส่วนตัวผ่าน collector:

```bash
.venv/bin/python collector.py configure-fees --claude-fee "$CLAUDE_MONTHLY_FEE_USD" --codex-fee "$CODEX_MONTHLY_FEE_USD"
```

รูปแบบ settings คือ `{"monthly_fees_usd":{"claude":number,"codex":number}}`
ค่า environment มีลำดับความสำคัญสูงกว่า settings ค่าที่ไม่ทราบหรือไม่ถูกต้องเป็น `null`
ยอดรวมเป็น `null` และ `fees_complete=false` จนกว่าจะทราบราคาทั้งสองเจ้า
ไม่อนุมานราคาแพ็กเกจจากชื่อ plan

`session.estimated_cost_usd` เป็นค่าประเมินของ session ที่ provider รายงาน
ส่วน `api_billed_cost_usd` เป็น `null` เสมอ เพราะยังไม่มีการเชื่อมข้อมูล invoice/API billing
การใช้งาน subscription ไม่ใช่การเรียกเก็บเงิน API และยังไม่วัดมูลค่างานหรือเวลาที่ประหยัด

## ค่า AI คุ้มไหม (ROI รายเดือน)

`subscriptions` แสดงค่าสมาชิก AI ทั้ง 5 เจ้า (Claude, Codex, Gemini, Grok, Muse) เป็นบาท
และ `roi` ตอบว่ากำไรธุรกิจก่อนหักค่า AI ≥ 3 เท่าของค่า AI หรือไม่ และค่า AI อยู่ในช่วง 5–10% ของรายได้หรือไม่
Gemini, Grok, Muse ยังไม่มี quota collector จึงเป็น cost-only ตัวเลขทั้งหมดผู้ใช้กรอกเอง ไม่อนุมานราคา:

```bash
.venv/bin/python collector.py configure-subscription --provider gemini --fee-thb "$GEMINI_FEE_THB" --plan "AI Pro" --active-days 20
.venv/bin/python collector.py configure-business --month 2026-10 --revenue-thb "$REVENUE_THB" --profit-before-ai-thb "$PROFIT_THB"
```

ทุกคำสั่ง `configure-*` merge เข้า `settings.json` โดยไม่ลบ section อื่น
เจ้าที่จ่ายเงินแต่แทบไม่ได้ใช้ (quota สัปดาห์ < 5% หลังผ่านครึ่งรอบ หรือใช้ ≤ 2 วันใน 30 วัน)
ถูก flag `consider_pausing` รายละเอียดฟิลด์อยู่ใน [contract](contracts/ai-usage.v1.md)

## ความสดของข้อมูลและการแจ้งเตือน

snapshot เก่ากว่า 15 นาทีมีสถานะ `stale` และไม่สร้าง alert ใหม่
ข้อมูลที่ขาด, ค่าเปอร์เซ็นต์เสีย และโควตาที่ผ่านเวลารีเซ็ตจะไม่ถูกแปลงเป็นยอดเหลือที่ใช้ได้
timestamp การเก็บข้อมูลที่อยู่ในอนาคตเกิน 60 วินาทีถูกปฏิเสธ

alert เกิดเฉพาะโควตาที่มีข้อมูลสดและยังไม่ผ่านเวลารีเซ็ต:

- ใช้ตั้งแต่ 85% แต่ยังไม่ครบ: `quota_low`
- ใช้ครบ 100%: `quota_reached`
- เหลืออย่างน้อย 30% และใกล้รีเซ็ต: `use_before_reset` โดยใช้ช่วง 6 ชั่วโมงสำหรับรอบไม่เกิน 360 นาที หรือ 24 ชั่วโมงสำหรับรอบอย่างน้อย 10080 นาที

ไม่สร้าง `use_before_reset` ถ้าไม่ทราบระยะของรอบหรือเวลารีเซ็ต
alert id ผูกกับ provider, window, kind และรอบรีเซ็ต เพื่อให้ browser ตัดการเตือนซ้ำได้
backend ไม่ส่งการแจ้งเตือนออกภายนอก การแสดงและสิทธิ์ notification เป็นของ frontend

## Owner authorization และ readiness

production และ listener ที่ไม่ใช่ loopback ต้องมี `AI_USAGE_API_AUTH_ISSUER`,
`AI_USAGE_API_AUTH_PUBLIC_KEY` และ `AI_USAGE_API_AUTH_SUBJECTS` ตาม
[`vendor/service-auth`](vendor/service-auth/README.md)
อ่านข้อมูลด้วย RS256 JWT ที่มี `aud=ai-usage-api`, `scope=ai-usage:read`,
`tenant_id=owner` และ subject ที่อนุญาต อายุ token ไม่เกิน 120 วินาที
production ไม่ยอมรับ `AI_USAGE_API_TOKEN`; JWT configuration ที่ไม่ครบจะปิดการอ่าน

`/health` เป็น liveness สาธารณะ `/ready` ตรวจว่า projection ตอบได้และ owner JWT พร้อมใช้
service อาจพร้อมแม้ยังไม่มี provider ที่เชื่อมต่อ เพื่อให้ dashboard แสดง `not_connected` ได้
readiness จึงไม่ยืนยันว่ามีโควตาหรือข้อมูล provider สด local development ที่ไม่ได้ตั้ง JWT
จะตอบ `/ready` เป็น 503 พร้อม `auth=development` แต่ยังอ่านหน้า usage ได้ใน loopback
HTTP response ใช้ `Cache-Control: no-store` และ CORS allowlist

## ทดสอบและ build

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=vendor/service-auth:. python3 -m pytest tests
docker build -t solo-ai-usage .
```

image ไม่มี snapshot หรือ provider credentials ต้อง mount sanitized state แบบ read-only
และให้ owner auth configuration ตอน runtime collector รันในเครื่องที่มี provider CLI
[`service.yaml`](service.yaml) กำหนด deploy target เป็น `unassigned`; ยังไม่ได้ deploy service นี้
