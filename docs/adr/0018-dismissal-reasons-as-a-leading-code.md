# 0018: Dismissal reasons are a fixed vocabulary stored as a leading code

- **Status:** accepted for D3
- **Date:** 2026-11-03
- **Phase:** D3
- **Evidence:** `docs/DESKTOP-AUDIT-AND-PLAN.md` §10 ("false-positive review loop; structured dismiss reasons")

## Context

Dismissing an incident is the only measurement of "the system was wrong" this project has (06 §9). Until
now a dismissal carried a free-text reason, which cannot be counted, so the dismissal rate could say how
often but never why. The plan asks for structured reasons and a review loop whose output is a rate per
module version, with the interface still saying it is not a false-positive rate.

## Decision

- A fixed vocabulary lives in `vigil.domain.dismissal` (`nothing_there`, `harmless`, `authorised`,
  `camera_artifact`, `wrong_zone`, `duplicate`, `other`), served by `GET /incidents/dismissal-reasons` so no
  client invents its own.
- The chosen code is stored **at the front of the transition's existing reason text**, `[code] detail`.
  No schema change and no migration. A dismissal made without a code, by an older client or by the
  engine, reads as `not_given` and is still counted.
- The console's dismiss prompt requires a choice. The API does **not**: a dismissal without a code is still
  accepted, because the lifecycle must not depend on a client having a vocabulary.
- Analytics adds `dismissal_reasons`, a count by code over the dismissed incidents in the window, with its
  own definition: "the operator's label, not a verified cause".

## Consequences

- The dismissal rate per module version and the reasons behind it now come from the same operator action.
- It is still **not a false-positive rate**. It omits every false positive nobody dismissed and every true
  positive nobody acknowledged, and a reason is a label an operator picked, not something checked against the
  picture. The Reports screen says so beside the table.
- The code lives in free text, so a hand-written reason that begins `[harmless]` is read as that code. The
  vocabulary is closed, so anything else is `not_given`; the case is covered by a test.
- Changing a code's meaning later would silently change old counts. Codes are therefore never renamed or
  reused; a new meaning gets a new code.

## Rejected alternatives

- **A `reason_code` column.** Cleaner, and a migration on the incident store for a field only analytics
  reads. Worth doing if codes ever need joins or indexes; not for counting a few thousand rows.
- **Making the API require a code.** It would make every existing caller and test fail for a label, and
  would let a missing vocabulary block closing an incident.
- **Free-text mining.** Counting words in reasons would look like a measurement and be a guess.
