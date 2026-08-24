---
name: attio-crm-operations
description: >
  Binding value-format invariants for Attio CRM attribute writes — record-reference,
  domains, location, and team attributes — derived from observed API 400 failures.
---

# Attio CRM Operations — SOP & Guardrails

> **Executor:** Aurther | **Tools:** 25 Attio tools (People, Companies, Lists, Entries, Notes, Tasks, Comments, Interactions, Members)

---

# ⛔ HARD GUARDRAILS & ERROR RECOVERY MATRIX (P0)

Each entry below is a real failure and the invariant that prevents it. Apply the
stated format exactly — Attio rejects loosely-typed attribute values with a 400.

[GUARDRAIL E-01] Failure: Attio API error (400): An invalid value was passed to attribute with slug "company". | Root Cause: Attempted to update person record's company attribute with a JSON value that did not match Attio's expected format for record-reference attributes. | Binding Invariant: For record-reference attributes, the value must be a list containing an object with 'id' (UUID) and optionally 'name' or other fields, but Attio expects a specific structure. The correct format is: [{"id": "<uuid>"}].

[GUARDRAIL E-02] Failure: Attio API error (400): An invalid value was passed to attribute with slug "domains". | Root Cause: Attempted to update company record's domains attribute with a string value instead of a list of domain objects. | Binding Invariant: The domains attribute expects a list of objects, each with a 'value' field containing the domain string. Correct format: [{"value": "acmeunited.com"}].

[GUARDRAIL E-03] Failure: Attio API error (400): An invalid value was passed to attribute with slug "primary_location". | Root Cause: Attempted to update company record's primary_location attribute with a string value instead of a location object. | Binding Invariant: The primary_location attribute expects a list containing a single object with location subfields (line_1, locality, region, postcode, country_code, etc.). Correct format: [{"line_1": "1 Waterview Drive", "locality": "Shelton", "region": "Connecticut", "postcode": "06484", "country_code": "US"}].

[GUARDRAIL E-04] Failure: Attio API error (400): An invalid value was passed to attribute with slug "team". | Root Cause: Attempted to update company record's team attribute with a string value instead of a record-reference object. | Binding Invariant: The team attribute expects a list containing an object with 'id' (UUID) of the person record. Correct format: [{"id": "<person_uuid>"}].
