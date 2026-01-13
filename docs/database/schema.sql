-- Home Assistant Database Schema (PostgreSQL)
-- Designed for: documents + people/places + payments + appointments + notes + devices
-- Storage: binaries in MinIO, metadata/search in PostgreSQL

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Common helpers ----------

-- Update updated_at automatically
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Keep tsvector in sync for document_text
CREATE OR REPLACE FUNCTION document_text_set_search_vector()
RETURNS TRIGGER AS $$
DECLARE
  cfg regconfig;
BEGIN
  -- Map common language codes to PostgreSQL text search configs
  IF NEW.language IN ('de', 'de-DE', 'de_DE') THEN
    cfg := 'german'::regconfig;
  ELSIF NEW.language IN ('en', 'en-US', 'en_GB', 'en-GB') THEN
    cfg := 'english'::regconfig;
  ELSE
    cfg := 'simple'::regconfig;
  END IF;

  NEW.search_vector := to_tsvector(cfg, COALESCE(NEW.text_content, ''));
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------- Lookup tables ----------

CREATE TABLE IF NOT EXISTS document_types (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key text NOT NULL UNIQUE,     -- e.g. invoice, contract, medical, email, other
  label text NOT NULL,          -- human-readable
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_document_types_updated_at
BEFORE UPDATE ON document_types
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_tags_updated_at
BEFORE UPDATE ON tags
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS note_categories (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_note_categories_updated_at
BEFORE UPDATE ON note_categories
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- People / Places / Contact ----------

CREATE TABLE IF NOT EXISTS people (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name text NOT NULL,               -- "Dr. Max Mustermann", "Girlfriend", etc.
  given_name text,
  family_name text,
  birth_date date,
  is_self boolean NOT NULL DEFAULT false,   -- mark "me"
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_people_display_name ON people USING btree (display_name);
CREATE INDEX IF NOT EXISTS idx_people_deleted_at ON people USING btree (deleted_at);

CREATE TRIGGER trg_people_updated_at
BEFORE UPDATE ON people
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS places (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name text NOT NULL,        -- "Hausarztpraxis X", "IKEA Köln", "AOK", ...
  place_type text,                   -- office, doctor_office, shop, insurer, other (keep flexible)
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_places_display_name ON places USING btree (display_name);
CREATE INDEX IF NOT EXISTS idx_places_place_type ON places USING btree (place_type);
CREATE INDEX IF NOT EXISTS idx_places_deleted_at ON places USING btree (deleted_at);

CREATE TRIGGER trg_places_updated_at
BEFORE UPDATE ON places
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Relationship between people and places (N:M)
CREATE TABLE IF NOT EXISTS person_places (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id uuid NOT NULL REFERENCES people(id),
  place_id uuid NOT NULL REFERENCES places(id),
  relationship_type text NOT NULL,     -- works_at, patient_at, lives_at, customer_of, ...
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_person_places_rel
ON person_places(person_id, place_id, relationship_type, COALESCE(valid_from, date '0001-01-01'), COALESCE(valid_to, date '9999-12-31'));

CREATE INDEX IF NOT EXISTS idx_person_places_person ON person_places(person_id);
CREATE INDEX IF NOT EXISTS idx_person_places_place ON person_places(place_id);

CREATE TRIGGER trg_person_places_updated_at
BEFORE UPDATE ON person_places
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Addresses are kept structured for easy filtering
CREATE TABLE IF NOT EXISTS addresses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  country_code char(2) NOT NULL DEFAULT 'DE',
  postal_code text,
  city text,
  street text,
  house_number text,
  addition text,         -- building/apt/etc
  raw text,              -- optionally store original string
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_addresses_postal_city ON addresses(postal_code, city);

CREATE TRIGGER trg_addresses_updated_at
BEFORE UPDATE ON addresses
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS place_addresses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  place_id uuid NOT NULL REFERENCES places(id),
  address_id uuid NOT NULL REFERENCES addresses(id),
  address_type text NOT NULL DEFAULT 'main', -- billing, shipping, office, practice, ...
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_place_addresses_place ON place_addresses(place_id);
CREATE INDEX IF NOT EXISTS idx_place_addresses_address ON place_addresses(address_id);

CREATE TRIGGER trg_place_addresses_updated_at
BEFORE UPDATE ON place_addresses
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS person_addresses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id uuid NOT NULL REFERENCES people(id),
  address_id uuid NOT NULL REFERENCES addresses(id),
  address_type text NOT NULL DEFAULT 'home', -- home, billing, old_home, ...
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_person_addresses_person ON person_addresses(person_id);
CREATE INDEX IF NOT EXISTS idx_person_addresses_address ON person_addresses(address_id);

CREATE TRIGGER trg_person_addresses_updated_at
BEFORE UPDATE ON person_addresses
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Generic contact points (attach to person or place via join tables)
CREATE TABLE IF NOT EXISTS contact_points (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_type text NOT NULL,   -- email, phone, website, fax, iban, bic, customer_id, ...
  value text NOT NULL,
  label text,                  -- e.g. "office", "private", "billing"
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_contact_points_type ON contact_points(contact_type);
CREATE INDEX IF NOT EXISTS idx_contact_points_value ON contact_points(value);

CREATE TRIGGER trg_contact_points_updated_at
BEFORE UPDATE ON contact_points
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS person_contact_points (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  person_id uuid NOT NULL REFERENCES people(id),
  contact_point_id uuid NOT NULL REFERENCES contact_points(id),
  is_preferred boolean NOT NULL DEFAULT false,
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_person_contact_points_person ON person_contact_points(person_id);
CREATE INDEX IF NOT EXISTS idx_person_contact_points_cp ON person_contact_points(contact_point_id);

CREATE TRIGGER trg_person_contact_points_updated_at
BEFORE UPDATE ON person_contact_points
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS place_contact_points (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  place_id uuid NOT NULL REFERENCES places(id),
  contact_point_id uuid NOT NULL REFERENCES contact_points(id),
  is_preferred boolean NOT NULL DEFAULT false,
  valid_from date,
  valid_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_place_contact_points_place ON place_contact_points(place_id);
CREATE INDEX IF NOT EXISTS idx_place_contact_points_cp ON place_contact_points(contact_point_id);

CREATE TRIGGER trg_place_contact_points_updated_at
BEFORE UPDATE ON place_contact_points
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Documents / Emails ----------

CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text,                                  -- optional human-friendly title
  document_type_id uuid REFERENCES document_types(id),
  owner_person_id uuid REFERENCES people(id),  -- e.g. you or girlfriend
  created_on date,                             -- "date of creation" (from the document)
  saved_at timestamptz NOT NULL DEFAULT now(), -- date of saving into the system
  source text,                                 -- scan, email, upload, import, ...
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,  -- AI extracted metadata / OCR info / identifiers
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(document_type_id);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_person_id);
CREATE INDEX IF NOT EXISTS idx_documents_created_on ON documents(created_on);
CREATE INDEX IF NOT EXISTS idx_documents_saved_at ON documents(saved_at);
CREATE INDEX IF NOT EXISTS idx_documents_deleted_at ON documents(deleted_at);
CREATE INDEX IF NOT EXISTS idx_documents_metadata_gin ON documents USING gin (metadata);

CREATE TRIGGER trg_documents_updated_at
BEFORE UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Store MinIO pointers (and allow multiple versions/files per document)
CREATE TABLE IF NOT EXISTS document_blobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  bucket text NOT NULL,
  object_key text NOT NULL,
  version_id text,
  etag text,
  sha256 text,                -- for dedupe detection
  content_type text,
  file_name text,
  size_bytes bigint,
  is_primary boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_blobs_primary
ON document_blobs(document_id)
WHERE is_primary;

CREATE INDEX IF NOT EXISTS idx_document_blobs_doc ON document_blobs(document_id);
CREATE INDEX IF NOT EXISTS idx_document_blobs_sha256 ON document_blobs(sha256);

CREATE TRIGGER trg_document_blobs_updated_at
BEFORE UPDATE ON document_blobs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Optional links between documents (invoice <-> contract, replaces, evidence_for, etc.)
CREATE TABLE IF NOT EXISTS document_links (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  from_document_id uuid NOT NULL REFERENCES documents(id),
  to_document_id uuid NOT NULL REFERENCES documents(id),
  link_type text NOT NULL,  -- related_to, replaces, supersedes, part_of, evidence_for, ...
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_links
ON document_links(from_document_id, to_document_id, link_type);

CREATE INDEX IF NOT EXISTS idx_document_links_from ON document_links(from_document_id);
CREATE INDEX IF NOT EXISTS idx_document_links_to ON document_links(to_document_id);

CREATE TRIGGER trg_document_links_updated_at
BEFORE UPDATE ON document_links
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Document <-> People
CREATE TABLE IF NOT EXISTS document_people (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  person_id uuid NOT NULL REFERENCES people(id),
  role text,                         -- author, recipient, patient, doctor, payer, landlord, ...
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_people
ON document_people(document_id, person_id, COALESCE(role, ''));

CREATE INDEX IF NOT EXISTS idx_document_people_doc ON document_people(document_id);
CREATE INDEX IF NOT EXISTS idx_document_people_person ON document_people(person_id);

CREATE TRIGGER trg_document_people_updated_at
BEFORE UPDATE ON document_people
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Document <-> Places
CREATE TABLE IF NOT EXISTS document_places (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  place_id uuid NOT NULL REFERENCES places(id),
  role text,                         -- issuer, provider, store, office, ...
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_places
ON document_places(document_id, place_id, COALESCE(role, ''));

CREATE INDEX IF NOT EXISTS idx_document_places_doc ON document_places(document_id);
CREATE INDEX IF NOT EXISTS idx_document_places_place ON document_places(place_id);

CREATE TRIGGER trg_document_places_updated_at
BEFORE UPDATE ON document_places
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Document <-> Tags
CREATE TABLE IF NOT EXISTS document_tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(id),
  tag_id uuid NOT NULL REFERENCES tags(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_document_tags
ON document_tags(document_id, tag_id);

CREATE INDEX IF NOT EXISTS idx_document_tags_doc ON document_tags(document_id);
CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag_id);

CREATE TRIGGER trg_document_tags_updated_at
BEFORE UPDATE ON document_tags
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Optional extracted text for searching (usually much smaller than PDFs)
-- If you do not want to store full text, you can still store a small snippet and keywords.
CREATE TABLE IF NOT EXISTS document_text (
  document_id uuid PRIMARY KEY REFERENCES documents(id),
  language text,                      -- de, en, ...
  text_content text,                  -- OCR/extracted text
  text_sha256 text,
  search_vector tsvector,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_document_text_vector ON document_text USING gin (search_vector);
CREATE INDEX IF NOT EXISTS idx_document_text_sha256 ON document_text(text_sha256);

CREATE TRIGGER trg_document_text_set_search_vector
BEFORE INSERT OR UPDATE ON document_text
FOR EACH ROW EXECUTE FUNCTION document_text_set_search_vector();

CREATE TRIGGER trg_document_text_updated_at
BEFORE UPDATE ON document_text
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Email fields for documents of type "email"
CREATE TABLE IF NOT EXISTS emails (
  document_id uuid PRIMARY KEY REFERENCES documents(id),
  message_id text,                -- RFC message-id
  subject text,
  from_address text,
  sent_at timestamptz,
  received_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent_at ON emails(sent_at);

CREATE TRIGGER trg_emails_updated_at
BEFORE UPDATE ON emails
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS email_recipients (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email_document_id uuid NOT NULL REFERENCES emails(document_id),
  recipient_type text NOT NULL,   -- to, cc, bcc
  address text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_email_recipients_email ON email_recipients(email_document_id);
CREATE INDEX IF NOT EXISTS idx_email_recipients_address ON email_recipients(address);

CREATE TRIGGER trg_email_recipients_updated_at
BEFORE UPDATE ON email_recipients
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Each attachment is itself a document; link it to the email
CREATE TABLE IF NOT EXISTS email_attachments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email_document_id uuid NOT NULL REFERENCES emails(document_id),
  attachment_document_id uuid NOT NULL REFERENCES documents(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_email_attachments
ON email_attachments(email_document_id, attachment_document_id);

CREATE INDEX IF NOT EXISTS idx_email_attachments_email ON email_attachments(email_document_id);
CREATE INDEX IF NOT EXISTS idx_email_attachments_attachment ON email_attachments(attachment_document_id);

CREATE TRIGGER trg_email_attachments_updated_at
BEFORE UPDATE ON email_attachments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Payments ----------

CREATE TABLE IF NOT EXISTS payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_person_id uuid REFERENCES people(id), -- whose money account (optional)
  counterparty_person_id uuid REFERENCES people(id),
  counterparty_place_id uuid REFERENCES places(id),
  payment_date date NOT NULL,
  amount_cents bigint NOT NULL CHECK (amount_cents <> 0), -- allow negative for refunds
  currency_code char(3) NOT NULL DEFAULT 'EUR',
  method text,                                -- bank_transfer, cash, card, paypal, ...
  reference text,                             -- "Verwendungszweck"
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb, -- e.g. bank account details, transaction id
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(payment_date);
CREATE INDEX IF NOT EXISTS idx_payments_owner ON payments(owner_person_id);
CREATE INDEX IF NOT EXISTS idx_payments_counterparty_person ON payments(counterparty_person_id);
CREATE INDEX IF NOT EXISTS idx_payments_counterparty_place ON payments(counterparty_place_id);
CREATE INDEX IF NOT EXISTS idx_payments_deleted_at ON payments(deleted_at);
CREATE INDEX IF NOT EXISTS idx_payments_metadata_gin ON payments USING gin (metadata);

CREATE TRIGGER trg_payments_updated_at
BEFORE UPDATE ON payments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Link payments to documents with an allocated amount (supports split and partial payments)
CREATE TABLE IF NOT EXISTS payment_allocations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  payment_id uuid NOT NULL REFERENCES payments(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  amount_cents bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payment_allocations_payment ON payment_allocations(payment_id);
CREATE INDEX IF NOT EXISTS idx_payment_allocations_document ON payment_allocations(document_id);

CREATE TRIGGER trg_payment_allocations_updated_at
BEFORE UPDATE ON payment_allocations
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Appointments ----------

CREATE TABLE IF NOT EXISTS appointments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text NOT NULL,
  description text,
  starts_at timestamptz NOT NULL,
  ends_at timestamptz,
  timezone text NOT NULL DEFAULT 'Europe/Berlin',
  status text NOT NULL DEFAULT 'planned',     -- planned, confirmed, cancelled, done
  owner_person_id uuid REFERENCES people(id),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_appointments_starts ON appointments(starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_owner ON appointments(owner_person_id);
CREATE INDEX IF NOT EXISTS idx_appointments_deleted_at ON appointments(deleted_at);

CREATE TRIGGER trg_appointments_updated_at
BEFORE UPDATE ON appointments
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS appointment_people (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  appointment_id uuid NOT NULL REFERENCES appointments(id),
  person_id uuid NOT NULL REFERENCES people(id),
  role text,   -- attendee, doctor, patient, ...
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_people
ON appointment_people(appointment_id, person_id, COALESCE(role, ''));

CREATE INDEX IF NOT EXISTS idx_appointment_people_appt ON appointment_people(appointment_id);
CREATE INDEX IF NOT EXISTS idx_appointment_people_person ON appointment_people(person_id);

CREATE TRIGGER trg_appointment_people_updated_at
BEFORE UPDATE ON appointment_people
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS appointment_places (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  appointment_id uuid NOT NULL REFERENCES appointments(id),
  place_id uuid NOT NULL REFERENCES places(id),
  role text,   -- location, provider, ...
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_appointment_places
ON appointment_places(appointment_id, place_id, COALESCE(role, ''));

CREATE INDEX IF NOT EXISTS idx_appointment_places_appt ON appointment_places(appointment_id);
CREATE INDEX IF NOT EXISTS idx_appointment_places_place ON appointment_places(place_id);

CREATE TRIGGER trg_appointment_places_updated_at
BEFORE UPDATE ON appointment_places
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Notes / Knowledge ----------

CREATE TABLE IF NOT EXISTS notes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_person_id uuid REFERENCES people(id),
  category_id uuid REFERENCES note_categories(id),
  title text,
  content text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_notes_owner ON notes(owner_person_id);
CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category_id);
CREATE INDEX IF NOT EXISTS idx_notes_deleted_at ON notes(deleted_at);
CREATE INDEX IF NOT EXISTS idx_notes_metadata_gin ON notes USING gin (metadata);

CREATE TRIGGER trg_notes_updated_at
BEFORE UPDATE ON notes
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS note_tags (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES notes(id),
  tag_id uuid NOT NULL REFERENCES tags(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_note_tags
ON note_tags(note_id, tag_id);

CREATE INDEX IF NOT EXISTS idx_note_tags_note ON note_tags(note_id);
CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag_id);

CREATE TRIGGER trg_note_tags_updated_at
BEFORE UPDATE ON note_tags
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Link notes to other entities (optional but very useful)
CREATE TABLE IF NOT EXISTS note_people (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES notes(id),
  person_id uuid NOT NULL REFERENCES people(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_note_people ON note_people(note_id, person_id);
CREATE TRIGGER trg_note_people_updated_at
BEFORE UPDATE ON note_people
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS note_places (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES notes(id),
  place_id uuid NOT NULL REFERENCES places(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_note_places ON note_places(note_id, place_id);
CREATE TRIGGER trg_note_places_updated_at
BEFORE UPDATE ON note_places
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS note_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note_id uuid NOT NULL REFERENCES notes(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_note_documents ON note_documents(note_id, document_id);
CREATE TRIGGER trg_note_documents_updated_at
BEFORE UPDATE ON note_documents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------- Devices ----------

CREATE TABLE IF NOT EXISTS devices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  display_name text NOT NULL,
  device_type text NOT NULL,                 -- ir_blaster, tv, ac, sensor, plug, ...
  manufacturer text,
  model text,
  place_id uuid REFERENCES places(id),       -- where it is located (optional)
  capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_devices_place ON devices(place_id);
CREATE INDEX IF NOT EXISTS idx_devices_type ON devices(device_type);
CREATE INDEX IF NOT EXISTS idx_devices_deleted_at ON devices(deleted_at);
CREATE INDEX IF NOT EXISTS idx_devices_capabilities_gin ON devices USING gin (capabilities);

CREATE TRIGGER trg_devices_updated_at
BEFORE UPDATE ON devices
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS device_identifiers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id uuid NOT NULL REFERENCES devices(id),
  identifier_type text NOT NULL,  -- mac, ip, serial, zigbee_ieee, mqtt_topic, ...
  identifier_value text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_device_identifiers
ON device_identifiers(identifier_type, identifier_value);
CREATE INDEX IF NOT EXISTS idx_device_identifiers_device ON device_identifiers(device_id);

CREATE TRIGGER trg_device_identifiers_updated_at
BEFORE UPDATE ON device_identifiers
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Current state snapshot (one row per device)
CREATE TABLE IF NOT EXISTS device_states (
  device_id uuid PRIMARY KEY REFERENCES devices(id),
  state jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_device_states_state_gin ON device_states USING gin (state);

-- Optional event log (state changes, actions, errors)
CREATE TABLE IF NOT EXISTS device_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  device_id uuid NOT NULL REFERENCES devices(id),
  event_type text NOT NULL,                 -- state_change, command, error, heartbeat
  event jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_device_events_device ON device_events(device_id);
CREATE INDEX IF NOT EXISTS idx_device_events_created_at ON device_events(created_at);
CREATE INDEX IF NOT EXISTS idx_device_events_event_gin ON device_events USING gin (event);

