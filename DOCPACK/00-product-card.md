---
title: solo-ai-usage — Product Card
doc_id: CARD-solo-ai-usage
doc_type: product-card
verdict: showcase
version: 0.1
date: 2026-10-11
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# solo-ai-usage — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

แสดงโควตา Claude/Codex เวลารีเซ็ต และการแจ้งเตือน ให้เจ้าของเห็นก่อนใช้โควตาหมด

## 2. ปัญหาและผู้ใช้เป้าหมาย

โควตา AI subscription หมดโดยไม่รู้ตัวทำให้งาน agent สะดุด ผู้ใช้คือเจ้าของ

## 3. สิ่งที่ทำได้จริงตอนนี้

- `server.py` (API read-only) และ `collector.py` (อ่านโควตา รวม Claude statusline hook)
- contract `contracts/ai-usage.v1.md`
- รันเป็น systemd user unit ในเครื่อง

## 4. หลักฐานสถานะ

| รายการ | ค่า |
|---|---|
| Stack | Python |
| Commit | 3 ครั้ง (2026-10-04 ถึง 2026-10-11) |
| ซอร์สโค้ด | 8 ไฟล์ / 1547 บรรทัด |
| เทสต์ | 4 ไฟล์ (ไม่ได้รันในงานนี้) |
| CI | ไม่มี workflow |
| Deploy config | `Dockerfile` และ `systemd/` ตัวอย่าง; ทะเบียนระบุ `local` |
| Live URL | ไม่มี (รันในเครื่อง) |

## 5. คุณค่าและจุดต่าง

- แก้ปัญหาเฉพาะของเจ้าของ ไม่มีทางเลือกสำเร็จรูปที่ผูกกับ statusline hook
- **[สมมติฐาน]** ไม่มีมูลค่าขาย

## 6. ช่องว่างสำคัญ

- ผูกกับโครงสร้าง CLI ของผู้ให้บริการ — เปลี่ยนรูปแบบเมื่อไรก็พัง
- ไม่มี CI และ deploy ออนไลน์ (ตั้งใจ local)

## 7. คำตัดสิน

**`showcase`** — เครื่องมือส่วนตัวที่ทำงานได้ แต่ไม่ใช่ธุรกิจ จะเปลี่ยนเป็น `invest` หากมีผู้ใช้อื่นที่ต้องการดูโควตาแบบเดียวกัน

## 8. ขั้นต่อไป

1. เก็บ README ให้ตรงกับ unit ที่ใช้จริง

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `server.py`, `collector.py`, `contracts/`
