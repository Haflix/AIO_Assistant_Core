# Home Assistant Database Plan (PostgreSQL + MinIO)

This is a **practical schema plan** for a “paperwork brain”:

- **PostgreSQL** stores structured data, relationships, extracted text, and search indexes.
- **MinIO** stores the heavy binary objects (PDF scans, email EML files, images, etc.).

Design goals:

- **Simple interaction**: predictable tables, few “magic” fields, no over-normalization.
- **Real-world robustness**: handles partial payments, multiple attachments, contact changes, document versions.
- **Good searchability**: keywords, metadata filters, and optional full-text search.
- **Accessible**: you can add items manually without needing the AI pipeline to be perfect.

---

## High-level approach (important)

You mentioned “lists of IDs in PostgreSQL”. PostgreSQL *can* store arrays of UUIDs, but for core relations it becomes painful:

- You can’t easily enforce foreign keys.
- Deleting/merging people/places becomes error-prone.
- Many queries get slower and harder to write.

So this plan uses **simple join tables** for real relationships (document↔people, person↔places, payment↔documents, etc.).  
We still use **`jsonb`** where it actually makes life easier (AI-extracted metadata, device capabilities, OCR fields).

---

## Core entities you asked for

### Documents & Emails
One table for all “items”, and specialized tables where needed.

- **`documents`**: one row per logical document (invoice, contract, medical report, email, …).
- **`document_blobs`**: one row per stored file version/object in MinIO (PDF, JPG, EML, …).
- **`document_links`**: optional graph between documents (invoice ↔ contract, “replaces”, “part of”, etc.).
- **`emails`**: email-specific fields (from/to/subject/message-id) for documents of type `email`.
- **`email_recipients`**: multiple recipients (To/Cc/Bcc).
- **`email_attachments`**: attachment documents linked to the email document.
- **`document_text`** (optional but recommended): extracted/OCR text + full-text index.

Relationships:

- Document ↔ People: `document_people`
- Document ↔ Places: `document_places`
- Document ↔ Tags: `document_tags`

### People

- **`people`**: human entities (you, girlfriend, doctors, landlords, …).
- People ↔ Places: `person_places` (works_at / patient_at / lives_at / customer_of / …)
- People contact info: via `contact_points` + `person_contact_points`

### Places

- **`places`**: organizations or physical places (doctor office, insurance, furniture shop, …).
- Place contact info: via `contact_points` + `place_contact_points`
- Addresses: `addresses` + `place_addresses` (and optionally `person_addresses`)

### Contact info
Unified “contact point” table that can attach to people or places:

- **`contact_points`**: phone/email/website/fax/IBAN/etc.
- Join tables link contact points to either people or places.

### Payments

- **`payments`**: one payment event (bank transfer, cash, card).
- **`payment_allocations`**: link payments to documents *with an amount*.
  - This fixes the classic failure case: partial payment or one transfer paying multiple invoices.

### Appointments

- **`appointments`**: events with start/end, timezone, status.
- Link tables: `appointment_people`, `appointment_places`.
- Optional link to documents/notes via `appointment_documents`, `appointment_notes`.

### Random knowledge & notes

- **`notes`**: your text notes.
- **`note_categories`**: simple category system (medical, finance, home, …).
- Notes can link to people/places/documents/devices via join tables.

### Home devices

- **`devices`**: device inventory (IR devices, smart plugs, sensors, etc.)
- **`device_identifiers`**: MAC/IP/serial/model-specific IDs (many per device).
- **`device_states`**: current state snapshot (jsonb).
- **`device_events`** (optional): history log (state changes, commands).

---

## “Keep it simple” schema rules (so it stays usable)

- **Use UUID primary keys everywhere** (easy merging, safe remote sync).
- **Use `created_at`, `updated_at`, `deleted_at`** on everything that’s user-facing.
  - Soft delete prevents “oops I deleted the wrong doctor”.
- **Use lookup tables instead of massive enums** for anything you’ll extend (document types, tags, categories).
- **Don’t store big binaries in Postgres** (MinIO holds blobs; Postgres stores pointers).
- **Use `jsonb` only for “unknown shape” data** (AI metadata, OCR confidence, device capabilities).

---

## Data model overview (text diagram)

Primary hubs:

- `documents` is the hub for paperwork and email.
- `people` + `places` are hubs for who/where.
- `payments` is a hub for money flows.
- `appointments` is a hub for scheduling.
- `notes` is a hub for your personal knowledge.
- `devices` is a hub for home automation inventory.

Key join tables:

- `document_people`, `document_places`, `document_tags`
- `person_places`
- `payment_allocations`
- `appointment_people`, `appointment_places`
- `note_people`, `note_places`, `note_documents`, `note_devices`

---

## Scenarios that usually break naïve plans (and how this plan handles them)

### 1) One bank transfer pays multiple invoices
Naïve DB: `payments.document_id` (fails).

Solution here:

- `payment_allocations(payment_id, document_id, amount)`

### 2) One invoice is paid in multiple partial payments
Naïve DB: `documents.paid_amount` (becomes wrong over time).

Solution:

- Multiple `payment_allocations` rows can point to the same document.

### 3) Email with many attachments
Naïve DB: store attachments as files in an array field (hard to search/relate).

Solution:

- Each attachment is its own `documents` row; link via `email_attachments`.

### 4) A contract gets an addendum / new signed scan version
Naïve DB: overwrite the blob reference (lose history).

Solution:

- Multiple `document_blobs` per `documents` row, with `is_primary` and versioning fields.

### 5) Doctor changes address/phone
Naïve DB: “just update place.phone” (lose history and makes old documents confusing).

Solution:

- `contact_points` and `addresses` support `valid_from/valid_to` on the link rows.

### 6) One person has multiple roles (landlord + employer)
Solution:

- Relationship type lives on `person_places.relationship_type`.

### 7) You want privacy separation (you vs girlfriend ownership)
Keep it simple:

- `documents.owner_person_id` and `notes.owner_person_id` identify ownership.
- You *can later* add row-level security or “households” without redesign.

Shared ownership without extra tables:

- Keep **one primary** `owner_person_id`.
- Add secondary owners using `document_people.role = 'owner'` if you need it.

### 8) Searching for “all documents containing keyword X about doctor Y”
Solution:

- Full-text search in `document_text` + join through `document_people`.

### 9) German-specific things (IBAN, insurance numbers, tax IDs)
Solution:

- `contact_points` can store typed values (e.g. `iban`) and you can add validation in the application.
- For document metadata, store extracted identifiers in `documents.metadata jsonb` as well.

### 10) You scan the same document twice (duplicates)
Solution:

- Store content hashes on `document_blobs` (`sha256`) to detect duplicates.
- Optionally store `document_text.sha256` too.

---

## What’s intentionally NOT included (to avoid complexity)

- A fully normalized accounting system (ledger, charts of accounts).
- A full CRM model with companies, departments, positions.
- A complex permissions model (can be added later).
- A complex “graph database” approach (Postgres joins are enough).

---

## Additional “stress test” scenarios (where you might extend later)

These are real cases that can appear over years. The schema can handle them, but you may want small additions depending on how far you go.

### A) You want multiple “profiles/households” (multi-tenant)
If later you store data for multiple people/households beyond “me + girlfriend”, adding a table like `households` and making `owner_household_id` the primary scope can help.

Current mitigation:

- `owner_person_id` works fine for 1–2 owners.

### B) GDPR / privacy / “right to be forgotten”
If you need to selectively wipe a person, you’ll want a clear policy:

- keep document records but anonymize links, or
- hard delete everything linked to that person.

Current mitigation:

- join tables make it possible to delete links cleanly.
- `deleted_at` supports “soft delete first”.

### C) Encryption at rest for very sensitive metadata
If you store medical identifiers or bank details in `metadata jsonb`, you may want application-level encryption (or pgcrypto with careful key management).

Current mitigation:

- keep truly sensitive secrets out of the DB where possible; store references instead.

### D) Very large OCR text (performance / storage)
If you end up storing hundreds of thousands of pages, `document_text.text_content` can grow.

Options:

- keep only `search_vector` + a short snippet in Postgres, put full OCR text in MinIO
- or add a separate search engine later (OpenSearch/Elasticsearch)

### E) Merging duplicates (same doctor entered twice)
You’ll eventually want a “merge person/place” operation.

Current mitigation:

- UUID PKs + join tables make merges straightforward (update FK references).

### F) “One PDF contains multiple logical documents”
Example: a scan batch that includes 3 invoices in one PDF.

Options:

- create 3 `documents` that share one `document_blob` (requires allowing blob reuse), or
- store 1 blob but create 3 documents and split later (requires either duplication or a “page range” model).

If this becomes common, extend `document_blobs` with `page_count` and create `document_blob_slices(document_id, blob_id, page_from, page_to)`.

### G) Tracking processing state (OCR done? extracted? verified?)
If you want strong pipeline state tracking, add a small `document_processing` table.

Current mitigation:

- use `documents.metadata` fields like `{"ocr":{"status":"done","confidence":0.92}}`.

### H) Conflicting timestamps / timezones (especially emails and appointments)
German DST changes can create confusing appointment times.

Current mitigation:

- appointments store `timestamptz` + explicit `timezone` label.

### I) MinIO object lifecycle / versioning
If blobs are replaced, you may want MinIO versioning enabled so `document_blobs.version_id` stays meaningful.

Current mitigation:

- schema already stores `version_id` and `etag` so you can verify integrity.

---

## Next step in repo

See `docs/database/schema.sql` for a concrete SQL schema draft that implements this plan.

---

## Next step in repo

See `docs/database/schema.sql` for a concrete SQL schema draft that implements this plan.

